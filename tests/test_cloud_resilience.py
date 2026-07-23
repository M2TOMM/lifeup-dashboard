import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import server


ROOT = Path(__file__).resolve().parents[1]


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class CloudResilienceTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        getattr(server, "CLOUD_READ_CACHE", {}).clear()
        with server.CLOUD_RUNTIME_CONFIG_LOCK:
            server.CLOUD_RUNTIME_CONFIG["api_token"] = ""
        self.connection = {"host": "127.0.0.1", "port": 13276}

    def tearDown(self):
        getattr(server, "CLOUD_READ_CACHE", {}).clear()
        with server.CLOUD_RUNTIME_CONFIG_LOCK:
            server.CLOUD_RUNTIME_CONFIG["api_token"] = ""

    def test_configuration_errors_have_stable_codes_and_help(self):
        missing = self.client.post(
            "/api/cloud/test", json={"host": "", "port": 13276, "save": False}
        )
        invalid_port = self.client.post(
            "/api/cloud/test", json={"host": "127.0.0.1", "port": "abc", "save": False}
        )

        self.assertEqual(missing.status_code, 400, missing.get_json())
        self.assertEqual(missing.get_json()["code"], "CLOUD_HOST_MISSING")
        self.assertEqual(missing.get_json()["category"], "configuration")
        self.assertTrue(missing.get_json()["suggestion"])
        self.assertEqual(invalid_port.status_code, 400, invalid_port.get_json())
        self.assertEqual(invalid_port.get_json()["code"], "CLOUD_PORT_INVALID")

    def test_network_auth_timeout_and_response_errors_are_distinct(self):
        cases = [
            (
                URLError(ConnectionRefusedError(10061, "refused")),
                "CLOUD_CONNECTION_REFUSED",
                "network",
            ),
            (TimeoutError("timed out"), "CLOUD_TIMEOUT", "timeout"),
            (
                HTTPError(
                    "http://127.0.0.1:13276/info",
                    401,
                    "Unauthorized",
                    {},
                    io.BytesIO(b'{"message":"bad token"}'),
                ),
                "CLOUD_AUTH_FAILED",
                "authentication",
            ),
        ]
        for error, code, category in cases:
            with self.subTest(code=code), patch.object(server, "urlopen", side_effect=error):
                response = self.client.post(
                    "/api/cloud/test", json={**self.connection, "save": False}
                )
            self.assertEqual(response.get_json()["code"], code, response.get_json())
            self.assertEqual(response.get_json()["category"], category)

        with patch.object(server, "urlopen", return_value=_FakeResponse(b"not-json")):
            malformed = self.client.post(
                "/api/cloud/test", json={**self.connection, "save": False}
            )
        self.assertEqual(malformed.get_json()["code"], "CLOUD_RESPONSE_INVALID")
        self.assertEqual(malformed.get_json()["category"], "response")

    def test_read_cache_is_memory_only_and_expires(self):
        live_result = {
            "route": "/tasks",
            "base_url": "http://127.0.0.1:13276",
            "response": {"code": 200, "data": [{"id": 1}]},
            "data": [{"id": 1}],
        }
        with patch.object(server, "cloud_request", return_value=live_result) as request_mock:
            with patch.object(server.time, "time", return_value=100.0):
                first = server.cloud_cached_request(self.connection, "/tasks", cache_ttl=30)
            with patch.object(server.time, "time", return_value=110.0):
                second = server.cloud_cached_request(self.connection, "/tasks", cache_ttl=30)
            with patch.object(server.time, "time", return_value=131.0):
                third = server.cloud_cached_request(self.connection, "/tasks", cache_ttl=30)

        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(first["cache"]["source"], "live")
        self.assertEqual(second["cache"]["source"], "memory_cache")
        self.assertEqual(third["cache"]["source"], "live")
        self.assertNotIn("api_token", repr(server.CLOUD_READ_CACHE))

    def test_achievement_batch_keeps_successful_categories_when_one_fails(self):
        categories = {
            "route": "/achievement_categories",
            "base_url": "http://127.0.0.1:13276",
            "data": [{"id": 1, "name": "成长"}, {"id": 2, "name": "探索"}],
            "cache": {"source": "live", "fetched_at": "2026-07-18 15:00:00"},
        }
        category_one = {
            "route": "/achievements/1",
            "base_url": "http://127.0.0.1:13276",
            "data": [{"id": 11, "name": "第一步"}],
            "cache": {"source": "memory_cache", "fetched_at": "2026-07-18 14:59:50"},
        }

        def fake_cached(_config, route, **_kwargs):
            if route == "/achievement_categories":
                return categories
            if route == "/achievements/1":
                return category_one
            raise server.CloudRequestError(
                "CLOUD_TIMEOUT",
                "读取探索分类超时",
                "稍后只重试这个分类。",
                category="timeout",
                status=504,
                retryable=True,
            )

        with patch.object(server, "cloud_cached_request", side_effect=fake_cached):
            response = self.client.post(
                "/api/cloud/data", json={**self.connection, "dataset": "achievements"}
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload["partial"])
        self.assertEqual([row["id"] for row in payload["rows"]], [11])
        self.assertEqual(
            [item["status"] for item in payload["categories"]], ["success", "error"]
        )
        self.assertEqual(payload["errors"][0]["code"], "CLOUD_TIMEOUT")
        self.assertEqual(payload["categories"][0]["cache"]["source"], "memory_cache")

    def test_single_achievement_category_returns_cache_metadata(self):
        result = {
            "route": "/achievements/7",
            "base_url": "http://127.0.0.1:13276",
            "data": [{"id": 70, "name": "里程碑"}],
            "cache": {
                "source": "memory_cache",
                "fetched_at": "2026-07-18 15:00:00",
                "expires_at": "2026-07-18 15:00:30",
            },
        }
        with patch.object(server, "cloud_cached_request", return_value=result) as cached:
            response = self.client.post(
                "/api/cloud/data",
                json={
                    **self.connection,
                    "dataset": "achievement_category",
                    "category_id": 7,
                },
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["cache"]["source"], "memory_cache")
        self.assertEqual(payload["rows"][0]["category_id"], 7)
        cached.assert_called_once()

    def test_frontend_has_progressive_category_retry_and_cache_labels(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("cloudLoadAchievements", html)
        self.assertIn("cloudRetryAchievementCategory", html)
        self.assertIn("cloudRenderAchievementProgress", html)
        self.assertIn("memory_cache", html)
        self.assertIn("只重试此分类", html)

    def test_frontend_keeps_successes_and_retries_only_failed_category(self):
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
  cloudHost: element('127.0.0.1'), cloudPort: element('13276'),
  cloudToken: element(''), cloudDataTable: element()
};
const document = {
  body: { classList }, getElementById: (id) => elements[id] || null,
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop,
  createElement: () => element()
};
const localStorage = { getItem: () => 'cloud', setItem: noop, removeItem: noop };
const calls = [];
let categoryTwoAttempts = 0;
const response = (payload, ok = true, statusText = '') => ({
  ok, statusText, json: async () => payload
});
const fetch = async (_url, opts = {}) => {
  const body = JSON.parse(opts.body || '{}');
  calls.push(body);
  if (body.dataset === 'achievement_categories') {
    return response({
      rows: [{ id: 1, name: '成长' }, { id: 2, name: '探索' }],
      cache: { source: 'live', fetched_at: '2026-07-18 15:00:00' }
    });
  }
  if (body.dataset === 'achievement_category' && body.category_id === 1) {
    return response({
      rows: [{ id: 11, name: '第一项' }],
      cache: { source: 'memory_cache', fetched_at: '2026-07-18 14:59:55' }
    });
  }
  if (body.dataset === 'achievement_category' && body.category_id === 2) {
    categoryTwoAttempts += 1;
    if (categoryTwoAttempts === 1) {
      return response({
        code: 'CLOUD_TIMEOUT', category: 'timeout', retryable: true,
        error: '读取探索分类超时', suggestion: '稍后只重试这个分类。'
      }, false, 'Gateway Timeout');
    }
    return response({
      rows: [{ id: 22, name: '第二项' }],
      cache: { source: 'live', fetched_at: '2026-07-18 15:00:10' }
    });
  }
  throw new Error('unexpected request ' + JSON.stringify(body));
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
  await sandbox.cloudLoadData('achievements');
  const partial = elements.cloudDataTable.innerHTML;
  for (const expected of ['成长', '探索', '第一项', 'CLOUD_TIMEOUT', '只重试此分类', '已保留 1 条成功数据', '内存缓存']) {
    if (!partial.includes(expected)) throw new Error('missing partial state: ' + expected);
  }
  await sandbox.cloudRetryAchievementCategory(2);
  const recovered = elements.cloudDataTable.innerHTML;
  if (!recovered.includes('第一项') || !recovered.includes('第二项')) {
    throw new Error('successful rows were not preserved after retry');
  }
  if (recovered.includes('CLOUD_TIMEOUT')) throw new Error('recovered category still shows old error');
  const catTwoCalls = calls.filter((item) => item.dataset === 'achievement_category' && item.category_id === 2);
  if (catTwoCalls.length !== 2 || catTwoCalls[1].force_refresh !== true) {
    throw new Error('retry did not target only failed category with force_refresh');
  }
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
