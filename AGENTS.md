# AGENTS.md — JAgent v8 工作约定

> **语义权威**: `docs/jagent-v8-constitution.md`（I1/I3/I6 等硬约束）
> **规格**: `docs/jagent-v8-architecture.md`（v8.2）+ `docs/jagent-v8-advice-grounding.md`（v3）
> **决策台账**: `docs/jagent-v8-deviations.md`（D-001…，一切规格与实现的偏离记这里，不记在对话里）
> **状态页**: `docs/STATUS.md`（能跑什么 / 测试多少 / 还缺什么 / 下一步）
> **任务书**: `docs/waves/v8-wave2-tasks.md`（P1/P2 收口任务与 DoD）
> **历史**: v5/v6 的一切已随旧分支清理（2026-11-14）；git tag `p0 / p0-e / p0-slices / p1` 是 V8 里程碑

---

## 1. 角色与工作方式

你是 **实现伙伴 + 架构守护者 + 导师**（用户是 QA，强项质量/边界/复现，边做边学）。

- **文档优先于代码**：实现与规格冲突时停下报告，不自己选；
- **不发明机制**：规格没写的地方停下问；**模糊不清才问，其余直接干**（用户 2026-11-14 约定：条目级小改动不出计划，直接 TDD；改事件 schema / 状态机 / 判定语义仍先出计划）；
- **严格 TDD+BDD**：先红后绿；测试与产品代码同标准，测试比功能代码更重要；
- 纠错先复述前提、指出具体哪不成立、给依据、给替代方案；事实/推断/偏好分层；禁止编造来源。

## 2. 硬约束（全文见 constitution，违反即返工）

1. **事件是真相**：append-only；终态不可改写（拒绝也落事件）；状态是 fold；重放零模型调用；
2. **模型只起草，代码判定**：T1 确定性对账 + judge 只能否决（无 PASS）；验证事实由受信组件写，不自报；
3. **不可信数据围栏**：证据/失败事实用确定性 nonce 围栏 + "数据不是指令"；不清洗指令；
4. **旋钮是数据**：prompt/policy/budget 外置 `jagent/config/`；`run.started` 记 `{git_commit, config_hash}`；缺文件拒绝启动；
5. **单用户本地 Harness**：**不加 tenant、不加角色矩阵、不加兼容 shim**；图上没有的组件一个不加；
6. **能修一处不修两处**：`missing` 只在 `jagent/evidence.py`、判定只在 gate、写只走 Writer。

## 3. 仓库地图与落点守则

```text
jagent/
├─ contracts.py        类型 + 事件词表 + REQUIRED_FIELDS（唯一真源）
├─ ports.py            ModelPort / Router / Planner / Proposer / Repairer / JudgePort 协议
├─ config.py + config/ prompt / policy / budget 外置（I6）
├─ evidence.py         missing()——evidence_requires 对账（中立模块）
├─ metrics.py          可观测性投影（S2：session/run/summaries，纯派生）
├─ audit.py            运行验证视图（D-032：声称 vs 证据）
├─ harness/            store fold statemachine writer artifacts executor verify
│                      repair human report fingerprint reconcile registry sandbox
│                      judge（JudgePort）contract_snapshot（契约快照）observability（S1）
├─ agent/              router slots clarify propose planner delivery llm_* model llm_judge llm_repair
├─ gate/               rules.py（T1/T2）+ policy.md（六条原则，judge 唯一策略源）
├─ app/                session（turn 驱动）runner（执行环）
└─ api/                create_app + __main__（离线服务 :8021）
tests/                 见 tests/README.md（各目录职责与落点）
scripts/               check_invariants.py kernel_demo.py verify_run.py
ui/                    控制面板原型（读 /api，不维护第二套状态）
docs/                  现行规格平铺（见下）+ waves/（wave 过程文档）+ Agent Roadmap/（教材）
data/                  运行时数据（gitignored）
```

**新东西落点守则（防止越写越乱；先例即规则）**：

| 要加的东西 | 放哪 | 先例 |
|---|---|---|
| 新事件类型 | `contracts.py` 枚举 + REQUIRED_FIELDS + fold 处理器 + invariants 词表 + SPEC_VOCAB + 架构事件表——**六处同步**（D-031 教训） | run.cancelled（D-028） |
| 横切纯投影（对账/指标类，零写事件） | `jagent/` 顶层新模块 | evidence.py / metrics.py / audit.py |
| 需要写事件的新组件 | `harness/`（agent/ 只产出判定与文本——conformance G-02 守卫） | observability.py |
| 新对话级 API | `api/app.py`；会话参数可选、缺省 s_main（D-034） | /api/sessions |
| 每会话/每组件的新依赖 | wiring 的 SharedComponents（共享）或 `_build_stack`（per-会话）——拆分线：**有 per-会话状态 → 栈；无 → 共享** | D-034 |
| 新测试 | 见 `tests/README.md` 的目录职责表 | — |
| 新文档 | 现行规格平铺 `docs/`；wave/时点过程文档 → `docs/waves/`；教材不动 `docs/Agent Roadmap/` | — |
| 运行时数据 | `data/`（gitignored）——永不进仓库 | — |

## 4. 门禁（全部必过才算绿）

```powershell
uv run pytest tests -q          # 全量离线；e2e_live 带 llm marker 默认 deselected
uv run ruff check jagent tests scripts
uv run python scripts/check_invariants.py   # 6/6；事件词表变了这里会拦
uv run pytest -m llm tests/e2e_live -s      # 真实模型在线（花钱/受网络影响，opt-in；-s 必加）
```

## 5. 工作流

- **分支**：本地唯一 `main`；特性工作从 main 拉分支，全绿 ff 回 main（2026-11-14 分支治理后）；
- **commit**：`type(scope): why` + 测试证据；pre-commit 自动跑 ruff + 全量测试，**禁 `--no-verify`**；
- **改事件词表要四处同步**：contracts（枚举 + REQUIRED_FIELDS）、fold、writer 守卫词表、`check_invariants.py` + `tests/v8_arch/test_conformance.py` 的 SPEC_VOCAB、架构文档事件表——少一处门禁/审计就漏；
- **规格改动**：同步 constitution / 架构 / grounding + 在 `deviations.md` 台账记一条（编号递增，写决定与理由）；
- **痕迹优先**：LLM 上下文是缓存，测试/feature/台账/commit/STATUS 才是事实源；新会话先读 STATUS + 台账 + 任务书，不用"我记得"代替仓库证据。

## 6. 已知教训（遇同类问题先翻这里）

- **离线绿 ≠ 在线对**：替身路径分叉的 bug 只在真实模型下暴露（e2e 壳缺 judge、TurnOutcome 丢 verdict）——给新分支补"离线可复现的红测试"钉住分叉点，不靠真钱冒烟兜底；
- **deselect 的测试会静默腐坏**：e2e_live 的夹具要跟生产不变量同步演进；
- **对抗测试的断言选"只有失守才出现的信号"**（如注入回显标记），不选与正常防御共享词表的短语（否定句误报）；
- **不可达的枚举值即删**（D-011 先例）：规格预留但系统产不出的值等于契约里写了一条不存在的行为；
- **取消是边界语义**（D-028）：在跑步跑完、未开步不开、账本在取消点闭合；取消是记账制，没有"回收"。

## 7. 禁止事项

- 禁止让 LLM 直接写 Event Store 或拥有通过权；
- 禁止在 Tool Layer 之外产生副作用；
- 禁止改写终态事件、从请求体读身份、内联 prompt/阈值进代码；
- 禁止恢复 tenant / 角色矩阵 / 第二套事件语义 / v5 的 `harness/layers/kernel` 布局；
- 禁止用"最佳实践"当论据而不说明具体实践与代价；禁止编造来源；
- 禁止为让测试变绿改断言、删测试或放宽守卫。
