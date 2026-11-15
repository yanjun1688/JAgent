"""G-06 v2 · `shell.exec` 的命令体沙箱（POSIX 统一底座，D-037）。

`shell.exec` 的执行底座在两平台都是 `bash -c`（Windows 用 Git for Windows
自带的 bash），所以命令体的静态检查只有**一套 POSIX 语义**：`type ..\\..\\x`
这类 cmd 拼写不是白名单成员，`C:/Windows/...`、`//server/share`、`/etc/passwd`
照样越出 workspace（红队 RT-08，P0）。这里在**启动进程之前**对命令体做一次
确定性的静态检查。

设计约束：

* `check_command` 是纯函数，不启动进程、不写盘、不读环境变量，因此可以单测；
* 判断只依赖「命令文本 + workspace（+ 可选 cwd）」三样东西；
* 拒绝时给用户一句话（``reason``），给日志一段细节（``detail``）；
* 只在 registry 里接线，不改变 `ToolResult` 的语义：非零退出仍是观测，
  沙箱拒绝才是 ``sandbox_denied``。

覆盖的规则
----------

1. 绝对路径：盘符路径（``C:/...``）、UNC（``//server/share``）、
   POSIX 绝对路径（``/etc/passwd``，含 ``/etc`` 这类单段根目录）；
2. 父目录逃逸：token 里出现 ``..`` 路径段时，解析结果必须仍在 workspace 内，
   所以 ``a/../b`` 合法，``../../x`` 被拒；
3. 符号链接逃逸：相对路径 token 用 ``Path.resolve()`` 跟随符号链接后再判包含关系；
4. 环境变量展开：``%VAR%``、``$HOME``、``${VAR}``、``$(...)``、``$env:...``、
   ``~`` 一律拒绝——展开发生在沙箱之外，静态检查无法知道它指向哪；
5. 反斜杠：引号外出现即拒绝——POSIX bash 里它是转义符，静态检查无法可靠解析
   ``\\..\\`` 这类拼写，宁多拒；引号内是字面量；
6. 重定向目标：``>`` / ``>>`` / ``<`` 后面的路径走 1–3 的同一套检查；
7. 命令白名单：只允许 ``python``、``pytest``、``node``、``npm``、``git``、``echo``、
   ``cat``、``ls``、``which``、``grep``；复合命令（``&&``、``|``、``;``、换行）
   的每一段都单独检查第一 token（``;`` 在 bash 里确实是分隔符；换行按分隔符
   处理：宁可多拒，不可漏拒）。

**挡不住的（诚实说明）**：这是「防误操作」，不是「防恶意」的安全边界。

* 解释器代码参数不做语义检查：``python -c "open('/etc/x')"``、``node -e "..."``、
  ``pytest --rootdir=...`` 都能在解释器内部读任意路径。按产品要求**故意不拦**，
  因为拦它需要解析 Python/JS，静态规则只会制造误报。
* 白名单二进制自身的越界能力：``git --git-dir=...``、``git config core.pager=...``、
  ``npm exec``、``node --eval`` 等。
* 硬链接：``Path.resolve()`` 只解析符号链接；workspace 内的硬链接可以指向外部文件。
* TOCTOU：检查与 ``subprocess.run`` 之间文件系统可被替换（检查不是原子操作）。
* ``check_command`` 看的是 shell 命令行文本，不做 bash 的完整展开
  （通配符、brace expansion 等）；遇到看不懂的形态按「拒绝」处理。
* 命令体内部的重定向（例如 ``python -c "open('x','w')"``）完全看不到。
* 超时只杀直接子进程（``bash -c`` 单命令在 POSIX 上通常 exec 成 payload 进程，
  等效；Windows 下 bash 的孙进程可能存活）——进程级收口归 ToolBackend/Docker。

真正的隔离（独立用户、Job Object、容器、只读挂载）不在本模块范围内。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

#: 允许被调用的命令名（不含扩展名，大小写不敏感）。
#: 其余二进制一律拒绝：``rm``、``sh``、``bash``、``powershell``、``cmd``、
#: 以及 v1 的 cmd 内建（``type``/``dir``/``where``/``findstr``，D-037 退出）。
ALLOWED_BINARIES = frozenset(
    {
        "python",
        "pytest",
        "node",
        "npm",
        "git",
        "echo",
        "cat",
        "ls",
        "which",
        "grep",
    }
)

#: 可执行文件后缀，比较白名单前剥掉。
_EXE_SUFFIXES = (".exe", ".cmd", ".bat", ".com")

#: 命令分隔符：每一段的第一 token 都要重新过白名单。
_SEPARATORS = frozenset({";", "|", "&"})

#: 重定向操作符（含带 fd 号的写法，如 ``2>``、``1>>``）。
_REDIRECT_RE = re.compile(r"^(?:[0-9]+)?(?:>>?|<)$")

#: 盘符绝对路径：``C:\`` / ``C:/``。
_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")

#: 盘符相对路径：``C:foo``（相对「该盘当前目录」，无法证明在 workspace 内）。
_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:")

#: POSIX 绝对路径：``/etc/passwd``、``/usr/bin`` 这类多段路径。
_POSIX_ABSOLUTE_RE = re.compile(r"^/(?:[^/\\]+[\\/])")

#: 常见 POSIX 根目录的单段写法（``/etc``、``/tmp``）。
_POSIX_ROOTS = frozenset(
    {
        "bin",
        "boot",
        "dev",
        "etc",
        "home",
        "media",
        "mnt",
        "opt",
        "proc",
        "root",
        "sbin",
        "srv",
        "sys",
        "tmp",
        "usr",
        "var",
    }
)

#: cmd.exe / PowerShell 的环境变量写法。
_ENV_PERCENT_RE = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")
_ENV_DOLLAR_RE = re.compile(r"\$(?:env:|[A-Za-z_{(])")

#: 出现这些字符的 token 不是「路径参数」（是代码片段、正则、URL 等）。
_NON_PATH_CHARS = frozenset("(){}[]'\"$%&|;<=>")

#: scrub_env 需要改写/去重的环境变量名（Windows 环境变量大小写不敏感）。
_SCRUBBED_KEYS = frozenset({"HOME", "USERPROFILE", "TEMP", "TMP"})


def is_absolute_spelling(text: str) -> bool:
    """平台无关的「绝对路径拼写」判定：POSIX `/...`、Windows 盘符/UNC 都算。

    `Path.is_absolute()` 只懂当前平台的语义（Windows 上 `/etc` 不算绝对、
    Linux 上 `C:/x` 不算绝对）；跨平台日志重放与工具路径检查需要把两种
    拼写都认出来。实现与 tools_v6 的 `_is_absolute` 同构（先例即规则）。
    """
    pure_posix = PurePosixPath(text)
    pure_windows = PureWindowsPath(text)
    return (
        pure_posix.is_absolute()
        or pure_windows.is_absolute()
        or bool(pure_windows.drive)
    )


@dataclass(frozen=True)
class SandboxVerdict:
    """一次命令体检查的结论。

    ``ok`` 为假时 ``reason`` 是一句给用户看的话，``detail`` 是给日志的细节
    （token、解析结果、异常类型等），两者都不含命令的输出。
    """

    ok: bool
    reason: str = ""
    detail: str = ""


_OK = SandboxVerdict(ok=True)


def _deny(reason: str, detail: str = "") -> SandboxVerdict:
    return SandboxVerdict(ok=False, reason=reason, detail=detail)


def _within(root: Path, target: Path) -> bool:
    """``target`` 是否等于 ``root`` 或落在其下（Windows 上大小写不敏感）。"""
    return target == root or root in target.parents


def _has_parent_segment(token: str) -> bool:
    return any(part == ".." for part in re.split(r"[\\/]+", token))


def _absolute_kind(token: str) -> str:
    """绝对路径分类：``unc`` / ``drive`` / ``drive_relative`` / ``posix`` / ``""``。"""
    if token.startswith("\\\\") or token.startswith("//"):
        return "unc"
    if _DRIVE_ABSOLUTE_RE.match(token):
        return "drive"
    if _DRIVE_RELATIVE_RE.match(token):
        return "drive_relative"
    if token == "/" or _POSIX_ABSOLUTE_RE.match(token):
        return "posix"
    first = token[1:].split("/", 1)[0].split("\\", 1)[0].lower() if token.startswith("/") else ""
    if first in _POSIX_ROOTS:
        return "posix"
    return ""


def _looks_like_path(token: str) -> bool:
    """保守判断一个 token 是否是要落到文件系统上的路径参数。

    代码片段（``print(1)``）、URL、开关（``-q``）不在这里解析；
    ``..`` / 绝对路径的 token 即使含特殊字符也会在别处被显式检查。
    ``/x`` 开头的 token 不是开关——POSIX 里那就是绝对路径，交给规则 1。
    """
    if not token or token.startswith("-"):
        return False
    if any(ch in _NON_PATH_CHARS for ch in token):
        return False
    return True


def _path_parts(token: str) -> list[str]:
    """从 token 里挑出需要做包含性检查的路径片段。

    ``--git-dir=C:\\outside`` 这种「选项=绝对路径」也要查，所以按 ``=`` 拆开
    只检查右侧；代码片段（含括号/引号/$ 等）整段跳过——解释器内部的访问
    不在静态检查范围内（见模块 docstring）。
    """
    parts: list[str] = []
    if _absolute_kind(token) or _looks_like_path(token):
        parts.append(token)
    if "=" in token:
        for part in token.split("=")[1:]:
            if part and (_absolute_kind(part) or _has_parent_segment(part)):
                parts.append(part)
    return parts


def _check_env(token: str) -> SandboxVerdict | None:
    """规则 4：环境变量展开与 tilde——展开发生在沙箱之外，静态检查无法知道指向。"""
    if token.startswith("~"):
        return _deny(
            f"命令含 tilde 展开 {token!r}（等价于 $HOME 环境变量展开），无法确认它指向哪里，已拒绝执行",
            f"tilde token={token!r}",
        )
    if _ENV_PERCENT_RE.search(token) or _ENV_DOLLAR_RE.search(token):
        return _deny(
            f"命令含环境变量展开 {token!r}，无法确认它指向哪里，已拒绝执行",
            f"env-expansion token={token!r}",
        )
    return None


def _check_path_token(
    token: str, base: Path, root: Path, *, redirect: bool = False
) -> SandboxVerdict | None:
    """检查单个路径 token 是否仍落在 workspace 内（规则 1–3、5）。"""
    kind = _absolute_kind(token)
    if kind == "unc":
        return _deny(
            f"UNC 路径 {token!r} 不在 workspace 内，已拒绝执行",
            f"unc token={token!r}",
        )
    if kind == "drive_relative":
        return _deny(
            f"盘符相对路径 {token!r} 无法确认落在 workspace 内，已拒绝执行",
            f"drive-relative token={token!r}",
        )
    try:
        if kind in ("drive", "posix"):
            target = Path(token).resolve()
        else:
            target = (base / token).resolve()
    except (OSError, ValueError) as exc:
        return _deny(
            f"命令里的路径 {token!r} 无法解析，已拒绝执行",
            f"{type(exc).__name__}: {exc}; token={token!r}",
        )
    if _within(root, target):
        return None
    if redirect:
        reason = f"重定向目标 {token!r} 不在 workspace 内，已拒绝执行"
    elif kind in ("drive", "posix"):
        reason = f"绝对路径 {token!r} 不在 workspace 内，已拒绝执行"
    elif _has_parent_segment(token):
        reason = f"父目录逃逸 {token!r} 落到 workspace 之外，已拒绝执行"
    else:
        reason = f"路径 {token!r} 解析后落在 workspace 之外，已拒绝执行"
    return _deny(reason, f"resolved={target}; token={token!r}")


def _check_executable(token: str, base: Path, root: Path) -> SandboxVerdict | None:
    """规则 6：只放行白名单里的解释器/内建命令。"""
    raw = token.strip()
    name = PureWindowsPath(raw).name.lower()
    for suffix in _EXE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    name = name.strip()
    if not name:
        return _deny("命令为空：cmd 必须是非空字符串", "empty executable token")
    if name not in ALLOWED_BINARIES:
        allowed = ", ".join(sorted(ALLOWED_BINARIES))
        return _deny(
            f"命令 {name!r} 不在沙箱白名单内（允许：{allowed}），已拒绝执行",
            f"executable={raw!r}; allowed={sorted(ALLOWED_BINARIES)}",
        )
    kind = _absolute_kind(raw)
    if kind == "unc":
        return _deny(
            f"解释器路径 {raw!r} 是 UNC 路径，已拒绝执行",
            f"unc executable={raw!r}",
        )
    if kind == "drive_relative":
        return _deny(
            f"解释器路径 {raw!r} 是盘符相对路径，已拒绝执行",
            f"drive-relative executable={raw!r}",
        )
    if kind in ("drive", "posix"):
        # 解释器允许用绝对路径调用（Windows 上 `sys.executable` 就是绝对路径，
        # 现网与单测都靠它）。只要求名字在白名单里，见模块 docstring 的边界说明。
        return None
    if "\\" in raw or "/" in raw:
        try:
            target = (base / raw).resolve()
        except (OSError, ValueError) as exc:
            return _deny(
                f"解释器路径 {raw!r} 无法解析，已拒绝执行",
                f"{type(exc).__name__}: {exc}",
            )
        if not _within(root, target):
            return _deny(
                f"解释器路径 {raw!r} 不在 workspace 内，已拒绝执行",
                f"resolved={target}",
            )
    return None


class _BackslashOutsideQuotesError(ValueError):
    """引号外出现反斜杠：POSIX bash 里它是转义符，静态检查不可靠解析（D-037）。"""


def _tokenize(cmd: str) -> list[str]:
    """把命令行切成 token，保留重定向操作符，丢弃引号。

    POSIX bash 的最小够用切分：空白、换行、``;``、``|``、``&``、``<``、``>``
    在引号外是分隔符；``^`` 是普通字面量；**引号外的反斜杠是转义符**——
    静态检查无法可靠解析 ``\\..\\`` 这类拼写，直接拒绝（宁多拒，D-037）；
    引号内的反斜杠是字面量，原样保留。
    未闭合的引号一直吃到行尾——看不懂的形态交给后面的保守检查。
    """
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    length = len(cmd)
    while i < length:
        ch = cmd[i]
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            raise _BackslashOutsideQuotesError(
                f"backslash outside quotes at {i}: {cmd[max(0, i - 12): i + 12]!r}"
            )
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch in "\r\n":
            # 换行在 bash 里同样是命令分隔符
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(";")
            i += 1
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            i += 1
            continue
        if ch in ";|&":
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(ch)
            i += 1
            continue
        if ch in "<>":
            operator = ch
            if ch == ">" and i + 1 < length and cmd[i + 1] == ">":
                operator = ">>"
                i += 1
            if buf and "".join(buf).isdigit():
                # `2>` / `2>>`：fd 号属于操作符
                operator = "".join(buf) + operator
                buf = []
            elif buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(operator)
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def check_command(cmd: str, workspace: Path, *, cwd: Path | None = None) -> SandboxVerdict:
    """检查一条准备交给 ``shell.exec`` 的命令体。

    :param cmd: 用户/计划给出的命令行文本。
    :param workspace: workspace 根目录；所有路径解析后必须落在它下面。
    :param cwd: 命令实际的工作目录（默认 workspace）。相对路径按它解析，
        这样 ``cwd`` 在子目录时 ``..`` 回到 workspace 根仍然合法。
    :return: ``SandboxVerdict``；``ok`` 为假时 registry 必须拒绝执行。

    纯函数：不启动进程、不写盘、不读环境变量、不碰全局状态。
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return _deny("命令为空：cmd 必须是非空字符串", "empty command")
    try:
        root = Path(workspace).resolve()
    except (OSError, ValueError) as exc:
        return _deny("workspace 无法解析，已拒绝执行", f"{type(exc).__name__}: {exc}")
    base = root if cwd is None else Path(cwd)
    try:
        base = base.resolve()
    except (OSError, ValueError) as exc:
        return _deny("命令的工作目录无法解析，已拒绝执行", f"{type(exc).__name__}: {exc}")
    if not _within(root, base):
        return _deny(
            f"命令的工作目录 {str(cwd)!r} 不在 workspace 内，已拒绝执行",
            f"cwd={base}",
        )

    try:
        tokens = _tokenize(cmd)
    except _BackslashOutsideQuotesError as exc:
        return _deny(
            "命令在引号外含反斜杠：POSIX bash 里它是转义符，静态检查无法可靠解析，已拒绝执行",
            f"{exc}",
        )
    exec_pending = True
    saw_exec = False
    redirect_target = False
    fd_dup = False
    for token in tokens:
        if fd_dup:
            # `2>&1` 里的 fd 号，不是命令、也不是路径
            fd_dup = False
            continue
        if token == "&" and redirect_target:
            # 重定向目标写成 `&1` / `&2`：文件描述符复制，不是路径
            fd_dup = True
            redirect_target = False
            continue
        if token in _SEPARATORS:
            exec_pending = True
            redirect_target = False
            continue
        if _REDIRECT_RE.match(token):
            redirect_target = True
            continue
        verdict = _check_env(token)
        if verdict is not None:
            return verdict
        if exec_pending:
            verdict = _check_executable(token, base, root)
            if verdict is not None:
                return verdict
            exec_pending = False
            saw_exec = True
            redirect_target = False
            continue
        if redirect_target:
            redirect_target = False
            if token.startswith("&"):
                continue
            verdict = _check_path_token(token, base, root, redirect=True)
            if verdict is not None:
                return verdict
            continue
        if _looks_like_path(token) or _absolute_kind(token) or "=" in token:
            for part in _path_parts(token):
                verdict = _check_path_token(part, base, root)
                if verdict is not None:
                    return verdict
    if not saw_exec:
        return _deny("命令为空：cmd 必须是非空字符串", f"no executable token in {cmd!r}")
    return _OK


def _path_key(env: dict) -> str:
    """`PATH` 在 Windows 上可能叫 `Path`：环境变量名大小写不敏感，键名却不是。"""
    for key in env:
        if key.upper() == "PATH":
            return key
    return "PATH"


def _prepend_service_interpreter(env: dict) -> None:
    """把「跑服务的这个解释器」放到子进程 PATH 的最前面。

    没有这一步，`python -m pytest` 用的是 PATH 上碰巧排第一的那个 python——
    和跑服务的解释器不是同一个，于是 `python -m jagent.api` 起服务后
    「跑回归测试」必然 `No module named pytest`（本机就是这样红的）。

    白名单又不允许绝对路径，用户没有别的办法指定解释器；所以「命令里的 python
    就是跑 JAgent 的这个 python」是这里唯一自洽的语义。

    **不做 resolve()**（D-038 容器验证发现）：Linux 的 uv/venv python 是指向
    真实解释器的符号链接，resolve 会把 exe_dir 解析到系统 python 目录——前置
    失效，`python -m pytest` 落到系统解释器（No module named pytest）。按
    未解析的字符串去重即可，两平台语义一致。
    """
    exe_dir = str(Path(sys.executable).parent)
    key = _path_key(env)
    parts = [part for part in env.get(key, "").split(os.pathsep) if part]
    rest = [part for part in parts if part != exe_dir]
    env[key] = os.pathsep.join([exe_dir, *rest])


def scrub_env(base: dict | None = None, workspace: Path | None = None) -> dict:
    """给子进程一份把 home/tmp 关进 workspace 的环境变量。

    ``HOME`` / ``USERPROFILE`` 指向 ``workspace/.sandbox/home``，
    ``TEMP`` / ``TMP`` 指向 ``workspace/.sandbox/tmp``，目录不存在时创建；
    其余变量（``PATH``、``SystemRoot`` 等）原样保留，否则子进程连解释器都找不到。
    ``PATH`` 会被重排：服务自己的解释器目录排在最前，见 ``_prepend_service_interpreter``。

    :param base: 起始环境变量，默认 ``os.environ``；不会被原地修改。
    :param workspace: 不传时只复制一份环境变量，不做任何改写/建目录。
    """
    env = dict(os.environ if base is None else base)
    if workspace is None:
        return env
    sandbox = Path(workspace).resolve() / ".sandbox"
    home = sandbox / "home"
    tmp = sandbox / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    for key in [key for key in env if key.upper() in _SCRUBBED_KEYS]:
        # Windows 的环境变量大小写不敏感，先清掉 `Home` 这类同义键再写。
        env.pop(key, None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    if sys.platform == "win32":
        # Git Bash（MSYS2）调用原生 exe 时会改写 POSIX 风格参数（/etc → Git 安装
        # 目录）：关掉改写，放行命令的参数不被静默变形（D-037）。
        env["MSYS2_ARG_CONV_EXCL"] = "*"
    _prepend_service_interpreter(env)
    return env


__all__ = [
    "ALLOWED_BINARIES",
    "SandboxVerdict",
    "check_command",
    "is_absolute_spelling",
    "scrub_env",
]
