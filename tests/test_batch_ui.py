import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BatchUiContractTests(unittest.TestCase):
    def test_frontend_blocks_oversized_batches_and_invalid_prices(self):
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
const element = {
  classList, style: {}, textContent: '', innerHTML: '', value: '', disabled: false,
  addEventListener: noop, querySelector: () => null, querySelectorAll: () => [],
  setAttribute: noop, focus: noop
};
let selectedChecks = [];
let priceValue = '';
const document = {
  body: { classList },
  getElementById: (id) => id === 'f_batchprice' ? { ...element, value: priceValue } : { ...element },
  querySelector: () => null,
  querySelectorAll: (selector) => selector === '.batch-check:checked' ? selectedChecks : [],
  addEventListener: noop,
  createElement: () => ({ ...element })
};
const localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
const sandbox = {
  console, document, localStorage, window: { addEventListener: noop }, location: { protocol: 'http:' },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  fetch: noop, confirm: () => true, alert: noop, Blob, URL
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

let apiCalls = [];
let toasts = [];
sandbox.api = (path, options) => {
  apiCalls.push({ path, options });
  return new Promise(() => {});
};
sandbox.toast = (message, type) => toasts.push({ message, type });
sandbox.closeModal = noop;

selectedChecks = Array.from({ length: 201 }, (_, index) => ({ value: String(index + 1) }));
sandbox.batchAction('tasks', 'disable');
if (apiCalls.length !== 0 || !toasts.some(item => item.message.includes('最多'))) {
  throw new Error('oversized task batch was not blocked in the browser');
}

selectedChecks = [{ value: '1' }];
for (const invalidPrice of ['-1', '1.5', '2147483648']) {
  priceValue = invalidPrice;
  apiCalls = [];
  toasts = [];
  sandbox.doBatchPrice();
  if (apiCalls.length !== 0 || !toasts.some(item => item.type === 'error')) {
    throw new Error('invalid price reached the API: ' + invalidPrice);
  }
}

priceValue = '2147483647';
apiCalls = [];
toasts = [];
sandbox.doBatchPrice();
if (apiCalls.length !== 1) {
  throw new Error('valid boundary price did not reach the API');
}
const body = JSON.parse(apiCalls[0].options.body);
if (apiCalls[0].path !== '/api/local/batch-previews' || body.entity !== 'items' ||
    !Array.isArray(body.rows) || body.rows.length !== 1 ||
    body.rows[0].action !== 'price' || body.rows[0].data.id !== 1 ||
    body.rows[0].data.price !== 2147483647) {
  throw new Error('valid price payload was changed: ' + apiCalls[0].options.body);
}
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
