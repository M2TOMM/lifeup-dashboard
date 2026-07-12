import io
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import server


class BackupValidationTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.client = server.app.test_client()
        self.test_root = tempfile.mkdtemp(prefix="lifeup-backup-validation-")
        self.import_dir = os.path.join(self.test_root, "browser-imports")
        self.import_patch = patch.object(server, "BROWSER_IMPORT_DIR", self.import_dir)
        self.import_patch.start()

        self.baseline_path = os.path.join(self.test_root, "baseline.zip")
        with open(self.baseline_path, "wb") as output:
            output.write(self.make_backup().getvalue())
        server.load_backup(self.baseline_path)
        self.baseline_tmpdir = server.STATE["tmpdir"]
        self.baseline_marker = os.path.join(self.baseline_tmpdir, "baseline-marker.txt")
        with open(self.baseline_marker, "w", encoding="utf-8") as marker:
            marker.write("still active")

    def tearDown(self):
        self.import_patch.stop()
        active_tmpdir = server.STATE.get("tmpdir")
        server.STATE.clear()
        server.STATE.update(self._old_state)
        if active_tmpdir and active_tmpdir != self._old_state.get("tmpdir"):
            shutil.rmtree(active_tmpdir, ignore_errors=True)
        shutil.rmtree(self.baseline_tmpdir, ignore_errors=True)
        shutil.rmtree(self.test_root, ignore_errors=True)

    def make_backup(self, extra_entries=None, include_database=True):
        output = io.BytesIO()
        with tempfile.TemporaryDirectory(prefix="lifeup-valid-db-") as tmpdir:
            if include_database:
                db_path = os.path.join(tmpdir, "LifeUpDB.db")
                connection = sqlite3.connect(db_path)
                connection.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
                connection.commit()
                connection.close()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                if include_database:
                    archive.write(db_path, "databases/LifeUpDB.db")
                for name, contents in extra_entries or []:
                    archive.writestr(name, contents)
        output.seek(0)
        return output

    def assert_baseline_is_still_active(self):
        self.assertTrue(server.STATE["loaded"])
        self.assertEqual(server.STATE["backup_path"], self.baseline_path)
        self.assertEqual(server.STATE["tmpdir"], self.baseline_tmpdir)
        self.assertTrue(os.path.exists(self.baseline_marker))

    def upload(self, file_object, filename="LifeupBackup.zip"):
        return self.client.post(
            "/api/open-upload",
            data={"files": (file_object, filename)},
            content_type="multipart/form-data",
        )

    def test_rejects_path_traversal_and_preserves_loaded_backup(self):
        response = self.upload(
            self.make_backup(extra_entries=[("..\\escaped.txt", b"do not extract")]),
            "traversal.zip",
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        payload = response.get_json()
        self.assertIn("不安全路径", payload["error"])
        self.assertIn("重新", payload["suggestion"])
        self.assert_baseline_is_still_active()

    def test_rejects_missing_database_and_preserves_loaded_backup(self):
        response = self.upload(
            self.make_backup(include_database=False, extra_entries=[("readme.txt", b"not a backup")]),
            "missing-database.zip",
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        payload = response.get_json()
        self.assertIn("databases/LifeUpDB.db", payload["error"])
        self.assertTrue(payload["suggestion"])
        self.assert_baseline_is_still_active()

    def test_rejects_corrupt_zip_and_preserves_loaded_backup(self):
        response = self.upload(io.BytesIO(b"this is not a zip"), "corrupt.zip")

        self.assertEqual(response.status_code, 400, response.get_json())
        payload = response.get_json()
        self.assertIn("ZIP", payload["error"])
        self.assertTrue(payload["suggestion"])
        self.assert_baseline_is_still_active()

    def test_rejects_archive_over_expanded_size_limit(self):
        with patch.object(server, "MAX_BACKUP_EXPANDED_BYTES", 1024, create=True):
            response = self.upload(
                self.make_backup(extra_entries=[("media/shop/large.bin", b"x" * 4096)]),
                "oversized.zip",
            )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("解压后", response.get_json()["error"])
        self.assert_baseline_is_still_active()

    def test_rejects_windows_device_names(self):
        response = self.upload(
            self.make_backup(extra_entries=[("media/shop/NUL.png", b"unsafe on Windows")]),
            "device-name.zip",
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("不安全路径", response.get_json()["error"])
        self.assert_baseline_is_still_active()

    def test_request_too_large_returns_beginner_friendly_json(self):
        with patch.dict(server.app.config, {"MAX_CONTENT_LENGTH": 128}):
            response = self.upload(io.BytesIO(b"x" * 1024), "too-large.zip")

        self.assertEqual(response.status_code, 413)
        payload = response.get_json()
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("code"), "REQUEST_TOO_LARGE")
        self.assertIn("过大", payload["error"])
        self.assertTrue(payload["suggestion"])
        self.assert_baseline_is_still_active()

    def test_rejects_non_zip_with_beginner_friendly_json(self):
        response = self.upload(io.BytesIO(b"plain text"), "notes.txt")

        self.assertEqual(response.status_code, 400, response.get_json())
        payload = response.get_json()
        self.assertIn("ZIP", payload["error"])
        self.assertTrue(payload["suggestion"])
        self.assert_baseline_is_still_active()

    def test_batch_upload_keeps_last_valid_backup_when_later_file_fails(self):
        response = self.client.post(
            "/api/open-upload",
            data={
                "files": [
                    (self.make_backup(), "valid.zip"),
                    (io.BytesIO(b"broken"), "broken.zip"),
                ]
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(len(payload["errors"]), 1)
        self.assertTrue(server.STATE["loaded"])
        self.assertEqual(server.STATE["backup_path"], payload["path"])
        self.assertTrue(os.path.exists(server.STATE["db_path"]))


if __name__ == "__main__":
    unittest.main()
