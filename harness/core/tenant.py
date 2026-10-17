"""Request-scoped authenticated identity and tenant helpers."""

from __future__ import annotations

import hashlib
import re
from contextvars import ContextVar, Token

from harness.models.principal import Principal

# Single source of truth for what a tenant id may look like. The first and
# last characters must be alphanumeric, so ".", "..", "acme." and ".acme"
# cannot become valid identities (they would otherwise be rewritten into a
# different directory component).
TENANT_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?$")

current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)


def set_current_principal(principal: Principal) -> Token[Principal | None]:
    return current_principal.set(principal)


def reset_current_principal(token: Token[Principal | None]) -> None:
    current_principal.reset(token)


def get_current_principal() -> Principal | None:
    return current_principal.get()


def current_tenant_id() -> str:
    principal = current_principal.get()
    if principal is None:
        raise RuntimeError("No authenticated principal in context")
    return principal.tenant_id


def current_tenant_id_or_none() -> str | None:
    principal = current_principal.get()
    return principal.tenant_id if principal is not None else None


def safe_tenant_dir_component(tenant_id: str) -> str:
    """Map a tenant id to an injective, single-component directory name.

    Valid tenant ids (``TENANT_ID_RE``) pass through unchanged. Anything that
    had to be rewritten (invalid characters, leading/trailing dots, overlong
    values, legacy data) gets a short hash suffix, so two distinct tenant ids
    can never silently share one directory.
    """
    raw = tenant_id
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)
    value = value.strip(".")
    value = (value[:64] or "tenant").strip(".") or "tenant"
    if value != raw:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        value = f"{value[:48]}_{digest}"
    return value
