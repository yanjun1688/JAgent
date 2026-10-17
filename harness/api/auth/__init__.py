"""Authentication/authorization package (JWT resource server + ASGI middleware)."""

from __future__ import annotations

from harness.api.auth.base import AuthProvider
from harness.api.auth.jwt_provider import InvalidTokenError, JwtAuthProvider, JwtSettings
from harness.api.auth.middleware import DEFAULT_POLICY, AuthMiddleware, PolicyRule, RoutePolicy
from harness.models.principal import Principal, Role


def configure_auth_provider(provider: AuthProvider | None) -> None:
    """Install the process-wide auth provider (production: JwtAuthProvider)."""
    from harness.api.app import app

    app.state.auth_provider = provider


__all__ = [
    "AuthMiddleware",
    "AuthProvider",
    "DEFAULT_POLICY",
    "InvalidTokenError",
    "JwtAuthProvider",
    "JwtSettings",
    "PolicyRule",
    "Principal",
    "Role",
    "RoutePolicy",
    "configure_auth_provider",
]
