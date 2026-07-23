"""
LifeUp 管理面板 - 后端服务
直接读写 LifeUp 备份存档(.zip)中的 SQLite 数据库
"""
import copy, csv, io, zipfile, sqlite3, os, tempfile, shutil, json, time, hashlib, secrets, threading, stat, struct, re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
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
ANDROID_ZIP_FLAGS = 0x808
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(
    os.environ.get('LIFEUP_DASHBOARD_DATA_DIR') or PROJECT_DIR
)
BROWSER_IMPORT_DIR = os.path.join(DATA_DIR, 'workspaces', 'browser-imports')
EXPORT_DIR = os.path.join(DATA_DIR, 'exports')
SNAPSHOT_DIR = os.path.join(DATA_DIR, 'workspaces', 'snapshots')
RESTORE_DIR = os.path.join(DATA_DIR, 'workspaces', 'restores')
WORK_DIR = os.path.join(DATA_DIR, 'work')
GOAL_CONFIG_PATH = os.path.join(DATA_DIR, 'lifeup_goal_mappings.json')
ORIGINAL_BACKUP_PATH = os.path.abspath(os.path.join(PROJECT_DIR, '..', 'LifeupBackup.zip'))
PROTECTED_BACKUP_PATHS = {os.path.normcase(os.path.realpath(ORIGINAL_BACKUP_PATH))}
STATE_LOCK = threading.RLock()
KEY_ENTITY_TABLES = ('taskmodel', 'shopitemmodel', 'userachievementmodel')
SNAPSHOT_ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
SNAPSHOT_FILENAME_PATTERN = re.compile(r'^snapshot-([0-9a-f]{32})\.zip$')
WORKSPACE_IMPORT_FILENAME_PATTERN = re.compile(
    r'^\d{8}-\d{6}-[0-9a-f]{8}-.+\.zip$', re.IGNORECASE
)
WORKSPACE_RESTORE_FILENAME_PATTERN = re.compile(
    r'^restore-\d{8}-\d{6}-[0-9a-f]{32}-[0-9a-f]{16}\.zip$', re.IGNORECASE
)
WORKSPACE_EXPORT_FILENAME_PATTERN = re.compile(
    r'^LifeupBackup-export-\d{8}-\d{6}-[0-9a-f]{6}\.zip$', re.IGNORECASE
)
WORKSPACE_CLEANUP_PREVIEW_TOKEN_PATTERN = re.compile(r'^[0-9a-f]{64}$')
WORKSPACE_CLEANUP_PREVIEW_TTL_SECONDS = 600
MAX_WORKSPACE_CLEANUP_ITEMS = 500
WORKSPACE_CLEANUP_PREVIEWS = {}
WORKSPACE_CLEANUP_LOCK = threading.RLock()
SNAPSHOT_COMMENT_PREFIX = b'LIFEUP_DASHBOARD_SNAPSHOT_V1\n'
MAX_SNAPSHOT_NAME_LENGTH = 100
MAX_SNAPSHOT_LIST_LIMIT = 200
MAX_BATCH_SIZE = 200
MAX_TASK_IMPORT_FILE_BYTES = 1024 * 1024
MAX_TASK_IMPORT_NOTE_LENGTH = 2000
MAX_TASK_IMPORT_ITEM_REWARDS = 20
TASK_IMPORT_COLUMNS = (
    'title', 'category', 'frequency', 'target_count', 'priority', 'difficulty',
    'skills', 'coin', 'exp', 'note', 'item_rewards', 'is_frozen',
    'duplicate_policy',
)
TASK_IMPORT_REQUIRED_COLUMNS = {
    'title', 'category', 'frequency', 'target_count', 'coin', 'exp', 'skills',
    'is_frozen',
}
MAX_ITEM_IMPORT_FILE_BYTES = 1024 * 1024
ITEM_IMPORT_COLUMNS = (
    'action', 'item_id', 'name', 'category', 'price', 'stock',
    'is_purchase_enabled', 'effect_type', 'effect_value', 'effect_skill',
    'price_mode', 'price_value', 'duplicate_policy',
)
MAX_ACHIEVEMENT_IMPORT_FILE_BYTES = 1024 * 1024
ACHIEVEMENT_IMPORT_COLUMNS = (
    'action', 'name', 'category', 'description', 'coin', 'exp', 'icon',
    'conditions', 'duplicate_policy',
)
MAX_ICON_FILE_BYTES = 5 * 1024 * 1024
MAX_ICON_LIST_LIMIT = 200
MAX_ICON_SEARCH_LENGTH = 200
MAX_GOAL_COUNT = 50
MAX_GOAL_CATEGORY_COUNT = 100
MAX_GOAL_TARGET_COUNT = 1_000_000
GOAL_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
ICON_UPLOAD_EXTENSIONS = {
    '.png': 'png',
    '.jpg': 'jpeg',
    '.jpeg': 'jpeg',
    '.gif': 'gif',
    '.webp': 'webp',
}
ICON_REFERENCE_SPECS = (
    {
        'entity': 'items', 'table': 'shopitemmodel', 'name_column': 'itemname',
        'icon_column': 'icon', 'deleted_column': 'isdel',
        'folder': 'download', 'editable': True,
    },
    {
        'entity': 'achievements', 'table': 'userachievementmodel',
        'name_column': 'content', 'icon_column': 'icon',
        'deleted_column': 'isdelete', 'folder': 'download', 'editable': True,
    },
    {
        'entity': 'skills', 'table': 'skillmodel', 'name_column': 'content',
        'icon_column': 'icon', 'deleted_column': 'isdel',
        'folder': 'attr', 'editable': False,
    },
    {
        'entity': 'system_achievements', 'table': 'achievementinfomodel',
        'name_column': 'title', 'icon_column': 'icon',
        'deleted_column': None, 'folder': None, 'editable': False,
        'builtin': True,
    },
)
MAX_ITEM_PRICE = 2_147_483_647
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
LOCAL_BATCH_CONTRACT_VERSION = 1
LOCAL_BATCH_PREVIEW_TTL_SECONDS = 600
LOCAL_BATCH_PREVIEW_TOKEN_PATTERN = re.compile(r'^[0-9a-f]{64}$')
LOCAL_BATCH_DIGEST_PATTERN = re.compile(r'^[0-9a-f]{64}$')
LOCAL_BATCH_PREVIEWS = {}
LOCAL_BATCH_PREVIEW_LOCK = threading.RLock()
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


class LocalBatchExecutionChanged(RuntimeError):
    """A preview target no longer matches the executable database state."""


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


def _path_is_reparse_point(path):
    """Reject Windows junctions and other reparse points, not only symlinks."""
    try:
        attributes = getattr(os.lstat(path), 'st_file_attributes', 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _workspace_cleanup_roots():
    """Return only directories whose contents are owned by this project."""
    return (
        ('browser_import', BROWSER_IMPORT_DIR, WORKSPACE_IMPORT_FILENAME_PATTERN,
         '旧的浏览器导入工作副本'),
        ('restore', RESTORE_DIR, WORKSPACE_RESTORE_FILENAME_PATTERN,
         '旧的快照恢复工作副本'),
        ('export', EXPORT_DIR, WORKSPACE_EXPORT_FILENAME_PATTERN,
         '应用自动生成的导出备份'),
        ('work', WORK_DIR, None, '本机日志或临时验证文件'),
    )


def _cleanup_preview_expired_locked(now):
    expired = [
        token for token, preview in WORKSPACE_CLEANUP_PREVIEWS.items()
        if preview.get('expires_at', 0) <= now
    ]
    for token in expired:
        WORKSPACE_CLEANUP_PREVIEWS.pop(token, None)


def _is_workspace_cleanup_candidate(category, path, root, filename_pattern):
    if (
        not _path_is_within(path, root)
        or os.path.islink(path)
        or _path_is_reparse_point(path)
        or not os.path.isfile(path)
    ):
        return False
    if filename_pattern is not None and not filename_pattern.fullmatch(os.path.basename(path)):
        return False
    if _paths_refer_to_same_file(path, STATE.get('backup_path')):
        return False
    if _canonical_path(path) in {_canonical_path(value) for value in PROTECTED_BACKUP_PATHS}:
        return False
    if _paths_refer_to_same_file(path, ORIGINAL_BACKUP_PATH):
        return False
    return category in {'browser_import', 'restore', 'export', 'work'}


def _iter_workspace_cleanup_candidates():
    for category, root, filename_pattern, reason in _workspace_cleanup_roots():
        if not os.path.isdir(root) or os.path.islink(root) or _path_is_reparse_point(root):
            continue
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            path = os.path.abspath(entry.path)
            if not _is_workspace_cleanup_candidate(
                category, path, root, filename_pattern
            ):
                continue
            try:
                stat_result = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            yield {
                'category': category,
                'root': os.path.abspath(root),
                'path': path,
                'name': entry.name,
                'relative_path': entry.name,
                'reason': reason,
                'size': stat_result.st_size,
                'mtime_ns': stat_result.st_mtime_ns,
                'inode': getattr(stat_result, 'st_ino', None),
                'modified_at': datetime.fromtimestamp(
                    stat_result.st_mtime
                ).astimezone().isoformat(timespec='seconds'),
            }


def create_workspace_cleanup_preview(now=None):
    now = time.time() if now is None else float(now)
    candidates = list(_iter_workspace_cleanup_candidates())
    candidates.sort(key=lambda item: (item['category'], item['relative_path'].lower()))
    truncated = len(candidates) > MAX_WORKSPACE_CLEANUP_ITEMS
    candidates = candidates[:MAX_WORKSPACE_CLEANUP_ITEMS]
    token = secrets.token_hex(32)
    stored_items = {}
    public_items = []
    for candidate in candidates:
        item_id = secrets.token_hex(12)
        stored_items[item_id] = candidate
        public_items.append({
            'id': item_id,
            'category': candidate['category'],
            'name': candidate['name'],
            'relative_path': candidate['relative_path'],
            'reason': candidate['reason'],
            'size': candidate['size'],
            'modified_at': candidate['modified_at'],
        })
    with WORKSPACE_CLEANUP_LOCK:
        _cleanup_preview_expired_locked(now)
        WORKSPACE_CLEANUP_PREVIEWS[token] = {
            'expires_at': now + WORKSPACE_CLEANUP_PREVIEW_TTL_SECONDS,
            'items': stored_items,
        }
    return {
        'preview_token': token,
        'expires_in': WORKSPACE_CLEANUP_PREVIEW_TTL_SECONDS,
        'items': public_items,
        'count': len(public_items),
        'total_bytes': sum(item['size'] for item in public_items),
        'truncated': truncated,
    }


def execute_workspace_cleanup(preview_token, item_ids, now=None):
    now = time.time() if now is None else float(now)
    if (
        not isinstance(preview_token, str)
        or not WORKSPACE_CLEANUP_PREVIEW_TOKEN_PATTERN.fullmatch(preview_token)
    ):
        raise ValueError('CLEANUP_PREVIEW_EXPIRED')
    if (
        not isinstance(item_ids, list)
        or not item_ids
        or len(item_ids) > MAX_WORKSPACE_CLEANUP_ITEMS
        or any(not isinstance(item_id, str) or not item_id for item_id in item_ids)
        or len(set(item_ids)) != len(item_ids)
    ):
        raise ValueError('INVALID_CLEANUP_SELECTION')

    with WORKSPACE_CLEANUP_LOCK:
        _cleanup_preview_expired_locked(now)
        preview = WORKSPACE_CLEANUP_PREVIEWS.get(preview_token)
        if preview is None:
            raise ValueError('CLEANUP_PREVIEW_EXPIRED')
        if any(item_id not in preview['items'] for item_id in item_ids):
            raise ValueError('INVALID_CLEANUP_SELECTION')
        selected = [(item_id, preview['items'][item_id]) for item_id in item_ids]
        WORKSPACE_CLEANUP_PREVIEWS.pop(preview_token, None)

    roots = {
        category: (os.path.abspath(root), filename_pattern)
        for category, root, filename_pattern, _reason in _workspace_cleanup_roots()
    }
    results = []
    freed_bytes = 0
    for item_id, item in selected:
        category = item['category']
        root_info = roots.get(category)
        path = item['path']
        public_result = {
            'id': item_id,
            'name': item['name'],
            'category': category,
        }
        if root_info is None or not _is_workspace_cleanup_candidate(
            category, path, root_info[0], root_info[1]
        ):
            results.append({**public_result, 'status': 'changed',
                            'message': '文件已变化或不再属于可清理范围，未删除'})
            continue
        try:
            stat_result = os.stat(path, follow_symlinks=False)
            identity_matches = (
                stat_result.st_size == item['size']
                and stat_result.st_mtime_ns == item['mtime_ns']
                and getattr(stat_result, 'st_ino', None) == item['inode']
            )
            if not identity_matches:
                results.append({**public_result, 'status': 'changed',
                                'message': '文件在预览后发生变化，未删除'})
                continue
            os.remove(path)
            freed_bytes += item['size']
            results.append({**public_result, 'status': 'deleted', 'message': '已删除'})
        except OSError:
            results.append({**public_result, 'status': 'failed',
                            'message': '删除失败，文件可能正在使用或权限不足'})

    deleted = sum(result['status'] == 'deleted' for result in results)
    return {
        'ok': all(result['status'] == 'deleted' for result in results),
        'deleted': deleted,
        'failed': len(results) - deleted,
        'freed_bytes': freed_bytes,
        'results': results,
    }


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


class _NonSeekableZipOutput:
    """Make zipfile emit the data descriptors used by Android/Java backups."""

    def __init__(self, raw_output):
        self.raw_output = raw_output
        self.offset = 0

    def write(self, data):
        written = self.raw_output.write(data)
        self.offset += written
        return written

    def tell(self):
        return self.offset

    def seek(self, *_args, **_kwargs):
        raise OSError('non-seekable output')

    def flush(self):
        self.raw_output.flush()


class _AndroidZipInfo(zipfile.ZipInfo):
    """Always mark filenames as UTF-8, matching LifeUp's Java ZIP writer."""

    def _encodeFilenameFlags(self):
        return self.filename.encode('utf-8'), self.flag_bits | 0x800


def _android_zip_info(file_path, archive_name):
    source_info = zipfile.ZipInfo.from_file(file_path, arcname=archive_name)
    info = _AndroidZipInfo(source_info.filename, date_time=source_info.date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 0
    info.create_version = 20
    info.extract_version = 20
    info.external_attr = 0
    info.internal_attr = 0
    info.extra = b''
    info.comment = b''
    return info


def _write_android_zip_member(archive, file_path, archive_name):
    info = _android_zip_info(file_path, archive_name)
    with open(file_path, 'rb') as source, archive.open(info, 'w') as destination:
        shutil.copyfileobj(source, destination, length=1024 * 1024)


def _clear_zip_central_directory_external_attrs(output_path):
    """Clear desktop permission bits that Python adds to ZIP central entries."""
    with open(output_path, 'r+b') as archive:
        archive.seek(0, os.SEEK_END)
        archive_size = archive.tell()
        tail_size = min(archive_size, 65557)
        archive.seek(archive_size - tail_size)
        tail = archive.read(tail_size)
        eocd_index = tail.rfind(b'PK\x05\x06')
        if eocd_index < 0:
            raise zipfile.BadZipFile('ZIP end-of-central-directory record not found')

        eocd = tail[eocd_index:eocd_index + 22]
        if len(eocd) != 22:
            raise zipfile.BadZipFile('Truncated ZIP end-of-central-directory record')
        entry_count = struct.unpack_from('<H', eocd, 10)[0]
        central_offset = struct.unpack_from('<I', eocd, 16)[0]

        archive.seek(central_offset)
        for _ in range(entry_count):
            header_offset = archive.tell()
            header = archive.read(46)
            if len(header) != 46 or header[:4] != b'PK\x01\x02':
                raise zipfile.BadZipFile('Invalid ZIP central-directory entry')
            filename_length, extra_length, comment_length = struct.unpack_from(
                '<HHH', header, 28
            )
            archive.seek(header_offset + 38)
            archive.write(b'\x00\x00\x00\x00')
            archive.seek(
                header_offset + 46 + filename_length + extra_length + comment_length
            )


def _write_backup_archive(
    output_path, workspace_root, snapshot_database, archive_comment=None
):
    skipped_database_files = {
        DB_INTERNAL.casefold(),
        f'{DB_INTERNAL}-wal'.casefold(),
        f'{DB_INTERNAL}-shm'.casefold(),
        f'{DB_INTERNAL}-journal'.casefold(),
    }
    with open(output_path, 'wb') as raw_output:
        stream = _NonSeekableZipOutput(raw_output)
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
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
                    archive_name = os.path.relpath(
                        file_path, workspace_root
                    ).replace('\\', '/')
                    if archive_name.casefold() in skipped_database_files:
                        continue
                    _write_android_zip_member(archive, file_path, archive_name)
            _write_android_zip_member(archive, snapshot_database, DB_INTERNAL)
            if archive_comment:
                # ZIP comments are bounded metadata stored with the single snapshot file.
                # Source: https://docs.python.org/3.10/library/zipfile.html#zipfile.ZipFile.comment
                archive.comment = archive_comment
    _clear_zip_central_directory_external_attrs(output_path)


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
            infos = archive.infolist()
            metadata_matches = (
                bool(infos)
                and {info.flag_bits for info in infos} == {ANDROID_ZIP_FLAGS}
                and {info.create_system for info in infos} == {0}
                and {info.external_attr for info in infos} == {0}
                and not any(info.extra or info.comment for info in infos)
            )
            if not metadata_matches:
                raise BackupExportError(
                    'EXPORT_VALIDATION_FAILED',
                    '导出 ZIP 未通过 Android 恢复兼容性校验',
                    '导出文件没有发布；请更新 Dashboard 后重新导出。',
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
        icon_integrity = _validate_icon_references_for_export_locked()
        result = _publish_workspace_archive_locked(final_path)
        return {**result, 'icon_integrity': icon_integrity}


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


def _local_batch_error(code, message, status, suggestion=None, details=None):
    payload = {'ok': False, 'code': code, 'error': message}
    if suggestion:
        payload['suggestion'] = suggestion
    if details is not None:
        payload['details'] = details
    return jsonify(payload), status


def _local_batch_row_error(code, message, field=None):
    error = {'code': code, 'message': message}
    if field:
        error['field'] = field
    return error


def _append_local_batch_row_error(row, code, message, field=None):
    if any(error.get('code') == code for error in row['errors']):
        return
    row['errors'].append(_local_batch_row_error(code, message, field))


def _detect_safe_icon_format(content):
    """Identify a small allowlist of raster images from their real bytes."""
    if not isinstance(content, (bytes, bytearray)):
        return None
    size = len(content)
    if (
        size >= 24
        and content[:8] == b'\x89PNG\r\n\x1a\n'
        and content[12:16] == b'IHDR'
        and content[-12:] == b'\x00\x00\x00\x00IEND\xaeB`\x82'
    ):
        width = int.from_bytes(content[16:20], 'big')
        height = int.from_bytes(content[20:24], 'big')
        if 0 < width <= 16384 and 0 < height <= 16384:
            return 'png'
    if size >= 8 and content[:3] == b'\xff\xd8\xff' and content[-2:] == b'\xff\xd9':
        index = 2
        sof_markers = {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        }
        while index + 4 <= size:
            if content[index] != 0xFF:
                index += 1
                continue
            while index < size and content[index] == 0xFF:
                index += 1
            if index >= size:
                break
            marker = content[index]
            index += 1
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > size:
                break
            segment_size = int.from_bytes(content[index:index + 2], 'big')
            if segment_size < 2 or index + segment_size > size:
                break
            if marker in sof_markers and segment_size >= 8:
                height = int.from_bytes(content[index + 3:index + 5], 'big')
                width = int.from_bytes(content[index + 5:index + 7], 'big')
                if 0 < width <= 16384 and 0 < height <= 16384:
                    return 'jpeg'
                break
            if marker == 0xDA:
                break
            index += segment_size
    if (
        size >= 14
        and content[:6] in (b'GIF87a', b'GIF89a')
        and content[-1:] == b';'
    ):
        width = int.from_bytes(content[6:8], 'little')
        height = int.from_bytes(content[8:10], 'little')
        if 0 < width <= 16384 and 0 < height <= 16384:
            return 'gif'
    if (
        size >= 16
        and content[:4] == b'RIFF'
        and content[8:12] == b'WEBP'
        and content[12:16] in (b'VP8 ', b'VP8L', b'VP8X')
    ):
        declared_size = int.from_bytes(content[4:8], 'little') + 8
        if declared_size <= size:
            return 'webp'
    return None


def _icon_file_bytes(path):
    try:
        size = os.path.getsize(path)
        if size <= 0 or size > MAX_ICON_FILE_BYTES:
            return None, size
        with open(path, 'rb') as handle:
            return handle.read(MAX_ICON_FILE_BYTES + 1), size
    except OSError:
        return None, 0


def _safe_icon_reference(value, maximum=500):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        return None
    return text


def _download_icon_target_info(reference):
    """Resolve an editable LifeUp icon filename inside media/download only."""
    text = _safe_icon_reference(reference, 255)
    if (
        text is None
        or text in ('.', '..')
        or '/' in text
        or '\\' in text
        or os.path.basename(text) != text
    ):
        return None
    extension = Path(text).suffix.lower()
    expected_format = ICON_UPLOAD_EXTENSIONS.get(extension)
    if expected_format is None or not STATE.get('loaded'):
        return None
    media_root = os.path.abspath(
        os.path.join(STATE['tmpdir'], 'media', 'download')
    )
    target = os.path.abspath(os.path.join(media_root, text))
    if os.path.dirname(target) != media_root or not os.path.isfile(target):
        return None
    content, size = _icon_file_bytes(target)
    detected_format = _detect_safe_icon_format(content)
    if detected_format != expected_format:
        return None
    return {
        'filename': text,
        'reference': text,
        'folder': 'download',
        'path': f'media/download/{text}',
        'media_url': f'/api/media/download/{text}',
        'size': size,
        'format': detected_format,
        'sha256': hashlib.sha256(content).hexdigest(),
    }


def _normalize_icon_replace_data(row, data, cursor):
    entity_type = data.get('entity_type')
    if entity_type in ('skills', 'system_achievements'):
        _append_local_batch_row_error(
            row, 'ICON_ENTITY_READ_ONLY',
            '技能和系统成就图标保持只读，不能批量替换。',
            'data.entity_type',
        )
        return None
    editable_specs = {
        spec['entity']: spec for spec in ICON_REFERENCE_SPECS if spec['editable']
    }
    spec = editable_specs.get(entity_type)
    if spec is None:
        _append_local_batch_row_error(
            row, 'INVALID_ICON_ENTITY',
            '图标替换只支持商品或自定义成就。', 'data.entity_type'
        )
        return None

    target_id = data.get('id')
    if type(target_id) is not int or not 0 < target_id <= MAX_SQLITE_INTEGER:
        _append_local_batch_row_error(
            row, 'INVALID_ID',
            f'data.id 必须是 1～{MAX_SQLITE_INTEGER} 之间的整数。',
            'data.id',
        )
        return None
    old_icon = _safe_icon_reference(data.get('old_icon'))
    if old_icon is None:
        _append_local_batch_row_error(
            row, 'INVALID_ICON_SOURCE', '原图标引用格式无效。', 'data.old_icon'
        )
    new_icon = _safe_icon_reference(data.get('new_icon'), 255)
    target_info = _download_icon_target_info(new_icon) if new_icon else None
    if target_info is None:
        _append_local_batch_row_error(
            row, 'ICON_TARGET_NOT_FOUND',
            '目标图标不是 media/download 中有效的安全图片。',
            'data.new_icon',
        )

    cursor.execute(
        f'SELECT {spec["name_column"]}, {spec["icon_column"]} '
        f'FROM {spec["table"]} WHERE id=? AND {spec["deleted_column"]}=0',
        (target_id,),
    )
    record = cursor.fetchone()
    if record is None:
        _append_local_batch_row_error(
            row, 'TARGET_NOT_FOUND',
            '目标不存在或已被删除，请刷新图标检查后重试。', 'data.id'
        )
    elif old_icon is not None and record[1] != old_icon:
        _append_local_batch_row_error(
            row, 'ICON_SOURCE_CHANGED',
            '目标当前图标与所选原图标不一致，请刷新后重试。',
            'data.old_icon',
        )
    if old_icon and new_icon and old_icon == new_icon:
        _append_local_batch_row_error(
            row, 'ICON_REPLACEMENT_UNCHANGED',
            '新旧图标相同，不需要执行替换。', 'data.new_icon'
        )

    row['normalized_data'].update({
        'entity_type': entity_type,
        'id': target_id,
        'entity_name': record[0] if record is not None else '',
        'old_icon': old_icon or '',
        'new_icon': new_icon or '',
        'target_path': target_info['path'] if target_info else '',
        'target_media_url': target_info['media_url'] if target_info else '',
        'target_sha256': target_info['sha256'] if target_info else '',
    })
    return (entity_type, target_id)


def _stable_local_batch_digest(normalized_request):
    serialized = json.dumps(
        normalized_request,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return hashlib.sha256(serialized).hexdigest()


def _task_import_integer(value, default, minimum=0, maximum=MAX_ITEM_PRICE):
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r'[+-]?\d+', value.strip()):
        number = int(value.strip())
    else:
        return None
    return number if minimum <= number <= maximum else None


def _task_import_name_map(cursor, table, id_column, name_column, where):
    cursor.execute(
        f'SELECT {id_column}, {name_column} FROM {table} WHERE {where}'
    )
    result = {}
    for record_id, raw_name in cursor.fetchall():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name:
            continue
        result.setdefault(name.casefold(), []).append((record_id, name))
    return result


def _normalize_task_create_data(row, data, category_map, skill_map, item_map):
    normalized = row['normalized_data']

    title = data.get('title')
    if isinstance(title, str):
        title = title.strip()
    if not isinstance(title, str) or not 1 <= len(title) <= 200:
        _append_local_batch_row_error(
            row, 'INVALID_TITLE', '任务名称必须是 1～200 个字符。', 'data.title'
        )
    else:
        normalized['title'] = title

    category = data.get('category')
    if isinstance(category, str):
        category = category.strip()
    matches = category_map.get(category.casefold(), []) if category else []
    if not matches:
        _append_local_batch_row_error(
            row, 'CATEGORY_NOT_FOUND', '找不到这个任务分类。', 'data.category'
        )
    elif len(matches) > 1:
        _append_local_batch_row_error(
            row, 'CATEGORY_AMBIGUOUS', '存在多个同名任务分类。', 'data.category'
        )
    else:
        normalized['category_id'] = matches[0][0]
        normalized['category'] = matches[0][1]

    frequency_aliases = {
        '单次': 0, '每日': 1, '每周': 2, '每月': 3, '每年': 4, '无限': 5,
        'once': 0, 'daily': 1, 'weekly': 2, 'monthly': 3,
        'yearly': 4, 'unlimited': 5,
    }
    raw_frequency = data.get('frequency')
    frequency = None
    if raw_frequency is None or (
        isinstance(raw_frequency, str) and not raw_frequency.strip()
    ):
        frequency = 1
    elif type(raw_frequency) is int and 0 <= raw_frequency <= 5:
        frequency = raw_frequency
    elif isinstance(raw_frequency, str):
        key = raw_frequency.strip()
        if re.fullmatch(r'\d+', key) and 0 <= int(key) <= 5:
            frequency = int(key)
        else:
            frequency = frequency_aliases.get(key.casefold())
    if frequency is None:
        _append_local_batch_row_error(
            row, 'INVALID_FREQUENCY', '频率必须是单次、每日、每周、每月、每年或无限。',
            'data.frequency'
        )
    else:
        normalized['frequency'] = frequency

    integer_fields = (
        ('target_count', 1, 1, 'INVALID_TARGET_COUNT', '目标次数'),
        ('priority', 1, 1, 'INVALID_PRIORITY', '重要程度', 4),
        ('difficulty', 1, 1, 'INVALID_DIFFICULTY', '困难程度', 4),
        ('coin', 0, 0, 'INVALID_COIN', '金币奖励'),
        ('exp', 0, 0, 'INVALID_EXP', '经验奖励'),
    )
    for spec in integer_fields:
        field, default, minimum, code, label = spec[:5]
        maximum = spec[5] if len(spec) > 5 else MAX_ITEM_PRICE
        value = _task_import_integer(data.get(field), default, minimum, maximum)
        if value is None:
            _append_local_batch_row_error(
                row, code, f'{label}必须是 {minimum}～{maximum} 的整数。',
                f'data.{field}'
            )
        else:
            normalized[field] = value

    raw_skills = data.get('skills')
    if raw_skills is None or raw_skills == '':
        skill_names = []
    elif isinstance(raw_skills, str):
        skill_names = [name.strip() for name in raw_skills.split('|') if name.strip()]
    elif isinstance(raw_skills, list) and all(isinstance(name, str) for name in raw_skills):
        skill_names = [name.strip() for name in raw_skills if name.strip()]
    else:
        skill_names = None
    if skill_names is None or len(skill_names) > 3 or len(
        {name.casefold() for name in skill_names or []}
    ) != len(skill_names or []):
        _append_local_batch_row_error(
            row, 'INVALID_SKILLS', '技能必须是最多 3 个不重复的名称。', 'data.skills'
        )
    else:
        normalized_skill_ids = []
        normalized_skill_names = []
        for name in skill_names:
            skill_matches = skill_map.get(name.casefold(), [])
            if not skill_matches:
                _append_local_batch_row_error(
                    row, 'SKILL_NOT_FOUND', f'找不到技能“{name}”。', 'data.skills'
                )
            elif len(skill_matches) > 1:
                _append_local_batch_row_error(
                    row, 'SKILL_AMBIGUOUS', f'存在多个同名技能“{name}”。', 'data.skills'
                )
            else:
                normalized_skill_ids.append(skill_matches[0][0])
                normalized_skill_names.append(skill_matches[0][1])
        if not any(
            error['code'] in ('SKILL_NOT_FOUND', 'SKILL_AMBIGUOUS')
            for error in row['errors']
        ):
            normalized['skill_ids'] = normalized_skill_ids
            normalized['skills'] = normalized_skill_names
            attributes = normalized_skill_names + ['', '', '']
            normalized['attr1'] = attributes[0]
            normalized['attr2'] = attributes[1]
            normalized['attr3'] = attributes[2]

    raw_note = data.get('note')
    if raw_note is None:
        note = ''
    elif isinstance(raw_note, str):
        note = raw_note.strip()
    else:
        note = None
    if note is None or len(note) > MAX_TASK_IMPORT_NOTE_LENGTH:
        _append_local_batch_row_error(
            row, 'INVALID_NOTE',
            f'备注必须是最多 {MAX_TASK_IMPORT_NOTE_LENGTH} 个字符的文本。',
            'data.note',
        )
    else:
        normalized['note'] = note

    raw_rewards = data.get('item_rewards')
    reward_specs = []
    if raw_rewards is None or (
        isinstance(raw_rewards, str) and not raw_rewards.strip()
    ):
        reward_specs = []
    elif isinstance(raw_rewards, str):
        for part in (value.strip() for value in raw_rewards.split('|')):
            if not part:
                continue
            if '*' not in part:
                _append_local_batch_row_error(
                    row, 'INVALID_ITEM_REWARDS',
                    '商品奖励格式必须是“商品名*数量”，多个奖励用 | 分隔。',
                    'data.item_rewards',
                )
                continue
            name, amount = part.rsplit('*', 1)
            reward_specs.append({'name': name.strip(), 'amount': amount.strip()})
    elif isinstance(raw_rewards, list):
        for reward in raw_rewards:
            if not isinstance(reward, dict):
                _append_local_batch_row_error(
                    row, 'INVALID_ITEM_REWARDS',
                    'JSON 商品奖励必须是包含 name 和 amount 的对象。',
                    'data.item_rewards',
                )
                continue
            reward_specs.append({
                'name': reward.get('name'),
                'amount': reward.get('amount', 1),
            })
    else:
        _append_local_batch_row_error(
            row, 'INVALID_ITEM_REWARDS',
            '商品奖励必须是模板文本或 JSON 对象数组。', 'data.item_rewards'
        )

    if len(reward_specs) > MAX_TASK_IMPORT_ITEM_REWARDS:
        _append_local_batch_row_error(
            row, 'INVALID_ITEM_REWARDS',
            f'每个任务最多设置 {MAX_TASK_IMPORT_ITEM_REWARDS} 个商品奖励。',
            'data.item_rewards',
        )
    normalized_rewards = []
    seen_reward_names = set()
    for reward in reward_specs[:MAX_TASK_IMPORT_ITEM_REWARDS]:
        name = reward.get('name')
        if isinstance(name, str):
            name = name.strip()
        if not isinstance(name, str) or not 1 <= len(name) <= 200:
            _append_local_batch_row_error(
                row, 'INVALID_ITEM_REWARD_NAME',
                '商品奖励名称必须是 1～200 个字符。', 'data.item_rewards'
            )
            continue
        name_key = name.casefold()
        if name_key in seen_reward_names:
            _append_local_batch_row_error(
                row, 'DUPLICATE_ITEM_REWARD',
                f'商品奖励“{name}”在同一任务中重复。', 'data.item_rewards'
            )
            continue
        seen_reward_names.add(name_key)

        amount = _task_import_integer(reward.get('amount'), 1, 1)
        if amount is None:
            _append_local_batch_row_error(
                row, 'INVALID_ITEM_REWARD_AMOUNT',
                f'商品奖励“{name}”的数量必须是 1～{MAX_ITEM_PRICE} 的整数。',
                'data.item_rewards',
            )
        reward_matches = item_map.get(name_key, [])
        if not reward_matches:
            _append_local_batch_row_error(
                row, 'ITEM_REWARD_NOT_FOUND', f'找不到商品“{name}”。',
                'data.item_rewards'
            )
        elif len(reward_matches) > 1:
            _append_local_batch_row_error(
                row, 'ITEM_REWARD_AMBIGUOUS', f'存在多个同名商品“{name}”。',
                'data.item_rewards'
            )
        elif amount is not None:
            normalized_rewards.append({
                'item_id': reward_matches[0][0],
                'name': reward_matches[0][1],
                'amount': amount,
            })
    reward_error_codes = {
        'INVALID_ITEM_REWARDS', 'INVALID_ITEM_REWARD_NAME',
        'INVALID_ITEM_REWARD_AMOUNT', 'DUPLICATE_ITEM_REWARD',
        'ITEM_REWARD_NOT_FOUND', 'ITEM_REWARD_AMBIGUOUS',
    }
    if not any(error['code'] in reward_error_codes for error in row['errors']):
        normalized['item_rewards'] = normalized_rewards

    raw_frozen = data.get('is_frozen')
    frozen = None
    if raw_frozen is None or (isinstance(raw_frozen, str) and not raw_frozen.strip()):
        frozen = False
    elif type(raw_frozen) is bool:
        frozen = raw_frozen
    elif type(raw_frozen) is int and raw_frozen in (0, 1):
        frozen = bool(raw_frozen)
    elif isinstance(raw_frozen, str):
        frozen_values = {
            '0': False, '1': True, '否': False, '是': True,
            'false': False, 'true': True, 'no': False, 'yes': True,
        }
        frozen = frozen_values.get(raw_frozen.strip().casefold())
    if frozen is None:
        _append_local_batch_row_error(
            row, 'INVALID_FROZEN_STATE', '冻结状态只接受是/否、true/false 或 0/1。',
            'data.is_frozen'
        )
    else:
        normalized['is_frozen'] = frozen

    raw_policy = data.get('duplicate_policy')
    policy = raw_policy.strip().lower() if isinstance(raw_policy, str) else raw_policy
    if policy in (None, ''):
        policy = ''
    if policy not in ('', 'skip', 'create'):
        _append_local_batch_row_error(
            row, 'INVALID_DUPLICATE_POLICY', '重复策略只接受 skip 或 create。',
            'data.duplicate_policy'
        )
    else:
        normalized['duplicate_policy'] = policy


def _item_import_boolean(value, default):
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        aliases = {
            '0': False, '1': True, '否': False, '是': True,
            'false': False, 'true': True, 'no': False, 'yes': True,
        }
        return aliases.get(value.strip().casefold())
    return None


def _item_import_signed_integer(value, minimum, maximum):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r'[+-]?\d+', value.strip()):
        number = int(value.strip())
    else:
        return None
    return number if minimum <= number <= maximum else None


def _normalize_item_create_data(row, data, category_map, skill_map):
    normalized = row['normalized_data']

    name = data.get('name')
    if isinstance(name, str):
        name = name.strip()
    if not isinstance(name, str) or not 1 <= len(name) <= 200:
        _append_local_batch_row_error(
            row, 'INVALID_ITEM_NAME', '商品名称必须是 1～200 个字符。', 'data.name'
        )
    else:
        normalized['name'] = name

    category = data.get('category')
    if isinstance(category, str):
        category = category.strip()
    matches = category_map.get(category.casefold(), []) if category else []
    if not matches:
        _append_local_batch_row_error(
            row, 'ITEM_CATEGORY_NOT_FOUND', '找不到这个商品分类。', 'data.category'
        )
    elif len(matches) > 1:
        _append_local_batch_row_error(
            row, 'ITEM_CATEGORY_AMBIGUOUS', '存在多个同名商品分类。', 'data.category'
        )
    else:
        normalized['category_id'] = matches[0][0]
        normalized['category'] = matches[0][1]

    price = _task_import_integer(data.get('price'), 0, 0)
    if price is None:
        _append_local_batch_row_error(
            row, 'INVALID_PRICE', f'价格必须是 0～{MAX_ITEM_PRICE} 的整数。',
            'data.price'
        )
    else:
        normalized['price'] = price

    stock = _task_import_integer(data.get('stock'), -1, -1)
    if stock is None:
        _append_local_batch_row_error(
            row, 'INVALID_STOCK', f'库存必须是 -1～{MAX_ITEM_PRICE} 的整数。',
            'data.stock'
        )
    else:
        normalized['stock'] = stock

    purchase_enabled = _item_import_boolean(
        data.get('is_purchase_enabled'), True
    )
    if purchase_enabled is None:
        _append_local_batch_row_error(
            row, 'INVALID_PURCHASE_STATE',
            '购买状态只接受是/否、true/false、yes/no 或 0/1。',
            'data.is_purchase_enabled'
        )
    else:
        normalized['is_purchase_enabled'] = purchase_enabled
        normalized['isdisablepurchase'] = 0 if purchase_enabled else 1

    raw_effect_type = data.get('effect_type')
    effect_type = (
        raw_effect_type.strip().casefold()
        if isinstance(raw_effect_type, str)
        else raw_effect_type
    )
    effect_aliases = {
        None: 'none', '': 'none', 'none': 'none', '无': 'none',
        'coin': 'coin', '金币': 'coin', 'exp': 'exp', '经验': 'exp',
    }
    effect_type = effect_aliases.get(effect_type)
    if effect_type is None:
        _append_local_batch_row_error(
            row, 'INVALID_EFFECT_TYPE', '基础效果只接受 none、coin 或 exp。',
            'data.effect_type'
        )
    else:
        normalized['effect_type'] = effect_type

    effect_value = _task_import_integer(
        data.get('effect_value'), 0 if effect_type == 'none' else None,
        0 if effect_type == 'none' else 1,
    )
    if effect_value is None or (effect_type == 'none' and effect_value != 0):
        _append_local_batch_row_error(
            row, 'INVALID_EFFECT_VALUE',
            '无效果时数值必须为 0；金币或经验效果必须是正整数。',
            'data.effect_value'
        )
    else:
        normalized['effect_value'] = effect_value

    raw_skill = data.get('effect_skill')
    if raw_skill is None or (isinstance(raw_skill, str) and not raw_skill.strip()):
        skill_name = ''
    elif not isinstance(raw_skill, str):
        skill_name = ''
        _append_local_batch_row_error(
            row, 'INVALID_EFFECT_SKILL', '效果技能必须是文本名称。',
            'data.effect_skill'
        )
    else:
        skill_name = raw_skill.strip()
    if skill_name and effect_type != 'exp':
        _append_local_batch_row_error(
            row, 'EFFECT_SKILL_NOT_ALLOWED', '只有经验效果可以指定技能。',
            'data.effect_skill'
        )
    elif skill_name:
        skill_matches = skill_map.get(skill_name.casefold(), [])
        if not skill_matches:
            _append_local_batch_row_error(
                row, 'EFFECT_SKILL_NOT_FOUND', f'找不到技能“{skill_name}”。',
                'data.effect_skill'
            )
        elif len(skill_matches) > 1:
            _append_local_batch_row_error(
                row, 'EFFECT_SKILL_AMBIGUOUS', f'存在多个同名技能“{skill_name}”。',
                'data.effect_skill'
            )
        else:
            normalized['effect_skill'] = skill_matches[0][1]
            normalized['effect_skill_id'] = skill_matches[0][0]

    raw_policy = data.get('duplicate_policy')
    policy = raw_policy.strip().lower() if isinstance(raw_policy, str) else raw_policy
    if policy in (None, ''):
        policy = ''
    if policy not in ('', 'skip', 'create'):
        _append_local_batch_row_error(
            row, 'INVALID_DUPLICATE_POLICY', '重复策略只接受 skip 或 create。',
            'data.duplicate_policy'
        )
    else:
        normalized['duplicate_policy'] = policy


def _normalize_item_price_data(row, data):
    normalized = row['normalized_data']
    legacy_price = data.get('price')
    raw_mode = data.get('price_mode')
    raw_value = data.get('price_value')
    if raw_mode is None and raw_value is None and legacy_price is not None:
        raw_mode = 'set'
        raw_value = legacy_price
    mode = raw_mode.strip().casefold() if isinstance(raw_mode, str) else raw_mode
    if mode in (None, ''):
        mode = 'set'
    if mode not in ('set', 'add', 'percent'):
        _append_local_batch_row_error(
            row, 'INVALID_PRICE_MODE', '改价方式只接受 set、add 或 percent。',
            'data.price_mode'
        )
        return
    bounds = {
        'set': (0, MAX_ITEM_PRICE),
        'add': (-MAX_ITEM_PRICE, MAX_ITEM_PRICE),
        'percent': (-100, MAX_ITEM_PRICE),
    }[mode]
    value = _item_import_signed_integer(raw_value, *bounds)
    if value is None:
        _append_local_batch_row_error(
            row, 'INVALID_PRICE_VALUE',
            '改价值必须是所选模式允许范围内的整数。', 'data.price_value'
        )
        return
    normalized['price_mode'] = mode
    normalized['price_value'] = value


def _calculate_item_price(current_price, mode, value):
    if mode == 'set':
        new_price = value
    elif mode == 'add':
        new_price = current_price + value
    else:
        new_price = (current_price * (100 + value) + 50) // 100
    return new_price if 0 <= new_price <= MAX_ITEM_PRICE else None


def _normalize_achievement_create_data(row, data, category_map):
    normalized = row['normalized_data']

    name = data.get('name')
    if isinstance(name, str):
        name = name.strip()
    if not isinstance(name, str) or not 1 <= len(name) <= 200:
        _append_local_batch_row_error(
            row, 'INVALID_ACHIEVEMENT_NAME',
            '成就名称必须是 1～200 个字符。', 'data.name'
        )
    else:
        normalized['name'] = name

    category = data.get('category')
    if isinstance(category, str):
        category = category.strip()
    if category in (None, ''):
        normalized['category'] = ''
        normalized['category_id'] = 0
    elif not isinstance(category, str):
        _append_local_batch_row_error(
            row, 'INVALID_ACHIEVEMENT_CATEGORY',
            '成就分类必须是文本名称。', 'data.category'
        )
    else:
        matches = category_map.get(category.casefold(), [])
        if not matches:
            _append_local_batch_row_error(
                row, 'INVALID_ACHIEVEMENT_CATEGORY',
                f'找不到成就分类“{category}”。', 'data.category'
            )
        elif len(matches) > 1:
            _append_local_batch_row_error(
                row, 'AMBIGUOUS_ACHIEVEMENT_CATEGORY',
                f'存在多个同名成就分类“{category}”。', 'data.category'
            )
        else:
            normalized['category_id'] = matches[0][0]
            normalized['category'] = matches[0][1]

    description = data.get('description', '')
    if isinstance(description, str):
        description = description.strip()
    if not isinstance(description, str) or len(description) > 2000:
        _append_local_batch_row_error(
            row, 'INVALID_ACHIEVEMENT_DESCRIPTION',
            '成就描述必须是不超过 2000 个字符的文本。',
            'data.description'
        )
    else:
        normalized['description'] = description

    for field, code, label in (
        ('coin', 'INVALID_ACHIEVEMENT_COIN', '金币奖励'),
        ('exp', 'INVALID_ACHIEVEMENT_EXP', '经验奖励'),
    ):
        value = _task_import_integer(data.get(field), 0, 0)
        if value is None:
            _append_local_batch_row_error(
                row, code,
                f'{label}必须是 0～{MAX_ITEM_PRICE} 的整数。',
                f'data.{field}'
            )
        else:
            normalized[field] = value

    icon = data.get('icon', '')
    if isinstance(icon, str):
        icon = icon.strip()
    unsafe_icon = (
        isinstance(icon, str)
        and (
            any(ord(char) < 32 for char in icon)
            or re.match(r'^(?:javascript|vbscript|data):', icon, re.IGNORECASE)
        )
    )
    if not isinstance(icon, str) or len(icon) > 500 or unsafe_icon:
        _append_local_batch_row_error(
            row, 'INVALID_ACHIEVEMENT_ICON',
            '图标引用必须是不超过 500 个字符的安全文本。',
            'data.icon'
        )
    else:
        normalized['icon'] = icon

    conditions = data.get('conditions', '')
    if conditions is not None and (
        not isinstance(conditions, str) or conditions.strip()
    ):
        _append_local_batch_row_error(
            row, 'ACHIEVEMENT_CONDITIONS_NOT_SUPPORTED',
            'Task 9 不支持导入解锁条件，请清空 conditions。',
            'data.conditions'
        )
    else:
        normalized['conditions'] = ''

    raw_policy = data.get('duplicate_policy')
    policy = (
        raw_policy.strip().lower()
        if isinstance(raw_policy, str)
        else raw_policy
    )
    if policy in (None, ''):
        policy = ''
    if policy not in ('', 'skip', 'create'):
        _append_local_batch_row_error(
            row, 'INVALID_DUPLICATE_POLICY',
            '重复策略只接受 skip 或 create。', 'data.duplicate_policy'
        )
    else:
        normalized['duplicate_policy'] = policy


def _normalize_local_batch_rows(entity, raw_rows, cursor):
    allowed_actions = {
        'tasks': {'enable', 'disable', 'delete', 'freeze', 'unfreeze', 'create'},
        'items': {'enable', 'disable', 'delete', 'price', 'create'},
        'achievements': {'create'},
        'icons': {'replace'},
    }
    results = []
    normalized_request_rows = []
    line_indices = {}
    target_indices = {}
    create_title_indices = {}
    create_item_name_indices = {}
    create_achievement_name_indices = {}
    category_map = {}
    skill_map = {}
    task_item_map = {}
    system_achievement_names = set()
    needs_task_create_maps = entity == 'tasks' and any(
        isinstance(raw_row, dict)
        and isinstance(raw_row.get('action'), str)
        and raw_row.get('action').strip().lower() == 'create'
        for raw_row in raw_rows
    )
    if needs_task_create_maps:
        category_map = _task_import_name_map(
            cursor, 'categorymodel', 'id', 'categoryname',
            'isdelete=0 AND categorytype=0'
        )
        skill_map = _task_import_name_map(
            cursor, 'skillmodel', 'id', 'content', 'isdel=0'
        )
        task_item_map = _task_import_name_map(
            cursor, 'shopitemmodel', 'id', 'itemname', 'isdel=0'
        )
    needs_item_create_maps = entity == 'items' and any(
        isinstance(raw_row, dict)
        and isinstance(raw_row.get('action'), str)
        and raw_row.get('action').strip().lower() == 'create'
        for raw_row in raw_rows
    )
    if needs_item_create_maps:
        category_map = _task_import_name_map(
            cursor, 'shopcategorymodel', 'id', 'categoryname', 'isdelete=0'
        )
        skill_map = _task_import_name_map(
            cursor, 'skillmodel', 'id', 'content', 'isdel=0'
        )
    needs_achievement_create_map = entity == 'achievements' and any(
        isinstance(raw_row, dict)
        and isinstance(raw_row.get('action'), str)
        and raw_row.get('action').strip().lower() == 'create'
        for raw_row in raw_rows
    )
    if needs_achievement_create_map:
        category_map = _task_import_name_map(
            cursor, 'userachcategorymodel', 'id', 'categoryname', 'isdelete=0'
        )
        cursor.execute('SELECT title FROM achievementinfomodel')
        system_achievement_names = {
            title.strip().casefold()
            for (title,) in cursor.fetchall()
            if isinstance(title, str) and title.strip()
        }

    for index, raw_row in enumerate(raw_rows):
        row = {
            'line': index + 1,
            'status': 'error',
            'errors': [],
            'normalized_data': {},
            'planned_action': None,
        }
        action = None
        if not isinstance(raw_row, dict):
            _append_local_batch_row_error(
                row,
                'INVALID_ROW',
                '这一行必须是包含 line、action 和 data 的对象。',
                'row',
            )
        else:
            raw_line = raw_row.get('line')
            if type(raw_line) is int and raw_line > 0:
                row['line'] = raw_line
                line_indices.setdefault(raw_line, []).append(index)
            else:
                _append_local_batch_row_error(
                    row,
                    'INVALID_LINE',
                    'line 必须是正整数；结果暂用当前行序号显示。',
                    'line',
                )

            raw_action = raw_row.get('action')
            if isinstance(raw_action, str):
                action = raw_action.strip().lower()
            if action not in allowed_actions[entity]:
                allowed_text = '、'.join(sorted(allowed_actions[entity]))
                _append_local_batch_row_error(
                    row,
                    'INVALID_ACTION',
                    f'不支持这个操作；可用操作：{allowed_text}。',
                    'action',
                )

            data = raw_row.get('data')
            if not isinstance(data, dict):
                _append_local_batch_row_error(
                    row,
                    'INVALID_DATA',
                    'data 必须是对象。',
                    'data',
                )
            else:
                if entity == 'tasks' and action == 'create':
                    _normalize_task_create_data(
                        row, data, category_map, skill_map, task_item_map
                    )
                    title = row['normalized_data'].get('title')
                    if title:
                        create_title_indices.setdefault(
                            title.casefold(), []
                        ).append(index)
                elif entity == 'items' and action == 'create':
                    _normalize_item_create_data(row, data, category_map, skill_map)
                    item_name = row['normalized_data'].get('name')
                    if item_name:
                        create_item_name_indices.setdefault(
                            item_name.casefold(), []
                        ).append(index)
                elif entity == 'achievements' and action == 'create':
                    _normalize_achievement_create_data(row, data, category_map)
                    achievement_name = row['normalized_data'].get('name')
                    if achievement_name:
                        name_key = achievement_name.casefold()
                        if name_key in system_achievement_names:
                            _append_local_batch_row_error(
                                row, 'SYSTEM_ACHIEVEMENT_NAME_CONFLICT',
                                '成就名称与系统成就标识冲突，不能批量新增。',
                                'data.name'
                            )
                        else:
                            create_achievement_name_indices.setdefault(
                                name_key, []
                            ).append(index)
                elif entity == 'icons' and action == 'replace':
                    icon_target = _normalize_icon_replace_data(row, data, cursor)
                    if icon_target is not None:
                        target_indices.setdefault(icon_target, []).append(index)
                else:
                    target_id = data.get('id')
                    if target_id is None and entity == 'items':
                        raw_item_id = data.get('item_id')
                        if isinstance(raw_item_id, str) and re.fullmatch(
                            r'\d+', raw_item_id.strip()
                        ):
                            target_id = int(raw_item_id.strip())
                    if (
                        type(target_id) is int
                        and 0 < target_id <= MAX_SQLITE_INTEGER
                    ):
                        row['normalized_data']['id'] = target_id
                        target_indices.setdefault(target_id, []).append(index)
                    else:
                        _append_local_batch_row_error(
                            row,
                            'INVALID_ID',
                            f'data.id 必须是 1～{MAX_SQLITE_INTEGER} 之间的整数。',
                            'data.id',
                        )

                if entity == 'items' and action == 'price':
                    _normalize_item_price_data(row, data)

        results.append(row)
        normalized_request_rows.append({
            'line': row['line'],
            'action': action,
            'data': dict(row['normalized_data']),
        })

    duplicate_indices = set()
    for indices in line_indices.values():
        if len(indices) > 1:
            duplicate_indices.update(indices)
            for index in indices:
                _append_local_batch_row_error(
                    results[index],
                    'DUPLICATE_LINE',
                    '同一预览中 line 不能重复。',
                    'line',
                )
    for indices in target_indices.values():
        if len(indices) > 1:
            duplicate_indices.update(indices)
            for index in indices:
                _append_local_batch_row_error(
                    results[index],
                    'DUPLICATE_TARGET',
                    '同一预览中不能重复处理同一个实体 ID。',
                    'data.id',
                )

    existing_tasks = {}
    if entity == 'tasks' and create_title_indices:
        cursor.execute(
            'SELECT id, content FROM taskmodel WHERE isdeleterecord=0'
        )
        for task_id, raw_title in cursor.fetchall():
            if isinstance(raw_title, str):
                existing_tasks.setdefault(
                    raw_title.strip().casefold(), []
                ).append(task_id)
        for title_key, indices in create_title_indices.items():
            import_lines = sorted(results[index]['line'] for index in indices)
            existing_ids = sorted(existing_tasks.get(title_key, []))
            found = bool(existing_ids or len(indices) > 1)
            for index in indices:
                results[index]['duplicate'] = {
                    'found': found,
                    'existing_task_ids': existing_ids,
                    'import_lines': import_lines,
                }
                if found:
                    duplicate_indices.add(index)
                    if results[index]['normalized_data'].get('duplicate_policy') == '':
                        _append_local_batch_row_error(
                            results[index],
                            'DUPLICATE_POLICY_REQUIRED',
                            '检测到同名任务，请选择跳过或仍然新增。',
                            'data.duplicate_policy',
                        )
                elif results[index]['normalized_data'].get('duplicate_policy') == '':
                    results[index]['normalized_data']['duplicate_policy'] = 'create'

    existing_items = {}
    if entity == 'items' and create_item_name_indices:
        cursor.execute('SELECT id, itemname FROM shopitemmodel WHERE isdel=0')
        for item_id, raw_name in cursor.fetchall():
            if isinstance(raw_name, str):
                existing_items.setdefault(
                    raw_name.strip().casefold(), []
                ).append(item_id)
        for name_key, indices in create_item_name_indices.items():
            import_lines = sorted(results[index]['line'] for index in indices)
            existing_ids = sorted(existing_items.get(name_key, []))
            found = bool(existing_ids or len(indices) > 1)
            for index in indices:
                results[index]['duplicate'] = {
                    'found': found,
                    'existing_item_ids': existing_ids,
                    'import_lines': import_lines,
                }
                if found:
                    duplicate_indices.add(index)
                    if results[index]['normalized_data'].get('duplicate_policy') == '':
                        _append_local_batch_row_error(
                            results[index],
                            'DUPLICATE_POLICY_REQUIRED',
                            '检测到同名商品，请选择跳过或仍然新增。',
                            'data.duplicate_policy',
                        )
                elif results[index]['normalized_data'].get('duplicate_policy') == '':
                    results[index]['normalized_data']['duplicate_policy'] = 'create'

    existing_achievements = {}
    if entity == 'achievements' and create_achievement_name_indices:
        cursor.execute(
            'SELECT id, content FROM userachievementmodel WHERE isdelete=0'
        )
        for achievement_id, raw_name in cursor.fetchall():
            if isinstance(raw_name, str):
                existing_achievements.setdefault(
                    raw_name.strip().casefold(), []
                ).append(achievement_id)
        for name_key, indices in create_achievement_name_indices.items():
            import_lines = sorted(results[index]['line'] for index in indices)
            existing_ids = sorted(existing_achievements.get(name_key, []))
            found = bool(existing_ids or len(indices) > 1)
            for index in indices:
                results[index]['duplicate'] = {
                    'found': found,
                    'existing_achievement_ids': existing_ids,
                    'import_lines': import_lines,
                }
                if found:
                    duplicate_indices.add(index)
                    if results[index]['normalized_data'].get(
                        'duplicate_policy'
                    ) == '':
                        _append_local_batch_row_error(
                            results[index], 'DUPLICATE_POLICY_REQUIRED',
                            '检测到同名自定义成就，请选择跳过或仍然新增。',
                            'data.duplicate_policy'
                        )
                elif results[index]['normalized_data'].get(
                    'duplicate_policy'
                ) == '':
                    results[index]['normalized_data'][
                        'duplicate_policy'
                    ] = 'create'

    item_target_prices = {}
    if entity in ('tasks', 'items') and target_indices:
        table_name, deleted_column = {
            'tasks': ('taskmodel', 'isdeleterecord'),
            'items': ('shopitemmodel', 'isdel'),
        }[entity]
        target_ids = sorted(target_indices)
        available_ids = set()
        if target_ids:
            placeholders = ','.join('?' * len(target_ids))
            selected_columns = 'id, price' if entity == 'items' else 'id'
            cursor.execute(
                f'SELECT {selected_columns} FROM {table_name} '
                f'WHERE {deleted_column}=0 AND id IN ({placeholders})',
                target_ids,
            )
            records = cursor.fetchall()
            available_ids = {record[0] for record in records}
            if entity == 'items':
                item_target_prices = {record[0]: record[1] for record in records}
        for target_id, indices in target_indices.items():
            if target_id in available_ids:
                continue
            for index in indices:
                _append_local_batch_row_error(
                    results[index],
                    'TARGET_NOT_FOUND',
                    '目标不存在或已被删除，请刷新页面后重试。',
                    'data.id',
                )

    if entity == 'items':
        for row in results:
            normalized = row['normalized_data']
            if (
                normalized.get('id') not in item_target_prices
                or 'price_mode' not in normalized
                or 'price_value' not in normalized
            ):
                continue
            current_price = item_target_prices[normalized['id']]
            if type(current_price) is not int or not 0 <= current_price <= MAX_ITEM_PRICE:
                _append_local_batch_row_error(
                    row, 'INVALID_CURRENT_PRICE',
                    '商品当前价格不是可安全计算的整数。', 'data.id'
                )
                continue
            new_price = _calculate_item_price(
                current_price, normalized['price_mode'], normalized['price_value']
            )
            if new_price is None:
                _append_local_batch_row_error(
                    row, 'INVALID_PRICE_RESULT',
                    f'改价结果必须在 0～{MAX_ITEM_PRICE} 之间。',
                    'data.price_value'
                )
                continue
            normalized['current_price'] = current_price
            normalized['price'] = new_price

    for index, row in enumerate(results):
        normalized_request_rows[index] = {
            'line': row['line'],
            'action': normalized_request_rows[index]['action'],
            'data': dict(row['normalized_data']),
        }
        if row['errors']:
            continue
        row['status'] = 'ready'
        row['planned_action'] = {
            'entity': entity,
            'action': normalized_request_rows[index]['action'],
            'data': dict(row['normalized_data']),
        }
        if entity == 'tasks' and row['planned_action']['action'] == 'create':
            row['planned_action']['duplicate_found'] = row['duplicate']['found']
        if entity == 'items' and row['planned_action']['action'] == 'create':
            row['planned_action']['duplicate_found'] = row['duplicate']['found']
        if entity == 'achievements' and row['planned_action']['action'] == 'create':
            row['planned_action']['duplicate_found'] = row['duplicate']['found']

    return results, normalized_request_rows, len(duplicate_indices)


def _cleanup_local_batch_previews_locked(now):
    stale_tokens = [
        token
        for token, preview in LOCAL_BATCH_PREVIEWS.items()
        if preview.get('expires_at', 0) <= now
    ]
    for token in stale_tokens:
        LOCAL_BATCH_PREVIEWS.pop(token, None)


def _allocate_local_batch_preview_token_locked():
    for _attempt in range(10):
        token = secrets.token_hex(32)
        if token not in LOCAL_BATCH_PREVIEWS:
            return token
    raise RuntimeError('unable to allocate local batch preview token')


def _execute_local_batch_action(cursor, planned_action):
    entity = planned_action['entity']
    action = planned_action['action']
    data = planned_action['data']
    if entity == 'tasks' and action == 'create':
        if planned_action.get('duplicate_found') and data['duplicate_policy'] == 'skip':
            return {'affected': 0, 'skipped': True, 'reason': 'duplicate'}
        _insert_task_with_cursor(cursor, data)
        return {'affected': 1}
    if entity == 'items' and action == 'create':
        if planned_action.get('duplicate_found') and data['duplicate_policy'] == 'skip':
            return {'affected': 0, 'skipped': True, 'reason': 'duplicate'}
        _insert_item_with_cursor(cursor, data)
        return {'affected': 1}
    if entity == 'achievements' and action == 'create':
        if planned_action.get('duplicate_found') and data['duplicate_policy'] == 'skip':
            return {'affected': 0, 'skipped': True, 'reason': 'duplicate'}
        _insert_achievement_with_cursor(cursor, data)
        return {'affected': 1}
    if entity == 'icons' and action == 'replace':
        spec = {
            row['entity']: row
            for row in ICON_REFERENCE_SPECS
            if row['editable']
        }[data['entity_type']]
        target_info = _download_icon_target_info(data['new_icon'])
        if (
            target_info is None
            or not secrets.compare_digest(
                target_info['sha256'], data.get('target_sha256', '')
            )
        ):
            raise LocalBatchExecutionChanged('preview icon file changed')
        cursor.execute(
            f'UPDATE {spec["table"]} SET {spec["icon_column"]}=? '
            f'WHERE id=? AND {spec["deleted_column"]}=0 '
            f'AND {spec["icon_column"]}=?',
            (data['new_icon'], data['id'], data['old_icon']),
        )
        if cursor.rowcount != 1:
            raise LocalBatchExecutionChanged('preview icon target changed')
        return {'affected': 1}
    target_id = data['id']
    if entity == 'tasks':
        now = now_ms()
        if action == 'enable':
            cursor.execute(
                'UPDATE taskmodel SET taskstatus=1, updatedtime=? '
                'WHERE id=? AND isdeleterecord=0',
                (now, target_id),
            )
        elif action == 'disable':
            cursor.execute(
                'UPDATE taskmodel SET taskstatus=0, updatedtime=? '
                'WHERE id=? AND isdeleterecord=0',
                (now, target_id),
            )
        elif action == 'delete':
            cursor.execute(
                'UPDATE taskmodel SET isdeleterecord=1, updatedtime=? '
                'WHERE id=? AND isdeleterecord=0',
                (now, target_id),
            )
        elif action == 'freeze':
            cursor.execute(
                'UPDATE taskmodel SET isfrozen=1, updatedtime=? '
                'WHERE id=? AND isdeleterecord=0',
                (now, target_id),
            )
        else:
            cursor.execute(
                'UPDATE taskmodel SET isfrozen=0, updatedtime=? '
                'WHERE id=? AND isdeleterecord=0',
                (now, target_id),
            )
    elif action == 'enable':
        cursor.execute(
            'UPDATE shopitemmodel SET isdisablepurchase=0 WHERE id=? AND isdel=0',
            (target_id,),
        )
    elif action == 'disable':
        cursor.execute(
            'UPDATE shopitemmodel SET isdisablepurchase=1 WHERE id=? AND isdel=0',
            (target_id,),
        )
    elif action == 'delete':
        cursor.execute(
            'UPDATE shopitemmodel SET isdel=1 WHERE id=? AND isdel=0',
            (target_id,),
        )
    else:
        cursor.execute(
            'UPDATE shopitemmodel SET price=? '
            'WHERE id=? AND isdel=0 AND price=?',
            (data['price'], target_id, data['current_price']),
        )
    if cursor.rowcount != 1:
        raise LocalBatchExecutionChanged('preview target changed')
    return {'affected': 1}


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


def _icon_reference_expected_path(reference, default_folder):
    text = _safe_icon_reference(reference)
    if text is None or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', text):
        return None
    normalized = text.replace('\\', '/')
    if normalized.startswith('/'):
        return None
    parts = normalized.split('/')
    if any(part in ('', '.', '..') for part in parts):
        return None
    if len(parts) == 1:
        if default_folder not in MEDIA_FOLDERS:
            return None
        return f'media/{default_folder}/{parts[0]}'
    if len(parts) == 3 and parts[0].casefold() == 'media':
        folder = next(
            (name for name in MEDIA_FOLDERS if name.casefold() == parts[1].casefold()),
            None,
        )
        if folder:
            return f'media/{folder}/{parts[2]}'
    return None


def _icon_table_columns(cursor, table):
    try:
        return {row[1] for row in cursor.execute(f'PRAGMA table_info({table})')}
    except sqlite3.DatabaseError:
        return set()


def _collect_icon_references(cursor):
    groups = {}
    for spec in ICON_REFERENCE_SPECS:
        if spec.get('builtin'):
            continue
        columns = _icon_table_columns(cursor, spec['table'])
        required = {'id', spec['name_column'], spec['icon_column']}
        if spec['deleted_column']:
            required.add(spec['deleted_column'])
        if not required.issubset(columns):
            continue
        where = (
            f'WHERE {spec["deleted_column"]}=0'
            if spec['deleted_column']
            else ''
        )
        cursor.execute(
            f'SELECT id, {spec["name_column"]}, {spec["icon_column"]} '
            f'FROM {spec["table"]} {where}'
        )
        for record_id, entity_name, raw_reference in cursor.fetchall():
            expected_path = _icon_reference_expected_path(
                raw_reference, spec['folder']
            )
            if expected_path is None:
                continue
            reference = raw_reference.strip()
            key = (reference.casefold(), expected_path.casefold())
            group = groups.setdefault(key, {
                'reference': reference,
                'expected_path': expected_path,
                'exists': False,
                'editable': False,
                'references': [],
            })
            group['editable'] = group['editable'] or spec['editable']
            group['references'].append({
                'entity': spec['entity'],
                'id': record_id,
                'name': str(entity_name or ''),
                'editable': spec['editable'],
            })
    return groups


def _workspace_icon_files(reference_groups):
    workspace = os.path.abspath(STATE['tmpdir'])
    references_by_path = {}
    for group in reference_groups.values():
        references_by_path.setdefault(
            group['expected_path'].casefold(), []
        ).extend(group['references'])

    files = []
    actual_paths = set()
    for folder in sorted(MEDIA_FOLDERS):
        folder_root = os.path.abspath(os.path.join(workspace, 'media', folder))
        if not _path_is_within(folder_root, workspace) or not os.path.isdir(folder_root):
            continue
        for root, directories, filenames in os.walk(folder_root):
            directories[:] = [
                name for name in directories
                if not os.path.islink(os.path.join(root, name))
            ]
            for filename in filenames:
                path = os.path.join(root, filename)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                extension = Path(filename).suffix.lower()
                expected_format = ICON_UPLOAD_EXTENSIONS.get(extension)
                if expected_format is None:
                    continue
                relative = os.path.relpath(path, workspace).replace('\\', '/')
                actual_paths.add(relative.casefold())
                content, size = _icon_file_bytes(path)
                detected_format = _detect_safe_icon_format(content)
                signature_valid = detected_format is not None
                extension_matches_signature = detected_format == expected_format
                references = list(references_by_path.get(relative.casefold(), []))
                relative_inside_media = os.path.relpath(path, folder_root).replace('\\', '/')
                files.append({
                    'path': relative,
                    'folder': folder,
                    'filename': relative_inside_media,
                    'size': size,
                    'format': detected_format or expected_format,
                    'signature_valid': signature_valid,
                    'extension_matches_signature': extension_matches_signature,
                    'referenced': bool(references),
                    'references': references,
                    'media_url': f'/api/media/{folder}/{relative_inside_media}',
                })
    for group in reference_groups.values():
        group['exists'] = group['expected_path'].casefold() in actual_paths
    files.sort(key=lambda row: row['path'].casefold())
    return files


def _validate_icon_references_for_export_locked():
    """Reject exports whose direct local icon references are missing or unreadable."""
    connection = None
    try:
        connection = get_db()
        reference_groups = _collect_icon_references(connection.cursor())
        files = _workspace_icon_files(reference_groups)
    except (OSError, sqlite3.DatabaseError) as exc:
        raise BackupExportError(
            'ICON_REFERENCE_VALIDATION_FAILED',
            '导出前无法完成图标引用检查',
            '没有生成导出文件；请重新加载工作副本后再试。',
            422,
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    missing_groups = [
        group for group in reference_groups.values() if not group['exists']
    ]
    invalid_files = [
        row for row in files if row['referenced'] and not row['signature_valid']
    ]
    missing_references = sum(
        len(group['references']) for group in missing_groups
    )
    invalid_references = sum(
        len(row['references']) for row in invalid_files
    )
    if missing_references or invalid_references:
        raise BackupExportError(
            'ICON_REFERENCE_VALIDATION_FAILED',
            '导出前图标引用检查未通过：'
            f'{missing_references} 条引用缺少文件，'
            f'{invalid_references} 条引用指向无法识别的图片',
            '请打开“图标资源”页面查看失效引用，并替换为有效图片后再导出。',
            422,
        )
    return {
        'status': 'ok',
        'direct_references': sum(
            len(group['references']) for group in reference_groups.values()
        ),
        'referenced_files': sum(row['referenced'] for row in files),
        'missing_references': 0,
        'invalid_references': 0,
    }


def _icon_list_args():
    search = request.args.get('search', '').strip()
    if len(search) > MAX_ICON_SEARCH_LENGTH:
        raise BatchValidationError(
            f'图标搜索词不能超过 {MAX_ICON_SEARCH_LENGTH} 个字符'
        )
    folder = request.args.get('folder', 'all').strip() or 'all'
    if folder != 'all' and folder not in MEDIA_FOLDERS:
        raise BatchValidationError('未知图标目录')
    status_filter = request.args.get('status', 'all').strip() or 'all'
    if status_filter not in (
        'all', 'referenced', 'unreferenced', 'invalid', 'mismatch'
    ):
        raise BatchValidationError(
            'status 只接受 all、referenced、unreferenced、invalid 或 mismatch'
        )
    try:
        limit = int(request.args.get('limit', '100'))
        offset = int(request.args.get('offset', '0'))
    except (TypeError, ValueError) as exc:
        raise BatchValidationError('图标分页参数必须是整数') from exc
    if not 1 <= limit <= MAX_ICON_LIST_LIMIT or not 0 <= offset <= 1000000:
        raise BatchValidationError(
            f'limit 必须是 1～{MAX_ICON_LIST_LIMIT}，offset 必须不小于 0'
        )
    return search, folder, status_filter, limit, offset


def media_url(folder, filename):
    if not filename:
        return ''
    text = str(filename).strip()
    if text.startswith(('http://', 'https://', 'data:')):
        return text
    expected_path = _icon_reference_expected_path(text, folder)
    prefix = f'media/{folder}/'
    if not expected_path or not expected_path.startswith(prefix):
        return ''
    return f'/api/media/{folder}/{quote(expected_path[len(prefix):], safe="/")}'


@app.route('/api/local/icons')
def list_local_icons():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    try:
        search, folder, status_filter, limit, offset = _icon_list_args()
    except BatchValidationError as exc:
        return _local_batch_error('INVALID_ICON_FILTER', str(exc), 400)
    connection = None
    try:
        with STATE_LOCK:
            connection = get_db()
            reference_groups = _collect_icon_references(connection.cursor())
            files = _workspace_icon_files(reference_groups)
    except RuntimeError:
        return _local_batch_error(
            'NO_BACKUP_LOADED', '未加载本地备份工作副本。', 400,
            '请先载入工作副本，再管理图标。'
        )
    except (OSError, sqlite3.DatabaseError):
        return _local_batch_error(
            'ICON_AUDIT_FAILED', '图标检查失败。', 500,
            '没有修改任何文件或数据库；请重新加载工作副本后重试。'
        )
    finally:
        if connection is not None:
            connection.close()

    groups = sorted(
        reference_groups.values(),
        key=lambda row: (row['reference'].casefold(), row['expected_path'].casefold()),
    )
    summary = {
        'files': len(files),
        'referenced_files': sum(row['referenced'] for row in files),
        'unreferenced_files': sum(not row['referenced'] for row in files),
        'invalid_files': sum(not row['signature_valid'] for row in files),
        'extension_mismatches': sum(
            row['signature_valid'] and not row['extension_matches_signature']
            for row in files
        ),
        'missing_references': sum(
            len(group['references']) for group in groups if not group['exists']
        ),
    }
    search_key = search.casefold()
    filtered = []
    for row in files:
        if folder != 'all' and row['folder'] != folder:
            continue
        if search_key and search_key not in row['path'].casefold():
            continue
        if status_filter == 'referenced' and not row['referenced']:
            continue
        if status_filter == 'unreferenced' and row['referenced']:
            continue
        if status_filter == 'invalid' and row['signature_valid']:
            continue
        if status_filter == 'mismatch' and (
            not row['signature_valid'] or row['extension_matches_signature']
        ):
            continue
        filtered.append(row)
    return jsonify({
        'ok': True,
        'files': filtered[offset:offset + limit],
        'reference_groups': groups,
        'replacement_targets': [
            {
                'reference': row['filename'], 'path': row['path'],
                'media_url': row['media_url'], 'size': row['size'],
            }
            for row in files
            if row['folder'] == 'download'
            and row['signature_valid']
            and '/' not in row['filename']
        ],
        'summary': summary,
        'pagination': {
            'total': len(filtered), 'limit': limit, 'offset': offset,
            'has_more': offset + limit < len(filtered),
        },
    })


@app.route('/api/local/icon-files', methods=['POST'])
def upload_local_icon_file():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return _local_batch_error(
            'ICON_FILE_REQUIRED', '请选择一个图标文件。', 400
        )
    filename = upload.filename
    if (
        len(filename) > 255
        or filename in ('.', '..')
        or '/' in filename
        or '\\' in filename
        or os.path.basename(filename) != filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        return _local_batch_error(
            'ICON_INVALID_FILENAME', '图标文件名包含非法路径或控制字符。', 400
        )
    extension = Path(filename).suffix.lower()
    expected_format = ICON_UPLOAD_EXTENSIONS.get(extension)
    if expected_format is None:
        return _local_batch_error(
            'ICON_UNSUPPORTED_TYPE',
            '只支持 PNG、JPEG、GIF 或 WebP 图片；不支持 SVG。', 400
        )
    content = upload.stream.read(MAX_ICON_FILE_BYTES + 1)
    if not content:
        return _local_batch_error('ICON_FILE_EMPTY', '图标文件不能为空。', 400)
    if len(content) > MAX_ICON_FILE_BYTES:
        return _local_batch_error(
            'ICON_FILE_TOO_LARGE', '单个图标不能超过 5 MiB。', 400
        )
    detected_format = _detect_safe_icon_format(content)
    if detected_format != expected_format:
        return _local_batch_error(
            'ICON_TYPE_MISMATCH',
            '文件扩展名与真实图片格式不一致，或图片内容已损坏。', 400
        )
    canonical_extension = {
        'png': '.png', 'jpeg': '.jpg', 'gif': '.gif', 'webp': '.webp'
    }[detected_format]
    digest = hashlib.sha256(content).hexdigest()
    generated_name = f'lifeup_dashboard_{digest[:24]}{canonical_extension}'
    try:
        with STATE_LOCK:
            if not STATE.get('loaded'):
                raise RuntimeError('not loaded')
            target_directory = os.path.abspath(
                os.path.join(STATE['tmpdir'], 'media', 'download')
            )
            if not _path_is_within(target_directory, STATE['tmpdir']):
                raise OSError('invalid media root')
            os.makedirs(target_directory, exist_ok=True)
            target = os.path.abspath(os.path.join(target_directory, generated_name))
            if os.path.dirname(target) != target_directory:
                raise OSError('invalid generated path')
            created = False
            if os.path.exists(target):
                with open(target, 'rb') as existing:
                    if existing.read(MAX_ICON_FILE_BYTES + 1) != content:
                        raise OSError('generated filename collision')
            else:
                target_created_by_request = False
                try:
                    with open(target, 'xb') as output:
                        target_created_by_request = True
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    created = True
                except Exception:
                    if target_created_by_request:
                        try:
                            os.remove(target)
                        except OSError:
                            pass
                    raise
    except RuntimeError:
        return _local_batch_error(
            'NO_BACKUP_LOADED', '未加载本地备份工作副本。', 400
        )
    except OSError:
        return _local_batch_error(
            'ICON_WRITE_FAILED', '图标无法安全写入工作副本。', 500,
            '没有覆盖任何已有图标；请检查磁盘空间后重试。'
        )
    icon = {
        'filename': generated_name,
        'reference': generated_name,
        'folder': 'download',
        'path': f'media/download/{generated_name}',
        'media_url': f'/api/media/download/{generated_name}',
        'size': len(content),
        'format': detected_format,
        'sha256': digest,
        'signature_valid': True,
    }
    return jsonify({
        'ok': True, 'created': created, 'deduplicated': not created,
        'icon': icon,
    }), 201 if created else 200

def now_ms():
    return int(time.time() * 1000)


CLOUD_CONFIG_PATH = os.path.join(DATA_DIR, 'lifeup_cloud_config.json')
CLOUD_RUNTIME_CONFIG = {'api_token': ''}
CLOUD_RUNTIME_CONFIG_LOCK = threading.Lock()
CLOUD_READ_CACHE_TTL_SECONDS = 30
CLOUD_READ_CACHE = {}
CLOUD_READ_CACHE_LOCK = threading.RLock()
CLOUD_PREVIEW_TTL_SECONDS = 10 * 60
CLOUD_EXECUTION_TTL_SECONDS = 60 * 60
CLOUD_PREVIEWS = {}
CLOUD_EXECUTIONS = {}
CLOUD_EXECUTION_LOCK = threading.Lock()
CLOUD_OPERATION_LOG_PATH = os.path.join(WORK_DIR, 'cloud-operation-log.jsonl')
CLOUD_OPERATION_LOG_LOCK = threading.RLock()
CLOUD_OPERATION_LOG_LIMIT = 500


class CloudRequestError(RuntimeError):
    """Stable, user-facing classification for cloud connection failures."""

    def __init__(self, code, message, suggestion, category, status=502, retryable=True):
        super().__init__(message)
        self.code = code
        self.suggestion = suggestion
        self.category = category
        self.status = status
        self.retryable = retryable

    def details(self):
        return {
            'code': self.code,
            'error': str(self),
            'category': self.category,
            'suggestion': self.suggestion,
            'retryable': self.retryable,
        }


class CloudTaskValidationError(ValueError):
    """A cloud add-task field failed validation before preview registration."""

    def __init__(self, message, field='record', row=None):
        super().__init__(message)
        self.field = field
        self.row = row


@app.errorhandler(CloudRequestError)
def handle_cloud_request_error(error):
    return jsonify(error.details()), error.status


def cloud_error_response(error):
    return jsonify(error.details()), error.status


def clear_cloud_read_cache():
    with CLOUD_READ_CACHE_LOCK:
        CLOUD_READ_CACHE.clear()


def _cloud_config_error(code, message, suggestion):
    return CloudRequestError(
        code, message, suggestion,
        category='configuration', status=400, retryable=False,
    )


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
        raise _cloud_config_error(
            'CLOUD_HOST_MISSING',
            '请先填写手机 IP',
            '在手机云人升中开启服务，然后填写手机当前局域网 IP，例如 192.168.1.20。',
        )
    try:
        port = int(port_raw or 13276)
    except (TypeError, ValueError):
        raise _cloud_config_error(
            'CLOUD_PORT_INVALID',
            '端口必须是数字',
            '填写手机云人升页面显示的端口；默认通常是 13276。',
        )
    if port < 1 or port > 65535:
        raise _cloud_config_error(
            'CLOUD_PORT_INVALID',
            '端口必须在 1-65535 之间',
            '填写手机云人升页面显示的端口；默认通常是 13276。',
        )
    return {
        'host': host,
        'port': port,
        'api_token': token,
        'base_url': f'http://{host}:{port}' if host else ''
    }


def update_runtime_cloud_token(data=None):
    """Keep the optional API token in process memory only."""
    data = data or {}
    changed = False
    with CLOUD_RUNTIME_CONFIG_LOCK:
        previous = CLOUD_RUNTIME_CONFIG.get('api_token', '')
        if data.get('clear_token'):
            CLOUD_RUNTIME_CONFIG['api_token'] = ''
        else:
            token = str(data.get('api_token') or data.get('token') or '').strip()
            if token:
                CLOUD_RUNTIME_CONFIG['api_token'] = token
        changed = previous != CLOUD_RUNTIME_CONFIG.get('api_token', '')
        present = bool(CLOUD_RUNTIME_CONFIG.get('api_token'))
    if changed:
        clear_cloud_read_cache()
    return present


def cloud_token_in_memory():
    with CLOUD_RUNTIME_CONFIG_LOCK:
        return bool(CLOUD_RUNTIME_CONFIG.get('api_token'))


def _cloud_http_error(error):
    if error.code in (401, 403):
        return CloudRequestError(
            'CLOUD_AUTH_FAILED',
            '手机拒绝了认证，Token 不正确或已经失效',
            '回到手机云人升复制当前 Token，重新填写并检测连接；Token 只会保存在本进程内存中。',
            category='authentication', status=401, retryable=False,
        )
    return CloudRequestError(
        'CLOUD_HTTP_ERROR',
        f'手机服务返回 HTTP {error.code}',
        '确认 Host 和端口指向云人升服务；如果手机刚切换网络，请更新 IP 后重试。',
        category='response', status=502, retryable=True,
    )


def _cloud_transport_error(error, write=False):
    reason = error.reason if isinstance(error, URLError) else error
    reason_text = str(reason or error).lower()
    if isinstance(reason, (TimeoutError,)) or 'timed out' in reason_text or 'timeout' in reason_text:
        suggestion = (
            '手机端可能已经执行。请先刷新任务列表或查看手机，不要立刻重复点击。'
            if write else
            '确认手机与电脑在同一局域网、云人升服务仍开启，然后点击重试。'
        )
        return CloudRequestError(
            'CLOUD_TIMEOUT', '连接手机超时', suggestion,
            category='timeout', status=504, retryable=not write,
        )
    refused = isinstance(reason, ConnectionRefusedError) or getattr(reason, 'errno', None) in (10061, 111)
    if refused or 'refused' in reason_text or '10061' in reason_text:
        return CloudRequestError(
            'CLOUD_CONNECTION_REFUSED',
            '手机拒绝连接，通常是云人升服务未开启或端口不正确',
            '在手机云人升页面确认服务已开启，并核对页面显示的端口后重试。',
            category='network', status=502, retryable=True,
        )
    return CloudRequestError(
        'CLOUD_NETWORK_UNREACHABLE',
        '无法连接到手机',
        '确认手机与电脑连接同一局域网，IP 没有变化，并允许 LifeUp 在局域网中提供服务。',
        category='network', status=502, retryable=True,
    )


def _decode_cloud_response(raw, status):
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CloudRequestError(
            'CLOUD_RESPONSE_INVALID',
            f'手机返回了无法识别的数据（HTTP {status}）',
            '确认 Host 和端口对应云人升服务，而不是其他网页或应用端口。',
            category='response', status=502, retryable=True,
        ) from exc
    if not isinstance(payload, dict) or 'code' not in payload:
        raise CloudRequestError(
            'CLOUD_RESPONSE_INVALID',
            '手机返回格式不符合云人升 API 约定',
            '确认手机 LifeUp/云人升版本支持当前 API，并核对 Host 与端口。',
            category='response', status=502, retryable=True,
        )
    try:
        response_code = int(payload.get('code'))
    except (TypeError, ValueError) as exc:
        raise CloudRequestError(
            'CLOUD_RESPONSE_INVALID',
            '手机返回的 API 状态码格式异常',
            '确认手机 LifeUp/云人升版本支持当前 API，并重新检测连接。',
            category='response', status=502, retryable=True,
        ) from exc
    if response_code != 200:
        message = str(payload.get('message') or payload.get('msg') or '')
        if response_code in (401, 403) or any(word in message.lower() for word in ('token', 'auth', 'unauthorized')):
            raise CloudRequestError(
                'CLOUD_AUTH_FAILED',
                '手机拒绝了认证，Token 不正确或已经失效',
                '回到手机云人升复制当前 Token，重新填写并检测连接。',
                category='authentication', status=401, retryable=False,
            )
        raise CloudRequestError(
            'CLOUD_API_ERROR',
            message or f'云人升 API 返回错误状态 {response_code}',
            '在手机确认云人升服务状态；如果问题持续，请记录当前页面和接口名称。',
            category='response', status=502, retryable=True,
        )
    return payload


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
        raise _cloud_http_error(exc) from exc
    except (TimeoutError, URLError) as exc:
        raise _cloud_transport_error(exc) from exc
    payload = _decode_cloud_response(raw, status)
    return {'route': route, 'base_url': cfg['base_url'], 'response': payload, 'data': payload.get('data')}


def _cloud_cache_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')


def cloud_cached_request(config, route, timeout=12, cache_ttl=CLOUD_READ_CACHE_TTL_SECONDS, force_refresh=False):
    """Read-through process-memory cache. Token values never enter cache keys or entries."""
    cfg = normalize_cloud_config(config)
    explicit_token = bool(str((config or {}).get('api_token') or (config or {}).get('token') or '').strip())
    key = (cfg['base_url'], route)
    current = time.time()
    if not force_refresh and not explicit_token:
        with CLOUD_READ_CACHE_LOCK:
            cached = CLOUD_READ_CACHE.get(key)
            if cached and cached['expires_at'] > current:
                result = copy.deepcopy(cached['result'])
                result['cache'] = {
                    'source': 'memory_cache',
                    'fetched_at': _cloud_cache_time(cached['fetched_at']),
                    'expires_at': _cloud_cache_time(cached['expires_at']),
                }
                return result
            if cached:
                CLOUD_READ_CACHE.pop(key, None)

    result = cloud_request(config, route, timeout=timeout)
    fetched_at = time.time()
    expires_at = fetched_at + max(1, int(cache_ttl))
    result = copy.deepcopy(result)
    result['cache'] = {
        'source': 'live',
        'fetched_at': _cloud_cache_time(fetched_at),
        'expires_at': _cloud_cache_time(expires_at),
    }
    if not explicit_token:
        with CLOUD_READ_CACHE_LOCK:
            CLOUD_READ_CACHE[key] = {
                'result': copy.deepcopy({k: v for k, v in result.items() if k != 'cache'}),
                'fetched_at': fetched_at,
                'expires_at': expires_at,
            }
    return result


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
        raise _cloud_http_error(exc) from exc
    except (TimeoutError, URLError) as exc:
        raise _cloud_transport_error(exc, write=True) from exc
    payload = _decode_cloud_response(raw, status)
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
    for index, raw in enumerate(raw_urls, start=1):
        url = str(raw or '').strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme != 'lifeup' or parsed.netloc != 'api' or parsed.path != '/add_task':
            raise CloudTaskValidationError(
                '当前只允许执行 lifeup://api/add_task 新增任务', 'url', index
            )
        try:
            validate_lifeup_task_query(parsed.query)
        except CloudTaskValidationError as exc:
            exc.row = index
            raise
        urls.append(url)
    if not urls:
        raise CloudTaskValidationError('没有可执行的 LifeUp API URL', 'url')
    if len(urls) > 200:
        raise CloudTaskValidationError('单次最多执行 200 条任务，建议分批导入', 'url')
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
        raise CloudTaskValidationError(f'不支持的任务参数: {unknown[0]}', unknown[0])
    todo_values = query.get('todo', [])
    if len(todo_values) != 1 or not todo_values[0].strip():
        raise CloudTaskValidationError('todo 任务标题不能为空且只能出现一次', 'todo')

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
            raise CloudTaskValidationError(f'{field} 只能出现一次', field)
        if not values:
            continue
        raw = values[0].strip()
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise CloudTaskValidationError(f'{field} 必须是整数', field)
        if str(value) != raw and raw not in (f'+{value}', f'-{abs(value)}'):
            raise CloudTaskValidationError(f'{field} 必须是整数', field)
        if value < minimum:
            raise CloudTaskValidationError(f'{field} 不能小于 {minimum}', field)
        if maximum is not None and value > maximum:
            raise CloudTaskValidationError(f'{field} 不能大于 {maximum}', field)

    for raw in query.get('skills', []):
        text = raw.strip()
        try:
            skill_id = int(text)
        except (TypeError, ValueError):
            raise CloudTaskValidationError('skills 必须是整数 ID', 'skills')
        if str(skill_id) != text or skill_id < 0:
            raise CloudTaskValidationError('skills 必须是非负整数 ID', 'skills')


def cloud_task_summaries(urls):
    summaries = []
    for index, url in enumerate(urls, start=1):
        query = parse_qs(urlparse(url).query, keep_blank_values=True)
        summaries.append({
            'row': index,
            'title': (query.get('todo') or [''])[0],
            'category_id': (query.get('category') or [None])[0],
            'skill_ids': list(query.get('skills') or []),
        })
    return summaries


def cloud_operation_timestamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def append_cloud_operation(record):
    """Append a deliberately small, secret-free cloud audit record."""
    path = Path(CLOUD_OPERATION_LOG_PATH)
    with CLOUD_OPERATION_LOG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')


def read_cloud_operations(limit=CLOUD_OPERATION_LOG_LIMIT):
    path = Path(CLOUD_OPERATION_LOG_PATH)
    with CLOUD_OPERATION_LOG_LOCK:
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except FileNotFoundError:
            return []
    records = []
    for line in lines[-max(1, min(int(limit), CLOUD_OPERATION_LOG_LIMIT)):]:
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def cloud_execution_items(summaries, raw_results):
    values = as_cloud_rows(raw_results)
    items = []
    for index, summary in enumerate(summaries):
        raw = values[index] if index < len(values) else None
        status = 'unknown'
        if isinstance(raw, bool):
            status = 'success' if raw else 'failed'
        elif isinstance(raw, dict):
            flags = [raw.get(key) for key in ('ok', 'success') if key in raw]
            code = raw.get('code')
            if any(value is False for value in flags):
                status = 'failed'
            elif flags and all(value is True for value in flags):
                status = 'success'
            elif code is not None:
                try:
                    status = 'success' if int(code) in (0, 200) else 'failed'
                except (TypeError, ValueError):
                    status = 'unknown'
            else:
                status = 'success'
        elif raw is not None:
            status = 'success'
        message = {
            'success': '手机接口已确认成功',
            'failed': '手机接口报告失败',
            'unknown': '手机未返回此条的明确结果，请刷新任务确认',
        }[status]
        items.append({
            'row': summary['row'],
            'title': summary['title'],
            'status': status,
            'message': message,
        })
    return items


def cloud_execution_summary(items):
    return {
        status: sum(1 for item in items if item['status'] == status)
        for status in ('success', 'failed', 'unknown')
    }


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


TASK_STATUS_PENDING = 0
TASK_STATUS_COMPLETED = 1
TASK_STATUS_ABANDONED = 2


def is_task_completed_status(value):
    return int_value(value, TASK_STATUS_PENDING) == TASK_STATUS_COMPLETED


def is_task_pending_status(value):
    return int_value(value, TASK_STATUS_PENDING) == TASK_STATUS_PENDING


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


def _normalize_goal_category_ids(value, field_name):
    if not isinstance(value, list):
        raise ValueError(f'{field_name} 必须是分类 ID 数组')
    if len(value) > MAX_GOAL_CATEGORY_COUNT:
        raise ValueError(f'{field_name} 最多包含 {MAX_GOAL_CATEGORY_COUNT} 个分类')
    normalized = []
    seen = set()
    for raw_id in value:
        if type(raw_id) is not int or raw_id <= 0:
            raise ValueError(f'{field_name} 只能包含正整数分类 ID')
        if raw_id not in seen:
            normalized.append(raw_id)
            seen.add(raw_id)
    return normalized


def normalize_goal_config(data):
    if not isinstance(data, dict):
        raise ValueError('宏愿配置必须是 JSON 对象')
    version = data.get('version', 1)
    if version != 1:
        raise ValueError('当前只支持 version=1 的宏愿配置')
    goals = data.get('goals', [])
    if not isinstance(goals, list):
        raise ValueError('goals 必须是数组')
    if len(goals) > MAX_GOAL_COUNT:
        raise ValueError(f'最多配置 {MAX_GOAL_COUNT} 个宏愿')

    normalized_goals = []
    goal_ids = set()
    for index, raw_goal in enumerate(goals, start=1):
        if not isinstance(raw_goal, dict):
            raise ValueError(f'第 {index} 个宏愿必须是 JSON 对象')
        goal_id = str(raw_goal.get('id') or '').strip()
        if not GOAL_ID_PATTERN.fullmatch(goal_id):
            raise ValueError(
                f'第 {index} 个宏愿的 id 必须以字母或数字开头，且只能包含字母、数字、下划线和连字符'
            )
        if goal_id in goal_ids:
            raise ValueError(f'宏愿 id 重复：{goal_id}')
        goal_ids.add(goal_id)

        title = str(raw_goal.get('title') or '').strip()
        if not title:
            raise ValueError(f'第 {index} 个宏愿缺少标题')
        if len(title) > 100:
            raise ValueError(f'第 {index} 个宏愿标题最多 100 个字符')
        description = str(raw_goal.get('description') or '').strip()
        if len(description) > 500:
            raise ValueError(f'第 {index} 个宏愿说明最多 500 个字符')
        target_count = raw_goal.get('target_count')
        if type(target_count) is not int or not 1 <= target_count <= MAX_GOAL_TARGET_COUNT:
            raise ValueError(
                f'第 {index} 个宏愿的目标数必须是 1～{MAX_GOAL_TARGET_COUNT} 之间的整数'
            )
        deadline = str(raw_goal.get('deadline') or '').strip()
        if deadline:
            try:
                datetime.strptime(deadline, '%Y-%m-%d')
            except ValueError as exc:
                raise ValueError(f'第 {index} 个宏愿的截止日期必须是 YYYY-MM-DD') from exc

        task_category_ids = _normalize_goal_category_ids(
            raw_goal.get('task_category_ids', []), 'task_category_ids'
        )
        achievement_category_ids = _normalize_goal_category_ids(
            raw_goal.get('achievement_category_ids', []), 'achievement_category_ids'
        )
        if not task_category_ids and not achievement_category_ids:
            raise ValueError(f'第 {index} 个宏愿至少要映射一个任务或成就分类')
        normalized_goals.append({
            'id': goal_id,
            'title': title,
            'description': description,
            'target_count': target_count,
            'deadline': deadline,
            'task_category_ids': task_category_ids,
            'achievement_category_ids': achievement_category_ids,
        })
    return {'version': 1, 'goals': normalized_goals}


def load_goal_config():
    empty = {'version': 1, 'goals': []}
    try:
        with open(GOAL_CONFIG_PATH, 'r', encoding='utf-8') as handle:
            raw = json.load(handle)
        return normalize_goal_config(raw), '', str(raw.get('updated_at') or '')
    except FileNotFoundError:
        return empty, '', ''
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return empty, '宏愿配置文件无法读取或格式不正确，请重新保存配置。', ''


def save_goal_config(config):
    normalized = normalize_goal_config(config)
    saved_at = datetime.now().astimezone().isoformat(timespec='seconds')
    payload = dict(normalized)
    payload['updated_at'] = saved_at
    directory = os.path.dirname(os.path.abspath(GOAL_CONFIG_PATH))
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix='.lifeup-goals-', suffix='.tmp', dir=directory
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, GOAL_CONFIG_PATH)
        temporary_path = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return normalized, saved_at


def _goal_local_only_response():
    return jsonify({
        'code': 'GOAL_MAPPING_LOCAL_ONLY',
        'error': '真实宏愿映射当前只读取本地备份，不能在手机云人升模式下使用。',
        'suggestion': '请先把顶部数据源切换为“本地备份”。',
    }), 403


def _goal_record(kind, row, category_name):
    completed = (
        is_task_completed_status(row.get('status'))
        if kind == 'task'
        else int_value(row.get('status'), 0) >= 1
    )
    updated_ms = int_value(row.get('updatedtime'), 0)
    return {
        'kind': kind,
        'id': row.get('id'),
        'name': row.get('name') or '-',
        'category_id': row.get('categoryid'),
        'category_name': category_name,
        'completed': completed,
        'status': 'completed' if completed else 'active',
        'status_label': '已完成' if completed else '进行中',
        'updated_at': updated_ms,
        'updated_date': ms_to_date(updated_ms),
    }


def local_goal_overview():
    config, config_error, config_updated_at = load_goal_config()
    connection = get_db()
    try:
        cursor = connection.cursor()
        cursor.execute("""
            SELECT id, categoryname AS name
            FROM categorymodel
            WHERE isdelete=0
            ORDER BY orderincategory, id
        """)
        task_categories = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""
            SELECT id, categoryname AS name
            FROM userachcategorymodel
            WHERE isdelete=0
            ORDER BY orderincategory, id
        """)
        achievement_categories = [dict(row) for row in cursor.fetchall()]
        task_category_names = {row['id']: row['name'] for row in task_categories}
        achievement_category_names = {
            row['id']: row['name'] for row in achievement_categories
        }

        cursor.execute("""
            SELECT id, content AS name, categoryid, taskstatus AS status,
                   COALESCE(updatedtime, createdtime, 0) AS updatedtime
            FROM taskmodel
            WHERE isdeleterecord=0
        """)
        task_rows = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""
            SELECT id, content AS name, categoryid, achievementstatus AS status,
                   COALESCE(finishtime, updatetime, createtime, 0) AS updatedtime
            FROM userachievementmodel
            WHERE isdelete=0
        """)
        achievement_rows = [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()

    recent_cutoff = now_ms() - 30 * 24 * 60 * 60 * 1000
    computed_goals = []
    for goal in config['goals']:
        active_task_ids = {
            category_id for category_id in goal['task_category_ids']
            if category_id in task_category_names
        }
        active_achievement_ids = {
            category_id for category_id in goal['achievement_category_ids']
            if category_id in achievement_category_names
        }
        missing_mappings = [
            {'kind': 'task_category', 'id': category_id}
            for category_id in goal['task_category_ids']
            if category_id not in task_category_names
        ] + [
            {'kind': 'achievement_category', 'id': category_id}
            for category_id in goal['achievement_category_ids']
            if category_id not in achievement_category_names
        ]
        related_records = [
            _goal_record('task', row, task_category_names[row['categoryid']])
            for row in task_rows if row.get('categoryid') in active_task_ids
        ] + [
            _goal_record(
                'achievement', row, achievement_category_names[row['categoryid']]
            )
            for row in achievement_rows
            if row.get('categoryid') in active_achievement_ids
        ]
        related_records.sort(
            key=lambda row: (row['updated_at'], row['kind'], row['id'] or 0),
            reverse=True,
        )
        completed_count = sum(1 for row in related_records if row['completed'])
        recent_count = sum(
            1 for row in related_records
            if row['completed'] and row['updated_at'] >= recent_cutoff
        )
        computed = dict(goal)
        computed.update({
            'current_count': completed_count,
            'completed_count': completed_count,
            'related_count': len(related_records),
            'recent_count': recent_count,
            'recent_days': 30,
            'progress': round(
                min(completed_count / goal['target_count'] * 100, 100), 1
            ),
            'mapped_categories': {
                'tasks': [
                    {'id': category_id, 'name': task_category_names[category_id]}
                    for category_id in goal['task_category_ids']
                    if category_id in task_category_names
                ],
                'achievements': [
                    {
                        'id': category_id,
                        'name': achievement_category_names[category_id],
                    }
                    for category_id in goal['achievement_category_ids']
                    if category_id in achievement_category_names
                ],
            },
            'missing_mappings': missing_mappings,
            'related_records': related_records,
        })
        computed_goals.append(computed)

    meta = overview_meta('local')
    meta.update({
        'config_source': os.path.basename(GOAL_CONFIG_PATH),
        'config_updated_at': config_updated_at,
    })
    return {
        'meta': meta,
        'configured': bool(config['goals']) and not config_error,
        'config': config,
        'config_error': config_error,
        'category_options': {
            'tasks': task_categories,
            'achievements': achievement_categories,
        },
        'goals': computed_goals,
    }


REVIEW_METRICS = {
    'focus_minutes': ('番茄专注时长', '分钟'),
    'tasks_completed': ('完成任务数', '条'),
    'coin_change': ('金币净变化', '金币'),
    'exp_change': ('经验净变化', '经验'),
    'achievements_completed': ('完成成就数', '项'),
}


def review_reference_now():
    return datetime.now()


def review_period_window(period, reference=None):
    reference = reference or review_reference_now()
    today = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'week':
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
        previous_start = start - timedelta(days=7)
        label, comparison_label = '本周', '上周'
    elif period == 'month':
        start = today.replace(day=1)
        end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)
        previous_end = start
        previous_start = datetime(
            previous_end.year - (previous_end.month == 1),
            (previous_end.month - 2) % 12 + 1,
            1,
        )
        label, comparison_label = '本月', '上月'
    elif period == 'year':
        start = datetime(today.year, 1, 1)
        end = datetime(today.year + 1, 1, 1)
        previous_start = datetime(today.year - 1, 1, 1)
        label, comparison_label = '本年', '上年'
    else:
        raise ValueError('复盘周期只能是 week、month 或 year')
    if period != 'month':
        previous_end = start
    return {
        'period': period,
        'label': label,
        'comparison_label': comparison_label,
        '_start': start,
        '_end': end,
        '_previous_start': previous_start,
        '_previous_end': previous_end,
        'start': start.strftime('%Y-%m-%d'),
        'end': (end - timedelta(days=1)).strftime('%Y-%m-%d'),
        'previous_start': previous_start.strftime('%Y-%m-%d'),
        'previous_end': (previous_end - timedelta(days=1)).strftime('%Y-%m-%d'),
        'timezone_label': '电脑本地时区',
    }


def review_datetime(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and not value.strip().lstrip('-').isdigit():
        try:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except ValueError:
            return None
    numeric = number_value(value, 0)
    if numeric <= 0:
        return None
    try:
        return datetime.fromtimestamp(numeric / 1000 if numeric > 100_000_000_000 else numeric)
    except (OSError, OverflowError, ValueError):
        return None


def review_event(kind, row_id, name, timestamp, value, detail, source_label):
    occurred_at = review_datetime(timestamp)
    if occurred_at is None:
        return None
    return {
        'kind': kind,
        'id': row_id,
        'name': str(name or '未命名记录'),
        'value': number_value(value, 0),
        'detail': str(detail or ''),
        'source_label': source_label,
        '_datetime': occurred_at,
    }


def public_review_record(record):
    occurred_at = record['_datetime']
    value = record.get('value', 0)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return {
        key: value_ for key, value_ in record.items() if not key.startswith('_')
    } | {
        'value': value,
        'date': occurred_at.strftime('%Y-%m-%d'),
        'time': occurred_at.strftime('%Y-%m-%d %H:%M:%S'),
    }


def review_metric(key, events, window, available=True, missing_reason=''):
    label, unit = REVIEW_METRICS[key]
    if not available:
        return {
            'label': label, 'unit': unit, 'available': False,
            'value': None, 'previous_value': None, 'delta': None,
            'record_count': 0, 'previous_record_count': 0,
            'current_records': [], 'previous_records': [],
            'source_label': '', 'missing_reason': missing_reason,
        }
    current = [
        row for row in events
        if window['_start'] <= row['_datetime'] < window['_end']
    ]
    previous = [
        row for row in events
        if window['_previous_start'] <= row['_datetime'] < window['_previous_end']
    ]
    current.sort(key=lambda row: row['_datetime'], reverse=True)
    previous.sort(key=lambda row: row['_datetime'], reverse=True)
    value = sum(row['value'] for row in current)
    previous_value = sum(row['value'] for row in previous)
    for number_name, number in (('value', value), ('previous_value', previous_value)):
        if isinstance(number, float) and number.is_integer():
            if number_name == 'value':
                value = int(number)
            else:
                previous_value = int(number)
    return {
        'label': label,
        'unit': unit,
        'available': True,
        'value': value,
        'previous_value': previous_value,
        'delta': value - previous_value,
        'record_count': len(current),
        'previous_record_count': len(previous),
        'current_records': [public_review_record(row) for row in current],
        'previous_records': [public_review_record(row) for row in previous],
        'source_label': current[0]['source_label'] if current else (
            previous[0]['source_label'] if previous else '真实记录'
        ),
        'missing_reason': '',
    }


def review_series(period, events_by_metric, window, available):
    points = []
    cursor = window['_start']
    while cursor < window['_end']:
        if period == 'year':
            next_cursor = datetime(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
            label = f'{cursor.month}月'
        else:
            next_cursor = cursor + timedelta(days=1)
            label = f'{cursor.month}/{cursor.day}'
        point = {'label': label, 'date': cursor.strftime('%Y-%m-%d')}
        for key in REVIEW_METRICS:
            if not available.get(key, True):
                point[key] = None
                continue
            total = sum(
                row['value'] for row in events_by_metric.get(key, [])
                if cursor <= row['_datetime'] < next_cursor
            )
            point[key] = int(total) if isinstance(total, float) and total.is_integer() else total
        points.append(point)
        cursor = next_cursor
    return points


def build_review_overview(period, source, events_by_metric, available=None, missing=None, warnings=None):
    available = available or {}
    missing = missing or {}
    window = review_period_window(period)
    metrics = {
        key: review_metric(
            key, events_by_metric.get(key, []), window,
            available.get(key, True), missing.get(key, ''),
        )
        for key in REVIEW_METRICS
    }
    insights = []
    for key in ('focus_minutes', 'tasks_completed', 'achievements_completed'):
        metric = metrics[key]
        if not metric['available']:
            continue
        if metric['record_count'] == 0:
            text = f'{window["label"]}暂未记录{metric["label"]}。'
            icon = 'ℹ️'
        elif metric['delta'] > 0:
            text = f'{metric["label"]}比{window["comparison_label"]}增加 {metric["delta"]} {metric["unit"]}。'
            icon = '📈'
        elif metric['delta'] < 0:
            text = f'{metric["label"]}比{window["comparison_label"]}减少 {abs(metric["delta"])} {metric["unit"]}。'
            icon = '📉'
        else:
            text = f'{metric["label"]}与{window["comparison_label"]}持平。'
            icon = '➡️'
        insights.append({'icon': icon, 'text': text, 'metric': key})
    for key in ('coin_change', 'exp_change'):
        metric = metrics[key]
        if metric['available']:
            direction = '净增加' if metric['value'] > 0 else ('净减少' if metric['value'] < 0 else '没有净变化')
            amount = f' {abs(metric["value"])} {metric["unit"]}' if metric['value'] else ''
            insights.append({'icon': '💰' if key == 'coin_change' else '✨', 'text': f'{window["label"]}{metric["label"]}{direction}{amount}。', 'metric': key})
    gaps = [reason for reason in missing.values() if reason]
    gaps.extend(warnings or [])
    public_window = {key: value for key, value in window.items() if not key.startswith('_')}
    return {
        'meta': overview_meta(source, '；'.join(warnings or [])),
        'window': public_window,
        'metrics': metrics,
        'series': review_series(period, events_by_metric, window, available),
        'insights': insights,
        'gaps': list(dict.fromkeys(gaps)),
    }


def local_review_table_ready(conn, table, required_columns):
    found = {
        row['name'] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()
    }
    return set(required_columns).issubset(found)


def first_review_timestamp(row, keys):
    for key in keys:
        value = row.get(key)
        if review_datetime(value) is not None:
            return value
    return 0


def local_review_overview(period):
    conn = get_db()
    events = {key: [] for key in REVIEW_METRICS}
    available = {key: True for key in REVIEW_METRICS}
    missing = {}
    try:
        if local_review_table_ready(
            conn, 'taskmodel',
            ('id', 'content', 'taskstatus', 'isdeleterecord', 'updatedtime'),
        ):
            rows = conn.execute("""
                SELECT t.id, t.content, t.taskstatus, t.updatedtime, t.endtime,
                       t.createdtime, t.rewardcoin, t.expreward,
                       COALESCE(c.categoryname, '-') AS category_name
                FROM taskmodel t
                LEFT JOIN categorymodel c ON t.categoryid = c.id
                WHERE COALESCE(t.isdeleterecord, 0)=0 AND COALESCE(t.taskstatus, 0)=1
            """).fetchall()
            for raw in rows:
                row = dict(raw)
                event = review_event(
                    'task', row['id'], row['content'],
                    first_review_timestamp(row, ('updatedtime', 'endtime', 'createdtime')),
                    1,
                    f'{row["category_name"]}；任务标注奖励 {row.get("rewardcoin") or 0} 金币 / {row.get("expreward") or 0} 经验',
                    'taskmodel',
                )
                if event:
                    events['tasks_completed'].append(event)
        else:
            available['tasks_completed'] = False
            missing['tasks_completed'] = '当前备份缺少可识别的任务完成记录字段。'

        if local_review_table_ready(
            conn, 'tomatomodel',
            ('id', 'lasttime', 'starttime', 'isabandoned', 'isdel'),
        ):
            rows = conn.execute("""
                SELECT p.*, t.content AS task_name
                FROM tomatomodel p
                LEFT JOIN taskmodel t ON t.id = p.taskmodelid
                WHERE COALESCE(p.isdel, 0)=0 AND COALESCE(p.isabandoned, 0)=0
            """).fetchall()
            for raw in rows:
                row = dict(raw)
                merged = dict(row)
                if row.get('task_name'):
                    merged['taskName'] = row['task_name']
                normalized = normalize_focus_record(merged, 'local')
                if not normalized['completed']:
                    continue
                event = review_event(
                    'focus', row['id'], normalized['task_name'],
                    first_review_timestamp(row, ('starttime', 'endtime', 'createtime')),
                    normalized['duration'],
                    '已完成番茄记录',
                    'tomatomodel',
                )
                if event:
                    events['focus_minutes'].append(event)
        else:
            available['focus_minutes'] = False
            missing['focus_minutes'] = '当前备份缺少可识别的番茄记录字段。'

        if local_review_table_ready(
            conn, 'coinmodel', ('id', 'createtime', 'isdecrease', 'changedvalue'),
        ):
            rows = conn.execute("""
                SELECT id, createtime, isdecrease, changedvalue, content, rescode, relatedid
                FROM coinmodel
                WHERE COALESCE(changedvalue, 0)<>0
            """).fetchall()
            if not rows:
                available['coin_change'] = False
                missing['coin_change'] = '当前备份没有金币流水，无法计算所选周期的金币净变化。'
            for raw in rows:
                row = dict(raw)
                amount = abs(number_value(row['changedvalue'], 0))
                signed = -amount if int_value(row['isdecrease'], 0) else amount
                event = review_event(
                    'coin', row['id'], row.get('content') or '金币流水', row['createtime'], signed,
                    f'{"支出" if signed < 0 else "收入"}；来源码 {row.get("rescode")}',
                    'coinmodel',
                )
                if event:
                    events['coin_change'].append(event)
        else:
            available['coin_change'] = False
            missing['coin_change'] = '当前备份缺少金币流水表或必要字段。'

        if local_review_table_ready(
            conn, 'expmodel', ('id', 'createtime', 'isdecrease', 'value'),
        ):
            rows = conn.execute("""
                SELECT id, createtime, isdecrease, value, content, rescode, relatedid
                FROM expmodel
                WHERE COALESCE(value, 0)<>0
            """).fetchall()
            if not rows:
                available['exp_change'] = False
                missing['exp_change'] = '当前备份没有经验流水，无法计算所选周期的经验净变化。'
            for raw in rows:
                row = dict(raw)
                amount = abs(number_value(row['value'], 0))
                signed = -amount if int_value(row['isdecrease'], 0) else amount
                event = review_event(
                    'exp', row['id'], row.get('content') or '经验流水', row['createtime'], signed,
                    f'{"减少" if signed < 0 else "增加"}；来源码 {row.get("rescode")}',
                    'expmodel',
                )
                if event:
                    events['exp_change'].append(event)
        else:
            available['exp_change'] = False
            missing['exp_change'] = '当前备份缺少经验流水表或必要字段。'

        if local_review_table_ready(
            conn, 'userachievementmodel',
            ('id', 'content', 'achievementstatus', 'isdelete', 'finishtime'),
        ):
            rows = conn.execute("""
                SELECT a.id, a.content, a.finishtime, a.updatetime, a.createtime,
                       COALESCE(c.categoryname, '-') AS category_name
                FROM userachievementmodel a
                LEFT JOIN userachcategorymodel c ON a.categoryid = c.id
                WHERE COALESCE(a.isdelete, 0)=0 AND COALESCE(a.achievementstatus, 0)>=1
            """).fetchall()
            for raw in rows:
                row = dict(raw)
                event = review_event(
                    'achievement', row['id'], row['content'],
                    first_review_timestamp(row, ('finishtime', 'updatetime', 'createtime')),
                    1, row['category_name'], 'userachievementmodel',
                )
                if event:
                    events['achievements_completed'].append(event)
        else:
            available['achievements_completed'] = False
            missing['achievements_completed'] = '当前备份缺少可识别的成就完成记录字段。'
    finally:
        conn.close()
    return build_review_overview(period, 'local', events, available, missing)


def cloud_review_overview(period):
    events = {key: [] for key in REVIEW_METRICS}
    available = {key: True for key in REVIEW_METRICS}
    missing = {
        'coin_change': '手机云人升只提供当前金币余额，没有金币历史流水，无法计算周期净变化。',
        'exp_change': '手机云人升只提供当前技能经验快照，没有经验历史流水，无法计算周期净变化。',
    }
    available['coin_change'] = False
    available['exp_change'] = False
    warnings = []

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            'tasks': pool.submit(cloud_request, {}, '/tasks', 30),
            'focus': pool.submit(cloud_request, {}, '/pomodoro_records', 30),
            'achievement_categories': pool.submit(cloud_request, {}, '/achievement_categories', 20),
        }
        datasets = {}
        for key, future in futures.items():
            try:
                datasets[key] = [
                    row for row in as_cloud_rows(future.result()['data'])
                    if isinstance(row, dict)
                ]
            except Exception as exc:
                datasets[key] = []
                warnings.append(f'{key} 读取失败：{exc}')

        category_rows = datasets['achievement_categories']
        achievement_futures = {
            row.get('id'): pool.submit(
                cloud_request, {}, f'/achievements/{row.get("id")}', 20
            )
            for row in category_rows if row.get('id') is not None
        }
        achievement_rows = []
        for category_id, future in achievement_futures.items():
            try:
                for row in as_cloud_rows(future.result()['data']):
                    if isinstance(row, dict):
                        copied = dict(row)
                        copied.setdefault('categoryId', category_id)
                        achievement_rows.append(copied)
            except Exception as exc:
                warnings.append(f'成就分类 {category_id} 读取失败：{exc}')

    if any(text.startswith('tasks ') for text in warnings):
        available['tasks_completed'] = False
        missing['tasks_completed'] = '手机任务记录读取失败，无法计算完成任务数。'
    else:
        missing_task_time = 0
        for row in datasets['tasks']:
            if not is_task_completed_status(
                first_value(row, ['status', 'taskstatus'], TASK_STATUS_PENDING)
            ):
                continue
            timestamp = first_review_timestamp(row, [
                'finishedTime', 'finishTime', 'completedTime', 'completionTime',
                'updatedTime', 'updatetime', 'endTime', 'endtime',
            ])
            event = review_event(
                'task', row.get('id'), first_value(row, ['nameExtended', 'name', 'todo', 'content'], '手机任务'),
                timestamp, 1,
                f'分类 ID {first_value(row, ["categoryId", "categoryid"], "-")}；手机只读任务记录',
                '/tasks',
            )
            if event:
                events['tasks_completed'].append(event)
            else:
                missing_task_time += 1
        if missing_task_time:
            warnings.append(f'{missing_task_time} 条已完成手机任务缺少完成时间，未计入周期统计。')

    if any(text.startswith('focus ') for text in warnings):
        available['focus_minutes'] = False
        missing['focus_minutes'] = '手机番茄记录读取失败，无法计算专注时长。'
    else:
        for row in datasets['focus']:
            normalized = normalize_focus_record(row, 'cloud')
            if not normalized['completed']:
                continue
            event = review_event(
                'focus', row.get('id'), normalized['task_name'],
                first_review_timestamp(row, [
                    'startTime', 'starttime', 'endTime', 'endtime', 'createtime', 'createdTime',
                ]),
                normalized['duration'], '已完成手机番茄记录', '/pomodoro_records',
            )
            if event:
                events['focus_minutes'].append(event)

    if not category_rows and any(text.startswith('achievement_categories ') for text in warnings):
        available['achievements_completed'] = False
        missing['achievements_completed'] = '手机成就分类读取失败，无法计算完成成就数。'
    else:
        category_names = {
            row.get('id'): first_value(row, ['name', 'categoryName'], '-')
            for row in category_rows
        }
        missing_achievement_time = 0
        for row in achievement_rows:
            if int_value(first_value(row, ['status', 'achievementstatus'], 0), 0) < 1:
                continue
            category_id = first_value(row, ['categoryId', 'categoryid'], 0)
            event = review_event(
                'achievement', row.get('id'), first_value(row, ['name', 'title', 'content'], '手机成就'),
                first_review_timestamp(row, [
                    'finishTime', 'finishedTime', 'completedTime', 'updatetime', 'updatedTime',
                ]),
                1, category_names.get(category_id, f'分类 {category_id}'),
                f'/achievements/{category_id}',
            )
            if event:
                events['achievements_completed'].append(event)
            else:
                missing_achievement_time += 1
        if missing_achievement_time:
            warnings.append(f'{missing_achievement_time} 条已完成手机成就缺少完成时间，未计入周期统计。')

    return build_review_overview(period, 'cloud', events, available, missing, warnings)


ACTIVITY_METRICS = {
    'tasks_completed': ('完成任务数', '条'),
    'focus_minutes': ('番茄专注时长', '分钟'),
}


def activity_period_window(period, reference=None):
    reference = reference or review_reference_now()
    today = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'day':
        start, end, label = today, today + timedelta(days=1), '今日'
    elif period == 'week':
        start = today - timedelta(days=today.weekday())
        end, label = start + timedelta(days=7), '本周'
    elif period == 'month':
        start = today.replace(day=1)
        end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)
        label = '本月'
    else:
        raise ValueError('热力图范围只能是 day、week 或 month')
    return {
        'period': period,
        'label': label,
        '_start': start,
        '_end': end,
        '_reference_date': today.date(),
        'start': start.strftime('%Y-%m-%d'),
        'end': (end - timedelta(days=1)).strftime('%Y-%m-%d'),
        'timezone_label': '电脑本地时区',
    }


def activity_streak(series, key, reference_date):
    eligible = [
        row for row in series
        if datetime.strptime(row['date'], '%Y-%m-%d').date() <= reference_date
    ]
    longest = running = 0
    for row in eligible:
        if number_value(row.get(key), 0) > 0:
            running += 1
            longest = max(longest, running)
        else:
            running = 0
    current = 0
    for row in reversed(eligible):
        if number_value(row.get(key), 0) <= 0:
            break
        current += 1
    return {'current': current, 'longest': longest}


def build_activity_overview(period, source, events, available=None, missing=None, warnings=None):
    available = available or {key: True for key in ACTIVITY_METRICS}
    missing = missing or {}
    warnings = warnings or []
    window = activity_period_window(period)
    selected = {
        key: sorted(
            [row for row in events.get(key, []) if window['_start'] <= row['_datetime'] < window['_end']],
            key=lambda row: row['_datetime'],
            reverse=True,
        )
        for key in ACTIVITY_METRICS
    }
    metrics = {}
    for key, (label, unit) in ACTIVITY_METRICS.items():
        is_available = available.get(key, True)
        rows = selected[key] if is_available else []
        value = sum(row['value'] for row in rows) if is_available else None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        metrics[key] = {
            'label': label,
            'unit': unit,
            'available': is_available,
            'value': value,
            'record_count': len(rows),
            'missing_reason': '' if is_available else missing.get(key, ''),
        }

    series = []
    cursor = window['_start']
    while cursor < window['_end']:
        next_cursor = cursor + timedelta(days=1)
        point = {
            'label': f'{cursor.month}/{cursor.day}',
            'date': cursor.strftime('%Y-%m-%d'),
        }
        for key in ACTIVITY_METRICS:
            if not available.get(key, True):
                point[key] = None
                continue
            value = sum(
                row['value'] for row in events.get(key, [])
                if cursor <= row['_datetime'] < next_cursor
            )
            point[key] = int(value) if isinstance(value, float) and value.is_integer() else value
        point['active'] = (
            number_value(point.get('tasks_completed'), 0) > 0
            or number_value(point.get('focus_minutes'), 0) > 0
        )
        series.append(point)
        cursor = next_cursor

    streak_series = [
        dict(row, active_value=1 if row['active'] else 0)
        for row in series
    ]
    streaks = {
        'tasks': activity_streak(streak_series, 'tasks_completed', window['_reference_date']),
        'focus': activity_streak(streak_series, 'focus_minutes', window['_reference_date']),
        'active': activity_streak(streak_series, 'active_value', window['_reference_date']),
    }
    public_window = {key: value for key, value in window.items() if not key.startswith('_')}
    return {
        'meta': overview_meta(source, '；'.join(warnings)),
        'window': public_window,
        'metrics': metrics,
        'series': series,
        'streaks': streaks,
        'records': {
            'tasks': [public_review_record(row) for row in selected['tasks_completed']],
            'focus': [public_review_record(row) for row in selected['focus_minutes']],
        },
        'gaps': list(dict.fromkeys([
            *[reason for reason in missing.values() if reason],
            *warnings,
        ])),
    }


def local_activity_overview(period):
    events = {key: [] for key in ACTIVITY_METRICS}
    available = {key: True for key in ACTIVITY_METRICS}
    missing = {}
    conn = get_db()
    try:
        task_columns = {
            row['name'] for row in conn.execute('PRAGMA table_info(taskmodel)').fetchall()
        }
        task_base = {'id', 'content', 'taskstatus', 'isdeleterecord'}
        task_time_fields = task_columns.intersection({'updatedtime', 'endtime', 'createdtime'})
        if task_base.issubset(task_columns) and task_time_fields:
            category_names = {}
            if 'categoryid' in task_columns and local_review_table_ready(
                conn, 'categorymodel', ('id', 'categoryname')
            ):
                category_names = {
                    row['id']: row['categoryname']
                    for row in conn.execute('SELECT id, categoryname FROM categorymodel').fetchall()
                }
            for raw in conn.execute('SELECT * FROM taskmodel').fetchall():
                row = dict(raw)
                if int_value(row.get('isdeleterecord'), 0) or not is_task_completed_status(
                    row.get('taskstatus')
                ):
                    continue
                category_name = category_names.get(row.get('categoryid'), '-')
                event = review_event(
                    'task', row.get('id'), row.get('content'),
                    first_review_timestamp(row, ('updatedtime', 'endtime', 'createdtime')),
                    1, category_name, 'taskmodel',
                )
                if event:
                    events['tasks_completed'].append(event)
        else:
            available['tasks_completed'] = False
            missing['tasks_completed'] = '当前备份缺少可识别的任务完成时间字段，未生成猜测数据。'

        focus_columns = {
            row['name'] for row in conn.execute('PRAGMA table_info(tomatomodel)').fetchall()
        }
        focus_base = {'id', 'isabandoned', 'isdel'}
        focus_time_fields = focus_columns.intersection(
            {'starttime', 'endtime', 'createtime'}
        )
        has_duration = bool(focus_columns.intersection({'lasttime', 'duration', 'minutes'}))
        can_derive_duration = {'starttime', 'endtime'}.issubset(focus_columns)
        if focus_base.issubset(focus_columns) and focus_time_fields and (has_duration or can_derive_duration):
            task_names = {}
            if 'taskmodelid' in focus_columns and {'id', 'content'}.issubset(task_columns):
                task_names = {
                    row['id']: row['content']
                    for row in conn.execute('SELECT id, content FROM taskmodel').fetchall()
                }
            for raw in conn.execute('SELECT * FROM tomatomodel').fetchall():
                row = dict(raw)
                if int_value(row.get('isdel'), 0) or int_value(row.get('isabandoned'), 0):
                    continue
                merged = dict(row)
                if task_names.get(row.get('taskmodelid')):
                    merged['taskName'] = task_names[row.get('taskmodelid')]
                normalized = normalize_focus_record(merged, 'local')
                if not normalized['completed']:
                    continue
                event = review_event(
                    'focus', row.get('id'), normalized['task_name'],
                    first_review_timestamp(row, ('starttime', 'endtime', 'createtime')),
                    normalized['duration'], '已完成番茄记录', 'tomatomodel',
                )
                if event:
                    events['focus_minutes'].append(event)
        else:
            available['focus_minutes'] = False
            missing['focus_minutes'] = '当前备份缺少可识别的番茄时间或时长字段，未生成猜测数据。'
    finally:
        conn.close()
    return build_activity_overview(period, 'local', events, available, missing)


def cloud_activity_overview(period):
    events = {key: [] for key in ACTIVITY_METRICS}
    available = {key: True for key in ACTIVITY_METRICS}
    missing = {}
    warnings = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            'tasks': pool.submit(cloud_request, {}, '/tasks', 30),
            'focus': pool.submit(cloud_request, {}, '/pomodoro_records', 30),
        }
        datasets = {}
        for key, future in futures.items():
            try:
                datasets[key] = [
                    row for row in as_cloud_rows(future.result()['data'])
                    if isinstance(row, dict)
                ]
            except Exception as exc:
                datasets[key] = []
                available['tasks_completed' if key == 'tasks' else 'focus_minutes'] = False
                missing['tasks_completed' if key == 'tasks' else 'focus_minutes'] = (
                    f'手机{"任务" if key == "tasks" else "番茄"}记录读取失败：{exc}'
                )

    missing_task_time = 0
    if available['tasks_completed']:
        for row in datasets['tasks']:
            if not is_task_completed_status(
                first_value(row, ['status', 'taskstatus'], TASK_STATUS_PENDING)
            ):
                continue
            timestamp = first_review_timestamp(row, [
                'finishedTime', 'finishTime', 'completedTime', 'completionTime',
                'updatedTime', 'updatetime', 'endTime', 'endtime',
            ])
            event = review_event(
                'task', row.get('id'), first_value(row, ['nameExtended', 'name', 'todo', 'content'], '手机任务'),
                timestamp, 1,
                f'分类 ID {first_value(row, ["categoryId", "categoryid"], "-")}',
                '/tasks',
            )
            if event:
                events['tasks_completed'].append(event)
            else:
                missing_task_time += 1
        if missing_task_time:
            warnings.append(f'{missing_task_time} 条已完成手机任务缺少完成时间，未计入热力图。')

    missing_focus_fields = 0
    if available['focus_minutes']:
        for row in datasets['focus']:
            normalized = normalize_focus_record(row, 'cloud')
            timestamp = first_review_timestamp(row, [
                'startTime', 'starttime', 'beginTime', 'begintime',
                'endTime', 'endtime', 'createdTime', 'createtime', 'date',
            ])
            if not normalized['completed'] or not timestamp:
                missing_focus_fields += 1
                continue
            event = review_event(
                'focus', row.get('id'), normalized['task_name'], timestamp,
                normalized['duration'], '已完成手机番茄记录', '/pomodoro_records',
            )
            if event:
                events['focus_minutes'].append(event)
        if missing_focus_fields:
            warnings.append(f'{missing_focus_fields} 条手机番茄缺少可识别时间或时长，未计入热力图。')
    return build_activity_overview(period, 'cloud', events, available, missing, warnings)


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
        rows = [row for row in rows if is_task_pending_status(row.get('status'))]
    elif filter_type == 'done':
        rows = [row for row in rows if is_task_completed_status(row.get('status'))]
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


ACHIEVEMENT_TYPE_NORMAL = 0
ACHIEVEMENT_TYPE_SUBCATEGORY = 1


def _annotate_achievement_subcategories(rows):
    """按 LifeUp 原生顺序标记子分类，以及紧随其后的成员成就。"""
    annotated = [dict(row) for row in rows]
    current_subcategory = {}
    child_counts = {}
    for row in annotated:
        category_id = row.get('categoryid', row.get('categoryId', 0))
        try:
            row_type = int(row.get('type', ACHIEVEMENT_TYPE_NORMAL) or 0)
        except (TypeError, ValueError):
            row_type = ACHIEVEMENT_TYPE_NORMAL
        if row_type == ACHIEVEMENT_TYPE_SUBCATEGORY:
            current_subcategory[category_id] = row
            child_counts[row.get('id')] = 0
            row['record_kind'] = 'subcategory'
            row['subcategory_id'] = None
            row['subcategory_name'] = None
        else:
            header = current_subcategory.get(category_id)
            row['record_kind'] = 'achievement'
            row['subcategory_id'] = header.get('id') if header else None
            row['subcategory_name'] = header.get('name') if header else None
            if header:
                child_counts[header.get('id')] = child_counts.get(header.get('id'), 0) + 1
    for row in annotated:
        row['subcategory_child_count'] = (
            child_counts.get(row.get('id'), 0)
            if row.get('record_kind') == 'subcategory'
            else None
        )
    return annotated


def _filter_annotated_achievements(rows, search='', category_id=''):
    filtered_by_category = rows
    if str(category_id).strip():
        wanted = str(category_id).strip()
        filtered_by_category = [
            row for row in rows
            if str(row.get('categoryid', row.get('categoryId', ''))) == wanted
        ]
    query = str(search or '').strip().casefold()
    if not query:
        return filtered_by_category
    directly_matched = {
        row.get('id') for row in filtered_by_category
        if query in str(row.get('name') or '').casefold()
        or query in str(row.get('description') or '').casefold()
    }
    matched_subcategories = {
        row.get('id') for row in filtered_by_category
        if row.get('record_kind') == 'subcategory' and row.get('id') in directly_matched
    }
    needed_headers = {
        row.get('subcategory_id') for row in filtered_by_category
        if row.get('id') in directly_matched and row.get('subcategory_id') is not None
    }
    return [
        row for row in filtered_by_category
        if row.get('id') in directly_matched
        or row.get('id') in needed_headers
        or row.get('subcategory_id') in matched_subcategories
    ]


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
    achievements = []
    for row in all_rows:
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
            'orderincategory': first_value(row, ['order', 'orderincategory'], 0),
            'source': 'cloud',
            'read_only': True
        })
    achievements.sort(key=lambda row: (
        row.get('categoryid', 0), row.get('orderincategory', 0), row.get('id') or 0
    ))
    achievements = _filter_annotated_achievements(
        _annotate_achievement_subcategories(achievements), search, cat_id
    )
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
        cur.execute("SELECT COUNT(*) FROM taskmodel WHERE isdeleterecord=0 AND taskstatus=1")
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
        if is_task_completed_status(status):
            task_done += 1
        elif is_task_pending_status(status):
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


@app.route('/api/workspace-cleanup/preview', methods=['POST'])
def api_workspace_cleanup_preview():
    data = request.get_json(silent=True)
    if data is not None and not isinstance(data, dict):
        return jsonify({
            'code': 'INVALID_CLEANUP_REQUEST',
            'error': '清理预览请求必须是 JSON 对象',
            'suggestion': '请刷新维护页面后重试。',
        }), 400
    return jsonify(create_workspace_cleanup_preview())


@app.route('/api/workspace-cleanup/execute', methods=['POST'])
def api_workspace_cleanup_execute():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({
            'code': 'INVALID_CLEANUP_REQUEST',
            'error': '清理请求必须是 JSON 对象',
            'suggestion': '请重新生成预览并勾选要删除的文件。',
        }), 400
    try:
        result = execute_workspace_cleanup(
            data.get('preview_token'), data.get('item_ids')
        )
        return jsonify(result)
    except ValueError as exc:
        if str(exc) == 'CLEANUP_PREVIEW_EXPIRED':
            return jsonify({
                'code': 'CLEANUP_PREVIEW_EXPIRED',
                'error': '清理预览已失效或已使用',
                'suggestion': '请重新扫描，再勾选并确认删除。',
            }), 409
        return jsonify({
            'code': 'INVALID_CLEANUP_SELECTION',
            'error': '清理选择无效',
            'suggestion': '只能删除当前预览中勾选的项目，请重新扫描。',
        }), 400


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


@app.route('/api/goals')
def goals_overview():
    if request_data_source() == 'cloud':
        return _goal_local_only_response()
    try:
        return jsonify(local_goal_overview())
    except Exception as exc:
        return jsonify({
            'code': 'GOAL_OVERVIEW_FAILED',
            'error': f'读取真实宏愿失败：{exc}',
            'suggestion': '请确认已载入有效的 LifeUp 本地备份。',
        }), 500


@app.route('/api/goals/config', methods=['POST'])
def goals_config():
    if request_data_source() == 'cloud':
        return _goal_local_only_response()
    data = request.get_json(silent=True)
    try:
        normalized, saved_at = save_goal_config(data)
        return jsonify({
            'ok': True,
            'config': normalized,
            'config_source': os.path.basename(GOAL_CONFIG_PATH),
            'updated_at': saved_at,
        })
    except ValueError as exc:
        return jsonify({
            'code': 'GOAL_CONFIG_INVALID',
            'error': str(exc),
            'suggestion': '请检查标题、目标数、日期和分类映射后重试。',
        }), 400
    except OSError as exc:
        return jsonify({
            'code': 'GOAL_CONFIG_SAVE_FAILED',
            'error': f'保存宏愿配置失败：{exc}',
            'suggestion': '请检查项目目录是否可写，然后重试。',
        }), 500


@app.route('/api/review')
def review_overview():
    period = request.args.get('period', 'week')
    if period not in ('week', 'month', 'year'):
        return jsonify({
            'code': 'REVIEW_PERIOD_INVALID',
            'error': '复盘周期只能是 week、month 或 year',
            'suggestion': '请重新选择本周、本月或本年。',
        }), 400
    source = request_data_source()
    try:
        if source == 'cloud':
            return jsonify(cloud_review_overview(period))
        return jsonify(local_review_overview(period))
    except Exception as exc:
        return jsonify({
            'code': 'REVIEW_OVERVIEW_FAILED',
            'error': f'读取真实复盘失败：{exc}',
            'suggestion': '请确认数据源可用后重试；失败不会修改备份或手机数据。',
        }), 500


@app.route('/api/activity/heatmap')
def activity_heatmap():
    period = request.args.get('period', 'month')
    if period not in ('day', 'week', 'month'):
        return jsonify({
            'code': 'ACTIVITY_PERIOD_INVALID',
            'error': '热力图范围只能是 day、week 或 month',
            'suggestion': '请重新选择今日、本周或本月。',
        }), 400
    source = request_data_source()
    try:
        if source == 'cloud':
            return jsonify(cloud_activity_overview(period))
        return jsonify(local_activity_overview(period))
    except Exception as exc:
        return jsonify({
            'code': 'ACTIVITY_HEATMAP_FAILED',
            'error': f'读取真实日常与番茄热力图失败：{exc}',
            'suggestion': '请确认数据源可用后重试；失败不会修改备份或手机数据。',
        }), 500


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
        status_cond = {'all': '1=1', 'active': 'taskstatus=0', 'done': 'taskstatus=1', 'frozen': 't1.isfrozen=1'}.get(filter_type, '1=1')
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
                   t1.extrainfo, t1.enableebbinghausmode, t1.ishandleoverdue,
                   t1.tasktargetid
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
            t['target_count'] = targets.get(t.pop('tasktargetid', None), 1)
            # 技能关联
            linked_skills = skill_links.get(t['id'], [])
            t['skill_ids'] = linked_skills
            t['skill_names'] = [skill_names.get(sid, '?') for sid in linked_skills]
            # 解析 extrainfo JSON
            try:
                t['extrainfo_obj'] = json.loads(t.get('extrainfo') or '{}')
            except (TypeError, json.JSONDecodeError):
                t['extrainfo_obj'] = {}
            # 商品奖励
            t['item_rewards'] = reward_links.get(t['id'], [])

        return jsonify(tasks)
    finally:
        conn.close()

def _insert_task_with_cursor(cur, data):
    """Insert one already validated task without committing the connection."""
    now = now_ms()
    target_times = data.get('target_count', 1)
    cur.execute(
        'INSERT INTO tasktargetmodel '
        '(targettimes, extraexpreward, repeatendinclusive, repeatendmode, repeatendbehavior) '
        'VALUES (?, 0, 1, 0, 0)',
        (target_times,),
    )
    target_id = cur.lastrowid

    st = data.get('start_time', '')
    et = data.get('end_time', '')
    if st and et:
        try:
            if int(et) - int(st) < 86400000:
                et = str(int(st) + 86400000)
        except (TypeError, ValueError):
            pass

    extrainfo = json.dumps({
        'autoUseItems': bool(data.get('auto_use_items', False)),
        'coinPunishmentFactor': float(data.get('coin_punishment_factor', 0)),
        'expPunishmentFactor': float(data.get('exp_punishment_factor', 0)),
        't_f_m': 1,
        'writeFeelings': False,
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
            ?, ?, ?, ?, ?,
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
        int(bool(data.get('is_frozen', data.get('isfrozen', False)))),
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
        '',
    ))
    new_id = cur.lastrowid

    skill_ids = data.get('skill_ids', [])
    if isinstance(skill_ids, str):
        skill_ids = json.loads(skill_ids)
    for skill_id in skill_ids:
        cur.execute(
            'INSERT INTO taskmodel_skillids (taskmodel_id, skillids) VALUES (?, ?)',
            (new_id, int(skill_id)),
        )

    item_rewards = data.get('item_rewards', [])
    if isinstance(item_rewards, str):
        item_rewards = json.loads(item_rewards)
    for reward in item_rewards:
        cur.execute(
            'INSERT INTO taskrewardmodel '
            '(taskmodelid, shopitemmodelid, amount, createtime, updatetime) '
            'VALUES (?, ?, ?, ?, ?)',
            (
                new_id,
                int(reward['item_id']),
                int(reward.get('amount', 1)),
                now,
                now,
            ),
        )
    return new_id


@app.route('/api/tasks/add', methods=['POST'])
def add_task():
    data = request.get_json()
    conn = get_db()
    try:
        new_id = _insert_task_with_cursor(conn.cursor(), data)
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
        field_columns = {
            'title': 'content',
            'frequency': 'taskfrequency',
            'coin': 'rewardcoin',
            'exp': 'expreward',
            'note': 'remark',
            'category_id': 'categoryid',
            'difficulty': 'taskdifficultydegree',
            'tagcolor': 'tagcolor',
            'priority': 'taskurgencydegree',
            'tasktype': 'tasktype',
            'rewardcoinvariable': 'rewardcoinvariable',
        }
        assignments = []
        values = []
        for request_field, column in field_columns.items():
            if request_field in data:
                assignments.append(f'{column}=?')
                values.append(data[request_field])
        if assignments:
            cur.execute(
                f"UPDATE taskmodel SET {', '.join(assignments)} WHERE id=?",
                values + [data['id']],
            )
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

        cur.execute("UPDATE taskmodel SET updatedtime=? WHERE id=?", (now, data['id']))
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
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
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
            i['icon_url'] = media_url('download', i.get('icon'))

        return jsonify(items)
    finally:
        conn.close()

def _insert_item_with_cursor(cur, data):
    now = now_ms()
    purchase_limits = json_text(data.get('purchaselimits'), '[]')
    extra_info = json_text(data.get('extrainfo'), '{}')
    shop_stock = data.get('count', data.get('stock', -1))
    inventory_stock = data.get('count', data.get('stock', 0))
    cur.execute(
        'INSERT INTO inventorymodel '
        '(createtime, stocknumber, updatetime, isstarred) VALUES (?, ?, ?, 0)',
        (now, inventory_stock, now),
    )
    inventory_id = cur.lastrowid

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
        shop_stock,
        data.get('category_id', 0),
        now,
        1 if data.get('isdisablepurchase') else 0,
        inventory_id,
        data.get('customusebuttontext', ''),
        purchase_limits,
        extra_info,
    ))
    item_id = cur.lastrowid

    effect_data = data
    effect_type = data.get('effect_type')
    if 'effects' not in data and effect_type in ('coin', 'exp'):
        effect = {
            'type': effect_type,
            'value': data.get('effect_value', 0),
        }
        if effect_type == 'exp' and data.get('effect_skill_id'):
            effect['skill_id'] = data['effect_skill_id']
        effect_data = dict(data)
        effect_data['effects'] = [effect]
    for effect_row in build_item_effects(effect_data, item_id, now):
        cur.execute("""
            INSERT INTO goodseffectmodel
            (createtime, shopitemid, goodseffecttype, relatedinfos, isdel, updatetime, relatedid, values_lpcolumn)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, effect_row)
    recipe_ids = create_simple_synthesis_recipes(cur, effect_data, item_id, now)
    return item_id, recipe_ids


@app.route('/api/items/add', methods=['POST'])
def add_item():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        new_item_id, recipe_ids = _insert_item_with_cursor(cur, data)
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
        cat_cond = 'AND categoryid=?' if cat_id else ''
        params = [int(cat_id)] if cat_id else []
        cur.execute(f"""
            SELECT id, content as name, description, type, categoryid, rewardcoin as coin,
                   expreward as exp, icon, achievementstatus, currentvalue, progress,
                   createtime, finishtime, updatetime, isgotreward, targetcompletetime,
                   orderincategory
            FROM userachievementmodel
            WHERE isdelete = 0 {cat_cond}
            ORDER BY categoryid, orderincategory, id
        """, params)
        ach_list = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT id, categoryname FROM userachcategorymodel WHERE isdelete=0")
        cats = {r['id']: r['categoryname'] for r in cur.fetchall()}
        for a in ach_list:
            a['category_name'] = cats.get(a.get('categoryid'), '-')
            a['icon_url'] = media_url('download', a.get('icon'))

        ach_list = _filter_annotated_achievements(
            _annotate_achievement_subcategories(ach_list), search, cat_id
        )
        return jsonify(ach_list)
    finally:
        conn.close()


def _achievement_rows_for_category(cur, category_id, exclude_id=None):
    params = [category_id]
    exclude_cond = ''
    if exclude_id is not None:
        exclude_cond = 'AND id<>?'
        params.append(exclude_id)
    cur.execute(f"""
        SELECT id, content as name, type, categoryid, orderincategory
        FROM userachievementmodel
        WHERE isdelete=0 AND categoryid=? {exclude_cond}
        ORDER BY orderincategory, id
    """, params)
    return [dict(row) for row in cur.fetchall()]


def _achievement_subcategory_for_record(cur, achievement_id, category_id):
    rows = _annotate_achievement_subcategories(
        _achievement_rows_for_category(cur, category_id)
    )
    row = next((item for item in rows if item['id'] == achievement_id), None)
    return row.get('subcategory_id') if row else None


def _achievement_subcategory_children(cur, subcategory_id, category_id):
    annotated = _annotate_achievement_subcategories(
        _achievement_rows_for_category(cur, category_id)
    )
    return [
        row for row in annotated
        if row.get('subcategory_id') == subcategory_id
    ]


def _renumber_achievement_rows(cur, rows):
    for order, row in enumerate(rows):
        cur.execute(
            'UPDATE userachievementmodel SET orderincategory=? WHERE id=?',
            (order, row['id']),
        )


def _place_achievement_record(
    cur, achievement_id, category_id, subcategory_id=None, old_category_id=None
):
    cur.execute(
        """
        SELECT id, content as name, type, categoryid, orderincategory
        FROM userachievementmodel WHERE id=? AND isdelete=0
        """,
        (achievement_id,),
    )
    moved = cur.fetchone()
    if not moved:
        raise ValueError('成就不存在或已删除')
    moved = dict(moved)
    moved['categoryid'] = category_id
    cur.execute(
        'UPDATE userachievementmodel SET categoryid=? WHERE id=?',
        (category_id, achievement_id),
    )

    if old_category_id is not None and old_category_id != category_id:
        _renumber_achievement_rows(
            cur, _achievement_rows_for_category(cur, old_category_id, achievement_id)
        )

    rows = _achievement_rows_for_category(cur, category_id, achievement_id)
    try:
        moved_type = int(moved.get('type', ACHIEVEMENT_TYPE_NORMAL) or 0)
    except (TypeError, ValueError):
        moved_type = ACHIEVEMENT_TYPE_NORMAL

    if moved_type == ACHIEVEMENT_TYPE_SUBCATEGORY:
        insert_at = len(rows)
    elif subcategory_id not in (None, ''):
        try:
            subcategory_id = int(subcategory_id)
        except (TypeError, ValueError) as exc:
            raise ValueError('子分类 ID 必须是正整数') from exc
        header_index = next(
            (
                index for index, row in enumerate(rows)
                if row['id'] == subcategory_id
                and int(row.get('type', 0) or 0) == ACHIEVEMENT_TYPE_SUBCATEGORY
            ),
            None,
        )
        if header_index is None:
            raise ValueError('所选子分类不存在，或不属于当前大类')
        insert_at = len(rows)
        for index in range(header_index + 1, len(rows)):
            if int(rows[index].get('type', 0) or 0) == ACHIEVEMENT_TYPE_SUBCATEGORY:
                insert_at = index
                break
    else:
        insert_at = next(
            (
                index for index, row in enumerate(rows)
                if int(row.get('type', 0) or 0) == ACHIEVEMENT_TYPE_SUBCATEGORY
            ),
            len(rows),
        )
    rows.insert(insert_at, moved)
    _renumber_achievement_rows(cur, rows)

def _insert_achievement_with_cursor(cur, data):
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
    return cur.lastrowid


@app.route('/api/achievements/add', methods=['POST'])
def add_achievement():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        achievement_id = _insert_achievement_with_cursor(cur, data)
        record_type = int(data.get('type', ACHIEVEMENT_TYPE_NORMAL) or 0)
        if record_type in (ACHIEVEMENT_TYPE_NORMAL, ACHIEVEMENT_TYPE_SUBCATEGORY):
            _place_achievement_record(
                cur,
                achievement_id,
                int(data.get('category_id', 0) or 0),
                data.get('subcategory_id') if record_type == ACHIEVEMENT_TYPE_NORMAL else None,
            )
        conn.commit()
        return jsonify({'ok': True, 'id': achievement_id})
    except ValueError as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
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
        achievement_id = int(data['id'])
        cur.execute(
            'SELECT type, categoryid FROM userachievementmodel WHERE id=? AND isdelete=0',
            (achievement_id,),
        )
        existing = cur.fetchone()
        if not existing:
            return jsonify({'error': '成就不存在或已删除'}), 404
        old_type = int(existing['type'] or 0)
        old_category_id = int(existing['categoryid'] or 0)
        new_type = int(data.get('type', old_type) or 0)
        new_category_id = int(data.get('category_id', old_category_id) or 0)
        if (
            old_type == ACHIEVEMENT_TYPE_SUBCATEGORY
            and (
                new_type != ACHIEVEMENT_TYPE_SUBCATEGORY
                or new_category_id != old_category_id
            )
        ):
            children = _achievement_subcategory_children(
                cur, achievement_id, old_category_id
            )
            if children:
                return jsonify({
                    'error': '这个子分类下仍有成就，请先把这些成就移到其他子分类或“大类直属”。',
                    'code': 'ACHIEVEMENT_SUBCATEGORY_NOT_EMPTY',
                    'child_count': len(children),
                }), 409
        old_subcategory_id = (
            _achievement_subcategory_for_record(cur, achievement_id, old_category_id)
            if old_type != ACHIEVEMENT_TYPE_SUBCATEGORY else None
        )
        field_columns = {
            'name': 'content',
            'description': 'description',
            'type': 'type',
            'category_id': 'categoryid',
            'coin': 'rewardcoin',
            'exp': 'expreward',
            'icon': 'icon',
        }
        assignments = []
        values = []
        for request_field, column in field_columns.items():
            if request_field in data:
                assignments.append(f'{column}=?')
                values.append(data[request_field])
        assignments.append('updatetime=?')
        values.append(now)
        cur.execute(
            f"UPDATE userachievementmodel SET {', '.join(assignments)} WHERE id=?",
            values + [achievement_id],
        )
        if new_type == ACHIEVEMENT_TYPE_NORMAL:
            if 'subcategory_id' in data:
                requested_subcategory_id = data.get('subcategory_id')
            elif (
                old_type == ACHIEVEMENT_TYPE_NORMAL
                and new_category_id == old_category_id
            ):
                requested_subcategory_id = old_subcategory_id
            else:
                requested_subcategory_id = None
        else:
            requested_subcategory_id = None
        normalized_requested_subcategory = (
            int(requested_subcategory_id)
            if requested_subcategory_id not in (None, '') else None
        )
        placement_changed = (
            new_type != old_type
            or new_category_id != old_category_id
            or (
                new_type == ACHIEVEMENT_TYPE_NORMAL
                and normalized_requested_subcategory != old_subcategory_id
            )
        )
        if new_type in (ACHIEVEMENT_TYPE_NORMAL, ACHIEVEMENT_TYPE_SUBCATEGORY) and placement_changed:
            _place_achievement_record(
                cur,
                achievement_id,
                new_category_id,
                normalized_requested_subcategory,
                old_category_id,
            )
        conn.commit()
        return jsonify({'ok': True})
    except ValueError as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/achievements/delete', methods=['POST'])
def delete_achievement():
    data = request.get_json()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT id, type, categoryid FROM userachievementmodel WHERE id=? AND isdelete=0',
            (data['id'],),
        )
        target = cur.fetchone()
        if not target:
            return jsonify({'error': '成就不存在或已删除'}), 404
        if int(target['type'] or 0) == ACHIEVEMENT_TYPE_SUBCATEGORY:
            children = _achievement_subcategory_children(
                cur, target['id'], target['categoryid']
            )
            if children:
                return jsonify({
                    'error': '这个子分类下仍有成就，请先把这些成就移到其他子分类或“大类直属”。',
                    'code': 'ACHIEVEMENT_SUBCATEGORY_NOT_EMPTY',
                    'child_count': len(children),
                }), 409
        cur.execute("UPDATE userachievementmodel SET isdelete=1, updatetime=? WHERE id=?",
                    (now_ms(), data['id']))
        _renumber_achievement_rows(
            cur, _achievement_rows_for_category(cur, target['categoryid'], data['id'])
        )
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
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
            WHERE taskstatus = 1 AND isdeleterecord = 0
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

def _task_import_error(code, message, status=400, suggestion=None):
    return _local_batch_error(code, message, status, suggestion)


def _task_import_template_rows():
    return [{
        'title': '晨间复盘',
        'category': '生活',
        'frequency': '每日',
        'target_count': 1,
        'priority': 2,
        'difficulty': 2,
        'skills': '心境',
        'coin': 10,
        'exp': 5,
        'note': '记录今天的收获与改进点',
        'item_rewards': '🎁诸天系统·绑定礼包*1',
        'is_frozen': '否',
        'duplicate_policy': '',
    }]


@app.route('/api/local/task-import-templates/csv', methods=['GET'])
def download_task_import_csv_template():
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=TASK_IMPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(_task_import_template_rows())
    body = b'\xef\xbb\xbf' + output.getvalue().encode('utf-8')
    return Response(
        body,
        mimetype='text/csv',
        headers={
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': 'attachment; filename=task_import_template.csv',
        },
    )


@app.route('/api/local/task-import-templates/json', methods=['GET'])
def download_task_import_json_template():
    rows = _task_import_template_rows()
    rows[0]['skills'] = ['心境']
    rows[0]['item_rewards'] = [
        {'name': '🎁诸天系统·绑定礼包', 'amount': 1}
    ]
    rows[0]['is_frozen'] = False
    body = b'\xef\xbb\xbf' + json.dumps(
        rows, ensure_ascii=False, indent=2
    ).encode('utf-8')
    return Response(
        body,
        mimetype='application/json',
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Content-Disposition': 'attachment; filename=task_import_template.json',
        },
    )


def _validate_task_import_columns(columns):
    if not columns or any(not isinstance(column, str) for column in columns):
        return False
    normalized = [column.strip() for column in columns]
    required = TASK_IMPORT_REQUIRED_COLUMNS
    return (
        len(normalized) == len(set(normalized))
        and required.issubset(normalized)
        and set(normalized).issubset(TASK_IMPORT_COLUMNS)
    )


def _parse_task_import_csv(text):
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
        if not _validate_task_import_columns(reader.fieldnames):
            raise ValueError('columns')
        rows = []
        for record in reader:
            if None in record:
                raise ValueError('columns')
            data = {
                column: record.get(column, '')
                for column in TASK_IMPORT_COLUMNS
            }
            if not any(str(value or '').strip() for value in data.values()):
                continue
            rows.append({
                'line': reader.line_num,
                'action': 'create',
                'data': data,
            })
    except csv.Error as exc:
        raise RuntimeError('csv') from exc
    if not 1 <= len(rows) <= MAX_BATCH_SIZE:
        raise OverflowError('rows')
    return rows


def _parse_task_import_json(text):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError('json') from exc
    if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_BATCH_SIZE:
        raise OverflowError('rows')
    rows = []
    allowed = set(TASK_IMPORT_COLUMNS)
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise OverflowError('rows')
        if any(not isinstance(key, str) for key in item) or not set(item).issubset(allowed):
            raise ValueError('columns')
        rows.append({'line': index, 'action': 'create', 'data': dict(item)})
    return rows


@app.route('/api/local/task-import-files', methods=['POST'])
def parse_task_import_file():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return _task_import_error(
            'TASK_IMPORT_FILE_REQUIRED',
            '请选择一个 CSV 或 JSON 任务文件。',
        )
    extension = Path(upload.filename).suffix.lower()
    if extension not in ('.csv', '.json'):
        return _task_import_error(
            'TASK_IMPORT_UNSUPPORTED_FORMAT',
            '只支持 .csv 或 .json 文件。',
        )
    content = upload.stream.read(MAX_TASK_IMPORT_FILE_BYTES + 1)
    if not content:
        return _task_import_error(
            'TASK_IMPORT_FILE_REQUIRED',
            '上传文件不能为空。',
        )
    if len(content) > MAX_TASK_IMPORT_FILE_BYTES:
        return _task_import_error(
            'TASK_IMPORT_FILE_TOO_LARGE',
            '任务导入文件不能超过 1 MiB。',
        )
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return _task_import_error(
            'TASK_IMPORT_INVALID_ENCODING',
            '文件必须使用 UTF-8 或 UTF-8 BOM 编码。',
        )
    try:
        rows = (
            _parse_task_import_csv(text)
            if extension == '.csv'
            else _parse_task_import_json(text)
        )
    except ValueError:
        return _task_import_error(
            'TASK_IMPORT_INVALID_COLUMNS',
            '文件列名不符合任务导入模板。',
        )
    except OverflowError:
        return _task_import_error(
            'TASK_IMPORT_INVALID_ROWS',
            f'文件必须包含 1～{MAX_BATCH_SIZE} 条任务。',
        )
    except RuntimeError as exc:
        code = (
            'TASK_IMPORT_INVALID_CSV'
            if str(exc) == 'csv'
            else 'TASK_IMPORT_INVALID_JSON'
        )
        return _task_import_error(code, '文件内容无法解析，请重新检查模板格式。')
    return jsonify({
        'ok': True,
        'format': extension[1:],
        'rows': rows,
    })


def _item_import_error(code, message, status=400, suggestion=None):
    return _local_batch_error(code, message, status, suggestion)


def _item_import_template_rows():
    return [
        {
            'action': 'create',
            'item_id': '',
            'name': '补给宝箱',
            'category': '乾坤百宝',
            'price': 25,
            'stock': -1,
            'is_purchase_enabled': '是',
            'effect_type': 'coin',
            'effect_value': 8,
            'effect_skill': '',
            'price_mode': '',
            'price_value': '',
            'duplicate_policy': '',
        },
        {
            'action': 'price',
            'item_id': '',
            'name': '',
            'category': '',
            'price': '',
            'stock': '',
            'is_purchase_enabled': '',
            'effect_type': '',
            'effect_value': '',
            'effect_skill': '',
            'price_mode': 'percent',
            'price_value': 10,
            'duplicate_policy': '',
        },
    ]


@app.route('/api/local/item-import-templates/csv', methods=['GET'])
def download_item_import_csv_template():
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=ITEM_IMPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(_item_import_template_rows())
    body = b'\xef\xbb\xbf' + output.getvalue().encode('utf-8')
    return Response(
        body,
        mimetype='text/csv',
        headers={
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': 'attachment; filename=item_import_template.csv',
        },
    )


@app.route('/api/local/item-import-templates/json', methods=['GET'])
def download_item_import_json_template():
    rows = _item_import_template_rows()
    rows[0]['is_purchase_enabled'] = True
    body = b'\xef\xbb\xbf' + json.dumps(
        rows, ensure_ascii=False, indent=2
    ).encode('utf-8')
    return Response(
        body,
        mimetype='application/json',
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Content-Disposition': 'attachment; filename=item_import_template.json',
        },
    )


def _validate_item_import_columns(columns):
    if not columns or any(not isinstance(column, str) for column in columns):
        return False
    normalized = [column.strip() for column in columns]
    required = set(ITEM_IMPORT_COLUMNS) - {'duplicate_policy'}
    return (
        len(normalized) == len(set(normalized))
        and required.issubset(normalized)
        and set(normalized).issubset(ITEM_IMPORT_COLUMNS)
    )


def _parse_item_import_csv(text):
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
        if not _validate_item_import_columns(reader.fieldnames):
            raise ValueError('columns')
        rows = []
        for record in reader:
            if None in record:
                raise ValueError('columns')
            values = {
                column: record.get(column, '')
                for column in ITEM_IMPORT_COLUMNS
            }
            if not any(str(value or '').strip() for value in values.values()):
                continue
            action = str(values.pop('action', '') or '').strip().lower()
            rows.append({
                'line': reader.line_num,
                'action': action,
                'data': values,
            })
    except csv.Error as exc:
        raise RuntimeError('csv') from exc
    if not 1 <= len(rows) <= MAX_BATCH_SIZE:
        raise OverflowError('rows')
    return rows


def _parse_item_import_json(text):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError('json') from exc
    if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_BATCH_SIZE:
        raise OverflowError('rows')
    rows = []
    allowed = set(ITEM_IMPORT_COLUMNS)
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise OverflowError('rows')
        if any(not isinstance(key, str) for key in item) or not set(item).issubset(allowed):
            raise ValueError('columns')
        action = item.get('action')
        action = action.strip().lower() if isinstance(action, str) else action
        rows.append({
            'line': index,
            'action': action,
            'data': {key: value for key, value in item.items() if key != 'action'},
        })
    return rows


@app.route('/api/local/item-import-files', methods=['POST'])
def parse_item_import_file():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return _item_import_error(
            'ITEM_IMPORT_FILE_REQUIRED',
            '请选择一个 CSV 或 JSON 商品文件。',
        )
    extension = Path(upload.filename).suffix.lower()
    if extension not in ('.csv', '.json'):
        return _item_import_error(
            'ITEM_IMPORT_UNSUPPORTED_FORMAT',
            '只支持 .csv 或 .json 文件。',
        )
    content = upload.stream.read(MAX_ITEM_IMPORT_FILE_BYTES + 1)
    if not content:
        return _item_import_error(
            'ITEM_IMPORT_FILE_REQUIRED',
            '上传文件不能为空。',
        )
    if len(content) > MAX_ITEM_IMPORT_FILE_BYTES:
        return _item_import_error(
            'ITEM_IMPORT_FILE_TOO_LARGE',
            '商品导入文件不能超过 1 MiB。',
        )
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return _item_import_error(
            'ITEM_IMPORT_INVALID_ENCODING',
            '文件必须使用 UTF-8 或 UTF-8 BOM 编码。',
        )
    try:
        rows = (
            _parse_item_import_csv(text)
            if extension == '.csv'
            else _parse_item_import_json(text)
        )
    except ValueError:
        return _item_import_error(
            'ITEM_IMPORT_INVALID_COLUMNS',
            '文件列名不符合商品导入模板。',
        )
    except OverflowError:
        return _item_import_error(
            'ITEM_IMPORT_INVALID_ROWS',
            f'文件必须包含 1～{MAX_BATCH_SIZE} 条商品操作。',
        )
    except RuntimeError as exc:
        code = (
            'ITEM_IMPORT_INVALID_CSV'
            if str(exc) == 'csv'
            else 'ITEM_IMPORT_INVALID_JSON'
        )
        return _item_import_error(code, '文件内容无法解析，请重新检查模板格式。')
    return jsonify({
        'ok': True,
        'format': extension[1:],
        'rows': rows,
    })


def _achievement_import_error(code, message, status=400, suggestion=None):
    return _local_batch_error(code, message, status, suggestion)


def _achievement_import_template_rows():
    return [{
        'action': 'create',
        'name': '筑基里程碑',
        'category': '里程碑',
        'description': '完成筑基',
        'coin': 88,
        'exp': 144,
        'icon': 'golden-core.png',
        'conditions': '',
        'duplicate_policy': '',
    }]


@app.route('/api/local/achievement-import-templates/csv', methods=['GET'])
def download_achievement_import_csv_template():
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=ACHIEVEMENT_IMPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(_achievement_import_template_rows())
    body = b'\xef\xbb\xbf' + output.getvalue().encode('utf-8')
    return Response(
        body,
        mimetype='text/csv',
        headers={
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': (
                'attachment; filename=achievement_import_template.csv'
            ),
        },
    )


@app.route('/api/local/achievement-import-templates/json', methods=['GET'])
def download_achievement_import_json_template():
    body = b'\xef\xbb\xbf' + json.dumps(
        _achievement_import_template_rows(), ensure_ascii=False, indent=2
    ).encode('utf-8')
    return Response(
        body,
        mimetype='application/json',
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Content-Disposition': (
                'attachment; filename=achievement_import_template.json'
            ),
        },
    )


def _validate_achievement_import_columns(columns):
    if not columns or any(not isinstance(column, str) for column in columns):
        return False
    normalized = [column.strip() for column in columns]
    required = set(ACHIEVEMENT_IMPORT_COLUMNS) - {
        'conditions', 'duplicate_policy'
    }
    return (
        len(normalized) == len(set(normalized))
        and required.issubset(normalized)
        and set(normalized).issubset(ACHIEVEMENT_IMPORT_COLUMNS)
    )


def _parse_achievement_import_csv(text):
    try:
        reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
        if not _validate_achievement_import_columns(reader.fieldnames):
            raise ValueError('columns')
        rows = []
        for record in reader:
            if None in record:
                raise ValueError('columns')
            values = {
                column: record.get(column, '')
                for column in ACHIEVEMENT_IMPORT_COLUMNS
            }
            if not any(str(value or '').strip() for value in values.values()):
                continue
            action = str(values.pop('action', '') or '').strip().lower()
            rows.append({
                'line': reader.line_num,
                'action': action,
                'data': values,
            })
    except csv.Error as exc:
        raise RuntimeError('csv') from exc
    if not 1 <= len(rows) <= MAX_BATCH_SIZE:
        raise OverflowError('rows')
    return rows


def _parse_achievement_import_json(text):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError('json') from exc
    if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_BATCH_SIZE:
        raise OverflowError('rows')
    rows = []
    allowed = set(ACHIEVEMENT_IMPORT_COLUMNS)
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise OverflowError('rows')
        if (
            any(not isinstance(key, str) for key in item)
            or not set(item).issubset(allowed)
        ):
            raise ValueError('columns')
        action = item.get('action')
        action = action.strip().lower() if isinstance(action, str) else action
        rows.append({
            'line': index,
            'action': action,
            'data': {key: value for key, value in item.items() if key != 'action'},
        })
    return rows


@app.route('/api/local/achievement-import-files', methods=['POST'])
def parse_achievement_import_file():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return _achievement_import_error(
            'ACHIEVEMENT_IMPORT_FILE_REQUIRED',
            '请选择一个 CSV 或 JSON 成就文件。',
        )
    extension = Path(upload.filename).suffix.lower()
    if extension not in ('.csv', '.json'):
        return _achievement_import_error(
            'ACHIEVEMENT_IMPORT_UNSUPPORTED_FORMAT',
            '只支持 .csv 或 .json 文件。',
        )
    content = upload.stream.read(MAX_ACHIEVEMENT_IMPORT_FILE_BYTES + 1)
    if not content:
        return _achievement_import_error(
            'ACHIEVEMENT_IMPORT_FILE_REQUIRED',
            '上传文件不能为空。',
        )
    if len(content) > MAX_ACHIEVEMENT_IMPORT_FILE_BYTES:
        return _achievement_import_error(
            'ACHIEVEMENT_IMPORT_FILE_TOO_LARGE',
            '成就导入文件不能超过 1 MiB。',
        )
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return _achievement_import_error(
            'ACHIEVEMENT_IMPORT_INVALID_ENCODING',
            '文件必须使用 UTF-8 或 UTF-8 BOM 编码。',
        )
    try:
        rows = (
            _parse_achievement_import_csv(text)
            if extension == '.csv'
            else _parse_achievement_import_json(text)
        )
    except ValueError:
        return _achievement_import_error(
            'ACHIEVEMENT_IMPORT_INVALID_COLUMNS',
            '文件列名不符合成就导入模板。',
        )
    except OverflowError:
        return _achievement_import_error(
            'ACHIEVEMENT_IMPORT_INVALID_ROWS',
            f'文件必须包含 1～{MAX_BATCH_SIZE} 条成就操作。',
        )
    except RuntimeError as exc:
        code = (
            'ACHIEVEMENT_IMPORT_INVALID_CSV'
            if str(exc) == 'csv'
            else 'ACHIEVEMENT_IMPORT_INVALID_JSON'
        )
        return _achievement_import_error(
            code, '文件内容无法解析，请重新检查模板格式。'
        )
    return jsonify({
        'ok': True,
        'format': extension[1:],
        'rows': rows,
    })

@app.route('/api/local/batch-previews', methods=['POST'])
def create_local_batch_preview():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _local_batch_error(
            'INVALID_REQUEST',
            '请求内容必须是有效的 JSON 对象。',
            400,
            '请发送 entity 和 rows 两个字段。',
        )
    entity = data.get('entity')
    if entity not in ('tasks', 'items', 'achievements', 'icons'):
        return _local_batch_error(
            'INVALID_ENTITY',
            'entity 只接受 tasks、items、achievements 或 icons。',
            400,
        )
    raw_rows = data.get('rows')
    if not isinstance(raw_rows, list) or not 1 <= len(raw_rows) <= MAX_BATCH_SIZE:
        return _local_batch_error(
            'INVALID_ROWS',
            f'rows 必须是 1～{MAX_BATCH_SIZE} 行的数组。',
            400,
        )

    connection = None
    try:
        with STATE_LOCK:
            connection = get_db()
            results, normalized_rows, duplicate_count = _normalize_local_batch_rows(
                entity, raw_rows, connection.cursor()
            )
            workspace_key = os.path.normcase(os.path.realpath(STATE['db_path']))
    except RuntimeError:
        return _local_batch_error(
            'NO_BACKUP_LOADED',
            '未加载本地备份工作副本。',
            400,
            '请先载入工作副本，再创建批量预览。',
        )
    except sqlite3.DatabaseError:
        return _local_batch_error(
            'BATCH_PREVIEW_FAILED',
            '读取批量目标时发生数据库错误。',
            500,
            '没有写入任何数据；请重新载入有效工作副本后重试。',
        )
    finally:
        if connection is not None:
            connection.close()

    normalized_request = {'entity': entity, 'rows': normalized_rows}
    digest = _stable_local_batch_digest(normalized_request)
    error_count = sum(row['status'] == 'error' for row in results)
    ready_count = len(results) - error_count
    created_at = time.time()
    with LOCAL_BATCH_PREVIEW_LOCK:
        _cleanup_local_batch_previews_locked(created_at)
        token = _allocate_local_batch_preview_token_locked()
        LOCAL_BATCH_PREVIEWS[token] = {
            'entity': entity,
            'rows': results,
            'digest': digest,
            'workspace_key': workspace_key,
            'expires_at': created_at + LOCAL_BATCH_PREVIEW_TTL_SECONDS,
            'used': False,
        }

    return jsonify({
        'ok': True,
        'contract_version': LOCAL_BATCH_CONTRACT_VERSION,
        'preview_token': token,
        'digest': digest,
        'expires_in': LOCAL_BATCH_PREVIEW_TTL_SECONDS,
        'entity': entity,
        'rows': results,
        'can_execute': error_count == 0,
        'summary': {
            'total': len(results),
            'ready': ready_count,
            'errors': error_count,
            'duplicates': duplicate_count,
        },
    }), 201


@app.route(
    '/api/local/batch-previews/<preview_token>/executions',
    methods=['POST'],
)
def execute_local_batch_preview(preview_token):
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
    if not LOCAL_BATCH_PREVIEW_TOKEN_PATTERN.fullmatch(preview_token or ''):
        return _local_batch_error(
            'PREVIEW_NOT_AVAILABLE',
            '批量预览令牌不存在。',
            409,
            '请重新打开批量预览。',
        )
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _local_batch_error(
            'INVALID_REQUEST',
            '请求内容必须是有效的 JSON 对象。',
            400,
        )
    digest = data.get('digest')
    if not isinstance(digest, str):
        return _local_batch_error(
            'DIGEST_REQUIRED',
            'digest 必须是创建预览时返回的 SHA-256 值。',
            400,
            '请关闭旧预览并重新创建。',
        )

    now = time.time()
    with LOCAL_BATCH_PREVIEW_LOCK:
        preview = LOCAL_BATCH_PREVIEWS.get(preview_token)
        if preview is None:
            return _local_batch_error(
                'PREVIEW_NOT_AVAILABLE',
                '批量预览令牌不存在。',
                409,
                '请重新打开批量预览。',
            )
        if preview['expires_at'] <= now:
            LOCAL_BATCH_PREVIEWS.pop(preview_token, None)
            return _local_batch_error(
                'PREVIEW_EXPIRED',
                '批量预览已过期。',
                409,
                '请重新创建预览并再次确认。',
            )
        if preview['used']:
            return _local_batch_error(
                'PREVIEW_ALREADY_USED',
                '这个批量预览已经执行过。',
                409,
                '请刷新数据并创建新的预览。',
            )
        if (
            not LOCAL_BATCH_DIGEST_PATTERN.fullmatch(digest)
            or not secrets.compare_digest(preview['digest'], digest)
        ):
            LOCAL_BATCH_PREVIEWS.pop(preview_token, None)
            return _local_batch_error(
                'PREVIEW_CONTENT_CHANGED',
                '预览内容摘要不一致，旧令牌已销毁。',
                409,
                '请重新创建预览，核对内容后再执行。',
            )
        if any(row['status'] == 'error' for row in preview['rows']):
            preview['used'] = True
            return _local_batch_error(
                'BATCH_PREVIEW_BLOCKED',
                '预览中存在错误行，不能执行。',
                422,
                '请修正所有错误行后重新创建预览。',
            )
        current_workspace = (
            os.path.normcase(os.path.realpath(STATE.get('db_path') or ''))
            if STATE.get('loaded')
            else ''
        )
        if current_workspace != preview['workspace_key']:
            LOCAL_BATCH_PREVIEWS.pop(preview_token, None)
            return _local_batch_error(
                'PREVIEW_WORKSPACE_CHANGED',
                '当前工作副本已变化，旧预览不能执行。',
                409,
                '请在当前工作副本中重新创建预览。',
            )
        preview['used'] = True

    entity_label = {
        'tasks': '任务',
        'items': '商品',
        'achievements': '成就',
        'icons': '图标引用',
    }[preview['entity']]
    snapshot = None
    connection = None
    try:
        with STATE_LOCK:
            current_workspace = (
                os.path.normcase(os.path.realpath(STATE.get('db_path') or ''))
                if STATE.get('loaded')
                else ''
            )
            if current_workspace != preview['workspace_key']:
                return _local_batch_error(
                    'PREVIEW_WORKSPACE_CHANGED',
                    '当前工作副本已变化，旧预览不能执行。',
                    409,
                    '请在当前工作副本中重新创建预览。',
                )
            snapshot = create_snapshot(
                f'批量操作前：{entity_label} {len(preview["rows"])} 行'
            )
            connection = get_db()
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.cursor()
            execution_rows = []
            for row in preview['rows']:
                action_result = _execute_local_batch_action(
                    cursor, row['planned_action']
                )
                execution_rows.append({
                    'line': row['line'],
                    'status': 'success',
                    'errors': [],
                    'normalized_data': dict(row['normalized_data']),
                    'planned_action': dict(row['planned_action']),
                    'result': action_result,
                })
            connection.commit()
    except LocalBatchExecutionChanged:
        if connection is not None:
            connection.rollback()
        return _local_batch_error(
            'BATCH_TARGET_CHANGED',
            '目标在预览后发生变化，全部操作已回滚。',
            409,
            '执行前快照已保留；请刷新数据并重新创建预览。',
        )
    except (SnapshotError, BackupExportError, OSError):
        if connection is not None:
            connection.rollback()
        return _local_batch_error(
            'BATCH_SNAPSHOT_FAILED',
            '执行前快照创建失败，因此没有写入数据库。',
            500,
            '请检查工作副本和快照目录后重试。',
        )
    except sqlite3.DatabaseError:
        if connection is not None:
            connection.rollback()
        app.logger.warning('Local batch transaction rolled back after a database error')
        return _local_batch_error(
            'BATCH_EXECUTION_FAILED',
            '批量数据库操作失败，全部写入已回滚。',
            500,
            '执行前快照已保留；请刷新数据后重试。',
        )
    except Exception:
        if connection is not None:
            connection.rollback()
        app.logger.exception('Unexpected local batch execution failure')
        return _local_batch_error(
            'BATCH_EXECUTION_FAILED',
            '批量操作失败，全部写入已回滚。',
            500,
            '执行前快照已保留；请刷新数据后重试。',
        )
    finally:
        if connection is not None:
            connection.close()

    affected = sum(row['result']['affected'] for row in execution_rows)
    return jsonify({
        'ok': True,
        'contract_version': LOCAL_BATCH_CONTRACT_VERSION,
        'snapshot': snapshot,
        'rows': execution_rows,
        'summary': {
            'total': len(execution_rows),
            'succeeded': len(execution_rows),
            'failed': 0,
            'affected': affected,
        },
    })


@app.route('/api/tasks/batch', methods=['POST'])
def batch_tasks():
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
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
    rejected = reject_cloud_local_write()
    if rejected:
        return rejected
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
        ph = ','.join('?' * len(ids))
        if action == 'disable':
            cur.execute(f"UPDATE shopitemmodel SET isdisablepurchase=1 WHERE id IN ({ph})", ids)
        elif action == 'enable':
            cur.execute(f"UPDATE shopitemmodel SET isdisablepurchase=0 WHERE id IN ({ph})", ids)
        elif action == 'price':
            cur.execute(f"UPDATE shopitemmodel SET price=? WHERE id IN ({ph})", [price] + ids)
        elif action == 'delete':
            cur.execute(f"UPDATE shopitemmodel SET isdel=1 WHERE id IN ({ph})", ids)
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
            WHERE taskstatus = 1 AND isdeleterecord = 0 AND (rewardcoin > 0 OR expreward > 0)
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
    except CloudRequestError as e:
        return cloud_error_response(e)
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
    except CloudRequestError as e:
        return cloud_error_response(e)
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
    force_refresh = data.get('force_refresh') is True
    try:
        if dataset == 'achievements':
            categories = cloud_cached_request(
                data, '/achievement_categories', timeout=15, force_refresh=force_refresh
            )
            rows = []
            errors = []
            category_results = []
            for cat in as_cloud_rows(categories['data']):
                cat_id = cat.get('id') if isinstance(cat, dict) else None
                if cat_id is None:
                    continue
                try:
                    result = cloud_cached_request(
                        data, f'/achievements/{cat_id}', timeout=15,
                        force_refresh=force_refresh,
                    )
                    count = 0
                    for row in as_cloud_rows(result['data']):
                        if isinstance(row, dict):
                            row = dict(row)
                            row.setdefault('category_id', cat_id)
                            row.setdefault('category_name', cat.get('name', ''))
                        rows.append(row)
                        count += 1
                    category_results.append({
                        'id': cat_id,
                        'name': cat.get('name', ''),
                        'status': 'success',
                        'count': count,
                        'cache': result.get('cache', {}),
                    })
                except CloudRequestError as e:
                    detail = {'category_id': cat_id, 'category_name': cat.get('name', '')}
                    detail.update(e.details())
                    errors.append(detail)
                    category_results.append({
                        'id': cat_id,
                        'name': cat.get('name', ''),
                        'status': 'error',
                        'count': 0,
                        'error': e.details(),
                    })
            return jsonify({
                'ok': True,
                'dataset': dataset,
                'route': '/achievement_categories + /achievements/{id}',
                'rows': rows,
                'count': len(rows),
                'errors': errors,
                'categories': category_results,
                'partial': bool(errors and rows),
                'cache': categories.get('cache', {}),
            })
        if dataset == 'achievement_category':
            try:
                category_id = int(data.get('category_id'))
            except (TypeError, ValueError) as exc:
                raise _cloud_config_error(
                    'CLOUD_CATEGORY_INVALID',
                    '成就分类 ID 必须是正整数',
                    '重新读取成就分类后再点击该分类的重试按钮。',
                ) from exc
            if category_id <= 0:
                raise _cloud_config_error(
                    'CLOUD_CATEGORY_INVALID',
                    '成就分类 ID 必须是正整数',
                    '重新读取成就分类后再点击该分类的重试按钮。',
                )
            result = cloud_cached_request(
                data, f'/achievements/{category_id}', timeout=15,
                force_refresh=force_refresh,
            )
            rows = []
            for raw_row in as_cloud_rows(result['data']):
                row = dict(raw_row) if isinstance(raw_row, dict) else raw_row
                if isinstance(row, dict):
                    row.setdefault('category_id', category_id)
                rows.append(row)
            return jsonify({
                'ok': True,
                'dataset': dataset,
                'category_id': category_id,
                'route': result['route'],
                'rows': rows,
                'count': len(rows),
                'cache': result.get('cache', {}),
            })
        if dataset not in routes:
            return jsonify({'error': '未知云人升数据集'}), 400
        result = cloud_cached_request(
            data, routes[dataset], timeout=20, force_refresh=force_refresh
        )
        rows = as_cloud_rows(result['data'])
        return jsonify({
            'ok': True,
            'dataset': dataset,
            'route': result['route'],
            'rows': rows,
            'count': len(rows),
            'cache': result.get('cache', {}),
        })
    except CloudRequestError as e:
        return cloud_error_response(e)
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
        operation_id = secrets.token_hex(16)
        summaries = cloud_task_summaries(urls)
        append_cloud_operation({
            'version': 1,
            'operation_id': operation_id,
            'type': 'preview',
            'created_at': cloud_operation_timestamp(),
            'status': 'ready',
            'count': len(urls),
            'digest': digest[:16],
            'items': summaries,
        })
        with CLOUD_EXECUTION_LOCK:
            cleanup_cloud_execution_state(now)
            CLOUD_PREVIEWS[token] = {
                'config': config,
                'urls': tuple(urls),
                'summaries': tuple(copy.deepcopy(summaries)),
                'digest': digest,
                'operation_id': operation_id,
                'created_at': now,
                'expires_at': now + CLOUD_PREVIEW_TTL_SECONDS,
                'status': 'ready',
                'idempotency_key': None
            }
        return jsonify({
            'ok': True,
            'count': len(urls),
            'preview_token': token,
            'operation_id': operation_id,
            'digest': digest,
            'expires_in': CLOUD_PREVIEW_TTL_SECONDS,
            'items': summaries,
        })
    except CloudTaskValidationError as e:
        detail = {'row': e.row, 'field': e.field, 'message': str(e)}
        return jsonify({
            'code': 'CLOUD_TASK_VALIDATION_FAILED',
            'error': str(e),
            'errors': [detail],
        }), 400
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
        summaries = list(copy.deepcopy(preview.get('summaries') or cloud_task_summaries(urls)))
        preview_operation_id = preview.get('operation_id')

    try:
        result = cloud_post_json(config, '/api/contentprovider', {'urls': urls}, timeout=45)
        report_items = cloud_execution_items(summaries, result['data'])
        operation_id = secrets.token_hex(16)
        payload = {
            'ok': True,
            'count': len(urls),
            'route': result['route'],
            'base_url': result['base_url'],
            'results': report_items,
            'summary': cloud_execution_summary(report_items),
            'operation_id': operation_id,
            'idempotent_replay': False
        }
    except Exception as e:
        with CLOUD_EXECUTION_LOCK:
            preview = CLOUD_PREVIEWS.get(preview_token)
            if preview and preview.get('idempotency_key') == idempotency_key:
                preview['status'] = 'uncertain'
        operation_id = secrets.token_hex(16)
        report_items = [dict(
            item,
            status='unknown',
            message='执行结果不确定，请先刷新手机任务，不要立即重复执行',
        ) for item in summaries]
        error_code = e.code if isinstance(e, CloudRequestError) else 'CLOUD_EXECUTION_FAILED'
        try:
            append_cloud_operation({
                'version': 1,
                'operation_id': operation_id,
                'preview_operation_id': preview_operation_id,
                'type': 'execute',
                'created_at': cloud_operation_timestamp(),
                'status': 'uncertain',
                'count': len(urls),
                'error_code': error_code,
                'idempotency_digest': hashlib.sha256(
                    idempotency_key.encode('utf-8')
                ).hexdigest()[:16],
                'summary': cloud_execution_summary(report_items),
                'items': report_items,
            })
        except OSError:
            pass
        if isinstance(e, CloudRequestError):
            details = e.details()
            details['operation_id'] = operation_id
            return jsonify(details), e.status
        if isinstance(e, ValueError):
            return jsonify({'error': str(e), 'operation_id': operation_id}), 400
        return jsonify({
            'error': '云端新增任务执行失败，请先刷新手机任务确认结果。',
            'operation_id': operation_id,
        }), 500

    execution_record = {
        'version': 1,
        'operation_id': operation_id,
        'preview_operation_id': preview_operation_id,
        'type': 'execute',
        'created_at': cloud_operation_timestamp(),
        'status': 'completed',
        'count': len(urls),
        'idempotency_digest': hashlib.sha256(
            idempotency_key.encode('utf-8')
        ).hexdigest()[:16],
        'summary': payload['summary'],
        'items': report_items,
    }
    try:
        append_cloud_operation(execution_record)
    except OSError:
        payload['log_warning'] = '任务已发送，但本地操作记录写入失败。'

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


@app.route('/api/cloud/operations')
def api_cloud_operations():
    records = list(reversed(read_cloud_operations()))
    return jsonify({'ok': True, 'operations': records, 'count': len(records)})


@app.route('/api/cloud/operations/export')
def api_cloud_operations_export():
    body = json.dumps(read_cloud_operations(), ensure_ascii=False, indent=2)
    return Response(
        body,
        mimetype='application/json',
        headers={
            'Content-Disposition': (
                'attachment; filename=lifeup_cloud_operation_report.json'
            )
        },
    )


@app.route('/api/cloud/task-import-templates/csv')
def download_cloud_task_import_csv_template():
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\r\n')
    writer.writerow([
        'todo', 'notes', 'coin', 'coin_var', 'exp', 'skills',
        'category', 'frequency', 'importance', 'difficulty',
    ])
    writer.writerow(['写日记', '睡前记录', 2, 0, 5, '心境', '每日清单', 0, 2, 2])
    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': (
                'attachment; filename=cloud_task_import_template.csv'
            )
        },
    )


@app.route('/api/cloud/task-import-templates/json')
def download_cloud_task_import_json_template():
    body = json.dumps([{
        'todo': '写日记',
        'notes': '睡前记录',
        'coin': 2,
        'coin_var': 0,
        'exp': 5,
        'skills': ['心境'],
        'category': '每日清单',
        'frequency': 0,
        'importance': 2,
        'difficulty': 2,
    }], ensure_ascii=False, indent=2)
    return Response(
        body,
        mimetype='application/json',
        headers={
            'Content-Disposition': (
                'attachment; filename=cloud_task_import_template.json'
            )
        },
    )


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
        'history': 'SELECT updatedtime as time, content as title, rewardcoin as coin, expreward as exp FROM taskmodel WHERE taskstatus=1 AND isdeleterecord=0 ORDER BY updatedtime DESC',
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
