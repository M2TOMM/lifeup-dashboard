import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import server
from tests.fixtures import SCHEMA_SQL, SEED_SQL


ROOT = Path(__file__).resolve().parents[1]
ACHIEVEMENT_IMPORT_HEADER = (
    "action,name,category,description,coin,exp,icon,conditions,duplicate_policy"
)


class AchievementBatchImportApiTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.root = tempfile.mkdtemp(prefix="lifeup-achievement-import-")
        self.workspace = os.path.join(self.root, "workspace")
        self.database = os.path.join(
            self.workspace, "databases", "LifeUpDB.db"
        )
        self.snapshot_dir = os.path.join(self.root, "snapshots")
        os.makedirs(os.path.dirname(self.database), exist_ok=True)
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.executescript(SEED_SQL)
            connection.executescript(
                """
                CREATE TABLE achievementinfomodel (
                    id INTEGER PRIMARY KEY,
                    title TEXT
                );
                CREATE TABLE unlockconditionmodel (
                    id INTEGER PRIMARY KEY,
                    userachievementid INTEGER,
                    isdel INTEGER DEFAULT 0
                );
                INSERT INTO achievementinfomodel (id, title)
                VALUES (1, 'achievement_base_new_player');
                INSERT INTO userachievementmodel (
                    id, content, description, type, categoryid, rewardcoin,
                    expreward, icon, achievementstatus, currentvalue, progress,
                    createtime, updatetime, isdelete, isgotreward,
                    rewardcoinvariable, orderincategory
                ) VALUES (
                    201, '已有成就', '', 0, 7, 0, 0, '', 0, 0, 0,
                    1000, 1000, 0, 0, 0, 0
                );
                """
            )
            connection.commit()
        finally:
            connection.close()
        self.snapshot_patch = mock.patch.object(
            server, "SNAPSHOT_DIR", self.snapshot_dir
        )
        self.snapshot_patch.start()
        server.STATE.update(
            {
                "backup_path": None,
                "db_path": self.database,
                "tmpdir": self.workspace,
                "loaded": True,
            }
        )
        server.LOCAL_BATCH_PREVIEWS.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        server.LOCAL_BATCH_PREVIEWS.clear()
        server.STATE.clear()
        server.STATE.update(self._old_state)
        self.snapshot_patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        self.assertFalse(os.path.exists(self.root), "临时成就导入目录没有清理")

    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def upload(self, name, content, expected_status=200, headers=None):
        response = self.client.post(
            "/api/local/achievement-import-files",
            data={"file": (io.BytesIO(content), name)},
            content_type="multipart/form-data",
            headers=headers or {},
        )
        self.assertEqual(
            response.status_code,
            expected_status,
            response.get_data(as_text=True),
        )
        return response.get_json()

    def preview(self, rows, expected_status=201, headers=None):
        response = self.client.post(
            "/api/local/batch-previews",
            json={"entity": "achievements", "rows": rows},
            headers=headers,
        )
        self.assertEqual(
            response.status_code, expected_status, response.get_json()
        )
        return response.get_json()

    def execute(self, preview, expected_status=200, headers=None):
        response = self.client.post(
            f"/api/local/batch-previews/{preview['preview_token']}/executions",
            json={"digest": preview["digest"]},
            headers=headers,
        )
        self.assertEqual(
            response.status_code, expected_status, response.get_json()
        )
        return response.get_json()

    def test_templates_and_utf8_csv_and_json_file_parsing(self):
        csv_response = self.client.get(
            "/api/local/achievement-import-templates/csv"
        )
        self.assertEqual(csv_response.status_code, 200)
        self.assertTrue(csv_response.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(ACHIEVEMENT_IMPORT_HEADER, csv_response.get_data(as_text=True))

        json_response = self.client.get(
            "/api/local/achievement-import-templates/json"
        )
        self.assertEqual(json_response.status_code, 200)
        self.assertTrue(json_response.data.startswith(b"\xef\xbb\xbf"))

        parsed_csv = self.upload(
            "achievements.csv",
            (
                ACHIEVEMENT_IMPORT_HEADER
                + "\r\n"
                + "create,筑基里程碑,里程碑,完成筑基,88,144,"
                + "golden-core.png,,\r\n"
            ).encode("utf-8-sig"),
        )
        self.assertEqual(parsed_csv["rows"][0]["line"], 2)
        self.assertEqual(
            parsed_csv["rows"][0]["data"]["name"], "筑基里程碑"
        )

        parsed_json = self.upload(
            "achievements.json",
            json.dumps(
                [{"action": "create", "name": "金丹里程碑"}],
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        self.assertEqual(parsed_json["rows"][0]["line"], 1)
        self.assertEqual(parsed_json["rows"][0]["action"], "create")

        artifact_csv_path = ROOT / "outputs" / "achievement_import_template.csv"
        artifact_json_path = ROOT / "outputs" / "achievement_import_template.json"
        self.assertTrue(artifact_csv_path.exists())
        self.assertTrue(artifact_json_path.exists())
        artifact_csv = artifact_csv_path.read_bytes()
        artifact_json = artifact_json_path.read_bytes()
        self.assertTrue(artifact_csv.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(artifact_json.startswith(b"\xef\xbb\xbf"))
        parsed_artifact_csv = self.upload(
            "achievement_import_template.csv", artifact_csv
        )
        parsed_artifact_json = self.upload(
            "achievement_import_template.json", artifact_json
        )
        self.assertEqual(len(parsed_artifact_csv["rows"]), 1)
        self.assertEqual(len(parsed_artifact_json["rows"]), 1)
        self.assertEqual(parsed_artifact_csv["rows"][0]["action"], "create")

    def test_upload_rejections_have_stable_achievement_codes(self):
        self.assertEqual(server.MAX_ACHIEVEMENT_IMPORT_FILE_BYTES, 1024 * 1024)
        missing = self.client.post(
            "/api/local/achievement-import-files",
            data={},
            content_type="multipart/form-data",
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(
            missing.get_json()["code"], "ACHIEVEMENT_IMPORT_FILE_REQUIRED"
        )

        cases = (
            (
                "achievements.txt",
                b"action",
                "ACHIEVEMENT_IMPORT_UNSUPPORTED_FORMAT",
            ),
            (
                "achievements.csv",
                b"\xff\xfeinvalid",
                "ACHIEVEMENT_IMPORT_INVALID_ENCODING",
            ),
            (
                "achievements.csv",
                (ACHIEVEMENT_IMPORT_HEADER + '\r\n"unterminated').encode(),
                "ACHIEVEMENT_IMPORT_INVALID_CSV",
            ),
            (
                "achievements.json",
                b"{not-json",
                "ACHIEVEMENT_IMPORT_INVALID_JSON",
            ),
            (
                "achievements.csv",
                b"action,name,unknown\r\ncreate,A,B\r\n",
                "ACHIEVEMENT_IMPORT_INVALID_COLUMNS",
            ),
            (
                "achievements.json",
                b"[]",
                "ACHIEVEMENT_IMPORT_INVALID_ROWS",
            ),
            (
                "achievements.json",
                json.dumps([{"action": "create"}] * 201).encode(),
                "ACHIEVEMENT_IMPORT_INVALID_ROWS",
            ),
        )
        for name, content, code in cases:
            with self.subTest(code=code):
                body = self.upload(name, content, expected_status=400)
                self.assertFalse(body["ok"])
                self.assertEqual(body["code"], code)
        with mock.patch.object(server, "MAX_ACHIEVEMENT_IMPORT_FILE_BYTES", 16):
            too_large = self.upload(
                "achievements.json", b" " * 17, expected_status=400
            )
        self.assertEqual(
            too_large["code"], "ACHIEVEMENT_IMPORT_FILE_TOO_LARGE"
        )

    def test_preview_normalizes_base_fields_and_category(self):
        preview = self.preview(
            [
                {
                    "line": 7,
                    "action": "create",
                    "data": {
                        "name": "  筑基里程碑  ",
                        "category": " 里程碑 ",
                        "description": "完成筑基",
                        "coin": "88",
                        "exp": 144,
                        "icon": "golden-core.png",
                        "conditions": "",
                        "duplicate_policy": "",
                    },
                }
            ]
        )
        self.assertTrue(preview["can_execute"])
        row = preview["rows"][0]
        self.assertEqual(row["line"], 7)
        self.assertEqual(row["normalized_data"]["name"], "筑基里程碑")
        self.assertEqual(row["normalized_data"]["category_id"], 7)
        self.assertEqual(row["normalized_data"]["duplicate_policy"], "create")
        self.assertEqual(row["planned_action"]["action"], "create")

    def test_conditions_system_name_and_invalid_icon_block_preview(self):
        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {
                        "name": "有条件",
                        "conditions": "完成任务 10 次",
                    },
                },
                {
                    "line": 2,
                    "action": "create",
                    "data": {"name": "achievement_base_new_player"},
                },
                {
                    "line": 3,
                    "action": "create",
                    "data": {
                        "name": "危险图标",
                        "icon": "javascript:alert(1)",
                    },
                },
            ]
        )
        codes = [
            {error["code"] for error in row["errors"]}
            for row in preview["rows"]
        ]
        self.assertIn("ACHIEVEMENT_CONDITIONS_NOT_SUPPORTED", codes[0])
        self.assertIn("SYSTEM_ACHIEVEMENT_NAME_CONFLICT", codes[1])
        self.assertIn("INVALID_ACHIEVEMENT_ICON", codes[2])
        self.assertFalse(preview["can_execute"])

    def test_system_conflict_never_offers_duplicate_policy_bypass(self):
        preview = self.preview(
            [
                {
                    "line": 8,
                    "action": "create",
                    "data": {"name": "achievement_base_new_player"},
                },
                {
                    "line": 9,
                    "action": "create",
                    "data": {
                        "name": " achievement_base_new_player ",
                        "duplicate_policy": "create",
                    },
                },
            ]
        )
        for row in preview["rows"]:
            codes = {error["code"] for error in row["errors"]}
            self.assertIn("SYSTEM_ACHIEVEMENT_NAME_CONFLICT", codes)
            self.assertNotIn("DUPLICATE_POLICY_REQUIRED", codes)
            self.assertFalse(row.get("duplicate", {}).get("found", False))

    def test_existing_and_file_duplicates_require_row_policy(self):
        preview = self.preview(
            [
                {
                    "line": 2,
                    "action": "create",
                    "data": {"name": "已有成就"},
                },
                {
                    "line": 3,
                    "action": "create",
                    "data": {"name": "同批重复"},
                },
                {
                    "line": 4,
                    "action": "create",
                    "data": {"name": " 同批重复 "},
                },
            ]
        )
        self.assertFalse(preview["can_execute"])
        for row in preview["rows"]:
            self.assertIn(
                "DUPLICATE_POLICY_REQUIRED",
                {error["code"] for error in row["errors"]},
            )
        self.assertEqual(
            preview["rows"][0]["duplicate"]["existing_achievement_ids"],
            [201],
        )
        self.assertEqual(
            preview["rows"][1]["duplicate"]["import_lines"], [3, 4]
        )

    def test_invalid_fields_and_ambiguous_category_are_stable_row_errors(self):
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO userachcategorymodel "
                "(id, categoryname, isdelete, orderincategory) "
                "VALUES (9, '重复分类', 0, 2)"
            )
            connection.execute(
                "INSERT INTO userachcategorymodel "
                "(id, categoryname, isdelete, orderincategory) "
                "VALUES (10, ' 重复分类 ', 0, 3)"
            )
            connection.commit()
        finally:
            connection.close()

        preview = self.preview(
            [
                {
                    "line": 12,
                    "action": "create",
                    "data": {
                        "name": "边界成就",
                        "category": "重复分类",
                        "description": "x" * 2001,
                        "coin": True,
                        "exp": 2_147_483_648,
                        "icon": "data:image/png;base64,AA==",
                        "conditions": ["不支持"],
                        "duplicate_policy": "later",
                    },
                },
                {
                    "line": 13,
                    "action": "update",
                    "data": {"name": "不允许编辑"},
                },
            ]
        )
        first_codes = {error["code"] for error in preview["rows"][0]["errors"]}
        self.assertTrue(
            {
                "AMBIGUOUS_ACHIEVEMENT_CATEGORY",
                "INVALID_ACHIEVEMENT_DESCRIPTION",
                "INVALID_ACHIEVEMENT_COIN",
                "INVALID_ACHIEVEMENT_EXP",
                "INVALID_ACHIEVEMENT_ICON",
                "ACHIEVEMENT_CONDITIONS_NOT_SUPPORTED",
                "INVALID_DUPLICATE_POLICY",
            }.issubset(first_codes),
            first_codes,
        )
        self.assertIn(
            "INVALID_ACTION",
            {error["code"] for error in preview["rows"][1]["errors"]},
        )

    def test_create_and_skip_execute_without_conditions_or_system_changes(self):
        connection = self.connect()
        try:
            before_system = connection.execute(
                "SELECT id, title FROM achievementinfomodel ORDER BY id"
            ).fetchall()
            before_conditions = connection.execute(
                "SELECT id, userachievementid, isdel "
                "FROM unlockconditionmodel ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {
                        "name": "已有成就",
                        "duplicate_policy": "skip",
                    },
                },
                {
                    "line": 2,
                    "action": "create",
                    "data": {
                        "name": "新增成就",
                        "category": "里程碑",
                        "description": "完成一次安全新增",
                        "coin": 88,
                        "exp": 144,
                        "icon": "golden-core.png",
                    },
                },
            ]
        )
        executed = self.execute(preview)
        self.assertEqual(
            executed["summary"],
            {"total": 2, "succeeded": 2, "failed": 0, "affected": 1},
        )
        self.assertEqual(
            executed["rows"][0]["result"],
            {"affected": 0, "skipped": True, "reason": "duplicate"},
        )
        self.assertEqual(executed["rows"][1]["result"], {"affected": 1})

        connection = self.connect()
        try:
            created = connection.execute(
                """
                SELECT content, description, type, categoryid, rewardcoin,
                       expreward, icon, achievementstatus, currentvalue,
                       progress, createtime, updatetime, isdelete, isgotreward,
                       rewardcoinvariable, orderincategory
                FROM userachievementmodel WHERE content='新增成就'
                """
            ).fetchone()
            after_system = connection.execute(
                "SELECT id, title FROM achievementinfomodel ORDER BY id"
            ).fetchall()
            after_conditions = connection.execute(
                "SELECT id, userachievementid, isdel "
                "FROM unlockconditionmodel ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

        self.assertIsNotNone(created)
        self.assertEqual(
            (created["content"], created["description"]),
            ("新增成就", "完成一次安全新增"),
        )
        self.assertEqual(
            (
                created["type"],
                created["categoryid"],
                created["rewardcoin"],
                created["expreward"],
                created["icon"],
            ),
            (0, 7, 88, 144, "golden-core.png"),
        )
        self.assertEqual(
            (
                created["achievementstatus"],
                created["currentvalue"],
                created["progress"],
                created["isdelete"],
                created["isgotreward"],
                created["rewardcoinvariable"],
                created["orderincategory"],
            ),
            (0, 0, 0, 0, 0, 0, 0),
        )
        self.assertEqual(created["createtime"], created["updatetime"])
        self.assertEqual([tuple(row) for row in after_system], [tuple(row) for row in before_system])
        self.assertEqual(
            [tuple(row) for row in after_conditions],
            [tuple(row) for row in before_conditions],
        )
        self.assertEqual(
            len(list(Path(self.snapshot_dir).glob("snapshot-*.zip"))), 1
        )

    def test_database_failure_rolls_back_all_achievement_rows_and_keeps_snapshot(self):
        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {"name": "先创建成就"},
                },
                {
                    "line": 2,
                    "action": "create",
                    "data": {"name": "触发失败成就"},
                },
            ]
        )
        connection = self.connect()
        try:
            connection.executescript(
                """
                CREATE TRIGGER fail_achievement_import
                BEFORE INSERT ON userachievementmodel
                WHEN NEW.content = '触发失败成就'
                BEGIN
                    SELECT RAISE(ABORT, 'private achievement trigger detail');
                END;
                """
            )
            connection.commit()
        finally:
            connection.close()

        failed = self.execute(preview, expected_status=500)
        self.assertEqual(failed["code"], "BATCH_EXECUTION_FAILED")
        self.assertNotIn(
            "private achievement", json.dumps(failed, ensure_ascii=False)
        )
        connection = self.connect()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM userachievementmodel "
                "WHERE content IN ('先创建成就', '触发失败成就')"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)
        self.assertEqual(
            len(list(Path(self.snapshot_dir).glob("snapshot-*.zip"))), 1
        )

    def test_cloud_source_cannot_upload_preview_or_execute_achievement_import(self):
        headers = {"X-LifeUp-Data-Source": "cloud"}
        blocked_upload = self.upload(
            "achievements.json",
            b"[{}]",
            expected_status=403,
            headers=headers,
        )
        self.assertEqual(
            blocked_upload["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE"
        )
        blocked_preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {"name": "云端禁止"},
                }
            ],
            expected_status=403,
            headers=headers,
        )
        self.assertEqual(
            blocked_preview["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE"
        )

        local_preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "create",
                    "data": {"name": "仅本地"},
                }
            ]
        )
        blocked_execute = self.execute(
            local_preview, expected_status=403, headers=headers
        )
        self.assertEqual(
            blocked_execute["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE"
        )
        connection = self.connect()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM userachievementmodel "
                "WHERE content IN ('云端禁止', '仅本地')"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)
        self.assertEqual(list(Path(self.snapshot_dir).glob("snapshot-*.zip")), [])


class AchievementBatchImportUiContractTests(unittest.TestCase):
    def test_achievement_page_uses_upload_preview_duplicate_and_execution_contract(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("成就 CSV/JSON 批量导入", html)
        self.assertIn("/api/local/achievement-import-files", html)
        self.assertIn("/api/local/achievement-import-templates/csv", html)
        self.assertIn("/api/local/achievement-import-templates/json", html)
        self.assertIn("previewAchievementImportRows", html)
        self.assertIn("setAchievementImportDuplicatePolicy", html)
        self.assertIn("existing_achievement_ids", html)
        self.assertNotIn("innerHTML = row.data.name", html)

    def test_achievement_upload_runtime_uses_formdata_preview_and_cloud_guard(self):
        node = (
            os.environ.get("NODE_BINARY")
            or shutil.which("node")
            or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        if not os.path.exists(node):
            self.skipTest("Node.js runtime is unavailable")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('index.html', 'utf8');
let source = '';
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)) {
  source += match[1] + '\n';
}
const noop = () => {};
const classList = { add: noop, remove: noop, toggle: noop, contains: () => false };
const baseElement = {
  classList, style: {}, textContent: '', innerHTML: '', value: '', disabled: false,
  addEventListener: noop, querySelector: () => null, querySelectorAll: () => [],
  setAttribute: noop, focus: noop
};
const file = { name: 'achievements.csv', size: 123 };
const elements = {
  achievementImportFile: { ...baseElement, files: [file] },
  achievementImportStatus: { ...baseElement },
  achievementImportParseBtn: { ...baseElement }
};
const document = {
  body: { classList },
  getElementById: (id) => elements[id] || { ...baseElement },
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: noop,
  createElement: () => ({ ...baseElement })
};
const localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
class MockFormData {
  constructor() { this.entries = []; }
  append(...args) { this.entries.push(args); }
}
const sandbox = {
  console, document, localStorage, FormData: MockFormData,
  window: { addEventListener: noop }, location: { protocol: 'http:' },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  fetch: noop, confirm: () => true, alert: noop, Blob, URL
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

let apiCalls = [];
let previewCall = null;
sandbox.api = (path, options) => {
  apiCalls.push({ path, options });
  return Promise.resolve({
    rows: [{ line: 1, action: 'create', data: { name: '导入成就' } }]
  });
};
sandbox.openLocalBatchPreview = (entity, rows) => {
  previewCall = { entity, rows };
  return Promise.resolve({ ok: true });
};

(async () => {
  await sandbox.parseAchievementImportFile();
  if (apiCalls.length !== 1 || apiCalls[0].path !== '/api/local/achievement-import-files') {
    throw new Error('achievement file did not use its upload endpoint');
  }
  const options = apiCalls[0].options || {};
  if (!(options.body instanceof MockFormData) || options.headers && options.headers['Content-Type']) {
    throw new Error('achievement upload did not preserve browser multipart boundary');
  }
  if (!previewCall || previewCall.entity !== 'achievements' || previewCall.rows.length !== 1) {
    throw new Error('parsed achievement rows did not enter achievement preview');
  }
  sandbox.achievementImportRows = [{ data: { duplicate_policy: '' } }];
  sandbox.setAchievementImportDuplicatePolicy(0, 'skip');
  if (sandbox.achievementImportRows[0].data.duplicate_policy !== 'skip') {
    throw new Error('achievement duplicate policy was not stored');
  }
  sandbox.dataSource = 'cloud';
  if (!sandbox.isCloudReadOnlyWrite('/api/local/achievement-import-files', { method: 'POST' })) {
    throw new Error('cloud mode did not block achievement import upload');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
