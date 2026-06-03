from __future__ import annotations

import asyncio
from typing import Any

from harness.models.tools import SideEffect, ToolDefinition

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
            "result": {"type": "string"},
            "screenshot": {"type": "string"},
            "error": {"type": "string"},
        },
    },
    idempotency_key_fields=["action", "url", "selector", "value"],
    side_effects=[SideEffect.EXTERNAL],
    timeout_ms=60000,
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
            capture_output=True, text=True, timeout=10,
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

        from playwright.async_api import async_playwright
        p = await async_playwright().start()
        self._browser = await p.chromium.launch(headless=True)
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
        return {"success": False, "error": "Playwright not installed. Run: pip install playwright install"}

    action = input["action"]
    timeout_ms = input.get("timeout_ms", 30000)
    bm = BrowserManager.get_instance()

    try:
        match action:
            case "navigate":
                url = input.get("url")
                if not url:
                    return {"success": False, "error": "url is required for navigate action"}
                title = await bm.navigate(url, timeout_ms)
                return {"success": True, "result": f"Navigated to {url}. Page title: {title}"}

            case "click":
                selector = input.get("selector")
                if not selector:
                    return {"success": False, "error": "selector is required for click action"}
                await bm.click(selector, timeout_ms)
                return {"success": True, "result": f"Clicked element: {selector}"}

            case "type":
                selector = input.get("selector")
                value = input.get("value")
                if not selector or value is None:
                    return {"success": False, "error": "selector and value are required for type action"}
                await bm.type_text(selector, value, timeout_ms)
                return {"success": True, "result": f"Typed into element: {selector}"}

            case "extract":
                selector = input.get("selector")
                if not selector:
                    return {"success": False, "error": "selector is required for extract action"}
                text = await bm.extract(selector, timeout_ms)
                return {"success": True, "result": text}

            case "screenshot":
                encoded = await bm.screenshot()
                return {"success": True, "screenshot": f"data:image/png;base64,{encoded}"}

            case _:
                return {"success": False, "error": f"Unknown action: {action}"}
    except asyncio.TimeoutError:
        return {"success": False, "error": f"Browser action '{action}' timed out after {timeout_ms}ms"}
    except Exception as exc:
        return {"success": False, "error": f"Browser action '{action}' failed: {exc}"}
