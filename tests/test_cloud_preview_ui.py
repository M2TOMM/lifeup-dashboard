import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CloudPreviewUiTests(unittest.TestCase):
    def test_task_url_builder_rejects_invalid_batch_numbers(self):
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
const document = {
  body: { classList }, getElementById: () => null, querySelector: () => null,
  querySelectorAll: () => [], addEventListener: noop,
  createElement: () => ({ classList, style: {}, addEventListener: noop })
};
const localStorage = { getItem: () => 'cloud', setItem: noop, removeItem: noop };
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
const invalid = [
  { todo: 'Bad', coin: 'abc' },
  { todo: 'Bad', coin: '-1' },
  { todo: 'Bad', importance: '999' },
  { todo: 'Bad', difficulty: '0' },
  { todo: 'Bad', skills: 'abc' },
  { todo: 'Bad', frequency: 'abc' }
];
for (const input of invalid) {
  let failed = false;
  try { sandbox.cloudBuildTaskUrl(input); } catch (error) { failed = true; }
  if (!failed) throw new Error('invalid task parameters accepted: ' + JSON.stringify(input));
}
"""
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_preview_token_is_used_and_execute_button_is_locked(self):
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
const element = (value = '') => ({
  value, textContent: '', innerHTML: '', disabled: false, files: [],
  classList, style: {}, addEventListener: noop, querySelector: () => null,
  querySelectorAll: () => [], setAttribute: noop
});
const elements = {
  cloudHost: element('127.0.0.1'), cloudPort: element('13276'), cloudToken: element('token'),
  cloudTodo: element('Previewed'), cloudNotes: element(''), cloudCoin: element('1'),
  cloudExp: element('2'), cloudSkills: element(''), cloudCategory: element(''),
  cloudLevels: element('2,3'), cloudTaskPreview: element(), cloudStatus: element(),
  cloudExecuteTaskBtn: element(), cloudDataTable: element()
};
const document = {
  body: { classList }, getElementById: (id) => elements[id] || null,
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop,
  createElement: () => element()
};
const localStorage = { getItem: () => 'cloud', setItem: noop, removeItem: noop };
const requests = [];
let finishExecute;
const response = (payload) => ({ ok: true, json: async () => payload });
const fetch = async (url, opts = {}) => {
  requests.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
  if (url.endsWith('/api/cloud/preview')) {
    return response({ ok: true, count: 1, preview_token: 'preview-token', expires_in: 600 });
  }
  if (url.endsWith('/api/cloud/execute')) {
    return await new Promise((resolve) => {
      finishExecute = () => resolve(response({ ok: true, count: 1, results: [] }));
    });
  }
  if (url.endsWith('/api/cloud/data')) return response({ ok: true, dataset: 'tasks', rows: [] });
  throw new Error('unexpected request: ' + url);
};
const sandbox = {
  console, document, localStorage, fetch, window: { addEventListener: noop },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  confirm: () => true, alert: noop, Blob, URL,
  crypto: { randomUUID: () => 'idempotency-key' }
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

(async () => {
  await sandbox.cloudPreviewTask();
  if (requests.length !== 1 || !requests[0].url.endsWith('/api/cloud/preview')) {
    throw new Error('preview was not registered with the server');
  }
  if (elements.cloudExecuteTaskBtn.disabled) throw new Error('execute button stayed disabled after preview');

  const first = sandbox.cloudExecuteTask();
  const second = sandbox.cloudExecuteTask();
  await Promise.resolve();
  const executeRequests = requests.filter((item) => item.url.endsWith('/api/cloud/execute'));
  if (executeRequests.length !== 1) throw new Error('duplicate execute request was sent');
  if (!elements.cloudExecuteTaskBtn.disabled) throw new Error('execute button was not locked');
  const body = executeRequests[0].body;
  if (body.preview_token !== 'preview-token' || body.idempotency_key !== 'task-idempotency-key' || body.urls) {
    throw new Error('execute was not bound to the preview token');
  }
  finishExecute();
  await Promise.all([first, second]);

  sandbox.cloudInvalidateTaskPreview();
  if (!elements.cloudExecuteTaskBtn.disabled) throw new Error('editing did not invalidate the preview');
})().catch((error) => {
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
