# Contributing — JAgent / Harness

协作总则（受信边界、分层 L1–L7、TDD、§3.4 开发前三步审查、§3.5 根治原则）见
[`AGENTS.md`](./AGENTS.md)，本文件只固化**工程流程约定**。

## 1. 一条改动 = 一个分支 = 一个 PR

- 从最新 `main` 切分支，独立提交、独立评审、独立合并、可独立回滚；不同主题不混在同一分支/PR。
- 分支命名（语义前缀）：
  - `feat/<scope>` 新功能
  - `fix/<scope>` 缺陷修复
  - `review/<scope>` 受信边界/架构审查驱动的修复
  - `chore/<scope>` 工程治理（CI、依赖、文档结构）
  - `docs/<scope>` 纯文档
- 提交信息用 Conventional Commits：`feat|fix|refactor|docs|chore|test(scope): …`。

## 2. 并行开发必须用独立 worktree（多会话/多 Agent 时强制）

git 分支只隔离**已提交历史**，**未提交改动属于工作目录、不随分支隔离**。两个会话在同一目录切不同分支，改动会混在一起。并行工作请为每条线开独立工作树：

```powershell
# 从 main 为某主题新建独立目录 + 新分支
git worktree add D:\JAgent-<topic> -b <type>/<topic> main

# 查看 / 清理
git worktree list
git worktree remove D:\JAgent-<topic>      # 删除 worktree
git worktree prune                          # 清理失效登记
```

- 每个会话只在自己的 worktree 目录里改代码，**禁止在别人正在用的目录里动手**。
- 开发、跑测试、提交都在各自目录完成；合并统一走 GitHub PR。
- 分支间文件不交叉；确有依赖时，先合并前置分支再 rebase。

## 3. 测试可移植性（CI 在 Linux 干净 runner 上跑）

- **禁止在测试中写死机器绝对路径**（如 `D:/Project/JAgent/...`、`/home/...`）。
  - 工作区根用**相对路径**（按进程 CWD 解析），如 `data/workspaces/...`；
  - 需要临时目录一律用 pytest 的 `tmp_path`。
- 不依赖特定平台、不依赖某个已存在的本地数据库/服务；外部进程（docker、playwright、MCP）必须可 mock/skip。

## 4. 本地与 CI 的统一命令

```bash
uv sync --frozen                          # 安装项目 + dev 依赖
uv run ruff check harness tests scripts   # Lint（pre-commit 与 CI 同一范围）
uv run pytest -q -p no:cacheprovider      # 全量测试
```

- 已安装 pre-commit：提交时自动跑 ruff、`test_bug_summary.py`、全量 pytest。
  钩子是辅助，**不可用 `--no-verify` 绕过真正失败**；最终以 CI 为准。
- 全量基线请记录在 PR（`X passed / Y skipped`）。

## 5. TDD 与文档同步

- 缺陷/受信层改动严格 TDD：先写复现/根因回归测试（红）→ 实现（绿）→ 全量回归。
- 代码与文档冲突时**以代码为准**，但必须回报差异并在同一 PR 修正文档
  （DESIGN/ADR/review）；`archive/` 为历史归档，不回改。
- 新增/修改事件类型或 API：以后端 Pydantic Model 为唯一来源，重新生成 OpenAPI/前端 TS 类型。

## 6. CI 与分支保护

- 每个对 `main` 的 PR 触发 GitHub Actions（`ruff` + `pytest`），必须通过才能合并。
- `main` 启用分支保护：禁止直推，要求 CI 通过 + 评审。
