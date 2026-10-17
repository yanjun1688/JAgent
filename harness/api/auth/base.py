"""Auth provider protocol and request credential extraction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from harness.models.principal import Principal


class AuthProvider(Protocol):
    async def authenticate(self, scope: dict[str, Any]) -> Principal | None: ...


def header_value(scope: Mapping[str, Any], name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def extract_request_token(scope: Mapping[str, Any]) -> str | None:
    """Extract a credential from Authorization or the WS subprotocol.

    Browsers cannot set Authorization on WebSocket handshakes, so the token
    travels as a subprotocol value:
        Sec-WebSocket-Protocol: jagent.jwt, jagent.token.<jwt>
    The server accepts ``jagent.jwt``; the token is never echoed back.
    """
    authorization = header_value(scope, "authorization")
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()

    protocols = header_value(scope, "sec-websocket-protocol")
    if protocols:
        for raw in protocols.split(","):
            candidate = raw.strip()
            if candidate.startswith("jagent.token."):
                token = candidate[len("jagent.token.") :].strip()
                if token:
                    return token
    return None


def ws_accept_subprotocol(scope: Mapping[str, Any]) -> str | None:
    protocols = header_value(scope, "sec-websocket-protocol")
    if not protocols:
        return None
    offered = {item.strip() for item in protocols.split(",")}
    return "jagent.jwt" if "jagent.jwt" in offered else None
