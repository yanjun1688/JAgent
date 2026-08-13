from __future__ import annotations

import asyncio
import sys
from typing import Any

from harness.models.tools import (
    Guardrail,
    OperationContract,
    SideEffect,
    SuccessIndicator,
    ToolDefinition,
    ToolScopeTarget,
)
from harness.tools.base import BaseTool, operation


def _subprocess_supported() -> bool:
    """检测当前事件循环是否支持子进程。

    Windows 的 SelectorEventLoop 不支持 asyncio 子进程（playwright 内部会抛
    NotImplementedError）。ProactorEventLoop 支持。serve.py 已在启动时切换到
    Proactor 策略；这里作为兜底给出明确错误而非深埋在 playwright 内部。
    """
    if sys.platform != "win32":
        return True
    try:
        loop = asyncio.get_running_loop()
        return getattr(loop, "_proactor", None) is not None
    except RuntimeError:
        return True

BROWSER_DEF = ToolDefinition(
    name="browser",
    description="Control a web browser: navigate, click, type, extract text, or take screenshots.",
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "type", "extract", "screenshot"],
                "description": "Browser action to perform",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (required for 'navigate' action)",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for the target element (required for click/type/extract)",
            },
            "value": {
                "type": "string",
                "description": "Text to type (required for 'type' action)",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Timeout in milliseconds for navigation or element wait",
                "default": 30000,
            },
        },
        "required": ["action"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "action": {"type": "string"},
            "url": {"type": "string"},
            "result": {"type": "string"},
            "screenshot": {"type": "string"},
            "error": {"type": "string"},
        },
    },
    idempotency_key_fields=["action", "url", "selector", "value"],
    side_effects=[SideEffect.EXTERNAL],
    guardrails=[Guardrail(guardrail_type="scope", config={})],
    timeout_ms=60000,
    success_indicator=SuccessIndicator(field="success", op="eq", value=True),
    # ADR-010 D-02: 判别键（取代原魔法 `_OPERATION_KEYS`）。
    operation_key="action",
    # ADR-010 D-04: scope 目标契约化（取代 ScopeGuardrail 名称特判）。
    scope_targets=[ToolScopeTarget(kind="domain", input_field="url")],
    # S02: per-operation contracts — extract/screenshot are read-only (probe
    # allowed); navigate/click/type mutate external browser state (no probe).
    operations=[
        OperationContract(
            operation="navigate",
            side_effects=[SideEffect.EXTERNAL],
            probe_allowed=False,
            ref_allowed_fields={"url": False},
        ),
        OperationContract(
            operation="click",
            side_effects=[SideEffect.EXTERNAL],
            probe_allowed=False,
            ref_allowed_fields={"selector": False, "value": False},
        ),
        OperationContract(
            operation="type",
            side_effects=[SideEffect.EXTERNAL],
            probe_allowed=False,
            ref_allowed_fields={"selector": False, "value": True},
        ),
        OperationContract(
            operation="extract",
            side_effects=[],
            probe_allowed=True,
            ref_allowed_fields={"selector": False, "value": True},
        ),
        OperationContract(
            operation="screenshot",
            side_effects=[],
            probe_allowed=True,
            ref_allowed_fields={"selector": False, "value": True},
        ),
    ],
)


_playwright_available: bool | None = None


def _check_playwright() -> bool:
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    try:
        import playwright  # noqa: F401

        _playwright_available = True
    except ImportError:
        _playwright_available = False
    return _playwright_available


def _check_browser_installed() -> bool:
    try:
        import subprocess

        result = subprocess.run(
            ["playwright", "install", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 or "already" in result.stdout.lower()
    except Exception:
        return True  # Let runtime error surface naturally


class BrowserManager:
    _instance: BrowserManager | None = None

    def __init__(self) -> None:
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            try:
                await self._page.evaluate("1")
                return self._page
            except Exception:
                pass

        # Bug JAGENT-2026-P1-13 (Windows compat): 当前 loop 不支持子进程时，
        # playwright 内部 Connection.run() 会抛 NotImplementedError。提前给出
        # 明确错误，避免深埋在 playwright 后台 task 中。
        if not _subprocess_supported():
            raise RuntimeError(
                "Browser unavailable on this event loop: asyncio subprocess is not supported "
                "(Windows SelectorEventLoop). Use ProactorEventLoopPolicy (set in serve.py)."
            )

        from playwright.async_api import async_playwright

        p = await async_playwright().start()
        try:
            self._browser = await asyncio.wait_for(p.chromium.launch(headless=True), timeout=15.0)
        except (asyncio.TimeoutError, NotImplementedError, Exception) as e:
            raise RuntimeError(f"Playwright init failed: {type(e).__name__}: {e}")
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        return self._page

    async def navigate(self, url: str, timeout_ms: int = 30000) -> str:
        page = await self._ensure_page()
        await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return await page.title()

    async def click(self, selector: str, timeout_ms: int = 30000) -> None:
        page = await self._ensure_page()
        await page.click(selector, timeout=timeout_ms)

    async def type_text(self, selector: str, value: str, timeout_ms: int = 30000) -> None:
        page = await self._ensure_page()
        await page.fill(selector, value, timeout=timeout_ms)

    async def extract(self, selector: str, timeout_ms: int = 30000) -> str:
        page = await self._ensure_page()
        element = await page.wait_for_selector(selector, timeout=timeout_ms)
        if element is None:
            return ""
        return await element.inner_text()

    async def screenshot(self) -> str:
        page = await self._ensure_page()
        import base64

        bytes_data = await page.screenshot(type="png")
        return base64.b64encode(bytes_data).decode("ascii")

    async def close(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        self._browser = None
        self._context = None
        self._page = None
        BrowserManager._instance = None

    @classmethod
    def get_instance(cls) -> BrowserManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    async def cleanup(cls) -> None:
        if cls._instance is not None:
            await cls._instance.close()


async def browser_fn(input: dict[str, Any]) -> dict[str, Any]:
    if not _check_playwright():
        return {
            "success": False,
            "action": input.get("action", "unknown"),
            "error": "Playwright not installed. Run: pip install playwright install",
        }

    action = input["action"]
    timeout_ms = input.get("timeout_ms", 30000)
    bm = BrowserManager.get_instance()

    try:
        match action:
            case "navigate":
                url = input.get("url")
                if not url:
                    return {"success": False, "action": action, "error": "url is required for navigate action"}
                title = await bm.navigate(url, timeout_ms)
                return {
                    "success": True,
                    "action": action,
                    "url": url,
                    "result": f"Navigated to {url}. Page title: {title}",
                }

            case "click":
                selector = input.get("selector")
                if not selector:
                    return {"success": False, "action": action, "error": "selector is required for click action"}
                await bm.click(selector, timeout_ms)
                return {"success": True, "action": action, "result": f"Clicked element: {selector}"}

            case "type":
                selector = input.get("selector")
                value = input.get("value")
                if not selector or value is None:
                    return {
                        "success": False,
                        "action": action,
                        "error": "selector and value are required for type action",
                    }
                await bm.type_text(selector, value, timeout_ms)
                return {"success": True, "action": action, "result": f"Typed into element: {selector}"}

            case "extract":
                selector = input.get("selector")
                if not selector:
                    return {"success": False, "action": action, "error": "selector is required for extract action"}
                text = await bm.extract(selector, timeout_ms)
                return {"success": True, "action": action, "result": text}

            case "screenshot":
                encoded = await bm.screenshot()
                return {"success": True, "action": action, "screenshot": f"data:image/png;base64,{encoded}"}

            case _:
                return {"success": False, "action": action, "error": f"Unknown action: {action}"}
    except Exception as exc:
        return {
            "success": False,
            "action": action,
            "error": f"Browser action '{action}' failed: {type(exc).__name__}: {exc}",
        }


class BrowserTool(BaseTool):
    """browser 声明式实现（ADR-010 D-01/D-08）— 取代 BROWSER_DEF + fn。

    业务执行复用 ``browser_fn``；BrowserManager 生命周期收敛到 ``close()``（D-08）。
    """

    name = "browser"
    description = "Control a web browser: navigate, click, type, extract text, or take screenshots."
    input_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["navigate", "click", "type", "extract", "screenshot"]},
            "url": {"type": "string"},
            "selector": {"type": "string"},
            "value": {"type": "string"},
            "timeout_ms": {"type": "integer", "default": 30000},
        },
        "required": ["action"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "action": {"type": "string"},
            "url": {"type": "string"},
            "result": {"type": "string"},
            "screenshot": {"type": "string"},
            "error": {"type": "string"},
        },
    }
    operation_key = "action"
    side_effects = [SideEffect.EXTERNAL]
    idempotency_key_fields = ["action", "url", "selector", "value"]
    guardrails = [Guardrail(guardrail_type="scope", config={})]
    timeout_ms = 60000
    success_indicator = SuccessIndicator(field="success", op="eq", value=True)
    scope_targets = [ToolScopeTarget(kind="domain", input_field="url")]

    async def _run_action(self, input: dict) -> dict:
        return await browser_fn(input)

    @operation("navigate", side_effects=[SideEffect.EXTERNAL], ref_allowed_fields={"url": False})
    async def do_navigate(self, input):
        return await self._run_action(input)

    @operation("click", side_effects=[SideEffect.EXTERNAL], ref_allowed_fields={"selector": False, "value": False})
    async def do_click(self, input):
        return await self._run_action(input)

    @operation("type", side_effects=[SideEffect.EXTERNAL], ref_allowed_fields={"selector": False, "value": True})
    async def do_type(self, input):
        return await self._run_action(input)

    @operation("extract", probe_allowed=True, ref_allowed_fields={"selector": False, "value": True})
    async def do_extract(self, input):
        return await self._run_action(input)

    @operation("screenshot", probe_allowed=True, ref_allowed_fields={"selector": False, "value": True})
    async def do_screenshot(self, input):
        return await self._run_action(input)

    async def close(self) -> None:
        await BrowserManager.cleanup()
