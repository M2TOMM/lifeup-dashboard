import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]


class CloudTaskComposerBackendTests(unittest.TestCase):
    def setUp(self):
        getattr(server, "CLOUD_PREVIEWS", {}).clear()
        getattr(server, "CLOUD_EXECUTIONS", {}).clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tempdir.name) / "cloud-operation-log.jsonl"
        self.log_patch = patch.object(
            server, "CLOUD_OPERATION_LOG_PATH", str(self.log_path)
        )
        self.log_patch.start()
        self.client = server.app.test_client()
        self.connection = {
            "host": "127.0.0.1",
            "port": 13276,
            "api_token": "never-write-this-token",
        }

    def tearDown(self):
        self.log_patch.stop()
        self.tempdir.cleanup()
        getattr(server, "CLOUD_PREVIEWS", {}).clear()
        getattr(server, "CLOUD_EXECUTIONS", {}).clear()

    def test_preview_validation_identifies_row_and_field(self):
        response = self.client.post(
            "/api/cloud/preview",
            json={
                **self.connection,
                "urls": [
                    "lifeup://api/add_task?todo=Good&coin=1",
                    "lifeup://api/add_task?todo=Bad&difficulty=9",
                ],
            },
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["code"], "CLOUD_TASK_VALIDATION_FAILED")
        self.assertEqual(payload["errors"][0]["row"], 2)
        self.assertEqual(payload["errors"][0]["field"], "difficulty")
        self.assertNotIn("preview_token", payload)

    def test_execution_report_is_per_item_and_idempotent_without_secrets(self):
        urls = [
            "lifeup://api/add_task?todo=First",
            "lifeup://api/add_task?todo=Second",
        ]
        preview_response = self.client.post(
            "/api/cloud/preview", json={**self.connection, "urls": urls}
        )
        self.assertEqual(preview_response.status_code, 200, preview_response.get_json())
        token = preview_response.get_json()["preview_token"]
        fake_result = {
            "route": "/api/contentprovider",
            "base_url": "http://127.0.0.1:13276",
            "data": [{"ok": True}, {"ok": False, "message": "token echoed?"}],
            "response": {
                "code": 200,
                "data": [{"ok": True}, {"ok": False}],
                "api_token": self.connection["api_token"],
            },
        }

        with patch.object(server, "cloud_post_json", return_value=fake_result) as cloud_post:
            first = self.client.post(
                "/api/cloud/execute",
                json={"preview_token": token, "idempotency_key": "raw-idempotency-key"},
            )
            replay = self.client.post(
                "/api/cloud/execute",
                json={"preview_token": token, "idempotency_key": "raw-idempotency-key"},
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        payload = first.get_json()
        self.assertEqual(
            [item["status"] for item in payload["results"]],
            ["success", "failed"],
        )
        self.assertEqual([item["row"] for item in payload["results"]], [1, 2])
        self.assertEqual(payload["summary"], {"success": 1, "failed": 1, "unknown": 0})
        self.assertNotIn("raw", payload)
        self.assertTrue(replay.get_json()["idempotent_replay"])
        self.assertEqual(replay.get_json()["operation_id"], payload["operation_id"])
        cloud_post.assert_called_once()

        operations = self.client.get("/api/cloud/operations").get_json()["operations"]
        self.assertEqual([record["type"] for record in operations], ["execute", "preview"])
        serialized = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(self.connection["api_token"], serialized)
        self.assertNotIn("raw-idempotency-key", serialized)
        self.assertNotIn(token, serialized)
        self.assertIn("idempotency_digest", serialized)

    def test_operation_report_can_be_exported_as_safe_json(self):
        response = self.client.post(
            "/api/cloud/preview",
            json={
                **self.connection,
                "urls": ["lifeup://api/add_task?todo=Exported"],
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())

        exported = self.client.get("/api/cloud/operations/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers.get("Content-Disposition", ""))
        records = json.loads(exported.get_data(as_text=True))
        self.assertEqual(records[0]["type"], "preview")
        self.assertNotIn("preview_token", records[0])
        self.assertNotIn(self.connection["api_token"], exported.get_data(as_text=True))

    def test_uncertain_write_is_recorded_with_stable_error_without_retry(self):
        preview_response = self.client.post(
            "/api/cloud/preview",
            json={
                **self.connection,
                "urls": ["lifeup://api/add_task?todo=Uncertain"],
            },
        )
        token = preview_response.get_json()["preview_token"]
        error = server.CloudRequestError(
            "CLOUD_TIMEOUT",
            "连接手机超时",
            "请先查看手机任务，不要立即重复点击。",
            category="timeout",
            status=504,
            retryable=False,
        )

        with patch.object(server, "cloud_post_json", side_effect=error) as cloud_post:
            response = self.client.post(
                "/api/cloud/execute",
                json={"preview_token": token, "idempotency_key": "uncertain-key"},
            )

        self.assertEqual(response.status_code, 504, response.get_json())
        self.assertEqual(response.get_json()["code"], "CLOUD_TIMEOUT")
        self.assertFalse(response.get_json()["retryable"])
        records = self.client.get("/api/cloud/operations").get_json()["operations"]
        self.assertEqual(records[0]["status"], "uncertain")
        self.assertEqual(records[0]["summary"]["unknown"], 1)
        self.assertNotIn("uncertain-key", self.log_path.read_text(encoding="utf-8"))
        cloud_post.assert_called_once()

    def test_cloud_templates_are_downloadable(self):
        csv_response = self.client.get("/api/cloud/task-import-templates/csv")
        json_response = self.client.get("/api/cloud/task-import-templates/json")

        self.assertEqual(csv_response.status_code, 200)
        self.assertTrue(csv_response.get_data(as_text=True).startswith("\ufefftodo,"))
        self.assertIn("attachment", csv_response.headers["Content-Disposition"])
        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(json.loads(json_response.get_data(as_text=True))[0]["todo"], "写日记")


class CloudTaskComposerFrontendTests(unittest.TestCase):
    def test_live_options_name_mapping_and_precise_batch_errors(self):
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
  cloudHost: element('127.0.0.1'), cloudPort: element('13276'), cloudToken: element('secret'),
  cloudComposerStatus: element(), cloudCategorySearch: element(), cloudCategory: element(),
  cloudSkillSearch: element(), cloudSkillOptions: element(), cloudSkills: element(),
  cloudOperationLog: element()
};
const document = {
  body: { classList }, getElementById: (id) => elements[id] || null,
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop,
  createElement: () => element()
};
const localStorage = { getItem: () => 'cloud', setItem: noop, removeItem: noop };
const requests = [];
const response = (payload) => ({ ok: true, json: async () => payload });
const fetch = async (url, opts = {}) => {
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({ url, body });
  if (body && body.dataset === 'task_categories') return response({ rows: [{ id: 7, name: '每日清单' }], cache: { source: 'live' } });
  if (body && body.dataset === 'skills') return response({ rows: [{ id: 8, content: '心境' }], cache: { source: 'live' } });
  throw new Error('unexpected request: ' + url);
};
const sandbox = {
  console, document, localStorage, fetch, window: { addEventListener: noop },
  setTimeout: noop, clearTimeout: noop, URLSearchParams, AbortController,
  confirm: () => false, alert: noop, Blob, URL
};
sandbox.window.window = sandbox.window;
sandbox.window.document = document;
sandbox.window.localStorage = localStorage;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

(async () => {
  await sandbox.cloudLoadComposerOptions(true);
  if (requests.length !== 2 || !elements.cloudCategory.innerHTML.includes('每日清单')) {
    throw new Error('task categories were not loaded from the current cloud connection');
  }
  if (!elements.cloudSkillOptions.innerHTML.includes('心境')) throw new Error('skills were not rendered');

  const csvRows = sandbox.cloudParseCsv('todo,coin,category,skills\r\nGood,1,每日清单,心境\r\nBad,abc,每日清单,心境');
  const valid = sandbox.cloudNormalizeBatchRow(csvRows[0], 0);
  const url = sandbox.cloudBuildTaskUrl(valid);
  if (!url.includes('category=7') || !url.includes('skills=8')) throw new Error('names did not map to live IDs');
  let error;
  try { sandbox.cloudBuildTaskUrl(sandbox.cloudNormalizeBatchRow(csvRows[1], 1)); }
  catch (caught) { error = caught; }
  if (!error || error.field !== 'coin' || csvRows[1].__sourceRow !== 3) {
    throw new Error('CSV error did not identify source row 3 and field coin');
  }

  sandbox.cloudRenderOperations([{ type: 'execute', created_at: '2026-07-18T12:00:00+08:00', count: 1,
    idempotency_digest: 'safe-digest', summary: { success: 1, failed: 0, unknown: 0 },
    items: [{ row: 1, title: 'Good', status: 'success', message: '手机接口已确认成功' }] }]);
  if (!elements.cloudOperationLog.innerHTML.includes('safe-digest') || !elements.cloudOperationLog.innerHTML.includes('Good')) {
    throw new Error('safe operation report was not rendered');
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
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
