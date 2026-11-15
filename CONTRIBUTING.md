# CONTRIBUTING

单人项目起步，按"随时会有第二个人"的标准维护。clone 之后 30 分钟内应该能跑起来。

## 环境

```bash
uv sync                    # Python 3.11 + uv；依赖锁定在 uv.lock
cp .env.example .env       # 填 LLM_* 三件套；不填则模板模式（离线可跑，诚实降级）
uv run pytest tests -q     # 全量离线套件（CI 跑的就是它；在线 e2e 默认排除）
uv run ruff check jagent tests
uv run pre-commit install  # 装钩子（commit 前自动跑上面两条）
```

Windows 注意：shell 工具的执行底座是 `bash -c`（D-037），装 Git for Windows 即可；
Linux/macOS 用系统 bash。前端面板：`cd ui && npm install && npm run build`（构建产物
`ui/dist` 不入库，不 build 只有内置单文件页）。

## 门禁（全部必过才算绿）

1. `uv run pytest tests -q`（CI 同款，全量离线）；
2. `uv run ruff check jagent tests`；
3. 平台归属：Windows cmd/bash 语义差异的用例必须带 `skipif` 与理由（先例：G-06 系列）；
4. 对本地私有路径的新引用必须在 `tests/v8_arch/test_conformance.py` 的 allowlist 登记。

CI（GitHub Actions）跑的就是 1+2，ubuntu 上执行；平台专属用例在那里自动跳过。

## 本地私有内容（D-034 re-baseline）

`docs/`（规格与台账）、`scripts/`（本地工具）、`data/`（运行时数据）、`.env` 不进公开仓库，
只存在于维护者的本地工作区。公开树上的文档引用它们的地方都在 conformance allowlist
登记过——**不要在公开文档里新增对它们的裸引用**（门禁会拦）。

## 分支与提交

- `main` 是唯一集成分支；特性工作从 main 拉分支，全绿 ff 回 main；
- commit：`type(scope): why` + 测试证据；**禁 `--no-verify`**；
- 本地 `local/full-history` 分支是完整开发史备份（公开树是压缩快照），不要推送、不要重写；
- 合并完的特性分支随手删掉（本地 + 远程），不留死指针。

## AI 并行开发协议（重要）

多个 AI 会话在同一个仓库工作时，**必须隔离，不允许共享工作区**：

1. **一个会话 = 一个 git worktree**：
   `git worktree add ../jagent-sess-b -b feat/sess-b main`
   ——worktree 各有独立工作树与 index，分支切换/stash 互不可见（同一 worktree 里
   切分支会改写别人的工作树基线，2026-11-15 事故本体）；
2. **一个会话 = 一份数据根**：起服务/跑 e2e 时各自设 `JAGENT_DATA_DIR`
   （如 `data/sess-b`）与 `JAGENT_PORT` 偏移，不共享 `events.db`；
3. **分支所有权**：只动自己的分支；别人分支上的工作树 = 别人的领地；
4. commit 前先 `git branch --show-current` 确认自己在哪里。

## 提交与反馈

- 测试与产品代码同标准：先红后绿，修 bug 必须带根因测试；
- 遇到"Agent/工具/环境反复在某处失败"，按 Harness Engineering 处理：识别缺什么
  （工具/护栏/文档/门禁）→ 把修复**沉淀进仓库**（测试、规则、文档），不停留在对话里；
- 环境差异类问题（Windows vs Linux、本地 vs CI）先跑
  `scripts/ci_check.sh`（本地 docker 预演 CI）再下结论。
