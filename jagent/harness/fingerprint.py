"""JAgent v8 · 输入指纹（fingerprint）。

用途：判断「这一步的输入有没有变」。变了就必须重跑并让下游 stale；
没变就允许复用上次的产物。指纹只看内容，不看 mtime——touch 一下不算变化。

两层：
* `fingerprint(paths)`：对**指定文件**求摘要（旧的、精确的入口）；
* `snapshot_workspace(root)`：对**整个 workspace** 求摘要（G-05 恢复对账用）。
  递归、跳过 `.git/node_modules/__pycache__`、文件数超上限就退化成「目录 + 大小」，
  避免为了一棵树把几千个文件全读一遍。

只读：本模块不写任何文件，缺文件记为 `"missing"` 而不是抛错（缺也是一种事实）。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..contracts import digest_of

#: 文件不存在（或在读的时候消失）时的占位值。
MISSING = "missing"

#: 递归 workspace 时跳过的目录名：版本库 / 依赖 / 解释器缓存都不是「输入」。
SKIP_DIRS: frozenset[str] = frozenset({".git", "node_modules", "__pycache__"})

#: 单次 workspace 指纹最多哈希多少个文件；超了就只按「目录 + 大小」算。
MAX_FILES = 2000

#: 快照两种模式：逐个文件算内容（准），或只按目录聚合大小（快、够用）。
MODE_FILES = "files"
MODE_DIRS = "dirs+size"


def _digest_file(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return MISSING


def fingerprint(paths: Sequence[Path], *, extra: dict[str, Any] | None = None) -> str:
    """对 `paths` 的内容 + 文件名 + `extra` 求一个稳定摘要。

    路径按入参顺序参与计算（同一个序列每次求值都得到同一个串）；
    条目里带文件名，避免「换了路径但内容相同」被当成没变。
    """
    entries: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        exists = path.is_file()
        entries.append(
            {
                "name": path.name,
                "path": str(path),
                "sha256": _digest_file(path) if exists else MISSING,
            }
        )
    return digest_of({"files": entries, "extra": dict(extra or {})})


def changed(before: str, after: str) -> bool:
    """两次指纹是否不同。缺省值不特殊化：不同就是变了。"""
    return before != after


# --------------------------------------------------------------------------- #
# workspace 快照（G-05：恢复对账）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Snapshot:
    """一次 workspace 指纹的结果。`fp_hash` 之外的信息只为**可审计**（事件里看得见）。"""

    fp_hash: str
    mode: str = MODE_FILES
    files: int = 0
    truncated: bool = False


def _walk(root: Path, skip_dirs: frozenset[str]) -> list[Path]:
    """按确定性顺序列出 root 下的文件（不进 skip_dirs）。"""
    files: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        # 原地改 dirnames 才是「不进去」；排序保证两次遍历得到同一个顺序。
        dirnames[:] = sorted(name for name in dirnames if name not in skip_dirs)
        files.extend(Path(current) / name for name in sorted(filenames))
    return files


def workspace_paths(
    workspace: str | Path,
    *,
    max_files: int = MAX_FILES,
    skip_dirs: frozenset[str] = SKIP_DIRS,
) -> tuple[list[Path], bool]:
    """`(files, truncated)`。文件数超过 `max_files` 时返回 `([], True)`。

    上限的意义：恢复要快。一棵几万文件的树逐个 hash 会让「隔天回来接着跑」
    变成「先卡十分钟」，那比不精确更糟。
    """
    root = Path(workspace)
    if not root.is_dir():
        return [], False
    files = _walk(root, skip_dirs)
    if len(files) > max_files:
        return [], True
    return files, False


def _dir_rollup(root: Path, skip_dirs: frozenset[str]) -> list[dict[str, Any]]:
    """只按「目录 + 文件数 + 总字节」聚合：不读内容，但大小变了就看得见。"""
    totals: dict[str, list[int]] = {"./": [0, 0]}
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in skip_dirs)
        rel = Path(current).relative_to(root).as_posix()
        bucket = totals.setdefault(rel, [0, 0])
        for name in filenames:
            try:
                size = (Path(current) / name).stat().st_size
            except OSError:  # 读不到大小也是事实：记 -1 而不是崩
                size = -1
            bucket[0] += 1
            bucket[1] += size
    return [{"dir": rel, "files": n, "bytes": size} for rel, (n, size) in sorted(totals.items())]


def snapshot_workspace(
    workspace: str | Path,
    *,
    max_files: int = MAX_FILES,
    skip_dirs: frozenset[str] = SKIP_DIRS,
) -> Snapshot:
    """对整个 workspace 采一次指纹。文件太多就退化成目录+大小（`mode` 如实记录）。"""
    root = Path(workspace)
    files, truncated = workspace_paths(root, max_files=max_files, skip_dirs=skip_dirs)
    if truncated:
        return Snapshot(
            fp_hash=digest_of({"mode": MODE_DIRS, "dirs": _dir_rollup(root, skip_dirs)}),
            mode=MODE_DIRS,
            files=0,
            truncated=True,
        )
    return Snapshot(fp_hash=fingerprint(files), mode=MODE_FILES, files=len(files), truncated=False)


def file_digest(path: str | Path) -> str:
    """单个文件的内容摘要；缺席/不可读记 `MISSING`（缺也是一种事实）。"""
    return _digest_file(Path(path))


def probe_fingerprints(workspace: str | Path, probes: Sequence[str]) -> dict[str, str]:
    """逐个 probe 算内容摘要，key 是计划里声明的那个路径字符串。

    为什么不用整树指纹：`env_probes` 是计划自己声明的依赖，颗粒度到文件。
    拿整树指纹去判它，改一个无关文件就会把这一步也打回重跑——失效就不局部了。
    """
    root = Path(workspace)
    out: dict[str, str] = {}
    for raw in probes:
        path = Path(str(raw))
        target = path if path.is_absolute() else root / path
        out[str(raw)] = file_digest(target)
    return out

