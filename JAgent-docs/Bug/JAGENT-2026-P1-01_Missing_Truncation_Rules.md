# Bug: P1-1 Missing Tool Output Truncation Rules

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P1-01 |
| 严重级别 | P1 高优先级 |
| 发现日期 | 2026-07-22 |
| 所在文件 | `harness/core/context_manager.py` |
| 影响范围 | Phase 3 上下文管理测试 — 工具截断规则缺失 (3 项) |
| 违反规范 | 测试计划 §5.1.2 工具结果截断 |

## 现象

测试计划 Phase 3 中引用 `truncate_tool_output()` 函数和 `TRUNCATION_RULES` 表，但这两个组件在当前代码库中不存在。导致以下测试失败：

- CW-U7: http_request body >2000 chars 截断
- CW-U8: http_request body <2000 chars 不截断
- CW-U9: browser text_content >1000 chars 截断
- CW-U10: file_op content >500 chars 截断
- CW-U11: 未知工具使用默认规则
- CW-U12: SOFT_ERROR 截断 error 字段
- CW-U13: 截断后保留关键字段
- CW-U14: `truncate_tool_output` 输入 None
- CW-U15: 截断规则表不可变

## 根因

测试计划设计了独立的工具输出截断层，但当前代码中截断逻辑散落在 `_build_conversation_context`（500 chars）和 `ContextManager._generate_summary`（2000 chars）中，没有统一的 `truncate_tool_output()` 函数和 `TRUNCATION_RULES` 配置表。

## 为什么现有机制没拦住

- 截断需求在架构文档中提及但未明确为独立受信组件
- 现有截断是内联实现，缺乏可测试的公共接口

## 修复方案

在 `harness/core/context_manager.py` 或新建 `harness/core/truncation.py` 中添加：

```python
TRUNCATION_RULES = {
    "http_request": {"body": 2000, "__default__": 1000},
    "browser": {"text_content": 1000, "__default__": 1000},
    "file_op": {"content": 500, "__default__": 1000},
    "__default__": {"__default__": 1000},
}

def truncate_tool_output(tool_name: str, output: dict | str | None) -> dict:
    """Apply truncation rules to tool output."""
```

## 测试用例

修复后运行 `pytest tests/test_context_window.py::TestToolOutputTruncation -v`，9 项测试应全部通过。
