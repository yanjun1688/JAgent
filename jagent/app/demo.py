"""JAgent v8 · MVP 演示。

跑三个场景，证明 MVP「能跑起来」：
1. 确定性两步任务：找测试目录 → 跑回归（断言：exit_code == 0）
2. 断言失败：查一个不存在的端点，失败被分类、修复不了就失败退出（不谎报完成）
3. 建议路径：澄清槽位 → 建议 → 合规校验 → 被拦就退回框架建议

另外做一次「重放一致」断言：关掉模型（本来就没用），重放两次得到同一指纹。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..contracts import Advice, Budget
from ..wiring import build_app


class NaiveProposer:
    """故意写差的建议：有指令、有编造数字、没有风险披露。
    用来证明合规校验会拦，且拦不住时退到兜底模板。"""

    def propose(self, *, slots, evidence=(), framework_only=False, violations=()):
        return Advice(
            body="建议买入，2024年涨幅可达8.5%，全仓进场。",
            info=(), risks=(), framework=(), sources=(), disclaimer="",
        )

ANSWER_POOL: dict[str, str] = {
    "repo_path": ".",
    "target": "tests",
    "ticker": "示例标的",
    "horizon": "1年",
    "risk_profile": "均衡",
    "capital": "10万以内",
}


def _answer_for(question) -> dict[str, str]:
    if question is None:
        return {}
    name = question.slot
    if name in ANSWER_POOL:
        return {name: ANSWER_POOL[name]}
    if question.options:
        return {name: question.options[0]}
    return {name: "就按默认"}


def drive(app, text: str, *, max_turns: int = 6):
    """模拟用户：遇到问题就用 ANSWER_POOL 回答；返回最后一次 outcome 与问答记录。"""
    answers: dict[str, str] | None = None
    asked: list[str] = []
    outcome = None
    for _ in range(max_turns):
        outcome = app.session.handle_turn(text, answers)
        if outcome.status != "needs_input":
            return outcome, asked
        asked.append(f"[{outcome.question.slot}] {outcome.question.text}")
        answers = _answer_for(outcome.question)
    return outcome, asked


def main() -> int:
    root = Path("data/v8_demo")
    shutil.rmtree(root, ignore_errors=True)
    app = build_app(root, budget=Budget(auto_approve_tools=("shell.exec",)))

    ws = app.root / "ws"
    tests = ws / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )

    print("=" * 72)
    print("场景 1：确定性任务（找目录 → 跑回归）")
    out, asked = drive(app, "找一下 tests 然后跑回归测试")
    for line in asked:
        print("  问：", line)
    print("  状态：", out.status)
    print("  结果：", out.result.summary if out.result else "(无)")
    if out.result:
        for step in out.result.steps:
            print(f"    - {step.id}: {step.status} checks={[c.get('passed') for c in step.checks]}")

    print("=" * 72)
    print("场景 2：断言失败（端点不存在）")
    out2, asked2 = drive(app, "这个端点返回 code 200 吗 http://127.0.0.1:1/nope")
    print("  状态：", out2.status)
    print("  结果：", out2.result.summary if out2.result else "(无)")
    print("  剩余：", list(out2.result.remaining_plan) if out2.result else [])

    print("=" * 72)
    print("场景 3：模糊意图（建议路径 + 合规校验）")
    out3, asked3 = drive(app, "这支股票值得买吗")
    for line in asked3:
        print("  问：", line)
    print("  状态：", out3.status)
    print("  正文：")
    for line in out3.text.splitlines():
        print("    " + line)

    print("=" * 72)
    print("场景 4：不合规的建议（指令性 + 无来源 + 无风险）→ 打回 3 次 → 只输出兜底建议")
    bad = build_app(root / "bad", proposer=NaiveProposer(), budget=Budget(auto_approve_tools=("shell.exec",)))
    out4, asked4 = drive(bad, "这支股票值得买吗")
    print("  问的次数：", len(asked4))
    print("  模式：", [e.payload.get("mode") for e in bad.store.read() if e.type.value == "advice.delivered"])
    print("  正文前 3 行：")
    for line in out4.text.splitlines()[:3]:
        print("    " + line)
    bad.close()

    print("=" * 72)
    print("重放一致性")
    app.reload()
    fp1 = app.world.fingerprint()
    app.reload()
    fp2 = app.world.fingerprint()
    print("  两次重放指纹一致：", fp1 == fp2)
    print("  事件总数：", app.store.max_seq())

    # 建议路径被拦时走了兜底吗？
    delivered = [
        e for e in app.store.read() if e.type.value == "advice.delivered"
    ]
    print("  建议投递次数：", len(delivered), "模式：", [e.payload.get("mode") for e in delivered])
    app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
