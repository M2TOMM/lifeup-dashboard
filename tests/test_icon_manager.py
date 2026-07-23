import base64
import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import server


ROOT = Path(__file__).resolve().parents[1]
PNG_A = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)
PNG_B = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB"
    "9Y9ZP14AAAAASUVORK5CYII="
)
JPEG_A = (
    b"\xff\xd8"
    b"\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03"
    b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    b"\xff\xd9"
)


class IconManagerApiTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.root = tempfile.mkdtemp(prefix="lifeup-icon-manager-")
        self.workspace = os.path.join(self.root, "workspace")
        self.database = os.path.join(
            self.workspace, "databases", "LifeUpDB.db"
        )
        self.snapshot_dir = os.path.join(self.root, "snapshots")
        os.makedirs(os.path.dirname(self.database), exist_ok=True)
        os.makedirs(os.path.join(self.workspace, "media", "download"))
        os.makedirs(os.path.join(self.workspace, "media", "attr"))
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(
                """
                CREATE TABLE taskmodel (
                    id INTEGER PRIMARY KEY
                );
                CREATE TABLE shopitemmodel (
                    id INTEGER PRIMARY KEY,
                    itemname TEXT,
                    icon TEXT,
                    isdel INTEGER NOT NULL
                );
                CREATE TABLE userachievementmodel (
                    id INTEGER PRIMARY KEY,
                    content TEXT,
                    icon TEXT,
                    isdelete INTEGER NOT NULL
                );
                CREATE TABLE skillmodel (
                    id INTEGER PRIMARY KEY,
                    content TEXT,
                    icon TEXT,
                    iconresname TEXT,
                    isdel INTEGER NOT NULL
                );
                CREATE TABLE achievementinfomodel (
                    id INTEGER PRIMARY KEY,
                    title TEXT,
                    icon TEXT
                );
                INSERT INTO taskmodel (id) VALUES (1);
                INSERT INTO shopitemmodel (id, itemname, icon, isdel) VALUES
                    (1, '已有文件商品', 'used.png', 0),
                    (2, '缺失文件商品', 'missing.png', 0),
                    (3, '远程商品', 'https://example.com/remote.png', 0);
                INSERT INTO userachievementmodel
                    (id, content, icon, isdelete) VALUES
                    (11, '缺失文件成就', 'missing.png', 0),
                    (12, '内联成就', 'data:image/png;base64,AAAA', 0);
                INSERT INTO skillmodel
                    (id, content, icon, iconresname, isdel) VALUES
                    (21, '体魄', 'skill.jpg', 'ic_attr_strength_v2_03', 0);
                INSERT INTO achievementinfomodel (id, title, icon) VALUES
                    (31, '系统成就', 'ic_achieve_team');
                """
            )
            connection.commit()
        finally:
            connection.close()

        Path(self.workspace, "media", "download", "used.png").write_bytes(PNG_A)
        Path(self.workspace, "media", "download", "unused.png").write_bytes(PNG_B)
        Path(self.workspace, "media", "download", "broken.png").write_bytes(
            b"not-a-real-png"
        )
        Path(self.workspace, "media", "attr", "skill.jpg").write_bytes(
            JPEG_A
        )

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
        self.assertFalse(os.path.exists(self.root), "临时图标目录没有清理")

    def upload(self, filename, content, expected_status=201, headers=None):
        response = self.client.post(
            "/api/local/icon-files",
            data={"file": (io.BytesIO(content), filename)},
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
            json={"entity": "icons", "rows": rows},
            headers=headers or {},
        )
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def execute(self, preview, expected_status=200, headers=None):
        response = self.client.post(
            f"/api/local/batch-previews/{preview['preview_token']}/executions",
            json={"digest": preview["digest"]},
            headers=headers or {},
        )
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def test_upload_checks_path_size_extension_signature_and_duplicate_names(self):
        first = self.upload("same.png", PNG_A)
        self.assertTrue(first["created"])
        self.assertFalse(first["deduplicated"])
        self.assertRegex(
            first["icon"]["filename"],
            r"^lifeup_dashboard_[0-9a-f]{24}\.png$",
        )
        self.assertEqual(first["icon"]["folder"], "download")
        self.assertEqual(first["icon"]["reference"], first["icon"]["filename"])
        self.assertNotEqual(first["icon"]["filename"], "same.png")

        second = self.upload("same.png", PNG_B)
        self.assertNotEqual(second["icon"]["filename"], first["icon"]["filename"])
        self.assertTrue(
            Path(self.workspace, "media", "download", first["icon"]["filename"]).is_file()
        )
        self.assertTrue(
            Path(self.workspace, "media", "download", second["icon"]["filename"]).is_file()
        )

        duplicate = self.upload("again.png", PNG_A, expected_status=200)
        self.assertFalse(duplicate["created"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["icon"]["filename"], first["icon"]["filename"])

        traversed = self.upload("../escape.png", PNG_A, expected_status=400)
        self.assertEqual(traversed["code"], "ICON_INVALID_FILENAME")
        self.assertFalse(Path(self.root, "escape.png").exists())

        disguised = self.upload(
            "fake.png", b"\xff\xd8\xff\xe0jpeg\xff\xd9", expected_status=400
        )
        self.assertEqual(disguised["code"], "ICON_TYPE_MISMATCH")

        truncated = self.upload("truncated.png", PNG_A[:24], expected_status=400)
        self.assertEqual(truncated["code"], "ICON_TYPE_MISMATCH")

        fake_jpeg = self.upload(
            "fake.jpg", b"\xff\xd8\xff\xe0not-an-image\xff\xd9", expected_status=400
        )
        self.assertEqual(fake_jpeg["code"], "ICON_TYPE_MISMATCH")

        unsupported = self.upload(
            "active.svg", b"<svg onload='alert(1)'></svg>", expected_status=400
        )
        self.assertEqual(unsupported["code"], "ICON_UNSUPPORTED_TYPE")

        with mock.patch.object(server, "MAX_ICON_FILE_BYTES", 8):
            too_large = self.upload("large.png", PNG_A, expected_status=400)
        self.assertEqual(too_large["code"], "ICON_FILE_TOO_LARGE")

    def test_list_audits_referenced_missing_unreferenced_and_invalid_files(self):
        response = self.client.get("/api/local/icons?limit=200&offset=0")
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["summary"]["files"], 4)
        self.assertEqual(body["summary"]["referenced_files"], 2)
        self.assertEqual(body["summary"]["unreferenced_files"], 2)
        self.assertEqual(body["summary"]["invalid_files"], 1)
        self.assertEqual(body["summary"]["missing_references"], 2)

        files = {row["path"]: row for row in body["files"]}
        used = files["media/download/used.png"]
        self.assertTrue(used["referenced"])
        self.assertTrue(used["signature_valid"])
        self.assertEqual(used["references"][0]["entity"], "items")
        self.assertEqual(
            used["media_url"], "/api/media/download/used.png"
        )
        self.assertFalse(files["media/download/unused.png"]["referenced"])
        self.assertFalse(files["media/download/broken.png"]["signature_valid"])
        self.assertTrue(files["media/attr/skill.jpg"]["referenced"])

        groups = {row["reference"]: row for row in body["reference_groups"]}
        self.assertFalse(groups["missing.png"]["exists"])
        self.assertTrue(groups["missing.png"]["editable"])
        self.assertEqual(len(groups["missing.png"]["references"]), 2)
        self.assertNotIn("ic_achieve_team", groups)
        self.assertNotIn("ic_attr_strength_v2_03", groups)
        self.assertNotIn("https://example.com/remote.png", groups)
        self.assertNotIn("data:image/png;base64,AAAA", groups)

        filtered = self.client.get(
            "/api/local/icons?status=unreferenced&search=unused&folder=download"
        ).get_json()
        self.assertEqual(
            [row["path"] for row in filtered["files"]],
            ["media/download/unused.png"],
        )

    def test_export_blocks_bad_icon_references_and_reports_successful_audit(self):
        export_dir = os.path.join(self.root, "exports")
        os.makedirs(export_dir)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE shopitemmodel SET icon='broken.png' WHERE id=1"
            )
            connection.execute(
                "UPDATE shopitemmodel SET icon='used.png' WHERE id=2"
            )
            connection.execute(
                "UPDATE userachievementmodel SET icon='used.png' WHERE id=11"
            )
            connection.commit()
        finally:
            connection.close()

        with mock.patch.object(server, "EXPORT_DIR", export_dir):
            blocked = self.client.post("/api/save", json={})
            self.assertEqual(blocked.status_code, 422, blocked.get_json())
            self.assertEqual(
                blocked.get_json()["code"],
                "ICON_REFERENCE_VALIDATION_FAILED",
            )
            self.assertEqual(list(Path(export_dir).glob("*.zip")), [])

            connection = sqlite3.connect(self.database)
            try:
                connection.execute(
                    "UPDATE shopitemmodel SET icon='used.png' WHERE id=1"
                )
                connection.commit()
            finally:
                connection.close()
            exported = self.client.post("/api/save", json={})

        self.assertEqual(exported.status_code, 200, exported.get_json())
        audit = exported.get_json()["icon_integrity"]
        self.assertEqual(audit["status"], "ok")
        self.assertEqual(audit["direct_references"], 4)
        self.assertEqual(audit["referenced_files"], 2)
        self.assertEqual(audit["missing_references"], 0)
        self.assertEqual(audit["invalid_references"], 0)

    def test_replace_preview_lists_entities_executes_in_one_transaction_and_snapshots(self):
        uploaded = self.upload("replacement.png", PNG_A)
        new_icon = uploaded["icon"]["reference"]
        preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "replace",
                    "data": {
                        "entity_type": "items",
                        "id": 2,
                        "old_icon": "missing.png",
                        "new_icon": new_icon,
                    },
                },
                {
                    "line": 2,
                    "action": "replace",
                    "data": {
                        "entity_type": "achievements",
                        "id": 11,
                        "old_icon": "missing.png",
                        "new_icon": new_icon,
                    },
                },
            ]
        )
        self.assertTrue(preview["can_execute"])
        self.assertEqual(preview["summary"]["ready"], 2)
        self.assertEqual(
            [row["normalized_data"]["entity_name"] for row in preview["rows"]],
            ["缺失文件商品", "缺失文件成就"],
        )
        self.assertEqual(list(Path(self.snapshot_dir).glob("snapshot-*.zip")), [])

        result = self.execute(preview)
        self.assertEqual(result["summary"]["affected"], 2)
        self.assertEqual(len(list(Path(self.snapshot_dir).glob("snapshot-*.zip"))), 1)
        connection = sqlite3.connect(self.database)
        try:
            item_icon = connection.execute(
                "SELECT icon FROM shopitemmodel WHERE id=2"
            ).fetchone()[0]
            achievement_icon = connection.execute(
                "SELECT icon FROM userachievementmodel WHERE id=11"
            ).fetchone()[0]
            system_icon = connection.execute(
                "SELECT icon FROM achievementinfomodel WHERE id=31"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(item_icon, new_icon)
        self.assertEqual(achievement_icon, new_icon)
        self.assertEqual(system_icon, "ic_achieve_team")

    def test_replace_rejects_missing_target_system_entity_and_changed_source(self):
        invalid = self.preview(
            [
                {
                    "line": 1,
                    "action": "replace",
                    "data": {
                        "entity_type": "system_achievements",
                        "id": 31,
                        "old_icon": "ic_achieve_team",
                        "new_icon": "used.png",
                    },
                },
                {
                    "line": 2,
                    "action": "replace",
                    "data": {
                        "entity_type": "items",
                        "id": 2,
                        "old_icon": "missing.png",
                        "new_icon": "absent.png",
                    },
                },
            ]
        )
        codes = [
            {error["code"] for error in row["errors"]}
            for row in invalid["rows"]
        ]
        self.assertIn("ICON_ENTITY_READ_ONLY", codes[0])
        self.assertIn("ICON_TARGET_NOT_FOUND", codes[1])
        self.assertFalse(invalid["can_execute"])

        valid = self.preview(
            [
                {
                    "line": 1,
                    "action": "replace",
                    "data": {
                        "entity_type": "items",
                        "id": 2,
                        "old_icon": "missing.png",
                        "new_icon": "used.png",
                    },
                }
            ]
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE shopitemmodel SET icon='changed-after-preview.png' WHERE id=2"
            )
            connection.commit()
        finally:
            connection.close()
        changed = self.execute(valid, expected_status=409)
        self.assertEqual(changed["code"], "BATCH_TARGET_CHANGED")
        self.assertEqual(len(list(Path(self.snapshot_dir).glob("snapshot-*.zip"))), 1)

    def test_cloud_source_cannot_list_upload_preview_or_execute_icons(self):
        headers = {"X-LifeUp-Data-Source": "cloud"}
        listed = self.client.get("/api/local/icons", headers=headers)
        self.assertEqual(listed.status_code, 403)
        self.assertEqual(
            listed.get_json()["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE"
        )
        uploaded = self.upload(
            "cloud.png", PNG_A, expected_status=403, headers=headers
        )
        self.assertEqual(uploaded["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE")
        blocked_preview = self.preview(
            [
                {
                    "line": 1,
                    "action": "replace",
                    "data": {
                        "entity_type": "items",
                        "id": 1,
                        "old_icon": "used.png",
                        "new_icon": "unused.png",
                    },
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
                    "action": "replace",
                    "data": {
                        "entity_type": "items",
                        "id": 1,
                        "old_icon": "used.png",
                        "new_icon": "unused.png",
                    },
                }
            ]
        )
        blocked_execute = self.execute(
            local_preview, expected_status=403, headers=headers
        )
        self.assertEqual(
            blocked_execute["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE"
        )


class IconManagerUiContractTests(unittest.TestCase):
    def test_icon_manager_has_local_navigation_upload_audit_and_preview_contract(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        for marker in (
            'data-page="icons"',
            "图标资源管理器",
            "/api/local/icons",
            "/api/local/icon-files",
            "loadIcons",
            "uploadIconFile",
            "previewIconReplacement",
            "openLocalBatchPreview('icons'",
            "无直接文件名引用",
            "escHtml(String(summary.missing_references || 0))",
        ):
            self.assertIn(marker, html)
        self.assertNotIn("innerHTML = icon.filename", html)

    def test_icon_upload_runtime_uses_formdata_and_cloud_guard(self):
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
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)) source += match[1] + '\n';
const noop = () => {};
const classList = { add: noop, remove: noop, toggle: noop, contains: () => false };
const baseElement = { classList, style: {}, textContent: '', innerHTML: '', value: '', disabled: false,
  addEventListener: noop, querySelector: () => null, querySelectorAll: () => [], setAttribute: noop, focus: noop };
const file = { name: 'icon.png', size: 123 };
const elements = { iconUploadFile: { ...baseElement, files: [file] }, iconUploadButton: { ...baseElement } };
const document = { body: { classList }, getElementById: id => elements[id] || { ...baseElement },
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, createElement: () => ({ ...baseElement }) };
const localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
class MockFormData { constructor() { this.entries = []; } append(...args) { this.entries.push(args); } }
const sandbox = { console, document, localStorage, FormData: MockFormData, window: { addEventListener: noop },
  location: { protocol: 'http:' }, setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  fetch: noop, confirm: () => true, alert: noop, Blob, URL };
sandbox.window.window = sandbox.window; sandbox.window.document = document; sandbox.window.localStorage = localStorage;
vm.createContext(sandbox); vm.runInContext(source, sandbox);
let request = null;
sandbox.api = (path, options) => { request = { path, options }; return Promise.resolve({ ok: true, icon: { filename: 'generated.png' } }); };
sandbox.loadIcons = () => Promise.resolve();
(async () => {
  await sandbox.uploadIconFile();
  if (!request || request.path !== '/api/local/icon-files') throw new Error('wrong icon upload endpoint');
  if (!(request.options.body instanceof MockFormData)) throw new Error('icon upload did not use FormData');
  if (request.options.headers && request.options.headers['Content-Type']) throw new Error('multipart boundary was overridden');
  sandbox.dataSource = 'cloud';
  if (!sandbox.isCloudReadOnlyWrite('/api/local/icon-files', { method: 'POST' })) throw new Error('cloud upload was not guarded');
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
