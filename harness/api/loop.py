"""Cross-platform event-loop factory for Uvicorn.

Windows needs a Proactor loop for asyncio subprocess support. Unix platforms
keep asyncio's platform-selected loop implementation.
"""

from __future__ import annotations

import asyncio
import sys


def configure_event_loop_policy() -> None:
    """Configure the process policy before any application loop is created."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def event_loop_factory(*, use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    """Return the loop Uvicorn should use for the API process."""
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()
