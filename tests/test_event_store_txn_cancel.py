"""取消安全的写事务回归测试。

根因（2026-09-09 Linux CI 首次照出）：watchdog 在 ``append_event`` 的
``BEGIN IMMEDIATE`` 与 ``commit`` 之间取消任务时，``asyncio.CancelledError``
继承 ``BaseException``，不被原 ``except Exception`` 捕获 → 不 rollback，
共享连接上遗留一个 OPEN 事务；随后看门狗写终态 ``RunFailed`` 再发 BEGIN 即撞
"cannot start a transaction within a transaction"，终态永远写不进，run 永久
卡在 running。

受信存储层必须保证：持写锁期间，无论正常返回、普通异常还是任务取消，事务都闭合，
且 CancelledError 被忠实传播（不得吞取消）。
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from harness.models.events import EventType
from harness.storage.event_store import EventStore


async def test_append_after_cancel_during_commit_leaves_no_open_transaction(store: EventStore):
    """在 commit 等待点取消在途 append：事务必须回滚闭合，连接可继续写。"""

    real_commit = aiosqlite.Connection.commit
    entered = asyncio.Event()

    async def slow_commit(self):
        entered.set()
        # 让取消稳定落在 BEGIN…commit 之间（此时事务 OPEN、INSERT 已执行）。
        await asyncio.sleep(0.05)
        return await real_commit(self)

    task = asyncio.create_task(
        store.append_event(
            "run-cancel",
            EventType.RUN_STARTED,
            {"intent": "x", "context_snapshot": {}},
        )
    )
    aiosqlite.Connection.commit = slow_commit
    try:
        await entered.wait()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        aiosqlite.Connection.commit = real_commit

    # 关键不变量：取消不得在共享连接上遗留未决事务。
    assert store.conn.in_transaction is False

    # 连接仍可正常写入（看门狗随后的 RunFailed 终态写入必须成功）。
    follow_up = await store.append_event(
        "run-cancel",
        EventType.AGENT_THOUGHT,
        {"thought": "after cancel", "token_count": 1},
    )
    assert follow_up.seq == 1
    rows = await store.get_events("run-cancel")
    assert [e.event_type for e in rows] == [EventType.AGENT_THOUGHT]


async def test_cancelled_append_releases_write_lock(store: EventStore):
    """取消后写锁必须被释放，否则后续所有 append 永久死等。"""

    real_commit = aiosqlite.Connection.commit
    entered = asyncio.Event()

    async def slow_commit(self):
        entered.set()
        await asyncio.sleep(0.05)
        return await real_commit(self)

    task = asyncio.create_task(
        store.append_event(
            "run-lock",
            EventType.RUN_STARTED,
            {"intent": "x", "context_snapshot": {}},
        )
    )
    aiosqlite.Connection.commit = slow_commit
    try:
        await entered.wait()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        aiosqlite.Connection.commit = real_commit

    # 若取消路径未释放锁，这里会超时挂死。
    await asyncio.wait_for(
        store.append_event(
            "run-lock",
            EventType.AGENT_THOUGHT,
            {"thought": "lock free", "token_count": 1},
        ),
        timeout=2,
    )
