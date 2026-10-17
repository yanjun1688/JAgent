"""TDD: JWT verifier contract (P1).

These tests reproduce the pentest findings at the token layer:
- forged / unsigned / algorithm-confused tokens must be rejected
- tenant_id comes only from a verified claim
- unknown roles, missing claims, overlong TTL and malformed tenant ids fail closed
"""

from __future__ import annotations

import time

import jwt
import pytest

from harness.api.auth.jwt_provider import InvalidTokenError, JwtAuthProvider
from harness.api.auth.principal import Principal, Role
from tests.support.jwt import (
    DEFAULT_KID,
    generate_ec_keypair,
    generate_rsa_keypair,
    make_settings,
    mint_none_alg,
    mint_token,
    mint_without_kid,
)


@pytest.fixture
def keys(tmp_path):
    private_pem, public_dir, kid = generate_rsa_keypair(tmp_path)
    return private_pem, public_dir, kid


@pytest.fixture
def provider(keys):
    _, public_dir, _ = keys
    return JwtAuthProvider(make_settings(public_dir))


def test_valid_token_yields_principal(provider, keys):
    private_pem, _, kid = keys
    token = mint_token(private_pem, kid, subject="ops:alice", tenant_id="tenant-a", roles=["operator"])
    principal = provider.verify_token(token)
    assert isinstance(principal, Principal)
    assert principal.subject == "ops:alice"
    assert principal.tenant_id == "tenant-a"
    assert principal.roles == {Role.OPERATOR}
    assert principal.auth_method == "jwt"
    assert principal.issuer == "jagent"
    assert principal.jwt_id == "test-jti"
    assert principal.expires_at is not None


def test_es256_token_is_accepted(tmp_path):
    private_pem, public_dir, kid = generate_ec_keypair(tmp_path)
    provider = JwtAuthProvider(make_settings(public_dir, algorithms=("ES256",)))
    token = mint_token(private_pem, kid, algorithm="ES256", tenant_id="tenant-a")
    principal = provider.verify_token(token)
    assert principal.tenant_id == "tenant-a"


def test_expired_token_is_rejected(provider, keys):
    private_pem, _, kid = keys
    now = int(time.time())
    token = mint_token(private_pem, kid, iat=now - 7200, nbf=now - 7200, exp=now - 60)
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_nbf_in_future_is_rejected(provider, keys):
    private_pem, _, kid = keys
    now = int(time.time())
    token = mint_token(private_pem, kid, iat=now, nbf=now + 3600, exp=now + 7200)
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_wrong_issuer_is_rejected(provider, keys):
    private_pem, _, kid = keys
    token = mint_token(private_pem, kid, issuer="evil")
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_wrong_audience_is_rejected(provider, keys):
    private_pem, _, kid = keys
    token = mint_token(private_pem, kid, audience="other-api")
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_alg_none_is_rejected(provider):
    with pytest.raises(InvalidTokenError):
        provider.verify_token(mint_none_alg())


def test_algorithm_confusion_is_rejected(keys, tmp_path):
    """A token signed with HS256 using the public key as secret must fail."""
    _, public_dir, kid = keys
    from cryptography.hazmat.primitives import serialization

    public_pem = (public_dir / f"{kid}.pem").read_bytes()
    public_key = serialization.load_pem_public_key(public_pem)
    raw_public = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    now = int(time.time())
    payload = {
        "iss": "jagent",
        "aud": "jagent-api",
        "sub": "attacker",
        "tenant_id": "tenant-a",
        "roles": ["admin"],
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "jti": "confusion",
    }
    token = jwt.encode(payload, raw_public, algorithm="HS256", headers={"kid": kid})
    provider = JwtAuthProvider(make_settings(public_dir))
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_unknown_kid_is_rejected(provider, keys):
    private_pem, _, _ = keys
    token = mint_token(private_pem, "unknown-kid")
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_missing_kid_is_rejected(provider, keys):
    private_pem, _, _ = keys
    token = mint_without_kid(private_pem)
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_bad_signature_is_rejected(provider, keys, tmp_path):
    other_private, _, _ = generate_rsa_keypair(tmp_path / "other")
    token = mint_token(other_private, DEFAULT_KID)
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


@pytest.mark.parametrize("claim", ["tenant_id", "sub", "jti", "iss", "aud", "exp", "iat"])
def test_missing_required_claim_is_rejected(provider, keys, claim):
    private_pem, _, kid = keys
    now = int(time.time())
    payload = {
        "iss": "jagent",
        "aud": "jagent-api",
        "sub": "user:alice",
        "tenant_id": "default",
        "roles": ["admin"],
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "jti": "jti",
    }
    payload.pop(claim)
    token = jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_unknown_role_is_rejected(provider, keys):
    private_pem, _, kid = keys
    token = mint_token(private_pem, kid, roles=["superadmin"])
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_overlong_ttl_is_rejected(keys):
    private_pem, public_dir, kid = keys
    provider = JwtAuthProvider(make_settings(public_dir, max_ttl_seconds=3600))
    token = mint_token(private_pem, kid, ttl=7200)
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


@pytest.mark.parametrize("tenant", ["", "../etc", "a/b", "a b", "x" * 65, "tenant\n", ".", "..", "acme."])
def test_malformed_tenant_is_rejected(provider, keys, tenant):
    private_pem, _, kid = keys
    token = mint_token(private_pem, kid, tenant_id=tenant)
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


def test_oversized_token_is_rejected(provider, keys):
    private_pem, _, kid = keys
    token = mint_token(private_pem, kid, padding="x" * 9000)
    with pytest.raises(InvalidTokenError):
        provider.verify_token(token)


async def test_authenticate_reads_bearer_header(provider, keys):
    private_pem, _, kid = keys
    token = mint_token(private_pem, kid, tenant_id="tenant-a")
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    principal = await provider.authenticate(scope)
    assert principal is not None
    assert principal.tenant_id == "tenant-a"


async def test_authenticate_reads_ws_subprotocol(provider, keys):
    private_pem, _, kid = keys
    token = mint_token(private_pem, kid, tenant_id="tenant-a")
    scope = {
        "type": "websocket",
        "headers": [(b"sec-websocket-protocol", f"jagent.jwt, jagent.token.{token}".encode())],
    }
    principal = await provider.authenticate(scope)
    assert principal is not None
    assert principal.tenant_id == "tenant-a"


async def test_authenticate_without_credentials_returns_none(provider):
    assert await provider.authenticate({"type": "http", "headers": []}) is None
