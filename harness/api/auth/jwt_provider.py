"""JWT verification (Resource Server side)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt

from harness.api.auth.base import AuthProvider, extract_request_token
from harness.core.tenant import TENANT_ID_RE
from harness.models.principal import Principal, Role

_log = logging.getLogger("harness.api.auth")

_MAX_TOKEN_BYTES = 8192
_MAX_SUBJECT_LEN = 256
_MAX_ROLES = 8
_FORBIDDEN_ALGORITHMS = {"none", "HS256", "HS384", "HS512"}


class InvalidTokenError(ValueError):
    """Raised when a JWT is structurally valid but not acceptable."""


@dataclass(frozen=True)
class JwtSettings:
    issuer: str
    audience: str
    algorithms: tuple[str, ...]
    public_keys_dir: Path | None = None
    jwks_url: str | None = None
    leeway_seconds: int = 30
    max_ttl_seconds: int = 43200
    tenant_claim: str = "tenant_id"
    roles_claim: str = "roles"

    @classmethod
    def from_env(cls) -> JwtSettings:
        mode = os.environ.get("JAGENT_AUTH_MODE", "jwt").strip().lower()
        if mode != "jwt":
            raise ValueError(
                f"JAGENT_AUTH_MODE={mode!r} is not supported by this build; expected 'jwt'"
            )

        issuer = os.environ.get("JAGENT_JWT_ISSUER", "").strip()
        audience = os.environ.get("JAGENT_JWT_AUDIENCE", "").strip()
        if not issuer or not audience:
            raise ValueError("JAGENT_JWT_ISSUER and JAGENT_JWT_AUDIENCE are required")

        algorithms = tuple(
            item.strip()
            for item in os.environ.get("JAGENT_JWT_ALGORITHMS", "RS256,ES256").split(",")
            if item.strip()
        )
        if not algorithms:
            raise ValueError("JAGENT_JWT_ALGORITHMS must not be empty")
        forbidden = _FORBIDDEN_ALGORITHMS.intersection(algorithms)
        if forbidden:
            raise ValueError(f"forbidden JWT algorithms: {sorted(forbidden)}")

        keys_dir_raw = os.environ.get("JAGENT_JWT_PUBLIC_KEYS_DIR", "").strip()
        jwks_url = os.environ.get("JAGENT_JWT_JWKS_URL", "").strip() or None
        if bool(keys_dir_raw) == bool(jwks_url):
            raise ValueError(
                "configure exactly one of JAGENT_JWT_PUBLIC_KEYS_DIR or JAGENT_JWT_JWKS_URL"
            )
        if jwks_url:
            raise NotImplementedError(
                "JWKS verification is not implemented in this phase; "
                "use JAGENT_JWT_PUBLIC_KEYS_DIR"
            )

        keys_dir = Path(keys_dir_raw).expanduser()
        if not keys_dir.is_dir():
            raise ValueError(f"JAGENT_JWT_PUBLIC_KEYS_DIR does not exist: {keys_dir}")

        return cls(
            issuer=issuer,
            audience=audience,
            algorithms=algorithms,
            public_keys_dir=keys_dir,
            jwks_url=jwks_url,
            leeway_seconds=_int_env("JAGENT_JWT_LEEWAY_SECONDS", 30),
            max_ttl_seconds=_int_env("JAGENT_JWT_MAX_TTL_SECONDS", 43200),
            tenant_claim=os.environ.get("JAGENT_JWT_TENANT_CLAIM", "tenant_id").strip() or "tenant_id",
            roles_claim=os.environ.get("JAGENT_JWT_ROLES_CLAIM", "roles").strip() or "roles",
        )


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


class JwtAuthProvider(AuthProvider):
    def __init__(self, settings: JwtSettings) -> None:
        self.settings = settings
        if settings.public_keys_dir is None:
            raise ValueError("public_keys_dir is required")
        self._keys = self._load_public_keys(settings.public_keys_dir)

    @classmethod
    def from_env(cls) -> JwtAuthProvider:
        return cls(JwtSettings.from_env())

    @staticmethod
    def _load_public_keys(keys_dir: Path) -> dict[str, bytes]:
        keys: dict[str, bytes] = {}
        for path in sorted(keys_dir.glob("*.pem")):
            keys[path.stem] = path.read_bytes()
        if not keys:
            raise ValueError(f"no public keys found in {keys_dir}")
        return keys

    def verify_token(self, token: str) -> Principal:
        if not isinstance(token, str) or not token:
            raise InvalidTokenError("missing token")
        if len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise InvalidTokenError("token too large")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(f"malformed token header: {exc}") from exc

        algorithm = header.get("alg")
        if algorithm not in self.settings.algorithms:
            raise InvalidTokenError(f"algorithm not allowed: {algorithm!r}")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidTokenError("token header must contain a kid")
        key = self._keys.get(kid)
        if key is None:
            raise InvalidTokenError(f"unknown kid: {kid}")

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self.settings.algorithms),
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=self.settings.leeway_seconds,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub", "jti"],
                    "verify_signature": True,
                },
            )
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(f"invalid token: {exc}") from exc

        tenant_id = claims.get(self.settings.tenant_claim)
        if not isinstance(tenant_id, str) or not TENANT_ID_RE.fullmatch(tenant_id):
            raise InvalidTokenError("tenant_id claim is missing or malformed")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > _MAX_SUBJECT_LEN:
            raise InvalidTokenError("sub claim is missing or malformed")

        roles_claim = claims.get(self.settings.roles_claim)
        if not isinstance(roles_claim, (list, tuple)) or not roles_claim:
            raise InvalidTokenError("roles claim must be a non-empty array")
        if len(roles_claim) > _MAX_ROLES:
            raise InvalidTokenError("too many roles")
        try:
            roles = frozenset(Role(item) for item in roles_claim)
        except (TypeError, ValueError) as exc:
            raise InvalidTokenError(f"unknown role: {exc}") from exc

        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        if not isinstance(issued_at, int) or not isinstance(expires_at, int):
            raise InvalidTokenError("iat/exp claims must be integers")
        ttl = expires_at - issued_at
        if ttl <= 0 or ttl > self.settings.max_ttl_seconds:
            raise InvalidTokenError("token TTL is outside the allowed range")

        token_use = claims.get("token_use")
        if token_use is not None and token_use != "access":
            raise InvalidTokenError("token_use must be 'access'")

        return Principal(
            subject=subject,
            tenant_id=tenant_id,
            roles=roles,
            auth_method="jwt",
            issuer=claims.get("iss"),
            jwt_id=claims.get("jti"),
            expires_at=expires_at,
        )

    async def authenticate(self, scope: dict[str, Any]) -> Principal | None:
        token = extract_request_token(scope)
        if token is None:
            return None
        try:
            return self.verify_token(token)
        except InvalidTokenError as exc:
            _log.warning("JWT rejected: %s", exc)
            return None
