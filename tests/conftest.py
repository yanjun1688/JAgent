from __future__ import annotations

import pytest

from harness.storage.event_store import EventStore


@pytest.fixture
async def store():
    store = EventStore(":memory:")
    await store.initialize()
    yield store
    await store.close()
