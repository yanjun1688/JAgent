# Bug: P0-3 _write_assistant_message 异常静默吞没

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P0-03 |
| 严重级别 | P0 必须修复 |
| 发现日期 | 2026-07-22 |
| 所在文件 | `harness/api/deps.py:143-144` |
| 影响范围 | 助手消息可靠性、可观测性 |
| 违反规范 | AGENTS.md §6.1 "受信组件内部异常不得泄露到非受信层，必须转换为结构化错误事件写入 Event Store" |

## 现象

`harness/api/deps.py:143-144`：

```python
        except Exception:
            pass
```

`_write_assistant_message()` 函数在写 assistant 消息失败时（EventStore 异常、conversation 不存在、db 死锁等），完全无日志输出，无错误事件，静默失败。

## 根因

实现时为了不让 assistant 消息写入失败阻塞 run cleanup，使用了 `try/except` 但异常处理留空。

## 为什么现有机制没拦住

- 无 lint rule 禁止裸 `except: pass`
- 函数体在 `cleanup_run_resources` 中通过 `asyncio.create_task` 调用，异常被 task 吞没

## 修复方案

```python
except Exception as e:
    _log.exception("Failed to write assistant message for run=%s conversation=%s: %s",
                    run_id, conversation_id, e)
```

若要求更严格：写入 `ToolFailed` 事件到 Event Store。

## 测试用例

1. Mock EventStore 使 `append_event` 抛出异常，验证日志包含 `Failed to write assistant message`
2. Integration test：关闭 db 连接后 run 结束，确认不 crash 且日志可查
