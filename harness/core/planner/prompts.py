"""Prompt / message construction for the non-trusted Planner.

All functions here format text for the LLM; they perform no validation and no
enforcement. Tool descriptions, plan prompts, feedback sections and the
answer-phase user context are built here.
"""

from __future__ import annotations

import json

from harness.core.fold import RunState
from harness.core.logger import agent_logger, fmtkv
from harness.core.planner.schema_contract import build_step_schema_text
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.models.events import Episode
from harness.tools.registry import ToolRegistry

_log = agent_logger("planner")


def build_feedback_section(feedback: str | None) -> str:
    if not feedback:
        return ""
    _log.debug("[feedback] Built feedback section (%d chars)", len(feedback))
    return (
        f"\n## System Monitoring Feedback\n"
        f"{feedback}\n"
        f"Take this feedback into account when planning the next steps.\n"
    )


def build_tool_descriptions(registry: ToolRegistry) -> str:
    tool_defs = registry.list_tool_defs()
    lines = []
    for td in tool_defs:
        line = f"  - {td.name}: {td.description}"
        schema = td.input_schema
        if schema and isinstance(schema, dict):
            props = schema.get("properties", {})
            required = schema.get("required", [])
            if props:
                param_lines = []
                for pname, pinfo in props.items():
                    ptype = pinfo.get("type", "any")
                    req = "required" if pname in required else "optional"
                    parts = [f"      {pname} ({ptype}, {req})"]
                    enum = pinfo.get("enum")
                    if enum:
                        parts.append(f"allowed: {json.dumps(enum, ensure_ascii=False)}")
                    desc = pinfo.get("description", "")
                    if desc:
                        parts.append(f"— {desc}")
                    param_lines.append(" ".join(parts))
                if param_lines:
                    line += "\n    Parameters:"
                    line += "\n" + "\n".join(param_lines)
        if td.requires_confirmation:
            line += " (requires confirmation)"
        lines.append(line)
    return "\n".join(lines) if lines else "  (no tools available)"


def build_tool_descriptions_subset(registry: ToolRegistry, tool_names: list[str]) -> str:
    """Build tool descriptions restricted to ``tool_names`` (F-6 read-only whitelist)."""
    by_name = {td.name: td for td in registry.list_tool_defs()}
    lines = []
    for name in tool_names:
        td = by_name.get(name)
        if td is None:
            continue
        line = f"  - {td.name} (read-only): {td.description}"
        lines.append(line)
    return "\n".join(lines) if lines else "  (no read-only tools allowed — no repair possible)"


def build_plan_prompt(
    registry: ToolRegistry,
    intent: str,
    feedback: str | None = None,
    conversation_context: str = "",
) -> str:
    text = get_prompt(
        AgentPhase.PLAN,
        step_schema=build_step_schema_text(),
        tool_descriptions=build_tool_descriptions(registry),
        intent=intent,
    )
    fb = build_feedback_section(feedback)
    if conversation_context:
        # Conversation history is injected once for initial planning only.
        # It never contaminates the event-sourced current request or revise prompts.
        context = conversation_context[:4000]
        text = text.replace(
            "## User Intent\n",
            "## Conversation Context (reference only)\n" + context + "\n\n## User Intent\n",
        )
    if fb:
        text = text.replace("## User Intent\n", fb + "## User Intent\n")
    return text


def build_answer_user_content(intent: str, state: RunState, conversation_context: str = "") -> str:
    """Build the answer-phase user message content from the folded run state."""
    parts = []

    # v2.2+ (JAGENT-2026-P1-13 Bug 4): 无工具执行时必须给 Answer 权威信号，
    # 防止模型自由发挥（如 325b42c5 对路径测试生成 CTF/Docker 泛化说明）。
    if len(state.tool_results) == 0:
        parts.append(
            "[NO TOOLS EXECUTED] — no external tool was run for this task. "
            "Answer only from existing knowledge. Do not describe or imply any "
            "file operation, HTTP fetch, browser visit, or other external action."
        )

    if state.latest_plan:
        lp = state.latest_plan
        reason = lp.get("revision_reason")
        remaining = lp.get("remaining_steps_summary")
        pstatus = lp.get("status")
        if reason or remaining or pstatus:
            parts.append(
                "[Run outcome — AUTHORITATIVE, from the event store. "
                "The revision result below is exactly what the system decided; "
                "do not describe the revision differently.]"
            )
            if reason:
                parts.append(f"Last revision reason: {reason}")
            if remaining:
                parts.append(f"Last revision result: {remaining}")
            if pstatus:
                parts.append(f"Plan final status: {pstatus}")
            parts.append("")

    if state.tool_results:
        tool_inputs = {tc.tool_call_id: tc.input for tc in state.tool_calls}
        parts.append(
            "[Tool execution results — AUTHORITATIVE, exhaustive record "
            "of every tool call that actually ran in this task. "
            "Do NOT add or imply any execution not listed here.]"
        )
        parts.append("[Execution digest]")
        for i, tr in enumerate(state.tool_results):
            status_label = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
            parts.append(f"Step {i + 1}: {tr.tool_name} → {status_label}")
        parts.append("[Detailed results]")
        for i, tr in enumerate(state.tool_results):
            status_label = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
            parts.append(f"## Step {i + 1}: {tr.tool_name} (status: {status_label})")
            tc_input = tool_inputs.get(tr.tool_call_id)
            if tc_input:
                input_str = str(tc_input)
                if len(input_str) > 2000:
                    input_str = input_str[:2000] + "\n...(input truncated)..."
                parts.append(f"Input: {input_str}")
            if tr.output is not None:
                output_str = str(tr.output)
                if len(output_str) > 5000:
                    output_str = output_str[:5000] + "\n...(truncated)..."
                parts.append(f"Output: {output_str}")
            if tr.error:
                parts.append(f"Error: {tr.error}")
            if tr.duration_ms:
                parts.append(f"Duration: {tr.duration_ms}ms")
            parts.append("")

    if state.summary:
        if isinstance(state.summary, Episode):
            summary_text = state.summary.to_context_text()
            if summary_text:
                parts.append("## Previous Context (Compressed)")
                parts.append(summary_text)

    if state.feedbacks:
        fb_ids = ",".join(getattr(fb, "feedback_id", "?")[:8] for fb in state.feedbacks)
        _log.info("[answer] Including %d feedbacks %s", len(state.feedbacks), fmtkv(feedback_ids=fb_ids))
        parts.append("[Feedback]")
        for fb in state.feedbacks:
            parts.append(fb.feedback_text)
        parts.append("")

    user_content = "User's request:\n" + intent
    if conversation_context:
        user_content += "\n\nConversation context (reference only):\n" + conversation_context[:4000]
    if parts:
        user_content += "\n\n" + "\n".join(parts)
    return user_content
