"""Cross-platform event-loop factory for Uvicorn.

Windows needs a Proactor loop for asyncio subprocess support (playwright Node
driver / docker)。Unix platforms keep asyncio's platform-selected loop
implementation。

uvicorn 契约：把本模块路径传给 ``--loop`` / ``uvicorn.run(loop=...)`` 时，
uvicorn 会把 ``event_loop_factory`` 这个函数当作 loop factory，每次启动用
零参调用它得到新 loop。因此 Windows 恒返回 Proactor，**不受 uvicorn 的
use_subprocess（reload/workers）分支影响** —— 那个分支在 loop="auto" 下会显式
实例化 SelectorEventLoop（P1-13 Bug 5 复发根因）。
"""

from __future__ import annotations

import asyncio
import sys


def configure_event_loop_policy() -> None:
    """Configure the process policy before any application loop is created.

    兜底手段：只对"走 asyncio 默认 policy 建 loop"的路径（scripts/tests 等
    直接 asyncio.run 的入口）生效。uvicorn 若显式传 loop_factory（loop=auto +
    reload/workers），policy 会被绕过 —— 那类入口必须依赖 event_loop_factory。
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def event_loop_factory() -> asyncio.AbstractEventLoop:
    """Return the loop Uvicorn should use for the API process.

    Windows 强制 Proactor（子进程支持）；其余平台返回平台默认 loop。
    uvicorn 以零参调用本函数，故不接收参数；旧签名里的 ``use_subprocess``
    参数已不需要 —— Proactor 在 reload/workers 下同样有效。
    """
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()
