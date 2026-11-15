"""P2-a · Artifact Store 完整性与并发（BUG2-05 / BUG2-06，QA 报告 v8-wave2-bugs-qa2）。

BUG2-06：``get_bytes`` 不校验 sha256——产物文件被原地替换后按原 id 原样返回，
证据被静默污染。修复：读取时重算 sha256，与 artifact_id 不符即抛
``ArtifactIntegrityError``（fail-closed；``_path_for`` 已挡路径逃逸，
这里补上内容维度的完整性）。

BUG2-05：同内容并发写共享同一个 ``.tmp`` 固定名，Windows 上抛
PermissionError（WinError 32/5）。修复：每次写用唯一临时名 + 原子 replace，
同内容并发写全部成功、落盘内容一致（同 sha ⇒ 同内容，后写覆盖无害）。
"""

from __future__ import annotations

import hashlib
import threading

import pytest

from jagent.harness.artifacts import ArtifactIntegrityError, ArtifactStore


@pytest.fixture()
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


# --------------------------------------------------------------------------- #
# BUG2-06：读取校验 sha256
# --------------------------------------------------------------------------- #

def test_get_bytes_verifies_sha256_and_rejects_tampered_content(store) -> None:
    """Given 落盘一个产物、文件内容被原地篡改，When get_bytes，
    Then ArtifactIntegrityError——被替换的内容不能冒充原产物。"""
    ref = store.put_bytes(b"hello evidence")
    assert ref.artifact_id == hashlib.sha256(b"hello evidence").hexdigest()

    path = store._path_for(ref.artifact_id)
    path.write_bytes(b"tampered!!")

    with pytest.raises(ArtifactIntegrityError):
        store.get_bytes(ref.artifact_id)


def test_get_bytes_returns_untampered_content(store) -> None:
    """Given 正常产物，When get_bytes，Then 原样返回（校验通过不误伤）。"""
    ref = store.put_bytes(b"clean payload")
    assert store.get_bytes(ref.artifact_id) == b"clean payload"


def test_text_and_json_read_paths_verify_too(store) -> None:
    """Given 篡改后的产物，When get_text / get_json，Then 同样拒绝——
    两个读取入口走同一校验，不给篡改留旁路。"""
    ref = store.put_json({"value": 42})
    store._path_for(ref.artifact_id).write_bytes(b'{"value": 999}')

    with pytest.raises(ArtifactIntegrityError):
        store.get_text(ref.artifact_id)


# --------------------------------------------------------------------------- #
# BUG2-05：并发同内容写不再共享 .tmp 固定名
# --------------------------------------------------------------------------- #

def test_concurrent_same_content_writes_all_succeed(store) -> None:
    """Given 多线程并发写同一内容（同 sha），When put_bytes，
    Then 全部成功、get_bytes 返回该内容——.tmp 不共享，竞争消失。"""
    data = b"same content" * 256
    errors: list[Exception] = []
    start = threading.Barrier(8)

    def worker() -> None:
        try:
            start.wait()
            store.put_bytes(data)
        except Exception as exc:  # noqa: BLE001 - 竞争错误要收集对比
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"并发写失败：{errors!r}"
    sha = hashlib.sha256(data).hexdigest()
    assert store.get_bytes(sha) == data


def test_concurrent_different_content_writes_isolated(store) -> None:
    """Given 并发写不同内容（不同 sha），When put_bytes，
    Then 每个产物各自可读——临时名唯一，互不覆盖。"""
    errors: list[Exception] = []
    start = threading.Barrier(6)

    def worker(index: int) -> None:
        try:
            start.wait()
            store.put_bytes(f"payload-{index}".encode())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"并发写失败：{errors!r}"
    for i in range(6):
        sha = hashlib.sha256(f"payload-{i}".encode()).hexdigest()
        assert store.get_bytes(sha) == f"payload-{i}".encode()
