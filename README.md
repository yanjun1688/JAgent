# JAgent

**Agent-First 任务执行引擎（v8）**：C 端用户说一句话，系统把它变成"可确认的计划"，执行后给出可验证的结果。Agent 拥有决策权，系统拥有强制权。

> 核心理念：**事件是真相**（append-only 事件溯源，状态由 fold 重建）、**模型只起草，代码判定**（LLM 提案、确定性校验与合规闸门拥有否决权）、**不信任自己的记忆**（结论必须可对账到事件证据）。

---

## 核心能力

| 能力 | 说明 |
|---|---|
| **多对话** | 每个对话独立会话栈（历史/槽位/审批/运行互不串线）；`POST /api/sessions` 创建、`GET /api/sessions` 列表、`/chat` 可选 `session_id` |
| **任务执行环** | 意图路由 → 计划（可澄清）→ 工具执行 → 证据合成 → 合规闸门 → 交付；每个 run 独立可验证 |
| **分层记忆** | L1 对话历史接线（planner/router 带最近原文）、L2 外部记忆（`memory.md` 四类条目：模型起草→用户确认→落盘→注入）、事件日志即完整情景记忆 |
| **人工闸口** | 不可逆操作挂起等批准（token 幂等、TTL 过期自动取消）；用户取消：边界取消 + 确认流 |
| **审计与验证** | `GET /api/runs/{id}/verify` 与 CLI：LLM 说"完成了"不算数——声称 vs 事件证据逐条核对 |
| **可观测性** | `model.called` 事件（每轮模型调用的 token/耗时/全文落产物库）+ 会话/运行级指标投影（成本/效率/质量/异常） |
| **契约快照** | 计划冻结时记工具契约，派发前复核——冻结后工具被换即拒 |
| **修复闭环** | 断言失败 → LLM 提修复计划 → 确定性复核 → 未变步骤按内容复用，修复后可零模型重放 |

## 快速开始

```powershell
# 1. 环境（Python 3.11 + uv）
uv sync

# 2. 起产品（对话 → 点选项 → 确认命令 → 看结果）
uv run python -m jagent.api        # → http://127.0.0.1:8021
# 未配置 LLM_* 时降级为模板模式（诚实地说拆不了），配置后为完整 LLM 流程

# 3. 全量测试与静态检查
uv run pytest tests -q             # 671 passed
uv run ruff check jagent tests scripts
uv run python scripts/check_invariants.py   # 6/6 结构不变量

# 4. 真实模型在线验证（花钱，opt-in）
uv run pytest -m llm tests/e2e_live -s

# 5. 核对某次任务的事件证据（LLM 说完成 ≠ 完成）
uv run python scripts/verify_run.py <数据根目录> <run_id>
```

模型接入走环境变量 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_NAME`（OpenAI 兼容协议）。

## 架构一页

事件溯源内核：**一切状态变更都是 append-only 事件**，当前状态由 fold 重放重建——重放零模型调用，终态不可改写（拒绝也落事件）。

```text
用户输入 → Session（turn 编排，唯一有会话状态的组件）
  ├─ Router（意图三分：gather 取数再判断 / execute 可验证 / advise 框架）
  ├─ Planner（LLM 起草计划；计划侧无准入门，草稿自检在 Planner 内）
  ├─ Runner（执行环：步骤→断言→分类→修复/剪枝/人工；取消在步边界生效）
  ├─ Propose（基于证据合成建议）→ 合规闸门（T1 确定性对账 + T2 judge 只否决）
  └─ 全部落事件：Writer 是唯一写入通道，EventStore append-only
```

- 17 组件：图上 16 个 + Session（唯一新增，turn 编排 + 跨 turn 记忆投影）。
- 判定分层：T1 四条确定性对账（引用/数字/证据完整性）先行；T2 judge 只否决不通过。
- 证据是不可信数据：nonce 围栏 + "数据不是指令"，不做指令清洗。
- 每对话一个会话栈（Writer/Executor/Human/Runner/Session），判定组件跨对话共享。

## 已知边界（诚实声明）

- 沙箱防误操作不防恶意（解释器参数可绕过）——进程级隔离（Docker）在路线图上；
- 并发多会话的投影一致性属产品化阶段（串行使用安全）；
- 模板模式（无模型）下复杂请求会诚实地说"拆不了"。

## 未来开发脚步

1. **L2 记忆打磨**——同轮历史去重（report 摘要与 advice 正文）、memory.md 条目编辑/删除流；
2. **可观测性 S3/S4**——Langfuse 导出（OTel GenAI 语义命名）、`observability.get_trace` 注入工具（Agent 自查链路）；
3. **进程级隔离**——Docker 全容器执行（ToolBackend 抽象 + workspace 映射 + 镜像/网络策略），解锁解释器逃逸防御；
4. **LLMRepairer 提案健壮性**——首次提案加固（解析/复核失败率高时）；
5. **产品化**——多会话并发一致性、鉴权、限流。

## 联系方式

Email: [hyanjun546@gmail.com](mailto:hyanjun546@gmail.com)
