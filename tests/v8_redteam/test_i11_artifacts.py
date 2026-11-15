"""I11 · 产物库：内容寻址只能按 id 取，不能变成任意文件读取。

规格依据：
- §3.1「Artifact Store 存大块内容（工具输出、建议全文、报告），id = sha256」；
- §3.1「事件只存引用（artifact_id）」，重放时按需读产物；
- 受信边界：artifact_id 来自事件/报告，若可指向任意绝对路径，回放/展示就变成读文件原语。
"""

from __future__ import annotations

import pytest

from jagent.harness.artifacts import ArtifactStore


def test_artifact_ids_are_content_addressed(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    ref = store.put_text("hello")

    assert ref.artifact_id == ref.sha256
    assert len(ref.artifact_id) == 64
    assert store.get_text(ref.artifact_id) == "hello"


def test_artifact_store_cannot_read_an_absolute_path(tmp_path) -> None:
    """已修复（本轮）：_path_for 只接受 64 位小写 hex，绝对路径直接 ValueError。"""

    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    store = ArtifactStore(tmp_path / "artifacts")

    with pytest.raises((FileNotFoundError, ValueError)):
        store.get_text(str(secret))


def test_artifact_store_cannot_traverse_with_dotdot(tmp_path) -> None:
    """已修复（本轮）：非 hex 的 id（含 ..）不会再拼进文件路径。"""

    store = ArtifactStore(tmp_path / "a" / "b" / "artifacts")
    secret = tmp_path / "a" / "b" / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")

    with pytest.raises((FileNotFoundError, ValueError)):
        store.get_text("no/../../../secret.txt")
