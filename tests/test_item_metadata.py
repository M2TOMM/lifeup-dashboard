import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

import server


ROOT = Path(__file__).resolve().parents[1]


class ItemMetadataRegressionTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self._tmpdir = tempfile.mkdtemp(prefix="lifeup-item-test-")
        self._db_path = os.path.join(self._tmpdir, "LifeUpDB.db")
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """
                CREATE TABLE shopitemmodel (
                    id INTEGER PRIMARY KEY,
                    itemname TEXT,
                    price INTEGER,
                    icon TEXT,
                    description TEXT,
                    stocknumber INTEGER,
                    shopcategoryid INTEGER,
                    isdisablepurchase INTEGER,
                    customusebuttontext TEXT,
                    purchaselimits TEXT,
                    extrainfo TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO shopitemmodel
                (id, itemname, price, icon, description, stocknumber,
                 shopcategoryid, isdisablepurchase, customusebuttontext,
                 purchaselimits, extrainfo)
                VALUES (1, '凡品卡包', 100, '', '原描述', -1, 2, 0, '', '[]', ?)
                """,
                (json.dumps({"limitScope": 0}),),
            )
            conn.commit()
        finally:
            conn.close()
        server.STATE.update(
            {
                "backup_path": None,
                "db_path": self._db_path,
                "tmpdir": self._tmpdir,
                "loaded": True,
            }
        )
        self.client = server.app.test_client()

    def tearDown(self):
        server.STATE.clear()
        server.STATE.update(self._old_state)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_update_item_preserves_existing_extrainfo(self):
        response = self.client.post(
            "/api/items/update",
            json={
                "id": 1,
                "name": "凡品卡包（改名）",
                "price": 120,
                "icon": "",
                "description": "新描述",
                "count": -1,
                "category_id": 2,
                "isdisablepurchase": 0,
                "customusebuttontext": "",
                "purchaselimits": "[]",
                "extrainfo": "{}",
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT itemname, price, extrainfo FROM shopitemmodel WHERE id=1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "凡品卡包（改名）")
        self.assertEqual(row[1], 120)
        self.assertEqual(json.loads(row[2]), {"limitScope": 0})

    def test_edit_form_preserves_blank_custom_use_button_text(self):
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
  classList, style: {}, textContent: '', innerHTML: '', value: '',
  addEventListener: noop, querySelector: () => null, querySelectorAll: () => [],
  setAttribute: noop
};
const document = {
  body: { classList },
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: noop,
  createElement: () => ({ ...element })
};
const localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
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
const output = sandbox.itemFormHtml(
  [],
  { id: 1, customusebuttontext: '', purchaselimits: '[]' },
  'edit'
);
const match = output.match(/id="f_use_text"[^>]+/);
if (!match || !match[0].includes('value=""')) {
  throw new Error('blank custom button text was not preserved: ' + (match ? match[0] : output));
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
