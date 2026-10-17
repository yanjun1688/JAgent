"""TDD: offline JWT issuing CLI (production bootstrap path)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.api.auth.jwt_provider import JwtAuthProvider
from tests.support.jwt import make_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "harness.api.auth.cli", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_init_creates_keypair_and_public_key_dir(tmp_path):
    result = _run("init", "--out", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "jwt_private.pem").is_file()
    public_keys = tmp_path / "jwt_public_keys"
    assert public_keys.is_dir()
    keys = list(public_keys.glob("*.pem"))
    assert len(keys) == 1
    assert keys[0].stem in result.stdout


def test_issued_token_verifies_with_public_key(tmp_path):
    init = _run("init", "--out", str(tmp_path))
    assert init.returncode == 0, init.stderr
    kid = next((tmp_path / "jwt_public_keys").glob("*.pem")).stem

    issued = _run(
        "issue",
        "--key",
        str(tmp_path / "jwt_private.pem"),
        "--kid",
        kid,
        "--sub",
        "ops:alice",
        "--tenant",
        "tenant-a",
        "--roles",
        "operator",
        "--ttl",
        "1h",
    )
    assert issued.returncode == 0, issued.stderr
    token = issued.stdout.strip()
    assert token.count(".") == 2

    provider = JwtAuthProvider(make_settings(tmp_path / "jwt_public_keys"))
    principal = provider.verify_token(token)
    assert principal.subject == "ops:alice"
    assert principal.tenant_id == "tenant-a"
    assert [role.value for role in principal.roles] == ["operator"]


def test_rotate_keeps_old_public_key(tmp_path):
    init = _run("init", "--out", str(tmp_path))
    assert init.returncode == 0, init.stderr
    before = {p.name for p in (tmp_path / "jwt_public_keys").glob("*.pem")}

    rotated = _run("rotate", "--out", str(tmp_path))
    assert rotated.returncode == 0, rotated.stderr
    after = {p.name for p in (tmp_path / "jwt_public_keys").glob("*.pem")}
    assert before.issubset(after)
    assert len(after) == len(before) + 1


@pytest.mark.parametrize(
    "args",
    [
        ("--roles", "superadmin"),
        ("--tenant", "../etc"),
        ("--tenant", "."),
        ("--tenant", ".."),
        ("--tenant", "acme."),
    ],
)
def test_issue_rejects_invalid_claims(tmp_path, args):
    init = _run("init", "--out", str(tmp_path))
    assert init.returncode == 0, init.stderr
    kid = next((tmp_path / "jwt_public_keys").glob("*.pem")).stem
    base = [
        "issue",
        "--key",
        str(tmp_path / "jwt_private.pem"),
        "--kid",
        kid,
        "--sub",
        "ops:alice",
        "--tenant",
        "tenant-a",
        "--roles",
        "operator",
    ]
    # Replace the value for whichever claim is being tested.
    key, value = args
    if key in base:
        idx = base.index(key)
        base[idx + 1] = value
    else:
        base.extend([key, value])
    result = _run(*base)
    assert result.returncode != 0


def test_issued_token_payload_has_required_claims(tmp_path):
    import jwt

    init = _run("init", "--out", str(tmp_path))
    assert init.returncode == 0, init.stderr
    kid = next((tmp_path / "jwt_public_keys").glob("*.pem")).stem
    issued = _run(
        "issue",
        "--key",
        str(tmp_path / "jwt_private.pem"),
        "--kid",
        kid,
        "--sub",
        "ops:alice",
        "--tenant",
        "tenant-a",
        "--roles",
        "viewer,operator",
        "--ttl",
        "12h",
    )
    assert issued.returncode == 0, issued.stderr
    claims = jwt.decode(issued.stdout.strip(), options={"verify_signature": False})
    assert claims["iss"] == "jagent"
    assert claims["aud"] == "jagent-api"
    assert claims["tenant_id"] == "tenant-a"
    assert set(claims["roles"]) == {"viewer", "operator"}
    assert claims["exp"] - claims["iat"] == 12 * 3600
    assert claims["jti"]
    assert json.dumps(claims)  # payload is JSON-serializable
