"""P1-13 Bug 5（Windows asyncio 子进程）复发回归测试。

背景：playwright（launch / connect_over_cdp 都需经子进程拉起 Node driver）与
docker 依赖 asyncio.create_subprocess_exec。Windows 上该调用只在 Proactor loop
可用；uvicorn loop="auto" + reload/workers（use_subprocess=True）会显式实例化
SelectorEventLoop，绕过 serve.py import 阶段的 policy 设置 →
NotImplementedError（任务后台抛错）。回归点：serve.py main() 必须把跨平台
event_loop_factory 传给 uvicorn.run(loop=...)（测试 1）；event_loop_factory
产出的 loop 在 win32 恒为 Proactor（测试 2）；其 loop 真实可跑子进程（测试 3，
直接复现 playwright 的失败机制）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from harness.api.loop import configure_event_loop_policy, event_loop_factory


def _run_child(loop: asyncio.AbstractEventLoop) -> int:
    """用目标 loop 启动一个真实子进程（playwright 驱动同款失败路径）。"""

    async def _spawn() -> int:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
        return await proc.wait()

    return loop.run_until_complete(_spawn())


def test_serve_main_forces_cross_platform_loop_factory():
    src = Path("harness/api/serve.py").read_text(encoding="utf-8")
    region = src.split("uvicorn.run", 1)[-1].split("if __name__", 1)[0]
    # reload 模式下 uvicorn 自定义 loop 路径才会被当零参 factory 调用，win32→Proactor。
    assert 'loop="harness.api.loop:event_loop_factory"' in region, (
        "serve.py main() 必须显式传 loop=event_loop_factory，否则 Windows reload 退回 "
        "SelectorEventLoop（policy 在 uvicorn 显式 loop_factory 下不生效）"
    )


def test_event_loop_factory_is_proactor_on_windows_only():
    loop = event_loop_factory()
    try:
        if sys.platform == "win32":
            assert type(loop).__name__ == "ProactorEventLoop", (
                "Windows 上 event_loop_factory 必须返回 Proactor，否则 playwright/docker 子进程抛 NotImplementedError"
            )
        else:
            assert type(loop).__name__.endswith("EventLoop")
    finally:
        loop.close()


def test_event_loop_factory_runs_real_subprocess():
    """复现 playwright 失败机制：Selector loop(win) 下 create_subprocess_exec 抛
    NotImplementedError；Proactor(win) / Selector(unix) 都应通过。"""
    loop = event_loop_factory()
    try:
        assert _run_child(loop) == 0
    finally:
        loop.close()


def test_event_loop_factory_loop_is_closable_and_reusable():
    loop = event_loop_factory()
    loop.run_until_complete(asyncio.sleep(0))
    assert not loop.is_closed()
    loop.close()
    assert loop.is_closed()
    # 每次调用返回新的可运行 loop
    loop2 = event_loop_factory()
    try:
        loop2.run_until_complete(asyncio.sleep(0))
    finally:
        loop2.close()


def test_configure_event_loop_policy_is_safe_on_all_platforms():
    configure_event_loop_policy()
    if sys.platform == "win32":
        assert type(asyncio.get_event_loop_policy()).__name__ == "WindowsProactorEventLoopPolicy"
