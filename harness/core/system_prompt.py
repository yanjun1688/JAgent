"""System prompt builder (L4) — injects role, tool definitions, and behavior constraints."""

from harness.models.tools import ToolDefinition

_SYSTEM_PROMPT_TEMPLATE = """You are an autonomous agent executing a task.

## Your Task
{intent}

## Available Tools
{tool_list}

## Instructions
1. Think step by step about what to do next.
2. Choose ONE tool to call, or output <STOP> if the task is complete.
3. Output your response in this format:

THOUGHT: <your reasoning>
TOOL: <tool_name>
ARGS: <JSON arguments>

Or if finished:
THOUGHT: <summary of what was accomplished>
<STOP>

## Rules
- Use tools to interact with the world. Do not simulate results.
- Do not repeat failed tool calls without adjusting your approach.
- If you encounter an error you cannot recover from, output <STOP> with a failure reason.
"""


def build_system_prompt(intent: str, tool_defs: list[ToolDefinition]) -> str:
    tool_list = "\n".join(
        f"- **{td.name}**: {td.description}"
        + (" (dangerous — requires confirmation)" if td.requires_confirmation else "")
        for td in tool_defs
    )
    if not tool_list:
        tool_list = "(no tools available)"
    return _SYSTEM_PROMPT_TEMPLATE.format(intent=intent, tool_list=tool_list)


def build_tool_schemas(tool_defs: list[ToolDefinition]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": td.name,
                "description": td.description,
                "parameters": td.input_schema,
            },
        }
        for td in tool_defs
    ]
