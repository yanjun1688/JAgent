"""Request-scoped tenant identity."""

import re
from contextvars import ContextVar, Token

current_tenant: ContextVar[str] = ContextVar("current_tenant", default="default")


def set_current_tenant(tenant_id: str) -> Token[str]:
    normalized = tenant_id.strip()
    if not normalized:
        normalized = "default"
    if len(normalized) > 128:
        raise ValueError("tenant_id must be a non-empty value of at most 128 characters")
    return current_tenant.set(normalized)


def reset_current_tenant(token: Token[str]) -> None:
    current_tenant.reset(token)


def safe_tenant_dir_component(tenant_id: str) -> str:
    """Sanitize a tenant id into a safe single path component.

    Used to build per-tenant filesystem roots so two tenants can never share
    (or escape) the same on-disk directory. Rejects traversal components and
    any character outside [A-Za-z0-9_.-].
    """
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", tenant_id)
    value = value.strip(".")
    return (value[:64] or "tenant").strip(".") or "tenant"
