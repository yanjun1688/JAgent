"""JAgent v8 · 一步执行。

Executor 的职责只有一个：把「一次工具调用」变成「一条可回放的事实」。
四条规则决定了这里的形状：

* **先算身份，再发 STEP_STARTED。** `idempotency_key` 必须在调用之前落盘，
  否则进程死在调用中途时，日志里没有「这一步开始了」的证据。
* **工具异常不是进程崩溃。** 捕获成 `ToolResult(ok=False, error_code="tool_exception")`，
  让失败分类器去判是不是可以重试——抛穿会让一次坏调用毁掉整个 run 的账。
* **全量进 artifact，截断进观测。** 事件要小（stdout/stderr 截到 4000 字符），
  证据要全（artifact 里是原文，按 sha256 寻址）。
* **非零退出码不是错误。** 它和 stdout/stderr 一样只是观测，判不判由断言决定。

Executor 不判 pass/fail，也不读 `step.verify`。断言属于 verifier。
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

from ..contracts import (
    EventType,
    Step,
    StepOutcome,
    ToolResult,
    canonical_json,
    digest_of,
)
from .artifacts import ArtifactStore
from .contract_snapshot import verify as verify_contract
from .registry import ToolRegistry
from .writer import Writer

#: 观测里 stdout/stderr 保留的字符数。全文在 artifact 里。
_OBS_LIMIT = 4000

#: 步骤间取值：`{{step_id.path.to.value}}`。整串是占位符时保留原类型，
#: 嵌在长字符串里时按字符串替换（例如 shell 命令拼参数）。
_BINDING = re.compile(r"\{\{\s*([A-Za-z0-9_\-]+)\.([^{}]+?)\s*\}\}")


class BindingError(Exception):
    """占位符解析不到值；这一步直接判失败，不把占位符发给工具。"""


def _idempotency_key(run_id: str, step_id: str, args_hash: str) -> str:
    """`sha256(run_id | step_id | args_hash)`：一步的身份只由这三样决定。"""
    material = f"{run_id}|{step_id}|{args_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    """把工具返回的 data 收敛成可 JSON 的值。

    观测要写进事件，事件要能序列化；一个返回了 `Path` 的工具不该让整条日志写不下去。
    认不出的值降级成 `repr`——宁可观测里是字符串，也不要事件写一半。
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # NaN/inf 在 JSON 里不可表示（BUG2-10）：观测收敛成 null——
        # 下游看到"没有值"，而不是一条写不进日志、投不出 API 的事件。
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return repr(value)


def _json_text(data: dict[str, Any]) -> str:
    try:
        return canonical_json(data)
    except (TypeError, ValueError):  # pragma: no cover - _jsonable 已经先收过一遍
        return repr(data)


def _observations(result: ToolResult, data: dict[str, Any]) -> dict[str, Any]:
    """断言能看到的全部事实：状态 + 退出码 + 截断后的输出 + 工具自己的 data。"""
    observations: dict[str, Any] = {
        "ok": bool(result.ok),
        "exit_code": result.exit_code,
        "stdout": (result.stdout or "")[:_OBS_LIMIT],
        "stderr": (result.stderr or "")[:_OBS_LIMIT],
    }
    observations.update(data)
    return observations


def _contains_binding(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_BINDING.search(value))
    if isinstance(value, dict):
        return any(_contains_binding(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_binding(item) for item in value)
    return False


def _resolve_path(
    observations: dict[str, dict[str, Any]], step_id: str, path: str
) -> Any:
    """按 `step_id.a.b.0.c` 从观测里取值；取不到就抛 BindingError。"""
    if step_id not in observations:
        raise BindingError(f"unknown or unfinished step {step_id!r}")
    current: Any = observations[step_id]
    walked = step_id
    for part in path.split("."):
        walked = f"{walked}.{part}"
        if isinstance(current, dict):
            if part not in current:
                # 唯一容器自动下钻：MCP 常见 `{"item": [...]}` 包裹，
                # 模型写 `data.0` 时按 `data.item.0` 解析。只有唯一候选才下钻，
                # 多个容器时保持 fail-closed（不猜）。
                containers = [
                    key for key, value in current.items() if isinstance(value, (list, tuple))
                ]
                if len(containers) == 1 and part.isdigit():
                    current = current[containers[0]]
                    try:
                        current = current[int(part)]
                    except IndexError:
                        raise BindingError(f"{walked} is not a valid list index") from None
                    continue
                available = ", ".join(sorted(str(key) for key in current)[:12]) or "（空）"
                raise BindingError(f"{walked} not found; available: {available}")
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                raise BindingError(f"{walked} is not a valid list index")
        else:
            raise BindingError(f"{walked} is not reachable ({type(current).__name__})")
    return current


def _bind_value(value: Any, observations: dict[str, dict[str, Any]]) -> Any:
    """整串占位符保留原类型；嵌在长字符串里按字符串替换。"""
    if isinstance(value, str):
        whole = _BINDING.fullmatch(value.strip())
        if whole is not None:
            return _resolve_path(observations, whole.group(1), whole.group(2))
        return _BINDING.sub(
            lambda match: str(_resolve_path(observations, match.group(1), match.group(2))),
            value,
        )
    if isinstance(value, dict):
        return {key: _bind_value(item, observations) for key, item in value.items()}
    if isinstance(value, list):
        return [_bind_value(item, observations) for item in value]
    return value


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        artifacts: ArtifactStore,
        writer: Writer,
        *,
        workspace: Path,
    ) -> None:
        self.registry = registry
        self.artifacts = artifacts
        self.writer = writer
        self.workspace = Path(workspace)

    # ------------------------------------------------------------------ api --
    def run_step(self, run_id: str, step: Step, attempt: int = 1) -> StepOutcome:
        raw_args = dict(step.args or {})
        args_hash = digest_of(raw_args)
        idempotency_key = _idempotency_key(run_id, step.id, args_hash)

        self.writer.emit(
            EventType.STEP_STARTED,
            {"step_id": step.id, "attempt": attempt, "idempotency_key": idempotency_key},
            run_id=run_id,
            step_id=step.id,
        )

        # 缺口 4：契约快照复核——冻结后工具被换（契约变了）→ 派发即拒，工具不跑。
        frozen = self.writer.world.runs[run_id].tool_snapshot
        if frozen and not verify_contract(frozen, self.registry.specs()):
            result = ToolResult(ok=False, error_code="tool_contract_changed")
        else:
            try:
                args = self._bind_args(run_id, raw_args)
                result = self._invoke(step, args)
            except BindingError as exc:
                result = ToolResult(
                    ok=False, error_code="invalid_args", error_message=f"binding failed: {exc}"
                )
        data = _jsonable(result.data or {})
        artifact_refs = self._write_artifacts(run_id, step.id, result, data)
        observations = _observations(result, data)

        self.writer.emit(
            EventType.STEP_FINISHED,
            {
                "step_id": step.id,
                "status": "ok" if result.ok else "failed",
                "error_code": result.error_code,
                "artifact_refs": list(artifact_refs),
                "observations": observations,
                "attempt": attempt,
                "error_message": result.error_message,
            },
            run_id=run_id,
            step_id=step.id,
        )

        return StepOutcome(
            ok=result.ok,
            observations=observations,
            artifact_refs=artifact_refs,
            error_code=result.error_code,
            error_message=result.error_message,
            attempt=attempt,
        )

    # -------------------------------------------------------------- helpers --
    def _bind_args(self, run_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """把 `{{step_id.path}}` 换成前序步骤的观测值。

        数据源是事件日志里的 STEP_FINISHED（不是内存），所以恢复后重跑同一步
        也能解析；取不到就抛 `BindingError`，让这一步以 invalid_args 失败。
        """
        if not _contains_binding(args):
            return args
        observations = self._observations_by_step(run_id)
        return _bind_value(args, observations)

    def _observations_by_step(self, run_id: str) -> dict[str, dict[str, Any]]:
        by_step: dict[str, dict[str, Any]] = {}
        for event in self.writer.store.read(run_id=run_id):
            if event.type is not EventType.STEP_FINISHED:
                continue
            step_id = event.payload.get("step_id")
            if isinstance(step_id, str):
                by_step[step_id] = dict(event.payload.get("observations") or {})
        return by_step

    def _invoke(self, step: Step, args: dict[str, Any]) -> ToolResult:
        try:
            spec = self.registry.get(step.tool)
            return self.registry.call(
                step.tool, args, workspace=self.workspace, timeout=spec.timeout_s
            )
        except Exception as exc:  # 工具抛异常 = 一次可记录的工具失败，不是进程失败
            return ToolResult(
                ok=False,
                error_code="tool_exception",
                error_message=f"{type(exc).__name__}: {exc}",
            )

    def _write_artifacts(
        self, run_id: str, step_id: str, result: ToolResult, data: dict[str, Any]
    ) -> tuple[str, ...]:
        payloads: list[tuple[str, str]] = []
        if result.stdout:
            payloads.append((result.stdout, "text/plain; charset=utf-8"))
        if result.stderr:
            payloads.append((result.stderr, "text/plain; charset=utf-8"))
        if data:
            payloads.append((_json_text(data), "application/json"))

        refs: list[str] = []
        for text, mime in payloads:
            ref = self.artifacts.put_text(text, mime=mime)
            if ref.artifact_id in refs:  # stdout == stderr 时别写两条一样的事件
                continue
            refs.append(ref.artifact_id)
            self.writer.emit(
                EventType.ARTIFACT_WRITTEN,
                {
                    "artifact_id": ref.artifact_id,
                    "sha256": ref.sha256,
                    "bytes": ref.nbytes,
                    "mime": ref.mime,
                },
                run_id=run_id,
                step_id=step_id,
            )
        return tuple(refs)
