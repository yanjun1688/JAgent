# Bug: P1-3 TestToolOutputTruncation 事件循环冲突

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P1-03 |
| 严重级别 | P1 高优先级 |
| 发现日期 | 2026-07-22 |
| 所在文件 | `tests/test_context_window.py:129-180` |
| 影响范围 | Phase 3 工具截断测试在全量运行时失败（单独运行通过） |

## 现象

`TestToolOutputTruncation` 中 3 项测试单独运行全部通过：
```
pytest tests/test_context_window.py::TestToolOutputTruncation -v → 3 passed
```

但在全量测试中失败：
```
pytest tests/ -v → 3 failed
```

## 根因

测试使用 `asyncio.get_event_loop().run_until_complete()` 手动管理事件循环，与 pytest-asyncio 的 `asyncio_mode = auto` 冲突。当其他 async 测试先运行后，默认事件循环可能已关闭或处于不一致状态，导致 `run_until_complete()` 抛出 `RuntimeError: Event loop is closed` 或类似异常。

## 为什么现有机制没拦住

- pytest-asyncio 配置为 `auto` 模式，自动将 async def 测试函数包装为协程
- `TestToolOutputTruncation` 中的测试方法是同步 `def`，内部手动调用 `asyncio.get_event_loop().run_until_complete()`
- 这种混合模式在全量运行时产生事件生命周期冲突

## 修复方案

将 `TestToolOutputTruncation` 中的测试改为 `async def`，由 pytest-asyncio 自动管理事件循环：

```python
async def test_conversation_context_truncates_long_content(self, store):
    await store.upsert_conversation("conv-1", "Test")
    ...
    ctx = await _build_conversation_context(store, "conv-1")
    assert len(ctx) <= 510
```

## 测试用例

修复后运行 `pytest tests/ -v`，3 项测试应全部通过。
