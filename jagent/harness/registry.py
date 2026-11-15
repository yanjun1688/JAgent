"""JAgent v8 · 工具注册表。

v8 只有四个工具，优先复用从 v6 归档里 vendor 进来的适配器
（`jagent.harness.tools_v6`，它们已经带 `PathScopeGuardrail`），
旧实现不满足 v8 语义的地方在这里兜底：

* `fs.read` / `fs.list`：直接用旧实现（`ReadFileTool` / `ListDirTool`）；
* `fs.write`：旧 `WriteFileTool` **只允许覆盖已存在文件**。v8 需要新建，
  所以当它返回 `path_not_found` 且父目录存在时，由这里自己写；
* `shell.exec`：旧仓库没有，内联 `subprocess.run(..., shell=True)`。
  **非零退出不是工具错误**，`exit_code` 就是观测，判不判由断言决定。

三条不做的事：不写事件（那是 executor 的）、不判 pass/fail（那是 verifier 的）、
不看观测值（那是断言的事）。工具只返回 `ToolResult`。

错误码只有两个来源：`contracts.ERROR_CLASS` 里已有的键，或者旧实现的错误码经
`_LEGACY_CODES` 映射后的结果。找不到映射的旧码一律收成 `tool_exception`，
并把原码写进 `error_message` 前缀——不发明分类器没见过的码。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..contracts import ERROR_CLASS, Idempotency, SideEffect, ToolResult, ToolSpec
from .sandbox import check_command, is_absolute_spelling, scrub_env

#: 工具实现：`(args, workspace, timeout) -> ToolResult`。永远不抛异常是约定，
#: 但 executor 仍然会兜住异常——自定义工具不必信任这份约定。
ToolFn = Callable[[dict[str, Any], Path, float], ToolResult]

#: 旧适配器的错误码 → v8 错误码。v8 的世界里只认 `contracts.ERROR_CLASS` 的键。
#: `io_error` 在旧实现里同时覆盖「文件被锁」和「磁盘满」，无法可靠二分；
#: 不猜瞬时性，按未分类的 `tool_exception` 记，原码保留在 error_message 里。
_LEGACY_CODES: dict[str, str] = {
    "path_out_of_scope": "sandbox_denied",
    "path_not_found": "not_found",
    "not_a_file": "invalid_args",
    "not_a_directory": "invalid_args",
    "file_too_large": "invalid_args",
    "invalid_input": "invalid_args",
    "not_utf8": "invalid_args",
    "no_backup": "not_found",
    "io_error": "tool_exception",
    "timeout": "timeout",
}

#: 兜底用的目录上限，与旧实现保持一致。
_MAX_ENTRIES = 200


def _normalize_error(code: str | None) -> str:
    if not code:
        return "tool_exception"
    mapped = _LEGACY_CODES.get(code, code)
    return mapped if mapped in ERROR_CLASS else "tool_exception"


class _ToolError(Exception):
    """内联兜底用的内部异常：带 v8 错误码，出口处转成 `ToolResult`。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = _normalize_error(code)
        self.message = message

    def as_result(self) -> ToolResult:
        return ToolResult(ok=False, error_code=self.code, error_message=self.message)


def _guarded(fn: ToolFn) -> ToolFn:
    """把内联工具的内部异常收成 `ToolResult`。"""

    def run(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
        try:
            return fn(args, workspace, timeout)
        except _ToolError as exc:
            return exc.as_result()
        except OSError as exc:
            return ToolResult(
                ok=False,
                error_code="tool_exception",
                error_message=f"{type(exc).__name__}: {exc}",
            )

    return run


# --------------------------------------------------------------------------- #
# ToolRegistry
# --------------------------------------------------------------------------- #

class ToolRegistry:
    """名字 → （声明, 实现）。声明是计划冻结时看到的那个 `ToolSpec`。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._fns: dict[str, ToolFn] = {}

    def add(self, spec: ToolSpec, fn: ToolFn) -> None:
        if not isinstance(spec, ToolSpec):
            raise TypeError(f"spec must be a ToolSpec, got {type(spec).__name__}")
        if not callable(fn):
            raise TypeError(f"fn must be callable, got {type(fn).__name__}")
        self._specs[spec.name] = spec
        self._fns[spec.name] = fn

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name!r}") from None

    def names(self) -> list[str]:
        return list(self._specs)

    def specs(self) -> list[ToolSpec]:
        """全部契约声明（缺口 4）：契约快照的冻结与复核都用它。"""
        return list(self._specs.values())

    def descriptors(self) -> list[dict[str, Any]]:
        return [spec.descriptor() for spec in self._specs.values()]

    def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        workspace: Path,
        timeout: float | None = None,
    ) -> ToolResult:
        """调一次工具。工具不存在 → `KeyError`（和 `get` 一致）。"""
        spec = self.get(name)
        fn = self._fns[name]
        if not isinstance(args, dict):
            return ToolResult(
                ok=False,
                error_code="invalid_args",
                error_message=f"tool args must be a dict, got {type(args).__name__}",
            )
        effective = spec.timeout_s if timeout is None else float(timeout)
        result = fn(dict(args), Path(workspace), effective)
        if not isinstance(result, ToolResult):
            raise TypeError(f"tool {name!r} returned {type(result).__name__}, expected ToolResult")
        return result


# --------------------------------------------------------------------------- #
# vendored v6 适配器（可用则复用，不可用则返回 None）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _LegacyTools:
    guard: Any
    read: Any
    list: Any
    write: Any


def _legacy_tools(workspace: Path) -> _LegacyTools | None:
    try:
        from .tools_v6 import ListDirTool, PathScopeGuardrail, ReadFileTool, WriteFileTool
    except Exception:  # pragma: no cover - 只有 vendor 副本损坏时才走这里
        return None
    try:
        guard = PathScopeGuardrail(workspace)
        return _LegacyTools(
            guard=guard,
            read=ReadFileTool(guard),
            list=ListDirTool(guard),
            write=WriteFileTool(guard),
        )
    except Exception:  # pragma: no cover
        return None


def _from_envelope(envelope: Any) -> ToolResult:
    """旧 `ToolEnvelope` → v8 `ToolResult`。旧实现永远返回 envelope，不抛异常。"""
    status = getattr(getattr(envelope, "status", None), "value", None)
    if status == "success":
        data = getattr(envelope, "data", None)
        return ToolResult(ok=True, data=dict(data or {}))
    raw = getattr(envelope, "error_code", None)
    mapped = _normalize_error(raw)
    message = str(getattr(envelope, "error_message", "") or "")
    if raw and raw != mapped:
        message = f"{raw}: {message}" if message else str(raw)
    return ToolResult(ok=False, error_code=mapped, error_message=message)


# --------------------------------------------------------------------------- #
# 内联兜底（旧仓库缺席、或旧实现不满足 v8 语义时）
# --------------------------------------------------------------------------- #

def _resolve(workspace: Path, raw: Any, *, default: str | None = None) -> Path:
    """兜底路径的最小 scope 检查，规则与旧 `PathScopeGuardrail` 相同。

    没有这层，兜底路径就成了「绝对路径随便读」的后门。
    """
    text = default if raw is None else raw
    if not isinstance(text, str) or not text.strip():
        raise _ToolError("invalid_args", "path must be a non-empty relative string")
    text = text.strip()
    if is_absolute_spelling(text):
        raise _ToolError("sandbox_denied", f"absolute paths are outside the workspace: {text!r}")
    root = workspace.resolve()
    target = (root / text).resolve()
    if target != root and root not in target.parents:
        raise _ToolError("sandbox_denied", f"path escapes the workspace: {text!r}")
    return target


def _relative(workspace: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:  # pragma: no cover - _resolve 已经保证在内部
        return str(path)


@_guarded
def _inline_read(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
    path = _resolve(workspace, args.get("path"))
    max_lines = args.get("max_lines")
    if max_lines is not None and (
        isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 1
    ):
        raise _ToolError(
            "invalid_args", f"max_lines must be a positive integer, got {max_lines!r}"
        )
    if not path.exists():
        raise _ToolError("not_found", f"{str(args.get('path'))!r} does not exist")
    if not path.is_file():
        raise _ToolError("invalid_args", f"{str(args.get('path'))!r} is not a regular file")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _ToolError(
            "invalid_args", f"{str(args.get('path'))!r} is not UTF-8 text: {exc}"
        ) from exc
    content = text if max_lines is None else "".join(text.splitlines(keepends=True)[:max_lines])
    return ToolResult(ok=True, data={"content": content})


@_guarded
def _inline_list(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
    path = _resolve(workspace, args.get("path"), default=".")
    if not path.exists():
        raise _ToolError("not_found", f"{str(args.get('path'))!r} does not exist")
    if not path.is_dir():
        raise _ToolError("invalid_args", f"{str(args.get('path'))!r} is not a directory")
    items = sorted(path.iterdir(), key=lambda item: item.name)
    entries = [
        {
            "name": item.name,
            "kind": "dir" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
        }
        for item in items[:_MAX_ENTRIES]
    ]
    return ToolResult(ok=True, data={"entries": entries, "truncated": len(items) > _MAX_ENTRIES})


@_guarded
def _inline_write(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
    path = _resolve(workspace, args.get("path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise _ToolError("invalid_args", "content must be a string")
    # 建目录是另一个副作用，不在 fs.write 的权限里：父目录不在就报 not_found。
    if not path.parent.exists():
        raise _ToolError(
            "not_found", f"parent directory of {str(args.get('path'))!r} does not exist"
        )
    path.write_text(content, encoding="utf-8")
    return ToolResult(
        ok=True,
        data={"path": _relative(workspace, path), "bytes": len(content.encode("utf-8"))},
    )


# --------------------------------------------------------------------------- #
# 四个工具
# --------------------------------------------------------------------------- #

def _fs_read(legacy: _LegacyTools | None) -> ToolFn:
    def run(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
        if legacy is None:
            return _inline_read(args, workspace, timeout)
        return _from_envelope(legacy.read(dict(args)))

    return run


def _fs_list(legacy: _LegacyTools | None) -> ToolFn:
    def run(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
        if legacy is None:
            return _inline_list(args, workspace, timeout)
        return _from_envelope(legacy.list(dict(args)))

    return run


def _fs_write(legacy: _LegacyTools | None) -> ToolFn:
    def run(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
        if legacy is None:
            return _inline_write(args, workspace, timeout)
        result = _from_envelope(legacy.write(dict(args)))
        if result.ok or result.error_code != "not_found":
            return result
        # 旧实现只覆盖已存在文件。v8 允许新建：父目录存在时自己写。
        # 父目录不存在时 _inline_write 仍返回 not_found，不会凭空造目录。
        return _inline_write(args, workspace, timeout)

    return run


@_guarded
def _shell_exec(args: dict[str, Any], workspace: Path, timeout: float) -> ToolResult:
    cmd = args.get("cmd", args.get("command"))
    if not isinstance(cmd, str) or not cmd.strip():
        raise _ToolError("invalid_args", "cmd must be a non-empty string")
    cwd = _resolve(workspace, args.get("cwd"), default=".")
    if not cwd.is_dir():
        raise _ToolError("invalid_args", f"cwd {str(args.get('cwd'))!r} is not a directory")
    # 命令体沙箱：不过就不启动进程。cwd 传进去是因为相对路径按实际工作目录解析。
    verdict = check_command(cmd, workspace, cwd=cwd)
    if not verdict.ok:
        return ToolResult(
            ok=False, error_code="sandbox_denied", error_message=verdict.reason
        )
    # 超时来自 spec（registry.call / executor 传的就是 ToolSpec.timeout_s）；
    # 直接调用本函数且没给 timeout 时回落到同一份 spec，不再硬编码。
    effective_timeout = float(timeout) if timeout and timeout > 0 else _SHELL_EXEC_SPEC.timeout_s
    try:
        # 执行底座统一为 POSIX（D-037）：两平台都是 `bash -c`——Windows 用
        # Git for Windows 自带的 bash（git 本来就是硬依赖），Linux 用系统 bash。
        # 显式定位而不是 shell=True：bash 缺失是可达的 command_not_found。
        proc = subprocess.run(
            [_locate_bash(), "-c", cmd],
            cwd=str(cwd),
            env=scrub_env(workspace=workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",  # 显式 utf-8：locale 解码（cp936 vs UTF-8）会让观测跨平台不可复现
            errors="replace",  # 解码失败不该冒充工具失败
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return ToolResult(
            ok=False,
            error_code="timeout",
            error_message=f"command timed out after {effective_timeout:g}s",
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        return ToolResult(ok=False, error_code="command_not_found", error_message=str(exc))
    # 非零退出不是错误：exit_code 是观测，交给断言判。
    return ToolResult(
        ok=True, exit_code=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or ""
    )


def _locate_bash() -> str:
    """定位执行底座的 bash。

    Windows 的查找顺序是 **git 旁边的 Git bash → 常规安装路径 → PATH**：
    `where bash` 在 Windows 上常常先命中 `System32\\bash.exe`——那是 WSL 启动器，
    没有发行版时会一直挂到超时；它不是 D-037 的执行底座。git 是硬依赖，与它同装的
    `bin/bash.exe` 才是 Git for Windows 的 bash；git 装在非默认盘（如
    `D:\\ToolsInstall\\Git`）时，PATH 与 ProgramFiles 都找不到它。

    `shell=True` 换成显式 `bash -c` 后，"没有 shell" 从不可达变成可达：
    找不到 bash 是一句明确的 `command_not_found`，而不是模糊的异常。
    """
    if sys.platform == "win32":
        sibling = _git_sibling_bash()
        if sibling is not None:
            return sibling
        roots = (
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        )
        for root in roots:
            if not root:
                continue
            for relative in ("Git/bin/bash.exe", "Git/usr/bin/bash.exe"):
                candidate = Path(root) / relative
                if candidate.is_file():
                    return str(candidate)
    found = shutil.which("bash")
    if found:
        return found
    raise FileNotFoundError(
        "bash 不可用：shell.exec 的执行底座是 POSIX bash（D-037）。"
        "Windows 需要安装 Git for Windows；POSIX 系统需要 /bin/bash。"
    )


def _git_sibling_bash() -> str | None:
    """git 可执行文件旁边的 `bin/bash.exe`（Git for Windows 的 bash）。

    `git` 在 `cmd/git.exe`，bash 在 `bin/bash.exe`——同一次安装，所以非默认盘也找得到。
    """
    git = shutil.which("git")
    if not git:
        return None
    candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
    return str(candidate) if candidate.is_file() else None


def _text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# --------------------------------------------------------------------------- #
# 默认注册表
# --------------------------------------------------------------------------- #

_FS_READ_SPEC = ToolSpec(
    name="fs.read",
    version="1",
    args_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "workspace-relative path"},
            "max_lines": {"type": ["integer", "null"], "minimum": 1},
        },
        "required": ["path"],
    },
    side_effect=SideEffect.NONE,
    idempotency=Idempotency.NATURAL,
    error_codes=("sandbox_denied", "not_found", "invalid_args", "tool_exception"),
    timeout_s=30.0,
    readonly=True,
    description="Read a UTF-8 text file inside the workspace.",
    observations=("ok", "content"),
)

_FS_LIST_SPEC = ToolSpec(
    name="fs.list",
    version="1",
    args_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "workspace-relative directory (default '.')"},
        },
    },
    side_effect=SideEffect.NONE,
    idempotency=Idempotency.NATURAL,
    error_codes=("sandbox_denied", "not_found", "invalid_args", "tool_exception"),
    timeout_s=10.0,
    readonly=True,
    description="List a directory inside the workspace (name, kind, size).",
    observations=("ok", "entries", "truncated"),
)

_FS_WRITE_SPEC = ToolSpec(
    name="fs.write",
    version="1",
    args_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "workspace-relative path"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    side_effect=SideEffect.REVERSIBLE,
    # 同样的 args 重复写会收敛到同一份文件，所以是天然幂等；它没有 key 可对账。
    idempotency=Idempotency.NATURAL,
    error_codes=("sandbox_denied", "not_found", "invalid_args", "tool_exception"),
    timeout_s=30.0,
    readonly=False,
    description="Write a UTF-8 text file inside the workspace (creates it when absent).",
    observations=("ok", "path", "bytes"),
)

_SHELL_EXEC_SPEC = ToolSpec(
    name="shell.exec",
    version="2",
    args_schema={
        "type": "object",
        "properties": {
            "cmd": {
                "type": "string",
                "description": "command line executed by bash -c (POSIX semantics, D-037)",
            },
            "cwd": {"type": "string", "description": "workspace-relative directory (default '.')"},
        },
        "required": ["cmd"],
    },
    side_effect=SideEffect.IRREVERSIBLE,
    # 一条命令不承诺可幂等：要不要 gated 是计划侧的事，声明必须诚实。
    idempotency=Idempotency.NONE,
    error_codes=("invalid_args", "sandbox_denied", "timeout", "command_not_found", "tool_exception"),
    timeout_s=60.0,
    readonly=False,
    description=(
        "Run a shell command inside the workspace via `bash -c` (POSIX semantics on both "
        "platforms, D-037); a non-zero exit is an observation, not an error. "
        "The command body is restricted before the process starts: absolute paths (POSIX and "
        "Windows spellings), UNC paths, parent-directory and symlink escapes, tilde and "
        "environment-variable expansion ($HOME, %VAR%, $env:), backslashes outside quotes, and "
        "redirection targets outside the workspace are denied; only these binaries may be "
        "invoked: python, pytest, node, npm, git, echo, cat, ls, which, grep. "
        "This is a guard against accidental escapes, not a security boundary: code handed to an "
        "allowed interpreter (e.g. python -c \"open('/etc/x')\") is not inspected."
    ),
    observations=("ok", "exit_code", "stdout", "stderr"),
)


def build_default_registry(workspace: Path) -> ToolRegistry:
    """四个工具的注册表。旧适配器可用就复用，否则逐工具兜底。

    S4 占位（D-031 后置，路线图 14 §5「可观测性注入」）：将来的
    ``observability.get_trace`` 只读工具注册在这里——让 Agent 失败时
    自查链路（按 run_id 取事件视图），而不是盲目重试。
    """
    workspace = Path(workspace)
    legacy = _legacy_tools(workspace)
    registry = ToolRegistry()
    registry.add(_FS_READ_SPEC, _fs_read(legacy))
    registry.add(_FS_LIST_SPEC, _fs_list(legacy))
    registry.add(_FS_WRITE_SPEC, _fs_write(legacy))
    registry.add(_SHELL_EXEC_SPEC, _shell_exec)
    return registry
