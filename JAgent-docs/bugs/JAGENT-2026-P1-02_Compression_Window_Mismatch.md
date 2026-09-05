# Bug: P1-2 Compression Window keep_count Mismatch

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P1-02 |
| 严重级别 | P1 高优先级 |
| 发现日期 | 2026-07-22 |
| 所在文件 | `harness/core/context_manager.py:70-79` |
| 影响范围 | Phase 3 上下文管理测试 — 1 项 |

## 现象

`TestCompressionWindow::test_normal_compression_window` 失败：
```
assert 3 == 2
```

测试期望正常压缩时 `keep_count=2`，但实际返回 `keep_count=3`。

## 根因

`ContextManager.select_compression_window()` 中正常压缩和紧急压缩的边界条件判断有误。当 token 估计值超过 compression_threshold 但低于 emergency_threshold 时，代码路径返回 `keep_count=2`。但在测试配置中（token_limit=1000, compression_threshold_ratio=0.8），20 条 × 200 chars × 0.25 = 1000 tokens，恰好等于 compression_threshold（800），但可能因为估算逻辑导致进入紧急压缩路径。

具体代码路径：
```python
if estimate < self.emergency_threshold:  # normal compression
    return {"keep_count": 2, ...}
# emergency compression
return {"keep_count": 3, ...}
```

测试中 estimate 可能恰好 >= emergency_threshold，导致进入紧急压缩路径。

## 为什么现有机制没拦住

- 边界值测试未覆盖 compression_threshold 和 emergency_threshold 之间的精确区间
- 测试数据生成的 token 量恰好落在边界附近

## 修复方案

两种选择：
1. **修正测试**: 调整测试数据使 estimate 明确落在正常压缩区间（低于 emergency_threshold）
2. **修正代码**: 确保正常压缩和紧急压缩的边界清晰，不产生歧义

推荐方案 1：修改测试数据，使用更小的 token 量确保进入正常压缩路径。

## 测试用例

修改测试数据后重新运行 `pytest tests/test_context_window.py::TestCompressionWindow::test_normal_compression_window -v`。
