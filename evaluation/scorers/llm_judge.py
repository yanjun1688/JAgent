"""LLM-as-Judge scorers — qualitative output scoring via the project LLM client.

Only invoked when a real LLM client is available (LLM_API_KEY set). Scoring is a
non-trusted concern: a judge failure degrades gracefully to ``None`` so the
evaluation pipeline never crashes on a missing/erroring LLM.
"""

from __future__ import annotations

import json

from evaluation.datasets.base import EvalCase
from harness.core.fold import RunState
from harness.core.llm_client import LLMClient

_JUDGE_PROMPT = """你是 Agent 输出质量评审专家。
用户意图: {intent}
Agent 最终输出: {output}

请从以下维度 1-5 打分:
1. 是否完整回答用户意图 (completeness)
2. 信息准确性 (accuracy)
3. 输出格式是否清晰 (formatting)

只返回 JSON，格式: {{"completeness": N, "accuracy": N, "formatting": N}}
"""


class LLMJudgeScorer:
    """Score final output quality using an independent LLM judge call."""

    def __init__(self, llm_client: LLMClient | None, model: str | None = None):
        self.llm = llm_client
        self.model = model

    async def score(self, case: EvalCase, state: RunState) -> dict | None:
        """Return a dict of judge scores, or None when the LLM is unavailable."""
        if self.llm is None:
            return None
        output = self._extract_output(state)
        prompt = _JUDGE_PROMPT.format(intent=case.intent, output=output[:4000])
        try:
            resp = await self.llm.chat(
                [{"role": "system", "content": prompt}],
                temperature=0.0,
                max_tokens=200,
            )
        except Exception:
            return None
        data = self._parse(resp.content)
        if not data:
            return None
        return {
            "task_correctness": data.get("completeness"),
            "output_quality": round(
                sum(data.get(k, 0) for k in ("completeness", "accuracy", "formatting")) / 3, 3
            ),
            "_judge_raw": data,
        }

    @staticmethod
    def _extract_output(state: RunState) -> str:
        """Prefer the run summary; fall back to the latest thought."""
        summary = getattr(state, "summary", None)
        if summary:
            return str(summary)
        thought = getattr(state, "latest_thought", None)
        if thought is not None:
            return str(getattr(thought, "thought", ""))
        return ""

    @staticmethod
    def _parse(content: str) -> dict | None:
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            content = content.rsplit("```", 1)[0]
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        out = {}
        for k in ("completeness", "accuracy", "formatting"):
            try:
                out[k] = float(data.get(k))
            except (TypeError, ValueError):
                out[k] = 0.0
        return out
