"""复合请求：一句话 → LLM 自选工具 → DAG → 执行 → 总结回复。

    pytest tests/e2e_live/test_compound_request.py -s

输入只有一句话（见 PROMPT）。期望链路：

    用户一句话
      → planner 先看工具目录（名字 + 描述）自己挑工具
      → 输出 steps DAG（含 then=advice）
      → executor 逐步执行（MCP 取行情/K线）
      → run 完成后，LLM 基于执行证据给出最终回复（建议）
      → 过合规闸门后交付

这是唯一一个「不拆场景」的测试：只发一次请求，看完整条链路和最终回答。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

from jagent.adapters.mcp import mcp_servers_from_env
from jagent.agent.llm_judge import LLMJudge
from jagent.agent.model import from_env
from jagent.api.__main__ import _load_dotenv
from jagent.contracts import Budget, EventType
from jagent.wiring import build_app
from tests.e2e_live.support import TracingModel

pytestmark = pytest.mark.llm

PROMPT = (
    "帮我查询今日{company}成交量与涨跌幅，并根据30日K线，"
    "为我评估现在{company}在技术指标上处于一个什么情况，最后得出投资建议"
)


def _log(message: str = "") -> None:
    print(message, file=sys.__stdout__, flush=True)


def test_compound_request_one_shot() -> None:
    env = _load_dotenv()
    inner = from_env(env)
    assert inner is not None, "没有可用的 LLM 配置（.env 的 LLM_*）"
    servers = mcp_servers_from_env(env)
    company = env.get("JAGENT_E2E_COMPANY", "兖矿能源")
    prompt = PROMPT.format(company=company)

    model = TracingModel(inner)
    root = Path(tempfile.mkdtemp(prefix="e2e_compound_"))
    # 接真实模型必须带 T2 judge（C1 / D-018 fail-closed），与 create_app 同一装配模式。
    app = build_app(
        root,
        model=model,
        judge=LLMJudge(model),
        mcp_servers=servers,
        budget=Budget(auto_approve_tools=("shell.exec",)),
    )
    started = time.perf_counter()
    try:
        _log("=" * 72)
        _log(f"[用户一句话] {prompt}")
        _log("")

        outcome = app.session.handle_turn(prompt)
        _log(f"[状态] {outcome.status}")

        # 按事件顺序把「LLM 选了什么工具、怎么执行」打出来
        for event in app.store.read():
            kind = event.type
            payload = event.payload
            if kind is EventType.INTENT_ROUTED:
                _log(f"  · 路由：{payload.get('type')}（置信度 {payload.get('confidence')}）")
            elif kind is EventType.PLAN_PROPOSED:
                plan = payload["plan"]
                _log(f"  · 计划：{len(plan['steps'])} 步" + (f"，then={plan['then']}" if plan.get("then") else ""))
                for step in plan["steps"]:
                    _log(f"      {step['id']}: {step['tool']}")
                    _log(f"          参数：{step.get('args')}")
            elif kind is EventType.STEP_FINISHED:
                error = payload.get("error_message") or ""
                _log(
                    f"  · 执行：{payload.get('step_id')} → {payload.get('status')}"
                    + (f"（{error[:80]}）" if error else "")
                )
            elif kind is EventType.ADVICE_DELIVERED:
                _log(f"  · 交付方式：{payload.get('mode')}")

        _log("")
        _log("[LLM 最终回复]")
        for line in (outcome.text or "").splitlines() or [""]:
            _log(f"  {line}")

        elapsed = time.perf_counter() - started
        _log("")
        _log(f"[耗时] {elapsed:.1f}s")

        # 断言：整条链路都通了才算过
        tools = [
            step["tool"]
            for event in app.store.read()
            if event.type is EventType.PLAN_PROPOSED
            for step in event.payload["plan"]["steps"]
        ]
        delivered = [
            event.payload for event in app.store.read() if event.type is EventType.ADVICE_DELIVERED
        ]

        assert outcome.status == "answered", f"status={outcome.status}"
        # 建议路径不返回 result，用事件确认 run 真的跑完了
        completed = [e for e in app.store.read() if e.type is EventType.RUN_COMPLETED]
        assert completed, "run 没有完成（缺 run.completed）"
        assert any(tool.startswith(("a_share.", "meta.")) for tool in tools), (
            f"没有调用 MCP 工具：{tools}"
        )
        assert delivered, "没有交付最终回复"
        assert delivered[-1].get("advice", {}).get("disclaimer"), "回复缺 disclaimer"
        assert (outcome.text or "").strip(), "最终回复为空"
    finally:
        app.close()
        shutil.rmtree(root, ignore_errors=True)
