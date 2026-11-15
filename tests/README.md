# tests/ · 目录职责与落点

新测试放哪：**横切新机制 → 下表对应目录；无归属 → `tests/v8/`**。
命名随内容，不随 wave（`v8_c3` 这类切片名是历史遗留，逐步并入 v8/）。

| 目录 | 职责 | 对应规格/出处 |
|---|---|---|
| `v8_harness/` | harness 层单元：终态守卫、产物完整性、契约快照、事件链、取消契约 | 架构 §3 / D-028 |
| `v8/` | 会话级与执行环：取消流程、恢复、LLM 组件、审计查询、多对话 | 架构 §4 / D-028/032/034 |
| `v8_acceptance/` | grounding §6 验收条目（#7…#18）逐条对账 | grounding §6 |
| `v8_gate/` | T1/T2 闸门单元 | grounding §3 |
| `v8_redteam/` | 红队攻击性测试（RT-\*；含 `known_bug` strict-xfail——修一条解一条） | redteam 报告 |
| `v8_hardening/` | G-05 恢复对账独立验收 | hardening 报告 |
| `v8_config/` | prompt/policy/budget 外置（I6） | brief §5.5 |
| `v8_model/` | 模型端口层（超时/重试/outage） | ports |
| `v8_arch/` | conformance 结构断言——**时点快照**，随台账裁决 re-baseline（G-01/G-02/G-23 先例） | conformance |
| `v8_repair/` | 修复闭环：LLMRepairer / 复用键 / 零模型重放（D-026/D-027） | brief §4 |
| `v8_c3/` | 历史 wave 遗留（T3 投递/plancheck） | C3 切片 |
| `e2e_live/` | **真实模型在线**：`pytest -m llm tests/e2e_live -s`（花钱，opt-in） | — |
| `support.py` / `v8_acceptance/support.py` / 各目录 `conftest.py` | 共享夹具（DraftPlanner / FixedRouter / rig 等） | — |

共享夹具约定：`tests/v8/conftest.py` 提供**最小 rig**（真 store + 真 fold + 真
Writer + 真 executor）；`v8_redteam/v8_hardening` 的 conftest 各自更富。
新共享夹具进对应层级 conftest，不互相 import 夹具函数（pytest 直呼禁用）。
