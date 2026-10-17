"""Test-only auth provider.

This module is never imported by production code. It exists so tests can
exercise the real AuthMiddleware without a JWT keypair, and still switch
tenants/roles per request when a test needs to.

Token format (test provider only):
    Authorization: Bearer test:<tenant>:<role>
    Sec-WebSocket-Protocol: jagent.jwt, jagent.token.test:<tenant>:<role>
"""

from __future__ import annotations

from typing import Any

from harness.api.auth.base import extract_request_token
from harness.api.auth.principal import Principal, Role


def auth_headers(tenant: str = "default", role: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer test:{tenant}:{role}"}


def ws_subprotocols(token: str) -> list[str]:
    return ["jagent.jwt", f"jagent.token.{token}"]


def test_token(tenant: str = "default", role: str = "admin") -> str:
    return f"test:{tenant}:{role}"


class TestAuthProvider:
    """Maps ``test:<tenant>:<role>`` tokens to a Principal.

    A ``None`` default means "reject every request" (used by 401 tests).
    """

    def __init__(self, default: Principal | None = None) -> None:
        self.default = default

    async def authenticate(self, scope: dict[str, Any]) -> Principal | None:
        token = extract_request_token(scope)
        if token and token.startswith("test:"):
            parts = token.split(":")
            if len(parts) == 3:
                _, tenant, role = parts
                try:
                    return Principal(
                        subject=f"test:{tenant}",
                        tenant_id=tenant,
                        roles={Role(role)},
                        auth_method="test",
                    )
                except ValueError:
                    return None
        return self.default
