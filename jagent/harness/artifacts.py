"""JAgent v8 · 内容寻址产物库。

产物 = 字节 + sha256。事件里只存 artifact_id（= sha256）与元数据，
重放时按需取回内容。这样事件日志保持小，证据又能被真正解析（而不是只有一个 hash）。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import canonical_json


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    sha256: str
    nbytes: int
    mime: str
    path: str


class ArtifactIntegrityError(RuntimeError):
    """产物内容与 artifact_id（sha256）不符：文件被替换或损坏。

    fail-closed：内容寻址库的可信度全在"读到的字节就是当初写的字节"；
    校验失败宁可让证据读取失败，也不把被污染的内容当真。"""


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, sha: str) -> Path:
        # artifact_id 来自事件，但事件也可能是被人改过的。只接受 64 位小写 hex，
        # 否则 `..` 或绝对路径会逃出 artifacts 根目录（红队 RT-11）。
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError(f"invalid artifact id: {sha!r}")
        return self.root / sha[:2] / sha

    def put_bytes(self, data: bytes, *, mime: str = "application/octet-stream") -> ArtifactRef:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path_for(sha)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Windows 并发 mkdir 的经典竞争（bpo-29715）：目录刚被别的线程建好时，
            # exist_ok 也可能收到 EACCES 而不是 EEXIST。目录在就当成功。
            if not path.parent.is_dir():
                raise
        if not path.exists():
            # 唯一临时名 + 原子 replace（BUG2-05）：固定 `.tmp` 名会让同内容并发写
            # 在 Windows 上撞出 WinError 32/5；同 sha ⇒ 同内容，后写覆盖无害。
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            try:
                self._atomic_replace(tmp, path)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)  # 失败残留清理；成功时 tmp 已被移走
        return ArtifactRef(artifact_id=sha, sha256=sha, nbytes=len(data), mime=mime, path=str(path))

    @staticmethod
    def _atomic_replace(tmp: Path, path: Path) -> None:
        """Windows 上并发 ``os.replace`` 到同一目标会短暂抛 WinError 5。

        内容寻址库的同名目标必然同内容（同 sha ⇒ 同字节）：目标一旦就位，
        任何一次成功落盘都等价——短暂退避重试即可收敛。
        """
        for attempt in range(5):
            if path.exists():
                return
            try:
                tmp.replace(path)
                return
            except PermissionError:
                time.sleep(0.005 * (attempt + 1))
        tmp.replace(path)  # 最后一搏不加退避：仍失败则把真实错误抛给调用方

    def put_text(self, text: str, *, mime: str = "text/plain; charset=utf-8") -> ArtifactRef:
        return self.put_bytes(text.encode("utf-8"), mime=mime)

    def put_json(self, value: Any) -> ArtifactRef:
        return self.put_text(canonical_json(value), mime="application/json")

    def get_bytes(self, artifact_id: str) -> bytes:
        path = self._path_for(artifact_id)
        if not path.exists():
            raise FileNotFoundError(f"artifact {artifact_id} not found under {self.root}")
        data = path.read_bytes()
        # 内容完整性（BUG2-06）：文件可被原地替换，读时重算 sha256 对账 artifact_id。
        if hashlib.sha256(data).hexdigest() != artifact_id:
            raise ArtifactIntegrityError(
                f"artifact {artifact_id} 内容校验失败：sha256 不匹配（被替换或损坏）"
            )
        return data

    def get_text(self, artifact_id: str) -> str:
        return self.get_bytes(artifact_id).decode("utf-8", errors="replace")

    def exists(self, artifact_id: str) -> bool:
        return self._path_for(artifact_id).exists()
