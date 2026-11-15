"""JAgent v8 · vendored v6 文件工具（自包含副本）。

为什么有这个文件
----------------
v8 还需要借 v6 的三个文件工具（它们自带 ``PathScopeGuardrail``），但 v6 的
``harness/`` 的 v6 版本已封存进 git 历史。为了让 ``jagent/`` 不再依赖
``harness.*``，这里把**实际被用到的那几个类**连同最小外壳复制进来，而不是
import 整个 kernel。

来源（v6 工作树，git 未跟踪；仓库 HEAD = 313c82c5ac64734a773f7eb5aef1d39c30356803）：

* ``harness/layers/adapters/tools/workspace.py``   → PathScopeGuardrail / WorkspaceError
* ``harness/layers/adapters/tools/read_file.py``  → ReadFileTool / read_file_spec
* ``harness/layers/adapters/tools/list_dir.py``   → ListDirTool / list_dir_spec
* ``harness/layers/adapters/tools/write_file.py`` → WriteFileTool / write_file_spec
* ``harness/layers/kernel/tools.py``              → 下面「最小外壳」的类定义

每个来源小节都用 ``# vendored from ... @ commit 313c82c`` 标注。搬运时只改了
import 和「为 v8 裁掉未使用字段」两处，行为逻辑逐字保留。

没有搬过来的：

* kernel 的 executor / scheduler / bundle / event 模型 —— v8 有自己的 executor；
* ``RestoreFileTool`` —— v8 不执行补偿，只调用 WriteFileTool；
* ``ToolSpec.__hash__`` / ``tool_contract_hash`` —— 工具契约哈希是
  ``jagent/harness/fingerprint.py`` 的事，而且 hash 会把 dict 拖进摘要；
* 旧 spec 的 ``retry_policy`` / ``required_roles`` / ``required_scopes`` ——
  v8 不读这些字段，留着只会假装它们被用到了。

注意：这里的工具名仍是 v6 的 ``read_file`` / ``list_dir`` / ``write_file``，
不是 v8 的 ``fs.read`` 等。``registry.py`` 只借实现，声明仍用 v8 自己的
``ToolSpec``；``_from_envelope`` 负责把 v6 envelope 翻成 v8 ``ToolResult``。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

# --------------------------------------------------------------------------- #
# 最小外壳
# vendored from harness/layers/kernel/tools.py @ commit 313c82c
#
# 只保留被三个工具直接引用的类型；没有 Tool protocol、RetryPolicy、FakeReadTool。
# --------------------------------------------------------------------------- #


class EnvelopeStatus(StrEnum):
    """Six states an envelope may report (v5.2 §10.2)."""

    SUCCESS = "success"
    BUSINESS_FAILURE = "business_failure"
    RETRYABLE_ERROR = "retryable_error"
    PERMANENT_ERROR = "permanent_error"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class TimeoutMode(StrEnum):
    FAIL = "fail"
    UNKNOWN = "unknown"
    RETRY_SAFE = "retry_safe"


class ReconcileMode(StrEnum):
    """How the reconciler may drive a hung attempt (v5.2 §6.4 / §10.1)."""

    QUERY = "query"
    RETRY_SAFE = "retry_safe"
    MANUAL = "manual"
    NONE = "none"


@dataclass(frozen=True)
class CompensationSpec:
    """Which tool undoes this one, and with what input (§10.1)."""

    tool_name: str
    input_template: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """The declaration a plan is frozen against.

    Trimmed to the fields the vendored tools actually set. The v6 original also
    carried retry/role/scope declarations and a hash; v8 does not read them.
    """

    name: str
    version: str = "1"
    description: str = ""
    read_only: bool = True
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    model_ref: str = ""
    timeout_seconds: float = 5.0
    timeout_mode: TimeoutMode = TimeoutMode.UNKNOWN
    reconcile_mode: ReconcileMode = ReconcileMode.NONE
    compensation: CompensationSpec | None = None
    read_set: tuple[str, ...] | None = None
    write_set: tuple[str, ...] | None = None
    requires_confirmation: bool = False

    @property
    def side_effects(self) -> bool:
        """True when calling this tool may change the outside world (§10.1)."""
        return not self.read_only


@dataclass(frozen=True)
class ToolEnvelope:
    """Result of one tool call. ``data`` is the only part the kernel hashes."""

    status: EnvelopeStatus
    #: ``None`` = no output; ``{}`` = the output is an empty object.
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    reconcile_hint: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is EnvelopeStatus.SUCCESS


@dataclass(frozen=True)
class ToolCall:
    """Everything one call carries, for tools that opt in (``accepts_call``).

    v8 never constructs this; it exists so ``WriteFileTool._unpack`` keeps its
    v6 shape. Only payload/idempotency_key are read, so the identity fields are
    optional here instead of required.
    """

    payload: Mapping[str, Any]
    idempotency_key: str
    attempt_id: str = ""
    run_id: str = ""
    lease_generation: int = 0
    context: Any = None


# --------------------------------------------------------------------------- #
# 工具名（原 harness/layers/tool_names.py，只取用到的常量）
# --------------------------------------------------------------------------- #

READ_FILE = "read_file"
WRITE_FILE = "write_file"
RESTORE_FILE = "restore_file"
LIST_DIR = "list_dir"

#: The backup lives next to the target so the undo needs no extra registry.
BACKUP_SUFFIX = ".bak"
TEMP_SUFFIX = ".tmp"


# --------------------------------------------------------------------------- #
# workspace scope
# vendored from harness/layers/adapters/tools/workspace.py @ commit 313c82c
# --------------------------------------------------------------------------- #


class WorkspaceError(Exception):
    """The requested path is outside the workspace, or not a legal tool path."""


def _is_absolute(text: str) -> bool:
    """True for POSIX absolutes, drive-letter paths and UNC shares.

    ``PureWindowsPath("C:x")`` is *drive-relative*, not absolute, but it still
    escapes a workspace rooted on another drive, so the drive alone is enough to
    refuse it.
    """
    return (
        PurePosixPath(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or bool(PureWindowsPath(text).drive)
    )


class PathScopeGuardrail:
    """One workspace root; ``check`` is the only way in."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def check(self, raw: str) -> Path:
        """Resolve *raw* to a path inside the workspace, or raise.

        ``resolve()`` follows symlinks, so a link pointing outside the root is
        refused by the same containment test as ``../``: the boundary is the
        resolved location, never the spelling of the path.
        """
        if not isinstance(raw, str) or not raw.strip():
            raise WorkspaceError("a tool path must be a non-empty relative string")
        text = raw.strip()
        if _is_absolute(text):
            raise WorkspaceError(f"absolute paths are outside the workspace: {text!r}")
        candidate = (self.root / text).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceError(f"path escapes the workspace: {text!r}")
        return candidate

    def relative(self, path: Path) -> str:
        """The workspace-relative POSIX form, which is what plans and tools carry."""
        return path.resolve().relative_to(self.root).as_posix()


# --------------------------------------------------------------------------- #
# read_file
# vendored from harness/layers/adapters/tools/read_file.py @ commit 313c82c
# --------------------------------------------------------------------------- #

#: The executor keeps at most 1MB of output per node; reading a multi-MB file
#: only to discard it wastes memory. Refuse early instead of allocating it.
MAX_FILE_BYTES = 5_000_000


def read_file_spec() -> ToolSpec:
    """The declaration a plan is frozen against (ordinary and test tools share it)."""
    return ToolSpec(
        name=READ_FILE,
        version="1",
        description="Read a UTF-8 text file inside the workspace.",
        read_only=True,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "workspace-relative path"},
                "max_lines": {"type": ["integer", "null"], "minimum": 1},
            },
            "required": ["path"],
        },
        output_schema={
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
        # v6 原文是旧包里的 read_file model_ref；这里改成自包含路径，
        # 保证 `jagent/` 里不再出现对旧包的点分引用。
        model_ref="jagent.harness.tools_v6:ReadFileInput",
        timeout_seconds=30.0,
        timeout_mode=TimeoutMode.RETRY_SAFE,
        reconcile_mode=ReconcileMode.RETRY_SAFE,
        read_set=None,
        write_set=(),
    )


class ReadFileTool:
    """Reads one file; every failure is an envelope, never an exception."""

    def __init__(self, guardrail: PathScopeGuardrail) -> None:
        self.guardrail = guardrail
        self.spec = read_file_spec()
        self.calls = 0
        self.last_payload: dict[str, Any] | None = None

    def __call__(self, payload: Mapping[str, Any]) -> ToolEnvelope:
        self.calls += 1
        self.last_payload = dict(payload)
        try:
            path = self.guardrail.check(payload.get("path", ""))
        except WorkspaceError as exc:
            return ToolEnvelope(
                status=EnvelopeStatus.PERMANENT_ERROR,
                error_code="path_out_of_scope",
                error_message=str(exc),
            )
        if not path.exists():
            return ToolEnvelope(
                status=EnvelopeStatus.PERMANENT_ERROR,
                error_code="path_not_found",
                error_message=f"{payload.get('path')!r} does not exist",
            )
        if not path.is_file():
            return ToolEnvelope(
                status=EnvelopeStatus.PERMANENT_ERROR,
                error_code="not_a_file",
                error_message=f"{payload.get('path')!r} is not a regular file",
            )
        try:
            size = path.stat().st_size
        except OSError as exc:
            return ToolEnvelope(
                status=EnvelopeStatus.RETRYABLE_ERROR,
                error_code="io_error",
                error_message=str(exc),
                retryable=True,
            )
        if size > MAX_FILE_BYTES:
            return ToolEnvelope(
                status=EnvelopeStatus.PERMANENT_ERROR,
                error_code="file_too_large",
                error_message=(
                    f"{payload.get('path')!r} is {size} bytes; "
                    f"the read limit is {MAX_FILE_BYTES}"
                ),
            )
        max_lines = payload.get("max_lines")
        if max_lines is not None and (
            isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 1
        ):
            return ToolEnvelope(
                status=EnvelopeStatus.PERMANENT_ERROR,
                error_code="invalid_input",
                error_message=f"max_lines must be a positive integer, got {max_lines!r}",
            )
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            return ToolEnvelope(
                status=EnvelopeStatus.PERMANENT_ERROR,
                error_code="not_utf8",
                error_message=f"{payload.get('path')!r} is not UTF-8 text: {exc}",
            )
        except OSError as exc:
            # Transient (locked file, network share): the honest outcome is "we
            # do not know whether the read happened", not "it did not".
            return ToolEnvelope(
                status=EnvelopeStatus.RETRYABLE_ERROR,
                error_code="io_error",
                error_message=str(exc),
                retryable=True,
            )
        content = text if max_lines is None else "".join(text.splitlines(keepends=True)[:max_lines])
        return ToolEnvelope(status=EnvelopeStatus.SUCCESS, data={"content": content})


# --------------------------------------------------------------------------- #
# list_dir
# vendored from harness/layers/adapters/tools/list_dir.py @ commit 313c82c
# --------------------------------------------------------------------------- #

#: A guard against a directory with a million entries becoming an envelope.
MAX_ENTRIES = 200


def list_dir_spec() -> ToolSpec:
    """The declaration a plan is frozen against."""
    return ToolSpec(
        name=LIST_DIR,
        version="1",
        description="List a directory inside the workspace (name, kind, size).",
        read_only=True,
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "workspace-relative directory (default '.')",
                }
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kind": {"type": "string"},
                            "size": {"type": ["integer", "null"]},
                        },
                        "required": ["name", "kind"],
                    },
                },
                "truncated": {"type": "boolean"},
            },
            "required": ["entries"],
        },
        model_ref="jagent.harness.tools_v6:ListDirInput",
        timeout_seconds=10.0,
        timeout_mode=TimeoutMode.RETRY_SAFE,
        reconcile_mode=ReconcileMode.RETRY_SAFE,
        read_set=None,
        write_set=(),
    )


class ListDirTool:
    """Lists one directory; every failure is an envelope, never an exception."""

    def __init__(self, guardrail: PathScopeGuardrail) -> None:
        self.guardrail = guardrail
        self.spec = list_dir_spec()
        self.calls = 0
        self.last_payload: dict[str, Any] | None = None

    def __call__(self, payload: Mapping[str, Any]) -> ToolEnvelope:
        self.calls += 1
        self.last_payload = dict(payload)
        raw = payload.get("path", ".")
        try:
            path = self.guardrail.check(str(raw))
        except WorkspaceError as exc:
            return ToolEnvelope(
                status=EnvelopeStatus.PERMANENT_ERROR,
                error_code="path_out_of_scope",
                error_message=str(exc),
            )
        if not path.exists():
            return ToolEnvelope(
                status=EnvelopeStatus.PERMANENT_ERROR,
                error_code="path_not_found",
                error_message=f"{str(raw)!r} does not exist",
            )
        if not path.is_dir():
            return ToolEnvelope(
                status=EnvelopeStatus.PERMANENT_ERROR,
                error_code="not_a_directory",
                error_message=f"{str(raw)!r} is not a directory",
            )
        try:
            items = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            # Transient (locked directory, network share): "we do not know whether
            # the listing happened" is the honest outcome.
            return ToolEnvelope(
                status=EnvelopeStatus.RETRYABLE_ERROR,
                error_code="io_error",
                error_message=str(exc),
                retryable=True,
            )
        entries = [
            {
                "name": item.name,
                "kind": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None,
            }
            for item in items[:MAX_ENTRIES]
        ]
        return ToolEnvelope(
            status=EnvelopeStatus.SUCCESS,
            data={"entries": entries, "truncated": len(items) > MAX_ENTRIES},
        )


# --------------------------------------------------------------------------- #
# write_file
# vendored from harness/layers/adapters/tools/write_file.py @ commit 313c82c
#
# RestoreFileTool is intentionally absent: v8 has no compensation executor.
# --------------------------------------------------------------------------- #


def write_file_spec() -> ToolSpec:
    """The declaration a plan is frozen against."""
    return ToolSpec(
        name=WRITE_FILE,
        version="1",
        description="Overwrite an existing UTF-8 text file inside the workspace.",
        read_only=False,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "workspace-relative path"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "bytes": {"type": "integer"},
                "backup": {"type": "string"},
            },
            "required": ["path", "bytes", "backup"],
        },
        model_ref="jagent.harness.tools_v6:WriteFileInput",
        timeout_seconds=30.0,
        timeout_mode=TimeoutMode.UNKNOWN,
        reconcile_mode=ReconcileMode.RETRY_SAFE,
        compensation=CompensationSpec(tool_name=RESTORE_FILE, input_template={}),
        requires_confirmation=True,
        read_set=(),
        write_set=None,
    )


class WriteFileTool:
    """Overwrite one existing file; deduplicate by idempotency key."""

    accepts_call = True

    def __init__(self, guardrail: PathScopeGuardrail) -> None:
        self.guardrail = guardrail
        self.spec = write_file_spec()
        self.calls = 0
        self.keys: list[str] = []
        self._by_key: dict[str, ToolEnvelope] = {}

    def __call__(self, payload: ToolCall | Mapping[str, Any]) -> ToolEnvelope:
        data, key = _unpack(payload)
        self.calls += 1
        if key is None:
            return self._write(data)
        self.keys.append(key)
        # The key is the only thing that lets a re-dispatched attempt be a no-op
        # instead of a second write. It is process-local here; an external system
        # is expected to deduplicate on the same key across processes (§5.2).
        cached = self._by_key.get(key)
        if cached is not None:
            return cached
        envelope = self._write(data)
        self._by_key[key] = envelope
        return envelope

    def _write(self, payload: Mapping[str, Any]) -> ToolEnvelope:
        path = self._target(payload)
        if isinstance(path, ToolEnvelope):
            return path
        content = payload.get("content")
        if not isinstance(content, str):
            return _permanent("invalid_input", "content must be a string")
        if not path.exists():
            # Creating is a different obligation than overwriting; refuses until a
            # tool with a delete-shaped undo exists. v8's registry falls back to
            # its inline write when it sees this code.
            return _permanent("path_not_found", f"{payload.get('path')!r} does not exist")
        if not path.is_file():
            return _permanent("not_a_file", f"{payload.get('path')!r} is not a regular file")
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        temporary = path.with_name(path.name + TEMP_SUFFIX)
        try:
            shutil.copy2(path, backup)
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            # Transient (locked file, full disk): the honest outcome is "we do not
            # know whether the write landed", not "it did not".
            return ToolEnvelope(
                status=EnvelopeStatus.RETRYABLE_ERROR,
                error_code="io_error",
                error_message=str(exc),
                retryable=True,
            )
        return ToolEnvelope(
            status=EnvelopeStatus.SUCCESS,
            data={
                "path": self.guardrail.relative(path),
                "bytes": len(content.encode("utf-8")),
                "backup": self.guardrail.relative(backup),
            },
        )

    def _target(self, payload: Mapping[str, Any]) -> Path | ToolEnvelope:
        try:
            return self.guardrail.check(payload.get("path", ""))
        except WorkspaceError as exc:
            return _permanent("path_out_of_scope", str(exc))


def _permanent(code: str, message: str) -> ToolEnvelope:
    return ToolEnvelope(
        status=EnvelopeStatus.PERMANENT_ERROR, error_code=code, error_message=message
    )


def _unpack(call: ToolCall | Mapping[str, Any]) -> tuple[Mapping[str, Any], str | None]:
    """Accept both shapes: the kernel's ``ToolCall`` and the bare payload.

    ``accepts_call`` keeps the kernel honest about passing the key, while the
    ``Mapping`` branch is what makes the class satisfy the kernel's ``Tool``
    protocol (and what a direct caller or a test uses).
    """
    if isinstance(call, ToolCall):
        return call.payload, call.idempotency_key
    return call, None
