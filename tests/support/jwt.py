"""JWT keypair and token helpers for auth tests."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from harness.api.auth.jwt_provider import JwtSettings

DEFAULT_KID = "test-kid"


def make_settings(public_dir: Path, **overrides) -> JwtSettings:
    settings = JwtSettings(
        issuer="jagent",
        audience="jagent-api",
        algorithms=("RS256",),
        public_keys_dir=public_dir,
        leeway_seconds=30,
        max_ttl_seconds=43200,
        tenant_claim="tenant_id",
        roles_claim="roles",
    )
    return replace(settings, **overrides) if overrides else settings


def generate_rsa_keypair(tmp_path: Path, kid: str = DEFAULT_KID) -> tuple[bytes, Path, str]:
    """Create an RSA keypair and write ``<kid>.pem`` into ``tmp_path``."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_dir = tmp_path / "jwt_public_keys"
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / f"{kid}.pem").write_bytes(public_pem)
    return private_pem, public_dir, kid


def generate_ec_keypair(tmp_path: Path, kid: str = "ec-kid") -> tuple[bytes, Path, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_dir = tmp_path / "jwt_public_keys"
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / f"{kid}.pem").write_bytes(public_pem)
    return private_pem, public_dir, kid


def mint_token(
    private_pem: bytes,
    kid: str = DEFAULT_KID,
    *,
    issuer: str = "jagent",
    audience: str = "jagent-api",
    subject: str = "user:alice",
    tenant_id: str = "default",
    roles: list[str] | None = None,
    iat: int | None = None,
    nbf: int | None = None,
    exp: int | None = None,
    ttl: int = 3600,
    jti: str = "test-jti",
    algorithm: str = "RS256",
    headers: dict | None = None,
    **extra,
) -> str:
    now = int(time.time()) if iat is None else iat
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "tenant_id": tenant_id,
        "roles": ["admin"] if roles is None else roles,
        "iat": now,
        "nbf": now if nbf is None else nbf,
        "exp": now + ttl if exp is None else exp,
        "jti": jti,
    }
    payload.update(extra)
    header = {"kid": kid}
    if headers:
        header.update(headers)
    return jwt.encode(payload, private_pem, algorithm=algorithm, headers=header)


def mint_without_kid(private_pem: bytes, **kwargs) -> str:
    kwargs.setdefault("headers", {})
    token = mint_token(private_pem, **kwargs)
    # Re-encode with an explicitly empty kid header.
    payload = jwt.decode(
        token,
        options={"verify_signature": False, "verify_aud": False, "verify_exp": False},
    )
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={})


def mint_none_alg(*, issuer: str = "jagent", audience: str = "jagent-api") -> str:
    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": "user:alice",
        "tenant_id": "default",
        "roles": ["admin"],
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "jti": "none-jti",
    }
    return jwt.encode(payload, "", algorithm="none")
