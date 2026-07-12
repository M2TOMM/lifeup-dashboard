import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CloudRenderingSecurityTests(unittest.TestCase):
    def test_cloud_rows_escape_text_and_reject_active_image_urls(self):
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
const content = { innerHTML: '', classList, style: {} };
const document = {
  body: { classList }, getElementById: (id) => id === 'content' ? content : null,
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop,
  createElement: () => ({ classList, style: {}, addEventListener: noop })
};
const localStorage = { getItem: () => 'cloud', setItem: noop, removeItem: noop };
const attack = '<img src=x onerror=alert(1)>';
const response = (payload) => ({ ok: true, json: async () => payload });
const fetch = async (url) => {
  if (url.includes('/api/categories/')) return response([{ id: 1, name: attack }]);
  if (url.includes('/api/tasks')) return response([{
    id: 1, title: attack, frequency: 1, coin: 1, exp: 1, done: 0,
    priority: 1, difficulty: 1, done_count: 0, target_count: 1,
    category_name: attack, read_only: true
  }]);
  if (url.includes('/api/items')) return response([{
    id: 1, name: attack, icon: 'javascript:alert(1)', price: 1, count: 1,
    inventory_count: 0, category_name: attack, description: attack, read_only: true
  }]);
  if (url.includes('/api/achievements')) return response([{
    id: 1, name: attack, icon: 'javascript:alert(1)', category_name: attack,
    achievementstatus: 0, coin: 1, description: attack, read_only: true
  }]);
  throw new Error('unexpected request: ' + url);
};
const sandbox = {
  console, document, localStorage, fetch, window: { addEventListener: noop },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  confirm: () => false, alert: noop, Blob, URL,
  location: { origin: 'http://127.0.0.1:5001' }
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
sandbox.window.location = sandbox.location;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
(async () => {
  if (sandbox.safeImageUrl('javascript:alert(1)') !== '') {
    throw new Error('active image URL was accepted');
  }
  for (const loader of [sandbox.loadTasks, sandbox.loadItems, sandbox.loadAchievements]) {
    await loader();
    if (content.innerHTML.includes(attack)) throw new Error('unescaped cloud text reached innerHTML');
    if (content.innerHTML.toLowerCase().includes('javascript:')) throw new Error('active image URL reached the DOM');
    if (!content.innerHTML.includes('&lt;img')) throw new Error('malicious text was not safely escaped');
  }
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
