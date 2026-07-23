"""LifeUp Dashboard MCP adapter.

This process speaks MCP over stdio and delegates every business operation to
the already-running local Flask server. It never opens LifeUp databases,
backup ZIP files, mapping files, or cloud credentials directly.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


SERVER_NAME = "lifeup-dashboard"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
DEFAULT_BASE_URL = "http://127.0.0.1:5000"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_API_PATHS = {
    "/api/status",
    "/api/cloud/config",
    "/api/tasks",
    "/api/items",
    "/api/achievements",
    "/api/dashboard/overview",
    "/api/focus/overview",
    "/api/goals",
    "/api/cloud/preview",
    "/api/cloud/execute",
}
SENSITIVE_KEYS = {
    "api_token",
    "apitoken",
    "authorization",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "token",
    "backup_path",
    "db_path",
    "filename",
    "base_url",
    "host",
    "port",
}


class AdapterError(Exception):
    """Base class for safe, user-facing adapter errors."""


class ToolInputError(AdapterError):
    """The MCP caller supplied invalid tool arguments."""


@dataclass
class DashboardHTTPError(AdapterError):
    status: int
    payload: Any

    def __str__(self) -> str:
        return f"LifeUp Dashboard HTTP {self.status}"


class DashboardConnectionError(AdapterError):
    """The local Dashboard could not be reached safely."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_base_url(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme != "http":
        raise ValueError("Dashboard 地址必须使用 http://")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("Dashboard 地址只允许 localhost、127.0.0.1 或 ::1")
    if parsed.username or parsed.password:
        raise ValueError("Dashboard 地址不能包含用户名、密码或 Token")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Dashboard 地址只能包含本机主机名和端口")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Dashboard 端口无效") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("Dashboard 地址必须包含 1 到 65535 之间的端口")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"http://{host}:{port}"


def sanitize_payload(value: Any) -> Any:
    """Remove credential-shaped fields without removing preview_token."""
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            normalized = str(key).strip().casefold()
            if normalized in SENSITIVE_KEYS:
                continue
            safe[key] = sanitize_payload(item)
        return safe
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


class DashboardClient:
    """Small allowlisted HTTP client for the local Flask service."""

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.base_url = normalize_base_url(base_url)
        self.timeout = float(timeout)
        if not 0.1 <= self.timeout <= 120:
            raise ValueError("HTTP 超时必须在 0.1 到 120 秒之间")
        self._opener = build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        if path not in ALLOWED_API_PATHS:
            raise ValueError("MCP 只能调用预先定义的 Dashboard API 路径")
        url = self.base_url + path
        if query:
            filtered = {key: value for key, value in query.items() if value is not None}
            if filtered:
                url += "?" + urlencode(filtered)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = int(response.status)
                payload = self._read_json(response)
        except HTTPError as exc:
            payload = self._read_json(exc)
            raise DashboardHTTPError(int(exc.code), sanitize_payload(payload)) from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise DashboardConnectionError(
                f"无法连接本机 LifeUp Dashboard：{self.base_url}。"
                "请先在另一个 PowerShell 窗口运行 python server.py。"
            ) from None
        return status, sanitize_payload(payload)

    @staticmethod
    def _read_json(response) -> Any:
        length = response.headers.get("Content-Length") if response.headers else None
        if length:
            try:
                if int(length) > MAX_RESPONSE_BYTES:
                    raise DashboardConnectionError("Dashboard 响应过大，MCP 已停止读取")
            except ValueError:
                pass
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DashboardConnectionError("Dashboard 响应过大，MCP 已停止读取")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DashboardConnectionError("Dashboard 返回了无效的 JSON 响应") from None


SOURCE_SCHEMA = {
    "type": "string",
    "enum": ["local", "cloud"],
    "description": "必须明确选择 local（工作副本）或 cloud（手机云人升）",
}


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "lifeup_get_status",
        "description": "读取所选数据源的连接/载入状态，不修改任何数据。",
        "inputSchema": _object_schema({"source": SOURCE_SCHEMA}, ["source"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "lifeup_list_tasks",
        "description": "查询任务列表。必须明确指定 local 或 cloud。",
        "inputSchema": _object_schema(
            {
                "source": SOURCE_SCHEMA,
                "filter": {"type": "string", "enum": ["all", "active", "done", "frozen"]},
                "search": {"type": "string", "maxLength": 200},
                "category_id": {"type": "integer", "minimum": 1},
            },
            ["source"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "lifeup_list_items",
        "description": "查询商品列表。必须明确指定 local 或 cloud。",
        "inputSchema": _object_schema(
            {
                "source": SOURCE_SCHEMA,
                "search": {"type": "string", "maxLength": 200},
                "category_id": {"type": "integer", "minimum": 1},
            },
            ["source"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "lifeup_list_achievements",
        "description": "查询自定义成就和子分类。系统成就及手机已有成就保持只读。",
        "inputSchema": _object_schema(
            {
                "source": SOURCE_SCHEMA,
                "search": {"type": "string", "maxLength": 200},
                "category_id": {"type": "integer", "minimum": 1},
            },
            ["source"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "lifeup_get_dashboard",
        "description": "读取 Dashboard 总览统计。必须明确指定数据源。",
        "inputSchema": _object_schema({"source": SOURCE_SCHEMA}, ["source"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "lifeup_get_focus",
        "description": "读取闭关/番茄专注统计。必须明确指定数据源。",
        "inputSchema": _object_schema({"source": SOURCE_SCHEMA}, ["source"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "lifeup_get_wishes",
        "description": "读取真实宏愿映射；当前仅 local 可用，cloud 会返回明确的只读能力缺口。",
        "inputSchema": _object_schema({"source": SOURCE_SCHEMA}, ["source"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "lifeup_preview_task",
        "description": (
            "预览一个手机云端新增任务请求，不执行写入。lifeup_url 必须是经过 URL 编码的 "
            "lifeup://api/add_task?... 官方地址；Dashboard 会再次严格校验。"
        ),
        "inputSchema": _object_schema(
            {
                "source": {"type": "string", "const": "cloud"},
                "lifeup_url": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            ["source", "lifeup_url"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "lifeup_create_task",
        "description": (
            "确认并执行已经预览的单个手机云端任务。只有用户明确确认后才可把 confirmed 设为 true；"
            "必须使用预览令牌和新的幂等键。失败或结果不确定时不要自动重试。"
        ),
        "inputSchema": _object_schema(
            {
                "source": {"type": "string", "const": "cloud"},
                "preview_token": {"type": "string", "minLength": 16, "maxLength": 256},
                "confirmed": {"type": "boolean", "const": True},
                "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
            },
            ["source", "preview_token", "confirmed", "idempotency_key"],
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
]


TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _validate_keys(arguments: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ToolInputError("工具参数必须是 JSON 对象")
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ToolInputError(f"存在不支持的参数：{', '.join(unknown)}")
    return arguments


def _source(arguments: dict[str, Any], *, cloud_only: bool = False) -> str:
    source = arguments.get("source")
    allowed = ("cloud",) if cloud_only else ("local", "cloud")
    if source not in allowed:
        options = "cloud" if cloud_only else "local 或 cloud"
        raise ToolInputError(f"source 必须明确填写 {options}")
    return source


def _optional_text(arguments: dict[str, Any], key: str, max_length: int = 200) -> str | None:
    if key not in arguments:
        return None
    value = arguments[key]
    if not isinstance(value, str) or len(value) > max_length:
        raise ToolInputError(f"{key} 必须是长度不超过 {max_length} 的文本")
    return value


def _optional_positive_int(arguments: dict[str, Any], key: str) -> int | None:
    if key not in arguments:
        return None
    value = arguments[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolInputError(f"{key} 必须是正整数")
    return value


def _read_result(client: DashboardClient, path: str, query: dict[str, Any]) -> dict[str, Any]:
    status, data = client.request("GET", path, query=query)
    return {"ok": True, "http_status": status, "source": query["source"], "data": data}


def call_tool(client: DashboardClient, name: str, arguments: Any) -> dict[str, Any]:
    if name not in TOOL_BY_NAME:
        raise ToolInputError(f"未知工具：{name}")

    if name == "lifeup_get_status":
        args = _validate_keys(arguments, {"source"})
        source = _source(args)
        path = "/api/status" if source == "local" else "/api/cloud/config"
        return _read_result(client, path, {"source": source})

    if name == "lifeup_list_tasks":
        args = _validate_keys(arguments, {"source", "filter", "search", "category_id"})
        source = _source(args)
        filter_value = args.get("filter", "all")
        if filter_value not in ("all", "active", "done", "frozen"):
            raise ToolInputError("filter 必须是 all、active、done 或 frozen")
        return _read_result(client, "/api/tasks", {
            "source": source,
            "filter": filter_value,
            "search": _optional_text(args, "search"),
            "category_id": _optional_positive_int(args, "category_id"),
        })

    if name in ("lifeup_list_items", "lifeup_list_achievements"):
        args = _validate_keys(arguments, {"source", "search", "category_id"})
        source = _source(args)
        path = "/api/items" if name == "lifeup_list_items" else "/api/achievements"
        return _read_result(client, path, {
            "source": source,
            "search": _optional_text(args, "search"),
            "category_id": _optional_positive_int(args, "category_id"),
        })

    if name in ("lifeup_get_dashboard", "lifeup_get_focus", "lifeup_get_wishes"):
        args = _validate_keys(arguments, {"source"})
        source = _source(args)
        path = {
            "lifeup_get_dashboard": "/api/dashboard/overview",
            "lifeup_get_focus": "/api/focus/overview",
            "lifeup_get_wishes": "/api/goals",
        }[name]
        return _read_result(client, path, {"source": source})

    if name == "lifeup_preview_task":
        args = _validate_keys(arguments, {"source", "lifeup_url"})
        source = _source(args, cloud_only=True)
        lifeup_url = args.get("lifeup_url")
        if not isinstance(lifeup_url, str) or not lifeup_url.startswith("lifeup://api/add_task?"):
            raise ToolInputError("lifeup_url 必须以 lifeup://api/add_task? 开头")
        if len(lifeup_url) > 4000:
            raise ToolInputError("lifeup_url 不能超过 4000 个字符")
        status, data = client.request(
            "POST", "/api/cloud/preview", body={"source": source, "urls": [lifeup_url]}
        )
        return {"ok": True, "http_status": status, "source": source, "data": data}

    args = _validate_keys(
        arguments, {"source", "preview_token", "confirmed", "idempotency_key"}
    )
    source = _source(args, cloud_only=True)
    if args.get("confirmed") is not True:
        raise ToolInputError("只有用户明确确认后，confirmed 才能设为 true")
    preview_token = args.get("preview_token")
    idempotency_key = args.get("idempotency_key")
    if not isinstance(preview_token, str) or not 16 <= len(preview_token) <= 256:
        raise ToolInputError("preview_token 长度无效，请重新预览")
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
        raise ToolInputError("idempotency_key 长度必须在 8 到 128 个字符之间")
    status, data = client.request(
        "POST",
        "/api/cloud/execute",
        body={"preview_token": preview_token, "idempotency_key": idempotency_key},
    )
    return {"ok": True, "http_status": status, "source": source, "data": data}


def tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    safe = sanitize_payload(payload)
    if not isinstance(safe, dict):
        safe = {"data": safe}
    return {
        "content": [
            {"type": "text", "text": json.dumps(safe, ensure_ascii=False, separators=(",", ":"))}
        ],
        "structuredContent": safe,
        "isError": is_error,
    }


class MCPServer:
    def __init__(self, client: DashboardClient):
        self.client = client

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request")
        if request_id is None:
            return None
        params = message.get("params") or {}
        try:
            if method == "initialize":
                requested = params.get("protocolVersion") if isinstance(params, dict) else None
                negotiated = (
                    requested
                    if requested in SUPPORTED_PROTOCOL_VERSIONS
                    else DEFAULT_PROTOCOL_VERSION
                )
                return self._result(request_id, {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "每次调用必须显式选择 local 或 cloud。只读优先；"
                        "新增任务必须先预览，并在用户明确确认后执行。"
                    ),
                })
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                if not isinstance(params, dict):
                    raise ToolInputError("tools/list 参数必须是对象")
                return self._result(request_id, {"tools": TOOLS})
            if method == "tools/call":
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    raise ToolInputError("tools/call 缺少工具名称")
                try:
                    payload = call_tool(self.client, params["name"], params.get("arguments", {}))
                    result = tool_result(payload)
                except DashboardHTTPError as exc:
                    result = tool_result({
                        "ok": False,
                        "http_status": exc.status,
                        "error": exc.payload,
                    }, is_error=True)
                except AdapterError as exc:
                    result = tool_result({"ok": False, "error": str(exc)}, is_error=True)
                return self._result(request_id, result)
            return self._error(request_id, -32601, "Method not found")
        except ToolInputError as exc:
            return self._error(request_id, -32602, str(exc))
        except Exception:
            return self._error(request_id, -32603, "Internal error")

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def run(self) -> None:
        while True:
            raw = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_MESSAGE_BYTES:
                self._write(self._error(None, -32700, "Parse error"))
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write(self._error(None, -32700, "Parse error"))
                continue
            response = self.handle(message)
            if response is not None:
                self._write(response)

    @staticmethod
    def _write(message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.buffer.write(encoded.encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LifeUp Dashboard MCP stdio adapter")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LIFEUP_DASHBOARD_URL", DEFAULT_BASE_URL),
        help="LifeUp Dashboard 本机 HTTP 地址（默认 http://127.0.0.1:5000）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("LIFEUP_MCP_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
        help="单次 Dashboard HTTP 请求超时秒数",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        client = DashboardClient(args.base_url, args.timeout)
    except (ValueError, TypeError) as exc:
        print(f"LifeUp MCP 配置错误：{exc}", file=sys.stderr)
        return 2
    MCPServer(client).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
