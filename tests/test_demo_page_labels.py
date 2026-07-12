import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DemoPageLabelTests(unittest.TestCase):
    def test_goals_and_review_are_clearly_labeled_as_demo_data(self):
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
const localStorage = { getItem: () => 'local', setItem: noop, removeItem: noop };
const sandbox = {
  console, document, localStorage, window: { addEventListener: noop },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  fetch: noop, confirm: () => false, alert: noop, Blob, URL
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
(async () => {
  for (const loader of [sandbox.loadGoals, sandbox.loadReview]) {
    await loader();
    if (!content.innerHTML.includes('演示数据') || !content.innerHTML.includes('不会写入')) {
      throw new Error('mock page lacks a clear demo-data warning');
    }
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
