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
import threading

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


class _BlockWorker:
    """排入 aiosqlite 工作线程队列后长时间阻塞 worker，使随后入队的操作
    （如 BEGIN IMMEDIATE）确定地"已入队、未执行"。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self._release = threading.Event()

    def __call__(self) -> None:
        self.entered.set()
        self._release.wait(timeout=5)

    def release(self) -> None:
        self._release.set()


def _queue_blocker(conn: aiosqlite.Connection) -> _BlockWorker:
    """同步把阻塞函数放进 worker FIFO（在 append 的 BEGIN 之前）。"""
    blocker = _BlockWorker()
    fut = asyncio.get_event_loop().create_future()
    conn._tx.put_nowait((fut, blocker))  # type: ignore[attr-defined]
    return blocker


async def test_cancel_when_begin_queued_but_not_executed_closes_transaction(store: EventStore):
    """真实 TOCTOU 窗口（2026-09-09 Linux CI）：取消到达时 BEGIN IMMEDIATE 已进入
    worker FIFO 但尚未执行，事件循环线程读 in_transaction 是 False。清理必须无条件
    在 BEGIN 之后入队 ROLLBACK（FIFO 保证后执行），孤儿 BEGIN 才不会遗留 OPEN 事务。"""
    blocker = _queue_blocker(store.conn)
    await asyncio.sleep(0)  # 让 worker 取出 blocker 并阻塞
    assert blocker.entered.wait(timeout=2)

    task = asyncio.create_task(
        store.append_event(
            "run-queued",
            EventType.RUN_STARTED,
            {"intent": "x", "context_snapshot": {}},
        )
    )
    await asyncio.sleep(0)  # append 把 BEGIN 排在被阻塞的 worker 队列中（尚未执行）
    task.cancel()  # 此刻 in_transaction == False，但 BEGIN 已入队
    with pytest.raises(asyncio.CancelledError):
        await task

    blocker.release()  # 放开 worker：按 FIFO 先执行孤儿 BEGIN，再执行清理 ROLLBACK
    await asyncio.sleep(0.1)  # 等 worker 排空队列

    assert store.conn.in_transaction is False

    # 看门狗随后的终态写入必须成功（旧代码在此撞 "cannot start a transaction"）。
    follow_up = await asyncio.wait_for(
        store.append_event(
            "run-queued",
            EventType.AGENT_THOUGHT,
            {"thought": "watchdog terminal write", "token_count": 1},
        ),
        timeout=2,
    )
    assert follow_up.seq == 1


async def test_cancel_when_begin_queued_then_repeated_cancels_still_closes(store: EventStore):
    """BEGIN 入队未执行时取消，并在清理 ROLLBACK 的 await 点反复再取消：事务仍必须
    闭合（ROLLBACK 入队即不可撤回，FIFO 保证执行），写锁释放、终态写可成功。"""
    blocker = _queue_blocker(store.conn)
    await asyncio.sleep(0)
    assert blocker.entered.wait(timeout=2)

    task = asyncio.create_task(
        store.append_event(
            "run-queued2",
            EventType.RUN_STARTED,
            {"intent": "x", "context_snapshot": {}},
        )
    )
    await asyncio.sleep(0)
    for _ in range(5):
        task.cancel()
        await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError):
        await task

    blocker.release()
    await asyncio.sleep(0.1)

    assert store.conn.in_transaction is False
    follow_up = await asyncio.wait_for(
        store.append_event(
            "run-queued2",
            EventType.AGENT_THOUGHT,
            {"thought": "watchdog terminal write", "token_count": 1},
        ),
        timeout=2,
    )
    assert follow_up.seq == 1


async def test_append_self_heals_from_orphaned_open_transaction(store: EventStore):
    """兜底加固：若共享连接上已遗留一个 OPEN 事务（任何未知取消窗口的后果），下一条
    append 的 BEGIN 会撞 "cannot start a transaction within a transaction"。受信存储层
    必须先 ROLLBACK 再重试一次，让写入（尤其看门狗终态写）成功，run 不卡 running。"""
    # 人为制造孤儿事务：绕过托管原语直接在共享连接上 BEGIN，且不提交/回滚。
    await store.conn.execute("BEGIN IMMEDIATE")
    assert store.conn.in_transaction is True

    follow_up = await asyncio.wait_for(
        store.append_event(
            "run-orphan",
            EventType.AGENT_THOUGHT,
            {"thought": "write despite orphaned txn", "token_count": 1},
        ),
        timeout=2,
    )
    assert follow_up.seq == 1
    assert store.conn.in_transaction is False
    rows = await store.get_events("run-orphan")
    assert [e.event_type for e in rows] == [EventType.AGENT_THOUGHT]

