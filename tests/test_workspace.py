from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from harness.api.deps import HarnessAPI
from harness.core.tenant import reset_current_tenant, set_current_tenant
from harness.models.events import EventType, RunStartedPayload
from harness.models.workspace import ExecutionTarget, ExecutionTargetType, Workspace, WorkspaceScope, WorkspaceUpdate
from harness.storage.event_store import EventStore
from harness.storage.scoped import ScopedEventStore


def make_workspace(tenant_id: str, name: str = "work") -> Workspace:
    now = time.time()
    return Workspace(
        workspace_id=f"{tenant_id}-{name}",
        tenant_id=tenant_id,
        name=name,
        scope=WorkspaceScope(
            target=ExecutionTarget(
                type=ExecutionTargetType.DIRECTORY,
                filesystem_root="data/test-workspaces",
            )
        ),
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_scoped_store_prevents_cross_tenant_reads_and_writes():
    store = EventStore(":memory:")
    await store.initialize()
    try:
        tenant_a = ScopedEventStore(store, "tenant-a")
        tenant_b = ScopedEventStore(store, "tenant-b")
        await tenant_a.ensure_tenant()
        await tenant_b.ensure_tenant()
        await tenant_a.create_workspace(make_workspace("tenant-a"))
        await tenant_a.append_event(
            "run-a", EventType.RUN_STARTED, RunStartedPayload(intent="a").model_dump(), workspace_id="tenant-a-work"
        )
        await tenant_b.append_event("run-b", EventType.RUN_STARTED, RunStartedPayload(intent="b").model_dump())
        rows = await tenant_a.execute_query(
            "SELECT event_type, COUNT(*) AS cnt FROM events WHERE created_at >= ? AND created_at <= ? GROUP BY event_type",
            (0, 9999999999),
        )
        assert rows == [{"event_type": "RunStarted", "cnt": 1}]
        aggregate = await tenant_a.execute_query_one(
            "SELECT COUNT(DISTINCT run_id) as run_count, COALESCE(SUM(CASE WHEN event_type = ? THEN json_extract(payload, '$.token_count') ELSE 0 END), 0) as token_sum FROM events WHERE created_at >= ? AND created_at <= ?",
            (EventType.AGENT_THOUGHT.value, 0, 9999999999),
        )
        assert aggregate == {"run_count": 1, "token_sum": 0}
        from harness.analysis.service import AnalysisService

        dashboard = await AnalysisService(tenant_a).get_dashboard(since=0, until=9999999999)
        assert dashboard.overview.total_runs == 1

        assert await tenant_a.get_workspace("tenant-a-work") is not None
        assert await tenant_b.get_workspace("tenant-a-work") is None
        assert [row["run_id"] for row in await tenant_a.list_runs()] == ["run-a"]
        assert [row["run_id"] for row in await tenant_b.list_runs()] == ["run-b"]
        assert (await tenant_a.get_events("run-b")) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_scoped_store_evict_run_to_conv_passthrough():
    """Bug 9 回归：ScopedEventStore 必须透传 evict_run_to_conv，scheduler 收尾不再抛 AttributeError。"""
    store = EventStore(":memory:")
    await store.initialize()
    try:
        scoped = ScopedEventStore(store, "tenant")
        # 不应抛 AttributeError
        scoped.evict_run_to_conv("run-xyz")
        # 缓存驱逐实际生效（不存在的 key 也应静默成功）
        scoped.evict_run_to_conv("run-xyz")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_workspace_update_and_soft_delete_are_scoped():
    store = EventStore(":memory:")
    await store.initialize()
    try:
        scoped = ScopedEventStore(store, "tenant")
        workspace = await scoped.create_workspace(make_workspace("tenant"))
        updated = await scoped.update_workspace(workspace.workspace_id, WorkspaceUpdate(description="changed"))
        assert updated is not None
        assert updated.description == "changed"
        deleted = await scoped.delete_workspace(workspace.workspace_id)
        assert deleted is not None
        assert deleted.status == "deleted"
        assert await scoped.get_workspace(workspace.workspace_id) is not None
        assert await scoped.list_workspaces() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_workspace_audit_events_are_excluded_from_run_listings():
    """B1 regression: WORKSPACE_* audit events (run_id == workspace_id) must
    not appear as phantom runs in list_runs / total_run_count."""
    store = EventStore(":memory:")
    await store.initialize()
    try:
        scoped = ScopedEventStore(store, "tenant")
        from harness.models.events import WorkspaceCreatedPayload

        workspace = await scoped.create_workspace(make_workspace("tenant", name="audit-ws"))
        await scoped.append_event(
            workspace.workspace_id,
            EventType.WORKSPACE_CREATED,
            WorkspaceCreatedPayload(
                workspace_id=workspace.workspace_id,
                tenant_id="tenant",
                name=workspace.name,
                description="",
                scope={},
            ).model_dump(),
            workspace_id=workspace.workspace_id,
            is_audit=True,
        )
        await scoped.append_event(
            "run-audit-1",
            EventType.RUN_STARTED,
            RunStartedPayload(intent="real run").model_dump(),
            workspace_id=workspace.workspace_id,
        )

        run_ids = [row["run_id"] for row in await scoped.list_runs()]
        assert run_ids == ["run-audit-1"], f"audit stream leaked into list_runs: {run_ids}"
        assert await scoped.total_run_count() == 1
        # Per-workspace count must not include the workspace's own audit events.
        assert await scoped.total_run_count(workspace.workspace_id) == 1

        # Audit trail is still queryable via the audit API.
        audit = await scoped.get_workspace_events(workspace.workspace_id)
        assert [e.event_type for e in audit] == [EventType.WORKSPACE_CREATED]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ensure_default_workspace_is_isolated_per_tenant():
    """P1-A 回归：每个租户的 default 工作区（id + 磁盘根）必须相互隔离。"""
    store = EventStore(":memory:")
    await store.initialize()
    try:
        api = HarnessAPI(store=store)
        await api.raw_store.ensure_tenant("tenant-a")
        await api.raw_store.ensure_tenant("tenant-b")

        token_a = set_current_tenant("tenant-a")
        try:
            ws_a = await api.ensure_default_workspace()
        finally:
            reset_current_tenant(token_a)

        token_b = set_current_tenant("tenant-b")
        try:
            ws_b = await api.ensure_default_workspace()
        finally:
            reset_current_tenant(token_b)

        assert ws_a.tenant_id == "tenant-a"
        assert ws_b.tenant_id == "tenant-b"
        assert ws_a.workspace_id != ws_b.workspace_id
        root_a = Path(ws_a.scope.target.filesystem_root).resolve()
        root_b = Path(ws_b.scope.target.filesystem_root).resolve()
        assert root_a != root_b
        # 各自无法看到对方的工作区
        assert await api.store.get_workspace(ws_b.workspace_id) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cross_tenant_workspace_id_collision_does_not_leak():
    """P1-A 回归：租户 B 试图占用租户 A 已拥有的 workspace_id 时必须失败，
    绝不能静默继承 A 的工作区定义。"""
    store = EventStore(":memory:")
    await store.initialize()
    try:
        api = HarnessAPI(store=store)
        await api.raw_store.ensure_tenant("tenant-a")
        await api.raw_store.ensure_tenant("tenant-b")
        now = time.time()

        def make_ws(tenant_id: str) -> Workspace:
            return Workspace(
                workspace_id="default",
                tenant_id=tenant_id,
                name="default",
                scope=WorkspaceScope(
                    target=ExecutionTarget(
                        type=ExecutionTargetType.DIRECTORY,
                        filesystem_root=f"data/workspaces/{tenant_id}/work",
                    )
                ),
                created_at=now,
                updated_at=now,
            )

        # 租户 A 独占共享 id "default"
        await api.raw_store.create_workspace(make_ws("tenant-a"))
        # 租户 B 用同一 id 创建 → raw store 抛 IntegrityError（不再是返回 A 的行）
        with pytest.raises(sqlite3.IntegrityError):
            await api.raw_store.create_workspace(make_ws("tenant-b"))
        # scoped facade 同样失败（含返回值复核路径）
        token_b = set_current_tenant("tenant-b")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await api.store.create_workspace(make_ws("tenant-b"))
        finally:
            reset_current_tenant(token_b)
        # 租户 B 通过 ensure_default_workspace 得到的是自己的 default-tenant-b，而非 A 的 "default"
        token_b = set_current_tenant("tenant-b")
        try:
            ws_b = await api.ensure_default_workspace()
        finally:
            reset_current_tenant(token_b)
        assert ws_b.workspace_id == "default-tenant-b"
        assert ws_b.tenant_id == "tenant-b"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_concurrent_workspace_creation_is_serialized():
    """P2 回归：workspace DML 必须与 append_event 的 BEGIN IMMEDIATE 串行化。

    Bug（黑盒并发创建 workspace 触发间歇 500）：create_workspace 的 INSERT
    未持 _db_write_lock，并发时另一协程的 append_event 可能在 INSERT 与
    commit 之间发起 BEGIN IMMEDIATE → "cannot start a transaction within a
    transaction" → 500，但 workspace 已落库（语义不一致）。
    """
    store = EventStore(":memory:")
    await store.initialize()
    try:
        scoped = ScopedEventStore(store, "tenant-a")
        await scoped.ensure_tenant()

        async def create_one(idx: int) -> None:
            ws = make_workspace("tenant-a", name=f"ws-{idx}")
            await scoped.create_workspace(ws)
            # 模拟 routes.create_workspace 的审计事件写入路径
            from harness.models.events import WorkspaceCreatedPayload

            await scoped.append_event(
                ws.workspace_id,
                EventType.WORKSPACE_CREATED,
                WorkspaceCreatedPayload(
                    workspace_id=ws.workspace_id,
                    tenant_id=ws.tenant_id,
                    name=ws.name,
                    description=ws.description,
                    scope=ws.scope.model_dump(mode="json"),
                ).model_dump(),
                workspace_id=ws.workspace_id,
                is_audit=True,
            )

        await asyncio.gather(*[create_one(i) for i in range(20)])
        workspaces = await scoped.list_workspaces()
        assert len(workspaces) == 20
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_execute_query_rejects_tenant_filter_bypass():
    """P2-A 回归：UNION/CTE/JOIN/别名/注释/多语句不得绕过 tenant_id 注入。"""
    store = EventStore(":memory:")
    await store.initialize()
    try:
        scoped = ScopedEventStore(store, "tenant-a")
        await scoped.ensure_tenant()
        evil_queries = [
            "SELECT * FROM events WHERE run_id = ? UNION SELECT * FROM events",
            "WITH x AS (SELECT * FROM events) SELECT * FROM x WHERE run_id = ?",
            "SELECT e.* FROM events e JOIN events f ON e.run_id = f.run_id WHERE e.run_id = ?",
            "SELECT * FROM events e WHERE e.run_id = ?",
            "SELECT * FROM events WHERE run_id = ?; DROP TABLE events",
            "SELECT * FROM events WHERE run_id = ? -- leak comment",
            "SELECT * FROM events WHERE run_id = ? /* leak */",
            "SELECT * FROM events",
        ]
        for query in evil_queries:
            with pytest.raises(ValueError):
                await scoped.execute_query(query, ["x"])
        # 合法查询仍可用且带租户过滤
        rows = await scoped.execute_query("SELECT run_id FROM events WHERE created_at >= ?", (0,))
        assert rows == []
    finally:
        await store.close()
