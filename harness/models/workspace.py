"""Tenant and workspace execution-boundary models."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ExecutionTargetType(str, Enum):
    DIRECTORY = "directory"
    SANDBOX = "sandbox"
    REMOTE = "remote"


class ExecutionTarget(BaseModel):
    type: ExecutionTargetType
    filesystem_root: str | None = None
    docker_image: str | None = None
    host_mount_src: str | None = None
    mount_root: str | None = "/workspace"
    host: str | None = None
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = None
    private_key_path: str | None = None
    remote_root: str | None = "/workspace"

    @model_validator(mode="after")
    def validate_target(self) -> "ExecutionTarget":
        required = {
            ExecutionTargetType.DIRECTORY: ("filesystem_root",),
            ExecutionTargetType.SANDBOX: ("docker_image", "host_mount_src", "mount_root"),
            ExecutionTargetType.REMOTE: ("host", "username", "private_key_path", "remote_root"),
        }[self.type]
        missing = [name for name in required if getattr(self, name) in (None, "")]
        if missing:
            raise ValueError(f"{self.type.value} target requires: {', '.join(missing)}")
        return self


class WorkspaceScope(BaseModel):
    target: ExecutionTarget
    allowed_tools: list[str] | None = None


class Workspace(BaseModel):
    workspace_id: str
    tenant_id: str
    name: str
    description: str = ""
    scope: WorkspaceScope
    status: Literal["active", "deleted"] = "active"
    created_at: float
    updated_at: float


class Tenant(BaseModel):
    tenant_id: str
    name: str = ""
    status: Literal["active", "suspended"] = "active"
    created_at: float
    updated_at: float


class WorkspaceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    scope: WorkspaceScope | None = None
