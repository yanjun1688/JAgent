"""Field-role single-source ref contract (root-cause A fix).

``ref`` policy is no longer a hand-maintained parallel list: a field's data-flow
role is declared ONCE in ``input_schema`` (``x-role``), default AUTHORITY
(fail-closed), and everything else is derived — guardrail enforcement (see
``test_output_ref_validation``), planner-facing capability text, and provider
schema sanitization.
"""

from __future__ import annotations

from harness.core.planner.prompts import build_tool_descriptions
from harness.core.system_prompt import build_tool_schemas
from harness.models.tools import FieldRole, ToolDefinition, field_role
from harness.tools.file_op import FileOpTool
from harness.tools.fetch_output import FetchOutputTool
from harness.tools.http_request import HTTP_REQUEST_DEF
from harness.tools.mcp_call import MCP_CALL_DEF
from harness.tools.registry import ToolRegistry

FILE_OP_DEF = FileOpTool().to_definition()


# ── field_role derivation & fail-closed default ──────────────────

def test_missing_role_defaults_authority():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert field_role(schema, "x") is FieldRole.AUTHORITY


def test_role_data_derived():
    schema = {"properties": {"content": {"type": "string", "x-role": "data"}}}
    assert field_role(schema, "content") is FieldRole.DATA


def test_unknown_field_defaults_authority():
    assert field_role({"properties": {}}, "nope") is FieldRole.AUTHORITY


def test_field_role_handles_non_dict_schema():
    assert field_role(None, "x") is FieldRole.AUTHORITY
    assert field_role({"properties": "bad"}, "x") is FieldRole.AUTHORITY


# ── tool-level derivation ────────────────────────────────────────

def test_file_op_content_data_path_authority():
    assert FILE_OP_DEF.ref_allowed("content") is True
    assert FILE_OP_DEF.ref_allowed("path") is False


def test_http_body_data_url_authority():
    assert HTTP_REQUEST_DEF.ref_allowed("body") is True
    assert HTTP_REQUEST_DEF.ref_allowed("headers") is True
    assert HTTP_REQUEST_DEF.ref_allowed("url") is False


def test_mcp_arguments_data_tool_name_authority():
    assert MCP_CALL_DEF.ref_allowed("arguments") is True
    assert MCP_CALL_DEF.ref_allowed("tool_name") is False


def test_fetch_output_ref_authority():
    assert FetchOutputTool().to_definition().ref_allowed("ref") is False


# ── planner-facing projection ────────────────────────────────────

def test_tool_description_projects_ref_capability():
    r = ToolRegistry()
    r._register(FILE_OP_DEF, lambda x: {})
    desc = build_tool_descriptions(r)
    content_line = next(line for line in desc.splitlines() if line.strip().startswith("content "))
    path_line = next(line for line in desc.splitlines() if line.strip().startswith("path "))
    assert "[ref: allowed]" in content_line
    assert "[ref: forbidden]" in path_line


# ── provider schema sanitization ─────────────────────────────────

def test_build_tool_schemas_strips_vendor_keys():
    td = ToolDefinition(
        name="t",
        description="d",
        input_schema={
            "type": "object",
            "properties": {"content": {"type": "string", "x-role": "data"}},
        },
        side_effects=[],
    )
    params = build_tool_schemas([td])[0]["function"]["parameters"]
    assert "x-role" not in params["properties"]["content"]


def test_build_tool_schemas_strips_nested_vendor_keys():
    td = ToolDefinition(
        name="t2",
        description="d",
        input_schema={
            "type": "object",
            "properties": {
                "body": {
                    "type": "object",
                    "x-role": "data",
                    "properties": {"nested": {"type": "string", "x-role": "data"}},
                }
            },
        },
        side_effects=[],
    )
    params = build_tool_schemas([td])[0]["function"]["parameters"]
    body = params["properties"]["body"]
    assert "x-role" not in body
    assert "x-role" not in body["properties"]["nested"]
