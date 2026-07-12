import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]


class SelectedDataSourceTests(unittest.TestCase):
    def test_cloud_dashboard_fetches_independent_datasets_concurrently(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_cloud_request(config, route, timeout=12):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            if route == "/achievement_categories":
                data = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
            elif route == "/coin":
                data = {"coin": 0}
            else:
                data = []
            return {"route": route, "base_url": "http://phone", "data": data}

        with patch.object(server, "cloud_request", side_effect=fake_cloud_request):
            result = server.cloud_dashboard_overview()

        self.assertEqual(result["meta"]["source"], "cloud")
        self.assertGreaterEqual(max_active, 3)

    def test_dashboard_and_focus_request_the_selected_source_only(self):
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
const domElement = {
  classList, style: {}, textContent: '', innerHTML: '', addEventListener: noop,
  querySelector: () => null, querySelectorAll: () => [], setAttribute: noop
};
const document = {
  body: { classList }, getElementById: () => domElement, querySelector: () => null,
  querySelectorAll: () => [], addEventListener: noop,
  createElement: () => ({ classList, style: {}, addEventListener: noop })
};
let sourceWrites = 0;
const localStorage = {
  getItem: (key) => key === 'lifeup_data_source' ? 'local' : null,
  setItem: (key) => { if (key === 'lifeup_data_source') sourceWrites += 1; },
  removeItem: noop
};
const requests = [];
const fetch = async (url) => {
  requests.push(url);
  return { ok: true, json: async () => ({ meta: { source: url.includes('source=cloud') ? 'cloud' : 'local' } }) };
};
const sandbox = {
  console, document, localStorage, fetch, window: { addEventListener: noop },
  setTimeout, clearTimeout, URLSearchParams, AbortController,
  confirm: () => false, alert: noop, Blob, URL
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
(async () => {
  await sandbox.fetchDashboardOverview();
  await sandbox.fetchFocusOverview();
  if (requests.length !== 2 || requests.some((url) => !url.includes('source=local'))) {
    throw new Error('pages ignored the selected local source: ' + JSON.stringify(requests));
  }
  if (sourceWrites !== 0) throw new Error('page load changed the selected data source');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
