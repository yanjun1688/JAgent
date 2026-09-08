<!--
PR 合并前请确认。标题建议 Conventional Commits：feat / fix / refactor / docs / chore / test。
一条独立改动 = 一个分支 = 一个 PR；不同主题不要混在同一 PR。
-->

## 变更内容
<!-- 这次改了什么、为什么改。Bug 修复必须回答 AGENTS.md §3.5 三问：
     1) 根因  2) 为什么现有机制没拦住  3) 如何防止复发（受信层通用防护 + 回归测试） -->

## 关联
- PRD / DESIGN / ADR / Issue：<!-- 链接或编号；ADR-012/013 等「待评审」决策需显式确认 -->

## 受信边界自查（AGENTS.md §2.2 / §6.3）
- [ ] 受信判定（门控 / skip / 恢复边界 / 证据 / 幂等 / guardrails）无 LLM 介入
- [ ] 副作用只发生在 Tool Layer；未绕过确认 / guardrails / 幂等
- [ ] 新增/修改事件类型已在后端枚举与前端（OpenAPI/TS）同步
- [ ] 受信层异常转为结构化错误事件，未泄漏到非受信层

## 测试（TDD：先红后绿）
- [ ] 已追加根因场景回归测试
- [ ] `uv run pytest -q -p no:cacheprovider` 通过（贴 `X passed / Y skipped`）：
- [ ] `uv run ruff check harness tests scripts` 通过
- [ ] 测试/代码不依赖机器绝对路径或特定工作目录（CI 在 Linux 干净 runner 执行）
- [ ] 涉及前端契约时已重新生成 OpenAPI/TS 类型

## 文档
- [ ] 已同步相关 DESIGN / ADR / review 记录；归档文档（archive/）不回改
