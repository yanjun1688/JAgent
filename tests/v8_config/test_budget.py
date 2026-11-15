"""C1：预算外置到 ``config/budget.toml``；缺文件/缺字段拒绝启动（宪法 I6 / brief §5.5）。

这些测试只钉"数据来源是文件"与"没有内联默认值"，不重估阈值本身该是多少。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from jagent.config import CONFIG_ROOT, ConfigError, load_config
from jagent.contracts import Budget, EventType, Plan, Step, Verify
from jagent.wiring import build_app


def test_budget_is_loaded_from_the_toml() -> None:
    """toml 的值 == 基线代码里的 Budget 默认值（本次只搬家，不调参）。"""
    config = load_config()

    assert config.root == CONFIG_ROOT
    assert dataclasses.asdict(config.budget) == dataclasses.asdict(Budget())


def test_loader_is_package_relative_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.root == CONFIG_ROOT
    assert config.policy_path == CONFIG_ROOT.parent / "gate" / "policy.md"
    assert config.hash


def test_missing_budget_file_refuses_startup(config_copy: Path) -> None:
    (config_copy / "budget.toml").unlink()

    with pytest.raises(ConfigError, match="budget.toml"):
        load_config(config_copy)


def test_missing_budget_field_refuses_startup(config_copy: Path) -> None:
    budget = config_copy / "budget.toml"
    budget.write_text(
        budget.read_text(encoding="utf-8").replace("max_repairs = 2\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="max_repairs"):
        load_config(config_copy)


def test_unknown_budget_field_refuses_startup(config_copy: Path) -> None:
    budget = config_copy / "budget.toml"
    budget.write_text(budget.read_text(encoding="utf-8") + "max_stepz = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="max_stepz"):
        load_config(config_copy)


def test_hash_is_stable_and_covers_every_config_byte(config_copy: Path) -> None:
    first = load_config(config_copy)

    assert load_config(config_copy).hash == first.hash

    budget = config_copy / "budget.toml"
    budget.write_text(
        budget.read_text(encoding="utf-8").replace("max_steps = 10", "max_steps = 11"),
        encoding="utf-8",
    )

    assert load_config(config_copy).hash != first.hash


def test_hash_covers_gate_policy(config_copy: Path) -> None:
    before = load_config(config_copy).hash
    policy = config_copy.parent / "gate" / "policy.md"
    policy.write_text(
        policy.read_text(encoding="utf-8") + "\n<!-- hash probe -->\n", encoding="utf-8"
    )

    assert load_config(config_copy).hash != before


def test_no_global_singleton() -> None:
    first = load_config()
    second = load_config()

    assert first is not second
    assert first.prompts is not second.prompts


def test_wiring_injects_the_config_budget(tmp_path: Path) -> None:
    config = load_config()
    app = build_app(tmp_path / "app")
    try:
        assert app.runner.budget == config.budget
    finally:
        app.close()


def test_explicit_budget_still_wins_over_config(tmp_path: Path) -> None:
    override = Budget(max_steps=1)
    app = build_app(tmp_path / "app", budget=override)
    try:
        assert app.runner.budget is override
    finally:
        app.close()


def test_budget_from_mapping_rejects_gaps_and_typos() -> None:
    with pytest.raises(ValueError, match="缺少字段"):
        Budget.from_mapping({})

    data = dataclasses.asdict(Budget())
    data["not_a_field"] = 1
    with pytest.raises(ValueError, match="未知字段"):
        Budget.from_mapping(data)


def test_evidence_limits_are_required_budget_fields() -> None:
    """证据阈值也是外部旋钮：少一项就拒绝启动，不回落代码里的默认值。"""
    data = dataclasses.asdict(Budget())
    for name in ("evidence_max_steps", "evidence_clip_chars"):
        partial = dict(data)
        del partial[name]
        with pytest.raises(ValueError, match=name):
            Budget.from_mapping(partial)


def test_toml_evidence_limits_drive_truncation_and_omission(
    tmp_path: Path, config_copy: Path
) -> None:
    """改 budget.toml 的两个值，证据包的截断长度与省略步数随之改变。"""
    budget = config_copy / "budget.toml"
    budget.write_text(
        budget.read_text(encoding="utf-8")
        .replace("evidence_max_steps = 8", "evidence_max_steps = 2")
        .replace("evidence_clip_chars = 600", "evidence_clip_chars = 40"),
        encoding="utf-8",
    )
    config = load_config(config_copy)
    assert (config.budget.evidence_max_steps, config.budget.evidence_clip_chars) == (2, 40)

    plan = Plan(
        steps=tuple(
            Step(
                id=f"s{i}",
                tool="fs.read",
                args={"path": "doc.md"},
                verify=Verify("len(content) > 0", needs=("content",)),
            )
            for i in range(3)
        ),
        then="advice",
    )
    app = build_app(tmp_path / "app", config=config)
    run_id = "run_budget_evidence"
    try:
        for i in range(3):
            app.writer.emit(
                EventType.STEP_FINISHED,
                {
                    "step_id": f"s{i}",
                    "status": "ok",
                    "artifact_refs": [f"art-{i}"],
                    "observations": {"content": "x" * 100},
                },
                run_id=run_id,
                step_id=f"s{i}",
            )
        bundle = app.session._evidence_bundle(run_id, plan)
        raw = app.session._evidence_of(run_id)
    finally:
        app.close()

    assert bundle.omitted_steps == 1
    assert [item.step_id for item in bundle.items] == ["s1", "s2"]
    assert [len(item.observations["content"]) for item in bundle.items] == [40, 40]
    assert [item.truncated for item in bundle.items] == [("content",), ("content",)]
    assert [step["step_id"] for step in raw["steps"]] == ["s1", "s2"]
