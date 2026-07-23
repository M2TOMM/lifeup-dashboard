import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
import zipfile
from unittest import mock

import server


ROOT = Path(__file__).resolve().parents[1]
MAX_ITEM_PRICE = 2_147_483_647


class LocalBatchPreviewApiTests(unittest.TestCase):
    def setUp(self):
        self._old_state = dict(server.STATE)
        self.root = tempfile.mkdtemp(prefix="lifeup-local-batch-preview-")
        self.workspace = os.path.join(self.root, "workspace")
        self.database = os.path.join(
            self.workspace, "databases", "LifeUpDB.db"
        )
        self.snapshot_dir = os.path.join(self.root, "snapshots")
        os.makedirs(os.path.dirname(self.database), exist_ok=True)
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(
                """
                CREATE TABLE taskmodel (
                    id INTEGER PRIMARY KEY,
                    taskstatus INTEGER NOT NULL,
                    updatedtime INTEGER NOT NULL,
                    isdeleterecord INTEGER NOT NULL,
                    isfrozen INTEGER NOT NULL
                );
                CREATE TABLE shopitemmodel (
                    id INTEGER PRIMARY KEY,
                    price INTEGER NOT NULL,
                    isdisablepurchase INTEGER NOT NULL,
                    isdel INTEGER NOT NULL
                );
                CREATE TABLE userachievementmodel (
                    id INTEGER PRIMARY KEY,
                    content TEXT NOT NULL,
                    isdelete INTEGER NOT NULL
                );
                INSERT INTO taskmodel
                    (id, taskstatus, updatedtime, isdeleterecord, isfrozen)
                VALUES
                    (1, 1, 100, 0, 0),
                    (2, 0, 100, 0, 0),
                    (3, 0, 100, 1, 0);
                INSERT INTO shopitemmodel
                    (id, price, isdisablepurchase, isdel)
                VALUES
                    (1, 10, 0, 0),
                    (2, 20, 1, 0),
                    (3, 30, 0, 1);
                INSERT INTO userachievementmodel (id, content, isdelete)
                VALUES (1, '只读成就', 0);
                """
            )
            connection.commit()
        finally:
            connection.close()

        self.snapshot_patch = mock.patch.object(
            server, "SNAPSHOT_DIR", self.snapshot_dir
        )
        self.snapshot_patch.start()
        server.STATE.update(
            {
                "backup_path": None,
                "db_path": self.database,
                "tmpdir": self.workspace,
                "loaded": True,
            }
        )
        if hasattr(server, "LOCAL_BATCH_PREVIEWS"):
            server.LOCAL_BATCH_PREVIEWS.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        if hasattr(server, "LOCAL_BATCH_PREVIEWS"):
            server.LOCAL_BATCH_PREVIEWS.clear()
        server.STATE.clear()
        server.STATE.update(self._old_state)
        self.snapshot_patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        self.assertFalse(os.path.exists(self.root), "临时批量测试目录没有清理")

    def rows(self, table, columns):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                f"SELECT {columns} FROM {table} ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

    def create_preview(self, entity, rows, expected_status=201):
        response = self.client.post(
            "/api/local/batch-previews",
            json={"entity": entity, "rows": rows},
        )
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def execute_preview(self, preview, expected_status=200, digest=None):
        response = self.client.post(
            f"/api/local/batch-previews/{preview['preview_token']}/executions",
            json={"digest": digest if digest is not None else preview["digest"]},
        )
        self.assertEqual(response.status_code, expected_status, response.get_json())
        return response.get_json()

    def snapshot_paths(self):
        if not os.path.isdir(self.snapshot_dir):
            return []
        return sorted(Path(self.snapshot_dir).glob("snapshot-*.zip"))

    def snapshot_task_rows(self, snapshot_path):
        extracted = tempfile.mkdtemp(prefix="lifeup-batch-snapshot-assert-")
        try:
            with zipfile.ZipFile(snapshot_path, "r") as archive:
                archive.extract("databases/LifeUpDB.db", extracted)
            connection = sqlite3.connect(
                os.path.join(extracted, "databases", "LifeUpDB.db")
            )
            try:
                return connection.execute(
                    "SELECT id, taskstatus, isfrozen FROM taskmodel ORDER BY id"
                ).fetchall()
            finally:
                connection.close()
        finally:
            shutil.rmtree(extracted, ignore_errors=True)

    def test_valid_task_preview_executes_after_snapshot_and_token_is_one_time(self):
        preview = self.create_preview(
            "tasks",
            [
                {"line": 1, "action": "disable", "data": {"id": 1}},
                {"line": 2, "action": "freeze", "data": {"id": 2}},
            ],
        )

        self.assertTrue(preview["ok"])
        self.assertEqual(preview["contract_version"], 1)
        self.assertRegex(preview["preview_token"], r"^[0-9a-f]{64}$")
        self.assertRegex(preview["digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(preview["expires_in"], 600)
        self.assertEqual(preview["entity"], "tasks")
        self.assertTrue(preview["can_execute"])
        self.assertEqual(
            preview["summary"],
            {"total": 2, "ready": 2, "errors": 0, "duplicates": 0},
        )
        self.assertEqual(len(preview["rows"]), 2)
        for line, action, row in zip(
            (1, 2), ("disable", "freeze"), preview["rows"]
        ):
            self.assertEqual(row["line"], line)
            self.assertEqual(row["status"], "ready")
            self.assertEqual(row["errors"], [])
            self.assertEqual(row["normalized_data"], {"id": line})
            self.assertEqual(
                row["planned_action"],
                {
                    "entity": "tasks",
                    "action": action,
                    "data": {"id": line},
                },
            )
        self.assertEqual(self.snapshot_paths(), [])

        result = self.execute_preview(preview)

        self.assertTrue(result["ok"])
        self.assertEqual(result["contract_version"], 1)
        self.assertEqual(
            result["summary"],
            {"total": 2, "succeeded": 2, "failed": 0, "affected": 2},
        )
        self.assertEqual([row["status"] for row in result["rows"]], ["success", "success"])
        self.assertEqual([row["result"] for row in result["rows"]], [{"affected": 1}, {"affected": 1}])
        self.assertEqual(
            self.rows(
                "taskmodel", "id, taskstatus, updatedtime, isdeleterecord, isfrozen"
            )[:2],
            [(1, 0, mock.ANY, 0, 0), (2, 0, mock.ANY, 0, 1)],
        )
        snapshots = self.snapshot_paths()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(
            self.snapshot_task_rows(snapshots[0])[:2],
            [(1, 1, 0), (2, 0, 0)],
            "快照必须保存执行前的数据",
        )
        self.assertEqual(result["snapshot"]["id"], snapshots[0].stem.removeprefix("snapshot-"))

        second = self.execute_preview(preview, expected_status=409)
        self.assertEqual(second["code"], "PREVIEW_ALREADY_USED")

    def test_partial_errors_block_execution_without_snapshot_or_writes(self):
        before = self.rows(
            "taskmodel", "id, taskstatus, updatedtime, isdeleterecord, isfrozen"
        )
        preview = self.create_preview(
            "tasks",
            [
                {"line": 1, "action": "freeze", "data": {"id": 1}},
                {"line": 2, "action": "disable", "data": {"id": "bad"}},
                {"line": 3, "action": "delete", "data": {"id": 999}},
            ],
        )

        self.assertFalse(preview["can_execute"])
        self.assertEqual(
            preview["summary"],
            {"total": 3, "ready": 1, "errors": 2, "duplicates": 0},
        )
        self.assertEqual([row["status"] for row in preview["rows"]], ["ready", "error", "error"])
        self.assertTrue(
            any(error.get("field") == "data.id" for error in preview["rows"][1]["errors"])
        )
        self.assertIn(
            "TARGET_NOT_FOUND",
            [error["code"] for error in preview["rows"][2]["errors"]],
        )

        blocked = self.execute_preview(preview, expected_status=422)
        self.assertEqual(blocked["code"], "BATCH_PREVIEW_BLOCKED")
        self.assertEqual(self.snapshot_paths(), [])
        self.assertEqual(
            self.rows(
                "taskmodel", "id, taskstatus, updatedtime, isdeleterecord, isfrozen"
            ),
            before,
        )

    def test_duplicate_lines_and_target_ids_mark_every_related_row(self):
        preview = self.create_preview(
            "tasks",
            [
                {"line": 1, "action": "freeze", "data": {"id": 1}},
                {"line": 1, "action": "disable", "data": {"id": 2}},
                {"line": 3, "action": "enable", "data": {"id": 1}},
            ],
        )

        self.assertFalse(preview["can_execute"])
        self.assertEqual(preview["summary"]["duplicates"], 3)
        self.assertEqual(preview["summary"]["errors"], 3)
        codes = [
            {error["code"] for error in row["errors"]}
            for row in preview["rows"]
        ]
        self.assertTrue({"DUPLICATE_LINE", "DUPLICATE_TARGET"}.issubset(codes[0]))
        self.assertIn("DUPLICATE_LINE", codes[1])
        self.assertIn("DUPLICATE_TARGET", codes[2])
        self.assertTrue(all(row["planned_action"] is None for row in preview["rows"]))

    def test_digest_mismatch_destroys_token_and_expired_token_is_rejected(self):
        preview = self.create_preview(
            "tasks",
            [{"line": 1, "action": "freeze", "data": {"id": 1}}],
        )
        changed = self.execute_preview(
            preview, expected_status=409, digest="0" * 64
        )
        self.assertEqual(changed["code"], "PREVIEW_CONTENT_CHANGED")
        destroyed = self.execute_preview(preview, expected_status=409)
        self.assertEqual(destroyed["code"], "PREVIEW_NOT_AVAILABLE")

        expired = self.create_preview(
            "tasks",
            [{"line": 2, "action": "freeze", "data": {"id": 2}}],
        )
        server.LOCAL_BATCH_PREVIEWS[expired["preview_token"]]["expires_at"] = (
            time.time() - 1
        )
        response = self.execute_preview(expired, expected_status=409)
        self.assertEqual(response["code"], "PREVIEW_EXPIRED")
        self.assertEqual(self.snapshot_paths(), [])

    def test_malformed_digest_string_destroys_the_old_token(self):
        preview = self.create_preview(
            "tasks",
            [{"line": 1, "action": "freeze", "data": {"id": 1}}],
        )

        changed = self.execute_preview(
            preview, expected_status=409, digest="truncated-digest"
        )

        self.assertEqual(changed["code"], "PREVIEW_CONTENT_CHANGED")
        destroyed = self.execute_preview(preview, expected_status=409)
        self.assertEqual(destroyed["code"], "PREVIEW_NOT_AVAILABLE")

    def test_non_ascii_digest_string_destroys_the_old_token(self):
        preview = self.create_preview(
            "tasks",
            [{"line": 1, "action": "freeze", "data": {"id": 1}}],
        )

        changed = self.execute_preview(
            preview, expected_status=409, digest="摘要内容已经变化"
        )

        self.assertEqual(changed["code"], "PREVIEW_CONTENT_CHANGED")
        destroyed = self.execute_preview(preview, expected_status=409)
        self.assertEqual(destroyed["code"], "PREVIEW_NOT_AVAILABLE")

    def test_id_outside_sqlite_integer_range_is_a_stable_row_error(self):
        preview = self.create_preview(
            "tasks",
            [{"line": 1, "action": "freeze", "data": {"id": 2**63}}],
        )

        self.assertFalse(preview["can_execute"])
        self.assertEqual(preview["summary"]["errors"], 1)
        self.assertEqual(preview["rows"][0]["status"], "error")
        self.assertEqual(preview["rows"][0]["normalized_data"], {})
        self.assertIn(
            "INVALID_ID",
            [error["code"] for error in preview["rows"][0]["errors"]],
        )

    def test_creating_preview_removes_tokens_as_soon_as_they_expire(self):
        expired = self.create_preview(
            "tasks",
            [{"line": 1, "action": "freeze", "data": {"id": 1}}],
        )
        expired_token = expired["preview_token"]
        server.LOCAL_BATCH_PREVIEWS[expired_token]["expires_at"] = time.time() - 1

        current = self.create_preview(
            "tasks",
            [{"line": 2, "action": "freeze", "data": {"id": 2}}],
        )

        self.assertNotIn(expired_token, server.LOCAL_BATCH_PREVIEWS)
        self.assertIn(current["preview_token"], server.LOCAL_BATCH_PREVIEWS)

    def test_item_price_boundaries_execute_and_achievement_delete_is_blocked(self):
        preview = self.create_preview(
            "items",
            [
                {"line": 1, "action": "price", "data": {"id": 1, "price": 0}},
                {
                    "line": 2,
                    "action": "price",
                    "data": {"id": 2, "price": MAX_ITEM_PRICE},
                },
            ],
        )
        self.assertTrue(preview["can_execute"])
        self.execute_preview(preview)
        self.assertEqual(
            self.rows("shopitemmodel", "id, price, isdisablepurchase, isdel")[:2],
            [(1, 0, 0, 0), (2, MAX_ITEM_PRICE, 1, 0)],
        )

        achievement = self.create_preview(
            "achievements",
            [{"line": 9, "action": "delete", "data": {"id": 1}}],
        )
        self.assertFalse(achievement["can_execute"])
        self.assertEqual(achievement["rows"][0]["status"], "error")
        self.assertIsNone(achievement["rows"][0]["planned_action"])
        self.assertIn(
            "INVALID_ACTION",
            [error["code"] for error in achievement["rows"][0]["errors"]],
        )

    def test_transaction_failure_rolls_back_all_rows_and_keeps_snapshot(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(
                """
                CREATE TRIGGER fail_second_item_batch_update
                BEFORE UPDATE ON shopitemmodel
                WHEN OLD.id = 2
                BEGIN
                    SELECT RAISE(ABORT, 'secret sqlite trigger detail');
                END;
                """
            )
            connection.commit()
        finally:
            connection.close()
        before = self.rows("shopitemmodel", "id, price, isdisablepurchase, isdel")
        preview = self.create_preview(
            "items",
            [
                {"line": 1, "action": "price", "data": {"id": 1, "price": 50}},
                {"line": 2, "action": "price", "data": {"id": 2, "price": 60}},
            ],
        )

        failed = self.execute_preview(preview, expected_status=500)

        self.assertEqual(failed["code"], "BATCH_EXECUTION_FAILED")
        self.assertNotIn("secret sqlite", json.dumps(failed, ensure_ascii=False))
        self.assertEqual(
            self.rows("shopitemmodel", "id, price, isdisablepurchase, isdel"),
            before,
        )
        self.assertEqual(len(self.snapshot_paths()), 1, "失败事务前创建的快照应保留")

    def test_invalid_top_level_requests_are_flat_400_errors(self):
        cases = (
            ({"entity": "unknown", "rows": [{}]}, "INVALID_ENTITY"),
            ({"entity": "tasks", "rows": []}, "INVALID_ROWS"),
            (
                {"entity": "tasks", "rows": [{}] * 201},
                "INVALID_ROWS",
            ),
        )
        for payload, code in cases:
            with self.subTest(code=code, size=len(payload.get("rows", []))):
                response = self.client.post(
                    "/api/local/batch-previews", json=payload
                )
                self.assertEqual(response.status_code, 400, response.get_json())
                body = response.get_json()
                self.assertEqual(body["ok"], False)
                self.assertEqual(body["code"], code)
                self.assertIsInstance(body["error"], str)
                self.assertNotIn("rows", body)

    def test_cloud_source_rejects_new_and_legacy_local_batch_endpoints(self):
        task_before = self.rows(
            "taskmodel", "id, taskstatus, updatedtime, isdeleterecord, isfrozen"
        )
        item_before = self.rows(
            "shopitemmodel", "id, price, isdisablepurchase, isdel"
        )
        headers = {"X-LifeUp-Data-Source": "cloud"}
        calls = (
            (
                "/api/local/batch-previews",
                {"entity": "tasks", "rows": [{"line": 1, "action": "delete", "data": {"id": 1}}]},
            ),
            (
                "/api/local/batch-previews/" + "a" * 64 + "/executions",
                {"digest": "b" * 64},
            ),
            ("/api/tasks/batch", {"ids": [1], "action": "delete"}),
            ("/api/items/batch", {"ids": [1], "action": "delete"}),
            ("/api/tasks/batch/freeze", {"ids": [1], "isfrozen": True}),
        )
        for path, payload in calls:
            with self.subTest(path=path):
                response = self.client.post(path, json=payload, headers=headers)
                self.assertEqual(response.status_code, 403, response.get_json())
                self.assertEqual(
                    response.get_json()["code"],
                    "LOCAL_WRITE_REQUIRES_LOCAL_SOURCE",
                )
        self.assertEqual(
            self.rows(
                "taskmodel", "id, taskstatus, updatedtime, isdeleterecord, isfrozen"
            ),
            task_before,
        )
        self.assertEqual(
            self.rows("shopitemmodel", "id, price, isdisablepurchase, isdel"),
            item_before,
        )


class LocalBatchPreviewUiContractTests(unittest.TestCase):
    def test_ui_renders_safe_preview_blocks_errors_and_only_uses_new_endpoints(self):
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
const elements = new Map();
function makeElement() {
  return {
    classList, style: {}, textContent: '', innerHTML: '', value: '', disabled: false,
    addEventListener: noop, querySelector: () => null, querySelectorAll: () => [],
    setAttribute: noop, getAttribute: () => null, focus: noop
  };
}
let selectedChecks = [];
let priceValue = '2147483647';
const document = {
  body: { classList },
  getElementById: (id) => {
    if (id === 'f_batchprice') return Object.assign(makeElement(), { value: priceValue });
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  },
  querySelector: () => null,
  querySelectorAll: (selector) => selector === '.batch-check:checked' ? selectedChecks : [],
  addEventListener: noop,
  createElement: () => makeElement()
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

if (typeof sandbox.renderLocalBatchPreviewRows !== 'function' ||
    typeof sandbox.executeLocalBatchPreview !== 'function') {
  throw new Error('unified local batch preview helpers are missing');
}
if (!sandbox.isCloudReadOnlyWrite('/api/local/batch-previews', { method: 'POST' }) &&
    sandbox.dataSource === 'cloud') {
  throw new Error('new local batch endpoint is not guarded in cloud mode');
}

let apiCalls = [];
let modalBodies = [];
let toasts = [];
sandbox.showModal = (title, body) => modalBodies.push({ title, body });
sandbox.closeModal = noop;
sandbox.toast = (message, type) => toasts.push({ message, type });
sandbox.loadTasks = noop;
sandbox.loadItems = noop;

async function main() {
  selectedChecks = [{ value: '1' }];
  sandbox.api = (path, options) => {
    apiCalls.push({ path, options });
    return Promise.resolve({
      ok: true, contract_version: 1, preview_token: 'e'.repeat(64), digest: 'd'.repeat(64),
      expires_in: 600, entity: 'tasks', can_execute: false,
      summary: { total: 1, ready: 0, errors: 1, duplicates: 0 },
      rows: [{
        line: 1, status: 'error',
        errors: [{ code: 'TARGET_NOT_FOUND', field: 'data.id', message: '<img src=x onerror=boom>' }],
        normalized_data: { id: 1 }, planned_action: null
      }]
    });
  };
  await sandbox.batchAction('tasks', 'disable');
  if (apiCalls.length !== 1 || apiCalls[0].path !== '/api/local/batch-previews') {
    throw new Error('task batch did not create a preview through the new endpoint');
  }
  const unsafeHtml = modalBodies[0] && modalBodies[0].body;
  if (!unsafeHtml || unsafeHtml.includes('<img src=x') || !unsafeHtml.includes('&lt;img')) {
    throw new Error('preview error text was not safely escaped');
  }
  if (!unsafeHtml.includes('阻止执行') || !unsafeHtml.includes('disabled')) {
    throw new Error('error preview was not clearly blocked');
  }
  await sandbox.executeLocalBatchPreview();
  if (apiCalls.length !== 1) throw new Error('blocked preview reached execute endpoint');

  apiCalls = [];
  modalBodies = [];
  sandbox.api = (path, options) => {
    apiCalls.push({ path, options });
    if (path === '/api/local/batch-previews') {
      return Promise.resolve({
        ok: true, contract_version: 1, preview_token: 'a'.repeat(64), digest: 'b'.repeat(64),
        expires_in: 600, entity: 'tasks', can_execute: true,
        summary: { total: 1, ready: 1, errors: 0, duplicates: 0 },
        rows: [{ line: 1, status: 'ready', errors: [], normalized_data: { id: 1 }, planned_action: { entity: 'tasks', action: 'freeze', data: { id: 1 } } }]
      });
    }
    if (path === '/api/local/batch-previews/' + 'a'.repeat(64) + '/executions') {
      const body = JSON.parse(options.body);
      if (body.digest !== 'b'.repeat(64) || body.preview_token) {
        throw new Error('execution sent anything other than the in-memory digest');
      }
      return Promise.resolve({
        ok: true, contract_version: 1, snapshot: { id: 'c'.repeat(32), name: '批量操作前' },
        rows: [{ line: 1, status: 'success', errors: [], normalized_data: { id: 1 }, planned_action: { entity: 'tasks', action: 'freeze', data: { id: 1 } }, result: { affected: 1 } }],
        summary: { total: 1, succeeded: 1, failed: 0, affected: 1 }
      });
    }
    throw new Error('unexpected endpoint: ' + path);
  };
  await sandbox.batchAction('tasks', 'freeze');
  const readyHtml = modalBodies[0] && modalBodies[0].body;
  if (!readyHtml || !readyHtml.includes('可执行') || readyHtml.includes('id="localBatchExecuteBtn" disabled')) {
    throw new Error('ready preview was not clearly executable');
  }
  await sandbox.executeLocalBatchPreview();
  const paths = apiCalls.map((entry) => entry.path);
  if (paths.length !== 2 || paths.some((path) => path === '/api/tasks/batch' || path === '/api/tasks/batch/freeze')) {
    throw new Error('valid task flow called a legacy endpoint: ' + paths.join(','));
  }
  if (!toasts.some((entry) => entry.message.includes('快照'))) {
    throw new Error('successful execution did not show snapshot feedback');
  }

  apiCalls = [];
  sandbox.api = (path, options) => {
    apiCalls.push({ path, options });
    return Promise.resolve({
      ok: true, contract_version: 1, preview_token: 'f'.repeat(64), digest: '1'.repeat(64),
      expires_in: 600, entity: 'items', can_execute: true,
      summary: { total: 1, ready: 1, errors: 0, duplicates: 0 },
      rows: [{ line: 1, status: 'ready', errors: [], normalized_data: { id: 1, price: 2147483647 }, planned_action: { entity: 'items', action: 'price', data: { id: 1, price: 2147483647 } } }]
    });
  };
  await sandbox.doBatchPrice();
  const itemBody = JSON.parse(apiCalls[0].options.body);
  if (apiCalls[0].path !== '/api/local/batch-previews' || itemBody.entity !== 'items' ||
      itemBody.rows[0].action !== 'price' || itemBody.rows[0].data.price !== 2147483647) {
    throw new Error('item price did not use the unified preview contract');
  }

  sandbox.updateBatchBar('items');
  if (sandbox.localBatchPreviewState !== null) {
    throw new Error('selection change retained an old preview token');
  }

  apiCalls = [];
  sandbox.dataSource = 'cloud';
  if (!sandbox.isCloudReadOnlyWrite('/api/local/batch-previews', { method: 'POST' })) {
    throw new Error('cloud guard missed the new local preview endpoint');
  }
  await sandbox.batchAction('tasks', 'delete');
  if (apiCalls.length !== 0) throw new Error('cloud mode entered local batch preview');
}

main().catch((error) => {
  console.error(error && error.stack || error);
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
