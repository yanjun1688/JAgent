"""JAgent v8 · MCP 工具适配器（外部适配层）。

MCP（Model Context Protocol）是外部工具提供方。这里把它接成 `ToolRegistry`
里的普通工具：planner 通过 `registry.descriptors()` 看见它，executor 通过
`registry.call()` 调它，HTTP 副作用只发生在**适配层**。内核 / 受信组件
（executor / fold / writer / 终态判定）对它一无所知。

分层约定（重要）：

* 本模块属于**外部适配层**（`jagent/adapters/`），不是 `harness/`——后者是
  确定性内核，架构合规测试禁止它 import `httpx`；
* 注册只走 `ToolRegistry.add()`，调用只走 `ToolRegistry.call()`，不绕过 Executor；
* 任何失败都收敛成 `ToolResult(ok=False, error_code=...)`，不抛穿到 run。

Fuyao 的响应形状是 `structuredContent = {request_id, code, message, data}`，
`code == 0` 才是业务成功；`data` 才是真正的载荷。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..contracts import Idempotency, SideEffect, ToolResult, ToolSpec
from ..harness.registry import ToolFn, ToolRegistry

#: MCP 协议版本（Fuyao 当前接受这个版本）。
PROTOCOL_VERSION = "2025-06-18"

#: 从环境变量里找 API key 的名字，按优先级排列。
#: 规范键是 `JAGENT_MCP_API_KEY`；其余是历史部署留下的别名（只做兼容，不再新增）。
KEY_NAMES: tuple[str, ...] = (
    "JAGENT_MCP_API_KEY",
    "FUYAO_API_KEY",
    "THS_API_KEY",
    "AICUBES_API_KEY",
    "tonghuashun-key",
    "tonghuashun_key",
    "API_KEY",
)


class MCPError(Exception):
    """MCP 调用失败；`error_code` 是 `contracts.ERROR_CLASS` 认得的码。"""

    def __init__(self, message: str, *, error_code: str = "tool_exception") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class McpServer:
    """一个 MCP 端点。`name` 只用于给工具加前缀，避免多服务重名。"""

    name: str
    url: str
    api_key: str = ""


# --------------------------------------------------------------------------- #
# JSON-RPC over HTTP
# --------------------------------------------------------------------------- #
class MCPClient:
    """最小 MCP 客户端：initialize / tools/list / tools/call。"""

    def __init__(self, url: str, api_key: str = "", *, timeout: float = 60.0) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self.session: str | None = None
        self._id = 0

    def _post(self, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            headers["X-api-key"] = self.api_key
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            response = httpx.post(
                self.url, json=payload, headers=headers, timeout=timeout or self.timeout
            )
        except httpx.TimeoutException as exc:
            raise MCPError(f"timeout: {exc}", error_code="timeout") from exc
        except httpx.TransportError as exc:
            raise MCPError(f"transport: {exc}", error_code="connection_reset") from exc

        self.session = response.headers.get("mcp-session-id", self.session)
        if response.status_code >= 400:
            if response.status_code in (401, 403):
                code = "permission_denied"
            elif response.status_code == 429:
                code = "rate_limited"
            elif response.status_code >= 500:
                code = "upstream_5xx"
            else:
                code = "tool_exception"
            raise MCPError(f"HTTP {response.status_code}: {response.text[:300]}", error_code=code)

        if not response.text.strip():
            return {}
        if "text/event-stream" in response.headers.get("content-type", ""):
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            raise MCPError(f"SSE response without data: {response.text[:300]}")
        return response.json()

    def initialize(self, *, timeout: float | None = None) -> dict[str, Any]:
        self._id += 1
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "jagent", "version": "0.2"},
                },
            },
            timeout=timeout,
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=timeout)
        return result

    def list_tools(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        self._id += 1
        result = self._post(
            {"jsonrpc": "2.0", "id": self._id, "method": "tools/list", "params": {}},
            timeout=timeout,
        )
        return list(result.get("result", {}).get("tools", []))

    def call(
        self, name: str, arguments: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self._id += 1
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments)},
            },
            timeout=timeout,
        )
        if "error" in result:
            raise MCPError(f"MCP error: {result['error']}")
        return dict(result.get("result", {}))


# --------------------------------------------------------------------------- #
# 结果映射
# --------------------------------------------------------------------------- #
def _first_text(result: Mapping[str, Any]) -> str:
    for block in result.get("content") or []:
        text = str(block.get("text") or "")
        if text:
            return text
    return ""


def _extract(result: Mapping[str, Any]) -> tuple[bool, Any, str | None, str]:
    """MCP `tools/call` 结果 → `(ok, data, error_code, error_message)`。"""
    if result.get("isError"):
        return False, None, "tool_exception", _first_text(result) or "MCP tool returned an error"

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        code = structured.get("code")
        if code is not None and code != 0:
            message = str(structured.get("message") or f"MCP code {code}")
            return False, None, "tool_exception", message
        if "data" in structured:
            return True, structured["data"], None, ""
        # 没有 data 键：把除元信息外的字段当载荷
        return (
            True,
            {k: v for k, v in structured.items() if k not in {"request_id", "code", "message"}},
            None,
            "",
        )

    for block in result.get("content") or []:
        text = str(block.get("text") or "")
        if not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        code = parsed.get("code")
        if code is not None and code != 0:
            return False, None, "tool_exception", str(parsed.get("message") or f"MCP code {code}")
        return True, parsed.get("data", parsed), None, ""

    return True, dict(result), None, ""


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def _build_tool(
    client: MCPClient, server_name: str, tool: Mapping[str, Any]
) -> tuple[ToolSpec, ToolFn]:
    raw_name = str(tool.get("name") or "")
    if not raw_name:
        raise ValueError("MCP tool without a name")
    qualified = f"{server_name}.{raw_name}"
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or not schema:
        schema = {"type": "object", "properties": {}}

    spec = ToolSpec(
        name=qualified,
        version=str(tool.get("version") or "1"),
        args_schema=schema,
        # MCP 的行情/资料查询都是只读、天然幂等。
        side_effect=SideEffect.NONE,
        idempotency=Idempotency.NATURAL,
        error_codes=(
            "timeout",
            "rate_limited",
            "upstream_5xx",
            "connection_reset",
            "permission_denied",
            "tool_exception",
        ),
        timeout_s=60.0,
        readonly=True,
        description=str(tool.get("description") or raw_name),
        # 载荷统一放在 `data` 观测里，断言可以引用 `data`。
        observations=("ok", "data"),
    )

    def run(args: dict[str, Any], workspace: Any, timeout: float) -> ToolResult:
        try:
            result = client.call(raw_name, args, timeout=timeout or spec.timeout_s)
        except MCPError as exc:
            return ToolResult(ok=False, error_code=exc.error_code, error_message=str(exc))
        except Exception as exc:  # noqa: BLE001 - 外部服务，失败就是 ToolResult
            return ToolResult(
                ok=False, error_code="tool_exception", error_message=f"{type(exc).__name__}: {exc}"
            )
        ok, data, error_code, error_message = _extract(result)
        if not ok:
            return ToolResult(ok=False, error_code=error_code, error_message=error_message)
        return ToolResult(ok=True, data={"data": data})

    return spec, run


def register_mcp_tools(
    registry: ToolRegistry,
    servers: Sequence[McpServer],
    *,
    tool_allowlist: Sequence[str] | None = None,
    discover_timeout: float = 15.0,
) -> list[str]:
    """把每个 MCP 服务暴露的工具注册进 registry；返回注册成功的工具名。

    发现阶段（initialize / tools/list）是**尽力而为**：任何一个服务连不上就跳过，
    不让 MCP 的可用性拖垮整个应用启动。工具名形如 `meta.get_meta_tickers_search`。
    """
    registered: list[str] = []
    allow = set(tool_allowlist) if tool_allowlist is not None else None
    for server in servers:
        try:
            client = MCPClient(server.url, server.api_key)
            client.initialize(timeout=discover_timeout)
            tools = client.list_tools(timeout=discover_timeout)
        except Exception:  # noqa: BLE001 - 发现失败不阻断启动
            continue
        for tool in tools:
            raw = str(tool.get("name") or "")
            if not raw:
                continue
            if allow is not None and raw not in allow:
                continue
            try:
                spec, fn = _build_tool(client, server.name, tool)
            except Exception:  # noqa: BLE001
                continue
            registry.add(spec, fn)
            registered.append(spec.name)
    return registered


def mcp_servers_from_env(env: Mapping[str, str]) -> list[McpServer]:
    """从环境变量构造 MCP 端点列表。

    * `JAGENT_MCP_ENABLED=0` 关闭（默认开启）；
    * key 取 `KEY_NAMES` 里第一个非空值（规范键 `JAGENT_MCP_API_KEY`）；没有 key 就不接 MCP；
    * 端点**必须由部署配置提供**：`JAGENT_MCP_META_URL` / `JAGENT_MCP_A_SHARE_URL`，
      缺哪个就跳过哪个。代码里不写死任何部署地址。
    """
    if str(env.get("JAGENT_MCP_ENABLED", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return []
    key = ""
    for name in KEY_NAMES:
        value = env.get(name)
        if value:
            key = str(value)
            break
    if not key:
        return []

    servers: list[McpServer] = []
    meta = str(env.get("JAGENT_MCP_META_URL") or "").strip()
    a_share = str(env.get("JAGENT_MCP_A_SHARE_URL") or "").strip()
    if meta:
        servers.append(McpServer("meta", meta, key))
    if a_share:
        servers.append(McpServer("a_share", a_share, key))
    return servers


__all__ = [
    "MCPClient",
    "MCPError",
    "McpServer",
    "mcp_servers_from_env",
    "register_mcp_tools",
]
