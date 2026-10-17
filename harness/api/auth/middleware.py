"""Pure ASGI authentication/authorization middleware.

Authentication happens before routing and before body parsing. The middleware
is the only writer of ``current_principal``. Role checks are declarative; an
unregistered route is denied by default.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from harness.core.tenant import reset_current_principal, set_current_principal
from harness.models.principal import Principal, Role

_log = logging.getLogger("harness.api.auth")

_HTTP_UNAUTHORIZED = {"error": "unauthorized", "message": "Authentication required"}
_HTTP_FORBIDDEN = {"error": "forbidden", "message": "Insufficient permissions"}


@dataclass(frozen=True)
class PolicyRule:
    method: str
    pattern: re.Pattern[str]
    role: Role


class RoutePolicy:
    def __init__(self, rules: tuple[PolicyRule, ...]) -> None:
        self.rules = rules

    def required_role(self, method: str, path: str) -> Role | None:
        normalized = "GET" if method.upper() == "HEAD" else method.upper()
        for rule in self.rules:
            if rule.method == normalized and rule.pattern.match(path):
                return rule.role
        return None

    def allows(self, method: str, path: str, principal: Principal) -> bool:
        required = self.required_role(method, path)
        if required is None:
            return False
        return principal.has_role(required)


DEFAULT_POLICY = RoutePolicy(
    (
        # Reads are viewer-accessible; new non-GET routes must be registered
        # explicitly or they fall through to deny-by-default.
        PolicyRule("GET", re.compile(r"^/api/v1/.*$"), Role.VIEWER),
        PolicyRule("POST", re.compile(r"^/api/v1/runs$"), Role.OPERATOR),
        PolicyRule(
            "POST",
            re.compile(r"^/api/v1/runs/[^/]+/(pause|resume|confirm|feedback)$"),
            Role.OPERATOR,
        ),
        PolicyRule("DELETE", re.compile(r"^/api/v1/runs/[^/]+$"), Role.OPERATOR),
        PolicyRule("POST", re.compile(r"^/api/v1/conversations$"), Role.OPERATOR),
        PolicyRule(
            "POST",
            re.compile(r"^/api/v1/conversations/[^/]+/messages$"),
            Role.OPERATOR,
        ),
        PolicyRule("PATCH", re.compile(r"^/api/v1/conversations/[^/]+$"), Role.OPERATOR),
        PolicyRule("DELETE", re.compile(r"^/api/v1/conversations/[^/]+$"), Role.OPERATOR),
        PolicyRule("POST", re.compile(r"^/api/v1/workspaces$"), Role.ADMIN),
        PolicyRule("PATCH", re.compile(r"^/api/v1/workspaces/[^/]+$"), Role.ADMIN),
        PolicyRule("DELETE", re.compile(r"^/api/v1/workspaces/[^/]+$"), Role.ADMIN),
        PolicyRule("POST", re.compile(r"^/api/v1/operations/retry$"), Role.ADMIN),
        # Interactive docs are admin-only (and can be disabled entirely).
        PolicyRule("GET", re.compile(r"^/(docs|redoc|openapi\.json).*$"), Role.ADMIN),
    )
)


class AuthMiddleware:
    def __init__(
        self,
        app,
        policy: RoutePolicy = DEFAULT_POLICY,
        public_paths: tuple[str, ...] = ("/health",),
    ) -> None:
        self.app = app
        self.policy = policy
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.public_paths:
            await self.app(scope, receive, send)
            return

        # CORS preflight is handled by the outer CORSMiddleware. A bare
        # OPTIONS request carries no identity; let routing return 405.
        if scope["type"] == "http" and scope.get("method", "").upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return

        provider = self._provider(scope)
        if provider is None:
            await self._reject(scope, send, 401, "authentication provider not configured")
            return

        try:
            principal = await provider.authenticate(scope)
        except Exception:
            _log.exception("auth provider failed for %s", path)
            await self._reject(scope, send, 401, "authentication failed")
            return

        if principal is None:
            await self._reject(scope, send, 401, "missing or invalid credentials")
            return

        method = scope.get("method", "GET") if scope["type"] == "http" else "GET"
        if not self.policy.allows(method, path, principal):
            await self._reject(scope, send, 403, "insufficient role")
            return

        token = set_current_principal(principal)
        state = scope.setdefault("state", {})
        state["principal"] = principal
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_principal(token)

    @staticmethod
    def _provider(scope) -> Any:
        app = scope.get("app")
        state = getattr(app, "state", None)
        return getattr(state, "auth_provider", None)

    async def _reject(self, scope, send, status: int, reason: str) -> None:
        if scope["type"] == "websocket":
            code = 4401 if status == 401 else 4403
            await send({"type": "websocket.close", "code": code, "reason": reason})
            return

        payload = _HTTP_UNAUTHORIZED if status == 401 else _HTTP_FORBIDDEN
        body = json.dumps(payload).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="jagent", error="invalid_token"'))
        _log.info("auth reject %s %s: %s", scope.get("method"), scope.get("path"), reason)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
