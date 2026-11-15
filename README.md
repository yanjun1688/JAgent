# JAgent

**Agent-First 任务执行引擎（v8）**：C 端用户说一句话，系统把它变成"可确认的计划"，执行后给出可验证的结果。Agent 拥有决策权，系统拥有强制权。

> 核心理念：**事件是真相**（append-only 事件溯源，状态由 fold 重建）、**模型只起草，代码判定**（LLM 提案、确定性校验与合规闸门拥有否决权）、**不信任自己的记忆**（结论必须可对账到事件证据）。

---

## 核心能力

| 能力 | 说明 |
|---|---|
| **多对话** | 每个对话独立会话栈（历史/槽位/审批/运行互不串线）；`POST /api/sessions` 创建、`GET /api/sessions` 列表、`/chat` 可选 `session_id` |
| **任务执行环** | 意图路由 → 计划（可澄清）→ 工具执行 → 证据合成 → 合规闸门 → 交付；每个 run 独立可验证 |
| **统一 POSIX 执行底座** | shell 工具在两平台都经 `bash -c` 执行，沙箱一套 POSIX 语义（Windows 用 Git for Windows 自带的 bash） |
| **分层记忆** | L1 对话历史接线（planner/router 带最近原文）、L2 外部记忆（`memory.md` 四类条目：模型起草→用户确认→落盘→注入）、事件日志即完整情景记忆 |
| **人工闸口** | 不可逆操作挂起等批准（token 幂等、TTL 过期自动取消）；用户取消：边界取消 + 确认流 |
| **审计与验证** | `GET /api/runs/{id}/verify`：LLM 说"完成了"不算数——声称 vs 事件证据逐条核对 |
| **可观测性** | `model.called` 事件（每轮模型调用的 token/耗时/全文落产物库）+ 进程日志出口（`GET /api/logs`）+ 会话/运行级指标投影（成本/效率/质量/异常） |
| **契约快照** | 计划冻结时记工具契约，派发前复核——冻结后工具被换即拒 |
| **修复闭环** | 断言失败 → LLM 提修复计划 → 确定性复核 → 未变步骤按内容复用，修复后可零模型重放 |

## 快速开始

```bash
# 0. 前置：Python 3.11、uv、Git（Windows 上 shell 工具还需要 Git for Windows 自带的 bash）
#    Node 18+ 仅在需要构建前端面板时（见 ui/README.md）

# 1. 环境
uv sync

# 2. 配置：cp .env.example .env，填入 LLM_* 三件套（不配置则降级为模板模式，
#    诚实地拒绝复杂任务；全部变量清单见 .env.example）

# 3. 起产品（对话 → 点选项 → 确认命令 → 看结果）
uv run python -m jagent.api        # → http://127.0.0.1:8021

# 4. 全量离线测试与静态检查（CI 跑的就是这两条）
uv run pytest tests -q             # 在线 e2e 默认排除，见下
uv run ruff check jagent tests

# 5. 真实模型在线验证（花钱，opt-in）
uv run pytest -m llm tests/e2e_live -s
```

## 配置（.env）

复制 `.env.example` 为 `.env` 并按需填写；完整变量清单与默认值见该文件。要点：

- `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_NAME`（或 `JAGENT_LLM_*` 前缀）：OpenAI 兼容协议的模型端点；
- `JAGENT_DATA_DIR`：数据根（事件库/产物/workspace/记忆），默认 `data/v8_http`；
- `JAGENT_HOST / JAGENT_PORT`：服务绑定地址与端口；
- `JAGENT_MCP_*`：MCP 工具服务接入（缺省关闭）。

## 开发约定

见 [AGENTS.md](AGENTS.md)：硬约束（事件溯源/代码判定/沙箱边界）、门禁、落点守则、**AI 并行开发协议**（多会话必须各用独立 git worktree 与数据根）。两个要点：

- **门禁面 = 发布面**：CI 跑全量离线套件；对本地私有内容的引用由 conformance 机械检查（引用必须登记 allowlist）；
- **本地私有内容**：规格文档与本地脚本按仓库约定不进公开仓库（D-034），只存在于开发者本地工作区——引用它们的地方在 conformance allowlist 登记。

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

- 沙箱防误操作不防恶意（解释器参数可绕过）——进程级隔离（Docker 全容器执行）在路线图上；
- 超时只杀直接子进程：Windows 下 bash 的孙进程可能存活到超时之后（POSIX 上 `bash -c` 单命令通常直接 exec，等效于杀掉工作进程）；
- 并发多会话的投影一致性属产品化阶段（串行使用安全）；
- 模板模式（无模型）下复杂请求会诚实地拒绝拆解；
- 真实模型 + Linux 平台的组合尚未做在线 e2e 验证（e2e_live 目前只在 Windows 上跑过）。

## 未来开发脚步

1. **L2 记忆打磨**——同轮历史去重（report 摘要与 advice 正文）、memory.md 条目编辑/删除流；
2. **可观测性 S3/S4**——Langfuse 导出（OTel GenAI 语义命名）、`observability.get_trace` 注入工具（Agent 自查链路）；
3. **进程级隔离**——ToolBackend 抽象 + Docker 全容器执行（dev/deploy/CI 三环境同构），解锁解释器逃逸防御与超时进程组收口；
4. **评估第二步**——在线真实模型能力评估（pass^k 稳定性）+ 失败分析报告；
5. **产品化**——多会话并发一致性、鉴权、限流。

## 联系方式

Email: [hyanjun546@gmail.com](mailto:hyanjun546@gmail.com)
