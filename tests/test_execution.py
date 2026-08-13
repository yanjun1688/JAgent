from __future__ import annotations

from pathlib import Path

import pytest

from harness.execution.docker import CommandFailedError, DockerSandboxBackend, SandboxUnavailableError
from harness.execution.factory import create_backend
from harness.execution.local import LocalDirectoryBackend
from harness.execution.ssh import RemoteSSHBackend
from harness.models.workspace import ExecutionTarget, ExecutionTargetType


@pytest.mark.asyncio
async def test_local_backend_rejects_absolute_and_parent_escape(tmp_path):
    backend = LocalDirectoryBackend(str(tmp_path))
    with pytest.raises(PermissionError):
        await backend.resolve("../outside.txt")
    with pytest.raises(PermissionError):
        await backend.resolve(str(tmp_path / "outside.txt"))


@pytest.mark.asyncio
async def test_local_backend_file_operations_are_rooted(tmp_path):
    backend = LocalDirectoryBackend(str(tmp_path))
    assert (await backend.write("nested/file.txt", "hello"))["success"]
    assert (await backend.append("nested/file.txt", " world"))["size"] == 11
    assert (await backend.read("nested/file.txt"))["content"] == "hello world"
    assert (await backend.list("nested"))["content"] == "nested/file.txt"
    assert (await backend.delete("nested/file.txt"))["success"]


@pytest.mark.asyncio
async def test_local_backend_missing_file_returns_error_contract(tmp_path):
    backend = LocalDirectoryBackend(str(tmp_path))
    result = await backend.read("missing.txt")
    assert result["success"] is False
    assert "not found" in result["error"]


# ── Factory ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_backend_directory():
    target = ExecutionTarget(type=ExecutionTargetType.DIRECTORY, filesystem_root="data/t")
    backend = await create_backend(target)
    assert isinstance(backend, LocalDirectoryBackend)


@pytest.mark.asyncio
async def test_create_backend_remote():
    target = ExecutionTarget(
        type=ExecutionTargetType.REMOTE,
        host="host",
        port=22,
        username="u",
        private_key_path="k",
        remote_root="/w",
    )
    backend = await create_backend(target)
    assert isinstance(backend, RemoteSSHBackend)


@pytest.mark.asyncio
async def test_create_backend_unknown_type():
    target = ExecutionTarget(type=ExecutionTargetType.DIRECTORY, filesystem_root="data/t")
    target.type = "unknown"  # type: ignore[assignment]
    with pytest.raises(ValueError):
        await create_backend(target)


@pytest.mark.asyncio
async def test_create_backend_sandbox_unavailable_when_docker_missing(monkeypatch):
    monkeypatch.setattr("harness.execution.docker.shutil.which", lambda _: None)
    target = ExecutionTarget(type=ExecutionTargetType.SANDBOX, docker_image="img", host_mount_src="data/m")
    with pytest.raises(SandboxUnavailableError):
        await create_backend(target)


# ── Docker backend ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_docker_backend_resolve_rejects_escape():
    backend = DockerSandboxBackend("img", "data/m", "/workspace")
    assert Path(backend.host_src).is_absolute()
    with pytest.raises(PermissionError):
        await backend.resolve("../outside.txt")
    with pytest.raises(PermissionError):
        await backend.resolve("/etc/passwd")


@pytest.mark.asyncio
async def test_docker_backend_file_failure_returns_error_contract(monkeypatch):
    backend = DockerSandboxBackend("img", "data/m", "/workspace")

    async def fake_exec(*args, **kwargs):
        raise CommandFailedError("cat: /workspace/missing: No such file")

    monkeypatch.setattr(backend, "_exec", fake_exec)
    result = await backend.read("missing.txt")
    assert result["success"] is False
    assert "missing.txt" in result["path"] or "Read failed" in result["error"]


# ── SSH backend ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ssh_backend_resolve_rejects_escape():
    backend = RemoteSSHBackend("h", 22, "u", "k", "/workspace")
    with pytest.raises(PermissionError):
        await backend.resolve("../outside.txt")
    with pytest.raises(PermissionError):
        await backend.resolve("/etc/passwd")


@pytest.mark.asyncio
async def test_ssh_backend_file_failure_returns_error_contract(monkeypatch):
    backend = RemoteSSHBackend("h", 22, "u", "k", "/workspace")

    class FakeSftp:
        def open(self, target, mode):
            raise OSError("No such file")

    async def fake_ensure():
        return FakeSftp()

    monkeypatch.setattr(backend, "_ensure", fake_ensure)
    result = await backend.read("missing.txt")
    assert result["success"] is False
    assert "Read failed" in result["error"]


@pytest.mark.asyncio
async def test_ssh_backend_mock_sftp_read_write_list_delete(monkeypatch):
    backend = RemoteSSHBackend("h", 22, "u", "k", "/workspace")

    class FakeFile:
        def __init__(self, content=""):
            self.content = content

        def read(self):
            return self.content

        def write(self, content):
            self.content = content

    class FakeSftp:
        def __init__(self):
            self.files = {}

        def open(self, target, mode):
            file = self.files.setdefault(target, FakeFile())
            if mode == "w":
                file.content = ""
            return file

        def listdir(self, target):
            return ["a.txt"]

        def remove(self, target):
            self.files.pop(target, None)

    sftp = FakeSftp()

    async def fake_ensure():
        return sftp

    monkeypatch.setattr(backend, "_ensure", fake_ensure)
    assert (await backend.write("a.txt", "hello"))["success"]
    assert (await backend.read("a.txt"))["content"] == "hello"
    assert (await backend.list("."))["content"] == "a.txt"
    assert (await backend.delete("a.txt"))["success"]


@pytest.mark.asyncio
async def test_ssh_backend_connection_failure_is_structured(monkeypatch):
    backend = RemoteSSHBackend("h", 22, "u", "k", "/workspace")

    async def failing_ensure():
        raise ConnectionError("connection refused")

    monkeypatch.setattr(backend, "_ensure", failing_ensure)
    with pytest.raises(ConnectionError, match="connection refused"):
        await backend.read("a.txt")


@pytest.mark.asyncio
async def test_ssh_backend_close_is_idempotent():
    backend = RemoteSSHBackend("h", 22, "u", "k", "/workspace")
    await backend.close()
    assert backend._sftp is None and backend._client is None
