# Bug: P1-4 Logging fmtkv 占位符 arity 崩溃

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P1-04 |
| 严重级别 | P1 高优先级（崩溃级） |
| 发现日期 | 2026-08-05 |
| 所在文件 | `harness/monitoring/run_monitor.py:122,167,286` |
| 影响范围 | 任意 run 走到"Failure event / SOFT_ERROR / dedup 命中"日志路径时整个进程崩溃 |

## 现象

run 执行中日志 handler 抛异常，整条调用链被打断：

```
TypeError: not enough arguments for format string
  File "logging/handlers.py", line 73, in emit          # shouldRollover → format
  File "logging/__init__.py", line 377, in getMessage   # msg = msg % self.args
  ... 冒泡路径:
  dag_executor.py:_execute_step → tools/executor.py:execute
  → storage/event_store.py:append_event → monitoring/run_monitor.py:_on_event
  → _log_anomaly.info(...)
```

事件写入被日志异常中断，当前 run 直接失败。

## 根因

`fmtkv(**fields)` 返回**单个**格式化字符串，但 3 处日志调用格式串含多个 `%s`：

```python
_log_anomaly.debug("Failure event %s %s %s %s", fmtkv(...))  # 4 占位符 vs 1 参数
_log_anomaly.debug("SOFT_ERROR %s %s", fmtkv(...))            # 2 占位符 vs 1 参数
_log_anomaly.info("Dedup hit — skipping injection %s %s", fmtkv(...))  # 2 占位符 vs 1 参数
```

logging 的 `msg % self.args` 参数不足即抛 `TypeError`，且在 handler 层（emit）未捕获，直接向调用方冒泡。

## 为什么现有机制没拦住

- 这几条日志路径触发条件苛刻（连续失败≥3 的统计路径、dedup 命中），常规单测不覆盖
- 无任何静态检查约束"日志格式串占位符数与实参匹配"
- 之前黑盒跑全部走分支 A/B 且失败不足 3 次，未触达该路径

## 修复方案

3 处格式串改为单 `%s`（fmtkv 返回单字符串）。

## 回归防护

新增 `tests/test_log_fmtkv_arity.py`：静态扫描全库 `_log_*.info/debug/warning/error("...%s...", fmtkv(` 模式，断言格式串恰好含 1 个 `%s`，防同类再犯。
