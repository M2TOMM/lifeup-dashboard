import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceCleanupApiTests(unittest.TestCase):
    def setUp(self):
        self.old_state = dict(server.STATE)
        self.root = tempfile.mkdtemp(prefix="lifeup-workspace-cleanup-")
        self.browser_imports = os.path.join(self.root, "workspaces", "browser-imports")
        self.restores = os.path.join(self.root, "workspaces", "restores")
        self.snapshots = os.path.join(self.root, "workspaces", "snapshots")
        self.exports = os.path.join(self.root, "exports")
        self.work = os.path.join(self.root, "work")
        for directory in (
            self.browser_imports,
            self.restores,
            self.snapshots,
            self.exports,
            self.work,
        ):
            os.makedirs(directory)

        self.current = self._write(
            self.browser_imports,
            "20260718-120000-aaaaaaaa-current.zip",
            b"current-workspace",
        )
        self.old_import = self._write(
            self.browser_imports,
            "20260717-120000-bbbbbbbb-old.zip",
            b"old-import",
        )
        self.restore = self._write(
            self.restores,
            "restore-20260718-120000-" + "c" * 32 + "-" + "d" * 16 + ".zip",
            b"old-restore",
        )
        self.export = self._write(
            self.exports,
            "LifeupBackup-export-20260718-120000-abcdef.zip",
            b"generated-export",
        )
        self.log = self._write(self.work, "task16-server.out.log", b"temporary-log")
        nested_work = os.path.join(self.work, "release-venv")
        os.makedirs(nested_work)
        self.nested_work_file = self._write(nested_work, "dependency.py", b"build dependency")
        self.snapshot = self._write(
            self.snapshots,
            "snapshot-" + "e" * 32 + ".zip",
            b"recovery-snapshot",
        )
        self.original = self._write(self.root, "LifeupBackup.zip", b"original-backup")
        self.unmanaged = self._write(self.exports, "keep-me.zip", b"unmanaged-export")

        self.patches = [
            patch.object(server, "BROWSER_IMPORT_DIR", self.browser_imports),
            patch.object(server, "RESTORE_DIR", self.restores),
            patch.object(server, "SNAPSHOT_DIR", self.snapshots),
            patch.object(server, "EXPORT_DIR", self.exports),
            patch.object(server, "WORK_DIR", self.work),
            patch.object(server, "ORIGINAL_BACKUP_PATH", self.original),
            patch.object(server, "PROTECTED_BACKUP_PATHS", {server._canonical_path(self.original)}),
        ]
        for active_patch in self.patches:
            active_patch.start()
        server.STATE.update(
            {
                "backup_path": self.current,
                "db_path": None,
                "tmpdir": None,
                "loaded": True,
            }
        )
        server.WORKSPACE_CLEANUP_PREVIEWS.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        server.WORKSPACE_CLEANUP_PREVIEWS.clear()
        server.STATE.clear()
        server.STATE.update(self.old_state)
        for active_patch in reversed(self.patches):
            active_patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _write(directory, name, contents):
        path = os.path.join(directory, name)
        with open(path, "wb") as output:
            output.write(contents)
        return path

    def preview(self):
        response = self.client.post("/api/workspace-cleanup/preview", json={})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_preview_lists_only_managed_candidates_and_protects_current_and_snapshots(self):
        payload = self.preview()

        names = {item["name"] for item in payload["items"]}
        self.assertEqual(
            names,
            {
                os.path.basename(self.old_import),
                os.path.basename(self.restore),
                os.path.basename(self.export),
                os.path.basename(self.log),
            },
        )
        self.assertNotIn(os.path.basename(self.current), names)
        self.assertNotIn(os.path.basename(self.original), names)
        self.assertNotIn(os.path.basename(self.snapshot), names)
        self.assertNotIn(os.path.basename(self.unmanaged), names)
        self.assertNotIn(os.path.basename(self.nested_work_file), names)
        self.assertTrue(payload["preview_token"])
        self.assertEqual(payload["count"], 4)
        self.assertEqual(payload["total_bytes"], sum(item["size"] for item in payload["items"]))
        for item in payload["items"]:
            self.assertNotIn(self.root, str(item))
            self.assertNotIn("path", item)

    def test_execute_deletes_only_selected_items_and_token_cannot_replay(self):
        preview = self.preview()
        by_name = {item["name"]: item for item in preview["items"]}
        selected = [by_name[os.path.basename(self.old_import)]["id"]]

        response = self.client.post(
            "/api/workspace-cleanup/execute",
            json={"preview_token": preview["preview_token"], "item_ids": selected},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["deleted"], 1)
        self.assertEqual(payload["failed"], 0)
        self.assertFalse(os.path.exists(self.old_import))
        for protected in (self.current, self.original, self.snapshot, self.restore, self.export, self.log):
            self.assertTrue(os.path.exists(protected), protected)

        replay = self.client.post(
            "/api/workspace-cleanup/execute",
            json={"preview_token": preview["preview_token"], "item_ids": selected},
        )
        self.assertEqual(replay.status_code, 409, replay.get_json())
        self.assertEqual(replay.get_json()["code"], "CLEANUP_PREVIEW_EXPIRED")

    def test_changed_file_is_not_deleted(self):
        preview = self.preview()
        item = next(entry for entry in preview["items"] if entry["name"] == os.path.basename(self.log))
        with open(self.log, "ab") as output:
            output.write(b"-changed-after-preview")

        response = self.client.post(
            "/api/workspace-cleanup/execute",
            json={"preview_token": preview["preview_token"], "item_ids": [item["id"]]},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["deleted"], 0)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["results"][0]["status"], "changed")
        self.assertTrue(os.path.exists(self.log))

    def test_execute_rejects_unknown_ids_without_deleting_anything(self):
        preview = self.preview()
        response = self.client.post(
            "/api/workspace-cleanup/execute",
            json={"preview_token": preview["preview_token"], "item_ids": ["not-from-preview"]},
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["code"], "INVALID_CLEANUP_SELECTION")
        for path in (self.old_import, self.restore, self.export, self.log):
            self.assertTrue(os.path.exists(path), path)

    def test_preview_skips_a_managed_root_when_it_is_a_reparse_point(self):
        real_check = server._path_is_reparse_point

        def fake_check(path):
            if server._canonical_path(path) == server._canonical_path(self.work):
                return True
            return real_check(path)

        with patch.object(server, "_path_is_reparse_point", side_effect=fake_check):
            payload = self.preview()

        names = {item["name"] for item in payload["items"]}
        self.assertNotIn(os.path.basename(self.log), names)
        self.assertIn(os.path.basename(self.old_import), names)


class WorkspaceCleanupFrontendContractTests(unittest.TestCase):
    def test_ui_requires_preview_selection_and_confirmation(self):
        source = (ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-page="maintenance"', source)
        self.assertIn("/api/workspace-cleanup/preview", source)
        self.assertIn("/api/workspace-cleanup/execute", source)
        self.assertIn("preview_token: workspaceCleanupPreview.preview_token", source)
        self.assertIn("item_ids: selected", source)
        self.assertIn("确认删除选中项", source)
        self.assertIn("原始备份、当前工作副本、快照和本机配置永远不会进入清理列表", source)


if __name__ == "__main__":
    unittest.main()
