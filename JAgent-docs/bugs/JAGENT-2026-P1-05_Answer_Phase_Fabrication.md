# Bug: P1-5 Answer 阶段编造执行结果与修订行为

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P1-05 |
| 严重级别 | P1 高优先级（用户可见的事实造假） |
| 发现日期 | 2026-08-05 |
| 所在文件 | `harness/core/planner.py`（generate_answer）、`harness/core/system_prompt.py`（_ANSWER_PROMPT） |
| 影响范围 | 所有 DAG 执行后生成最终答案的 run |

## 现象

两种编造，均经黑盒日志坐实：

1. **编造工具执行**（run `02590e8d`）：answer 声称"已按第 6 点条件触发补充流程，创建 CSV 结构文件并重新读取成功"。但沙箱里 `dataset.csv` 从未创建，修订返回空步骤后无任何 file_op 调用。**纯幻觉。**

2. **编造修订行为**（run `163009eb`）：answer 声称修订"未在修订中采用纯读取重试，而是明确必须补充创建步骤"。实际修订返回**空步骤**，"明确要求创建步骤"从未发生。

## 根因

- answer 阶段是非受信组件，`_ANSWER_PROMPT` 原本只有 "Provide a complete answer with all the information gathered"，**没有任何落地约束**
- `generate_answer` 把 `tool_results` 原样塞给 LLM，但未声明其权威性、未含修订结果 → LLM 面对"硬性交付未达成"的尴尬，选择编造圆场

## 为什么现有机制没拦住

- answer 的输出直接透传给用户，无受信层校验
- 上下文里执行记录未标"AUTHORITATIVE、唯一来源"，LLM 不认为必须严格溯源

## 修复方案

1. `_ANSWER_PROMPT` 增加 4 条接地规则：
   - 执行类陈述必须能追溯到 `[Tool execution results]` 记录
   - 禁止描述记录中不存在的工具调用
   - 失败未重跑成功时如实报告，禁止编造补救
   - `[Run outcome]` 亦为权威，不得与之矛盾
2. `generate_answer`：
   - tool_results 标为 AUTHORITATIVE 唯一执行记录，顶部加 `[Execution digest]`（step→status 摘要）
   - 从 `state.latest_plan`（fold 自 PLAN_REVISED/PLAN_COMPLETED）注入 `[Run outcome]` 块（revision_reason / remaining_steps_summary / plan status）

## 测试用例

`tests/test_planner.py` +2：
- `test_generate_answer_includes_revision_outcome`：outcome 块注入
- `test_generate_answer_omits_outcome_without_plan`：无 plan 记录（chat 路径）不注入

## 效果验证

run `ed1df97b`（修复后）：answer 首次如实报告"dataset.csv 不存在、任务尚未彻底完成"，无编造。
