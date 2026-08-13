"""Generate OpenAPI schema and TypeScript types for the frontend.

Usage:
    python scripts/generate_openapi.py

This generates:
    frontend/public/openapi.json   — OpenAPI 3.0 schema
    frontend/src/api/schema.ts     — TypeScript interfaces extracted from schema
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_PUBLIC = PROJECT_ROOT / "frontend" / "public"
FRONTEND_SRC_API = PROJECT_ROOT / "frontend" / "src" / "api"

# Build schema from the FastAPI app without running a server
from harness.api.app import app  # noqa: E402

schema = app.openapi()
FRONTEND_PUBLIC.mkdir(parents=True, exist_ok=True)
schema_path = FRONTEND_PUBLIC / "openapi.json"
schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"[OK] OpenAPI schema written to {schema_path}")


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


components = schema.get("components", {})
ts_source = _generate_interfaces(components)
schema_ts_path = FRONTEND_SRC_API / "schema.ts"
schema_ts_path.write_text(ts_source, encoding="utf-8")
print(f"[OK] TypeScript interfaces written to {schema_ts_path}")


# ── Optionally generate with openapi-typescript for richer types ──

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
