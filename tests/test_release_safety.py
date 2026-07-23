import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseSafetyTests(unittest.TestCase):
    def run_audit(self, target):
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "audit_release.py"), str(target)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_release_audit_accepts_documentation_and_binary(self):
        with tempfile.TemporaryDirectory(prefix="lifeup-release-good-") as directory:
            root = Path(directory)
            (root / "LifeUpDashboard.exe").write_bytes(b"not-a-real-exe")
            (root / "USER_GUIDE.md").write_text("safe guide", encoding="utf-8")

            result = self.run_audit(root)

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("release audit ok", result.stdout)

    def test_release_audit_rejects_backups_configs_databases_and_logs(self):
        forbidden = [
            "LifeupBackup.zip",
            "lifeup_cloud_config.json",
            "private.db",
            "server.log",
            "work/cloud-operation-log.jsonl",
        ]
        for relative in forbidden:
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory(prefix="lifeup-release-bad-") as directory:
                    path = Path(directory, *relative.split("/"))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("private", encoding="utf-8")
                    result = self.run_audit(directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("RELEASE AUDIT FAILED", result.stderr)

    def test_server_data_root_can_be_redirected_outside_bundle(self):
        with tempfile.TemporaryDirectory(prefix="lifeup-data-root-") as directory:
            environment = os.environ.copy()
            environment["LIFEUP_DASHBOARD_DATA_DIR"] = directory
            script = (
                "import json, server; "
                "print(json.dumps({'data': server.DATA_DIR, "
                "'imports': server.BROWSER_IMPORT_DIR, "
                "'config': server.CLOUD_CONFIG_PATH}))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertEqual(os.path.normcase(payload["data"]), os.path.normcase(directory))
        self.assertTrue(os.path.commonpath([payload["imports"], directory]) == directory)
        self.assertTrue(os.path.commonpath([payload["config"], directory]) == directory)

    def test_desktop_sets_stable_data_root_before_importing_server(self):
        source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
        env_marker = "os.environ.setdefault('LIFEUP_DASHBOARD_DATA_DIR', USER_DATA_DIR)"
        server_marker = "import server as server_module"

        self.assertIn("LOCALAPPDATA", source)
        self.assertGreater(source.find(server_marker), source.find(env_marker))
        self.assertIn("desktop-config.json", source)

    def test_build_script_audits_exe_stage_and_final_zip(self):
        source = (ROOT / "tools" / "build_desktop.ps1").read_text(encoding="utf-8")

        self.assertIn("requirements-desktop.txt", source)
        self.assertIn("-m unittest discover", source)
        self.assertIn("audit_release.py\") $Exe $Stage", source)
        self.assertIn("audit_release.py\") $Output", source)
        self.assertNotIn("Copy-Item -Recurse", source)


if __name__ == "__main__":
    unittest.main()
