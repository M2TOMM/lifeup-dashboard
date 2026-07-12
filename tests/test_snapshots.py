import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]


class SnapshotApiTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.root = tempfile.mkdtemp(prefix="lifeup-snapshot-test-")
        self.workspace = os.path.join(self.root, "workspace")
        self.snapshot_dir = os.path.join(self.root, "workspaces", "snapshots")
        self.restore_dir = os.path.join(self.root, "workspaces", "restores")
        os.makedirs(os.path.join(self.workspace, "databases"))

        self.database = os.path.join(self.workspace, "databases", "LifeUpDB.db")
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE taskmodel (id INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE shopitemmodel (id INTEGER PRIMARY KEY, itemname TEXT);
            CREATE TABLE userachievementmodel (id INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE snapshot_marker (value TEXT NOT NULL);
            INSERT INTO taskmodel(content) VALUES ('task-before');
            INSERT INTO shopitemmodel(itemname) VALUES ('item-before');
            INSERT INTO userachievementmodel(content) VALUES ('achievement-before');
            INSERT INTO snapshot_marker(value) VALUES ('before');
            """
        )
        connection.commit()
        connection.close()

        self.current_source = os.path.join(self.root, "current-workspace.zip")
        self.original_backup = os.path.join(self.root, "original-do-not-touch.zip")
        self._write_workspace_zip(self.current_source)
        shutil.copyfile(self.current_source, self.original_backup)

        self._patches = [
            patch.object(server, "SNAPSHOT_DIR", self.snapshot_dir, create=True),
            patch.object(server, "RESTORE_DIR", self.restore_dir, create=True),
            patch.object(server, "ORIGINAL_BACKUP_PATH", self.original_backup, create=True),
            patch.object(
                server,
                "PROTECTED_BACKUP_PATHS",
                {
                    server._canonical_path(self.original_backup),
                    server._canonical_path(self.current_source),
                },
            ),
        ]
        for active_patch in self._patches:
            active_patch.start()

        server.STATE.update(
            {
                "backup_path": self.current_source,
                "db_path": self.database,
                "tmpdir": self.workspace,
                "loaded": True,
            }
        )
        self.client = server.app.test_client()

    def tearDown(self):
        active_tmpdir = server.STATE.get("tmpdir")
        server.STATE.clear()
        server.STATE.update(self._old_state)
        for active_patch in reversed(self._patches):
            active_patch.stop()
        if (
            active_tmpdir
            and active_tmpdir != self.workspace
            and active_tmpdir != self._old_state.get("tmpdir")
        ):
            shutil.rmtree(active_tmpdir, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_workspace_zip(self, destination):
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(self.database, server.DB_INTERNAL)

    @staticmethod
    def _fingerprint(path):
        stat_result = os.stat(path)
        with open(path, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
        return digest, stat_result.st_size, stat_result.st_mtime_ns

    @staticmethod
    def _snapshot_path(snapshot_dir, snapshot_id):
        return os.path.join(snapshot_dir, f"snapshot-{snapshot_id}.zip")

    @staticmethod
    def _database_from_archive(archive_path):
        extracted = tempfile.mkdtemp(prefix="lifeup-snapshot-assert-")
        with zipfile.ZipFile(archive_path, "r") as archive:
            archive.extract(server.DB_INTERNAL, extracted)
        return extracted, os.path.join(extracted, *server.DB_INTERNAL.split("/"))

    @classmethod
    def _marker_from_archive(cls, archive_path):
        extracted, database_path = cls._database_from_archive(archive_path)
        try:
            connection = sqlite3.connect(database_path)
            try:
                return connection.execute("SELECT value FROM snapshot_marker").fetchone()[0]
            finally:
                connection.close()
        finally:
            shutil.rmtree(extracted, ignore_errors=True)

    def _create_snapshot(self, name="修改前"):
        response = self.client.post("/api/snapshots", json={"name": name})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["snapshot"]

    def _assert_state_unchanged(self, expected):
        for key in ("backup_path", "db_path", "tmpdir", "loaded"):
            self.assertEqual(server.STATE.get(key), expected.get(key), key)
        self.assertTrue(os.path.exists(expected["db_path"]))

    def _assert_file_fingerprints_unchanged(self, expected):
        for path, fingerprint in expected.items():
            self.assertTrue(os.path.isfile(path), path)
            self.assertEqual(self._fingerprint(path), fingerprint, path)

    def test_create_snapshot_requires_loaded_workspace(self):
        server.STATE.update(
            {"backup_path": None, "db_path": None, "tmpdir": None, "loaded": False}
        )

        response = self.client.post("/api/snapshots", json={"name": "empty"})

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["code"], "NO_BACKUP_LOADED")
        self.assertTrue(response.get_json()["suggestion"])
        self.assertFalse(os.path.exists(self.snapshot_dir))

    def test_create_list_and_independence_use_real_server_zip(self):
        original_before = self._fingerprint(self.original_backup)
        source_before = self._fingerprint(self.current_source)

        snapshot = self._create_snapshot("稳定基线")

        self.assertRegex(snapshot["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(snapshot["name"], "稳定基线")
        self.assertNotIn("path", snapshot)
        self.assertTrue(snapshot["created_at"])
        self.assertEqual(snapshot["integrity"]["archive"], "ok")
        self.assertEqual(snapshot["integrity"]["database"], "ok")
        self.assertEqual(
            set(snapshot["integrity"]["counts"]), set(server.KEY_ENTITY_TABLES)
        )

        snapshot_path = self._snapshot_path(self.snapshot_dir, snapshot["id"])
        self.assertTrue(os.path.isfile(snapshot_path))
        self.assertEqual(snapshot["size"], os.path.getsize(snapshot_path))
        with zipfile.ZipFile(snapshot_path, "r") as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(server.DB_INTERNAL, archive.namelist())
        snapshot_hash = self._fingerprint(snapshot_path)

        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE snapshot_marker SET value='after'")
        connection.execute("INSERT INTO taskmodel(content) VALUES ('task-after')")
        connection.commit()
        connection.close()

        self.assertEqual(self._fingerprint(snapshot_path), snapshot_hash)
        self.assertEqual(self._marker_from_archive(snapshot_path), "before")

        response = self.client.get("/api/snapshots?limit=10&offset=0")
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["pagination"]["total"], 1)
        self.assertEqual(payload["snapshots"][0]["id"], snapshot["id"])
        self.assertNotIn("path", payload["snapshots"][0])

        self.assertEqual(self._fingerprint(self.original_backup), original_before)
        self.assertEqual(self._fingerprint(self.current_source), source_before)

    def test_create_response_does_not_require_a_post_publish_metadata_rescan(self):
        with patch.object(
            server,
            "_read_snapshot_metadata",
            side_effect=AssertionError("post-publish metadata rescan"),
        ):
            response = self.client.post(
                "/api/snapshots", json={"name": "single-validation-pass"}
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        snapshot = response.get_json()["snapshot"]
        self.assertEqual(snapshot["name"], "single-validation-pass")
        snapshot_path = self._snapshot_path(self.snapshot_dir, snapshot["id"])
        self.assertTrue(os.path.isfile(snapshot_path))
        metadata = server._read_snapshot_metadata(snapshot_path, snapshot["id"])
        self.assertEqual(metadata["name"], "single-validation-pass")
        self.assertEqual(metadata["integrity"], snapshot["integrity"])

    def test_create_rejects_untrusted_fields_bad_names_and_missing_key_tables(self):
        bad_payloads = (
            {"name": [], "expected": "INVALID_SNAPSHOT_NAME"},
            {"name": "x" * 101, "expected": "INVALID_SNAPSHOT_NAME"},
            {"name": "bad\nname", "expected": "INVALID_SNAPSHOT_NAME"},
            {"name": "ok", "path": self.original_backup, "expected": "INVALID_REQUEST"},
        )
        for case in bad_payloads:
            with self.subTest(case=case):
                expected = case["expected"]
                payload = {key: value for key, value in case.items() if key != "expected"}
                response = self.client.post("/api/snapshots", json=payload)
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(response.get_json()["code"], expected)

        connection = sqlite3.connect(self.database)
        connection.execute("DROP TABLE userachievementmodel")
        connection.commit()
        connection.close()
        response = self.client.post("/api/snapshots", json={"name": "missing-table"})
        self.assertEqual(response.status_code, 422, response.get_json())
        self.assertEqual(response.get_json()["code"], "SNAPSHOT_VALIDATION_FAILED")
        self.assertEqual(
            list(Path(self.snapshot_dir).glob("*.zip")) if os.path.isdir(self.snapshot_dir) else [],
            [],
        )

    def test_snapshot_id_collision_never_overwrites_existing_file(self):
        os.makedirs(self.snapshot_dir)
        occupied_id = "1" * 32
        fresh_id = "2" * 32
        occupied_path = self._snapshot_path(self.snapshot_dir, occupied_id)
        with open(occupied_path, "wb") as occupied:
            occupied.write(b"existing-snapshot-must-stay")

        with patch.object(server.secrets, "token_hex", side_effect=[occupied_id, fresh_id]):
            snapshot = self._create_snapshot("collision-safe")

        self.assertEqual(snapshot["id"], fresh_id)
        with open(occupied_path, "rb") as occupied:
            self.assertEqual(occupied.read(), b"existing-snapshot-must-stay")

    def test_snapshot_publish_race_never_overwrites_new_target(self):
        race_id = "3" * 32
        race_path = self._snapshot_path(self.snapshot_dir, race_id)
        source_before = self._fingerprint(self.current_source)
        original_before = self._fingerprint(self.original_backup)
        original_link = os.link

        def occupy_at_publish(_source, target):
            self.assertEqual(target, race_path)
            with open(target, "wb") as canary:
                canary.write(b"appeared-after-allocation")
            return original_link(_source, target)

        with patch.object(server.secrets, "token_hex", return_value=race_id), patch.object(
            server.os, "link", side_effect=occupy_at_publish
        ):
            response = self.client.post(
                "/api/snapshots", json={"name": "publish-race"}
            )

        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["code"], "SNAPSHOT_ID_CONFLICT")
        with open(race_path, "rb") as canary:
            self.assertEqual(canary.read(), b"appeared-after-allocation")
        self.assertEqual(self._fingerprint(self.current_source), source_before)
        self.assertEqual(self._fingerprint(self.original_backup), original_before)

    def test_restore_creates_new_managed_source_and_reverts_workspace(self):
        original_before = self._fingerprint(self.original_backup)
        source_before = self._fingerprint(self.current_source)
        snapshot = self._create_snapshot()
        snapshot_path = self._snapshot_path(self.snapshot_dir, snapshot["id"])
        snapshot_before = self._fingerprint(snapshot_path)

        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE snapshot_marker SET value='modified-after-snapshot'")
        connection.commit()
        connection.close()

        response = self.client.post(f"/api/snapshots/{snapshot['id']}/restore")

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        workspace = payload["workspace"]
        self.assertTrue(workspace["loaded"])
        self.assertTrue(workspace["workspace_copy"])
        restored_source = workspace["backup_path"]
        self.assertEqual(server.STATE["backup_path"], restored_source)
        self.assertEqual(
            os.path.commonpath([os.path.abspath(restored_source), os.path.abspath(self.restore_dir)]),
            os.path.abspath(self.restore_dir),
        )
        for protected in (snapshot_path, self.current_source, self.original_backup):
            self.assertFalse(server._paths_refer_to_same_file(restored_source, protected))

        connection = sqlite3.connect(server.STATE["db_path"])
        try:
            marker = connection.execute("SELECT value FROM snapshot_marker").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(marker, "before")
        self.assertEqual(self._fingerprint(snapshot_path), snapshot_before)
        self.assertEqual(self._fingerprint(self.current_source), source_before)
        self.assertEqual(self._fingerprint(self.original_backup), original_before)

    def test_restore_publish_race_never_overwrites_new_target(self):
        snapshot = self._create_snapshot("restore-race")
        snapshot_path = self._snapshot_path(self.snapshot_dir, snapshot["id"])
        baseline_state = dict(server.STATE)
        protected_before = {
            path: self._fingerprint(path)
            for path in (snapshot_path, self.current_source, self.original_backup)
        }
        original_link = os.link
        published_targets = []

        def occupy_at_publish(_source, target):
            published_targets.append(target)
            with open(target, "wb") as canary:
                canary.write(b"appeared-after-allocation")
            return original_link(_source, target)

        with patch.object(server.os, "link", side_effect=occupy_at_publish):
            response = self.client.post(
                f"/api/snapshots/{snapshot['id']}/restore"
            )

        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["code"], "RESTORE_ID_CONFLICT")
        self.assertEqual(len(published_targets), 1)
        race_path = published_targets[0]
        with open(race_path, "rb") as canary:
            self.assertEqual(canary.read(), b"appeared-after-allocation")
        self._assert_state_unchanged(baseline_state)
        for path, fingerprint in protected_before.items():
            self.assertEqual(self._fingerprint(path), fingerprint, path)

    def test_missing_corrupt_or_corrupted_copy_cannot_switch_state(self):
        protected_before = {
            path: self._fingerprint(path)
            for path in (self.current_source, self.original_backup)
        }
        snapshot = self._create_snapshot()
        snapshot_path = self._snapshot_path(self.snapshot_dir, snapshot["id"])
        baseline_state = dict(server.STATE)

        os.remove(snapshot_path)
        response = self.client.post(f"/api/snapshots/{snapshot['id']}/restore")
        self.assertEqual(response.status_code, 404, response.get_json())
        self.assertEqual(response.get_json()["code"], "SNAPSHOT_NOT_FOUND")
        self._assert_state_unchanged(baseline_state)
        self._assert_file_fingerprints_unchanged(protected_before)

        snapshot = self._create_snapshot("corrupt")
        snapshot_path = self._snapshot_path(self.snapshot_dir, snapshot["id"])
        with open(snapshot_path, "wb") as output:
            output.write(b"not-a-zip")
        response = self.client.post(f"/api/snapshots/{snapshot['id']}/restore")
        self.assertEqual(response.status_code, 422, response.get_json())
        self.assertEqual(response.get_json()["code"], "SNAPSHOT_VALIDATION_FAILED")
        self._assert_state_unchanged(baseline_state)
        self._assert_file_fingerprints_unchanged(protected_before)

        snapshot = self._create_snapshot("copy-corruption")

        def write_corrupt_copy(_source, output, *args, **kwargs):
            output.write(b"copy-was-corrupted")

        with patch.object(server.shutil, "copyfileobj", side_effect=write_corrupt_copy):
            response = self.client.post(f"/api/snapshots/{snapshot['id']}/restore")
        self.assertEqual(response.status_code, 422, response.get_json())
        self.assertEqual(response.get_json()["code"], "SNAPSHOT_VALIDATION_FAILED")
        self._assert_state_unchanged(baseline_state)
        self._assert_file_fingerprints_unchanged(protected_before)
        if os.path.isdir(self.restore_dir):
            self.assertEqual(os.listdir(self.restore_dir), [])

    def test_delete_accepts_only_managed_ids_and_removes_only_selected_snapshot(self):
        first = self._create_snapshot("first")
        second = self._create_snapshot("second")
        first_path = self._snapshot_path(self.snapshot_dir, first["id"])
        second_path = self._snapshot_path(self.snapshot_dir, second["id"])
        baseline_state = dict(server.STATE)
        canary = os.path.join(self.root, "outside-canary.txt")
        with open(canary, "wb") as output:
            output.write(b"do-not-delete")

        for invalid_id in ("..%5Coutside-canary", "C:%5Ctemp%5Coutside", first["filename"]):
            with self.subTest(invalid_id=invalid_id):
                response = self.client.delete(f"/api/snapshots/{invalid_id}")
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(response.get_json()["code"], "INVALID_SNAPSHOT_ID")
                self.assertTrue(os.path.exists(first_path))
                self.assertTrue(os.path.exists(second_path))
                self.assertTrue(os.path.exists(canary))

        response = self.client.delete(f"/api/snapshots/{first['id']}")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["id"], first["id"])
        self.assertFalse(os.path.exists(first_path))
        self.assertTrue(os.path.exists(second_path))
        self._assert_state_unchanged(baseline_state)

        response = self.client.delete(f"/api/snapshots/{first['id']}")
        self.assertEqual(response.status_code, 404, response.get_json())
        self.assertEqual(response.get_json()["code"], "SNAPSHOT_NOT_FOUND")

    def test_delete_unexpected_error_returns_stable_json(self):
        snapshot = self._create_snapshot("delete-error")
        snapshot_path = self._snapshot_path(self.snapshot_dir, snapshot["id"])
        snapshot_before = self._fingerprint(snapshot_path)

        with patch.object(
            server, "delete_snapshot", side_effect=RuntimeError("unexpected")
        ):
            with self.assertLogs(server.app.logger, level="ERROR") as captured:
                response = self.client.delete(f"/api/snapshots/{snapshot['id']}")

        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertTrue(response.is_json)
        self.assertEqual(response.get_json()["code"], "SNAPSHOT_DELETE_FAILED")
        self.assertTrue(response.get_json()["suggestion"])
        self.assertIn("Unexpected snapshot delete error", captured.output[0])
        self.assertEqual(self._fingerprint(snapshot_path), snapshot_before)

    def test_cloud_source_rejects_snapshot_writes(self):
        cloud_headers = {"X-LifeUp-Data-Source": "cloud"}
        response = self.client.post(
            "/api/snapshots", json={"name": "cloud"}, headers=cloud_headers
        )
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE")

        snapshot = self._create_snapshot()
        snapshot_path = self._snapshot_path(self.snapshot_dir, snapshot["id"])
        for method, url in (
            (self.client.post, f"/api/snapshots/{snapshot['id']}/restore"),
            (self.client.delete, f"/api/snapshots/{snapshot['id']}"),
        ):
            response = method(url, headers=cloud_headers)
            self.assertEqual(response.status_code, 403, response.get_json())
            self.assertEqual(
                response.get_json()["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE"
            )
            self.assertTrue(os.path.exists(snapshot_path))

    def test_cloud_source_cannot_list_local_snapshot_metadata(self):
        snapshot = self._create_snapshot("local-only")
        snapshot_path = self._snapshot_path(self.snapshot_dir, snapshot["id"])

        response = self.client.get(
            "/api/snapshots?limit=10&offset=0",
            headers={"X-LifeUp-Data-Source": "cloud"},
        )

        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(
            response.get_json()["code"], "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE"
        )
        self.assertNotIn("snapshots", response.get_json())
        self.assertTrue(os.path.exists(snapshot_path))

    def test_list_pagination_validation_is_bounded(self):
        for query in ("limit=0", "limit=201", "limit=abc", "offset=-1"):
            with self.subTest(query=query):
                response = self.client.get(f"/api/snapshots?{query}")
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(response.get_json()["code"], "INVALID_PAGINATION")


class SnapshotBrowserContractTests(unittest.TestCase):
    def test_real_apply_loaded_status_updates_the_active_workspace_ui(self):
        node = (
            os.environ.get("NODE_BINARY")
            or shutil.which("node")
            or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        if not os.path.exists(node):
            self.skipTest("Node.js is unavailable")

        script = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('index.html', 'utf8');
const fullSource = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const start = fullSource.indexOf('function updateLoadStatus(');
const end = fullSource.indexOf('async function restoreLoadedWorkspaceAfterFailure(', start);
if (start < 0 || end < 0) throw new Error('loaded status helpers not found');
const source = fullSource.slice(start, end);

const elements = {
  connText: { textContent: '' },
  connDot: { className: 'conn-dot' },
  saveBtn: { disabled: true },
  filePath: { textContent: '', title: '' }
};
const stored = [];
const history = [];
let reloads = 0;
const sandbox = {
  console,
  IS_DESKTOP: false,
  document: { getElementById: (id) => elements[id] || null },
  localStorage: { setItem: (key, value) => stored.push({ key, value }) },
  addToLocalHistory: (path) => history.push(path),
  loadCurrentPage: () => { reloads += 1; }
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

const backupPath = 'C:\\workspaces\\restores\\restored.zip';
const applied = sandbox.applyLoadedStatus({
  loaded: true,
  backup_path: backupPath,
  filename: 'restored.zip',
  workspace_copy: true
});
if (applied !== true) throw new Error('real helper rejected a loaded workspace');
if (elements.connText.textContent !== '已加载' || elements.connDot.className !== 'conn-dot online') {
  throw new Error('connection status was not updated');
}
if (elements.saveBtn.disabled !== false) throw new Error('save button stayed disabled');
if (elements.filePath.textContent !== 'restored.zip' || elements.filePath.title !== backupPath) {
  throw new Error('workspace path display was not updated');
}
if (!stored.some((entry) => entry.key === 'lifeup_backup_path' && entry.value === backupPath)) {
  throw new Error('restored workspace path was not persisted');
}
if (history.length !== 1 || history[0] !== backupPath) {
  throw new Error('restored workspace was not added to local history');
}
if (reloads !== 1) throw new Error('current page was not refreshed after restore');
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_snapshot_page_uses_server_ids_safe_text_and_new_restore_endpoint(self):
        node = (
            os.environ.get("NODE_BINARY")
            or shutil.which("node")
            or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        if not os.path.exists(node):
            self.skipTest("Node.js is unavailable")

        script = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('index.html', 'utf8');
const fullSource = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const marker = fullSource.indexOf('PAGE 13: Snapshots');
const nextMarker = fullSource.indexOf('PAGE 14:', marker);
const start = fullSource.lastIndexOf('/*', marker);
const end = fullSource.lastIndexOf('/*', nextMarker);
if (marker < 0 || nextMarker < 0 || start < 0 || end < 0) throw new Error('snapshot source not found');
const source = fullSource.slice(start, end);
const escStart = fullSource.indexOf('function escHtml(');
const escEnd = fullSource.indexOf('function escAttr(', escStart);
if (escStart < 0 || escEnd < 0) throw new Error('real escHtml helper not found');
const escSource = fullSource.slice(escStart, escEnd);
if (source.includes('lifeup_snapshots')) throw new Error('browser-local fake snapshots remain');
if (source.includes("'/api/open'") || source.includes('_loadBackup(')) {
  throw new Error('snapshot restore still uses an arbitrary path loader');
}

const content = { innerHTML: '' };
const elements = {
  content,
  f_snapname: { value: 'Browser snapshot' },
  snapshotCreateConfirmBtn: { disabled: false, textContent: '📸 创建' }
};
const requests = [];
const applied = [];
const toasts = [];
const localStorage = new Proxy({}, { get() { throw new Error('snapshot page touched localStorage'); } });
const sandbox = {
  console, localStorage,
  document: { getElementById: (id) => elements[id] || null },
  currentPage: 'snapshots', dataSource: 'local',
  setTimeout: (fn) => fn(), clearTimeout: () => {},
  confirm: () => true,
  showModal: () => {}, closeModal: () => {},
  toast: (message, type) => toasts.push({ message, type }),
  applyLoadedStatus: (status) => { applied.push(status); return true; },
  cloudReadOnlyGuard: () => sandbox.dataSource === 'cloud',
  api: async (path, options = {}) => {
    requests.push({ path, options });
    if (path.startsWith('/api/snapshots?')) return {
      snapshots: [{
        id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        name: '<img src=x onerror=alert(1)>',
        filename: 'evil<script>.zip',
        created_at: '<b>today</b>', size: '<svg/onload=alert(4)>', status: 'ready', restorable: true,
        integrity: { archive: 'ok', database: 'ok', counts: {} }
      }, {
        id: 'cccccccccccccccccccccccccccccccc',
        name: 'invalid snapshot', filename: 'invalid.zip',
        created_at: '2026-07-12T08:00:00+08:00', size: 10,
        status: '<iframe srcdoc=alert(5)>', restorable: false,
        integrity: {
          archive: '<object data=javascript:alert(6)>',
          database: '<video onerror=alert(7)>',
          counts: { taskmodel: '<details open ontoggle=alert(8)>' }
        }
      }],
      count: '<marquee onstart=alert(9)>',
      pagination: { total: '<math onmouseover=alert(10)>', limit: 200, offset: 0 }
    };
    if (path.endsWith('/restore')) return {
      ok: true,
      workspace: {
        loaded: true, backup_path: 'C:\\workspaces\\restores\\restored.zip',
        filename: 'restored.zip', workspace_copy: true
      }
    };
    if (options.method === 'DELETE') return { ok: true };
    if (options.method === 'POST') return { ok: true, snapshot: { id: 'b'.repeat(32) } };
    throw new Error('unexpected request: ' + path);
  }
};
vm.createContext(sandbox);
vm.runInContext(escSource, sandbox);
vm.runInContext(source, sandbox);

(async () => {
  await sandbox.loadSnapshots();
  for (const rawTag of ['<img', '<script', '<b>', '<svg', '<iframe', '<object', '<video', '<details', '<marquee', '<math']) {
    if (content.innerHTML.includes(rawTag)) {
      throw new Error('untrusted snapshot metadata reached innerHTML: ' + rawTag);
    }
  }
  for (const escapedText of ['&lt;img', '&lt;script&gt;', '&lt;b&gt;today&lt;/b&gt;', '&lt;svg/onload', '&lt;iframe']) {
    if (!content.innerHTML.includes(escapedText)) {
      throw new Error('rendered snapshot metadata was not safely escaped: ' + escapedText);
    }
  }
  if (!content.innerHTML.includes('共 2 个服务器快照')) {
    throw new Error('invalid pagination total did not fall back to the safe row count');
  }

  await sandbox.restoreSnapshot(0);
  const restoreRequest = requests.find((entry) => entry.path === '/api/snapshots/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/restore');
  if (!restoreRequest) {
    throw new Error('restore did not use the managed snapshot id endpoint');
  }
  if (restoreRequest.options.method !== 'POST' || restoreRequest.options.body !== '{}') {
    throw new Error('restore request method or body is incorrect');
  }
  if (requests.some((entry) => entry.path === '/api/open')) throw new Error('restore used /api/open');
  if (applied.length !== 1 || !applied[0].workspace_copy) {
    throw new Error('restored workspace was not applied');
  }

  requests.length = 0;
  await sandbox.doCreateSnapshot();
  const createRequest = requests.find((entry) => entry.path === '/api/snapshots');
  if (!createRequest || createRequest.options.method !== 'POST') {
    throw new Error('create did not use POST /api/snapshots');
  }
  const createBody = JSON.parse(createRequest.options.body || 'null');
  if (!createBody || createBody.name !== 'Browser snapshot') {
    throw new Error('create request body did not contain the snapshot name');
  }

  requests.length = 0;
  await sandbox.deleteSnapshot(0);
  const deleteRequest = requests.find((entry) => entry.path === '/api/snapshots/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  if (!deleteRequest || deleteRequest.options.method !== 'DELETE' || deleteRequest.options.body != null) {
    throw new Error('delete did not use the managed snapshot id and DELETE method');
  }

  requests.length = 0;
  sandbox.dataSource = 'cloud';
  await sandbox.loadSnapshots();
  sandbox.createSnapshot();
  await sandbox.doCreateSnapshot();
  await sandbox.restoreSnapshot(0);
  await sandbox.deleteSnapshot(0);
  if (requests.length !== 0) throw new Error('cloud mode requested or changed local snapshots');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_create_completion_does_not_overwrite_page_after_navigation(self):
        node = (
            os.environ.get("NODE_BINARY")
            or shutil.which("node")
            or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        if not os.path.exists(node):
            self.skipTest("Node.js is unavailable")

        script = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('index.html', 'utf8');
const fullSource = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const marker = fullSource.indexOf('PAGE 13: Snapshots');
const nextMarker = fullSource.indexOf('PAGE 14:', marker);
const start = fullSource.lastIndexOf('/*', marker);
const end = fullSource.lastIndexOf('/*', nextMarker);
const source = fullSource.slice(start, end);

const content = { innerHTML: 'SNAPSHOT_PAGE' };
const input = { value: 'Async snapshot' };
const createButton = { disabled: false, textContent: '📸 创建' };
const elements = {
  content,
  f_snapname: input,
  snapshotCreateConfirmBtn: createButton
};
const requests = [];
let resolveCreate;
const sandbox = {
  console,
  document: { getElementById: (id) => elements[id] || null },
  currentPage: 'snapshots', dataSource: 'local',
  setTimeout: (fn) => fn(), clearTimeout: () => {},
  confirm: () => true,
  closeModal: () => {},
  toast: () => {},
  cloudReadOnlyGuard: () => false,
  escHtml: (value) => String(value == null ? '' : value)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
  api: (path, options = {}) => {
    requests.push({ path, options });
    if (path === '/api/snapshots' && options.method === 'POST') {
      return new Promise((resolve) => { resolveCreate = resolve; });
    }
    if (path.startsWith('/api/snapshots?')) {
      return Promise.resolve({ snapshots: [], pagination: { total: 0 } });
    }
    return Promise.reject(new Error('unexpected request: ' + path));
  }
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

(async () => {
  const operation = sandbox.doCreateSnapshot();
  if (typeof resolveCreate !== 'function') throw new Error('create request did not start');
  sandbox.currentPage = 'tasks';
  content.innerHTML = 'TASK_PAGE_SENTINEL';
  resolveCreate({ ok: true, snapshot: { id: 'b'.repeat(32), name: 'Async snapshot' } });
  await operation;
  if (content.innerHTML !== 'TASK_PAGE_SENTINEL') {
    throw new Error('create completion overwrote the page selected during the request');
  }
  if (requests.some((entry) => entry.path.startsWith('/api/snapshots?'))) {
    throw new Error('create completion refreshed snapshots after leaving the page');
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_delete_completion_does_not_overwrite_page_after_navigation(self):
        node = (
            os.environ.get("NODE_BINARY")
            or shutil.which("node")
            or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        if not os.path.exists(node):
            self.skipTest("Node.js is unavailable")

        script = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('index.html', 'utf8');
const fullSource = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const marker = fullSource.indexOf('PAGE 13: Snapshots');
const nextMarker = fullSource.indexOf('PAGE 14:', marker);
const start = fullSource.lastIndexOf('/*', marker);
const end = fullSource.lastIndexOf('/*', nextMarker);
const source = fullSource.slice(start, end);

const content = { innerHTML: '' };
const requests = [];
let resolveDelete;
const listPayload = {
  snapshots: [{
    id: 'a'.repeat(32), name: 'Delete later', filename: 'snapshot-' + 'a'.repeat(32) + '.zip',
    created_at: '2026-07-12T08:00:00+08:00', size: 2048,
    status: 'ready', restorable: true,
    integrity: { archive: 'ok', database: 'ok', counts: {} }
  }],
  pagination: { total: 1 }
};
const sandbox = {
  console,
  document: { getElementById: (id) => id === 'content' ? content : null },
  currentPage: 'snapshots', dataSource: 'local',
  confirm: () => true,
  toast: () => {},
  cloudReadOnlyGuard: () => false,
  escHtml: (value) => String(value == null ? '' : value)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
  api: (path, options = {}) => {
    requests.push({ path, options });
    if (path.startsWith('/api/snapshots?')) return Promise.resolve(listPayload);
    if (path === '/api/snapshots/' + 'a'.repeat(32) && options.method === 'DELETE') {
      return new Promise((resolve) => { resolveDelete = resolve; });
    }
    return Promise.reject(new Error('unexpected request: ' + path));
  }
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

(async () => {
  await sandbox.loadSnapshots();
  requests.length = 0;
  const operation = sandbox.deleteSnapshot(0);
  if (typeof resolveDelete !== 'function') throw new Error('delete request did not start');
  sandbox.currentPage = 'tasks';
  content.innerHTML = 'TASK_PAGE_SENTINEL';
  resolveDelete({ ok: true, id: 'a'.repeat(32) });
  await operation;
  if (content.innerHTML !== 'TASK_PAGE_SENTINEL') {
    throw new Error('delete completion overwrote the page selected during the request');
  }
  if (requests.some((entry) => entry.path.startsWith('/api/snapshots?'))) {
    throw new Error('delete completion refreshed snapshots after leaving the page');
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_create_response_does_not_close_a_later_unrelated_modal(self):
        node = (
            os.environ.get("NODE_BINARY")
            or shutil.which("node")
            or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        if not os.path.exists(node):
            self.skipTest("Node.js is unavailable")

        script = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('index.html', 'utf8');
const fullSource = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const marker = fullSource.indexOf('PAGE 13: Snapshots');
const nextMarker = fullSource.indexOf('PAGE 14:', marker);
const start = fullSource.lastIndexOf('/*', marker);
const end = fullSource.lastIndexOf('/*', nextMarker);
const source = fullSource.slice(start, end);

const content = { innerHTML: '' };
const createButton = { disabled: false, textContent: '📸 创建' };
const elements = {
  content,
  f_snapname: { value: 'Slow snapshot' },
  snapshotCreateConfirmBtn: createButton
};
let modalState = 'snapshot-open';
let resolveCreate;
const sandbox = {
  console,
  document: { getElementById: (id) => elements[id] || null },
  currentPage: 'snapshots', dataSource: 'local',
  cloudReadOnlyGuard: () => false,
  closeModal: () => { modalState = 'closed'; },
  toast: () => {},
  escHtml: (value) => String(value == null ? '' : value),
  api: (path, options = {}) => {
    if (path === '/api/snapshots' && options.method === 'POST') {
      return new Promise((resolve) => { resolveCreate = resolve; });
    }
    return Promise.reject(new Error('unexpected request: ' + path));
  }
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

(async () => {
  const operation = sandbox.doCreateSnapshot();
  if (typeof resolveCreate !== 'function') throw new Error('create request did not start');
  delete elements.snapshotCreateConfirmBtn;
  modalState = 'unrelated-task-modal-open';
  sandbox.currentPage = 'tasks';
  resolveCreate({ ok: true, snapshot: { id: 'b'.repeat(32), name: 'Slow snapshot' } });
  await operation;
  if (modalState !== 'unrelated-task-modal-open') {
    throw new Error('late snapshot response closed an unrelated modal');
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_restore_exposes_and_clears_visible_busy_state(self):
        node = (
            os.environ.get("NODE_BINARY")
            or shutil.which("node")
            or r"C:\Users\M2TO\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        if not os.path.exists(node):
            self.skipTest("Node.js is unavailable")

        script = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('index.html', 'utf8');
const fullSource = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const marker = fullSource.indexOf('PAGE 13: Snapshots');
const nextMarker = fullSource.indexOf('PAGE 14:', marker);
const start = fullSource.lastIndexOf('/*', marker);
const end = fullSource.lastIndexOf('/*', nextMarker);
const source = fullSource.slice(start, end);

function button(baseDisabled) {
  return {
    disabled: false,
    getAttribute: (name) => name === 'data-snapshot-disabled' && baseDisabled ? 'true' : null
  };
}
const createButton = button(false);
const invalidRestoreButton = button(true);
const deleteButton = button(false);
const buttons = [createButton, invalidRestoreButton, deleteButton];
const attributes = {};
const panel = {
  setAttribute: (name, value) => { attributes[name] = String(value); },
  querySelectorAll: () => buttons
};
const status = { textContent: '' };
const elements = { snapshotPanel: panel, snapshotActionStatus: status };
let resolveRestore;
const sandbox = {
  console,
  document: { getElementById: (id) => elements[id] || null },
  currentPage: 'snapshots', dataSource: 'local',
  confirm: () => true,
  toast: () => {},
  cloudReadOnlyGuard: () => false,
  applyLoadedStatus: () => true,
  api: (path, options = {}) => {
    if (path.endsWith('/restore') && options.method === 'POST') {
      return new Promise((resolve) => { resolveRestore = resolve; });
    }
    return Promise.reject(new Error('unexpected request: ' + path));
  }
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
sandbox.snapshotCache = [{
  id: 'a'.repeat(32), name: 'Busy restore', restorable: true,
  integrity: { archive: 'ok', database: 'ok', counts: {} }
}];

(async () => {
  const operation = sandbox.restoreSnapshot(0);
  if (typeof resolveRestore !== 'function') throw new Error('restore request did not start');
  if (attributes['aria-busy'] !== 'true' || !buttons.every((item) => item.disabled)) {
    throw new Error('restore did not expose a disabled busy state');
  }
  if (!status.textContent.includes('恢复')) throw new Error('restore busy label is missing');

  resolveRestore({
    ok: true,
    workspace: {
      loaded: true, workspace_copy: true,
      backup_path: 'C:\\workspaces\\restores\\restored.zip', filename: 'restored.zip'
    }
  });
  await operation;
  if (attributes['aria-busy'] !== 'false' || status.textContent !== '') {
    throw new Error('restore busy state did not clear');
  }
  if (createButton.disabled || deleteButton.disabled || !invalidRestoreButton.disabled) {
    throw new Error('buttons were not restored to their safe base states');
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
