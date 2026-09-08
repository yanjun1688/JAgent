# Review & Fix — 孤儿 run 受信层硬化：多租户归属 + 执行载体回收

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-08 |
| **审查人** | Agent 导师（架构审查 + TDD 实现） |
| **范围** | v3.4 基线（PR #16 合并后）受信层安全复核线索 #1（执行载体孤儿资源回收）、#2（mark_orphans 多租户归属） |
| **基线** | 修复前 `pytest` 1407 passed / 2 skipped；修复后 **1414 passed / 2 skipped**，`ruff check harness tests scripts` 0 error |
| **相关文档** | [DESIGN_v3.4](../Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md) · [ADR-011](../architecture/ADR-011_浏览器后端收敛与浏览器池.md) · [REPLAY_INSPECTOR_v1.0](../architecture/REPLAY_INSPECTOR_v1.0.md) |
| **状态** | ✅ 已修复（TDD：先红后绿） |

---

## 1. 线索 #2：mark_orphans 终态事件多租户归属错误（High）

### 根因
`mark_orphans`（`harness/core/lifecycle.py`）是跨租户系统维护例程，在 `app.py` lifespan 中以 **raw store（无租户上下文）** 调用（`app.py:79`）。它扫描所有租户的 run，但追加 `RunOrphaned`/`RunFailed` 时**未传 `tenant_id`/`workspace_id`**，落到 `EventStore.append_event` 的默认值 `tenant_id="default"`（`event_store.py:363`）；`workspace_id` 靠 `_run_to_workspace` 启动预热缓存补（`event_store.py:209-211,395-396`）。

### 影响（非 default 租户的孤儿 run）
1. 终态事件被写成 `default` 租户 → `ScopedEventStore.get_events` 按 tenant 过滤（`scoped.py:61`）后，**真实租户视角下 run 永远停在 RUNNING/PAUSED 僵尸**，方向 A 的终态收敛对跨租户失效。
2. WS 广播从事件 json 取 `tenant_id`（`deps.py:270`）得到 `default`，非 default 租户客户端被 `deps.py:275` 跳过 → **UI 收不到终结通知**。
3. 审计归属错误。

### 为何没拦住
现有 `tests/test_lifecycle.py` 全部用 raw store + default 租户，**无多租户用例**。

### 修法（受信、确定性）
`lifecycle.py` 从该 run **自身事件流机械派生** `tenant_id`（新增 `_derive_run_tenant`：run_id 全局唯一，事件应同租户；出现多租户记 error 并按多数归属，绝不静默落 default）与 `workspace_id`（首个非空），两次 append 显式传入。未改 L1 存储层语义。

### 测试
`tests/test_lifecycle.py::TestOrphanMultiTenantAttribution`：
- 非 default 租户孤儿终结后，其 scoped 视图可见 `RunOrphaned`/`RunFailed` 且列归属正确（tenant + workspace），default 租户视图为空；
- default 租户行为不变。

---

## 2. 线索 #1：孤儿 run 的 Docker 执行载体确定性泄漏（High）

### 根因
Docker 载体懒启动容器：`docker run -d --rm ... sleep infinity`（`docker.py` `_ensure_container`）。`--rm` 仅在容器**停止/退出**时删除，而 `sleep infinity` 永不退出 → harness 进程崩溃后容器持续运行并占住 bind mount。清理 `docker rm -f` 只在 `DockerSandboxBackend.close()`，而 `close()` 的全部调用点都在**活进程 scheduler 收尾路径**（`scheduler/base.py:583-587` finally、`api/deps.py:202-207` cleanup_run_resources 经 `run_end_cb`）。孤儿 run 不经 scheduler finally，`mark_orphans` 只 append 事件、不触碰载体。

对照组浏览器池能自愈，是因为它**订阅 `RUN_ORPHANED` 事件**（`browser_pool.py:102-110`）且有跨进程发现机制（磁盘 profile 锁 + 启动 `reap_stale_profile_locks`）。Docker/SSH 载体此前**无任何跨进程可发现身份**。

### 为何没拦住
测试全在单进程内 mock/monkeypatch（`tests/test_execution.py`），无"崩溃重启后容器残留"场景。

### 修法（受信、事件驱动，不依赖 Agent）
- **身份标签**：run-bound Docker 容器启动时打 `--label harness.managed=1 --label harness.run_id=<uuid> [--label harness.tenant_id=…]`（`docker.py` 新增 `_docker_run_args()`，`_sanitize_label_value` 归一化 label 字符集）。run_id 为 uuid、天然 label-safe，是 reaper 的关联键；tenant label 仅诊断。未绑定 run 的 backend **不打 managed 标签**，reaper 绝不触碰。
- **启动 reaper**：新增 `harness/execution/reaper.py`，在 `app.py` lifespan 中 **`mark_orphans` 之后**运行 `reap_orphaned_carriers(store)`：
  1. `docker ps --filter label=harness.managed=1`（JSON format 解析 id + run_id label）；
  2. 对每个 managed 容器 fold 其 run 事件流，判定是否仍 RUNNING/PAUSED；
  3. run **非活跃**（终态 / 无事件——managed 容器意味着 run 曾存在，无事件即已拆除/未知，绝不可能是活 run）→ `docker rm -f`；
  4. 无 label / 不可解析（外来容器）**永不触碰**。
  选择逻辑抽为纯函数 `plan_carrier_reaping(containers, active_run_ids)`，docker CLI 调用为可 monkeypatch 的薄封装。
- **接线**：`factory.create_backend(target, run_id=, tenant_id=)` 透传；`api/deps.py::start_run` 传入 `run_id` 与 `scoped_store.tenant_id`。

### 已知限制（记录，不在本期）
- **SSH（REMOTE）载体**：`RemoteSSHBackend` 为懒占位实现，不留可发现的远端 lease，reaper 无法关联；客户端死后服务端 sftp/ssh 进程一般超时退出，泄漏较轻。待远端载体落地时需引入远端 lease 登记表。
- **DIRECTORY（本地）载体**无外部资源，不受影响。

### 测试
`tests/test_carrier_reaper.py`（5 项）：
- run-bound backend 的 `docker run` 参数含 managed/run_id/tenant label；未绑定 backend 不打 managed 标签；
- 纯选择函数：只回收"managed 且 run 非活跃"的容器，活跃 run 保留、外来（无 label）永不触碰、run 无事件者回收；
- 集成（fake docker CLI）：终态 run 容器被 rm、活跃 run 容器保留、外来容器保留；docker 不可用时 no-op。

---

## 3. 受信边界复核

- 两处判定（tenant 派生、容器回收选择）均为**机械、确定性、只读事件流**，无 LLM 介入，符合 AGENTS.md §2.2 受信边界。
- reaper 先 `mark_orphans` 后回收，确保崩溃 run 已收敛终态；活跃 run（RUNNING/PAUSED）容器受事件流保护，不被误杀。
- 未引入超前抽象；未改 L1 存储语义；未回改归档文档。

## 4. 后续候选（未处理）
- L1 `append_event` 可加"未显式 tenant 时继承同 run 最近事件 tenant"的防御纵深（本期未做，避免影响全部调用方）。
- `mark_orphans` 性能 O(N×E) 全量 fold，万级 run 时改 SQL 级终态过滤（DESIGN 已注记）。
- review_20260722 技术债表其余项（`_write_assistant_message` 静默吞异常、API Schema 重复、fold 不截断、RateLimitGuardrail 类级 dict）待逐条核实。
