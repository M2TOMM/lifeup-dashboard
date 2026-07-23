import errno
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import server


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BackupExportTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.tempdir = tempfile.mkdtemp(prefix="lifeup-export-test-")
        self.workspace = os.path.join(self.tempdir, "workspace")
        self.export_dir = os.path.join(self.tempdir, "exports")
        os.makedirs(os.path.join(self.workspace, "databases"))
        os.makedirs(self.export_dir)

        self.database = os.path.join(self.workspace, "databases", "LifeUpDB.db")
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE taskmodel (id INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE shopitemmodel (id INTEGER PRIMARY KEY, itemname TEXT);
            CREATE TABLE userachievementmodel (id INTEGER PRIMARY KEY, content TEXT);
            INSERT INTO taskmodel(content) VALUES ('task-a'), ('task-b');
            INSERT INTO shopitemmodel(itemname) VALUES ('item-a');
            INSERT INTO userachievementmodel(content) VALUES ('achievement-a'), ('achievement-b'), ('achievement-c');
            """
        )
        connection.commit()
        connection.close()

        self.source_backup = os.path.join(self.tempdir, "source-workspace.zip")
        with open(self.source_backup, "wb") as source:
            source.write(b"source-must-not-change")

        server.STATE.update(
            {
                "backup_path": self.source_backup,
                "db_path": self.database,
                "tmpdir": self.workspace,
                "loaded": True,
            }
        )
        self.client = server.app.test_client()

    def tearDown(self):
        server.STATE.clear()
        server.STATE.update(self._old_state)
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def post_save(self, payload=None):
        with patch.object(server, "EXPORT_DIR", self.export_dir, create=True):
            return self.client.post("/api/save", json={} if payload is None else payload)

    def test_default_export_is_new_valid_archive_with_integrity_metadata(self):
        response = self.post_save()

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(os.path.dirname(payload["path"]), self.export_dir)
        self.assertNotEqual(os.path.normcase(payload["path"]), os.path.normcase(self.source_backup))
        self.assertEqual(payload["size"], os.path.getsize(payload["path"]))
        self.assertTrue(payload["generated_at"])
        self.assertEqual(payload["integrity"]["archive"], "ok")
        self.assertEqual(payload["integrity"]["database"], "ok")

        with zipfile.ZipFile(payload["path"], "r") as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(server.DB_INTERNAL, archive.namelist())

    def test_export_uses_android_compatible_zip_metadata(self):
        response = self.post_save()

        self.assertEqual(response.status_code, 200, response.get_json())
        with zipfile.ZipFile(response.get_json()["path"], "r") as archive:
            infos = archive.infolist()
            self.assertTrue(infos)
            self.assertEqual({info.flag_bits for info in infos}, {0x808})
            self.assertEqual({info.create_system for info in infos}, {0})
            self.assertEqual({info.external_attr for info in infos}, {0})
            self.assertFalse(any(info.extra or info.comment for info in infos))

    def test_export_validation_rejects_non_android_zip_metadata(self):
        incompatible = os.path.join(self.export_dir, "incompatible.zip")
        with zipfile.ZipFile(incompatible, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(self.database, server.DB_INTERNAL)

        with self.assertRaises(server.BackupExportError) as raised:
            server._inspect_backup_archive(incompatible)

        self.assertEqual(raised.exception.code, "EXPORT_VALIDATION_FAILED")
        self.assertIn("Android", str(raised.exception))

    def test_export_round_trip_preserves_key_entity_counts(self):
        before = self.entity_counts(self.database)
        response = self.post_save()

        self.assertEqual(response.status_code, 200, response.get_json())
        with tempfile.TemporaryDirectory(prefix="lifeup-export-roundtrip-") as extracted:
            with zipfile.ZipFile(response.get_json()["path"], "r") as archive:
                archive.extract(server.DB_INTERNAL, extracted)
            exported_db = os.path.join(extracted, *server.DB_INTERNAL.split("/"))
            after = self.entity_counts(exported_db)
        self.assertEqual(after, before)

    def test_rejects_original_or_current_source_path_without_changing_it(self):
        with open(self.source_backup, "rb") as source:
            original_bytes = source.read()
        with patch.object(
            server,
            "PROTECTED_BACKUP_PATHS",
            {os.path.normcase(os.path.abspath(self.source_backup))},
            create=True,
        ):
            response = self.post_save({"path": self.source_backup})

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["code"], "PROTECTED_OUTPUT_PATH")
        with open(self.source_backup, "rb") as source:
            self.assertEqual(source.read(), original_bytes)

    def test_disk_full_during_zip_write_keeps_existing_final_file(self):
        destination = os.path.join(self.export_dir, "existing.zip")
        original_bytes = b"previous-valid-export"
        with open(destination, "wb") as existing:
            existing.write(original_bytes)

        disk_full = OSError(errno.ENOSPC, "simulated disk full")
        with patch.object(server, "_write_android_zip_member", side_effect=disk_full):
            response = self.post_save({"path": destination})

        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(response.get_json()["code"], "EXPORT_IO_ERROR")
        with open(destination, "rb") as existing:
            self.assertEqual(existing.read(), original_bytes)
        self.assertEqual(os.listdir(self.export_dir), ["existing.zip"])

    def test_permission_denied_before_temp_write_keeps_existing_final_file(self):
        destination = os.path.join(self.export_dir, "permission-denied.zip")
        original_bytes = b"previous-valid-export"
        with open(destination, "wb") as existing:
            existing.write(original_bytes)

        with patch.object(server.tempfile, "mkstemp", side_effect=PermissionError("denied")):
            response = self.post_save({"path": destination})

        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(response.get_json()["code"], "EXPORT_IO_ERROR")
        with open(destination, "rb") as existing:
            self.assertEqual(existing.read(), original_bytes)
        self.assertEqual(os.listdir(self.export_dir), ["permission-denied.zip"])

    def test_zip_crc_validation_failure_does_not_replace_final_file(self):
        destination = os.path.join(self.export_dir, "crc-check.zip")
        original_bytes = b"previous-valid-export"
        with open(destination, "wb") as existing:
            existing.write(original_bytes)

        with patch.object(zipfile.ZipFile, "testzip", return_value=server.DB_INTERNAL):
            response = self.post_save({"path": destination})

        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(response.get_json()["code"], "EXPORT_VALIDATION_FAILED")
        with open(destination, "rb") as existing:
            self.assertEqual(existing.read(), original_bytes)
        self.assertEqual(os.listdir(self.export_dir), ["crc-check.zip"])

    def test_sqlite_integrity_failure_does_not_create_final_file(self):
        destination = os.path.join(self.export_dir, "corrupt-db.zip")
        with open(self.database, "wb") as database:
            database.write(b"not-a-sqlite-database")

        response = self.post_save({"path": destination})

        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(response.get_json()["code"], "EXPORT_VALIDATION_FAILED")
        self.assertFalse(os.path.exists(destination))
        self.assertEqual(os.listdir(self.export_dir), [])

    def test_explicit_missing_output_directory_is_rejected_without_creating_it(self):
        missing_dir = os.path.join(self.tempdir, "missing")
        destination = os.path.join(missing_dir, "export.zip")

        response = self.post_save({"path": destination})

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["code"], "INVALID_OUTPUT_PATH")
        self.assertFalse(os.path.exists(missing_dir))

    def test_malformed_json_is_rejected_without_starting_an_export(self):
        with patch.object(server, "EXPORT_DIR", self.export_dir, create=True):
            response = self.client.post(
                "/api/save", data="{", content_type="application/json"
            )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["code"], "INVALID_REQUEST")
        self.assertEqual(os.listdir(self.export_dir), [])

    def test_rejects_export_target_inside_current_workspace(self):
        destination = os.path.join(self.workspace, "nested-export.zip")
        with patch.object(server, "EXPORT_DIR", self.workspace, create=True):
            response = self.client.post("/api/save", json={"path": destination})

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["code"], "INVALID_OUTPUT_PATH")
        self.assertFalse(os.path.exists(destination))

    def test_replace_failure_keeps_existing_final_file_and_cleans_temporary_file(self):
        destination = os.path.join(self.export_dir, "locked.zip")
        original_bytes = b"previous-valid-export"
        with open(destination, "wb") as existing:
            existing.write(original_bytes)

        with patch.object(server.os, "replace", side_effect=PermissionError("file is locked")):
            response = self.post_save({"path": destination})

        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(response.get_json()["code"], "EXPORT_IO_ERROR")
        with open(destination, "rb") as existing:
            self.assertEqual(existing.read(), original_bytes)
        self.assertEqual(os.listdir(self.export_dir), ["locked.zip"])

    @staticmethod
    def entity_counts(database_path):
        connection = sqlite3.connect(database_path)
        try:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("taskmodel", "shopitemmodel", "userachievementmodel")
            }
        finally:
            connection.close()


class BackupExportBrowserContractTests(unittest.TestCase):
    def test_save_button_reports_export_path_size_time_and_integrity(self):
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
const start = fullSource.indexOf('function saveBackup()');
const end = fullSource.indexOf('// ====== Keyboard Shortcuts', start);
if (start < 0 || end < 0) throw new Error('saveBackup source not found');
const source = fullSource.slice(start, end);
const toasts = [];
const button = { disabled: false, textContent: '' };
const sandbox = {
  console,
  window: { pywebview: null },
  document: { getElementById: (id) => id === 'saveBtn' ? button : null },
  setTimeout: (fn) => fn(),
  clearTimeout: () => {},
  URL, Blob, FormData,
};
sandbox.window.window = sandbox.window;
sandbox.window.document = sandbox.document;
sandbox.cloudReadOnlyGuard = () => false;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
sandbox.dataSource = 'local';
sandbox.api = async (path, options) => {
  if (path !== '/api/save' || options.method !== 'POST') throw new Error('wrong request');
  return {
    ok: true,
    path: 'C:\\exports\\LifeupBackup-export.zip',
    size: 2048,
    generated_at: '2026-07-11T12:00:00+08:00',
    integrity: { archive: 'ok', database: 'ok' }
  };
};
sandbox.toast = (message, type) => toasts.push({ message, type });
Promise.resolve(sandbox.saveBackup()).then(() => {
  const message = toasts.map((entry) => entry.message).join('\n');
  for (const expected of ['C:\\exports\\LifeupBackup-export.zip', '2 KB', '2026-07-11T12:00:00+08:00', 'ZIP/SQLite 校验通过']) {
    if (!message.includes(expected)) throw new Error('missing export detail: ' + expected + '\n' + message);
  }
}).catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
