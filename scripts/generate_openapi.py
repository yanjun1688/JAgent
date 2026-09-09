"""Generate OpenAPI schema and TypeScript types for the frontend.

Usage:
    python scripts/generate_openapi.py            # (re)write generated artifacts
    python scripts/generate_openapi.py --check    # CI/pre-commit gate: fail if the
                                                  # checked-in artifacts are stale

This generates:
    frontend/public/openapi.json   — OpenAPI 3.0 schema
    frontend/src/api/schema.ts     — TypeScript interfaces extracted from schema

Artifacts are always written with LF line endings so regeneration is
byte-identical across platforms (Windows text-mode translation previously
produced CRLF and made every regeneration a no-content diff).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_PUBLIC = PROJECT_ROOT / "frontend" / "public"
FRONTEND_SRC_API = PROJECT_ROOT / "frontend" / "src" / "api"

# Build schema from the FastAPI app without running a server
from harness.api.app import app  # noqa: E402


def _write_lf(path: Path, text: str) -> None:
    """Write text with normalized LF endings, independent of host OS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ── Generate TypeScript interfaces from OpenAPI components.schemas ──


def _to_ts_type(s: str) -> str:
    """Map OpenAPI format/type to TypeScript."""
    if s in {"integer", "int32", "int64", "number"}:
        return "number"
    if s == "boolean":
        return "boolean"
    if s == "string":
        return "string"
    return s


def _render_prop(name: str, prop: dict, required: bool) -> str:
    variants = prop.get("anyOf") or prop.get("oneOf")
    if variants:
        non_null = [variant for variant in variants if variant.get("type") != "null"]
        if len(non_null) == 1:
            prop = non_null[0]
    ref = prop.get("$ref", "")
    if ref:
        ts_type = ref.rsplit("/", 1)[-1]
    elif prop.get("type") == "array":
        items = prop.get("items", {})
        item_ref = items.get("$ref", "")
        if item_ref:
            item_type = item_ref.rsplit("/", 1)[-1]
        else:
            item_type = _to_ts_type(items.get("type", "any"))
        ts_type = f"{item_type}[]"
    elif prop.get("type") == "object":
        ts_type = "Record<string, unknown>"
    else:
        ts_type = _to_ts_type(prop.get("type", "unknown"))
    suffix = "" if required else "?"
    return f"  {name}{suffix}: {ts_type}"


def _generate_interfaces(components: dict) -> str:
    schemas = (components or {}).get("schemas", {})
    lines = [
        "// Auto-generated from OpenAPI schema. Run `npm run generate-api` to refresh.",
        "// eslint-disable-next-line @typescript-eslint/no-unused-vars",
        "",
    ]
    for name, definition in schemas.items():
        if definition.get("enum"):
            values = " | ".join(json.dumps(value) for value in definition["enum"])
            lines.append(f"export type {name} = {values}")
            lines.append("")
            continue
        if definition.get("type") == "object":
            props = definition.get("properties", {})
            required_set = set(definition.get("required", []))
            lines.append(f"export interface {name} {{")
            for pname, pdef in props.items():
                # Fields carrying a Pydantic default are always present in
                # serialized responses, so keep them non-optional in TS to
                # avoid "possibly undefined" errors in consumers (M5).
                is_nullable = bool(pdef.get("anyOf") or pdef.get("oneOf"))
                is_required = pname in required_set or ("default" in pdef and not is_nullable)
                lines.append(_render_prop(pname, pdef, is_required))
            lines.append("}")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if checked-in artifacts are stale",
    )
    args = parser.parse_args()

    schema = app.openapi()
    schema_path = FRONTEND_PUBLIC / "openapi.json"
    schema_text = json.dumps(schema, indent=2, ensure_ascii=False)

    components = schema.get("components", {})
    ts_text = _generate_interfaces(components)
    schema_ts_path = FRONTEND_SRC_API / "schema.ts"

    artifacts = [(schema_path, schema_text), (schema_ts_path, ts_text)]

    if args.check:
        # Normalize CRLF so a Windows checkout with core.autocrlf converting the
        # working tree does not produce a false positive; generation still writes LF.
        stale = [
            str(p)
            for p, text in artifacts
            if (not p.exists()) or p.read_bytes().replace(b"\r\n", b"\n") != text.encode("utf-8")
        ]
        if stale:
            for p in stale:
                print(f"[STALE] {p} — run `python scripts/generate_openapi.py` and commit the result")
            return 1
        print("[OK] OpenAPI artifacts are up to date")
        return 0

    for path, text in artifacts:
        _write_lf(path, text)
        print(f"[OK] written to {path}")

    # ── Optionally generate with openapi-typescript for richer types ──
    # Best-effort only: requires network/npx and the output is not checked in,
    # so it is intentionally skipped in --check mode.
    try:
        result = subprocess.run(
            [
                "npx",
                "--yes",
                "openapi-typescript",
                str(schema_path),
                "--output",
                str(FRONTEND_SRC_API / "schema.openapi.ts"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"[OK] openapi-typescript output at {FRONTEND_SRC_API / 'schema.openapi.ts'}")
        else:
            print(f"[WARN] openapi-typescript skipped: {result.stderr.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[WARN] openapi-typescript skipped: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
