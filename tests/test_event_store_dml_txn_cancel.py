"""所有写方法必须取消安全且与事件写共用同一把写锁。

阶段二止血（2026-09-09）：workspace / tenant / conversation / claim 的写方法
曾用"裸隐式事务 + 裸 commit"，既无取消安全（CancelledError 不被 except Exception
捕获 → 不 rollback → 共享连接遗留 OPEN 事务，数据可能被下一次 BEGIN IMMEDIATE 的
自愈逻辑静默 ROLLBACK 丢弃），也无与 ``append_event`` 的互斥（并发 BEGIN 交错的
同一类孤儿事务）。修复把它们全部接入既有 ``_write_txn``（``_db_write_lock`` +
``_txn_immediate``）。

这些注入测试是失败模式的规格化契约：取消落在事务收尾的 commit 等待点时，受信存储层
必须保证不留孤儿事务、连接仍可写；并发混合写必须可串行化、不产生交错。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import aiosqlite
import pytest

from harness.models.events import EventType, RunStartedPayload
from harness.models.workspace import (
    ExecutionTarget,
    ExecutionTargetType,
    Workspace,
    WorkspaceScope,
    WorkspaceUpdate,
)
from harness.storage.event_store import EventStore


def _make_workspace(workspace_id: str, tenant_id: str = "tenant-a") -> Workspace:
    now = time.time()
    return Workspace(
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        name=workspace_id,
        scope=WorkspaceScope(
            target=ExecutionTarget(
                type=ExecutionTargetType.DIRECTORY,
                filesystem_root="data/test-workspaces",
            )
        ),
        created_at=now,
        updated_at=now,
    )


async def _assert_cancel_during_commit_is_safe(
    store: EventStore,
    setup: Callable[[EventStore], Awaitable[None]] | None,
    writer: Callable[[EventStore], Awaitable[Any]],
) -> None:
    """在写事务的 commit 等待点取消，事务必须回滚闭合、连接可继续写。"""
    if setup is not None:
        await setup(store)

    real_commit = aiosqlite.Connection.commit
    entered = asyncio.Event()

    async def slow_commit(self):
        entered.set()
        # 让取消稳定落在 INSERT…commit 之间（此时事务 OPEN）。
        await asyncio.sleep(0.05)
        return await real_commit(self)

    task = asyncio.create_task(writer(store))
    aiosqlite.Connection.commit = slow_commit
    try:
        await entered.wait()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        aiosqlite.Connection.commit = real_commit

    # 关键不变量：取消不得在共享连接上遗留未决事务（否则数据被后续自愈静默丢弃）。
    assert store.conn.in_transaction is False

    # 连接仍可正常写入。
    follow_up = await asyncio.wait_for(
        store.append_event(
            "post-cancel",
            EventType.AGENT_THOUGHT,
            {"thought": "after cancel", "token_count": 1},
        ),
        timeout=2,
    )
    assert follow_up.seq == 1


@pytest.mark.asyncio
async def test_ensure_tenant_cancel_during_commit(store: EventStore):
    await _assert_cancel_during_commit_is_safe(
        store, None, lambda s: s.ensure_tenant("tenant-cancel")
    )


@pytest.mark.asyncio
async def test_create_workspace_cancel_during_commit(store: EventStore):
    await _assert_cancel_during_commit_is_safe(
        store, None, lambda s: s.create_workspace(_make_workspace("ws-cancel"))
    )


@pytest.mark.asyncio
async def test_update_workspace_cancel_during_commit(store: EventStore):
    async def setup(s: EventStore) -> None:
        await s.create_workspace(_make_workspace("ws-upd-cancel"))

    async def writer(s: EventStore) -> None:
        await s.update_workspace("ws-upd-cancel", WorkspaceUpdate(description="changed"))

    await _assert_cancel_during_commit_is_safe(store, setup, writer)


@pytest.mark.asyncio
async def test_delete_workspace_cancel_during_commit(store: EventStore):
    async def setup(s: EventStore) -> None:
        await s.create_workspace(_make_workspace("ws-del-cancel"))

    async def writer(s: EventStore) -> None:
        await s.delete_workspace("ws-del-cancel")

    await _assert_cancel_during_commit_is_safe(store, setup, writer)


@pytest.mark.asyncio
async def test_upsert_conversation_cancel_during_commit(store: EventStore):
    await _assert_cancel_during_commit_is_safe(
        store, None, lambda s: s.upsert_conversation("conv-upsert-cancel", "T")
    )


@pytest.mark.asyncio
async def test_update_conversation_cancel_during_commit(store: EventStore):
    async def setup(s: EventStore) -> None:
        await s.upsert_conversation("conv-upd-cancel", "Old")

    async def writer(s: EventStore) -> None:
        await s.update_conversation("conv-upd-cancel", title="New")

    await _assert_cancel_during_commit_is_safe(store, setup, writer)


@pytest.mark.asyncio
async def test_delete_conversation_cancel_during_commit(store: EventStore):
    async def setup(s: EventStore) -> None:
        await s.upsert_conversation("conv-del-cancel", "T")

    async def writer(s: EventStore) -> None:
        await s.delete_conversation("conv-del-cancel")

    await _assert_cancel_during_commit_is_safe(store, setup, writer)


@pytest.mark.asyncio
async def test_increment_message_count_cancel_during_commit(store: EventStore):
    async def setup(s: EventStore) -> None:
        await s.upsert_conversation("conv-incr-cancel", "T")

    async def writer(s: EventStore) -> None:
        await s.increment_message_count("conv-incr-cancel")

    await _assert_cancel_during_commit_is_safe(store, setup, writer)


@pytest.mark.asyncio
async def test_claim_client_request_cancel_during_commit(store: EventStore):
    payload = RunStartedPayload(intent="x", conversation_id="conv-claim-cancel").model_dump()

    async def writer(s: EventStore) -> None:
        await s.claim_client_request("conv-claim-cancel", "cr-cancel", "run-claim-cancel", payload)

    await _assert_cancel_during_commit_is_safe(store, None, writer)


@pytest.mark.asyncio
async def test_concurrent_mixed_writes_and_appends_serialize(store: EventStore):
    """契约：所有写方法共用同一把写锁，并发混合写不产生孤儿事务/交错错误。"""

    async def worker(idx: int) -> None:
        tenant = f"tenant-{idx}"
        conv = f"conv-{idx}"
        ws = _make_workspace(f"ws-{idx}", tenant)
        await store.ensure_tenant(tenant)
        await store.create_workspace(ws)
        await store.upsert_conversation(conv, f"Title {idx}")
        await store.append_event(
            f"run-{idx}",
            EventType.RUN_STARTED,
            RunStartedPayload(intent="x", conversation_id=conv).model_dump(),
            workspace_id=ws.workspace_id,
        )
        await store.update_conversation(conv, title=f"Updated {idx}")
        await store.increment_message_count(conv)

    await asyncio.wait_for(asyncio.gather(*[worker(i) for i in range(20)]), timeout=20)

    assert store.conn.in_transaction is False
    for i in range(20):
        conv = await store.get_conversation(f"conv-{i}")
        assert conv is not None
        assert conv["message_count"] == 1


@pytest.mark.asyncio
async def test_concurrent_claims_and_appends_share_the_write_lock(store: EventStore):
    """claim 与 append 必须互斥：二者都写 events 表，分开的锁会让两条 INSERT 与
    并发 append 的 BEGIN IMMEDIATE 交错（同一孤儿事务类别）。"""

    async def appender(idx: int) -> None:
        await store.append_event(
            f"append-run-{idx}",
            EventType.RUN_STARTED,
            RunStartedPayload(intent="x").model_dump(),
        )

    async def claimant(idx: int) -> None:
        payload = RunStartedPayload(intent="x", conversation_id=f"conv-ca-{idx}").model_dump()
        await store.claim_client_request(
            f"conv-ca-{idx}",
            f"cr-{idx}",
            f"claim-run-{idx}",
            payload,
        )

    await asyncio.wait_for(
        asyncio.gather(*[appender(i) for i in range(10)], *[claimant(i) for i in range(10)]),
        timeout=20,
    )

    assert store.conn.in_transaction is False
    # 每个 claim 都产生了一条 seq=1 的 RUN_STARTED（无孤儿，无交错丢失）。
    for i in range(10):
        events = await store.get_events(f"claim-run-{i}")
        assert [e.seq for e in events] == [1]


@pytest.mark.asyncio
async def test_concurrent_duplicate_claim_has_single_winner(store: EventStore):
    """同一 client_request_id 的并发 claim：恰有一个创建者，其余返回同一 run_id。"""

    payload = RunStartedPayload(intent="x", conversation_id="conv-dup").model_dump()

    async def claim_with_run(run_id: str) -> tuple[str, bool]:
        return await store.claim_client_request("conv-dup", "cr-dup", run_id, payload)

    results = await asyncio.gather(claim_with_run("run-dup-a"), claim_with_run("run-dup-b"))

    winners = [claimed for _, claimed in results]
    assert winners.count(True) == 1
    # 失败方必须返回胜者的 run_id，而不是自己的。
    winner_run = next(run_id for run_id, claimed in results if claimed)
    assert all(run_id == winner_run for run_id, _ in results)
    events = await store.get_events(winner_run)
    assert [e.seq for e in events] == [1]
