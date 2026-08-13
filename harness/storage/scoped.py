"""Tenant-scoped facade over EventStore.

The facade fixes the tenant at construction time. Callers cannot supply a
different tenant to reads or writes, which makes the boundary auditable.
"""

import re
from typing import Any

from harness.models.events import Event, EventType
from harness.models.workspace import Tenant, Workspace, WorkspaceUpdate
from harness.storage.event_store import EventStore


class ScopedEventStore:
    def __init__(self, store: EventStore, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self._store = store
        self.tenant_id = tenant_id

    async def append_event(
        self,
        run_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        workspace_id: str | None = None,
        is_audit: bool = False,
    ) -> Event:
        return await self._store.append_event(
            run_id,
            event_type,
            payload,
            tenant_id=self.tenant_id,
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            is_audit=is_audit,
        )

    async def claim_client_request(
        self,
        conversation_id: str,
        client_request_id: str,
        run_id: str,
        payload: dict[str, Any],
        *,
        workspace_id: str | None = None,
    ) -> tuple[str, bool]:
        return await self._store.claim_client_request(
            conversation_id,
            client_request_id,
            run_id,
            payload,
            tenant_id=self.tenant_id,
            workspace_id=workspace_id,
        )

    async def get_events(self, run_id: str) -> list[Event]:
        return [e for e in await self._store.get_events(run_id) if e.tenant_id == self.tenant_id]

    async def get_event_range(self, run_id: str, from_seq: int, to_seq: int | None = None) -> list[Event]:
        return [e for e in await self._store.get_event_range(run_id, from_seq, to_seq) if e.tenant_id == self.tenant_id]

    async def get_events_for_runs(self, run_ids: list[str]) -> list[Event]:
        return await self._store.get_events_for_runs(run_ids, tenant_id=self.tenant_id)

    async def get_workspace_events(self, workspace_id: str) -> list[Event]:
        if await self.get_workspace(workspace_id) is None:
            return []
        return await self._store.get_workspace_events(workspace_id, tenant_id=self.tenant_id)

    async def list_runs(self, limit: int = 50, offset: int = 0, workspace_id: str | None = None) -> list[dict]:
        return await self._store.list_runs(limit, offset, tenant_id=self.tenant_id, workspace_id=workspace_id)

    async def total_run_count(self, workspace_id: str | None = None) -> int:
        return await self._store.total_run_count(tenant_id=self.tenant_id, workspace_id=workspace_id)

    async def list_all_run_ids(self) -> list[str]:
        return await self._store.list_all_run_ids(tenant_id=self.tenant_id)

    async def find_by_idempotency_key(self, run_id: str, event_type: EventType, key: str) -> Event | None:
        event = await self._store.find_by_idempotency_key(run_id, event_type, key)
        return event if event is not None and event.tenant_id == self.tenant_id else None

    async def find_confirmation_by_id(self, run_id: str, confirmation_id: str) -> Event | None:
        event = await self._store.find_confirmation_by_id(run_id, confirmation_id)
        return event if event is not None and event.tenant_id == self.tenant_id else None

    async def get_conversation(self, conversation_id: str) -> dict | None:
        row = await self._store.get_conversation(conversation_id)
        return row if row and row.get("tenant_id", "default") == self.tenant_id else None

    async def delete_conversation(self, conversation_id: str) -> None:
        if await self.get_conversation(conversation_id):
            await self._store.delete_conversation(conversation_id)

    async def update_conversation(
        self, conversation_id: str, title: str | None = None, status: str | None = None
    ) -> bool:
        if await self.get_conversation(conversation_id) is None:
            return False
        return await self._store.update_conversation(conversation_id, title=title, status=status)

    async def list_conversations(self, limit: int = 50, offset: int = 0, user_id: str | None = None) -> list[dict]:
        return await self._store.list_conversations(limit, offset, user_id, tenant_id=self.tenant_id)

    async def total_conversation_count(self, user_id: str | None = None) -> int:
        return await self._store.total_conversation_count(user_id, tenant_id=self.tenant_id)

    async def upsert_conversation(self, conversation_id: str, title: str, user_id: str = "default") -> None:
        await self._store.upsert_conversation(conversation_id, title, user_id, tenant_id=self.tenant_id)

    async def get_events_for_conversation(self, conversation_id: str) -> list[Event]:
        return [
            event
            for event in await self._store.get_events_for_conversation(conversation_id)
            if event.tenant_id == self.tenant_id
        ]

    async def increment_message_count(self, conversation_id: str) -> None:
        if await self.get_conversation(conversation_id):
            await self._store.increment_message_count(conversation_id)

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        workspace = await self._store.get_workspace(workspace_id)
        return workspace if workspace and workspace.tenant_id == self.tenant_id else None

    async def list_workspaces(self) -> list[Workspace]:
        return await self._store.list_workspaces(tenant_id=self.tenant_id)

    async def create_workspace(self, workspace: Workspace) -> Workspace:
        if workspace.tenant_id != self.tenant_id:
            raise ValueError("workspace tenant does not match scoped tenant")
        created = await self._store.create_workspace(workspace)
        # P1-A: 对返回值做同源复核 —— raw store 可能因 PK 冲突返回既存行，
        # 若该行属于其他租户必须失败，防止调用方拿到他人工作区。
        if created.tenant_id != self.tenant_id:
            raise ValueError("scoped tenant mismatch on workspace create")
        return created

    async def update_workspace(self, workspace_id: str, update: WorkspaceUpdate) -> Workspace | None:
        if await self.get_workspace(workspace_id) is None:
            return None
        return await self._store.update_workspace(workspace_id, update)

    async def delete_workspace(self, workspace_id: str) -> Workspace | None:
        if await self.get_workspace(workspace_id) is None:
            return None
        return await self._store.delete_workspace(workspace_id)

    async def ensure_tenant(self) -> Tenant:
        return await self._store.ensure_tenant(self.tenant_id)

    async def get_latest_seq(self, run_id: str) -> int:
        events = await self.get_events(run_id)
        return max((event.seq for event in events), default=0)

    async def execute_query(self, sql: str, params: list | tuple | None = None) -> list[dict]:
        """Run a predefined read query with tenant filtering injected.

        Contract (fail-closed):
          - Only a single SELECT over `events` with a `WHERE` clause is allowed.
          - `FROM events` must be written without a table alias (e.g.
            `FROM events e WHERE` is rejected) because the tenant filter is
            injected immediately after `from events where` via regex.
          - UNION / CTE (WITH) / JOIN / multi-reference of `events` / comment
            or statement separators are rejected outright: they could
            re-reference `events` outside the injected WHERE and leak
            cross-tenant rows. (P2-A hardening.)
        """
        normalized = re.sub(r"\s+", " ", sql.strip().lower())
        if not normalized.startswith("select") or " from events " not in f" {normalized} ":
            raise ValueError("Scoped analysis only permits SELECT queries over events")
        if re.search(r"\b(union|join|with|insert|update|delete|drop|alter|create|attach|pragma)\b", normalized):
            raise ValueError("Scoped event queries must not use UNION, JOIN, CTE, or DML")
        if ";" in sql or "--" in sql or "/*" in sql:
            raise ValueError("Scoped event queries must be a single statement without comment syntax")
        if len(re.findall(r"\bevents\b", normalized)) != 1:
            raise ValueError("Scoped event queries must reference the events table exactly once")
        match = re.search(r"\bfrom\s+events\s+where\s+", sql, flags=re.IGNORECASE)
        if match is None:
            raise ValueError("Scoped event queries must include a WHERE clause")
        scoped_sql = sql[: match.end()] + "tenant_id = ? AND " + sql[match.end() :]
        original_params = list(params or [])
        before_where = sql[: match.start()].count("?")
        original_params.insert(before_where, self.tenant_id)
        return await self._store.execute_query(scoped_sql, original_params)

    async def execute_query_one(self, sql: str, params: list | tuple | None = None) -> dict | None:
        rows = await self.execute_query(sql, params)
        return rows[0] if rows else None

    @property
    def raw_store(self) -> EventStore:
        """Infrastructure escape hatch; do not use from business handlers."""
        return self._store

    def evict_run_to_conv(self, run_id: str) -> None:
        """透传到 raw store 的 run→conversation 缓存驱逐。

        Bug JAGENT-2026-P1-13: scheduler 收尾（base.py finally）调用该方法，
        此前 ScopedEventStore 未实现 → 每次 run 结束抛 AttributeError。
        缓存只存在 raw store 上，按 tenant 无关，直接透传。
        """
        self._store.evict_run_to_conv(run_id)
