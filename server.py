"""
LifeUp 管理面板 - 后端服务
直接读写 LifeUp 备份存档(.zip)中的 SQLite 数据库
"""
import zipfile, sqlite3, os, tempfile, shutil, json, time, hashlib, secrets, threading, stat, re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

app = Flask(__name__, static_folder='.')
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024


@app.errorhandler(413)
def request_entity_too_large(_error):
    limit_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return jsonify({
        'code': 'REQUEST_TOO_LARGE',
        'error': f'上传文件过大（请求最大 {limit_mb} MB）',
        'suggestion': '请选择 LifeUp App 直接导出的 ZIP 备份；如果备份确实很大，请先减少无关媒体文件。'
    }), 413

# 全局状态
STATE = {
    'backup_path': None,
    'db_path': None,
    'tmpdir': None,
    'loaded': False
}

DB_INTERNAL = 'databases/LifeUpDB.db'
BROWSER_IMPORT_DIR = os.path.join(os.path.dirname(__file__), 'workspaces', 'browser-imports')
EXPORT_DIR = os.path.join(os.path.dirname(__file__), 'exports')
SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), 'workspaces', 'snapshots')
RESTORE_DIR = os.path.join(os.path.dirname(__file__), 'workspaces', 'restores')
ORIGINAL_BACKUP_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'LifeupBackup.zip'))
PROTECTED_BACKUP_PATHS = {os.path.normcase(os.path.realpath(ORIGINAL_BACKUP_PATH))}
STATE_LOCK = threading.RLock()
KEY_ENTITY_TABLES = ('taskmodel', 'shopitemmodel', 'userachievementmodel')
SNAPSHOT_ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
SNAPSHOT_FILENAME_PATTERN = re.compile(r'^snapshot-([0-9a-f]{32})\.zip$')
SNAPSHOT_COMMENT_PREFIX = b'LIFEUP_DASHBOARD_SNAPSHOT_V1\n'
MAX_SNAPSHOT_NAME_LENGTH = 100
MAX_SNAPSHOT_LIST_LIMIT = 200
MAX_BATCH_SIZE = 200
MAX_ITEM_PRICE = 2_147_483_647
MAX_BACKUP_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_BACKUP_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_BACKUP_MEMBER_BYTES = 512 * 1024 * 1024
MAX_BACKUP_ENTRIES = 20000
WINDOWS_RESERVED_NAMES = {
    'con', 'prn', 'aux', 'nul',
    *(f'com{number}' for number in range(1, 10)),
    *(f'lpt{number}' for number in range(1, 10))
}


class BackupValidationError(ValueError):
    def __init__(self, message, suggestion):
        super().__init__(message)
        self.suggestion = suggestion


class BackupExportError(RuntimeError):
    def __init__(self, code, message, suggestion, status=500):
        super().__init__(message)
        self.code = code
        self.suggestion = suggestion
        self.status = status


class SnapshotError(BackupExportError):
    """Stable API error for managed snapshot operations."""


class BatchValidationError(ValueError):
    """Client supplied an invalid local batch operation."""


def _export_error_details(exc):
    return {
        'code': exc.code,
        'error': str(exc),
        'suggestion': exc.suggestion,
    }


def _backup_error_details(exc):
    return {
        'error': str(exc),
        'suggestion': getattr(
            exc,
            'suggestion',
            '请检查文件是否为 LifeUp App 直接导出的完整 ZIP 备份。'
        )
    }


def _safe_archive_member_name(info):
    raw_name = str(info.filename or '')
    normalized = raw_name.replace('\\', '/')
    parts = [part for part in normalized.split('/') if part not in ('', '.')]
    unsafe_windows_name = any(
        part != part.rstrip(' .')
        or part.rstrip(' .').split('.', 1)[0].casefold() in WINDOWS_RESERVED_NAMES
        for part in parts
    )
    unsafe = (
        not normalized
        or '\x00' in normalized
        or normalized.startswith('/')
        or ':' in normalized
        or any(part == '..' for part in parts)
        or not parts
        or unsafe_windows_name
    )
    if unsafe:
        raise BackupValidationError(
            f'备份中包含不安全路径: {raw_name or "(空路径)"}',
            '请重新从 LifeUp App 导出备份，不要修改 ZIP 内部文件结构。'
        )
    unix_mode = info.external_attr >> 16
    if unix_mode and stat.S_IFMT(unix_mode) == stat.S_IFLNK:
        raise BackupValidationError(
            f'备份中包含不支持的符号链接: {raw_name}',
            '请重新从 LifeUp App 导出标准 ZIP 备份。'
        )
    return '/'.join(parts)


def _validated_archive_members(archive):
    infos = archive.infolist()
    if len(infos) > MAX_BACKUP_ENTRIES:
        raise BackupValidationError(
            f'备份内文件数量过多（最多 {MAX_BACKUP_ENTRIES} 个）',
            '请确认选择的是 LifeUp 直接导出的备份，而不是其他大型压缩包。'
        )

    members = []
    seen_names = set()
    expanded_bytes = 0
    database_found = False
    for info in infos:
        if info.flag_bits & 0x1:
            raise BackupValidationError(
                '备份包含加密文件，当前无法安全读取',
                '请从 LifeUp App 重新导出未加密的标准备份。'
            )
        safe_name = _safe_archive_member_name(info)
        name_key = safe_name.casefold()
        if name_key in seen_names:
            raise BackupValidationError(
                f'备份中存在重复路径: {safe_name}',
                '请重新从 LifeUp App 导出备份，避免手动合并 ZIP 内容。'
            )
        seen_names.add(name_key)
        if not info.is_dir():
            if info.file_size > MAX_BACKUP_MEMBER_BYTES:
                raise BackupValidationError(
                    f'备份内单个文件过大: {safe_name}',
                    '请确认选择的是正常的 LifeUp 备份文件。'
                )
            expanded_bytes += info.file_size
            if expanded_bytes > MAX_BACKUP_EXPANDED_BYTES:
                limit_mb = MAX_BACKUP_EXPANDED_BYTES // (1024 * 1024)
                raise BackupValidationError(
                    f'备份解压后大小超过安全限制（最多 {limit_mb} MB）',
                    '请确认选择的是 LifeUp 备份，而不是其他压缩包；必要时重新从 App 导出。'
                )
        if safe_name.casefold() == DB_INTERNAL.casefold() and not info.is_dir():
            database_found = True
        members.append((info, safe_name))

    if not database_found:
        raise BackupValidationError(
            f'备份中未找到 {DB_INTERNAL}',
            '这个 ZIP 不是完整的 LifeUp 备份，请从 LifeUp App 重新导出后再选择。'
        )
    return members


def _extract_validated_backup(path):
    if os.path.getsize(path) > MAX_BACKUP_ARCHIVE_BYTES:
        limit_mb = MAX_BACKUP_ARCHIVE_BYTES // (1024 * 1024)
        raise BackupValidationError(
            f'备份 ZIP 超过安全大小限制（最多 {limit_mb} MB）',
            '请确认选择的是 LifeUp 备份文件。'
        )

    tmp = tempfile.mkdtemp(prefix='lifeup-backup-')
    try:
        with zipfile.ZipFile(path, 'r') as archive:
            members = _validated_archive_members(archive)
            for info, safe_name in members:
                target = os.path.join(tmp, *safe_name.split('/'))
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                written = 0
                with archive.open(info, 'r') as source, open(target, 'wb') as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_BACKUP_MEMBER_BYTES or written > info.file_size:
                            raise BackupValidationError(
                                f'备份内文件解压大小异常: {safe_name}',
                                '请重新从 LifeUp App 导出备份。'
                            )
                        output.write(chunk)
        db = os.path.join(tmp, *DB_INTERNAL.split('/'))
        if not os.path.isfile(db):
            raise BackupValidationError(
                f'备份中未找到 {DB_INTERNAL}',
                '这个 ZIP 不是完整的 LifeUp 备份，请从 LifeUp App 重新导出后再选择。'
            )
        return tmp, db
    except zipfile.BadZipFile as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise BackupValidationError(
            '文件已损坏，不是有效的 ZIP 备份',
            '请重新从 LifeUp App 导出备份后再选择。'
        ) from exc
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

# ─── 存档读写 ───────────────────────────────────────────

def load_backup(path):
    """验证并解压备份；只有新工作区完整可用时才替换当前状态。"""
    with STATE_LOCK:
        tmp, db = _extract_validated_backup(path)
        old_tmpdir = STATE.get('tmpdir')
        STATE['backup_path'] = path
        STATE['tmpdir'] = tmp
        STATE['db_path'] = db
        STATE['loaded'] = True
        if old_tmpdir and old_tmpdir != tmp:
            shutil.rmtree(old_tmpdir, ignore_errors=True)
    return True

def _canonical_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _paths_refer_to_same_file(first, second):
    if not first or not second:
        return False
    if _canonical_path(first) == _canonical_path(second):
        return True
    try:
        return os.path.exists(first) and os.path.exists(second) and os.path.samefile(first, second)
    except OSError:
        return False


def _path_is_within(path, directory):
    try:
        return os.path.commonpath([_canonical_path(path), _canonical_path(directory)]) == _canonical_path(directory)
    except ValueError:
        return False


def _resolve_export_output(output_path):
    if output_path is None:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        filename = (
            f'LifeupBackup-export-{datetime.now().strftime("%Y%m%d-%H%M%S")}-'
            f'{secrets.token_hex(3)}.zip'
        )
        return os.path.abspath(os.path.join(EXPORT_DIR, filename))

    if not isinstance(output_path, str) or not output_path.strip():
        raise BackupExportError(
            'INVALID_OUTPUT_PATH',
            '导出路径必须是非空字符串',
            '请使用导出目录中的绝对 .zip 文件路径。',
            400,
        )
    output_path = output_path.strip()
    if not os.path.isabs(output_path) or not output_path.lower().endswith('.zip'):
        raise BackupExportError(
            'INVALID_OUTPUT_PATH',
            '导出路径必须是绝对 .zip 文件路径',
            '请在应用的 exports 目录中指定新的 ZIP 文件名。',
            400,
        )

    output_path = os.path.abspath(output_path)
    parent = os.path.dirname(output_path)
    if not os.path.isdir(parent):
        raise BackupExportError(
            'INVALID_OUTPUT_PATH',
            '导出目录不存在或不是文件夹',
            '请先选择已存在的 exports 目录，不会自动创建手动输入的目录。',
            400,
        )

    canonical_output = _canonical_path(output_path)
    protected = canonical_output in {_canonical_path(path) for path in PROTECTED_BACKUP_PATHS}
    protected = protected or _paths_refer_to_same_file(output_path, STATE.get('backup_path'))
    if protected:
        raise BackupExportError(
            'PROTECTED_OUTPUT_PATH',
            '不能覆盖原始备份或当前载入的来源文件',
            '请导出为 exports 目录中的新 ZIP，再确认无误后手动保存。',
            400,
        )

    if STATE.get('tmpdir') and _path_is_within(output_path, STATE['tmpdir']):
        raise BackupExportError(
            'INVALID_OUTPUT_PATH',
            '导出目标不能位于当前工作区内部',
            '请使用应用的 exports 目录，避免把导出文件再次打包进自身。',
            400,
        )
    if not _path_is_within(output_path, EXPORT_DIR):
        raise BackupExportError(
            'INVALID_OUTPUT_PATH',
            '导出目标必须位于应用管理的 exports 目录',
            '这是为了避免通过路径别名误覆盖原始备份。',
            400,
        )
    if os.path.isdir(output_path):
        raise BackupExportError(
            'INVALID_OUTPUT_PATH',
            '导出目标是文件夹，不是 ZIP 文件',
            '请指定 exports 目录中的 .zip 文件名。',
            400,
        )
    return output_path


def _readonly_database_connection(database_path):
    database_uri = Path(database_path).resolve().as_uri() + '?mode=ro'
    return sqlite3.connect(database_uri, uri=True, timeout=30)


def _database_integrity_report(database_path):
    connection = _readonly_database_connection(database_path)
    try:
        # Source: https://www.sqlite.org/pragma.html#pragma_integrity_check
        rows = [str(row[0]) for row in connection.execute('PRAGMA integrity_check').fetchall()]
        if rows != ['ok']:
            detail = '; '.join(rows[:3]) or '未返回校验结果'
            raise BackupExportError(
                'EXPORT_VALIDATION_FAILED',
                f'SQLite 完整性校验失败: {detail}',
                '当前工作副本可能已损坏；请不要恢复此导出文件，重新载入上一个有效备份。',
            )
        existing_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in KEY_ENTITY_TABLES
            if table in existing_tables
        }
        return {'database': 'ok', 'counts': counts}
    except sqlite3.DatabaseError as exc:
        raise BackupExportError(
            'EXPORT_VALIDATION_FAILED',
            'SQLite 数据库无法通过完整性校验',
            '当前工作副本可能已损坏；请重新载入上一个有效备份。',
        ) from exc
    finally:
        connection.close()


def _create_database_snapshot(source_database, snapshot_database):
    # SQLite backup creates a consistent snapshot even when the source uses WAL.
    # Source: https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup
    source = None
    destination = None
    try:
        source = _readonly_database_connection(source_database)
        destination = sqlite3.connect(snapshot_database)
        source.backup(destination)
        destination.commit()
    except sqlite3.DatabaseError as exc:
        raise BackupExportError(
            'EXPORT_VALIDATION_FAILED',
            '无法从当前工作副本创建一致的数据库快照',
            '请关闭正在占用数据库的程序后重试；仍失败时重新载入有效备份。',
        ) from exc
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def _write_backup_archive(
    output_path, workspace_root, snapshot_database, archive_comment=None
):
    skipped_database_files = {
        DB_INTERNAL.casefold(),
        f'{DB_INTERNAL}-wal'.casefold(),
        f'{DB_INTERNAL}-shm'.casefold(),
        f'{DB_INTERNAL}-journal'.casefold(),
    }
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for root, directories, files in os.walk(workspace_root):
            directories[:] = [
                name for name in directories
                if not os.path.islink(os.path.join(root, name))
            ]
            for filename in files:
                file_path = os.path.join(root, filename)
                if os.path.islink(file_path) or not os.path.isfile(file_path):
                    raise BackupExportError(
                        'EXPORT_VALIDATION_FAILED',
                        f'工作区包含不支持的链接或特殊文件: {filename}',
                        '请重新载入有效备份，不要在工作区中放置链接或特殊文件。',
                    )
                archive_name = os.path.relpath(file_path, workspace_root).replace('\\', '/')
                if archive_name.casefold() in skipped_database_files:
                    continue
                archive.write(file_path, archive_name)
        archive.write(snapshot_database, DB_INTERNAL)
        if archive_comment:
            # ZIP comments are bounded metadata stored with the single snapshot file.
            # Source: https://docs.python.org/3.10/library/zipfile.html#zipfile.ZipFile.comment
            archive.comment = archive_comment


def _inspect_backup_archive(archive_path):
    try:
        with zipfile.ZipFile(archive_path, 'r') as archive:
            # testzip reads every member and returns the first CRC failure.
            # Source: https://docs.python.org/3/library/zipfile.html#zipfile.ZipFile.testzip
            bad_member = archive.testzip()
            if bad_member:
                raise BackupExportError(
                    'EXPORT_VALIDATION_FAILED',
                    f'导出 ZIP 的 CRC 校验失败: {bad_member}',
                    '导出文件没有发布，请检查磁盘空间后重试。',
                )
    except zipfile.BadZipFile as exc:
        raise BackupExportError(
            'EXPORT_VALIDATION_FAILED',
            '临时导出文件不是有效的 ZIP',
            '导出文件没有发布，请检查磁盘空间后重试。',
        ) from exc

    extracted = None
    try:
        extracted, database_path = _extract_validated_backup(archive_path)
        report = _database_integrity_report(database_path)
        return {
            'archive': 'ok',
            'database': 'ok',
            'counts': report['counts'],
        }
    except BackupValidationError as exc:
        raise BackupExportError(
            'EXPORT_VALIDATION_FAILED',
            f'导出 ZIP 结构校验失败: {exc}',
            exc.suggestion,
        ) from exc
    finally:
        if extracted:
            shutil.rmtree(extracted, ignore_errors=True)


def _validate_export_archive(archive_path, expected_counts):
    report = _inspect_backup_archive(archive_path)
    if report['counts'] != expected_counts:
        raise BackupExportError(
            'EXPORT_VALIDATION_FAILED',
            '导出前后的关键数据数量不一致',
            '导出文件没有发布，请保持页面打开并重试。',
        )
    return report


def _sync_file(path):
    with open(path, 'r+b') as file_handle:
        file_handle.flush()
        os.fsync(file_handle.fileno())


def _publish_workspace_archive_locked(
    final_path, archive_comment_builder=None, generated_at=None,
    replace_existing=True,
):
    """Publish the active workspace to a server-resolved path while STATE_LOCK is held."""
    if not STATE['loaded']:
        raise BackupExportError(
            'NO_BACKUP_LOADED',
            '未加载备份',
            '请先载入一个 LifeUp 工作副本。',
            400,
        )

    workspace_root = STATE['tmpdir']
    source_database = STATE['db_path']
    temporary_archive = None
    snapshot_database = None
    try:
        snapshot_fd, snapshot_database = tempfile.mkstemp(
            prefix='lifeup-export-snapshot-', suffix='.db'
        )
        os.close(snapshot_fd)
        _create_database_snapshot(source_database, snapshot_database)
        snapshot_report = _database_integrity_report(snapshot_database)
        archive_comment = (
            archive_comment_builder(snapshot_report)
            if archive_comment_builder
            else None
        )

        archive_fd, temporary_archive = tempfile.mkstemp(
            prefix=f'.{os.path.basename(final_path)}.',
            suffix='.tmp',
            dir=os.path.dirname(final_path),
        )
        os.close(archive_fd)
        _write_backup_archive(
            temporary_archive,
            workspace_root,
            snapshot_database,
            archive_comment=archive_comment,
        )
        _sync_file(temporary_archive)
        integrity = _validate_export_archive(
            temporary_archive, snapshot_report['counts']
        )
        size = os.path.getsize(temporary_archive)
        generated_at = generated_at or datetime.now().astimezone().isoformat(
            timespec='seconds'
        )

        if replace_existing:
            # Exports intentionally replace the caller-selected destination.
            os.replace(temporary_archive, final_path)
            temporary_archive = None
        else:
            # Hard-link publication is atomic and fails if another process created
            # the managed target after its ID was allocated. The temporary name is
            # removed by the finally block while the published link remains valid.
            os.link(temporary_archive, final_path)
        return {
            'path': final_path,
            'filename': os.path.basename(final_path),
            'size': size,
            'generated_at': generated_at,
            'integrity': integrity,
        }
    finally:
        for temporary_path in (temporary_archive, snapshot_database):
            if temporary_path:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def save_backup(output_path=None):
    """从当前工作区创建、验证并原子发布一个新的 LifeUp 备份。"""
    with STATE_LOCK:
        if not STATE['loaded']:
            raise BackupExportError(
                'NO_BACKUP_LOADED',
                '未加载备份',
                '请先载入一个 LifeUp 工作副本。',
                400,
            )
        final_path = _resolve_export_output(output_path)
        return _publish_workspace_archive_locked(final_path)


def _normalize_snapshot_name(value, created_at):
    if value is None or (isinstance(value, str) and not value.strip()):
        return f'快照 {created_at.replace("T", " ")[:19]}'
    if not isinstance(value, str):
        raise SnapshotError(
            'INVALID_SNAPSHOT_NAME',
            '快照名称必须是文本',
            f'请输入不超过 {MAX_SNAPSHOT_NAME_LENGTH} 个字符的名称。',
            400,
        )
    name = value.strip()
    try:
        name.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise SnapshotError(
            'INVALID_SNAPSHOT_NAME',
            '快照名称包含无法保存的 Unicode 字符',
            '请删除异常字符后重新输入名称。',
            400,
        ) from exc
    if (
        len(name) > MAX_SNAPSHOT_NAME_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise SnapshotError(
            'INVALID_SNAPSHOT_NAME',
            '快照名称过长或包含不支持的控制字符',
            f'请输入不超过 {MAX_SNAPSHOT_NAME_LENGTH} 个字符的单行名称。',
            400,
        )
    return name


def _managed_snapshot_path(snapshot_id):
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
        raise SnapshotError(
            'INVALID_SNAPSHOT_ID',
            '快照 ID 格式无效',
            '请刷新快照列表后重试，不要手动输入文件路径。',
            400,
        )
    directory = os.path.abspath(SNAPSHOT_DIR)
    path = os.path.abspath(os.path.join(directory, f'snapshot-{snapshot_id}.zip'))
    try:
        if os.path.commonpath([path, directory]) != directory:
            raise ValueError('outside managed directory')
    except ValueError as exc:
        raise SnapshotError(
            'INVALID_SNAPSHOT_ID',
            '快照 ID 超出托管目录',
            '请刷新快照列表后重试。',
            400,
        ) from exc
    return path


def _allocate_snapshot_target():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    for _attempt in range(10):
        snapshot_id = secrets.token_hex(16)
        path = _managed_snapshot_path(snapshot_id)
        if not os.path.lexists(path):
            return snapshot_id, path
    raise SnapshotError(
        'SNAPSHOT_ID_CONFLICT',
        '无法分配新的快照 ID',
        '没有覆盖任何已有快照，请稍后重试。',
        409,
    )


def _snapshot_comment_builder(snapshot_id, name, created_at):
    def build(snapshot_report):
        counts = snapshot_report.get('counts') or {}
        missing_tables = [table for table in KEY_ENTITY_TABLES if table not in counts]
        if missing_tables:
            raise SnapshotError(
                'SNAPSHOT_VALIDATION_FAILED',
                '当前工作副本缺少必要的 LifeUp 数据表',
                '请重新加载完整的 LifeUp 备份后再创建快照。',
                422,
            )
        metadata = {
            'schema': 1,
            'id': snapshot_id,
            'name': name,
            'created_at': created_at,
            'integrity': {
                'archive': 'ok',
                'database': 'ok',
                'counts': {table: counts[table] for table in KEY_ENTITY_TABLES},
            },
        }
        encoded = SNAPSHOT_COMMENT_PREFIX + json.dumps(
            metadata, ensure_ascii=False, separators=(',', ':'), sort_keys=True
        ).encode('utf-8')
        if len(encoded) > 65535:
            raise SnapshotError(
                'INVALID_SNAPSHOT_NAME',
                '快照元数据过大',
                '请缩短快照名称后重试。',
                400,
            )
        return encoded

    return build


def _read_snapshot_metadata(snapshot_path, expected_id):
    try:
        with zipfile.ZipFile(snapshot_path, 'r') as archive:
            comment = archive.comment
        if not comment.startswith(SNAPSHOT_COMMENT_PREFIX):
            raise ValueError('snapshot metadata prefix is missing')
        metadata = json.loads(
            comment[len(SNAPSHOT_COMMENT_PREFIX):].decode('utf-8')
        )
        if not isinstance(metadata, dict) or metadata.get('schema') != 1:
            raise ValueError('unsupported snapshot metadata')
        if metadata.get('id') != expected_id:
            raise ValueError('snapshot id does not match filename')
        name = metadata.get('name')
        if (
            not isinstance(name, str)
            or not name
            or len(name) > MAX_SNAPSHOT_NAME_LENGTH
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise ValueError('invalid snapshot name')
        created_at = metadata.get('created_at')
        if not isinstance(created_at, str) or len(created_at) > 64:
            raise ValueError('invalid creation time')
        datetime.fromisoformat(created_at)
        integrity = metadata.get('integrity')
        counts = integrity.get('counts') if isinstance(integrity, dict) else None
        if (
            not isinstance(integrity, dict)
            or integrity.get('archive') != 'ok'
            or integrity.get('database') != 'ok'
            or not isinstance(counts, dict)
            or set(counts) != set(KEY_ENTITY_TABLES)
            or any(
                isinstance(counts[table], bool)
                or not isinstance(counts[table], int)
                or counts[table] < 0
                for table in KEY_ENTITY_TABLES
            )
        ):
            raise ValueError('invalid integrity metadata')
        return {
            'id': expected_id,
            'name': name,
            'filename': os.path.basename(snapshot_path),
            'size': os.path.getsize(snapshot_path),
            'created_at': created_at,
            'integrity': {
                'archive': 'ok',
                'database': 'ok',
                'counts': {table: counts[table] for table in KEY_ENTITY_TABLES},
            },
            'status': 'ready',
            'restorable': True,
        }
    except SnapshotError:
        raise
    except OSError as exc:
        raise SnapshotError(
            'SNAPSHOT_IO_ERROR',
            '快照文件暂时无法读取',
            '请检查文件权限，以及快照 ZIP 是否被其他程序占用。',
            500,
        ) from exc
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise SnapshotError(
            'SNAPSHOT_VALIDATION_FAILED',
            '快照文件或元数据已损坏',
            '这个快照不会被恢复；可以删除后重新创建。',
            422,
        ) from exc


def create_snapshot(name=None):
    with STATE_LOCK:
        if not STATE['loaded']:
            raise SnapshotError(
                'NO_BACKUP_LOADED',
                '未加载备份',
                '请先载入一个 LifeUp 工作副本。',
                400,
            )
        created_at = datetime.now().astimezone().isoformat(timespec='seconds')
        normalized_name = _normalize_snapshot_name(name, created_at)
        snapshot_id, final_path = _allocate_snapshot_target()
        try:
            result = _publish_workspace_archive_locked(
                final_path,
                archive_comment_builder=_snapshot_comment_builder(
                    snapshot_id, normalized_name, created_at
                ),
                generated_at=created_at,
                replace_existing=False,
            )
        except FileExistsError as exc:
            raise SnapshotError(
                'SNAPSHOT_ID_CONFLICT',
                '快照目标在发布前已被占用',
                '没有覆盖新出现的文件；请重试创建快照。',
                409,
            ) from exc
        return {
            'id': snapshot_id,
            'name': normalized_name,
            'filename': result['filename'],
            'size': result['size'],
            'created_at': created_at,
            'integrity': result['integrity'],
            'status': 'ready',
            'restorable': True,
        }


def list_snapshots(limit=100, offset=0):
    if not os.path.isdir(SNAPSHOT_DIR):
        return [], 0
    snapshots = []
    with os.scandir(SNAPSHOT_DIR) as entries:
        for entry in entries:
            match = SNAPSHOT_FILENAME_PATTERN.fullmatch(entry.name)
            if not match or not entry.is_file(follow_symlinks=False):
                continue
            snapshot_id = match.group(1)
            try:
                snapshots.append(_read_snapshot_metadata(entry.path, snapshot_id))
            except SnapshotError:
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                    created_at = datetime.fromtimestamp(
                        stat_result.st_mtime
                    ).astimezone().isoformat(timespec='seconds')
                    size = stat_result.st_size
                except OSError:
                    created_at = ''
                    size = 0
                snapshots.append({
                    'id': snapshot_id,
                    'name': '无法读取的快照',
                    'filename': entry.name,
                    'size': size,
                    'created_at': created_at,
                    'integrity': {},
                    'status': 'invalid',
                    'restorable': False,
                })
    snapshots.sort(
        key=lambda snapshot: (snapshot.get('created_at') or '', snapshot['id']),
        reverse=True,
    )
    total = len(snapshots)
    return snapshots[offset:offset + limit], total


def _allocate_restore_target(snapshot_id):
    os.makedirs(RESTORE_DIR, exist_ok=True)
    for _attempt in range(10):
        token = secrets.token_hex(8)
        filename = (
            f'restore-{datetime.now().strftime("%Y%m%d-%H%M%S")}-'
            f'{snapshot_id}-{token}.zip'
        )
        path = os.path.abspath(os.path.join(RESTORE_DIR, filename))
        if not os.path.lexists(path):
            return path
    raise SnapshotError(
        'RESTORE_ID_CONFLICT',
        '无法创建新的恢复工作副本',
        '没有覆盖任何已有文件，请稍后重试。',
        409,
    )


def _copy_snapshot_for_restore_locked(snapshot_path, snapshot_id, expected_counts):
    final_path = _allocate_restore_target(snapshot_id)
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f'.{os.path.basename(final_path)}.',
            suffix='.tmp',
            dir=RESTORE_DIR,
        )
        with os.fdopen(descriptor, 'wb') as output:
            with open(snapshot_path, 'rb') as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())

        try:
            integrity = _inspect_backup_archive(temporary_path)
        except BackupExportError as exc:
            raise SnapshotError(
                'SNAPSHOT_VALIDATION_FAILED',
                '恢复副本未通过 ZIP 或 SQLite 完整性校验',
                '当前工作副本保持不变；请删除损坏快照后重新创建。',
                422,
            ) from exc
        if integrity['counts'] != expected_counts:
            raise SnapshotError(
                'SNAPSHOT_VALIDATION_FAILED',
                '恢复副本的关键数据数量与快照记录不一致',
                '当前工作副本保持不变；请删除损坏快照后重新创建。',
                422,
            )
        try:
            # The validated temporary ZIP and managed destination share a
            # directory, so a hard link publishes atomically without replacing
            # a file that appeared after target allocation.
            os.link(temporary_path, final_path)
        except FileExistsError as exc:
            raise SnapshotError(
                'RESTORE_ID_CONFLICT',
                '恢复目标在发布前已被占用',
                '没有覆盖新出现的文件；请重试恢复快照。',
                409,
            ) from exc
        return final_path, integrity
    finally:
        if temporary_path:
            try:
                os.remove(temporary_path)
            except OSError:
                pass


def restore_snapshot(snapshot_id):
    with STATE_LOCK:
        snapshot_path = _managed_snapshot_path(snapshot_id)
        if not os.path.isfile(snapshot_path) or os.path.islink(snapshot_path):
            raise SnapshotError(
                'SNAPSHOT_NOT_FOUND',
                '快照不存在',
                '请刷新快照列表后重试。',
                404,
            )
        metadata = _read_snapshot_metadata(snapshot_path, snapshot_id)
        restored_path = None
        try:
            restored_path, integrity = _copy_snapshot_for_restore_locked(
                snapshot_path,
                snapshot_id,
                metadata['integrity']['counts'],
            )
            response = {
                'snapshot': metadata,
                'workspace': {
                    'loaded': True,
                    'backup_path': restored_path,
                    'filename': os.path.basename(restored_path),
                    'workspace_copy': True,
                },
                'integrity': integrity,
            }
            load_backup(restored_path)
            restored_path = None
            return response
        except BackupValidationError as exc:
            raise SnapshotError(
                'SNAPSHOT_VALIDATION_FAILED',
                '恢复副本无法作为 LifeUp 备份加载',
                '当前工作副本保持不变，请删除损坏快照后重新创建。',
                422,
            ) from exc
        finally:
            if restored_path:
                try:
                    os.remove(restored_path)
                except OSError:
                    pass


def delete_snapshot(snapshot_id):
    with STATE_LOCK:
        snapshot_path = _managed_snapshot_path(snapshot_id)
        if not os.path.lexists(snapshot_path):
            raise SnapshotError(
                'SNAPSHOT_NOT_FOUND',
                '快照不存在',
                '请刷新快照列表后重试。',
                404,
            )
        if _paths_refer_to_same_file(snapshot_path, STATE.get('backup_path')):
            raise SnapshotError(
                'SNAPSHOT_IN_USE',
                '当前加载来源仍指向这个快照，不能删除',
                '请先恢复为新的工作副本，再删除快照。',
                409,
            )
        os.remove(snapshot_path)
        return {'id': snapshot_id}


def _snapshot_pagination_args():
    try:
        limit = int(request.args.get('limit', '100'))
        offset = int(request.args.get('offset', '0'))
    except (TypeError, ValueError) as exc:
        raise SnapshotError(
            'INVALID_PAGINATION',
            '快照分页参数必须是整数',
            f'limit 请输入 1～{MAX_SNAPSHOT_LIST_LIMIT}，offset 请输入 0 或更大的整数。',
            400,
        ) from exc
    if not 1 <= limit <= MAX_SNAPSHOT_LIST_LIMIT or not 0 <= offset <= 1000000:
        raise SnapshotError(
            'INVALID_PAGINATION',
            '快照分页参数超出允许范围',
            f'limit 请输入 1～{MAX_SNAPSHOT_LIST_LIMIT}，offset 请输入 0～1000000。',
            400,
        )
    return limit, offset

def get_db():
    """获取数据库连接，每次调用都创建新连接"""
    if not STATE['loaded']:
        raise RuntimeError('未加载备份')
    conn = sqlite3.connect(STATE['db_path'])
    conn.row_factory = sqlite3.Row
    return conn


def validate_batch_ids(ids, resource_name):
    if not isinstance(ids, list) or not ids:
        raise BatchValidationError(f'请选择至少一个{resource_name}')
    if len(ids) > MAX_BATCH_SIZE:
        raise BatchValidationError(f'单次最多处理 {MAX_BATCH_SIZE} 个{resource_name}')
    if any(type(item_id) is not int or item_id <= 0 for item_id in ids):
        raise BatchValidationError('ids 必须是正整数列表')
    if len(set(ids)) != len(ids):
        raise BatchValidationError('ids 不能包含重复项')

    return ids


def validate_batch_request(data, allowed_actions, resource_name):
    if not isinstance(data, dict):
        raise BatchValidationError('请求内容必须是 JSON 对象')

    action = data.get('action')
    if action not in allowed_actions:
        allowed = '、'.join(sorted(allowed_actions))
        raise BatchValidationError(f'不支持的批量操作；允许的 action：{allowed}')

    ids = validate_batch_ids(data.get('ids'), resource_name)

    return ids, action


def validate_batch_price(price):
    if type(price) is not int or not 0 <= price <= MAX_ITEM_PRICE:
        raise BatchValidationError(
            f'price 必须是 0～{MAX_ITEM_PRICE} 之间的整数'
        )
    return price


def ensure_batch_targets_exist(cursor, table_name, ids, resource_name):
    placeholders = ','.join('?' * len(ids))
    cursor.execute(
        f'SELECT id FROM {table_name} WHERE id IN ({placeholders})',
        ids,
    )
    found_ids = {row[0] for row in cursor.fetchall()}
    missing_ids = [item_id for item_id in ids if item_id not in found_ids]
    if missing_ids:
        missing_text = '、'.join(str(item_id) for item_id in missing_ids)
        raise BatchValidationError(f'{resource_name}不存在：{missing_text}')


def request_data_source():
    """Return the data source explicitly selected by the caller."""
    source = request.args.get('source') or request.headers.get('X-LifeUp-Data-Source')
    if not source and request.is_json:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            source = payload.get('source')
    return 'cloud' if source == 'cloud' else 'local'


def reject_cloud_local_write():
    """Keep cloud mode from changing the loaded local backup workspace."""
    if request_data_source() != 'cloud':
        return None
    return jsonify({
        'code': 'LOCAL_WRITE_REQUIRES_LOCAL_SOURCE',
        'error': '手机云人升模式不能修改本地备份；请先切回本地备份模式。',
        'suggestion': '把顶部数据源切换为“本地备份”后再执行此操作。',
    }), 403


MEDIA_FOLDERS = {
    'attachments', 'attr', 'custom', 'download', 'feelings',
    'shop', 'sound', 'temp', 'userAch'
}


def media_url(folder, filename):
    if not filename:
        return ''
    text = str(filename).strip()
    if text.startswith(('http://', 'https://', 'data:')):
        return text
    return f'/api/media/{folder}/{text}'

def now_ms():
    return int(time.time() * 1000)


CLOUD_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'lifeup_cloud_config.json')
CLOUD_RUNTIME_CONFIG = {'api_token': ''}
CLOUD_RUNTIME_CONFIG_LOCK = threading.Lock()
CLOUD_PREVIEW_TTL_SECONDS = 10 * 60
CLOUD_EXECUTION_TTL_SECONDS = 60 * 60
CLOUD_PREVIEWS = {}
CLOUD_EXECUTIONS = {}
CLOUD_EXECUTION_LOCK = threading.Lock()


def load_cloud_config():
    try:
        with open(CLOUD_CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_cloud_config(config):
    data = {
        'host': config['host'],
        'port': config['port'],
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    with open(CLOUD_CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_cloud_config(data=None, allow_empty=False):
    data = data or {}
    saved = load_cloud_config()
    host = str(data.get('host', saved.get('host', ''))).strip()
    port_raw = data.get('port', saved.get('port', 13276))
    token = str(data.get('api_token') or data.get('token') or '').strip()
    if not token:
        with CLOUD_RUNTIME_CONFIG_LOCK:
            token = CLOUD_RUNTIME_CONFIG.get('api_token', '')
    if not host and not allow_empty:
        raise ValueError('请先填写手机 IP')
    try:
        port = int(port_raw or 13276)
    except (TypeError, ValueError):
        raise ValueError('端口必须是数字')
    if port < 1 or port > 65535:
        raise ValueError('端口必须在 1-65535 之间')
    return {
        'host': host,
        'port': port,
        'api_token': token,
        'base_url': f'http://{host}:{port}' if host else ''
    }


def update_runtime_cloud_token(data=None):
    """Keep the optional API token in process memory only."""
    data = data or {}
    with CLOUD_RUNTIME_CONFIG_LOCK:
        if data.get('clear_token'):
            CLOUD_RUNTIME_CONFIG['api_token'] = ''
        else:
            token = str(data.get('api_token') or data.get('token') or '').strip()
            if token:
                CLOUD_RUNTIME_CONFIG['api_token'] = token
        return bool(CLOUD_RUNTIME_CONFIG.get('api_token'))


def cloud_token_in_memory():
    with CLOUD_RUNTIME_CONFIG_LOCK:
        return bool(CLOUD_RUNTIME_CONFIG.get('api_token'))


def cloud_request(config, route, timeout=12):
    cfg = normalize_cloud_config(config)
    if not route.startswith('/'):
        route = '/' + route
    headers = {'Accept': 'application/json'}
    if cfg['api_token']:
        headers['Authorization'] = cfg['api_token']
    req = Request(cfg['base_url'] + route, headers=headers, method='GET')
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except HTTPError as exc:
        raw = exc.read()
        msg = raw.decode('utf-8', errors='replace') if raw else exc.reason
        raise ConnectionError(f'LifeUp Cloud HTTP {exc.code}: {msg}') from exc
    except (TimeoutError, URLError) as exc:
        raise ConnectionError('连接云人升超时，请确认手机和电脑在同一局域网，且云人升服务仍在开启') from exc
    try:
        payload = json.loads(raw.decode('utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'云人升返回的不是 JSON，HTTP {status}') from exc
    if not isinstance(payload, dict):
        raise ValueError('云人升返回格式不是对象')
    if int(payload.get('code', 500)) != 200:
        raise ConnectionError(payload.get('message') or '云人升 API 返回错误')
    return {'route': route, 'base_url': cfg['base_url'], 'response': payload, 'data': payload.get('data')}


def cloud_post_json(config, route, payload, timeout=45):
    cfg = normalize_cloud_config(config)
    if not route.startswith('/'):
        route = '/' + route
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json; charset=utf-8'}
    if cfg['api_token']:
        headers['Authorization'] = cfg['api_token']
    req = Request(
        cfg['base_url'] + route,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers=headers,
        method='POST'
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except HTTPError as exc:
        raw = exc.read()
        msg = raw.decode('utf-8', errors='replace') if raw else exc.reason
        raise ConnectionError(f'LifeUp Cloud HTTP {exc.code}: {msg}') from exc
    except (TimeoutError, URLError) as exc:
        raise ConnectionError('请求超时：手机端可能已经执行。请先刷新任务列表或看手机确认，不要立刻重复点击') from exc
    try:
        payload = json.loads(raw.decode('utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'云人升返回的不是 JSON，HTTP {status}') from exc
    if not isinstance(payload, dict):
        raise ValueError('云人升返回格式不是对象')
    if int(payload.get('code', 500)) != 200:
        raise ConnectionError(payload.get('message') or '云人升 API 返回错误')
    return {'route': route, 'base_url': cfg['base_url'], 'response': payload, 'data': payload.get('data')}


def as_cloud_rows(data):
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return [data]


def normalize_lifeup_urls(data):
    raw_urls = []
    if 'url' in data:
        raw_urls.append(data.get('url'))
    if 'urls' in data:
        if not isinstance(data.get('urls'), list):
            raise ValueError('urls 必须是数组')
        raw_urls.extend(data.get('urls'))
    urls = []
    for raw in raw_urls:
        url = str(raw or '').strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme != 'lifeup' or parsed.netloc != 'api' or parsed.path != '/add_task':
            raise ValueError('当前只允许执行 lifeup://api/add_task 新增任务')
        validate_lifeup_task_query(parsed.query)
        urls.append(url)
    if not urls:
        raise ValueError('没有可执行的 LifeUp API URL')
    if len(urls) > 200:
        raise ValueError('单次最多执行 200 条任务，建议分批导入')
    return urls


def validate_lifeup_task_query(raw_query):
    """Validate every add_task parameter before a URL can receive a preview token."""
    query = parse_qs(raw_query, keep_blank_values=True)
    allowed = {
        'todo', 'notes', 'coin', 'coin_var', 'exp', 'skills',
        'category', 'frequency', 'importance', 'difficulty'
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError(f'不支持的任务参数: {unknown[0]}')
    todo_values = query.get('todo', [])
    if len(todo_values) != 1 or not todo_values[0].strip():
        raise ValueError('todo 任务标题不能为空且只能出现一次')

    ranges = {
        'coin': (0, None),
        'coin_var': (0, None),
        'exp': (0, None),
        'category': (0, None),
        'frequency': (0, None),
        'importance': (1, 4),
        'difficulty': (1, 4),
    }
    for field, (minimum, maximum) in ranges.items():
        values = query.get(field, [])
        if len(values) > 1:
            raise ValueError(f'{field} 只能出现一次')
        if not values:
            continue
        raw = values[0].strip()
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f'{field} 必须是整数')
        if str(value) != raw and raw not in (f'+{value}', f'-{abs(value)}'):
            raise ValueError(f'{field} 必须是整数')
        if value < minimum:
            raise ValueError(f'{field} 不能小于 {minimum}')
        if maximum is not None and value > maximum:
            raise ValueError(f'{field} 不能大于 {maximum}')

    for raw in query.get('skills', []):
        text = raw.strip()
        try:
            skill_id = int(text)
        except (TypeError, ValueError):
            raise ValueError('skills 必须是整数 ID')
        if str(skill_id) != text or skill_id < 0:
            raise ValueError('skills 必须是非负整数 ID')


def cleanup_cloud_execution_state(now=None):
    """Drop expired in-memory preview and idempotency records. Lock must be held."""
    now = time.time() if now is None else now
    for token, preview in list(CLOUD_PREVIEWS.items()):
        if preview.get('expires_at', 0) <= now and preview.get('status') != 'executing':
            CLOUD_PREVIEWS.pop(token, None)
    for key, execution in list(CLOUD_EXECUTIONS.items()):
        if execution.get('expires_at', 0) <= now:
            CLOUD_EXECUTIONS.pop(key, None)


def validate_lifeup_url(url, allowed):
    text = str(url or '').strip()
    parsed = urlparse(text)
    if parsed.scheme != 'lifeup' or parsed.netloc != 'api':
        raise ValueError('只允许执行 lifeup://api/ 官方 URL')
    path = parsed.path.lstrip('/')
    if path not in allowed:
        raise ValueError('当前接口不允许执行该 LifeUp API')
    return text


def first_value(row, keys, default=None):
    for key in keys:
        if isinstance(row, dict) and key in row and row.get(key) is not None:
            return row.get(key)
    return default


def number_value(value, default=0):
    if value is None or value == '':
        return default
    try:
        if isinstance(value, str):
            value = value.strip().rstrip('%')
        return float(value)
    except (TypeError, ValueError):
        return default


def int_value(value, default=0):
    try:
        return int(number_value(value, default))
    except (TypeError, ValueError):
        return default


def json_text(value, default):
    if value is None or value == '':
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError('JSON 字段格式不正确') from exc
        return value
    raise ValueError('JSON 字段格式不正确')


def clamp_percent(value):
    value = number_value(value, 0)
    if value <= 1 and value > 0:
        value *= 100
    return round(max(0, min(100, value)), 1)


def calc_progress(raw_progress=0, current=0, target=0):
    target_num = number_value(target, 0)
    if target_num > 0:
        return clamp_percent(number_value(current, 0) / target_num * 100)
    return clamp_percent(raw_progress)


def ms_to_date(ms):
    ms = int_value(ms, 0)
    if ms <= 0:
        return ''
    try:
        return datetime.fromtimestamp(ms / 1000).strftime('%Y-%m-%d')
    except (OSError, OverflowError, ValueError):
        return ''


def ms_to_datetime(ms):
    ms = int_value(ms, 0)
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000)
    except (OSError, OverflowError, ValueError):
        return None


def overview_meta(source, warning=''):
    return {
        'source': source,
        'source_label': '手机实时数据' if source == 'cloud' else '本地备份数据',
        'read_only': source == 'cloud',
        'warning': warning,
        'refreshed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }


def cloud_category_map(dataset):
    route = {
        'tasks': '/tasks_categories',
        'items': '/items_categories',
        'achievements': '/achievement_categories'
    }[dataset]
    result = cloud_request({}, route, timeout=20)
    rows = as_cloud_rows(result['data'])
    return {row.get('id'): row.get('name', '-') for row in rows if isinstance(row, dict)}


def filter_cloud_rows(rows, search='', category_id='', name_keys=None):
    name_keys = name_keys or ['name', 'title']
    if search:
        needle = search.lower()
        rows = [
            row for row in rows
            if needle in str(first_value(row, name_keys, '')).lower()
            or needle in str(first_value(row, ['notes', 'desc', 'description'], '')).lower()
        ]
    if category_id != '':
        try:
            wanted = int(category_id)
            rows = [row for row in rows if int(first_value(row, ['categoryId', 'categoryid', 'shopcategoryid'], -999999)) == wanted]
        except (TypeError, ValueError):
            rows = []
    return rows


def list_cloud_tasks_for_dashboard():
    filter_type = request.args.get('filter', 'all')
    search = request.args.get('search', '').strip()
    cat_id = request.args.get('category_id', '')
    cats = cloud_category_map('tasks')
    result = cloud_request({}, '/tasks', timeout=30)
    rows = filter_cloud_rows(as_cloud_rows(result['data']), search, cat_id, ['name', 'nameExtended'])
    if filter_type == 'active':
        rows = [row for row in rows if int(row.get('status', 0) or 0) == 0]
    elif filter_type == 'done':
        rows = [row for row in rows if int(row.get('status', 0) or 0) >= 1]
    elif filter_type == 'frozen':
        rows = []
    tasks = []
    for row in rows:
        progress = row.get('countProgress') or {}
        tasks.append({
            'id': row.get('id'),
            'title': first_value(row, ['nameExtended', 'name', 'todo'], ''),
            'frequency': row.get('frequency', 0),
            'coin': row.get('coin', 0),
            'exp': row.get('exp', 0),
            'note': row.get('notes', ''),
            'done': row.get('status', 0),
            'done_count': progress.get('currentCount', 0) if isinstance(progress, dict) else 0,
            'target_count': progress.get('targetCount', 1) if isinstance(progress, dict) else 1,
            'categoryid': row.get('categoryId', 0),
            'category_name': cats.get(row.get('categoryId'), '-'),
            'createdtime': row.get('startTime') or 0,
            'updatedtime': row.get('deadline') or row.get('endTime') or 0,
            'difficulty': row.get('difficulty') or 1,
            'priority': row.get('importance') or 0,
            'isfrozen': 0,
            'rewardcoinvariable': row.get('coinVariable', 0),
            'skill_ids': row.get('skillIds') or [],
            'skill_names': [],
            'item_rewards': row.get('items') or [],
            'subtasks': row.get('subTasks') or [],
            'source': 'cloud',
            'read_only': True
        })
    return jsonify(tasks)


def list_cloud_items_for_dashboard():
    search = request.args.get('search', '').strip()
    cat_id = request.args.get('category_id', '')
    cats = cloud_category_map('items')
    result = cloud_request({}, '/items', timeout=45)
    rows = filter_cloud_rows(as_cloud_rows(result['data']), search, cat_id, ['name', 'itemName'])
    items = []
    for row in rows:
        category_id = row.get('categoryId', row.get('shopcategoryid', 0))
        icon = first_value(row, ['icon', 'iconUri'], '')
        if isinstance(icon, str) and icon.startswith('content://'):
            icon = ''
        items.append({
            'id': row.get('id'),
            'name': first_value(row, ['name', 'itemName', 'title'], ''),
            'price': row.get('price', 0),
            'icon': icon,
            'description': first_value(row, ['desc', 'description'], ''),
            'count': row.get('stockNumber', row.get('count', -1)),
            'shopcategoryid': category_id,
            'category_name': cats.get(category_id, '-'),
            'inventory_count': row.get('ownNumber', row.get('inventory_count', 0)),
            'isdisablepurchase': 1 if row.get('disablePurchase') else 0,
            'source': 'cloud',
            'read_only': True
        })
    return jsonify(items)


def list_cloud_achievements_for_dashboard():
    search = request.args.get('search', '').strip()
    cat_id = request.args.get('category_id', '')
    cats_result = cloud_request({}, '/achievement_categories', timeout=20)
    cat_rows = [row for row in as_cloud_rows(cats_result['data']) if isinstance(row, dict)]
    cats = {row.get('id'): row.get('name', '-') for row in cat_rows}
    all_rows = []
    errors = []
    for cat in cat_rows:
        category_id = cat.get('id')
        if category_id is None:
            continue
        try:
            result = cloud_request({}, f'/achievements/{category_id}', timeout=20)
            for row in as_cloud_rows(result['data']):
                if isinstance(row, dict):
                    row.setdefault('categoryId', category_id)
                    all_rows.append(row)
        except Exception as exc:
            errors.append(f'{category_id}: {exc}')
    rows = filter_cloud_rows(all_rows, search, cat_id, ['name', 'title', 'content'])
    achievements = []
    for row in rows:
        category_id = row.get('categoryId', row.get('categoryid', 0))
        status = first_value(row, ['status', 'achievementstatus'], 0)
        icon = first_value(row, ['icon', 'iconUri'], '')
        if isinstance(icon, str) and icon.startswith('content://'):
            icon = ''
        achievements.append({
            'id': row.get('id'),
            'name': first_value(row, ['name', 'title', 'content'], ''),
            'description': first_value(row, ['desc', 'description'], ''),
            'type': row.get('type', 0),
            'categoryid': category_id,
            'category_name': cats.get(category_id, '-'),
            'coin': first_value(row, ['coin', 'rewardCoin', 'rewardcoin'], 0),
            'exp': first_value(row, ['exp', 'rewardExp', 'expreward'], 0),
            'icon': icon,
            'achievementstatus': status,
            'currentvalue': first_value(row, ['currentValue', 'currentvalue'], 0),
            'progress': row.get('progress', 0),
            'source': 'cloud',
            'read_only': True
        })
    resp = jsonify(achievements)
    if errors:
        resp.headers['X-LifeUp-Cloud-Warnings'] = '; '.join(errors[:5])
    return resp


FOCUS_TYPES = ['打坐修炼', '研读功法', '炼丹制器', '斩妖除魔']
FOCUS_TAG = '闭关修炼'


def focus_type_from_text(*values):
    text = ' '.join(str(v or '') for v in values)
    for focus_type in FOCUS_TYPES:
        if focus_type in text:
            return focus_type
    if '打坐' in text:
        return '打坐修炼'
    if '研读' in text or '功法' in text:
        return '研读功法'
    if '炼丹' in text or '制器' in text:
        return '炼丹制器'
    if '斩妖' in text or '除魔' in text:
        return '斩妖除魔'
    return ''


def is_focus_pomodoro_record(row):
    if not isinstance(row, dict):
        return False
    text_parts = []
    for key in [
        'name', 'title', 'taskName', 'task_name', 'taskContent', 'content',
        'notes', 'remark', 'tags', 'tag', 'categoryName', 'category_name'
    ]:
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            text_parts.extend(str(v) for v in value)
        elif isinstance(value, dict):
            text_parts.append(json.dumps(value, ensure_ascii=False))
        elif value is not None:
            text_parts.append(str(value))
    text = ' '.join(text_parts)
    return FOCUS_TAG in text or any(t in text for t in FOCUS_TYPES)


def normalize_focus_record(row, source='cloud'):
    row = row or {}
    title = first_value(row, [
        'taskName', 'task_name', 'taskContent', 'taskcontent', 'content',
        'name', 'title', 'todo'
    ], '')
    notes = first_value(row, ['notes', 'remark', 'desc', 'description'], '')
    focus_type = focus_type_from_text(title, notes, row.get('tags'), row.get('tag'))

    start_ms = first_value(row, [
        'startTime', 'starttime', 'beginTime', 'begintime', 'createdTime',
        'createtime', 'date'
    ], 0)
    end_ms = first_value(row, ['endTime', 'endtime', 'finishTime', 'finishtime'], 0)
    duration = first_value(row, [
        'durationMinutes', 'duration_min', 'duration', 'lastTime', 'lasttime',
        'minutes', 'minute'
    ], 0)
    duration_min = int_value(duration, 0)
    if duration_min > 600:
        duration_min = max(1, round(duration_min / 60000))
    if duration_min <= 0 and int_value(start_ms, 0) > 0 and int_value(end_ms, 0) > int_value(start_ms, 0):
        duration_min = max(1, round((int_value(end_ms, 0) - int_value(start_ms, 0)) / 60000))

    started = ms_to_datetime(start_ms) or ms_to_datetime(end_ms) or ms_to_datetime(first_value(row, ['createtime'], 0))
    date_text = started.strftime('%Y-%m-%d') if started else ''
    hour = started.hour if started else 0

    abandoned = bool(int_value(first_value(row, ['isabandoned', 'abandoned', 'isAbandoned'], 0), 0))
    deleted = bool(int_value(first_value(row, ['isdel', 'isDel', 'deleted'], 0), 0))
    return {
        'id': row.get('id'),
        'date': date_text,
        'hour': hour,
        'type': focus_type or '闭关修炼',
        'task_name': title or focus_type or '番茄专注',
        'duration': duration_min,
        'expEarned': int_value(first_value(row, ['exp', 'rewardExp', 'expreward'], 0), 0),
        'coinEarned': int_value(first_value(row, ['coin', 'rewardCoin', 'rewardcoin'], 0), 0),
        'rewardSummary': first_value(row, ['rewardSummary', 'reward_summary', 'reward', 'rewards'], ''),
        'source': source,
        'completed': not abandoned and not deleted and duration_min > 0,
        'raw': row
    }


def summarize_focus_records(records, source, warning=''):
    now = datetime.now()
    today = now.date()
    week_start = today.fromordinal(today.toordinal() - today.weekday())
    month_start = today.replace(day=1)
    clean = []
    for record in records:
        if not record.get('completed'):
            continue
        if not record.get('date') or record.get('duration', 0) <= 0:
            continue
        clean.append(record)
    clean.sort(key=lambda r: (r.get('date') or '', r.get('id') or 0), reverse=True)

    def in_range(record, start_date):
        try:
            return datetime.strptime(record.get('date'), '%Y-%m-%d').date() >= start_date
        except (TypeError, ValueError):
            return False

    today_key = today.strftime('%Y-%m-%d')
    today_min = sum(r['duration'] for r in clean if r.get('date') == today_key)
    week_min = sum(r['duration'] for r in clean if in_range(r, week_start))
    month_min = sum(r['duration'] for r in clean if in_range(r, month_start))

    week_bar = []
    for offset in range(6, -1, -1):
        d = today.fromordinal(today.toordinal() - offset)
        key = d.strftime('%Y-%m-%d')
        week_bar.append({
            'label': f'{d.month}/{d.day}',
            'date': key,
            'value': sum(r['duration'] for r in clean if r.get('date') == key)
        })

    hour_distribution = [{'hour': h, 'value': 0} for h in range(24)]
    for record in clean:
        hour = int_value(record.get('hour'), 0)
        if 0 <= hour < 24:
            hour_distribution[hour]['value'] += 1

    return {
        'meta': overview_meta(source, warning),
        'todayFocusMin': today_min,
        'weekFocusMin': week_min,
        'monthFocusMin': month_min,
        'weekBarData': week_bar,
        'hourDistribution': hour_distribution,
        'focusSessions': clean[:60],
        'rewardHint': '奖励、兑换和开箱收益由 LifeUp 手机端番茄设置统一结算；本页只读取完成记录并展示概览。',
        'filterHint': '统计范围：任务名称、备注、标签或分类中包含“闭关修炼”或具体修炼类型的已完成番茄记录。'
    }


def cloud_focus_overview():
    result = cloud_request({}, '/pomodoro_records', timeout=30)
    rows = as_cloud_rows(result['data'])
    records = [
        normalize_focus_record(row, 'cloud')
        for row in rows
        if isinstance(row, dict) and is_focus_pomodoro_record(row)
    ]
    return summarize_focus_records(records, 'cloud')


def local_focus_overview():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.*, t.content AS task_name, t.remark AS task_remark
            FROM tomatomodel p
            LEFT JOIN taskmodel t ON t.id = p.taskmodelid
            WHERE COALESCE(p.isdel, 0)=0
            ORDER BY COALESCE(p.starttime, p.createtime, p.endtime, 0) DESC
            LIMIT 500
        """)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    normalized = []
    for row in rows:
        merged = dict(row)
        if row.get('task_name'):
            merged['taskName'] = row.get('task_name')
        if row.get('task_remark'):
            merged['notes'] = row.get('task_remark')
        if is_focus_pomodoro_record(merged):
            normalized.append(normalize_focus_record(merged, 'local'))
    return summarize_focus_records(normalized, 'local')


def focus_start_native(data):
    focus_type = str(data.get('type') or '打坐修炼').strip()
    minutes = int_value(data.get('minutes'), 25)
    if focus_type not in FOCUS_TYPES:
        raise ValueError('未知修炼类型')
    if minutes not in (15, 30, 45, 60):
        raise ValueError('闭关时长只能是 15/30/45/60 分钟')
    url = 'lifeup://api/goto?page=main&sub_page=pomodoro'
    validate_lifeup_url(url, {'goto'})
    try:
        result = cloud_post_json(data, '/api/contentprovider', {'urls': [url]}, timeout=20)
    except Exception as exc:
        raise ConnectionError(f'无法打开手机 LifeUp 番茄页：{exc}。请确认云人升服务已开启，手机和电脑在同一局域网。') from exc
    return {
        'ok': True,
        'url': url,
        'base_url': result['base_url'],
        'suggested_task_name': focus_type,
        'suggested_tags': [FOCUS_TAG, focus_type],
        'minutes': minutes,
        'message': '已请求手机打开 LifeUp 原生番茄页。请在手机端选择对应修炼任务并开始计时，完成后返回本页刷新统计。'
    }


def build_item_effects(data, item_id, now):
    def normalize_directional_values(effect, values):
        direction = str(effect.get('direction') or 'add')
        is_decrease = direction == 'decrease'
        is_random = bool(effect.get('is_random', False))
        relatedinfo = {}
        if is_random:
            min_v = int_value(effect.get('min_value'), 0)
            max_v = int_value(effect.get('max_value'), 0)
            cross_zero = bool(effect.get('cross_zero', False))
            if not cross_zero:
                low = min(abs(min_v), abs(max_v))
                high = max(abs(min_v), abs(max_v))
                if is_decrease:
                    min_v, max_v = -high, -low
                else:
                    min_v, max_v = low, high
            elif min_v > max_v:
                min_v, max_v = max_v, min_v
            values = max_v
            relatedinfo.update({
                'isRandom': True,
                'randomMin': min_v,
                'randomMax': max_v,
                'allowCrossZero': cross_zero
            })
        elif is_decrease:
            values = -abs(values)
        else:
            values = abs(values)
        return values, relatedinfo

    effects = data.get('effects', [])
    if not isinstance(effects, list):
        effects = []
    rows = []
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        kind = str(effect.get('type') or 'none')
        if kind == 'none':
            continue
        relatedinfos = ''
        relatedid = 0
        values = int_value(effect.get('value'), 0)
        effect_type = 0
        if kind == 'disabled':
            effect_type = 1
            values = 0
        elif kind == 'coin':
            effect_type = 2
            values, relatedinfo = normalize_directional_values(effect, values)
            if relatedinfo:
                relatedinfos = json.dumps(relatedinfo)
        elif kind == 'exp':
            effect_type = 4
            skills = []
            skill_id = int_value(effect.get('skill_id'), 0)
            if skill_id > 0:
                skills.append(skill_id)
            relatedinfo = {'attrs': [], 'e_v': 0, 'skills': skills}
            values, directional_info = normalize_directional_values(effect, values)
            relatedinfo.update(directional_info)
            relatedinfos = json.dumps(relatedinfo)
        elif kind == 'item':
            effect_type = 6
            relatedid = int_value(effect.get('item_id'), 0)
            if relatedid <= 0:
                continue
            values = int_value(effect.get('amount'), 1)
            values, relatedinfo = normalize_directional_values(effect, values)
            if relatedinfo:
                relatedinfos = json.dumps(relatedinfo)
        elif kind == 'lootbox':
            effect_type = 7
            entries = effect.get('items')
            if not isinstance(entries, list):
                entries = [{
                    'item_id': effect.get('item_id'),
                    'amount': effect.get('amount', 1),
                    'probability': effect.get('probability', 100),
                    'is_fixed': effect.get('is_fixed', False)
                }]
            items_infos = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item_id2 = int_value(
                    entry.get('item_id', entry.get('shopItemModelId', entry.get('shopItemModelID'))),
                    0
                )
                if item_id2 <= 0:
                    continue
                items_infos.append({
                    'amount': max(1, int_value(entry.get('amount'), 1)),
                    'isFixedReward': bool(entry.get('is_fixed', entry.get('isFixedReward', False))),
                    'probability': max(0, int_value(entry.get('probability'), 100)),
                    'shopItemModelId': item_id2
                })
            if not items_infos:
                continue
            relatedinfos = json.dumps({
                'itemsInfos': items_infos
            })
            values = 0
        elif kind == 'api':
            effect_type = 9
            url = str(effect.get('url') or '').strip()
            if not url:
                continue
            relatedinfos = json.dumps({'url': url, 'useWebView': bool(effect.get('use_webview', False))})
            values = 0
        else:
            continue
        rows.append((now, item_id, effect_type, relatedinfos, 0, now, relatedid, values))
    return rows


def ensure_synthesis_category(cur, now):
    cur.execute("""
        SELECT id FROM synthesiscategory
        WHERE isdelete=0
        ORDER BY orderincategory, id
        LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        return row['id']
    cur.execute("""
        INSERT INTO synthesiscategory
        (categoryname, isasc, sortby, isdelete, orderincategory, status)
        VALUES (?, 0, NULL, 0, 0, 0)
    """, ('简易合成',))
    return cur.lastrowid


def create_simple_synthesis_recipes(cur, data, item_id, now):
    effects = data.get('effects', [])
    if not isinstance(effects, list):
        return []
    recipes = []
    for effect in effects:
        if not isinstance(effect, dict) or str(effect.get('type') or '') != 'synthesis':
            continue
        inputs = effect.get('inputs', [])
        if not isinstance(inputs, list):
            inputs = []
        material_rows = []
        for entry in inputs:
            if not isinstance(entry, dict):
                continue
            material_id = item_id if entry.get('input_current_item') else int_value(entry.get('item_id'), 0)
            if material_id <= 0:
                continue
            material_rows.append({
                'item_id': material_id,
                'amount': max(1, int_value(entry.get('amount'), 1))
            })
        if not material_rows:
            raise ValueError('请至少添加一个合成材料')

        output_item_id = int_value(effect.get('output_item_id'), 0)
        if output_item_id <= 0:
            raise ValueError('请选择合成获得的目标商品')
        output_amount = max(1, int_value(effect.get('output_amount'), 1))
        output_amount = 1 if output_amount != 1 else output_amount
        category_id = int_value(effect.get('category_id'), 0) or ensure_synthesis_category(cur, now)
        title = str(effect.get('title') or '').strip() or f"{data.get('name', '新商品')} 合成"
        note = str(effect.get('note') or '').strip()
        if not note:
            parts = [f"{row['amount']}x#{row['item_id']}" for row in material_rows]
            note = ' + '.join(parts) + f" -> 1x#{output_item_id}"

        cur.execute("SELECT COALESCE(MAX(orderincategory), 0) + 10 FROM synthesismodel WHERE categoryid=? AND isdel=0",
                    (category_id,))
        order_in_category = cur.fetchone()[0] or 10
        cur.execute("""
            INSERT INTO synthesismodel
            (createtime, name, description, isdel, orderincategory, updatetime, categoryid)
            VALUES (?, ?, ?, 0, ?, ?, ?)
        """, (now, title, note, order_in_category, now, category_id))
        recipe_id = cur.lastrowid

        for row in material_rows:
            cur.execute("""
                INSERT INTO synthesisconnmodel
                (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
                VALUES (?, ?, 0, ?, ?, 0, ?)
            """, (row['amount'], now, row['item_id'], recipe_id, now))
        cur.execute("""
            INSERT INTO synthesisconnmodel
            (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
            VALUES (?, ?, 1, ?, ?, 0, ?)
        """, (output_amount, now, output_item_id, recipe_id, now))
        recipes.append(recipe_id)
    return recipes


def achievement_overview_from_rows(rows, stale_before_ms):
    total = len(rows)
    done = sum(1 for row in rows if int_value(row.get('status'), 0) >= 1)
    near = []
    stagnant = []
    categories = {}
    for row in rows:
        category_id = row.get('category_id', 0)
        category_name = row.get('category_name') or '-'
        bucket = categories.setdefault(category_id, {
            'id': category_id,
            'name': category_name,
            'total': 0,
            'done': 0,
            'near': 0,
            'stagnant': 0
        })
        bucket['total'] += 1
        status = int_value(row.get('status'), 0)
        progress = clamp_percent(row.get('progress', 0))
        last_update = int_value(row.get('updatedtime'), 0)
        if status >= 1:
            bucket['done'] += 1
            continue
        entry = {
            'id': row.get('id'),
            'name': row.get('name') or '?',
            'category_name': category_name,
            'progress': progress,
            'updated_date': ms_to_date(last_update)
        }
        if progress >= 70:
            bucket['near'] += 1
            near.append(entry)
        if progress <= 0 or (last_update > 0 and last_update < stale_before_ms):
            bucket['stagnant'] += 1
            stagnant.append(entry)

    category_list = []
    for bucket in categories.values():
        bucket['done_rate'] = round(bucket['done'] / max(bucket['total'], 1) * 100, 1)
        category_list.append(bucket)
    category_list.sort(key=lambda item: (item['done_rate'], -item['total'], item['name']))
    near.sort(key=lambda item: item['progress'], reverse=True)
    stagnant.sort(key=lambda item: (item['progress'], item['updated_date'] or '0000-00-00'))
    done_rate = round(done / max(total, 1) * 100, 1)
    return {
        'total': total,
        'done': done,
        'pending': max(total - done, 0),
        'done_rate': done_rate,
        'near_done': near[:8],
        'near_count': len(near),
        'stagnant': stagnant[:8],
        'stagnant_count': len(stagnant),
        'categories': category_list[:12]
    }


def local_dashboard_overview():
    conn = get_db()
    try:
        cur = conn.cursor()
        stale_before_ms = now_ms() - 30 * 24 * 60 * 60 * 1000

        cur.execute("SELECT nickname, userhead, userid FROM usermodel WHERE id=1")
        user = dict(cur.fetchone() or {})

        cur.execute("SELECT savingbalance FROM coinmodel ORDER BY id DESC LIMIT 1")
        coin_row = cur.fetchone()
        coins = coin_row['savingbalance'] if coin_row else 0

        cur.execute("SELECT usingdays, currentusingdaystreak, longestusingdaystreak FROM recordmodel WHERE id=1")
        record = dict(cur.fetchone() or {})

        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0")
        task_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND taskstatus=0 AND isfrozen=0")
        task_active = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND taskstatus>=1")
        task_done = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND isfrozen=1")
        task_frozen = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND taskstatus=0 AND endtime>0 AND endtime < ?", (now_ms(),))
        task_overdue = cur.fetchone()[0]
        cur.execute("""
            SELECT t.id, t.content as name, t.updatedtime, c.categoryname
            FROM taskmodel t
            LEFT JOIN categorymodel c ON t.categoryid = c.id
            WHERE t.isdeleterecord=0 AND t.taskstatus=0 AND t.isfrozen=0
              AND COALESCE(t.updatedtime, t.createdtime, 0) < ?
            ORDER BY COALESCE(t.updatedtime, t.createdtime, 0)
            LIMIT 8
        """, (stale_before_ms,))
        stale_tasks = [
            {
                'id': r['id'],
                'name': r['name'] or '?',
                'category_name': r['categoryname'] or '-',
                'updated_date': ms_to_date(r['updatedtime'])
            }
            for r in cur.fetchall()
        ]

        cur.execute("SELECT COUNT(*) FROM shopitemmodel WHERE isdel=0")
        item_total = cur.fetchone()[0]
        cur.execute("""
            SELECT COUNT(*) as owned_count, COALESCE(SUM(stocknumber), 0) as owned_units
            FROM inventorymodel
            WHERE stocknumber > 0
        """)
        inv = dict(cur.fetchone() or {})
        cur.execute("SELECT COALESCE(AVG(NULLIF(price, 0)), 0) FROM shopitemmodel WHERE isdel=0")
        avg_item_price = round(cur.fetchone()[0] or 0, 1)

        cur.execute("""
            SELECT ua.id, ua.content as name, ua.achievementstatus, ua.currentvalue,
                   ua.progress, ua.createtime, ua.updatetime, ua.categoryid,
                   uac.categoryname
            FROM userachievementmodel ua
            LEFT JOIN userachcategorymodel uac ON ua.categoryid = uac.id
            WHERE ua.isdelete = 0
            ORDER BY uac.orderincategory, ua.orderincategory, ua.id
        """)
        achievement_rows = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT userachievementid, currentvalue, targetvalues, progress
            FROM unlockconditionmodel
            WHERE isdel = 0
        """)
        conditions = {}
        for r in cur.fetchall():
            conditions.setdefault(r['userachievementid'], []).append(dict(r))

        achievements = []
        for row in achievement_rows:
            conds = conditions.get(row['id'], [])
            if conds:
                current = sum(number_value(c.get('currentvalue'), 0) for c in conds)
                target = sum(number_value(c.get('targetvalues'), 0) for c in conds)
                progress = calc_progress(row.get('progress'), current, target)
            else:
                progress = calc_progress(row.get('progress'), row.get('currentvalue'), 0)
            achievements.append({
                'id': row['id'],
                'name': row.get('name') or '?',
                'status': row.get('achievementstatus') or 0,
                'progress': progress,
                'category_id': row.get('categoryid') or 0,
                'category_name': row.get('categoryname') or '-',
                'updatedtime': row.get('updatetime') or row.get('createtime') or 0
            })
        achievement_stats = achievement_overview_from_rows(achievements, stale_before_ms)

        cur.execute("SELECT COALESCE(SUM(experience), 0) FROM skillmodel WHERE isdel=0")
        total_exp = cur.fetchone()[0] or 0
        cur.execute("""
            SELECT COALESCE(SUM(rewardcoin),0) as coin, COALESCE(SUM(expreward),0) as exp
            FROM taskmodel
            WHERE isdeleterecord=0 AND taskstatus=0 AND isfrozen=0
        """)
        pending_reward = dict(cur.fetchone() or {})

        return {
            'meta': overview_meta('local'),
            'user': user,
            'record': record,
            'tasks': {
                'total': task_total,
                'active': task_active,
                'done': task_done,
                'frozen': task_frozen,
                'overdue': task_overdue,
                'stale': stale_tasks
            },
            'items': {
                'total': item_total,
                'owned': inv.get('owned_count', 0) or 0,
                'owned_units': inv.get('owned_units', 0) or 0,
                'coverage_rate': round((inv.get('owned_count', 0) or 0) / max(item_total, 1) * 100, 1),
                'avg_price': avg_item_price
            },
            'achievements': achievement_stats,
            'economy': {
                'coins': coins,
                'total_exp': total_exp,
                'pending_coin': pending_reward.get('coin', 0) or 0,
                'pending_exp': pending_reward.get('exp', 0) or 0,
                'avg_item_price': avg_item_price
            }
        }
    finally:
        conn.close()


def cloud_dashboard_overview():
    stale_before_ms = now_ms() - 30 * 24 * 60 * 60 * 1000
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            'info': pool.submit(cloud_request, {}, '/info', 5),
            'tasks': pool.submit(cloud_request, {}, '/tasks', 30),
            'items': pool.submit(cloud_request, {}, '/items', 45),
            'categories': pool.submit(cloud_request, {}, '/achievement_categories', 20),
            'coin': pool.submit(cloud_request, {}, '/coin', 12),
            'skills': pool.submit(cloud_request, {}, '/skills', 20),
        }
        futures['info'].result()
        task_rows = [row for row in as_cloud_rows(futures['tasks'].result()['data']) if isinstance(row, dict)]
        item_rows = [row for row in as_cloud_rows(futures['items'].result()['data']) if isinstance(row, dict)]
        cat_rows = [row for row in as_cloud_rows(futures['categories'].result()['data']) if isinstance(row, dict)]
        cat_names = {row.get('id'): row.get('name', '-') for row in cat_rows}

        achievement_futures = {}
        for cat in cat_rows:
            category_id = cat.get('id')
            if category_id is not None:
                achievement_futures[category_id] = pool.submit(
                    cloud_request, {}, f'/achievements/{category_id}', 20
                )
        all_achievements = []
        for category_id, future in achievement_futures.items():
            result = future.result()
            for row in as_cloud_rows(result['data']):
                if isinstance(row, dict):
                    row.setdefault('categoryId', category_id)
                    all_achievements.append(row)

        try:
            coin_data = futures['coin'].result()['data']
            coin_row = coin_data[0] if isinstance(coin_data, list) and coin_data else coin_data
            coins = first_value(coin_row, ['coin', 'coins', 'balance', 'savingbalance', 'savingBalance'], 0)
        except Exception:
            coins = 0
        try:
            skill_rows = [row for row in as_cloud_rows(futures['skills'].result()['data']) if isinstance(row, dict)]
            total_exp = sum(number_value(first_value(row, ['experience', 'exp'], 0), 0) for row in skill_rows)
        except Exception:
            total_exp = 0

    task_active = 0
    task_done = 0
    task_overdue = 0
    now = now_ms()
    for row in task_rows:
        status = int_value(first_value(row, ['status', 'taskstatus'], 0), 0)
        if status >= 1:
            task_done += 1
        else:
            task_active += 1
            deadline = int_value(first_value(row, ['deadline', 'endTime', 'endtime'], 0), 0)
            if deadline > 0 and deadline < now:
                task_overdue += 1

    owned_items = 0
    owned_units = 0
    total_price = 0
    priced = 0
    for row in item_rows:
        owned = int_value(first_value(row, ['ownNumber', 'inventory_count', 'count', 'stockNumber'], 0), 0)
        if owned > 0:
            owned_items += 1
            owned_units += owned
        price = number_value(row.get('price'), 0)
        if price > 0:
            total_price += price
            priced += 1

    achievements = []
    for row in all_achievements:
        category_id = first_value(row, ['categoryId', 'categoryid'], 0)
        current = first_value(row, ['currentValue', 'currentvalue'], 0)
        target = first_value(row, ['targetValue', 'targetvalues', 'target'], 0)
        progress = calc_progress(row.get('progress'), current, target)
        achievements.append({
            'id': row.get('id'),
            'name': first_value(row, ['name', 'title', 'content'], '?'),
            'status': first_value(row, ['status', 'achievementstatus'], 0),
            'progress': progress,
            'category_id': category_id,
            'category_name': cat_names.get(category_id, '-'),
            'updatedtime': first_value(row, ['updatetime', 'updatedTime', 'finishTime'], 0)
        })

    avg_price = round(total_price / max(priced, 1), 1)
    return {
        'meta': overview_meta('cloud'),
        'user': {},
        'record': {},
        'tasks': {
            'total': len(task_rows),
            'active': task_active,
            'done': task_done,
            'frozen': 0,
            'overdue': task_overdue,
            'stale': []
        },
        'items': {
            'total': len(item_rows),
            'owned': owned_items,
            'owned_units': owned_units,
            'coverage_rate': round(owned_items / max(len(item_rows), 1) * 100, 1),
            'avg_price': avg_price
        },
        'achievements': achievement_overview_from_rows(achievements, stale_before_ms),
        'economy': {
            'coins': coins,
            'total_exp': total_exp,
            'pending_coin': 0,
            'pending_exp': 0,
            'avg_item_price': avg_price
        }
    }


# ─── API 路由 ────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/status')
def status():
    return jsonify({
        'loaded': STATE['loaded'],
        'backup_path': STATE['backup_path'],
        'filename': os.path.basename(STATE['backup_path']) if STATE['backup_path'] else None
    })


@app.route('/api/media/<folder>/<path:filename>')
def api_media(folder, filename):
    if folder not in MEDIA_FOLDERS:
        return jsonify({'error': '未知媒体目录'}), 404
    if not STATE['loaded']:
        return jsonify({'error': '未加载备份'}), 400
    media_root = os.path.abspath(os.path.join(STATE['tmpdir'], 'media', folder))
    target = os.path.abspath(os.path.join(media_root, filename))
    if target != media_root and not target.startswith(media_root + os.sep):
        return jsonify({'error': '非法媒体路径'}), 400
    if not os.path.exists(target):
        return jsonify({'error': '媒体文件不存在'}), 404
    return send_from_directory(media_root, os.path.relpath(target, media_root))

@app.route('/api/open', methods=['POST'])
def open_backup():
    data = request.get_json()
    path = data.get('path', '')
    paths = data.get('paths', [])

    # 批量加载模式
    if paths:
        results = []
        errors = []
        last_ok = None
        for p in paths:
            if not p or not os.path.exists(p):
                errors.append({'path': p, 'error': f'文件不存在: {p}'})
                continue
            try:
                load_backup(p)
                last_ok = p
                results.append({'path': p, 'ok': True})
            except Exception as e:
                errors.append({'path': p, **_backup_error_details(e)})
        msg = f'成功加载 {len(results)} 个文件'
        if errors:
            msg += f'，{len(errors)} 个失败'
        return jsonify({
            'ok': len(results) > 0,
            'path': last_ok or path,
            'results': results,
            'errors': errors,
            'message': msg
        })

    # 单文件模式
    if not path:
        return jsonify({'error': '请输入文件路径'}), 400
    if not os.path.exists(path):
        return jsonify({'error': f'文件不存在: {path}', 'suggestion': '请检查文件路径是否正确，文件是否已被移动或删除'}), 400
    # 检查是否为 zip 文件
    if not path.lower().endswith('.zip'):
        return jsonify({'error': '仅支持 LifeUp 备份 ZIP 文件', 'suggestion': '请选择 .zip 格式的备份文件'}), 400
    try:
        # 检查文件可读性
        with open(path, 'rb') as f:
            f.read(4)  # 读取 4 字节验证文件可读
        load_backup(path)
        return jsonify({'ok': True, 'path': path, 'filename': os.path.basename(path)})
    except BackupValidationError as e:
        return jsonify(_backup_error_details(e)), 400
    except zipfile.BadZipFile:
        return jsonify({'error': '文件已损坏，不是有效的 ZIP 文件', 'suggestion': '请重新从 LifeUp App 导出备份'}), 400
    except FileNotFoundError as e:
        return jsonify({'error': str(e), 'suggestion': '备份中缺少数据库文件，可能是不完整的备份'}), 400
    except PermissionError:
        return jsonify({'error': '文件被占用，无法读取', 'suggestion': '请关闭其他正在使用该文件的程序后重试'}), 400
    except Exception as e:
        return jsonify({'error': f'加载失败: {str(e)}'}), 500


@app.route('/api/open-upload', methods=['POST'])
def open_uploaded_backup():
    """Copy browser-selected backups into a managed workspace, then load the last valid copy."""
    uploads = request.files.getlist('files')
    if not uploads:
        return jsonify({
            'error': '没有选择 ZIP 备份文件',
            'suggestion': '请选择 LifeUp App 直接导出的 .zip 备份。'
        }), 400
    if len(uploads) > 20:
        return jsonify({
            'error': '一次最多选择 20 个备份文件',
            'suggestion': '请分批选择备份，每次不超过 20 个。'
        }), 400

    os.makedirs(BROWSER_IMPORT_DIR, exist_ok=True)
    results = []
    errors = []
    last_path = None
    for upload in uploads:
        original_name = upload.filename or 'LifeupBackup.zip'
        safe_name = secure_filename(original_name) or 'LifeupBackup.zip'
        if not safe_name.lower().endswith('.zip'):
            errors.append({
                'filename': original_name,
                'error': '只支持 ZIP 备份文件',
                'suggestion': '请选择 LifeUp App 直接导出的 .zip 备份。'
            })
            continue
        unique_name = f'{datetime.now().strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(4)}-{safe_name}'
        workspace_path = os.path.abspath(os.path.join(BROWSER_IMPORT_DIR, unique_name))
        try:
            upload.save(workspace_path)
            load_backup(workspace_path)
            last_path = workspace_path
            results.append({'filename': original_name, 'path': workspace_path, 'ok': True})
        except Exception as exc:
            try:
                os.remove(workspace_path)
            except OSError:
                pass
            errors.append({'filename': original_name, **_backup_error_details(exc)})

    if not last_path:
        first_error = errors[0] if errors else {
            'error': '没有可加载的备份文件',
            'suggestion': '请选择 LifeUp App 直接导出的 ZIP 备份。'
        }
        return jsonify({
            'error': f'上传的备份无法加载: {first_error["error"]}',
            'suggestion': first_error['suggestion'],
            'errors': errors
        }), 400
    return jsonify({
        'ok': True,
        'path': last_path,
        'filename': os.path.basename(last_path),
        'count': len(results),
        'results': results,
        'errors': errors,
        'workspace_copy': True
    })

@app.route('/api/save', methods=['POST'])
def save():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    data = request.get_json(silent=True)
    if data is None and request.content_length:
        error = BackupExportError(
            'INVALID_REQUEST',
            '请求内容不是有效的 JSON 对象',
            '浏览器导出无需填写路径；手动调用时请发送 JSON 对象。',
            400,
        )
        return jsonify(_export_error_details(error)), error.status
    if data is None:
        data = {}
    if not isinstance(data, dict):
        error = BackupExportError(
            'INVALID_REQUEST',
            '请求内容必须是 JSON 对象',
            '浏览器导出无需填写路径；手动调用时可传入 exports 目录中的 path。',
            400,
        )
        return jsonify(_export_error_details(error)), error.status
    try:
        result = save_backup(data.get('path'))
        return jsonify({'ok': True, **result})
    except BackupExportError as exc:
        return jsonify(_export_error_details(exc)), exc.status
    except OSError as exc:
        error = BackupExportError(
            'EXPORT_IO_ERROR',
            '导出文件写入或发布失败',
            '请检查磁盘空间、目录权限，以及目标 ZIP 是否被其他程序占用。',
        )
        app.logger.warning('Backup export I/O error: %s', exc)
        return jsonify(_export_error_details(error)), error.status
    except Exception as exc:
        error = BackupExportError(
            'EXPORT_FAILED',
            '导出备份失败',
            '当前来源文件未被覆盖；请保持页面打开后重试。',
        )
        app.logger.exception('Unexpected backup export error: %s', exc)
        return jsonify(_export_error_details(error)), error.status


def _snapshot_error_response(error):
    return jsonify(_export_error_details(error)), error.status


def _snapshot_validation_error(error):
    if error.code == 'NO_BACKUP_LOADED':
        return SnapshotError(
            error.code, str(error), error.suggestion, error.status
        )
    return SnapshotError(
        'SNAPSHOT_VALIDATION_FAILED',
        '快照未通过 ZIP 或 SQLite 完整性校验',
        '没有发布或恢复损坏文件；请重新加载完整备份后重试。',
        422,
    )


@app.route('/api/snapshots', methods=['GET', 'POST'])
def api_snapshots():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    try:
        if request.method == 'GET':
            limit, offset = _snapshot_pagination_args()
            with STATE_LOCK:
                snapshots, total = list_snapshots(limit=limit, offset=offset)
            return jsonify({
                'ok': True,
                'snapshots': snapshots,
                'count': len(snapshots),
                'pagination': {
                    'limit': limit,
                    'offset': offset,
                    'total': total,
                },
            })

        data = request.get_json(silent=True)
        if data is None and request.content_length:
            raise SnapshotError(
                'INVALID_REQUEST',
                '请求内容不是有效的 JSON 对象',
                '创建快照只需发送可选的 name 文本字段。',
                400,
            )
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise SnapshotError(
                'INVALID_REQUEST',
                '请求内容必须是 JSON 对象',
                '创建快照只需发送可选的 name 文本字段。',
                400,
            )
        unknown_fields = sorted(set(data) - {'name'})
        if unknown_fields:
            raise SnapshotError(
                'INVALID_REQUEST',
                '创建快照不接受路径、文件名或其他未知字段',
                '只保留 name 字段；快照路径由服务器安全生成。',
                400,
            )
        snapshot = create_snapshot(data.get('name'))
        return jsonify({'ok': True, 'snapshot': snapshot}), 201
    except SnapshotError as exc:
        return _snapshot_error_response(exc)
    except BackupExportError as exc:
        return _snapshot_error_response(_snapshot_validation_error(exc))
    except OSError as exc:
        error = SnapshotError(
            'SNAPSHOT_IO_ERROR',
            '快照目录或文件无法读写',
            '请检查磁盘空间、目录权限，以及 ZIP 是否被其他程序占用。',
            500,
        )
        app.logger.warning('Snapshot I/O error: %s', exc)
        return _snapshot_error_response(error)
    except Exception as exc:
        error = SnapshotError(
            'SNAPSHOT_FAILED',
            '快照操作失败',
            '当前工作副本和原始备份未被覆盖；请刷新后重试。',
            500,
        )
        app.logger.exception('Unexpected snapshot error: %s', exc)
        return _snapshot_error_response(error)


@app.route('/api/snapshots/<path:snapshot_id>/restore', methods=['POST'])
def api_restore_snapshot(snapshot_id):
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    try:
        result = restore_snapshot(snapshot_id)
        return jsonify({'ok': True, **result})
    except SnapshotError as exc:
        return _snapshot_error_response(exc)
    except OSError as exc:
        error = SnapshotError(
            'SNAPSHOT_IO_ERROR',
            '恢复工作副本无法写入或载入',
            '当前工作副本保持不变；请检查磁盘空间和目录权限后重试。',
            500,
        )
        app.logger.warning('Snapshot restore I/O error: %s', exc)
        return _snapshot_error_response(error)
    except Exception as exc:
        error = SnapshotError(
            'SNAPSHOT_RESTORE_FAILED',
            '恢复快照失败',
            '当前工作副本保持不变；请刷新快照列表后重试。',
            500,
        )
        app.logger.exception('Unexpected snapshot restore error: %s', exc)
        return _snapshot_error_response(error)


@app.route('/api/snapshots/<path:snapshot_id>', methods=['DELETE'])
def api_delete_snapshot(snapshot_id):
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    try:
        deleted = delete_snapshot(snapshot_id)
        return jsonify({'ok': True, **deleted})
    except SnapshotError as exc:
        return _snapshot_error_response(exc)
    except OSError as exc:
        error = SnapshotError(
            'SNAPSHOT_IO_ERROR',
            '快照文件无法删除',
            '请关闭正在占用快照 ZIP 的程序后重试。',
            500,
        )
        app.logger.warning('Snapshot delete I/O error: %s', exc)
        return _snapshot_error_response(error)
    except Exception as exc:
        error = SnapshotError(
            'SNAPSHOT_DELETE_FAILED',
            '删除快照失败',
            '没有删除当前工作副本或原始备份；请刷新快照列表后重试。',
            500,
        )
        app.logger.exception('Unexpected snapshot delete error: %s', exc)
        return _snapshot_error_response(error)


# ─── 总览 ───────────────────────────────────────────────

@app.route('/api/dashboard')
def dashboard():
    conn = get_db()
    try:
        cur = conn.cursor()

        # 用户信息
        cur.execute("SELECT nickname, userhead, userid FROM usermodel WHERE id=1")
        user = dict(cur.fetchone() or {})

        # 金币余额（从 coinmodel 取最新 savingbalance）
        cur.execute("SELECT savingbalance FROM coinmodel ORDER BY id DESC LIMIT 1")
        coin_row = cur.fetchone()
        coins = coin_row['savingbalance'] if coin_row else 0

        # 使用天数
        cur.execute("SELECT usingdays, currentusingdaystreak, longestusingdaystreak FROM recordmodel WHERE id=1")
        record = dict(cur.fetchone() or {})

        # 任务统计（去重：同名任务只计最新一条）
        cur.execute("""
            SELECT COUNT(*) as total FROM taskmodel t1
            WHERE t1.isdeleterecord=0 AND t1.isfrozen=0
              AND t1.id = (SELECT MAX(t2.id) FROM taskmodel t2 WHERE t2.content = t1.content AND t2.isdeleterecord=0 AND t2.isfrozen=0)
        """)
        task_total = cur.fetchone()['total']
        cur.execute("""
            SELECT COUNT(*) as active FROM taskmodel t1
            WHERE t1.isdeleterecord=0 AND t1.isfrozen=0 AND t1.taskstatus=0
              AND t1.id = (SELECT MAX(t2.id) FROM taskmodel t2 WHERE t2.content = t1.content AND t2.isdeleterecord=0 AND t2.isfrozen=0)
        """)
        task_active = cur.fetchone()['active']
        cur.execute("""
            SELECT COUNT(*) as done FROM taskmodel t1
            WHERE t1.isdeleterecord=0 AND t1.taskstatus=1
              AND t1.id = (SELECT MAX(t2.id) FROM taskmodel t2 WHERE t2.content = t1.content AND t2.isdeleterecord=0 AND t2.isfrozen=0)
        """)
        task_done = cur.fetchone()['done']

        # 商品统计
        cur.execute("SELECT COUNT(*) as total FROM shopitemmodel WHERE isdel=0")
        item_total = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) as inv FROM inventorymodel WHERE stocknumber>0")
        inv_count = cur.fetchone()['inv']

        # 成就统计
        cur.execute("SELECT COUNT(*) as total FROM userachievementmodel WHERE isdelete=0")
        ach_total = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) as done FROM userachievementmodel WHERE isdelete=0 AND achievementstatus>=1")
        ach_done = cur.fetchone()['done']

        # 系统成就
        cur.execute("SELECT COUNT(*) as total FROM achievementinfomodel")
        sys_ach = cur.fetchone()['total']

        # 技能/属性
        cur.execute("SELECT id, content as name, description, experience, type, color, icon, status FROM skillmodel WHERE isdel=0 ORDER BY orderincategory")
        skills = [dict(r) for r in cur.fetchall()]

        # 属性等级
        cur.execute("SELECT * FROM attributemodel WHERE id=1")
        attrs = dict(cur.fetchone() or {})

        # 等级计算 (简单估算)
        cur.execute("SELECT perlevelexp FROM levelmodel WHERE id=1")
        level_row = cur.fetchone()
        per_level_exp = level_row['perlevelexp'] if level_row else 100

        for s in skills:
            exp = s.get('experience', 0) or 0
            s['level'] = max(1, exp // per_level_exp + 1)
            s['current_exp'] = exp
            s['next_exp'] = s['level'] * per_level_exp
            s['progress'] = round((exp % per_level_exp) / per_level_exp * 100, 1)

        total_exp = sum(s['experience'] or 0 for s in skills)
        estimated_level = max(1, total_exp // per_level_exp)
        next_exp = (estimated_level) * per_level_exp
        level_progress = ((total_exp % per_level_exp) / per_level_exp * 100) if per_level_exp else 0

        return jsonify({
            'user': user,
            'coins': coins,
            'level': {'current': estimated_level, 'total_exp': total_exp, 'next_exp': next_exp, 'progress': round(level_progress, 1)},
            'record': record,
            'tasks': {'total': task_total, 'active': task_active, 'done': task_done},
            'items': {'total': item_total, 'inventory': inv_count},
            'achievements': {'total': ach_total, 'done': ach_done, 'system': sys_ach},
            'skills': skills,
            'attributes': attrs,
            'per_level_exp': per_level_exp
        })
    finally:
        conn.close()


@app.route('/api/dashboard/overview')
def dashboard_overview():
    source = request.args.get('source', 'local')
    try:
        if source == 'cloud':
            return jsonify(cloud_dashboard_overview())
        return jsonify(local_dashboard_overview())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/focus/overview')
def focus_overview():
    source = request.args.get('source', 'local')
    try:
        if source == 'cloud':
            return jsonify(cloud_focus_overview())
        return jsonify(local_focus_overview())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/focus/start', methods=['POST'])
def api_focus_start():
    data = request.get_json() or {}
    try:
        return jsonify(focus_start_native(data))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── 任务 CRUD ──────────────────────────────────────────

@app.route('/api/tasks')
def list_tasks():
    if request.args.get('source') == 'cloud':
        return list_cloud_tasks_for_dashboard()
    conn = get_db()
    try:
        cur = conn.cursor()
        filter_type = request.args.get('filter', 'all')  # all, active, done, frozen
        show_frozen = request.args.get('show_frozen', '0')  # 1=显示冻结
        if filter_type == 'frozen': show_frozen = '1'  # frozen筛选自动显示冻结
        search = request.args.get('search', '').strip()
        cat_id = request.args.get('category_id', '')
        status_cond = {'all': '1=1', 'active': 'taskstatus=0', 'done': 'taskstatus>=1', 'frozen': 't1.isfrozen=1'}.get(filter_type, '1=1')
        frozen_cond = '' if show_frozen == '1' else 'AND t1.isfrozen=0'
        search_cond = 'AND (t1.content LIKE ? OR t1.remark LIKE ?)' if search else ''
        search_params = [f'%{search}%', f'%{search}%'] if search else []
        cat_cond = f'AND t1.categoryid={int(cat_id)}' if cat_id else ''

        # 去重策略：进行中/全部 → 同名只保留最新；已完成/冻结 → 保留全部历史
        dedup_sql = """
              AND t1.id = (
                SELECT MAX(t2.id) FROM taskmodel t2
                WHERE t2.content = t1.content
                  AND t2.isdeleterecord=0 AND t2.isfrozen=0
              )
        """ if filter_type not in ('done', 'frozen') else ""

        cur.execute(f"""
            SELECT t1.id, t1.content as title, t1.taskfrequency as frequency, t1.rewardcoin as coin,
                   t1.expreward as exp, t1.remark as note, t1.taskstatus as done,
                   t1.currenttimes as done_count, t1.categoryid, t1.createdtime,
                   t1.updatedtime, t1.tagcolor, t1.taskdifficultydegree as difficulty,
                   t1.isfrozen, t1.taskurgencydegree as priority, t1.groupid, t1.tasktype,
                   t1.starttime, t1.endtime, t1.rewardcoinvariable,
                   t1.extrainfo, t1.enableebbinghausmode, t1.ishandleoverdue
            FROM taskmodel t1
            WHERE t1.isdeleterecord=0 {frozen_cond} AND {status_cond} {search_cond} {cat_cond}
            {dedup_sql}
            ORDER BY t1.taskurgencydegree DESC, t1.createdtime DESC
        """, search_params)
        tasks = [dict(r) for r in cur.fetchall()]

        # 获取分类名称
        cur.execute("SELECT id, categoryname FROM categorymodel")
        cats = {r['id']: r['categoryname'] for r in cur.fetchall()}

        # 获取 tasktarget（目标次数）
        cur.execute("SELECT id, targettimes FROM tasktargetmodel")
        targets = {r['id']: r['targettimes'] for r in cur.fetchall()}

        # 获取技能关联
        cur.execute("SELECT taskmodel_id, skillids FROM taskmodel_skillids")
        skill_links = {}
        for r in cur.fetchall():
            skill_links.setdefault(r['taskmodel_id'], []).append(r['skillids'])
        # 获取技能名称
        cur.execute("SELECT id, content FROM skillmodel WHERE isdel=0")
        skill_names = {r['id']: r['content'] for r in cur.fetchall()}

        # 获取商品奖励
        cur.execute("SELECT id, taskmodelid, shopitemmodelid, amount FROM taskrewardmodel WHERE taskmodelid IS NOT NULL")
        reward_links = {}
        for r in cur.fetchall():
            reward_links.setdefault(r['taskmodelid'], []).append({'id': r['id'], 'item_id': r['shopitemmodelid'], 'amount': r['amount']})

        for t in tasks:
            t['category_name'] = cats.get(t.get('categoryid'), '-')
            t['target_count'] = targets.get(t.get('id'), 1)
            # 技能关联
            linked_skills = skill_links.get(t['id'], [])
            t['skill_ids'] = linked_skills
            t['skill_names'] = [skill_names.get(sid, '?') for sid in linked_skills]
            # 解析 extrainfo JSON
            try:
                t['extrainfo_obj'] = _json3.loads(t.get('extrainfo') or '{}')
            except:
                t['extrainfo_obj'] = {}
            # 商品奖励
            t['item_rewards'] = reward_links.get(t['id'], [])

        return jsonify(tasks)
    finally:
        conn.close()

@app.route('/api/tasks/add', methods=['POST'])
def add_task():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        # 先插入 tasktarget
        target_times = data.get('target_count', 1)
        cur.execute("INSERT INTO tasktargetmodel (targettimes, extraexpreward, repeatendinclusive, repeatendmode, repeatendbehavior) VALUES (?, 0, 1, 0, 0)",
                    (target_times,))
        target_id = cur.lastrowid

        # 开始/截止时间
        st = data.get('start_time', '')
        et = data.get('end_time', '')
        # 校验：endtime 至少 = starttime + 24h，否则 LifeUp 可能崩溃
        if st and et:
            try:
                if int(et) - int(st) < 86400000:
                    et = str(int(st) + 86400000)
            except: pass

        # 生成 extrainfo JSON（LifeUp 任务元数据）
        coin_pf = float(data.get('coin_punishment_factor', 0))
        exp_pf = float(data.get('exp_punishment_factor', 0))
        extrainfo = json.dumps({
            "autoUseItems": bool(data.get('auto_use_items', False)),
            "coinPunishmentFactor": coin_pf,
            "expPunishmentFactor": exp_pf,
            "t_f_m": 1,
            "writeFeelings": False
        })

        cur.execute("""
            INSERT INTO taskmodel (
                content, taskfrequency, rewardcoin, expreward, remark,
                taskstatus, currenttimes, categoryid, createdtime, updatedtime, isdeleterecord,
                isfrozen, tagcolor, taskdifficultydegree, priority, tasktargetid,
                userid, isshared, tasktype, isneedtoremake, enableebbinghausmode,
                taskurgencydegree, ishandleoverdue, rewardcoinvariable,
                relatedattribute1, relatedattribute2, relatedattribute3,
                teamrecordid, teamid, taskid, taskcountextraid,
                lasttaskid, nexttaskid, groupid, orderincategory,
                isusespecificexpiretime, isuserinputstarttime, starttime, endtime,
                extrainfo, completereward
            ) VALUES (
                ?, ?, ?, ?, ?,
                0, 0, ?, ?, ?, 0,
                0, ?, ?, ?, ?,
                0, 0, ?, 0, ?,
                ?, ?, ?,
                ?, ?, ?,
                -1, -1, 0, -1,
                0, 0, 0, 0,
                0, 0, ?, ?,
                ?, ?
            )
        """, (
            data.get('title', '新任务'),
            data.get('frequency', 1),
            data.get('coin', 0),
            data.get('exp', 0),
            data.get('note', ''),
            data.get('category_id', 0),
            now, now,
            data.get('tagcolor', 0),
            data.get('difficulty', 1),
            0,
            target_id,
            data.get('tasktype', 0),
            data.get('enable_ebbinghaus', 0),
            data.get('priority', 1),
            data.get('ishandleoverdue', 0),
            data.get('rewardcoinvariable', 0),
            data.get('attr1', ''),
            data.get('attr2', ''),
            data.get('attr3', ''),
            st if st else now,
            et if et else (st if st else now),
            extrainfo,
            ''
        ))
        new_id = cur.lastrowid

        # 插入技能关联
        import json as _json
        skill_ids = data.get('skill_ids', [])
        if isinstance(skill_ids, str):
            skill_ids = _json.loads(skill_ids)
        for sid in skill_ids:
            cur.execute("INSERT INTO taskmodel_skillids (taskmodel_id, skillids) VALUES (?, ?)", (new_id, int(sid)))

        # 插入商品奖励
        item_rewards = data.get('item_rewards', [])
        if isinstance(item_rewards, str):
            item_rewards = _json.loads(item_rewards)
        for rw in item_rewards:
            cur.execute("INSERT INTO taskrewardmodel (taskmodelid, shopitemmodelid, amount, createtime, updatetime) VALUES (?, ?, ?, ?, ?)",
                        (new_id, int(rw['item_id']), int(rw.get('amount', 1)), now, now))

        conn.commit()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tasks/update', methods=['POST'])
def update_task():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        cur.execute("""
            UPDATE taskmodel SET content=?, taskfrequency=?, rewardcoin=?, expreward=?,
                remark=?, categoryid=?, updatedtime=?, taskdifficultydegree=?, tagcolor=?,
                taskurgencydegree=?, tasktype=?, rewardcoinvariable=?
            WHERE id=?
        """, (
            data.get('title'),
            data.get('frequency', 1),
            data.get('coin', 0),
            data.get('exp', 0),
            data.get('note', ''),
            data.get('category_id', 0),
            now,
            data.get('difficulty', 1),
            data.get('tagcolor', 0),
            data.get('priority', 0),
            data.get('tasktype', 0),
            data.get('rewardcoinvariable', 0),
            data['id']
        ))
        # 更新冻结状态
        if 'isfrozen' in data:
            cur.execute("UPDATE taskmodel SET isfrozen=? WHERE id=?",
                       (1 if data['isfrozen'] else 0, data['id']))
        # 更新开始/截止时间
        if 'start_time' in data:
            st = data['start_time']
            et = data.get('end_time')
            if st and et:
                try:
                    if int(et) - int(st) < 86400000:
                        et = str(int(st) + 86400000)
                except: pass
            cur.execute("UPDATE taskmodel SET starttime=?, endtime=? WHERE id=?",
                       (st, et, data['id']))
        elif 'end_time' in data:
            cur.execute("UPDATE taskmodel SET endtime=? WHERE id=?",
                       (data['end_time'], data['id']))
        # 更新惩罚系数（写入 extrainfo JSON）
        if any(k in data for k in ('coin_punishment_factor', 'exp_punishment_factor', 'auto_use_items')):
            cur.execute("SELECT extrainfo FROM taskmodel WHERE id=?", (data['id'],))
            ei_row = cur.fetchone()
            if ei_row and ei_row['extrainfo']:
                try:
                    ei = json.loads(ei_row['extrainfo']) if isinstance(ei_row['extrainfo'], str) else (ei_row['extrainfo'] or {})
                except:
                    ei = {}
            else:
                ei = {}
            if 'coin_punishment_factor' in data:
                ei['coinPunishmentFactor'] = float(data['coin_punishment_factor'])
            if 'exp_punishment_factor' in data:
                ei['expPunishmentFactor'] = float(data['exp_punishment_factor'])
            if 'auto_use_items' in data:
                ei['autoUseItems'] = bool(data['auto_use_items'])
            cur.execute("UPDATE taskmodel SET extrainfo=? WHERE id=?", (json.dumps(ei), data['id']))
        # 更新艾宾浩斯 & 逾期处理
        if 'enable_ebbinghaus' in data:
            cur.execute("UPDATE taskmodel SET enableebbinghausmode=? WHERE id=?", (data['enable_ebbinghaus'], data['id']))
        if 'ishandleoverdue' in data:
            cur.execute("UPDATE taskmodel SET ishandleoverdue=? WHERE id=?", (data['ishandleoverdue'], data['id']))
        # 更新 tasktarget
        if 'target_count' in data:
            cur.execute("UPDATE tasktargetmodel SET targettimes=? WHERE id=(SELECT tasktargetid FROM taskmodel WHERE id=?)",
                        (data['target_count'], data['id']))

        # 更新属性关联
        if any(k in data for k in ('attr1', 'attr2', 'attr3')):
            cur.execute("UPDATE taskmodel SET relatedattribute1=?, relatedattribute2=?, relatedattribute3=? WHERE id=?",
                       (data.get('attr1', ''), data.get('attr2', ''), data.get('attr3', ''), data['id']))

        # 更新技能关联（先删后插）
        if 'skill_ids' in data:
            cur.execute("DELETE FROM taskmodel_skillids WHERE taskmodel_id=?", (data['id'],))
            import json as _json2
            skill_ids = data['skill_ids']
            if isinstance(skill_ids, str):
                skill_ids = _json2.loads(skill_ids)
            for sid in skill_ids:
                cur.execute("INSERT INTO taskmodel_skillids (taskmodel_id, skillids) VALUES (?, ?)", (data['id'], int(sid)))

        # 更新商品奖励（先删后插）
        if 'item_rewards' in data:
            cur.execute("DELETE FROM taskrewardmodel WHERE taskmodelid=?", (data['id'],))
            import json as _json3
            item_rewards = data['item_rewards']
            if isinstance(item_rewards, str):
                item_rewards = _json3.loads(item_rewards)
            for rw in item_rewards:
                cur.execute("INSERT INTO taskrewardmodel (taskmodelid, shopitemmodelid, amount, createtime, updatetime) VALUES (?, ?, ?, ?, ?)",
                            (data['id'], int(rw['item_id']), int(rw.get('amount', 1)), now, now))

        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tasks/delete', methods=['POST'])
def delete_task():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE taskmodel SET isdeleterecord=1, updatedtime=? WHERE id=?", (now_ms(), data['id']))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ============================================================
# 子任务 API (P1)
# ============================================================

@app.route('/api/tasks/<int:task_id>/subtasks', methods=['GET'])
def list_subtasks(task_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.*,
                   CASE WHEN s.shopitemmodelid != 0 THEN i.itemname ELSE '' END as itemname
            FROM subtaskmodel s
            LEFT JOIN shopitemmodel i ON i.id = s.shopitemmodelid
            WHERE s.taskmodelid = ?
            ORDER BY s.orderincategory, s.id
        """, (task_id,))
        rows = cur.fetchall()
        status_names = {0: '待完成', 1: '已完成', 2: '已放弃'}
        return jsonify([{
            'id': r[0], 'createtime': r[1], 'subtaskgroupid': r[2],
            'shopitemamount': r[3], 'taskmodelid': r[4], 'content': r[5],
            'shopitemmodelid': r[6], 'taskstatus': r[7], 'rewardcoin': r[8],
            'expreward': r[9], 'rewardcoinvariable': r[10], 'orderincategory': r[11],
            'updatetime': r[12], 'remindtime': r[13], 'endtime': r[14],
            'itemname': r[15] if len(r) > 15 else '',
            'status_name': status_names.get(r[7], str(r[7]))
        } for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tasks/<int:task_id>/subtasks/add', methods=['POST'])
def add_subtask(task_id):
    data = request.get_json()
    conn = get_db()
    now = now_ms()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO subtaskmodel (
                taskmodelid, content, taskstatus, rewardcoin, expreward,
                rewardcoinvariable, shopitemmodelid, shopitemamount,
                subtaskgroupid, orderincategory,
                createtime, updatetime, remindtime, endtime
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id,
            data.get('content', '新子任务'),
            data.get('taskstatus', 0),
            data.get('rewardcoin', 0),
            data.get('expreward', 0),
            data.get('rewardcoinvariable', 0),
            data.get('shopitemmodelid', 0),
            data.get('shopitemamount', 0),
            data.get('subtaskgroupid', 0),
            data.get('orderincategory', 0),
            now, now,
            data.get('remindtime', 0),
            data.get('endtime', 0)
        ))
        new_id = cur.lastrowid
        conn.commit()
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/subtasks/update', methods=['POST'])
def update_subtask():
    data = request.get_json()
    conn = get_db()
    now = now_ms()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subtaskmodel SET
                content=?, taskstatus=?, rewardcoin=?, expreward=?,
                rewardcoinvariable=?, shopitemmodelid=?, shopitemamount=?,
                orderincategory=?, remindtime=?, endtime=?, updatetime=?
            WHERE id=?
        """, (
            data.get('content', ''),
            data.get('taskstatus', 0),
            data.get('rewardcoin', 0),
            data.get('expreward', 0),
            data.get('rewardcoinvariable', 0),
            data.get('shopitemmodelid', 0),
            data.get('shopitemamount', 0),
            data.get('orderincategory', 0),
            data.get('remindtime', 0),
            data.get('endtime', 0),
            now,
            data['id']
        ))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/subtasks/delete', methods=['POST'])
def delete_subtask():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM subtaskmodel WHERE id=?", (data['id'],))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ============================================================
# 清单/分组 API (P1)
# ============================================================

@app.route('/api/categories/groups', methods=['GET'])
def list_groups():
    """列出所有分组（categorymodel 用作清单容器）"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.id, c.categoryname, c.categorytype,
                   COUNT(t.id) as task_count
            FROM categorymodel c
            LEFT JOIN taskmodel t ON t.categoryid = c.id AND t.isdeleterecord = 0
            GROUP BY c.id
            ORDER BY c.id
        """)
        rows = cur.fetchall()
        return jsonify([{
            'id': r[0], 'categoryname': r[1], 'categorytype': r[2],
            'task_count': r[3]
        } for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tasks/group', methods=['POST'])
def set_task_group():
    """设置任务的 groupid（将同组任务关联，用于清单展示）"""
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE taskmodel SET groupid=?, updatedtime=? WHERE id=?",
                    (data.get('groupid', 0), now_ms(), data['id']))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tasks/batch/freeze', methods=['POST'])
def batch_freeze():
    """批量冻结/解冻（按 groupid 或 id 列表）"""
    data = request.get_json(silent=True)
    try:
        if not isinstance(data, dict):
            raise BatchValidationError('请求内容必须是 JSON 对象')
        if type(data.get('isfrozen')) is not bool:
            raise BatchValidationError('isfrozen 必须是布尔值')
        frozen = 1 if data['isfrozen'] else 0
        groupid = data.get('groupid')
        if groupid is not None:
            if type(groupid) is not int or groupid <= 0:
                raise BatchValidationError('groupid 必须是正整数')
            if data.get('ids') not in (None, []):
                raise BatchValidationError('ids 和 groupid 不能同时提供')
            ids = None
        else:
            ids = validate_batch_ids(data.get('ids'), '任务')
    except BatchValidationError as exc:
        return jsonify({'error': str(exc)}), 400

    now = now_ms()
    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.cursor()
        if groupid is not None:
            cur.execute(
                "SELECT id FROM taskmodel "
                "WHERE groupid=? AND isdeleterecord=0 LIMIT ?",
                (groupid, MAX_BATCH_SIZE + 1),
            )
            group_ids = [row[0] for row in cur.fetchall()]
            if not group_ids:
                raise BatchValidationError('该任务分组不存在或没有可处理任务')
            if len(group_ids) > MAX_BATCH_SIZE:
                raise BatchValidationError(
                    f'单次最多处理 {MAX_BATCH_SIZE} 个任务'
                )
            cur.execute("UPDATE taskmodel SET isfrozen=?, updatedtime=? WHERE groupid=? AND isdeleterecord=0",
                        (frozen, now, groupid))
            affected = cur.rowcount
        else:
            ensure_batch_targets_exist(cur, 'taskmodel', ids, '任务')
            placeholders = ','.join(['?'] * len(ids))
            cur.execute(f"UPDATE taskmodel SET isfrozen=?, updatedtime=? WHERE id IN ({placeholders})",
                        [frozen, now] + ids)
            affected = cur.rowcount
        conn.commit()
        return jsonify({'ok': True, 'affected': affected})
    except BatchValidationError as exc:
        conn.rollback()
        return jsonify({'error': str(exc)}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ============================================================
# 任务统计 & 复制 API (P2)
# ============================================================

@app.route('/api/tasks/stats')
def tasks_stats():
    """任务概览统计"""
    conn = get_db()
    try:
        cur = conn.cursor()
        # 总数
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0")
        total = cur.fetchone()[0]
        # 进行中
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND taskstatus=0")
        active = cur.fetchone()[0]
        # 已完成
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND taskstatus=1")
        done = cur.fetchone()[0]
        # 冻结
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND isfrozen=1")
        frozen = cur.fetchone()[0]
        # 逾期
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND taskstatus=0 AND endtime>0 AND endtime < ?", (now_ms(),))
        overdue = cur.fetchone()[0]
        # 惩罚任务
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND tasktype=30")
        punishment = cur.fetchone()[0]
        # 按频率分布
        cur.execute("SELECT taskfrequency, COUNT(*) as cnt FROM taskmodel WHERE isdeleterecord=0 GROUP BY taskfrequency")
        freq_dist = {r[0]: r[1] for r in cur.fetchall()}
        # 按分类分布
        cur.execute("""
            SELECT c.categoryname, COUNT(t.id) as cnt
            FROM categorymodel c
            LEFT JOIN taskmodel t ON t.categoryid=c.id AND t.isdeleterecord=0
            GROUP BY c.id
            ORDER BY cnt DESC
        """)
        cat_dist = [{'category': r[0], 'count': r[1]} for r in cur.fetchall()]
        # 总金币
        cur.execute("SELECT COALESCE(SUM(rewardcoin),0) FROM taskmodel WHERE isdeleterecord=0")
        total_coin = cur.fetchone()[0]

        return jsonify({
            'total': total, 'active': active, 'done': done,
            'frozen': frozen, 'overdue': overdue, 'punishment': punishment,
            'frequency_distribution': freq_dist,
            'category_distribution': cat_dist,
            'total_coin': total_coin
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/tasks/<int:task_id>/copy', methods=['POST'])
def copy_task(task_id):
    """深拷贝任务 + reward + skill 关联"""
    conn = get_db()
    now = now_ms()
    try:
        cur = conn.cursor()
        # 读取原任务
        cur.execute("SELECT * FROM taskmodel WHERE id=?", (task_id,))
        src = cur.fetchone()
        if not src:
            return jsonify({'error': '任务不存在'}), 404
        cols = [d[0] for d in cur.description]
        src_dict = dict(zip(cols, src))

        # 修改 title + 重置状态
        data = request.get_json() or {}
        new_title = data.get('title', (src_dict.get('content') or '任务') + ' (副本)')

        cur.execute("""
            INSERT INTO taskmodel (
                content, taskfrequency, rewardcoin, expreward, remark,
                taskstatus, currenttimes, categoryid, createdtime, updatedtime, isdeleterecord,
                isfrozen, tagcolor, taskdifficultydegree, priority, tasktargetid,
                userid, isshared, tasktype, isneedtoremake, enableebbinghausmode,
                taskurgencydegree, ishandleoverdue, rewardcoinvariable,
                relatedattribute1, relatedattribute2, relatedattribute3,
                teamrecordid, teamid, taskid, taskcountextraid,
                lasttaskid, nexttaskid, groupid, orderincategory,
                isusespecificexpiretime, isuserinputstarttime, starttime, endtime,
                extrainfo, completereward
            ) VALUES (
                ?, ?, ?, ?, ?,
                0, 0, ?, ?, ?, 0,
                ?, ?, ?, ?, ?,
                0, 0, ?, 0, 0,
                ?, 0, ?,
                ?, ?, ?,
                -1, -1, 0, -1,
                0, 0, 0, 0,
                0, 0, ?, ?,
                ?, ?
            )
        """, (
            new_title,
            src_dict.get('taskfrequency', 1),
            src_dict.get('rewardcoin', 0),
            src_dict.get('expreward', 0),
            src_dict.get('remark', ''),
            src_dict.get('categoryid', 0),
            now, now,
            src_dict.get('isfrozen', 0),
            src_dict.get('tagcolor', 0),
            src_dict.get('taskdifficultydegree', 1),
            src_dict.get('priority', 1),
            src_dict.get('tasktargetid', 0),
            src_dict.get('tasktype', 0),
            src_dict.get('taskurgencydegree', 0),
            src_dict.get('rewardcoinvariable', 0),
            src_dict.get('relatedattribute1', ''),
            src_dict.get('relatedattribute2', ''),
            src_dict.get('relatedattribute3', ''),
            now, now,
            src_dict.get('extrainfo', ''),
            src_dict.get('completereward', '')
        ))
        new_id = cur.lastrowid

        # 复制 item rewards
        cur.execute("SELECT * FROM taskrewardmodel WHERE taskmodelid=?", (task_id,))
        for rw in cur.fetchall():
            rw_cols = [d[0] for d in cur.description]
            rw_dict = dict(zip(rw_cols, rw))
            cur.execute(
                "INSERT INTO taskrewardmodel (taskmodelid, shopitemmodelid, amount, createtime, updatetime) VALUES (?, ?, ?, ?, ?)",
                (new_id, rw_dict['shopitemmodelid'], rw_dict.get('amount', 1), now, now))

        # 复制 skill 关联（表名 taskmodel_skillids）
        cur.execute("SELECT skillids FROM taskmodel_skillids WHERE taskmodel_id=?", (task_id,))
        skill_ids = [r[0] for r in cur.fetchall()]
        for sid in skill_ids:
            cur.execute("INSERT INTO taskmodel_skillids (taskmodel_id, skillids) VALUES (?, ?)", (new_id, sid))

        # 复制子任务
        cur.execute("SELECT * FROM subtaskmodel WHERE taskmodelid=?", (task_id,))
        for sub in cur.fetchall():
            sub_cols = [d[0] for d in cur.description]
            sub_dict = dict(zip(sub_cols, sub))
            cur.execute("""
                INSERT INTO subtaskmodel (
                    taskmodelid, content, taskstatus, rewardcoin, expreward,
                    rewardcoinvariable, shopitemmodelid, shopitemamount,
                    subtaskgroupid, orderincategory,
                    createtime, updatetime, remindtime, endtime
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                new_id,
                sub_dict.get('content', ''),
                sub_dict.get('taskstatus', 0),
                sub_dict.get('rewardcoin', 0),
                sub_dict.get('expreward', 0),
                sub_dict.get('rewardcoinvariable', 0),
                sub_dict.get('shopitemmodelid', 0),
                sub_dict.get('shopitemamount', 0),
                sub_dict.get('subtaskgroupid', 0),
                sub_dict.get('orderincategory', 0),
                now, now,
                sub_dict.get('remindtime', 0),
                sub_dict.get('endtime', 0)
            ))

        conn.commit()
        return jsonify({'ok': True, 'id': new_id, 'title': new_title})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ─── 商店 CRUD ──────────────────────────────────────────

@app.route('/api/items')
def list_items():
    if request.args.get('source') == 'cloud':
        return list_cloud_items_for_dashboard()
    conn = get_db()
    try:
        cur = conn.cursor()
        search = request.args.get('search', '').strip()
        category_id = request.args.get('category_id', '')
        cat_cond = 'AND s.shopcategoryid = ?' if category_id else ''
        search_cond = 'AND (s.itemname LIKE ? OR s.description LIKE ?)' if search else ''
        where_params = []
        if category_id:
            where_params.append(int(category_id))
        if search:
            where_params.extend([f'%{search}%', f'%{search}%'])
        cur.execute(f"""
            SELECT s.id, s.itemname as name, s.price, s.icon, s.description,
                   s.stocknumber as count, s.shopcategoryid, s.createtime, s.isdisablepurchase,
                   s.customusebuttontext, s.purchaselimits, s.extrainfo,
                   i.stocknumber as inventory_count, i.id as inventory_id, i.isstarred
            FROM shopitemmodel s
            LEFT JOIN inventorymodel i ON s.inventorymodel_id = i.id
            WHERE s.isdel = 0 {cat_cond} {search_cond}
            ORDER BY s.shopcategoryid, s.orderincategory, s.id
        """, where_params)
        items = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT id, categoryname FROM shopcategorymodel WHERE isdelete=0")
        cats = {r['id']: r['categoryname'] for r in cur.fetchall()}
        for i in items:
            i['category_name'] = cats.get(i.get('shopcategoryid'), '-')

        return jsonify(items)
    finally:
        conn.close()

@app.route('/api/items/add', methods=['POST'])
def add_item():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        purchase_limits = json_text(data.get('purchaselimits'), '[]')
        extra_info = json_text(data.get('extrainfo'), '{}')
        # 先创建 inventory 记录
        cur.execute("INSERT INTO inventorymodel (createtime, stocknumber, updatetime, isstarred) VALUES (?, ?, ?, 0)",
                    (now, data.get('count', 0), now))
        inv_id = cur.lastrowid

        cur.execute("""
            INSERT INTO shopitemmodel (itemname, price, icon, description, stocknumber,
                shopcategoryid, createtime, isdel, isdisablepurchase, inventorymodel_id, remoteismine,
                customusebuttontext, purchaselimits, extrainfo)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?)
        """, (
            data.get('name', '新商品'),
            data.get('price', 0),
            data.get('icon', ''),
            data.get('description', ''),
            data.get('count', -1),  # -1 = unlimited
            data.get('category_id', 0),
            now,
            1 if data.get('isdisablepurchase') else 0,
            inv_id,
            data.get('customusebuttontext', ''),
            purchase_limits,
            extra_info
        ))
        new_item_id = cur.lastrowid
        for row in build_item_effects(data, new_item_id, now):
            cur.execute("""
                INSERT INTO goodseffectmodel
                (createtime, shopitemid, goodseffecttype, relatedinfos, isdel, updatetime, relatedid, values_lpcolumn)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, row)
        recipe_ids = create_simple_synthesis_recipes(cur, data, new_item_id, now)
        conn.commit()
        return jsonify({'ok': True, 'id': new_item_id, 'synthesis_recipe_ids': recipe_ids})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/items/update', methods=['POST'])
def update_item():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        purchase_limits = json_text(data.get('purchaselimits'), '[]')
        cur.execute("""
            UPDATE shopitemmodel SET itemname=?, price=?, icon=?, description=?,
                stocknumber=?, shopcategoryid=?, isdisablepurchase=?,
                customusebuttontext=?, purchaselimits=?
            WHERE id=?
        """, (
            data.get('name'),
            data.get('price', 0),
            data.get('icon', ''),
            data.get('description', ''),
            data.get('count', -1),
            data.get('category_id', 0),
            data.get('isdisablepurchase', 0),
            data.get('customusebuttontext', ''),
            purchase_limits,
            data['id']
        ))
        conn.commit()
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/items/delete', methods=['POST'])
def delete_item():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        # 先获取 inventorymodel_id
        cur.execute("SELECT inventorymodel_id FROM shopitemmodel WHERE id=?", (data['id'],))
        row = cur.fetchone()
        cur.execute("UPDATE shopitemmodel SET isdel=1 WHERE id=?", (data['id'],))
        if row and row['inventorymodel_id']:
            cur.execute("DELETE FROM inventorymodel WHERE id=?", (row['inventorymodel_id'],))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ─── 成就 CRUD ──────────────────────────────────────────

@app.route('/api/achievements')
def list_achievements():
    if request.args.get('source') == 'cloud':
        return list_cloud_achievements_for_dashboard()
    conn = get_db()
    try:
        cur = conn.cursor()
        search = request.args.get('search', '').strip()
        cat_id = request.args.get('category_id', '')
        search_cond = 'AND (content LIKE ? OR description LIKE ?)' if search else ''
        search_params = [f'%{search}%', f'%{search}%'] if search else []
        cat_cond = f'AND categoryid={int(cat_id)}' if cat_id else ''
        cur.execute(f"""
            SELECT id, content as name, description, type, categoryid, rewardcoin as coin,
                   expreward as exp, icon, achievementstatus, currentvalue, progress,
                   createtime, finishtime, updatetime, isgotreward, targetcompletetime
            FROM userachievementmodel
            WHERE isdelete = 0 {search_cond} {cat_cond}
            ORDER BY categoryid, orderincategory, id
        """, search_params)
        ach_list = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT id, categoryname FROM userachcategorymodel WHERE isdelete=0")
        cats = {r['id']: r['categoryname'] for r in cur.fetchall()}
        for a in ach_list:
            a['category_name'] = cats.get(a.get('categoryid'), '-')

        return jsonify(ach_list)
    finally:
        conn.close()

@app.route('/api/achievements/add', methods=['POST'])
def add_achievement():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        cur.execute("""
            INSERT INTO userachievementmodel (content, description, type, categoryid, rewardcoin,
                icon, achievementstatus, currentvalue, progress, createtime, updatetime,
                isdelete, isgotreward, rewardcoinvariable, orderincategory, expreward)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 0, 0, 0, 0, ?)
        """, (
            data.get('name', '新成就'),
            data.get('description', ''),
            data.get('type', 0),
            data.get('category_id', 0),
            data.get('coin', 0),
            data.get('icon', ''),
            now, now,
            data.get('exp', 0)
        ))
        conn.commit()
        return jsonify({'ok': True, 'id': cur.lastrowid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/achievements/update', methods=['POST'])
def update_achievement():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        cur.execute("""
            UPDATE userachievementmodel SET content=?, description=?, type=?, categoryid=?,
                rewardcoin=?, expreward=?, icon=?, updatetime=?
            WHERE id=?
        """, (
            data.get('name'),
            data.get('description', ''),
            data.get('type', 0),
            data.get('category_id', 0),
            data.get('coin', 0),
            data.get('exp', 0),
            data.get('icon', ''),
            now,
            data['id']
        ))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/achievements/delete', methods=['POST'])
def delete_achievement():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE userachievementmodel SET isdelete=1, updatetime=? WHERE id=?",
                    (now_ms(), data['id']))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/achievements/system')
def system_achievements():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ai.*,
                   CASE ai.eventtype
                       WHEN 0 THEN '手动触发'
                       WHEN 1 THEN '完成指定任务次数'
                       WHEN 2 THEN '连续完成任务'
                       WHEN 3 THEN '完成任务总次数'
                       WHEN 4 THEN '连续使用天数'
                       WHEN 5 THEN '属性达到等级'
                       WHEN 6 THEN '累计番茄数'
                       WHEN 7 THEN '累计金币数'
                       WHEN 8 THEN '当前金币数'
                       WHEN 10 THEN '本源等级'
                       WHEN 11 THEN '购买商品次数'
                       WHEN 12 THEN '使用商品次数'
                       WHEN 13 THEN '合成数量'
                       WHEN 14 THEN '商品拥有数量'
                       WHEN 15 THEN '人生等级'
                       ELSE 'type' || ai.eventtype
                   END as eventtype_name
            FROM achievementinfomodel ai
            ORDER BY ai.achievementtype, ai.levelnumber
        """)
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

# ─── 成就条件管理 ──────────────────────────────────────────

CONDITION_TYPE_MAP = {
    0: '手动触发', 1: '完成指定任务次数', 2: '连续完成任务',
    3: '完成任务总次数', 4: '连续使用天数', 5: '属性达到等级',
    6: '累计番茄数', 7: '累计金币数', 8: '当前金币数',
    9: '使用天数', 10: '本源等级', 11: '购买商品次数',
    12: '使用商品次数', 13: '合成数量', 14: '商品拥有数量',
    15: '人生等级', 16: 'ATM存款', 17: '今日新增金币',
    18: '累计专注时长', 19: '社区赞数'
}

@app.route('/api/achievements/<int:ach_id>/conditions')
def list_achievement_conditions(ach_id):
    """查询某成就的所有解锁条件"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT uc.*, ua.content as achievement_name
            FROM unlockconditionmodel uc
            JOIN userachievementmodel ua ON uc.userachievementid = ua.id
            WHERE uc.userachievementid = ? AND uc.isdel = 0
            ORDER BY uc.id
        """, (ach_id,))
        conds = []
        for r in cur.fetchall():
            d = dict(r)
            d['conditiontype_name'] = CONDITION_TYPE_MAP.get(d.get('conditiontype'), f'未知({d.get("conditiontype")})')
            try:
                ri = json.loads(d.get('relatedinfos', '{}') or '{}')
                d['relatedinfos_parsed'] = ri
                d['unlocked_times'] = ri.get('unlockedTimes', 0)
            except:
                d['relatedinfos_parsed'] = {}
                d['unlocked_times'] = 0
            conds.append(d)
        return jsonify({'achievement_id': ach_id, 'conditions': conds, 'count': len(conds)})
    finally:
        conn.close()

@app.route('/api/achievements/<int:ach_id>/conditions', methods=['POST'])
def add_achievement_condition(ach_id):
    """为成就添加解锁条件"""
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        relatedinfos = json.dumps({
            'ignoreValue': data.get('ignore_value', 0),
            'lastUnlockTaskId': 0,
            'unlockedTimes': 0
        })
        cur.execute("""
            INSERT INTO unlockconditionmodel
            (userachievementid, conditiontype, targetvalues, currentvalue, progress,
             relatedid, relatedids, relatedinfos, createtime, updatetime, isdel)
            VALUES (?, ?, ?, 0, 0, ?, '', ?, ?, ?, 0)
        """, (
            ach_id,
            data.get('conditiontype', 1),
            data.get('targetvalues', 1),
            data.get('relatedid', 0),
            relatedinfos,
            now, now
        ))
        conn.commit()
        return jsonify({'ok': True, 'id': cur.lastrowid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/conditions/<int:cond_id>', methods=['POST'])
def update_condition(cond_id):
    """编辑成就条件"""
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        cur.execute("""
            UPDATE unlockconditionmodel
            SET conditiontype=?, targetvalues=?, currentvalue=?, relatedid=?,
                progress=?, updatetime=?
            WHERE id=?
        """, (
            data.get('conditiontype', 1),
            data.get('targetvalues', 1),
            data.get('currentvalue', 0),
            data.get('relatedid', 0),
            data.get('progress', 0),
            now,
            cond_id
        ))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/conditions/<int:cond_id>/delete', methods=['POST'])
def delete_condition(cond_id):
    """删除成就条件"""
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE unlockconditionmodel SET isdel=1, updatetime=? WHERE id=?",
                    (now_ms(), cond_id))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ─── 技能/属性 ──────────────────────────────────────────

@app.route('/api/skills')
def list_skills():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, content as name, description, experience, type, color, icon, status,
                   groupid, orderincategory
            FROM skillmodel WHERE isdel=0 ORDER BY orderincategory
        """)
        skills = [dict(r) for r in cur.fetchall()]

        # 计算每个技能的经验进度
        cur.execute("SELECT perlevelexp FROM levelmodel WHERE id=1")
        ple = cur.fetchone()
        per_level = ple['perlevelexp'] if ple else 100

        for s in skills:
            exp = s.get('experience', 0) or 0
            s['level'] = max(1, exp // per_level + 1)
            s['current_exp'] = exp
            s['next_exp'] = (s['level']) * per_level
            s['progress'] = round((exp % per_level) / per_level * 100, 1)
            s['icon_url'] = media_url('attr', s.get('icon'))

        return jsonify(skills)
    finally:
        conn.close()

# ─── 分类列表 ───────────────────────────────────────────

@app.route('/api/categories/tasks')
def task_categories():
    if request.args.get('source') == 'cloud':
        rows = [{'id': key, 'name': value} for key, value in cloud_category_map('tasks').items()]
        return jsonify(rows)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, categoryname as name FROM categorymodel WHERE isdelete=0 AND categorytype=0 ORDER BY orderincategory")
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

@app.route('/api/categories/shop')
@app.route('/api/categories/items')
def shop_categories():
    if request.args.get('source') == 'cloud':
        rows = [{'id': key, 'name': value} for key, value in cloud_category_map('items').items()]
        return jsonify(rows)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, categoryname as name FROM shopcategorymodel WHERE isdelete=0 ORDER BY orderincategory")
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

@app.route('/api/categories/achievements')
def ach_categories():
    if request.args.get('source') == 'cloud':
        rows = [{'id': key, 'name': value} for key, value in cloud_category_map('achievements').items()]
        return jsonify(rows)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, categoryname as name FROM userachcategorymodel WHERE isdelete=0 ORDER BY orderincategory")
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

# ─── 背包 ───────────────────────────────────────────────

@app.route('/api/inventory')
def inventory():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT inv.id, inv.stocknumber, inv.isstarred, inv.extrainfo, inv.updatetime,
                   s.itemname as item_name, s.icon as item_icon, s.price, s.id as shop_id
            FROM inventorymodel inv
            JOIN shopitemmodel s ON inv.shopitemmodel_id = s.id
            ORDER BY inv.isstarred DESC, inv.updatetime DESC
        """)
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()

# ─── 合成配方 ───────────────────────────────────────────

@app.route('/api/synthesis')
def list_synthesis():
    conn = get_db()
    try:
        cur = conn.cursor()
        cat_id = request.args.get('category_id', '')
        search = request.args.get('search', '').strip()
        cat_cond = f'AND sm.categoryid={int(cat_id)}' if cat_id else ''
        search_params = []
        if search:
            search_params = [f'%{search}%', f'%{search}%']
            search_cond = 'AND (sm.name LIKE ? OR sm.id IN (SELECT sc2.synthesismodelid FROM synthesisconnmodel sc2 JOIN shopitemmodel sh ON sc2.shopitemmodelid = sh.id WHERE sh.itemname LIKE ? AND sc2.isdel=0))'
        else:
            search_cond = ''

        cur.execute(f"""
            SELECT sm.id, sm.name as title, sm.description as note,
                   sm.categoryid, sm.orderincategory, sm.createtime, sm.updatetime,
                   sc.categoryname
            FROM synthesismodel sm
            LEFT JOIN synthesiscategory sc ON sm.categoryid = sc.id
            WHERE sm.isdel=0 {cat_cond} {search_cond}
            ORDER BY sm.categoryid, sm.orderincategory, sm.id
        """, search_params)
        recipes = [dict(r) for r in cur.fetchall()]

        # 批量查所有配方关联
        if recipes:
            ids = ','.join(str(r['id']) for r in recipes)
            cur.execute(f"""
                SELECT sc.synthesismodelid, sc.isoutput, sc.amount, sc.shopitemmodelid,
                       s.itemname, s.icon
                FROM synthesisconnmodel sc
                JOIN shopitemmodel s ON sc.shopitemmodelid = s.id
                WHERE sc.synthesismodelid IN ({ids}) AND sc.isdel=0
                ORDER BY sc.isoutput, sc.id
            """)
            conns = cur.fetchall()
            for recipe in recipes:
                rid = recipe['id']
                recipe['inputs'] = [{'item_id': c['shopitemmodelid'], 'item_name': c['itemname'],
                                     'icon': c['icon'], 'amount': c['amount']}
                                    for c in conns if c['synthesismodelid'] == rid and c['isoutput'] == 0]
                recipe['outputs'] = [{'item_id': c['shopitemmodelid'], 'item_name': c['itemname'],
                                      'icon': c['icon'], 'amount': c['amount']}
                                     for c in conns if c['synthesismodelid'] == rid and c['isoutput'] == 1]

        return jsonify({'recipes': recipes, 'total': len(recipes)})
    finally:
        conn.close()


@app.route('/api/synthesis/categories')
def synth_categories():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, categoryname as name FROM synthesiscategory WHERE isdelete=0 ORDER BY orderincategory")
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()


@app.route('/api/synthesis/add', methods=['POST'])
def add_synthesis():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        # 创建配方
        cur.execute("""
            INSERT INTO synthesismodel (name, description, categoryid, createtime, updatetime, isdel, orderincategory)
            VALUES (?, ?, ?, ?, ?, 0, 0)
        """, (data.get('title', '新配方'), data.get('note', ''), data.get('category_id', 1), now, now))
        recipe_id = cur.lastrowid

        # 输入材料
        for inp in data.get('inputs', []):
            cur.execute("""
                INSERT INTO synthesisconnmodel (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
                VALUES (?, ?, 0, ?, ?, 0, ?)
            """, (inp.get('amount', 1), now, inp['item_id'], recipe_id, now))

        # 输出产物
        for out in data.get('outputs', []):
            cur.execute("""
                INSERT INTO synthesisconnmodel (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
                VALUES (?, ?, 1, ?, ?, 0, ?)
            """, (out.get('amount', 1), now, out['item_id'], recipe_id, now))

        conn.commit()
        return jsonify({'ok': True, 'id': recipe_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/synthesis/update', methods=['POST'])
def update_synthesis():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        rid = data['id']

        # 更新配方基本信息
        cur.execute("""
            UPDATE synthesismodel SET name=?, description=?, categoryid=?, updatetime=?
            WHERE id=?
        """, (data.get('title'), data.get('note', ''), data.get('category_id', 1), now, rid))

        # 重新构建材料关联：先软删旧的全部
        cur.execute("UPDATE synthesisconnmodel SET isdel=1, updatetime=? WHERE synthesismodelid=? AND isdel=0",
                    (now, rid))

        for inp in data.get('inputs', []):
            cur.execute("""
                INSERT INTO synthesisconnmodel (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
                VALUES (?, ?, 0, ?, ?, 0, ?)
            """, (inp.get('amount', 1), now, inp['item_id'], rid, now))

        for out in data.get('outputs', []):
            cur.execute("""
                INSERT INTO synthesisconnmodel (amount, createtime, isoutput, shopitemmodelid, synthesismodelid, isdel, updatetime)
                VALUES (?, ?, 1, ?, ?, 0, ?)
            """, (out.get('amount', 1), now, out['item_id'], rid, now))

        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/synthesis/delete', methods=['POST'])
def delete_synthesis():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        cur.execute("UPDATE synthesismodel SET isdel=1, updatetime=? WHERE id=?", (now, data['id']))
        cur.execute("UPDATE synthesisconnmodel SET isdel=1, updatetime=? WHERE synthesismodelid=?", (now, data['id']))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ─── 合成计算器 ──────────────────────────────────────

@app.route('/api/synthesis/calculate')
def synthesis_calculate():
    """计算当前库存能合成哪些配方。返回：可合成 / 缺1件 / 缺多件"""
    conn = get_db()
    try:
        cur = conn.cursor()
        # 1. 从 inventoryrecordmodel 汇总实际库存（聚合增减记录）
        cur.execute("""
            SELECT shopitemmodel_id as item_id,
                   SUM(CASE WHEN isdecrease=0 THEN changenumber ELSE -changenumber END) as stock
            FROM inventoryrecordmodel
            WHERE isdel = 0
            GROUP BY shopitemmodel_id
            HAVING stock > 0
        """)
        inventory = {r['item_id']: r['stock'] for r in cur.fetchall()}

        # Fallback: inventorymodel 中有 stocknumber>0 的也合并
        cur.execute("""
            SELECT shopitemmodel_id as item_id, stocknumber
            FROM inventorymodel
            WHERE stocknumber > 0
        """)
        for r in cur.fetchall():
            if r['item_id'] not in inventory:
                inventory[r['item_id']] = r['stocknumber']
            else:
                inventory[r['item_id']] += r['stocknumber']

        # 2. 获取所有配方输入 (isoutput=0)
        cur.execute("""
            SELECT sc.synthesismodelid as recipe_id, sc.shopitemmodelid as item_id,
                   sc.amount as count
            FROM synthesisconnmodel sc
            JOIN synthesismodel sm ON sc.synthesismodelid = sm.id
            WHERE sc.isoutput = 0 AND sc.isdel = 0 AND sm.isdel = 0
        """)
        recipe_inputs = {}
        for r in cur.fetchall():
            recipe_inputs.setdefault(r['recipe_id'], []).append({
                'item_id': r['item_id'], 'count': r['count']
            })

        # 3. 计算每个配方的完成度
        can_synth = []  # 可合成
        miss_one = []   # 缺1种
        miss_more = []  # 缺多种
        no_match = []   # 无库存

        for rid, inputs in recipe_inputs.items():
            owned_count = 0
            total_needed = len(inputs)
            for inp in inputs:
                stock = inventory.get(inp['item_id'], 0)
                if stock >= inp['count']:
                    owned_count += 1
            if total_needed == 0:
                continue
            ratio = owned_count / total_needed
            entry = {'recipe_id': rid, 'owned': owned_count, 'needed': total_needed, 'ratio': round(ratio, 2)}
            if ratio >= 1.0:
                can_synth.append(entry)
            elif owned_count == total_needed - 1 and total_needed > 1:
                miss_one.append(entry)
            elif owned_count > 0:
                miss_more.append(entry)
            else:
                no_match.append(entry)

        return jsonify({
            'inventory_count': len(inventory),
            'total_recipes': len(recipe_inputs),
            'can_synth': sorted(can_synth, key=lambda x: -x['ratio']),
            'miss_one': sorted(miss_one, key=lambda x: -x['ratio']),
            'miss_more': sorted(miss_more, key=lambda x: -x['ratio']),
            'no_match': sorted(no_match, key=lambda x: -x['ratio'])
        })
    finally:
        conn.close()


# ─── 活动时间线 ────────────────────────────────────────

@app.route('/api/history')
def activity_history():
    """合并任务完成历史 + 物品变动记录，按时间倒序"""
    conn = get_db()
    try:
        cur = conn.cursor()
        events = []
        from datetime import datetime

        # 1. 任务完成记录
        cur.execute("""
            SELECT id, content, taskstatus, updatedtime, currenttimes, categoryid,
                   rewardcoin, expreward, endtime, createdtime
            FROM taskmodel
            WHERE taskstatus >= 1 AND isdeleterecord = 0
            ORDER BY updatedtime DESC
            LIMIT 200
        """)
        for r in cur.fetchall():
            ts = r['updatedtime'] or r['endtime'] or r['createdtime']
            if ts and ts > 0:
                dt = datetime.fromtimestamp(ts / 1000)
                events.append({
                    'type': 'task',
                    'id': r['id'],
                    'name': r['content'],
                    'status': r['taskstatus'],
                    'count': r['currenttimes'],
                    'coin': r['rewardcoin'] or 0,
                    'exp': r['expreward'] or 0,
                    'time': dt.strftime('%Y-%m-%d %H:%M:%S'),
                    'ts': ts
                })

        # 2. 物品变动记录
        cur.execute("""
            SELECT ir.id, ir.createtime, ir.isdecrease, ir.changenumber, ir.desc_lpcolumn,
                   s.itemname, s.id as shop_id
            FROM inventoryrecordmodel ir
            JOIN shopitemmodel s ON ir.shopitemmodel_id = s.id
            WHERE 1=1
            ORDER BY ir.createtime DESC
            LIMIT 200
        """)
        for r in cur.fetchall():
            ts = r['createtime']
            if ts and ts > 0:
                dt = datetime.fromtimestamp(ts / 1000)
                events.append({
                    'type': 'item',
                    'id': r['id'],
                    'name': r['itemname'],
                    'shop_id': r['shop_id'],
                    'change': r['changenumber'],
                    'is_decrease': bool(r['isdecrease']),
                    'desc': r['desc_lpcolumn'] or '',
                    'time': dt.strftime('%Y-%m-%d %H:%M:%S'),
                    'ts': ts
                })

        events.sort(key=lambda e: e['ts'], reverse=True)

        return jsonify({
            'events': events,
            'task_count': sum(1 for e in events if e['type'] == 'task'),
            'item_count': sum(1 for e in events if e['type'] == 'item'),
            'total': len(events)
        })
    finally:
        conn.close()


# ─── 卡池管理 ──────────────────────────────────────────

@app.route('/api/pools')
def list_pools():
    """列出所有随机奖励型卡池（goodseffectmodel type=7）"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ge.id, ge.shopitemid, s.itemname, s.icon, ge.relatedinfos,
                   ge.goodseffecttype, ge.values_lpcolumn
            FROM goodseffectmodel ge
            JOIN shopitemmodel s ON ge.shopitemid = s.id
            WHERE ge.goodseffecttype = 7 AND ge.isdel = 0
            ORDER BY ge.id
        """)
        pools = []
        for r in cur.fetchall():
            pool = dict(r)
            pool['entries'] = []
            try:
                info = json.loads(r['relatedinfos'] or '{}')
                items = info.get('itemsInfos', [])
                for item in items:
                    sid = item.get('shopItemModelId', item.get('shopItemModelID', 0))
                    cur2 = conn.cursor()
                    cur2.execute("SELECT itemname, icon FROM shopitemmodel WHERE id=? AND isdel=0", (sid,))
                    srow = cur2.fetchone()
                    pool['entries'].append({
                        'item_id': sid,
                        'item_name': srow['itemname'] if srow else f'(已删除#{sid})',
                        'icon': srow['icon'] if srow else '',
                        'probability': item.get('probability', 0),
                        'is_fixed': item.get('isFixedReward', False),
                        'amount': item.get('amount', 1)
                    })
            except Exception as e:
                pool['parse_error'] = str(e)
            pools.append(pool)

        cur.execute("""
            SELECT ge.id, ge.shopitemid, s.itemname, s.icon, ge.relatedinfos,
                   ge.goodseffecttype, ge.values_lpcolumn
            FROM goodseffectmodel ge
            JOIN shopitemmodel s ON ge.shopitemid = s.id
            WHERE ge.goodseffecttype IN (2, 4, 5, 6, 9) AND ge.isdel = 0
            ORDER BY ge.goodseffecttype, ge.id
        """)
        simple_effects = []
        for r in cur.fetchall():
            ef = dict(r)
            ef['entries'] = []
            ef['effect_label'] = {1: '属性', 2: '资源', 4: '经验', 5: '解锁', 6: '成就', 9: '其他'}.get(r['goodseffecttype'], f'type{r["goodseffecttype"]}')
            simple_effects.append(ef)

        return jsonify({
            'pools': pools,
            'simple_effects': simple_effects,
            'pool_count': len(pools),
            'effect_count': len(simple_effects)
        })
    finally:
        conn.close()


@app.route('/api/pools/update', methods=['POST'])
def update_pool():
    """更新卡池内容（概率/物品/固定奖励等）"""
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()
        effect_id = data['id']

        entries = data.get('entries', [])
        items_infos = []
        for e in entries:
            items_infos.append({
                'amount': e.get('amount', 1),
                'isFixedReward': e.get('is_fixed', False),
                'probability': e.get('probability', 100),
                'shopItemModelId': e.get('item_id', 0)
            })
        new_json = json.dumps({'itemsInfos': items_infos})

        cur.execute("""
            UPDATE goodseffectmodel
            SET relatedinfos = ?, updatetime = ?
            WHERE id = ?
        """, (new_json, now, effect_id))

        conn.commit()
        return jsonify({'ok': True, 'entries': len(items_infos)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/pools/add', methods=['POST'])
def add_pool():
    """创建新的卡池效果"""
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        now = now_ms()

        entries = data.get('entries', [])
        items_infos = []
        for e in entries:
            items_infos.append({
                'amount': e.get('amount', 1),
                'isFixedReward': e.get('is_fixed', False),
                'probability': e.get('probability', 100),
                'shopItemModelId': e.get('item_id', 0)
            })
        relatedinfos = json.dumps({'itemsInfos': items_infos})

        cur.execute("""
            INSERT INTO goodseffectmodel (createtime, shopitemid, goodseffecttype, relatedinfos, isdel, updatetime, relatedid, values_lpcolumn)
            VALUES (?, ?, 7, ?, 0, ?, 0, 0)
        """, (now, data['shopitemid'], relatedinfos, now))

        conn.commit()
        return jsonify({'ok': True, 'id': cur.lastrowid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/pools/search_cards')
def search_cards_for_pool():
    """搜索可加入卡池的物品"""
    q = request.args.get('q', '')
    conn = get_db()
    try:
        cur = conn.cursor()
        if q:
            cur.execute("""
                SELECT id, itemname, icon FROM shopitemmodel
                WHERE isdel = 0 AND itemname LIKE ?
                ORDER BY itemname
                LIMIT 50
            """, (f'%{q}%',))
        else:
            cur.execute("""
                SELECT id, itemname, icon FROM shopitemmodel
                WHERE isdel = 0 AND (itemname LIKE 'N-%' OR itemname LIKE 'R-%'
                       OR itemname LIKE 'SR-%' OR itemname LIKE 'SSR-%')
                ORDER BY
                    CASE
                        WHEN itemname LIKE 'N-%' THEN 1
                        WHEN itemname LIKE 'R-%' THEN 2
                        WHEN itemname LIKE 'SR-%' THEN 3
                        WHEN itemname LIKE 'SSR-%' THEN 4
                        ELSE 5
                    END, itemname
                LIMIT 200
            """)
        return jsonify([dict(r) for r in cur.fetchall()])
    finally:
        conn.close()


# ─── 成就进度一览 ──────────────────────────────────────

@app.route('/api/achievements/progress')
def achievements_progress():
    """用户成就进度一览（含系统成就条件对照）"""
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT ua.id, ua.content, ua.description, ua.type, ua.categoryid,
                   ua.rewardcoin, ua.expreward, ua.icon, ua.achievementstatus,
                   ua.currentvalue, ua.progress, ua.createtime, ua.finishtime,
                   uac.categoryname
            FROM userachievementmodel ua
            LEFT JOIN userachcategorymodel uac ON ua.categoryid = uac.id
            WHERE ua.isdelete = 0
            ORDER BY uac.orderincategory, ua.orderincategory, ua.id
        """)
        user_achs = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT ai.*,
                   CASE ai.eventtype
                       WHEN 0 THEN '手动触发' WHEN 1 THEN '完成指定任务次数'
                       WHEN 2 THEN '连续完成任务' WHEN 3 THEN '完成任务总次数'
                       WHEN 4 THEN '连续使用天数' WHEN 5 THEN '属性达到等级'
                       WHEN 6 THEN '累计番茄数' WHEN 7 THEN '累计金币数'
                       WHEN 8 THEN '当前金币数' WHEN 10 THEN '本源等级'
                       WHEN 11 THEN '购买商品次数' WHEN 12 THEN '使用商品次数'
                       WHEN 13 THEN '合成数量' WHEN 14 THEN '商品拥有数量'
                       WHEN 15 THEN '人生等级' ELSE 'type' || ai.eventtype
                   END as eventtype_name
            FROM achievementinfomodel ai
            ORDER BY ai.achievementtype, ai.levelnumber
        """)
        sys_achs = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT id, userachievementid, currentvalue, targetvalues, conditiontype,
                   progress, relatedinfos, relatedids, relatedid
            FROM unlockconditionmodel
            WHERE isdel = 0
        """)
        conditions = {}
        for r in cur.fetchall():
            cond = dict(r)
            cond['conditiontype_name'] = CONDITION_TYPE_MAP.get(cond.get('conditiontype'), f'未知({cond.get("conditiontype")})')
            ua_id = cond['userachievementid']
            if ua_id not in conditions:
                conditions[ua_id] = []
            conditions[ua_id].append(cond)

        for a in user_achs:
            a['conditions'] = conditions.get(a['id'], [])
            a['status_label'] = {0: '未完成', 1: '已完成', 2: '已领取奖励'}.get(a['achievementstatus'], '未知')
            a['total_progress'] = 0
            if a['conditions']:
                total_target = sum(c.get('targetvalues', 0) or 0 for c in a['conditions'])
                total_current = sum(c.get('currentvalue', 0) or 0 for c in a['conditions'])
                if total_target > 0:
                    a['total_progress'] = round(total_current / total_target * 100, 1)
                    a['current_total'] = total_current
                    a['target_total'] = total_target

        return jsonify({
            'achievements': user_achs,
            'system_achievements': sys_achs,
            'total': len(user_achs),
            'completed': sum(1 for a in user_achs if a['achievementstatus'] >= 1)
        })
    finally:
        conn.close()


# ─── 卡牌图鉴 ───────────────────────────────────────────

@app.route('/api/collection')
def card_collection():
    """卡牌收集进度（按稀有度分组）"""
    conn = get_db()
    try:
        cur = conn.cursor()
        search = request.args.get('search', '').strip()
        rarity = request.args.get('rarity', '').strip().upper()
        search_cond = 'AND s.itemname LIKE ?' if search else ''
        rarity_cond = ''
        where_params = []
        if search:
            where_params.append(f'%{search}%')
        if rarity in ('N', 'R', 'SR', 'SSR'):
            rarity_cond = f'AND s.itemname LIKE ?'
            where_params.append(f'{rarity}-%')
        cur.execute(f"""
            SELECT s.id, s.itemname, s.icon, s.price, s.description,
                   COALESCE(inv.stocknumber, 0) as owned
            FROM shopitemmodel s
            LEFT JOIN inventorymodel inv ON s.id = inv.shopitemmodel_id
            WHERE s.isdel = 0 AND (
                s.itemname LIKE 'N-%' OR s.itemname LIKE 'R-%' OR
                s.itemname LIKE 'SR-%' OR s.itemname LIKE 'SSR-%'
            ) {search_cond} {rarity_cond}
            ORDER BY
                CASE
                    WHEN s.itemname LIKE 'SSR-%' THEN 1
                    WHEN s.itemname LIKE 'SR-%' THEN 2
                    WHEN s.itemname LIKE 'R-%' THEN 3
                    WHEN s.itemname LIKE 'N-%' THEN 4
                    ELSE 5
                END, s.itemname
        """, where_params)
        cards = [dict(r) for r in cur.fetchall()]

        groups = {'SSR': [], 'SR': [], 'R': [], 'N': []}
        for c in cards:
            name = c['itemname']
            for r in ['SSR', 'SR', 'R', 'N']:
                if name.startswith(r + '-'):
                    groups[r].append(c)
                    break

        stats = {r: {'total': len(groups[r]), 'owned': sum(1 for c in groups[r] if c['owned'] > 0)} for r in groups}
        total = sum(s['total'] for s in stats.values())
        total_owned = sum(s['owned'] for s in stats.values())

        return jsonify({
            'groups': groups,
            'stats': stats,
            'total': total,
            'total_owned': total_owned,
            'completion_rate': round(total_owned / total * 100, 1) if total > 0 else 0
        })
    finally:
        conn.close()


# ─── 批量操作 ──────────────────────────────────────────

@app.route('/api/tasks/batch', methods=['POST'])
def batch_tasks():
    data = request.get_json(silent=True)
    try:
        ids, action = validate_batch_request(
            data,
            {'disable', 'enable', 'delete', 'freeze', 'unfreeze'},
            '任务',
        )
    except BatchValidationError as exc:
        return jsonify({'error': str(exc)}), 400

    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.cursor()
        ensure_batch_targets_exist(cur, 'taskmodel', ids, '任务')
        now = now_ms()
        ph = ','.join('?' * len(ids))
        if action == 'disable':
            cur.execute(f"UPDATE taskmodel SET taskstatus=0, updatedtime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'enable':
            cur.execute(f"UPDATE taskmodel SET taskstatus=1, updatedtime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'delete':
            cur.execute(f"UPDATE taskmodel SET isdeleterecord=1, updatedtime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'freeze':
            cur.execute(f"UPDATE taskmodel SET isfrozen=1, updatedtime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'unfreeze':
            cur.execute(f"UPDATE taskmodel SET isfrozen=0, updatedtime=? WHERE id IN ({ph})", [now] + ids)
        conn.commit()
        return jsonify({'ok': True, 'affected': cur.rowcount})
    except BatchValidationError as exc:
        conn.rollback()
        return jsonify({'error': str(exc)}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/items/batch', methods=['POST'])
def batch_items():
    data = request.get_json(silent=True)
    try:
        ids, action = validate_batch_request(
            data,
            {'disable', 'enable', 'delete', 'price'},
            '商品',
        )
        price = validate_batch_price(data.get('price')) if action == 'price' else None
    except BatchValidationError as exc:
        return jsonify({'error': str(exc)}), 400

    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.cursor()
        ensure_batch_targets_exist(cur, 'shopitemmodel', ids, '商品')
        now = now_ms()
        ph = ','.join('?' * len(ids))
        if action == 'disable':
            cur.execute(f"UPDATE shopitemmodel SET purchasable=0, updatetime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'enable':
            cur.execute(f"UPDATE shopitemmodel SET purchasable=1, updatetime=? WHERE id IN ({ph})", [now] + ids)
        elif action == 'price':
            cur.execute(f"UPDATE shopitemmodel SET price=?, updatetime=? WHERE id IN ({ph})", [price, now] + ids)
        elif action == 'delete':
            cur.execute(f"UPDATE shopitemmodel SET isdel=1, updatetime=? WHERE id IN ({ph})", [now] + ids)
        conn.commit()
        return jsonify({'ok': True, 'affected': cur.rowcount})
    except BatchValidationError as exc:
        conn.rollback()
        return jsonify({'error': str(exc)}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ─── 经济可视化 ────────────────────────────────────────

@app.route('/api/economy')
def economy_stats():
    """经济数据聚合（按日汇总 coin/exp）"""
    conn = get_db()
    try:
        cur = conn.cursor()
        from datetime import datetime, timedelta

        cur.execute("""
            SELECT updatedtime, rewardcoin, expreward, content
            FROM taskmodel
            WHERE taskstatus >= 1 AND isdeleterecord = 0 AND (rewardcoin > 0 OR expreward > 0)
            ORDER BY updatedtime
        """)
        daily = {}
        for r in cur.fetchall():
            ts = r['updatedtime']
            if not ts or ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts / 1000)
            day = dt.strftime('%Y-%m-%d')
            if day not in daily:
                daily[day] = {'coin': 0, 'exp': 0, 'count': 0}
            daily[day]['coin'] += (r['rewardcoin'] or 0)
            daily[day]['exp'] += (r['expreward'] or 0)
            daily[day]['count'] += 1

        cur.execute("""
            SELECT ir.createtime, ir.isdecrease, ir.changenumber, ir.desc_lpcolumn,
                   s.itemname, s.price
            FROM inventoryrecordmodel ir
            JOIN shopitemmodel s ON ir.shopitemmodel_id = s.id
            ORDER BY ir.createtime
        """)
        for r in cur.fetchall():
            ts = r['createtime']
            if not ts or ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts / 1000)
            day = dt.strftime('%Y-%m-%d')
            if day not in daily:
                daily[day] = {'coin': 0, 'exp': 0, 'count': 0}
            if r['isdecrease'] == 0:
                item_value = (r['price'] or 0) * (r['changenumber'] or 1)
                daily[day]['coin'] += item_value

        days = [{'date': d, **v} for d, v in sorted(daily.items())]
        total_coin = sum(d['coin'] for d in days)
        total_exp = sum(d['exp'] for d in days)
        avg_daily_coin = round(total_coin / max(len(days), 1), 1)

        today = datetime.now()
        last_14 = []
        for i in range(13, -1, -1):
            d = (today - timedelta(days=i)).strftime('%Y-%m-%d')
            if d in daily:
                last_14.append({'date': d, **daily[d]})
            else:
                last_14.append({'date': d, 'coin': 0, 'exp': 0, 'count': 0})

        return jsonify({
            'daily': days,
            'last_14': last_14,
            'total_coin': total_coin,
            'total_exp': total_exp,
            'avg_daily_coin': avg_daily_coin,
            'days': len(days)
        })
    finally:
        conn.close()


# ─── 数据导出 ─────────────────────────────────────────

# Cloud LifeUp API: read-only data plus safe add-task execution.

@app.route('/api/cloud/config', methods=['GET', 'POST'])
def api_cloud_config():
    try:
        if request.method == 'GET':
            cfg = normalize_cloud_config({}, allow_empty=True)
            return jsonify({
                'host': cfg['host'],
                'port': cfg['port'],
                'base_url': cfg['base_url'],
                'api_token_saved': False,
                'api_token_in_memory': cloud_token_in_memory(),
                'default_port': 13276
            })
        data = request.get_json() or {}
        update_runtime_cloud_token(data)
        cfg = normalize_cloud_config(data)
        save_cloud_config(cfg)
        return jsonify({
            'ok': True,
            'host': cfg['host'],
            'port': cfg['port'],
            'base_url': cfg['base_url'],
            'api_token_saved': False,
            'api_token_in_memory': cloud_token_in_memory()
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/cloud/test', methods=['POST'])
def api_cloud_test():
    data = request.get_json() or {}
    try:
        result = cloud_request(data, '/info')
        update_runtime_cloud_token(data)
        if data.get('save', True):
            save_cloud_config(normalize_cloud_config(data))
        return jsonify({
            'ok': True,
            'base_url': result['base_url'],
            'info': result['data'],
            'saved': bool(data.get('save', True)),
            'api_token_in_memory': cloud_token_in_memory()
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cloud/data', methods=['POST'])
def api_cloud_data():
    data = request.get_json() or {}
    dataset = data.get('dataset', 'tasks')
    routes = {
        'tasks': '/tasks',
        'task_categories': '/tasks_categories',
        'items': '/items',
        'item_categories': '/items_categories',
        'skills': '/skills',
        'coin': '/coin',
        'pomodoro_records': '/pomodoro_records',
        'achievement_categories': '/achievement_categories'
    }
    try:
        if dataset == 'achievements':
            categories = cloud_request(data, '/achievement_categories', timeout=15)
            rows = []
            errors = []
            for cat in as_cloud_rows(categories['data']):
                cat_id = cat.get('id') if isinstance(cat, dict) else None
                if cat_id is None:
                    continue
                try:
                    result = cloud_request(data, f'/achievements/{cat_id}', timeout=15)
                    for row in as_cloud_rows(result['data']):
                        if isinstance(row, dict):
                            row = dict(row)
                            row.setdefault('category_id', cat_id)
                            row.setdefault('category_name', cat.get('name', ''))
                        rows.append(row)
                except Exception as e:
                    errors.append({'category_id': cat_id, 'error': str(e)})
            return jsonify({
                'ok': True,
                'dataset': dataset,
                'route': '/achievement_categories + /achievements/{id}',
                'rows': rows,
                'count': len(rows),
                'errors': errors
            })
        if dataset not in routes:
            return jsonify({'error': '未知云人升数据集'}), 400
        result = cloud_request(data, routes[dataset], timeout=20)
        rows = as_cloud_rows(result['data'])
        return jsonify({
            'ok': True,
            'dataset': dataset,
            'route': result['route'],
            'rows': rows,
            'count': len(rows)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cloud/preview', methods=['POST'])
def api_cloud_preview():
    data = request.get_json() or {}
    try:
        urls = normalize_lifeup_urls(data)
        config = normalize_cloud_config(data)
        token = secrets.token_urlsafe(32)
        now = time.time()
        digest = hashlib.sha256('\n'.join(urls).encode('utf-8')).hexdigest()
        with CLOUD_EXECUTION_LOCK:
            cleanup_cloud_execution_state(now)
            CLOUD_PREVIEWS[token] = {
                'config': config,
                'urls': tuple(urls),
                'digest': digest,
                'created_at': now,
                'expires_at': now + CLOUD_PREVIEW_TTL_SECONDS,
                'status': 'ready',
                'idempotency_key': None
            }
        return jsonify({
            'ok': True,
            'count': len(urls),
            'preview_token': token,
            'digest': digest,
            'expires_in': CLOUD_PREVIEW_TTL_SECONDS
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cloud/execute', methods=['POST'])
def api_cloud_execute():
    data = request.get_json() or {}
    preview_token = str(data.get('preview_token') or '').strip()
    idempotency_key = str(data.get('idempotency_key') or '').strip()
    if not preview_token:
        return jsonify({'error': '请先生成预览，再使用预览令牌执行'}), 400
    if not idempotency_key:
        return jsonify({'error': '缺少幂等键，请重新生成预览'}), 400
    if len(idempotency_key) > 128:
        return jsonify({'error': '幂等键过长'}), 400

    now = time.time()
    with CLOUD_EXECUTION_LOCK:
        cleanup_cloud_execution_state(now)
        cached = CLOUD_EXECUTIONS.get(idempotency_key)
        if cached:
            if cached['preview_token'] != preview_token:
                return jsonify({'error': '幂等键已用于其他预览'}), 409
            payload = dict(cached['response'])
            payload['idempotent_replay'] = True
            return jsonify(payload)

        preview = CLOUD_PREVIEWS.get(preview_token)
        if not preview:
            return jsonify({'error': '预览令牌不存在或已过期，请重新预览'}), 409
        if preview['status'] != 'ready':
            message = '该预览正在执行，请勿重复点击' if preview['status'] == 'executing' else '该预览已使用或执行结果不确定，请刷新任务后重新预览'
            return jsonify({'error': message}), 409
        preview['status'] = 'executing'
        preview['idempotency_key'] = idempotency_key
        config = dict(preview['config'])
        urls = list(preview['urls'])

    try:
        result = cloud_post_json(config, '/api/contentprovider', {'urls': urls}, timeout=45)
        payload = {
            'ok': True,
            'count': len(urls),
            'route': result['route'],
            'base_url': result['base_url'],
            'results': result['data'],
            'raw': result['response'],
            'idempotent_replay': False
        }
    except Exception as e:
        with CLOUD_EXECUTION_LOCK:
            preview = CLOUD_PREVIEWS.get(preview_token)
            if preview and preview.get('idempotency_key') == idempotency_key:
                preview['status'] = 'uncertain'
        if isinstance(e, ValueError):
            return jsonify({'error': str(e)}), 400
        return jsonify({'error': str(e)}), 500

    with CLOUD_EXECUTION_LOCK:
        preview = CLOUD_PREVIEWS.get(preview_token)
        if preview:
            preview['status'] = 'consumed'
        CLOUD_EXECUTIONS[idempotency_key] = {
            'preview_token': preview_token,
            'response': payload,
            'expires_at': time.time() + CLOUD_EXECUTION_TTL_SECONDS
        }
    return jsonify(payload)


@app.route('/api/load', methods=['POST'])
def api_load():
    """通过路径重新加载备份"""
    data = request.get_json()
    path = data.get('path', '')
    if not path or not os.path.exists(path):
        return jsonify({'ok': False, 'error': '文件不存在'}), 404
    try:
        load_backup(path)
        STATE['backup_path'] = path
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT nickname, userid FROM usermodel LIMIT 1")
            userinfo = cur.fetchone()
            return jsonify({'ok': True, 'tables': len(tables), 'user': userinfo['nickname'] if userinfo else '未命名'})
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/export/<table>')
def export_table(table):
    """导出表格数据为 JSON/CSV"""
    allowed = {
        'tasks': 'SELECT id, content as title, taskfrequency as frequency, rewardcoin as coin, expreward as exp, taskstatus as done, currenttimes as done_count, categoryid, remark as note, createdtime, updatedtime FROM taskmodel WHERE isdeleterecord=0',
        'items': 'SELECT id, itemname as name, price, description, isdisablepurchase, icon, shopcategoryid, stocknumber as count, createtime FROM shopitemmodel WHERE isdel=0',
        'inventory': 'SELECT inv.id, inv.shopitemmodel_id, s.itemname, inv.stocknumber as count FROM inventorymodel inv JOIN shopitemmodel s ON inv.shopitemmodel_id=s.id',
        'achievements': 'SELECT id, content as name, description, type, categoryid, rewardcoin as coin, icon, achievementstatus, currentvalue, progress, createtime, finishtime, updatetime FROM userachievementmodel WHERE isdelete=0',
        'skills': 'SELECT id, content as name, description, experience as exp, color, icon, groupid FROM skillmodel WHERE isdel=0',
        'history': 'SELECT updatedtime as time, content as title, rewardcoin as coin, expreward as exp FROM taskmodel WHERE taskstatus>=1 AND isdeleterecord=0 ORDER BY updatedtime DESC',
    }
    if table not in allowed:
        return jsonify({'error': 'Unknown table'}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(allowed[table])
        rows = [dict(r) for r in cur.fetchall()]
        fmt = request.args.get('format', 'json')
        if fmt == 'csv':
            if not rows:
                return '', 200, {'Content-Type': 'text/csv'}
            import io, csv
            out = io.StringIO()
            keys = list(rows[0].keys())
            w = csv.DictWriter(out, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
            resp = out.getvalue()
            return resp, 200, {'Content-Type': 'text/csv; charset=utf-8'}
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ─── 启动 ───────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    # 可选：启动时自动加载指定备份
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if os.path.exists(path):
            print(f'加载备份: {path}')
            load_backup(path)
        else:
            print(f'备份文件不存在: {path}')

    print('LifeUp 管理面板后端已启动 → http://localhost:5000')
    app.run(host='127.0.0.1', port=5000, debug=False)
