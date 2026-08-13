from harness.execution.base import ExecutionBackend
from harness.execution.docker import DockerSandboxBackend
from harness.execution.local import LocalDirectoryBackend
from harness.execution.ssh import RemoteSSHBackend
from harness.models.workspace import ExecutionTarget, ExecutionTargetType


async def create_backend(target: ExecutionTarget) -> ExecutionBackend:
    if target.type == ExecutionTargetType.DIRECTORY:
        return LocalDirectoryBackend(target.filesystem_root or "")
    if target.type == ExecutionTargetType.SANDBOX:
        backend = DockerSandboxBackend(
            target.docker_image or "",
            target.host_mount_src or "",
            target.mount_root or "/workspace",
        )
        await backend.check_available()
        return backend
    if target.type == ExecutionTargetType.REMOTE:
        return RemoteSSHBackend(
            target.host or "",
            target.port,
            target.username or "",
            target.private_key_path or "",
            target.remote_root or "/workspace",
        )
    raise ValueError(f"Unsupported execution target: {target.type}")
