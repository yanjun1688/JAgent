from __future__ import annotations

import pytest

from harness.storage.event_store import EventStore


@pytest.fixture
async def store():
    store = EventStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture(autouse=True)
def _default_auth_provider():
    """Give existing API tests an authenticated principal by default.

    Auth tests override ``app.state.auth_provider`` with a JWT provider.
    This is a test seam, not a deployment mode: production code never
    enables it via env/header.
    """
    from harness.api.app import app
    from harness.api.auth.principal import Principal, Role
    from tests.support.auth import TestAuthProvider

    previous = getattr(app.state, "auth_provider", None)
    if previous is None:
        app.state.auth_provider = TestAuthProvider(
            Principal(
                subject="test:admin",
                tenant_id="default",
                roles={Role.ADMIN},
                auth_method="test",
            )
        )
    yield
    app.state.auth_provider = previous
