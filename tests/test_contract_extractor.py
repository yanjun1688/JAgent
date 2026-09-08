"""③ ContractExtractor.extract 重试语义对齐 Planner（解析失败/全丢重试并反馈）。

Review 指认：extract 只在 LLM 调用异常时重试，_parse 拿到空/坏 JSON 直接返回，
max_retries 对"格式错误"场景无效。期望语义（对齐 Planner.plan/revise）：
- JSON 解析失败或列表内全部被结构校验丢弃 → 把原因反馈给模型重试；
- `{"required_operations": []}`（无硬性交付）→ 合法空，立即返回不重试；
- 部分有效 → 直接返回有效子集；
- 预算用尽 → 返回 []（D-04 兜底），不抛异常。
"""

import json

import pytest

from harness import MockLLMClient
from harness.core.contract_extractor import ContractExtractor
from harness.models.tools import ToolDefinition
from harness.tools.registry import ToolRegistry


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r._register(
        ToolDefinition(
            name="file_op",
            description="file operations",
            input_schema={
                "type": "object",
                "properties": {"operation": {"type": "string"}, "path": {"type": "string"}},
                "required": ["operation", "path"],
            },
            idempotency_key_fields=["operation", "path"],
            side_effects=[],
        ),
        lambda i: i,
    )
    return r


def _valid_ops_json(n: int = 1) -> str:
    ops = [
        {"tool": "file_op", "input": {"operation": "read", "path": f"/tmp/f{i}.txt"}}
        for i in range(n)
    ]
    return json.dumps({"required_operations": ops})


def _extractor(llm: MockLLMClient, max_retries: int = 1) -> ContractExtractor:
    return ContractExtractor(llm, _registry(), max_retries=max_retries)


class TestExtractRetryOnParseFailure:
    @pytest.mark.asyncio
    async def test_parse_failure_is_retried_with_feedback(self):
        """格式错误(非 JSON) → 反馈给模型重试；第二次成功返回契约。"""
        llm = MockLLMClient(["this is not json at all", _valid_ops_json()])
        ex = _extractor(llm)
        contracts = await ex.extract("read /tmp/f1.txt")
        assert len(contracts) == 1
        assert contracts[0].tool == "file_op"
        assert len(llm.calls) == 2
        second_user_msgs = [m["content"] for m in llm.calls[1]["messages"] if m["role"] == "user"]
        assert second_user_msgs, "第 2 次调用必须带上一次失败反馈(user 消息)"
        assert "format" in second_user_msgs[0].lower() or "invalid" in second_user_msgs[0].lower()

    @pytest.mark.asyncio
    async def test_parse_failure_all_attempts_return_empty_not_raise(self):
        """预算用尽仍格式错误 → []（D-04），不抛异常。"""
        llm = MockLLMClient(["not json", "still not json"])
        ex = _extractor(llm)
        contracts = await ex.extract("read /tmp/f1.txt")
        assert contracts == []
        assert len(llm.calls) == 2  # max_retries=1 → 2 次尝试，不是一次就放弃


class TestExtractLegitEmptyNotRetried:
    @pytest.mark.asyncio
    async def test_empty_required_operations_returns_immediately(self):
        """合法空（无硬性交付）→ 立即返回 []，不浪费重试。"""
        llm = MockLLMClient([json.dumps({"required_operations": []})])
        ex = _extractor(llm)
        contracts = await ex.extract("just chat, no file ops")
        assert contracts == []
        assert len(llm.calls) == 1


class TestExtractDropAllRetried:
    @pytest.mark.asyncio
    async def test_all_items_dropped_is_retried_with_reason(self):
        """操作列表非空但全部被结构校验丢弃（如未知工具名）→ 反馈原因重试。"""
        llm = MockLLMClient(
            [
                json.dumps(
                    {"required_operations": [{"tool": "bogus_tool", "input": {"x": 1}}]}
                ),
                _valid_ops_json(),
            ]
        )
        ex = _extractor(llm)
        contracts = await ex.extract("read /tmp/f1.txt")
        assert len(contracts) == 1
        assert len(llm.calls) == 2
        second_user_msgs = [m["content"] for m in llm.calls[1]["messages"] if m["role"] == "user"]
        assert second_user_msgs
        assert "bogus_tool" in second_user_msgs[0]

    @pytest.mark.asyncio
    async def test_drop_all_exhausts_budget_returns_empty(self):
        llm = MockLLMClient(
            [
                json.dumps({"required_operations": [{"tool": "bogus_tool", "input": {}}]}),
                json.dumps({"required_operations": [{"tool": "also_bogus", "input": {}}]}),
            ]
        )
        ex = _extractor(llm)
        contracts = await ex.extract("read /tmp/f1.txt")
        assert contracts == []
        assert len(llm.calls) == 2


class TestExtractPartialValid:
    @pytest.mark.asyncio
    async def test_partial_valid_returns_valid_subset_without_retry(self):
        """部分有效 → 返回有效子集，不因个别无效项重试。"""
        llm = MockLLMClient(
            [
                json.dumps(
                    {
                        "required_operations": [
                            {"tool": "file_op", "input": {"operation": "read", "path": "/tmp/ok.txt"}},
                            {"tool": "bogus_tool", "input": {"x": 1}},
                        ]
                    }
                )
            ]
        )
        ex = _extractor(llm)
        contracts = await ex.extract("read /tmp/ok.txt")
        assert len(contracts) == 1
        assert contracts[0].input["path"] == "/tmp/ok.txt"
        assert len(llm.calls) == 1
