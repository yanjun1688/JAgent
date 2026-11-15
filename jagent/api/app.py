"""JAgent v8 · HTTP 产品面。

只暴露网页需要的几个 JSON 接口，所有文案都按「人话」返回：
不暴露事件、token、错误堆栈或内部术语。单会话 MVP，不做多用户鉴权。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue as _queue
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..adapters.mcp import McpServer
from ..app.session import TurnOutcome
from ..audit import run_verification
from ..contracts import Result
from ..metrics import run_metrics, session_metrics, session_summaries
from ..wiring import App, build_app
from .logsink import LogEntry, LogSink

logger = logging.getLogger(__name__)

#: 单文件网页；不依赖任何外部 CDN，离线可用。
STATIC_FILE = Path(__file__).parent / "static" / "index.html"

#: 构建好的控制面板。路径从本文件推出来，不依赖启动时的工作目录。
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
UI_DIST = PACKAGE_ROOT / "ui" / "dist"

WAITING_HUMAN_TEXT = "这一步需要你确认，确认后我继续。"
SERVER_ERROR_TEXT = "服务出错了，请重试"


def _sse_line(entry: LogEntry) -> str:
    payload = json.dumps(
        {
            "id": entry.id,
            "at": entry.at,
            "level": entry.level,
            "logger": entry.logger,
            "message": entry.message,
        },
        ensure_ascii=False,
    )
    return f"data: {payload}\n\n"


#: SSE 空闲心跳间隔（秒）：无新日志时发注释行，防中间层断连。
_SSE_HEARTBEAT_S = 15.0


async def sse_stream(
    sink: LogSink,
    *,
    after: int = -1,
    max_events: int | None = None,
) -> AsyncIterator[str]:
    """SSE 推流生成器：先回放存量（``after`` 之后），再实时推送每一行。

    日志落地即推（logsink 订阅广播），面板不再轮询。无新日志时按
    ``_SSE_HEARTBEAT_S`` 发心跳注释行。``max_events`` 是有界模式
    （测试/巡检用）：推满即收流；缺省无限流，客户端断开时反注册订阅。
    """
    sent = 0
    q = sink.subscribe()
    try:
        entries, cursor = sink.since(after)
        for entry in entries:
            yield _sse_line(entry)
            sent += 1
            if max_events is not None and sent >= max_events:
                return
        while True:
            try:
                entry = await asyncio.to_thread(q.get, timeout=_SSE_HEARTBEAT_S)
            except _queue.Empty:
                yield ": ping\n\n"
                continue
            if entry.id <= cursor:
                continue  # 订阅先于回放：重叠部分跳过，不重不漏
            cursor = entry.id
            yield _sse_line(entry)
            sent += 1
            if max_events is not None and sent >= max_events:
                return
    finally:
        sink.unsubscribe(q)


class ChatRequest(BaseModel):
    text: str = ""
    answers: dict[str, str] | None = None
    #: 多对话（D-034）：缺省走 s_main（向后兼容）；新会话先 POST /api/sessions。
    session_id: str = ""


class DecisionRequest(BaseModel):
    decision: str = ""
    comment: str = ""


class CancelRequest(BaseModel):
    reason: str = ""


def create_app(
    *,
    root: str | Path,
    app: App | None = None,
    model: Any = None,
    judge: Any = None,
    mcp_servers: Sequence[McpServer] | None = None,
) -> FastAPI:
    """建一个给网页用的 FastAPI 应用。

    `app` 传入已装配的 App 时直接复用；否则按 `root` 装配（默认模板/规则，离线可跑）。
    `model` 只在需要接真实模型时传入，本层不感知它的具体实现；接了模型就同时装
    T2 的 judge（`LLMJudge(model)`），否则 `build_app` 会拒绝启动。
    `mcp_servers` 非空时把 MCP 工具注册进工具层（见 `jagent.adapters.mcp`）。
    """
    if app is None and model is not None and judge is None:
        from ..agent.llm_judge import LLMJudge

        judge = LLMJudge(model)
    app_obj = (
        app
        if app is not None
        else build_app(
            Path(root), model=model, judge=judge, mcp_servers=mcp_servers
        )
    )

    api = FastAPI(title="JAgent", version="8", docs_url=None, redoc_url=None, openapi_url=None)
    api.state.jagent = app_obj
    # 进程日志环形窗口（运维观测，不是事件）：__main__ 用 attach() 接进
    # Python logging，端点只读；测试可直接往里写（见 tests/v8/test_api_logs.py）。
    api.state.log_sink = LogSink()
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @api.exception_handler(RequestValidationError)
    async def _bad_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return _error(400, "我没看懂这条请求，请换个说法再试。")

    @api.exception_handler(Exception)
    async def _unhandled(_request: Request, _exc: Exception) -> JSONResponse:
        # 堆栈只留在服务端（含日志窗口）；前端只看到一句可重试的话。
        logger.exception("unhandled error on %s", _request.url.path)
        return _error(500, SERVER_ERROR_TEXT)

    @api.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @api.post("/chat")
    def chat(payload: ChatRequest) -> JSONResponse:
        text = (payload.text or "").strip()
        if not text:
            return _error(400, "请先输入你想让我做的事。")
        session = app_obj.stack_for(payload.session_id or None).session
        logger.info(
            "chat turn start session=%s chars=%d", session.session_id, len(text)
        )
        outcome = session.handle_turn(text, payload.answers)
        logger.info(
            "chat turn done session=%s status=%s run=%s",
            session.session_id,
            outcome.status,
            outcome.run_id or "-",
        )
        approval = None
        if outcome.status == "waiting_human" and outcome.run_id:
            run = app_obj.world.runs.get(outcome.run_id)
            approval = run.approval if run is not None else None
        body = _turn_payload(outcome, approval)
        body["session_id"] = session.session_id
        return JSONResponse(body)

    @api.post("/api/sessions")
    def create_session() -> JSONResponse:
        # D-034：新建对话。创建即注册组件栈；首条消息落事件前不出现在列表。
        return JSONResponse({"session_id": app_obj.create_session()})

    @api.get("/api/sessions")
    def list_sessions() -> JSONResponse:
        return JSONResponse({"sessions": session_summaries(app_obj.store.read())})

    @api.post("/runs/{run_id}/decision")
    def decide(run_id: str, payload: DecisionRequest) -> JSONResponse:
        run = app_obj.world.runs.get(run_id)
        if run is None:
            return _error(404, "没有找到这次任务。")
        decision = (payload.decision or "").strip()
        if decision not in ("approve", "reject"):
            return _error(400, "只能选择「同意」或「拒绝」。")
        if not run.approval:
            return _error(409, "这一步已经处理过了，不需要再确认。")
        logger.info(
            "approval decision run=%s decision=%s comment_chars=%d",
            run_id,
            decision,
            len(payload.comment or ""),
        )
        try:
            outcome = app_obj.session.resume_run(run_id, decision, comment=payload.comment or "")
        except KeyError:
            return _error(404, "没有找到这次任务。")
        except RuntimeError:
            # 并发下可能被另一个请求抢先处理：对用户仍然是「已经处理过了」。
            return _error(409, "这一步已经处理过了，不需要再确认。")
        return JSONResponse(_turn_payload(outcome))

    @api.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str, payload: CancelRequest) -> JSONResponse:
        run = app_obj.world.runs.get(run_id)
        if run is None:
            return _error(404, "没有找到这次任务。")
        logger.info("run cancel requested run=%s reason=%s", run_id, payload.reason or "")
        try:
            outcome = app_obj.session.cancel_run(run_id, reason=payload.reason or "用户取消")
        except RuntimeError:
            # 终态（completed/failed）不可取消；对用户说清楚而不是 500。
            return _error(409, "这次任务已经结束，不能取消。")
        return JSONResponse(_turn_payload(outcome))

    @api.get("/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        run = app_obj.world.runs.get(run_id)
        if run is None:
            return _error(404, "没有找到这次任务。")
        result = run.result if isinstance(run.result, dict) else {}
        needs_human = run.status == "waiting_human" or bool(run.approval)
        question = _pending_question(app_obj) if needs_human and not run.approval else None
        # 批准的对象必须可见：用户批的是"要执行什么"，不是一个 hash（红队 RT-H3）。
        approval = run.approval or {}
        return JSONResponse(
            {
                "status": run.status,
                "summary": str(result.get("summary") or ""),
                "steps": [{"id": step.id, "status": step.status} for step in run.steps.values()],
                "needs_human": needs_human,
                "question": question,
                "approval": (
                    {
                        "step_id": approval.get("step_id", ""),
                        "what": approval.get("context") or approval.get("step_id", ""),
                    }
                    if approval
                    else None
                ),
            }
        )

    # ------------------------------------------------------------------ 面板 --
    # 只读的列表与增量事件流。路径统一挂在 /api 下，既避开已有的
    # /runs/{id}，也让 StaticFiles 挂到 / 时不会把这些接口吞掉。
    @api.get("/api/runs")
    def list_runs(limit: int = Query(50, ge=1, le=200)) -> JSONResponse:
        # 投影在另一线程被 Writer 改写：先持世界锁拍快照，再拼载荷（D-045）。
        snapshot = [
            (run.run_id, run.status, run.result) for run in app_obj.world.snapshot_runs()
        ]
        rows: list[dict[str, Any]] = []
        for run_id, status, result in snapshot:
            result = result if isinstance(result, dict) else {}
            rows.append(
                {
                    "run_id": run_id,
                    "status": status,
                    "summary": str(result.get("summary") or ""),
                    "updated_seq": _run_updated_seq(app_obj, run_id),
                }
            )
        rows.sort(key=lambda row: (row["updated_seq"], row["run_id"]), reverse=True)
        return JSONResponse({"runs": rows[:limit]})

    @api.get("/api/metrics/session")
    def metrics_session(session_id: str = Query("")) -> JSONResponse:
        # D-031 S2：会话级指标投影（纯派生，零采集）；可选按对话过滤（D-034）。
        events = app_obj.store.read()
        if session_id:
            events = [e for e in events if e.session_id == session_id]
        return JSONResponse(session_metrics(events))

    @api.get("/api/runs/{run_id}/metrics")
    def metrics_run(run_id: str) -> JSONResponse:
        if run_id not in app_obj.world.runs:
            return _error(404, "没有找到这次任务。")
        return JSONResponse(run_metrics(app_obj.store.read(), run_id))

    @api.get("/api/runs/{run_id}/verify")
    def verify_run(run_id: str) -> JSONResponse:
        # D-032：LLM 的结论必须可对账——声称 vs 事件证据并列，problem 由纯投影算出。
        if run_id not in app_obj.world.runs:
            return _error(404, "没有找到这次任务。")
        return JSONResponse(run_verification(app_obj.store.read(), run_id))

    @api.get("/api/runs/{run_id}/events")
    def run_events(run_id: str, after_seq: int = Query(0, ge=0)) -> JSONResponse:
        if run_id not in app_obj.world.runs:
            return _error(404, "没有找到这次任务。")
        events = app_obj.store.read(run_id=run_id, since_seq=after_seq)
        # payload 原样给前端，只摘掉审批 token：界面需要看到"批准的是什么"，
        # 但没有任何理由把可重放的凭据发给浏览器。
        body = [
            {
                "seq": event.seq,
                "type": event.type.value,
                "at": event.at,
                "payload": _public_payload(event.payload),
            }
            for event in events
        ]
        next_seq = events[-1].seq if events else after_seq
        return JSONResponse({"events": body, "next_seq": next_seq})

    @api.get("/api/logs")
    def recent_logs(after: int = Query(-1, ge=-1)) -> JSONResponse:
        """进程日志只读出口（运维观测，不是事件）：环形窗口 + 游标增量。

        内容 = uvicorn 生命周期 + 服务日志 + 执行链路（route/plan/step/
        verify/failure/repair/run 终态）+ 模型调用 + 异常堆栈；访问线不在。
        ``after=-1`` 取整个窗口，之后带上一页的 ``next`` 做增量。
        面板默认走 ``/api/logs/stream``（SSE 推流），这里是兼容/测试出口。
        """
        entries, cursor = api.state.log_sink.since(after)
        return JSONResponse(
            {
                "logs": [
                    {
                        "id": entry.id,
                        "at": entry.at,
                        "level": entry.level,
                        "logger": entry.logger,
                        "message": entry.message,
                    }
                    for entry in entries
                ],
                "next": cursor,
            }
        )

    @api.get("/api/logs/stream")
    async def stream_logs(
        after: int = Query(-1, ge=-1),
        max_events: int | None = Query(None, ge=1, le=500),
    ) -> StreamingResponse:
        """SSE 推流：连上先回放存量（``after`` 之后），再实时推送每一行。

        推流逻辑在 ``sse_stream``（模块级，可直接测试）；这里只是 HTTP 薄壳。
        """
        return StreamingResponse(
            sse_stream(api.state.log_sink, after=after, max_events=max_events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 静态页必须在所有 API 路由之后挂载，否则 "/" 会把 /healthz、/chat、
    # /runs/* 一起吃掉。构建产物存在时优先用它；否则退回内置单文件页。
    if (UI_DIST / "index.html").is_file():
        api.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
    else:

        @api.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_FILE, media_type="text/html")

    return api


# --------------------------------------------------------------------------- #
# 内部投影：只把对人有用、且不含敏感字段的部分给前端
# --------------------------------------------------------------------------- #


def _turn_payload(outcome: TurnOutcome, approval: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"status": outcome.status, "message": _message(outcome)}
    if outcome.error_code:
        body["error_code"] = outcome.error_code

    if outcome.status == "needs_input":
        question = outcome.question
        body["slot"] = question.slot if question is not None else ""
        body["choices"] = [
            {"label": option, "value": option} for option in (question.options if question is not None else ())
        ]

    if outcome.run_id:
        body["run_id"] = outcome.run_id

    # 让用户看见自己正在批什么（红队 RT-H3）：只给行为摘要，不给 token。
    if outcome.status == "waiting_human" and approval:
        body["approval"] = {
            "step_id": approval.get("step_id", ""),
            "what": approval.get("context") or approval.get("step_id", ""),
        }

    if outcome.status == "answered" and outcome.result is not None:
        body["result"] = _result_payload(outcome.result)

    # 输出分级（宪法 v2 I3）：气泡的可信档位由代码给，前端只渲染徽标，不自己推断。
    if outcome.delivery is not None:
        body["delivery"] = {
            "regime": outcome.delivery.regime.value,
            "checks": list(outcome.delivery.checks),
            "grounds": list(outcome.delivery.grounds),
            "fallback": dict(outcome.delivery.fallback),
        }

    return body


def _message(outcome: TurnOutcome) -> str:
    """气泡正文：**唯一来源是 `TurnOutcome.text`**（它已经是 delivery 文本）。

    `result.summary` 只是模板事实（RunRecord 的一部分），不再抢气泡（I8：事实与措辞分家）。
    只有 text 为空时才回退到模板摘要。
    """

    if outcome.status == "needs_input":
        return outcome.question.text if outcome.question is not None else "还需要你补充一点信息。"
    if outcome.status == "waiting_human":
        return WAITING_HUMAN_TEXT
    if outcome.status == "answered":
        if outcome.text:
            return outcome.text
        if outcome.result is not None and outcome.result.summary:
            return outcome.result.summary
        return "处理好了。"
    return outcome.text or ""


def _result_payload(result: Result) -> dict[str, Any]:
    return {
        "verdict": result.verdict.value,
        "summary": result.summary,
        "steps": [{"id": step.id, "status": step.status} for step in result.steps],
    }


def _pending_question(app_obj: App) -> dict[str, Any] | None:
    """当前挂起的问题（如果有）。审批闸口本身不是一个「问题」，所以为 None。"""
    question = getattr(app_obj.session, "_pending", None)
    if question is None:
        return None
    return {
        "text": question.text,
        "options": list(question.options),
        "slot": question.slot,
    }


def _run_updated_seq(app_obj: App, run_id: str) -> int:
    """这条 run 最后一条事件的 seq；没有事件时回 0。

    列表接口要按"最近动过"排序，而 RunState 只记了创建时的 seq，所以
    这里按 run_id 读一次增量（store 已有索引）。MVP 的 run 数量很小。
    """
    events = app_obj.store.read(run_id=run_id)
    return events[-1].seq if events else 0


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """事件 payload 给前端的投影：递归去掉审批 token，其余原样。"""
    return {
        key: _public_value(value)
        for key, value in payload.items()
        if key.lower() not in {"token", "approval_token"}
    }


def _public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _public_payload(value)
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return value


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"status": "error", "message": message})
