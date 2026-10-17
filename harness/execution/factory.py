from __future__ import annotations

import asyncio

from harness.core.tenant import current_tenant_id_or_none
from harness.execution.base import ExecutionBackend
from harness.execution.boundary import validate_workspace_target
from harness.execution.docker import DockerSandboxBackend
from harness.execution.local import LocalDirectoryBackend
from harness.execution.ssh import RemoteSSHBackend
from harness.models.workspace import ExecutionTarget, ExecutionTargetType


async def create_backend(
    target: ExecutionTarget,
    *,
    run_id: str | None = None,
    tenant_id: str | None = None,
) -> ExecutionBackend:
    effective_tenant = tenant_id or current_tenant_id_or_none()
    validated = (
        await asyncio.to_thread(validate_workspace_target, target, effective_tenant)
        if effective_tenant is not None
        else None
    )
    if target.type == ExecutionTargetType.DIRECTORY:
        return LocalDirectoryBackend(
            str(validated) if validated is not None else target.filesystem_root or ""
        )
    if target.type == ExecutionTargetType.SANDBOX:
        backend = DockerSandboxBackend(
            target.docker_image or "",
            str(validated) if validated is not None else target.host_mount_src or "",
            target.mount_root or "/workspace",
            run_id=run_id,
            tenant_id=effective_tenant,
        )
        await backend.check_available()
        return backend
    if target.type == ExecutionTargetType.REMOTE:
        return RemoteSSHBackend(
            target.host or "",
            target.port,
            target.username or "",
            str(validated) if validated is not None else target.private_key_path or "",
            target.remote_root or "/workspace",
        )
    raise ValueError(f"Unsupported execution target: {target.type}")
