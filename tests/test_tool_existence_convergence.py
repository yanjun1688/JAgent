"""R7 regression: tool-existence judgment converges on a single primitive.

Every trusted seam that answers "is this tool registered?" must share
``ToolRegistry.tool_def_or_error`` / ``unknown_tool_message`` (zero-dep source)
as the single bottom layer, and no seam may hand-write its own "unknown tool"
string. The registry-less serial path is the documented exception (list lookup,
fragment still from the shared source).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from harness import MockLLMClient
from harness.core.contract_extractor import ContractExtractor
from harness.core.planner.revision_invariants import step_is_mutating
from harness.models.plan import DagStep
from harness.models.tools import ToolDefinition, unknown_tool_message
from harness.tools.registry import ToolRegistry


def _registry_with_reader() -> ToolRegistry:
    reg = ToolRegistry()
    reg._register(
        ToolDefinition(name="reader", description="", side_effects=[]),
        lambda i: i,
    )
    return reg


class TestCallSitesUseCanonicalFragment:
    def test_contract_extractor_drops_unknown_tool_with_canonical_fragment(self):
        ex = ContractExtractor(MockLLMClient([]), ToolRegistry(), max_retries=1)
        contract, reason = ex._validate({"tool": "ghost_tool", "input": {"operation": "read"}})
        assert contract is None
        assert reason == unknown_tool_message("ghost_tool")

    def test_contract_extractor_accepts_registered_tool(self):
        ex = ContractExtractor(MockLLMClient([]), _registry_with_reader(), max_retries=1)
        contract, reason = ex._validate({"tool": "reader", "input": {"operation": "read"}})
        assert contract is not None
        assert reason == ""

    def test_step_is_mutating_unknown_tool_fails_closed(self):
        # No provable side effects ⇒ treated as mutating (Q-06 fail-closed).
        assert step_is_mutating(DagStep(id="s1", tool="ghost_tool", input={}), ToolRegistry()) is True

    def test_step_is_mutating_registered_read_only_tool(self):
        assert step_is_mutating(DagStep(id="s1", tool="reader", input={}), _registry_with_reader()) is False


class TestNoHandWrittenUnknownToolLiterals:
    """Source guard: the canonical fragment lives only in the zero-dep source
    (``models/tools.py``) and the distinct MCP-server concept (``tools/mcp_call.py``).
    Uses AST string constants so comments/docstrings' prose is judged by content
    while the old ``"Unknown tool: '%s'"`` literal would be caught if reintroduced.
    """

    _ALLOWED = {"models/tools.py", "tools/mcp_call.py"}
    # Quote or colon right after "tool" = a message fragment; a space = prose.
    _PATTERN = re.compile(r"unknown tool[':]", re.IGNORECASE)

    def test_no_hand_written_unknown_tool_message_in_harness(self):
        root = Path(__file__).resolve().parents[1] / "harness"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel in self._ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if self._PATTERN.search(node.value):
                        offenders.append(f"{rel}:{node.lineno}: {node.value!r}")
        assert offenders == [], "hand-written unknown-tool message(s): " + "; ".join(offenders)
