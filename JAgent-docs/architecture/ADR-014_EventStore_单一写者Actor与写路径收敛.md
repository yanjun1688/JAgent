# ADR-014 EventStore 单一写者 Actor 与写路径收敛（Writer-Actor，评审待定）

| 属性 | 值 |
|---|---|
| **状态** | 提议（Proposed，独立评审中，未拍板） |
| **版本** | v0.1 |
| **日期** | 2026-09-10 |
| **作者** | Agent 导师 + 开发 Agent（阶段二子任务 A/B 预审收尾 / v3.3） |
| **关联** | [PR #19/#22 event-store 取消安全]（`_txn_immediate`/`_begin_immediate_with_recovery`）· [止血 PR #23：侧表 DML 接入 `_write_txn`] · [ARCHITECTURE_v3.3](./ARCHITECTURE_v3.3_Workspace_多租户与执行载体.md)（L1 Event Store 基础设施） · [TODO_v3.3_Workspace](../plans/workspace-v3.3/TODO_v3.3_Workspace.md)（阶段二子任务 A/B） |
| **范围** | L1 Event Store（SQLite 单连接写路径的心智模型 + 事务生命周期） |
| **优先级** | 待排期（本 ADR 只决策方向，不作为止血依赖） |

---

## 1. 摘要

EventStore 的全部写现在收敛到**一个共享 aiosqlite 连接**上，事务正确性靠"每个写方法都手工推理 aiosqlite 工作线程的 FIFO 顺序 + 全局 asyncio 写锁 + 取消安全清理"维持。这套推理散落在多处，且反复漏接（本评审已观察到第 4 次"造了安全网但没接上"：`_seq_locks` → `_is_stop_signal` → `_write_txn` 死代码 → 本轮接线）。每次都在事件写入之外的路径（workspace/tenant/conversation/claim）撞出同类孤儿事务缺陷。

本 ADR 提出一个**待评审**的架构方向：

> **把 EventStore 的全部 DML 收敛到单一写者 Actor（一条串行写队列），消除"多协程在共享连接上各自推理事务生命周期"这一整类心智负担；读路径保留/并行化（WAL）。**

**本 ADR 不在评审前拍板。** 它的作用是：(a) 把方向、成本收益、不受影响部分摆清楚供独立评审；(b) 决定阶段二子任务 A/B 的修法去留——若走 writer-actor，A/B 逐方法加固应降级为"写测试契约、不做修法"。

---

## 2. 背景与问题

### 2.1 现状：单连接 + 全局锁 + 逐方法取消安全

- SQLite 单连接（`:memory:` 或文件）跨所有 run 共享；`aiosqlite` 每个操作 = `put_nowait` 进工作线程 FIFO + `await future`。
- 写正确性靠 `asyncio.Lock`（`_db_write_lock`）把所有写事务串行化 + `_txn_immediate` 的取消安全清理（`BEGIN IMMEDIATE` → … → `COMMIT`，任何 `BaseException` 无条件入队 `ROLLBACK`）+ `_begin_immediate_with_recovery`（孤儿事务自愈兜底）。
- **但"哪些写必须持锁、哪些要用 `_txn_immediate`、异常清理放哪"没有单一出口**：每个新写方法都要自己答一遍。历史故障清单：
  1. `append_event` 曾因 watchdog 在 BEGIN…commit 间取消而遗留孤儿事务（PR #22，Linux CI 双取消竞态）；
  2. workspace DML 曾与 `append_event` 的 BEGIN IMMEDIATE 交错（P2 黑盒并发创建 500）；
  3. `claim_client_request` 用第二把锁 `_request_claim_lock`（与 `_db_write_lock` 不一致）；
  4. `ensure_tenant`/conversation 系列**完全不持写锁**，裸隐式事务。

### 2.2 观察：这不是"漏一次"，是"结构性重复犯错"

阶段二预审确认了 PR #22 总结中"覆盖全部写路径含终态写"**并未覆盖侧表 DML**：

- 唯一实际接入 `_txn_immediate` 的调用点是事件追加（+本次止血 PR 补的侧表 `_write_txn`）；
- 止血前 `_write_txn` 是**死代码**（零调用）；
- 本评审文档累计发现第 4 次同型问题：`_seq_locks`、`_is_stop_signal`、`_write_txn` 死代码、本轮侧表裸写。

**心智模型归因**：正确性前提是"每个并发协程在共享连接上按 FIFO 顺序小心开/关事务"，而这不是 EventStore 的 API 能强制约束的——它依赖每个调用方手工服从。这不是某个 bug，是"把不变量交给调用方自觉"的设计缺陷。

### 2.3 明确要解决的用户可见问题

- **数据一致性**：客户端已收到成功响应、写却被静默回滚丢弃（取消落在裸写 commit 前 → 孤儿事务 → 下一条 append 自愈时 ROLLBACK 掉未提交数据）。
- **终态可达性**：看门狗终态写（RunFailed）在共享连接被孤儿事务污染时可能卡死/失败。
- **可维护性**：每加一个写方法都要重复一遍锁/事务/清理推理，第 N+1 次漏接只是时间问题。

---

## 3. 方案：单一写者 Actor

### 3.1 核心机制

把 EventStore 的所有 DML（事件追加、workspace/tenant/conversation/claim 的写）改为**唯一写者任务**处理：

```text
写调用方 --enqueue--> 写队列（asyncio.Queue / 单任务消费）
                        |
                        v
              唯一写者 task：一次只取一条命令，
              在唯一持锁上下文里完成该命令的全部 DML + COMMIT
              （取消/异常 → 统一 ROLLBACK 入队，兜底仍可用 _begin_immediate_with_recovery）
```

要点：
- **不再有多个协程同时执行写 SQL**：单写者天然串行，`_db_write_lock` 从"每个调用方都要记得持"变成 actor 内部唯一持有（甚至可去掉 asyncio.Lock，靠单任务保证）。
- **取消安全集中在 actor 一处**：调用方 enqueue 后 `await` 结果；若调用方被取消，命令要么未执行、要么已由 actor 以统一规则收尾——不存在"裸写协程在事务中间被取消"的窗口。
- **事务生命周期收敛**：BEGIN → DML → COMMIT 全部在 actor 的同一函数栈内，消除 FIFO 推理。
- 读路径**不经过 actor**（独立连接或同连接 WAL 并发读）。

### 3.2 写者崩溃/关闭语义（本方案新增复杂度的核心）

- 写者 task 自身被取消/异常：必须把当前 in-flight 命令标记失败、通知其调用方，然后**重建/续跑**（写者不能死，否则系统停摆）。
- 关闭（`close()`）需排空队列并处理在途命令。
- 需与现有"看门狗强制终态写"的"即使主循环被打断也要能写"约束对齐。

---

## 4. 成本与收益

### 4.1 收益

| 项 | 说明 |
|---|---|
| **消除整类缺陷** | "多协程共享连接上事务交错/孤儿"不再可能，取消不再能落在"事务中间的裸写"上 |
| **阶段二 A/B 大量归零** | 逐方法补 `except BaseException`/回滚/换原语的加固（止血 PR 的接线代码）在 actor 下变为"一处统一逻辑"，A/B 的逐方法工作量基本消失 |
| **删减点状修复** | PR #19/#22 的取消清理可简化（仍可保留自愈作为纵深防御）；`_write_txn` 的多处接线可删除 |
| **新增写方法的心智成本趋零** | 新方法只需 enqueue 一条命令，不再各自推理锁/事务 |

### 4.2 成本与风险

| 项 | 说明 |
|---|---|
| **L1 内核改动** | EventStore 是受信底座；写路径心智模型整体改变，需独立评审 + 本 ADR |
| **新增复杂度** | 写者 task 生命周期/崩溃恢复/排空关闭语义；结果如何回传调用方（future/回调） |
| **吞吐不变** | SQLite 单写者本来就是唯一写者——actor 不提高吞吐，收益是正确性推理简化 |
| **迁移面** | 现有全部写调用点从"直接 await 方法"改为"enqueue + await"或保留 facade 语义但改底层 |
| **与读路径的耦合** | 需决策读连接策略（独立连接 / 同连接 WAL 读），引入连接管理复杂度 |
| **与止血 PR 的关系** | 止血 PR #23 的接线代码若 actor 落地可删——但它是低成本、立即止血的独立增量，不构成沉没架构 |

---

## 5. 不受影响的部分（边界）

- **R8（LLM 封装/终态守卫）/ R1 / R2（调度器）**：正交层，writer-actor 与它们互不影响，可并行排期。
- **读路径正确性**：actor 只收敛写；读的 snapshot/WAL 策略另议。
- **schema 迁移、备份、初始化、`append_event` 的幂等键查重/seq 分配语义**：不因写者模型改变。
- **`_begin_immediate_with_recovery` 去留**：即便 actor 化，SQLite 单连接上"actor 自身被硬杀留下孤儿"仍可能，建议作为保险丝保留（降级为纵深防御，不再是主线）。
- **确定性注入测试的价值**：无论走哪条路线，取消/并发注入测试都是失败模式的规格化契约，应先落地为"今天会失败/止血后通过"的测试钉住行为。

---

## 6. 备选与不做

| 选项 | 评价 |
|---|---|
| **维持现状 + 逐方法加固**（阶段二 A/B 原计划） | 低成本止血可行（本轮已做 #23），但第 4 次同型漏接证明"靠调用方自觉"不可持续；只能作为"等 ADR 期间的止血"，不是终态 |
| **独立连接 + WAL（无 actor）** | 解决"读不阻塞/连接污染面"，但**不解决多写协程推理负担**——写仍可能交错，需与 actor 或更强的写串行化结合 |
| **换 Postgres 等网络 DB** | 远期方向（EventStore 类注释已预留）；与当前 SQLite 阶段正交，超出本 ADR 范围 |
| **受信组件内用 LLM 决策 / 上 Workflow Engine** | 与 Harness 架构冲突，明确不做 |

---

## 7. 评审要回答的问题

1. writer-actor 是否立项？若立项，与"独立连接 + WAL"是取一还是组合（先 actor 后 WAL / 反之）？
2. actor 写者崩溃/关闭语义是否能满足"看门狗终态写不可丢"约束？
3. 阶段二子任务 A/B 的处置：若立项，A/B 降级为"写测试契约、不做修法"；若不立项，A/B 按原优先级走逐方法加固。
4. 读路径连接策略是否纳入本 ADR，或另开文档？

---

*本 ADR 为草稿，供独立评审使用，未在本对话内拍板。评审前不产生依赖本方向的代码改动。*
