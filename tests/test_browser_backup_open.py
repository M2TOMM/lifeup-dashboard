import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]


class BrowserBackupOpenTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.client = server.app.test_client()

    def tearDown(self):
        active_tmpdir = server.STATE.get("tmpdir")
        server.STATE.clear()
        server.STATE.update(self._old_state)
        if active_tmpdir and active_tmpdir != self._old_state.get("tmpdir"):
            shutil.rmtree(active_tmpdir, ignore_errors=True)

    def make_backup(self):
        with tempfile.TemporaryDirectory(prefix="lifeup-upload-source-") as tmpdir:
            db_dir = os.path.join(tmpdir, "databases")
            os.makedirs(db_dir)
            db_path = os.path.join(db_dir, "LifeUpDB.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(db_path, "databases/LifeUpDB.db")
            output.seek(0)
            return output

    def test_browser_upload_creates_and_loads_workspace_copy(self):
        with tempfile.TemporaryDirectory(prefix="lifeup-browser-import-") as import_dir, patch.object(
            server, "BROWSER_IMPORT_DIR", import_dir, create=True
        ):
            response = self.client.post(
                "/api/open-upload",
                data={"files": (self.make_backup(), "LifeupBackup.zip")},
                content_type="multipart/form-data",
            )

            self.assertEqual(response.status_code, 200, response.get_json())
            payload = response.get_json()
            self.assertTrue(payload["workspace_copy"])
            self.assertTrue(os.path.exists(payload["path"]))
            self.assertEqual(os.path.commonpath([payload["path"], import_dir]), import_dir)
            self.assertEqual(server.STATE["backup_path"], payload["path"])

    def test_web_picker_and_windows_path_rendering(self):
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
let clickedInput = null;
const created = [];
const document = {
  body: { classList, appendChild: (el) => created.push(el) },
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
  addEventListener: noop,
  createElement: (tag) => {
    const el = { tag, classList, style: {}, addEventListener: noop, remove: noop };
    el.click = () => { clickedInput = el; };
    return el;
  }
};
const savedPath = 'C:\\Users\\USER\\LifeUp\\LifeupBackup.zip';
const localStorage = {
  getItem: (key) => key === 'lifeup_backup_path' ? savedPath : null,
  setItem: noop, removeItem: noop
};
const sandbox = {
  console, document, localStorage, window: { addEventListener: noop },
  location: { protocol: 'http:', origin: 'http://127.0.0.1:5000' },
  setTimeout: (fn) => fn(), clearTimeout: noop, URLSearchParams, AbortController,
  fetch: noop, confirm: () => false, alert: noop, Blob, URL, FormData
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
sandbox.window.location = sandbox.location;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
let modalHtml = '';
sandbox.showModal = (title, body) => { modalHtml = body; };
sandbox.renderQuickOpenModal();
if (!modalHtml.includes('value="' + savedPath + '"')) {
  throw new Error('Windows path was altered in the input: ' + modalHtml);
}
sandbox.openBrowserFileDialog(false);
if (!clickedInput || clickedInput.type !== 'file' || clickedInput.accept !== '.zip,application/zip') {
  throw new Error('web file picker was not created');
}
"""
        result = subprocess.run(
            [node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_direct_file_mode_does_not_attempt_api_fetch(self):
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
const elements = {
  content: { innerHTML: '', classList, style: {} },
  connText: { textContent: '' }, connDot: { className: '' }
};
const document = {
  body: { classList }, getElementById: (id) => elements[id] || null,
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop,
  createElement: () => ({ classList, style: {}, addEventListener: noop })
};
const localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
let fetchCalls = 0;
const sandbox = {
  console, document, localStorage, window: { addEventListener: noop },
  location: { protocol: 'file:', origin: 'null' },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  fetch: () => { fetchCalls += 1; }, confirm: () => false, alert: noop, Blob, URL, FormData
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
sandbox.window.location = sandbox.location;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
(async () => {
  try { await sandbox._loadBackup('C:\\bad.zip'); } catch (error) {}
  if (fetchCalls !== 0) throw new Error('file mode still attempted an API fetch');
  if (!elements.content.innerHTML.includes('http://127.0.0.1:5000')) {
    throw new Error('file mode did not explain how to open the working app');
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

    def test_failed_replacement_keeps_loaded_workspace_visible(self):
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
const elements = {
  content: { innerHTML: '', classList, style: {} },
  connText: { textContent: '' }, connDot: { className: '' },
  saveBtn: { disabled: true }, filePath: { textContent: '', title: '' },
  toast: { textContent: '', className: '', classList, style: {} }
};
const document = {
  body: { classList }, getElementById: (id) => elements[id] || null,
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop,
  createElement: () => ({ classList, style: {}, addEventListener: noop })
};
const localStorage = { getItem: () => 'local', setItem: noop, removeItem: noop };
const response = (ok, payload, status = 200) => ({
  ok, status, statusText: ok ? 'OK' : 'Bad Request', json: async () => payload
});
const requests = [];
const fetch = async (url) => {
  requests.push(url);
  if (url.endsWith('/api/open')) {
    return response(false, {
      error: '备份中未找到 databases/LifeUpDB.db',
      suggestion: '请从 LifeUp App 重新导出完整备份。'
    }, 400);
  }
  if (url.endsWith('/api/status')) {
    return response(true, {
      loaded: true, backup_path: 'C:\\workspace\\baseline.zip', filename: 'baseline.zip'
    });
  }
  throw new Error('unexpected request: ' + url);
};
const sandbox = {
  console, document, localStorage, fetch, window: { addEventListener: noop },
  location: { protocol: 'http:', origin: 'http://127.0.0.1:5000' },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  confirm: () => false, alert: noop, Blob, URL, FormData
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
sandbox.window.location = sandbox.location;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
let pageReloads = 0;
let shownError = null;
sandbox.loadCurrentPage = () => { pageReloads += 1; };
sandbox.showLoadError = (message, suggestion, preservePage) => {
  shownError = { message, suggestion, preservePage };
};
(async () => {
  try { await sandbox._loadBackup('C:\\bad.zip'); } catch (error) {}
  if (requests.length !== 2 || !requests[1].endsWith('/api/status')) {
    throw new Error('failed load did not re-check the active workspace');
  }
  if (pageReloads !== 1 || elements.connText.textContent !== '已加载') {
    throw new Error('active workspace was not restored in the UI');
  }
  if (!shownError || !shownError.preservePage || !shownError.suggestion.includes('重新导出完整备份')) {
    throw new Error('server suggestion was not shown without replacing the current page');
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
