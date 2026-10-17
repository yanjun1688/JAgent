"""Workspace execution-root boundary (tenant binding).

The shared base directory is not a tenant boundary by itself. A workspace root
must live *below* ``<base>/<tenant_id>/``; the base, the tenant directory
itself, and other tenants' directories are rejected. Remote SSH keys are bound
to a separate tenant-scoped key directory so ``private_key_path`` cannot read
arbitrary host files.
"""

from __future__ import annotations

import os
from pathlib import Path

from harness.core.tenant import safe_tenant_dir_component
from harness.models.workspace import ExecutionTarget, ExecutionTargetType

WORKSPACE_BASE_DIR = Path(
    os.environ.get("JAGENT_WORKSPACE_BASE_DIR", "data/workspaces")
).expanduser().resolve()

REMOTE_KEY_BASE_DIR = Path(
    os.environ.get("JAGENT_REMOTE_KEY_BASE_DIR", "data/remote_keys")
).expanduser().resolve()


class WorkspaceRootError(ValueError):
    """Raised when an execution root escapes the tenant's boundary."""


def _resolve(value: str | Path, *, label: str = "Execution root") -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceRootError(f"{label} '{value}' cannot be resolved") from exc


def _tenant_dir(base: Path, tenant_id: str) -> Path:
    return _resolve(base / safe_tenant_dir_component(tenant_id))


def _validate_directory_root(value: str, tenant_id: str, base: Path) -> Path:
    tenant_root = _tenant_dir(base, tenant_id)
    resolved = _resolve(value)
    if resolved == tenant_root:
        raise WorkspaceRootError(
            f"Execution root '{value}' must be below tenant '{tenant_id}' "
            "workspace directory, not the tenant directory itself"
        )
    try:
        resolved.relative_to(tenant_root)
    except ValueError as exc:
        raise WorkspaceRootError(
            f"Execution root '{value}' must be inside tenant '{tenant_id}' workspace directory"
        ) from exc
    return resolved


def _validate_remote_key(value: str, tenant_id: str, base: Path) -> Path:
    tenant_root = _tenant_dir(base, tenant_id)
    resolved = _resolve(value, label="Remote private key")
    try:
        resolved.relative_to(tenant_root)
    except ValueError as exc:
        raise WorkspaceRootError(
            f"Remote private key must be inside tenant '{tenant_id}' key directory"
        ) from exc
    if not resolved.is_file():
        raise WorkspaceRootError("Remote private key must point to an existing file")
    return resolved


def validate_workspace_target(
    target: ExecutionTarget,
    tenant_id: str,
    *,
    base_dir: Path | None = None,
    remote_key_base_dir: Path | None = None,
) -> Path:
    """Validate ``target`` and return the resolved path it is bound to.

    For remote targets the returned path is the SSH private key. For directory
    and sandbox targets it is the host-side workspace root.
    """
    if target.type == ExecutionTargetType.REMOTE:
        key_base = (remote_key_base_dir or REMOTE_KEY_BASE_DIR).expanduser().resolve()
        value = target.private_key_path
        if not value:
            raise WorkspaceRootError("remote target requires a private_key_path")
        return _validate_remote_key(value, tenant_id, key_base)

    base = (base_dir or WORKSPACE_BASE_DIR).expanduser().resolve()
    value = (
        target.filesystem_root
        if target.type == ExecutionTargetType.DIRECTORY
        else target.host_mount_src
    )
    if not value:
        raise WorkspaceRootError(f"{target.type.value} target requires an execution root")
    return _validate_directory_root(value, tenant_id, base)
