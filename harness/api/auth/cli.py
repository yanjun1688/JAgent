"""Offline JWT issuing CLI.

JAgent is a resource server: it only verifies tokens. This CLI is the
bootstrap issuer used while the platform has no user directory. The private
key stays on the operator's machine; only public keys are mounted into the
API container.

Examples:
    python -m harness.api.auth.cli init --out ./secrets
    python -m harness.api.auth.cli issue --key ./secrets/jwt_private.pem \
        --kid <kid> --sub ops:alice --tenant default --roles admin --ttl 12h
    python -m harness.api.auth.cli rotate --out ./secrets
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from harness.core.tenant import TENANT_ID_RE
from harness.models.principal import Role


def _parse_ttl(value: str) -> int:
    raw = value.strip().lower()
    if not raw:
        raise ValueError("ttl must not be empty")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if raw[-1] in multipliers:
        amount = int(raw[:-1])
        return amount * multipliers[raw[-1]]
    return int(raw)


def _validate_tenant(value: str) -> str:
    if not TENANT_ID_RE.fullmatch(value):
        raise ValueError(
            "tenant must be 1-64 chars, start/end alphanumeric, "
            "and only contain [A-Za-z0-9_.-]"
        )
    return value


def _validate_roles(value: str) -> list[str]:
    roles = [item.strip() for item in value.split(",") if item.strip()]
    if not roles:
        raise ValueError("at least one role is required")
    for role in roles:
        Role(role)  # raises ValueError for unknown roles
    return roles


def _generate_keypair(algorithm: str):
    if algorithm == "RS256":
        return rsa.generate_private_key(public_exponent=65537, key_size=3072)
    if algorithm == "ES256":
        return ec.generate_private_key(ec.SECP256R1())
    raise ValueError("algorithm must be RS256 or ES256")


def _kid_for(public_key) -> str:
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:16]


def _write_keypair(private_key, out: Path, *, private_name: str) -> tuple[str, Path, Path]:
    public_key = private_key.public_key()
    kid = _kid_for(public_key)
    out.mkdir(parents=True, exist_ok=True)
    public_dir = out / "jwt_public_keys"
    public_dir.mkdir(parents=True, exist_ok=True)

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_path = out / private_name
    private_path.write_bytes(private_pem)
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        pass
    public_path = public_dir / f"{kid}.pem"
    public_path.write_bytes(public_pem)
    return kid, private_path, public_path


def _cmd_init(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    algorithm = args.alg
    private_key = _generate_keypair(algorithm)
    kid, private_path, public_path = _write_keypair(
        private_key, out, private_name="jwt_private.pem"
    )
    print(f"kid={kid}")
    print(f"private_key={private_path}")
    print(f"public_key={public_path}")
    print(f"algorithm={algorithm}")
    return 0


def _cmd_issue(args: argparse.Namespace) -> int:
    key_path = Path(args.key).expanduser()
    if not key_path.is_file():
        raise ValueError(f"private key not found: {key_path}")
    private_pem = key_path.read_bytes()
    roles = _validate_roles(args.roles)
    tenant = _validate_tenant(args.tenant)
    ttl = _parse_ttl(args.ttl)
    now = int(time.time())
    payload = {
        "iss": args.iss,
        "aud": args.aud,
        "sub": args.sub,
        "tenant_id": tenant,
        "roles": roles,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": args.jti or uuid.uuid4().hex,
    }
    token = jwt.encode(
        payload,
        private_pem,
        algorithm=args.alg,
        headers={"kid": args.kid},
    )
    print(token)
    return 0


def _cmd_rotate(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    private_key = _generate_keypair(args.alg)
    kid, private_path, public_path = _write_keypair(
        private_key, out, private_name=f"jwt_private_{uuid.uuid4().hex[:8]}.pem"
    )
    print(f"kid={kid}")
    print(f"private_key={private_path}")
    print(f"public_key={public_path}")
    print("Old public keys were kept; remove them after their tokens expire.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jagent-auth", description="Offline JWT issuing CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="generate a signing keypair")
    init.add_argument("--out", required=True, help="output directory")
    init.add_argument("--alg", default="RS256", choices=["RS256", "ES256"])
    init.set_defaults(func=_cmd_init)

    issue = sub.add_parser("issue", help="issue a JWT")
    issue.add_argument("--key", required=True, help="private key PEM")
    issue.add_argument("--kid", required=True, help="key id matching the public key filename")
    issue.add_argument("--sub", required=True, help="subject")
    issue.add_argument("--tenant", required=True, help="tenant id")
    issue.add_argument("--roles", required=True, help="comma-separated roles")
    issue.add_argument("--ttl", default="12h", help="token lifetime (e.g. 12h, 30m, 3600)")
    issue.add_argument("--iss", default="jagent")
    issue.add_argument("--aud", default="jagent-api")
    issue.add_argument("--alg", default="RS256", choices=["RS256", "ES256"])
    issue.add_argument("--jti", default=None)
    issue.set_defaults(func=_cmd_issue)

    rotate = sub.add_parser("rotate", help="generate a new keypair, keeping old public keys")
    rotate.add_argument("--out", required=True, help="output directory")
    rotate.add_argument("--alg", default="RS256", choices=["RS256", "ES256"])
    rotate.set_defaults(func=_cmd_rotate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
