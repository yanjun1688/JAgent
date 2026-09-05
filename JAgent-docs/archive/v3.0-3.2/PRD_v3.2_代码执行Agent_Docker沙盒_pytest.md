# PRD v3.2: 代码执行 Agent — 基于 Docker 沙盒的 pytest 生成与运行

> **版本**: v3.2
> **状态**: Draft — 未审查 / 未做
> **日期**: 2026-07-27
> **产品经理**: （待定）
> **适用组件**: `harness/tools/sandbox.py`, `harness/tools/code_runner.py`, `harness/core/system_prompt.py`, `harness/tools/guardrails.py`
> **前置依赖**: PRD v3.0 Phase 1（上下文压缩与剪枝优化）

---

## 1. 执行摘要

### 1.1 当前问题

JAgent 当前已有 `file_op` 和 `Sandbox` 工具，可以读写文件和在子进程中执行命令，但缺少一套**面向代码生成与测试执行的受控闭环**：

- Agent 生成代码后，直接在宿主环境运行，缺乏隔离
- Windows 本地开发与 Linux 生产环境行为不一致
- 无法安全地执行 pytest 等测试框架
- 测试失败后的分析依赖 Agent 自主理解原始输出

### 1.2 产品目标

> 让 Agent 能够在**受控 Docker 沙盒**中生成 pytest 测试脚本、执行测试、分析结果，并迭代修复。

本 PRD 是一个**最小可行体验（MVE）**，不追求替代测试工程师，而是验证 LLM 在沙盒环境中安全写代码、跑测试、看懂结果的可行性。

### 1.3 关键约束

- 本地开发环境：Windows（已有 Docker Desktop）
- 生产环境：Linux + Docker
- 必须跨平台一致：同一套代码在 Windows 本地和 Linux 生产都能跑
- 代码执行必须隔离，禁止影响宿主项目代码

---

## 2. 用户场景

| # | 场景 | 当前表现 | 期望表现 |
|---|---|---------|---------|
| US-1 | 测试工程师想为一个函数生成 pytest 用例 | 手动编写，重复劳动 | Agent 读取函数后自动生成 pytest 脚本并在沙盒运行 |
| US-2 | 生成的测试脚本运行失败 | 需要人工查看错误、修改脚本 | Agent 读取 stderr，自动修复并重新运行 |
| US-3 | 担心 Agent 写代码会破坏项目 | 不敢让 Agent 执行 | Agent 代码只写入隔离的 workspace/，项目代码只读 |
| US-4 | 本地 Windows 运行，但生产是 Linux | 环境差异导致脚本行为不一致 | 统一在 Docker 容器内执行，消除环境差异 |
| US-5 | 需要审计 Agent 写过什么、运行过什么 | 无记录 | 所有代码写入、命令执行、测试结果写入 EventStore |

---

## 3. 范围声明

### 3.1 本 PRD 做

- Docker 容器作为代码执行沙盒
- Agent 生成 pytest 脚本并运行
- 脚本写入路径限制在 `workspace/`
- 测试结果结构化返回
- 失败后自动迭代修复（限制次数）
- 命令白名单、超时控制、资源限制
- 网络默认禁止

### 3.2 本 PRD 不做

- 不替代 IDE 或 Cursor 的完整代码编辑能力
- 不实现多文件项目级重构
- 不实现 pip 包自动安装管理（MVE 依赖预装在镜像中）
- 不实现复杂网络白名单（默认无网络，后续可扩展）
- 不实现 git 自动提交
- 不做 UI 界面
- 不做跨 Run 的测试历史沉淀

---

## 4. 沙盒架构设计

### 4.1 容器执行模型

每次代码执行对应一个临时 Docker 容器：

```
docker run --rm \
  --network none \
  --cpus="1" \
  --memory="512m" \
  -v $(pwd)/workspace:/app/workspace:rw \
  -v $(pwd)/project_code:/app/project:ro \
  -w /app/workspace \
  python:3.11-slim \
  python -m pytest workspace/tests/test_xxx.py -v
```

**设计要点**：
- `--rm`：执行完立即销毁容器
- `--network none`：默认无网络
- 项目代码只读挂载，workspace 读写挂载
- 工作目录固定为 `/app/workspace`

### 4.2 目录权限

| 目录 | 容器内路径 | 权限 | 用途 |
|------|-----------|------|------|
| Agent workspace | `/app/workspace` | 读写 | Agent 生成测试脚本、输出结果 |
| 被测项目代码 | `/app/project` | 只读 | Agent 读取待测试的代码 |
| 容器临时文件 | `/tmp` | 读写 | 容器内部临时使用 |

### 4.3 网络策略

- **默认**：`--network none`，容器无法访问外部网络
- **白名单模式（后续扩展）**：创建自定义 Docker 网络，仅允许访问白名单域名/IP
- MVE 阶段不实现动态白名单切换，只有"无网络"一种模式

### 4.4 资源限制

| 资源 | 默认值 | 可配置 |
|------|--------|--------|
| CPU | 1 核 | 是 |
| 内存 | 512 MB | 是 |
| 执行超时 | 60 秒 | 是 |
| 磁盘写入 | 不限制（依赖 workspace 大小） | 否（MVE） |

---

## 5. 功能需求

### 5.1 新增工具：code_runner

**工具名称**: `code_runner`

**职责**:
1. 把 Agent 生成的代码写入 `workspace/`
2. 在 Docker 容器内执行指定命令
3. 返回结构化执行结果

**关键参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `filename` | str | 是 | 相对于 workspace/ 的文件路径 |
| `code` | str | 是 | 要写入的代码内容 |
| `command` | str | 是 | 在容器内执行的命令，如 `python -m pytest ...` |
| `timeout` | int | 否 | 执行超时，默认 60 秒 |

**关键输出**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | bool | 命令是否成功（exit_code == 0） |
| `exit_code` | int | 进程退出码 |
| `stdout` | str | 标准输出 |
| `stderr` | str | 标准错误 |
| `workspace_files_changed` | list[str] | 本次写入/修改的 workspace 文件 |
| `duration_ms` | int | 执行耗时 |

### 5.2 安全校验（Tool Layer 强制）

`code_runner` 在执行前必须经过以下校验，Agent 无法绕过：

| 校验项 | 行为 |
|--------|------|
| 路径校验 | `filename` 解析后必须落在 `workspace/` 内，禁止 `../` 等越界 |
| 命令白名单 | 只允许 `python -m pytest`、`python`、`python3` 等预设命令 |
| 危险模式拦截 | 禁止 `rm -rf`、`format`、`fdisk`、`mkfs` 等破坏性命令 |
| 网络校验 | 容器启动时默认 `--network none` |
| 超时控制 | 超过 `timeout` 强制 `SIGKILL` |
| 资源限制 | Docker 启动参数限制 CPU/内存 |

### 5.3 Agent 闭环

```
用户：帮我对 /app/project/user_service.py 的 login 函数写一组 pytest 用例
    ↓
Agent 读取被测代码（file_op 或 code_runner 读项目代码）
    ↓
Agent 生成 pytest 脚本 → 调用 code_runner
    ↓
code_runner 写入 workspace/tests/test_user_service_login.py
    ↓
code_runner 启动 Docker 容器执行 pytest
    ↓
返回 stdout/stderr/exit_code
    ↓
Agent 分析结果：
    ├── 成功 → 总结通过/失败用例数
    ├── 失败 → 分析错误类型，决定修复
    └── 修复后再次调用 code_runner（最多 3 次迭代）
    ↓
返回最终报告给用户
```

### 5.4 迭代修复

当 pytest 失败时，Agent 可以：

1. 读取 `stderr` 和失败用例信息
2. 调用 `code_runner` 覆盖或修改测试脚本
3. 重新运行测试
4. 限制最多 3 次迭代，防止无限循环

迭代次数由 `SchedulerConfig` 或 Tool Definition 控制。

### 5.5 测试结果分析

Agent 看到的测试结果需要结构化，便于分析：

```xml
<test_result>
  <summary passed="3" failed="1" errors="0" skipped="0" />
  <failures>
    <failure test="test_login_with_wrong_password">
      <message>AssertionError: expected 401, got 200</message>
      <traceback>...</traceback>
    </failure>
  </failures>
</test_result>
```

MVE 阶段可通过 system prompt 要求 LLM 自行从 pytest 输出中提取，不强制做结构化解析器。

---

## 6. 与现有架构的关系

### 6.1 复用现有能力

| 现有组件 | 用途 |
|---------|------|
| `ToolExecutor` 8 步流程 | 安全校验、超时、幂等、事件写入 |
| `Sandbox` | 改造为 Docker 执行后端 |
| `GuardrailRunner` | 拦截危险命令和路径越界 |
| `RunMonitor` | 检测连续失败，注入反馈 |
| `EventStore` | 审计代码写入、命令执行、测试结果 |
| `fold.py` | 在 RunState 中记录测试执行结果 |

### 6.2 新增/改造文件

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `harness/tools/sandbox.py` | 改造 | 增加 Docker 执行后端，保留原有 subprocess 降级路径 |
| `harness/tools/code_runner.py` | 新增 | code_runner 工具实现 |
| `harness/tools/guardrails.py` | 扩展 | 增加代码执行相关 Guardrail 规则 |
| `harness/core/system_prompt.py` | 扩展 | 增加代码生成与测试分析 prompt |
| `harness/models/tools.py` | 可能扩展 | 如需要新增工具契约字段 |

---

## 7. 安全边界

### 7.1 默认安全策略

- **隔离**：代码执行在 Docker 容器，与宿主隔离
- **只写 workspace/**：Agent 生成文件只能落在 workspace/
- **只读项目代码**：被测代码只读挂载
- **无网络**：默认 `--network none`
- **命令白名单**：禁止任意 shell 命令
- **资源限制**：CPU/内存/超时
- **审计**：所有写入和执行写入 EventStore

### 7.2 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Agent 写入恶意代码 | 中 | 容器隔离 + 只写 workspace |
| Agent 执行破坏性命令 | 高 | 命令白名单 + Guardrails |
| 容器逃逸 | 低 | 禁止 `--privileged`，不挂载 Docker socket |
| 资源耗尽 | 中 | CPU/内存/超时限制 |
| 无限迭代修复 | 中 | 最多 3 次迭代限制 |
| 本地 Windows 路径问题 | 中 | Docker 容器内统一 Linux 路径 |

---

## 8. 非功能需求

### 8.1 性能

| 指标 | 要求 |
|------|------|
| 容器启动 + 执行简单 pytest | < 10 秒 |
| 单次 code_runner 调用总延迟 | < 15 秒（不含 LLM 生成时间） |
| 迭代修复总轮数 | ≤ 3 次 |

### 8.2 兼容性

- 本地 Windows + Docker Desktop 可用
- 生产 Linux + Docker 可用
- 不强制要求特定 Python 版本（由镜像决定）
- 如果 Docker 不可用，保留 fallback 到 subprocess + workspace 限制（MVE 阶段可选）

### 8.3 可观测性

- 每个 code_runner 调用写入 `ToolCalled` + `ToolCompleted`/`ToolFailed` 事件
- stdout/stderr 写入事件 payload
- 容器启动参数写入日志

---

## 9. 验收标准

### 9.1 功能验收

- [ ] Agent 能为一个简单 Python 函数生成可运行的 pytest 脚本
- [ ] 脚本只生成在 `workspace/` 目录内
- [ ] 测试在 Docker 容器内执行
- [ ] 默认情况下容器无网络访问
- [ ] 成功/失败结果以结构化形式返回
- [ ] 失败时 Agent 能自动尝试修复并重新运行
- [ ] 不破坏项目原始代码

### 9.2 安全验收

- [ ] 命令白名单能拦截危险命令
- [ ] 路径越界请求被 Guardrails 拒绝
- [ ] 超时后容器被强制终止
- [ ] 所有写入和执行操作写入 EventStore

### 9.3 回归验收

- [ ] 现有 719 项测试继续通过
- [ ] 新增 code_runner 相关测试覆盖正常/失败/安全拦截场景

---

## 10. 边界声明（本 PRD 不做）

- 不做 IDE 级代码编辑能力
- 不做多文件项目级重构
- 不做 pip 包自动安装管理
- 不做复杂网络白名单动态切换
- 不做 git 自动提交
- 不做前端 UI
- 不做跨 Run 的测试历史沉淀
- 不做多 Agent 协作测试

---

## 11. 与 PRD v3.0 Phase 1/2 的关系

| PRD | 关系 |
|-----|------|
| PRD v3.0 Phase 1 | 本 PRD 的前置依赖，提供长任务上下文稳定性 |
| PRD v3.0 Phase 2 | 与本 PRD 平行，记忆系统未来可辅助 code_runner 记住常见错误模式 |

---

## 12. 待决策问题

1. Docker 镜像用官方 `python:3.11-slim` 还是项目自定义镜像？
2. 是否需要在镜像中预装 `pytest` + 常见依赖？
3. workspace/ 目录是否纳入 `.gitignore`？
4. 是否保留 subprocess fallback？
5. code_runner 是一个工具还是拆分为 `code_writer` + `code_runner`？

---

## 13. 术语表

| 术语 | 定义 |
|------|------|
| **code_runner** | Agent 生成代码并在沙盒执行的工具 |
| **Docker 沙盒** | 使用 Docker 容器隔离代码执行环境 |
| **workspace/** | Agent 可读写的工作目录，与项目代码隔离 |
| **命令白名单** | 允许 Agent 执行的命令列表 |
| **pytest** | Python 测试框架 |

---

## 14. 引用资料

1. JAgent AGENTS.md v2.1 — 受信边界约束
2. JAgent ARCHITECTURE_v2.1.md — 整体架构
3. PRD v3.0 Phase 1 — 上下文压缩与剪枝优化
4. Docker Security Best Practices

---

**状态说明**：本文档为 Draft 状态，**未经过架构审查，未进入开发排期**。技术实现细节（API 契约、Docker 启动参数、Guardrail 规则）由后续 `ARCHITECTURE_v3.2.md` 定义。
