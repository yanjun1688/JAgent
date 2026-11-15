"""JAgent v8 · 进程内日志环形缓冲（/api/logs 与 /api/logs/stream 的数据源）。

**日志是运维观测，不是事件**（I1：真相只走事件库）——本模块零写事件、
零落盘：进程重启即清空，只保留一个"当前控制台"窗口。uvicorn 生命周期、
服务日志、执行链路（route → plan → step → verify → failure → repair →
run 终态）从 Python logging 流进来，HTTP 只读。

两种出口：
- ``GET /api/logs?after=``：轮询增量（兼容/测试用）；
- ``GET /api/logs/stream``：SSE 推流（面板默认）——``subscribe()`` 提供实时
  广播，日志落地即推，不再轮询。

uvicorn 的访问线（``uvicorn.access``）不进窗口：那是面板自己刷新自己的
噪音，不是系统在做什么。控制台 stderr 仍保留完整访问线。
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

#: 环形窗口大小：排障足够，内存有界（约 2000 行文本）。
CAPACITY = 2000

#: 面板里排除的 logger：访问线是自指噪音（D-039）。
_EXCLUDED_LOGGERS = ("uvicorn.access",)


@dataclass(frozen=True)
class LogEntry:
    id: int
    at: float  # unix 秒（与事件 `at` 同语义，前端负责格式化）
    level: str
    logger: str
    message: str


class LogSink:
    """线程安全的日志环形缓冲 + 实时订阅广播。"""

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_id = 0
        self._subscribers: list[queue.Queue[LogEntry]] = []

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
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(entry)
            except queue.Full:  # pragma: no cover - 无界队列不会满；兜底不抛
                pass
        return entry.id

    def since(self, after: int) -> tuple[list[LogEntry], int]:
        """id > after 的条目 + 新游标；窗口空时游标原样返回。"""
        with self._lock:
            entries = [entry for entry in self._entries if entry.id > after]
            cursor = self._entries[-1].id if self._entries else after
        return entries, cursor

    def subscribe(self) -> queue.Queue[LogEntry]:
        """注册一个实时订阅队列（无界；消费方负责及时取）。"""
        q: queue.Queue[LogEntry] = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[LogEntry]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


class _PanelFilter(logging.Filter):
    """面板过滤：访问线不进窗口（控制台不受影响）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name not in _EXCLUDED_LOGGERS


class LogSinkHandler(logging.Handler):
    """logging.Handler 桥：把 logging 记录投进 LogSink；自身永不抛出。"""

    def __init__(self, sink: LogSink, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.sink = sink
        self.addFilter(_PanelFilter())

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
    不重复挂子 logger（避免双写）。访问线由 handler 内的 filter 排除。
    同时保证控制台仍有一条 stderr 输出。
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
