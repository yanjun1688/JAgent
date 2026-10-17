"""Authenticated principal model (trusted identity, never client-supplied)."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, field_validator


class Role(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


_ROLE_LEVEL: dict[Role, int] = {
    Role.VIEWER: 1,
    Role.OPERATOR: 2,
    Role.ADMIN: 3,
}


class Principal(BaseModel):
    """The only identity shape trusted by the application.

    ``tenant_id`` is derived from a verified credential (JWT claim) and is
    the sole input to ``ScopedEventStore``. It must never be copied from a
    request header or body.
    """

    subject: str
    tenant_id: str
    roles: frozenset[Role]
    auth_method: Literal["jwt", "gateway", "test"] = "jwt"
    issuer: str | None = None
    jwt_id: str | None = None
    expires_at: int | None = None

    @field_validator("roles", mode="before")
    @classmethod
    def _coerce_roles(cls, value):
        if isinstance(value, str):
            raise ValueError("roles must be a collection of role names")
        try:
            return frozenset(Role(item) for item in value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown role: {exc}") from exc

    def has_role(self, required: Role) -> bool:
        """Role hierarchy: admin > operator > viewer."""
        if not self.roles:
            return False
        return max(_ROLE_LEVEL[role] for role in self.roles) >= _ROLE_LEVEL[required]
