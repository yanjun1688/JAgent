"""JAgent v8 · 进程内日志环形缓冲（/api/logs 的数据源）。

**日志是运维观测，不是事件**（I1：真相只走事件库）——本模块零写事件、
零落盘：进程重启即清空，只保留一个"当前控制台"窗口。uvicorn 访问线、
服务生命周期、模型调用、异常堆栈从 Python logging 流进来，HTTP 只读。

游标语义与事件流同款：``since(after)`` 返回 id > after 的条目 + 新游标；
id 单调递增，环形满员后丢最老（窗口语义，不是历史归档）。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

#: 环形窗口大小：排障足够，内存有界（约 2000 行文本）。
CAPACITY = 2000


@dataclass(frozen=True)
class LogEntry:
    id: int
    at: float  # unix 秒（与事件 `at` 同语义，前端负责格式化）
    level: str
    logger: str
    message: str


class LogSink:
    """线程安全的日志环形缓冲。

    append 由 ``LogSinkHandler``（uvicorn/应用日志）与端点测试直接调用；
    ``since`` 是 /api/logs 的唯一读取面。
    """

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_id = 0

    def append(self, *, level: str, logger: str, message: str) -> int:
        with self._lock:
            entry = LogEntry(
                id=self._next_id,
                at=time.time(),
                level=level,
                logger=logger,
                message=message,
            )
            self._entries.append(entry)
            self._next_id += 1
            return entry.id

    def since(self, after: int) -> tuple[list[LogEntry], int]:
        """id > after 的条目 + 新游标；窗口空时游标原样返回。"""
        with self._lock:
            entries = [entry for entry in self._entries if entry.id > after]
            cursor = self._entries[-1].id if self._entries else after
        return entries, cursor


class LogSinkHandler(logging.Handler):
    """logging.Handler 桥：把 logging 记录投进 LogSink；自身永不抛出。"""

    def __init__(self, sink: LogSink, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n" + "".join(
                    traceback.format_exception(*record.exc_info)
                )
            self.sink.append(
                level=record.levelname, logger=record.name, message=message
            )
        except Exception:  # noqa: BLE001 - 日志桥失败时按 logging 约定兜底
            self.handleError(record)


def attach(sink: LogSink, *, level: int = logging.INFO) -> Callable[[], None]:
    """把 sink 接进进程日志，返回 detach（测试与关停时用）。

    约定：uvicorn 以 ``log_config=None`` 启动，uvicorn.error / uvicorn.access
    未被 dictConfig 配置、沿 propagate 流到 root——所以 handler 只挂 root 一处，
    不重复挂子 logger（避免双写）。同时保证控制台仍有一条 stderr 输出。
    幂等：同一 sink 重复 attach 不产生重复 handler。
    """
    handler = LogSinkHandler(sink, level=level)
    root = logging.getLogger()

    existing = next(
        (
            existing
            for existing in root.handlers
            if isinstance(existing, LogSinkHandler) and existing.sink is sink
        ),
        None,
    )
    if existing is not None:
        return lambda: None

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )

    previous_level = root.level
    root.addHandler(handler)
    root.addHandler(console)
    if root.level > level:
        root.setLevel(level)

    def detach() -> None:
        root.removeHandler(handler)
        root.removeHandler(console)
        root.setLevel(previous_level)

    return detach
