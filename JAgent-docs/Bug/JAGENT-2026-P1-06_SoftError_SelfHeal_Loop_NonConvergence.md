# Problem: P1-6 Soft-Error 自愈循环无法收敛

| 属性 | 值 |
|------|-----|
| Problem ID | JAGENT-2026-P1-06 |
| 严重级别 | P1 高优先级（核心未决，阻塞分支 A 端到端绿跑） |
| 发现日期 | 2026-08-05 |
| 所在文件 | 非单一文件——涉及 revise 契约、RERUN RULES、scheduler 自愈循环 |
| 状态 | **已修复**（自愈收敛与 D12 下游恢复均有回归测试） |

## 现象

初始计划含"读取一个不存在文件"步骤 → 真实读取 soft-error → end-of-plan 修订触发。此后：

- **run a0f8810f**: round 1-5 修订每轮返回同一个"再次读取"步骤（新 description："Attempt to read ... again to get real results"），从不创建文件；round 5 返回 3 步时被 `Self-heal loop exceeded max (5) attempts` 拦停 → `RunFailed`。
- **run ed1df97b**: round 1-4 同样反复"重读"，round 5 直接返回空 → "task complete" → `RunCompleted`，但硬性交付（dataset.csv 成功读取）**未达成**。

分支 A（`Soft-error revise returned N steps → re-executing`）已**可靠触发**，但自愈循环不收敛。

## 根因

1. **RERUN RULES 引导 LLM 走"重试"而非"修复"**: revise 提示词明确"soft_error/failed 步骤要保留在修订计划中重试"。对"文件缺失"型 soft-error，LLM 的最小改动路径是重读——它不推导"需要先创建前置缺失物"。
2. **约束冲突**: 意图同时含"初始计划严禁创建该文件"与"若失败必须创建"，revise LLM 把两条解读为互相矛盾，最终选择"什么都不做"（返回空）。
3. **自相矛盾仍被接受**: 163009eb/ed1df97b 中 LLM 把 s1 标 `not_achieved` 却返回空步骤判"task complete"，scheduler 照单全收 → run `status=completed failures=0` 但交付物未达成（见 U2）。

## 为什么现有机制没拦住

- 受信层只做 5 次上限兜底（breaker），不校验修订的"收敛质量"
- "soft-error 是否可接受"被设计为 LLM 的判断权（09659299 的分支 B 是**正确**结局——探测失败可接受），系统无法无差别强制重跑
- 系统不知道意图声明的"硬性交付"语义（自由文本），无法自动判定"deliverable unmet"

## 候选方案（需决策，均未实施）

- **A. 拒绝自相矛盾**: 修订返回空 + 该 LLM 自标 `not_achieved` 步骤存在时，拒绝接受"task complete"，强制重新修订（上限内）。风险：仍是 LLM 判断，可靠性存疑。
- **B. 系统强制收敛**: 对"必须成功"型步骤（步骤描述含强语义或意图声明），soft-error 修订空时由受信层强制注入"创建+重读"型步骤。风险：无法语义化判定"soft-error 可接受"，破坏分支 B 合法路径。
- **C. 接受现状**: 分支 A 触发已验证；收敛性属 LLM 策略质量，靠意图措辞优化（本轮已证部分改善、不可靠）。

## 建议

先做 A（低风险、只堵自相矛盾），再评估是否值得投入 B。长期可在 LLM 能力升级后再评估纯措辞方案。

## D12 新发现：修订计划丢失下游步骤

### 现象

串行黑盒场景 `A → B → C` 中，A 成功、B 返回 404、C 被门控为 `SKIPPED`。LLM 随后只返回 B 的替代请求；旧 Scheduler 用修订计划替换原计划，替代请求成功后直接以当前计划 `1/1` 写入 `RunCompleted`，C 没有恢复执行。

### 根因

完成门和计划生命周期以当前修订计划为全集，未保存原始 Run 的目标步骤集合；`SKIPPED` 结果也未在依赖恢复时清除，因此下游无法重新进入拓扑执行。

### 修复标准

- 原始步骤全集由 Scheduler 保存并用于最终完成门。
- 修订步骤可替代失败步骤，但不得减少原始目标。
- 恢复失败前驱后，原先 `SKIPPED` 的下游步骤必须重新执行。
- `RunCompleted` 必须满足原始步骤全集 `step_normal=true` 且 `unmet_step_ids=[]`。
