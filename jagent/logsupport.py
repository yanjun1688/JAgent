"""JAgent v8 · 执行链路日志的统一语汇（D-039/D-040）。

**日志是运维观测，不是事件**（I1：真相只走事件库）。这里把"系统在做什么"
的日志收在一处：``run=`` / ``step=`` / ``session=`` 上下文一次拼装，键值字段
自动单行截断，级别语义（流转=info / 可自愈异常=warn / 终态失败=fail）统一。

摘要行之外可以附**详情块**（``blocks``）——Tool 的完整输入输出、Plan 的逐步
规划、LLM 的请求与响应原文。详情走多行缩进（前端 pre-wrap 原样渲染），
单块截断到 ``_BLOCK_LIMIT``，全文永远在事件/产物里可回放。

调用方只说发生了什么::

    from ..logsupport import blocks, event, warn, fail

    event("step started", run=run_id, step=step.id, tool=step.tool,
          args=short(args), detail=blocks([("args", args)]))
    event("model.call", purpose=purpose, ms=ms,
          detail=blocks([("prompt", prompt), ("reply", reply)]))
    fail("run FAILED", run=run_id, reason=reason)

与事件（I1）的分工：事件是可回放的真相，这里是同一时刻的运维投影——
日志行不参与 fold/重放，丢一条不影响正确性，所以允许截断与省略。
"""

from __future__ import annotations

import logging
from typing import Any

from .contracts import canonical_json

#: 执行链路统一 logger：面板与控制台同源，格式收敛在这一处。
log = logging.getLogger("jagent.chain")

#: 单字段摘要上限：一行之内能读到的长度。
_LIMIT = 220

#: 单个详情块上限：看清输入输出足够；全文在事件/产物里。
_BLOCK_LIMIT = 4000

_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


def short(value: Any, limit: int = _LIMIT) -> str:
    """日志摘要：单行化 + 截断；容器值先转 canonical JSON。"""
    if isinstance(value, (dict, list, tuple)):
        try:
            value = canonical_json(value)
        except Exception:  # noqa: BLE001 - 摘要辅助永不抛出
            value = repr(value)
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[:limit]}…({len(text)} chars)"


def blocks(*sections: tuple[str, Any], limit: int = _BLOCK_LIMIT) -> str:
    """多行详情块：``("标题", 内容)`` 逐段格式化，空内容跳过。

    输出形如::

          ── args ──
          {"path": ".."}
          ── stdout ──
          test_ok.py
    """
    out: list[str] = []
    for title, content in sections:
        if content is None or content == "" or content == {} or content == []:
            continue
        if isinstance(content, (dict, list, tuple)):
            try:
                content = canonical_json(content)
            except Exception:  # noqa: BLE001 - 摘要辅助永不抛出
                content = repr(content)
        text = str(content).rstrip()
        if len(text) > limit:
            text = f"{text[:limit]}\n…（截断 {len(text) - limit} 字符，全文见事件/产物）"
        out.append(f"    ── {title} ──\n{text}")
    return "\n".join(out)


def _fields(fields: dict[str, Any]) -> str:
    """键值串：空值不占版面，长值截断。"""
    parts = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={short(value)}")
    return " ".join(parts)


def _emit(
    level: str,
    name: str,
    *,
    session: str | None,
    run: str | None,
    step: str | None,
    exc: bool,
    detail: str | None,
    fields: dict[str, Any],
) -> None:
    """唯一格式化点：``[session=…]run=… [step=… ]<事件> <键值>`` + 缩进详情块。"""
    header = ""
    if session:
        header += f"session={session} "
    if run:
        header += f"run={run} "
    if step:
        header += f"step={step} "
    body = f"{header}{name} {_fields(fields)}".rstrip()
    if detail:
        body = f"{body}\n{detail}"
    log.log(
        _LEVELS[level],
        "%s",
        body,
        exc_info=True if exc else None,
    )


def event(name: str, *, session: str | None = None, run: str | None = None,
          step: str | None = None, detail: str | None = None, **fields: Any) -> None:
    """正常流转：意图、计划、步骤起止、通过、取消、模型调用。"""
    _emit("info", name, session=session, run=run, step=step, exc=False,
          detail=detail, fields=fields)


def warn(name: str, *, session: str | None = None, run: str | None = None,
         step: str | None = None, detail: str | None = None, **fields: Any) -> None:
    """可自愈的异常：断言失败、分类、修复、重试、模型降级。"""
    _emit("warning", name, session=session, run=run, step=step, exc=False,
          detail=detail, fields=fields)


def fail(name: str, *, session: str | None = None, run: str | None = None,
         step: str | None = None, exc: bool = False, detail: str | None = None,
         **fields: Any) -> None:
    """终态失败与第一现场：run failed、工具异常（``exc=True`` 带堆栈）。"""
    _emit("error", name, session=session, run=run, step=step, exc=exc,
          detail=detail, fields=fields)
