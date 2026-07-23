import ast
import json
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import mcp_server


PROJECT_DIR = Path(__file__).resolve().parents[1]


class QuietHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class FakeDashboardHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, format, *args):
        pass

    def _record(self, body=None):
        parsed = urlparse(self.path)
        self.__class__.requests.append({
            "method": self.command,
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "body": body,
        })
        return parsed

    def _json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = self._record()
        if parsed.path == "/api/status":
            self._json(200, {
                "loaded": True,
                "filename": "managed-copy.zip",
                "backup_path": r"C:\private\managed-copy.zip",
            })
            return
        if parsed.path == "/api/cloud/config":
            self._json(200, {
                "host": "192.168.1.9",
                "port": 13276,
                "base_url": "http://192.168.1.9:13276",
                "api_token_in_memory": True,
                "api_token_saved": False,
            })
            return
        if parsed.path == "/api/tasks":
            self._json(200, [{"id": 1, "title": "Read only 🧪", "api_token": "secret"}])
            return
        if parsed.path == "/api/items":
            self._json(200, [{"id": 2, "name": "Tea"}])
            return
        if parsed.path == "/api/achievements":
            self._json(200, [{"id": 3, "name": "Focus"}])
            return
        if parsed.path == "/api/dashboard/overview":
            self._json(200, {"source": parse_qs(parsed.query).get("source", [None])[0]})
            return
        if parsed.path == "/api/focus/overview":
            self._json(200, {"todayMinutes": 25})
            return
        if parsed.path == "/api/goals":
            if parse_qs(parsed.query).get("source") == ["cloud"]:
                self._json(403, {
                    "code": "GOAL_MAPPING_LOCAL_ONLY",
                    "error": "local only",
                    "authorization": "Bearer secret",
                })
            else:
                self._json(200, {"goals": []})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        parsed = self._record(body)
        if parsed.path == "/api/cloud/preview":
            self._json(200, {
                "ok": True,
                "preview_token": "p" * 32,
                "count": 1,
                "api_token": "must-not-leak",
            })
            return
        if parsed.path == "/api/cloud/execute":
            self._json(200, {
                "ok": True,
                "idempotent_replay": False,
                "base_url": "http://phone.private:13276",
            })
            return
        self._json(404, {"error": "not found"})


class MCPServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = QuietHTTPServer(("127.0.0.1", 0), FakeDashboardHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        FakeDashboardHandler.requests.clear()
        self.client = mcp_server.DashboardClient(self.base_url, timeout=2)
        self.server = mcp_server.MCPServer(self.client)

    def test_base_url_is_limited_to_loopback_without_credentials_or_paths(self):
        self.assertEqual(
            mcp_server.normalize_base_url("http://localhost:5000/"),
            "http://localhost:5000",
        )
        self.assertEqual(
            mcp_server.normalize_base_url("http://[::1]:5000"),
            "http://[::1]:5000",
        )
        invalid = [
            "https://127.0.0.1:5000",
            "http://192.168.1.3:5000",
            "http://user:pass@127.0.0.1:5000",
            "http://127.0.0.1:5000/api/status",
            "http://127.0.0.1",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                mcp_server.normalize_base_url(value)
        with self.assertRaises(ValueError):
            self.client.request("POST", "/api/save", body={})

    def test_adapter_does_not_import_dashboard_backend_or_storage_modules(self):
        tree = ast.parse((PROJECT_DIR / "mcp_server.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue({"server", "sqlite3", "zipfile"}.isdisjoint(imported))

    def test_initialize_and_tool_discovery_publish_safe_explicit_contracts(self):
        initialized = self.server.handle({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        fallback = self.server.handle({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        })
        self.assertEqual(
            fallback["result"]["protocolVersion"], mcp_server.DEFAULT_PROTOCOL_VERSION
        )
        discovered = self.server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
        })
        tools = discovered["result"]["tools"]
        self.assertEqual(len(tools), 9)
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "lifeup_get_status",
                "lifeup_list_tasks",
                "lifeup_list_items",
                "lifeup_list_achievements",
                "lifeup_get_dashboard",
                "lifeup_get_focus",
                "lifeup_get_wishes",
                "lifeup_preview_task",
                "lifeup_create_task",
            },
        )
        for tool in tools:
            self.assertIn("source", tool["inputSchema"]["required"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        serialized = json.dumps(tools)
        self.assertNotIn("api_token", serialized)
        self.assertNotIn("delete", serialized.casefold())
        self.assertNotIn("update", serialized.casefold())

    def test_read_tools_forward_explicit_source_and_filters(self):
        result = mcp_server.call_tool(self.client, "lifeup_list_tasks", {
            "source": "local",
            "filter": "active",
            "search": "Read",
            "category_id": 7,
        })
        self.assertEqual(result["source"], "local")
        self.assertEqual(result["data"][0]["title"], "Read only 🧪")
        self.assertNotIn("api_token", result["data"][0])
        recorded = FakeDashboardHandler.requests[-1]
        self.assertEqual(recorded["path"], "/api/tasks")
        self.assertEqual(recorded["query"]["source"], ["local"])
        self.assertEqual(recorded["query"]["filter"], ["active"])
        self.assertEqual(recorded["query"]["category_id"], ["7"])

    def test_status_does_not_expose_workspace_or_phone_configuration(self):
        local = mcp_server.call_tool(
            self.client, "lifeup_get_status", {"source": "local"}
        )
        cloud = mcp_server.call_tool(
            self.client, "lifeup_get_status", {"source": "cloud"}
        )
        self.assertTrue(local["data"]["loaded"])
        self.assertNotIn("backup_path", local["data"])
        self.assertNotIn("filename", local["data"])
        self.assertTrue(cloud["data"]["api_token_in_memory"])
        for key in ("host", "port", "base_url"):
            self.assertNotIn(key, cloud["data"])

    def test_invalid_source_and_unconfirmed_write_send_no_http_request(self):
        with self.assertRaises(mcp_server.ToolInputError):
            mcp_server.call_tool(self.client, "lifeup_list_items", {})
        with self.assertRaises(mcp_server.ToolInputError):
            mcp_server.call_tool(self.client, "lifeup_list_items", {"source": "auto"})
        with self.assertRaises(mcp_server.ToolInputError):
            mcp_server.call_tool(self.client, "lifeup_create_task", {
                "source": "cloud",
                "preview_token": "p" * 32,
                "confirmed": False,
                "idempotency_key": "mcp-test-key",
            })
        self.assertEqual(FakeDashboardHandler.requests, [])

    def test_preview_and_confirm_use_server_token_and_idempotency_flow(self):
        url = "lifeup://api/add_task?todo=Safe%20task"
        preview = mcp_server.call_tool(self.client, "lifeup_preview_task", {
            "source": "cloud", "lifeup_url": url
        })
        token = preview["data"]["preview_token"]
        self.assertNotIn("api_token", preview["data"])
        self.assertEqual(FakeDashboardHandler.requests[-1]["body"]["urls"], [url])

        executed = mcp_server.call_tool(self.client, "lifeup_create_task", {
            "source": "cloud",
            "preview_token": token,
            "confirmed": True,
            "idempotency_key": "mcp-safe-task-1",
        })
        self.assertTrue(executed["data"]["ok"])
        self.assertNotIn("base_url", executed["data"])
        self.assertEqual(FakeDashboardHandler.requests[-1]["body"], {
            "preview_token": token,
            "idempotency_key": "mcp-safe-task-1",
        })

    def test_http_errors_and_timeouts_are_safe_tool_results(self):
        response = self.server.handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "lifeup_get_wishes", "arguments": {"source": "cloud"}},
        })
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["http_status"], 403)
        self.assertNotIn("authorization", json.dumps(result).casefold())

        with patch.object(self.client._opener, "open", side_effect=socket.timeout()):
            timed_out = self.server.handle({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "lifeup_get_focus", "arguments": {"source": "local"}},
            })
        self.assertTrue(timed_out["result"]["isError"])
        self.assertIn("无法连接本机", timed_out["result"]["content"][0]["text"])

    def test_stdio_process_supports_initialize_discovery_and_read_call(self):
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "lifeup_list_tasks",
                    "arguments": {"source": "local", "filter": "all"},
                },
            },
        ]
        stdin_text = "".join(json.dumps(message) + "\n" for message in messages)
        completed = subprocess.run(
            [sys.executable, str(PROJECT_DIR / "mcp_server.py"), "--base-url", self.base_url],
            input=stdin_text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=PROJECT_DIR,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3])
        self.assertEqual(len(responses[1]["result"]["tools"]), 9)
        self.assertEqual(
            responses[2]["result"]["structuredContent"]["data"][0]["title"],
            "Read only 🧪",
        )


if __name__ == "__main__":
    unittest.main()
