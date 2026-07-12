import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]


class DataSourceIsolationTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        server.STATE.update(
            {
                "backup_path": "workspace-copy.zip",
                "db_path": "workspace-copy.db",
                "tmpdir": None,
                "loaded": True,
            }
        )
        self.client = server.app.test_client()

    def tearDown(self):
        server.STATE.clear()
        server.STATE.update(self._old_state)

    def test_cloud_source_cannot_save_local_backup(self):
        with patch.object(server, "save_backup") as save_backup:
            response = self.client.post("/api/save?source=cloud", json={})

        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertIn("本地备份", response.get_json()["error"])
        save_backup.assert_not_called()

    def test_cloud_source_cannot_mutate_synthesis_or_pools(self):
        cases = [
            ("/api/synthesis/add?source=cloud", {"title": "test"}),
            ("/api/synthesis/update?source=cloud", {"id": 1}),
            ("/api/synthesis/delete?source=cloud", {"id": 1}),
            ("/api/pools/add?source=cloud", {"shopitemid": 1}),
            ("/api/pools/update?source=cloud", {"id": 1}),
        ]

        with patch.object(server, "get_db", side_effect=AssertionError("local DB opened")):
            for path, payload in cases:
                with self.subTest(path=path):
                    response = self.client.post(path, json=payload)
                    self.assertEqual(response.status_code, 403, response.get_json())
                    self.assertIn("本地备份", response.get_json()["error"])

    def test_frontend_blocks_local_only_writes_in_cloud_mode(self):
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
const document = {
  body: { classList },
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: noop,
  createElement: () => ({ classList, style: {}, addEventListener: noop })
};
const localStorage = { getItem: () => 'cloud', setItem: noop, removeItem: noop };
let fetchCalls = 0;
const sandbox = {
  console, document, localStorage, window: { addEventListener: noop },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  fetch: () => { fetchCalls += 1; }, confirm: () => false, alert: noop, Blob, URL
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
for (const path of ['/api/save', '/api/synthesis/add', '/api/pools/update']) {
  if (!sandbox.isCloudReadOnlyWrite(path, { method: 'POST' })) {
    throw new Error('cloud write was not blocked: ' + path);
  }
}
const content = { innerHTML: '' };
document.getElementById = (id) => id === 'content' ? content : null;
Promise.all([sandbox.loadSynthesis(), sandbox.loadPools()]).then(() => {
  if (fetchCalls !== 0) {
    throw new Error('cloud-only page read local backup data');
  }
  if (!content.innerHTML.includes('本地备份')) {
    throw new Error('local-only page did not explain the data-source boundary');
  }
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
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
