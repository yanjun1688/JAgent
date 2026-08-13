"""Request/Response Pydantic models for REST API and WebSocket.

All response models define the OpenAPI schema shapes consumed by the frontend.
Every publicly readable field has an explicit type — no bare dicts crossing the API boundary.
"""

from typing import Any

from pydantic import BaseModel, Field

from harness.models.intent import DeliveryOperationInput
from harness.models.workspace import WorkspaceScope


class RequiredOperationInput(DeliveryOperationInput):
    """S07 (D-02): 调用方显式声明的硬性交付操作（方案 A，source=caller）。

    tool + input 与后端 DeliveryContract 对齐（同源契约）。未提供时由系统
    抽取兜底（source=extracted）。
    """

    tool: str
    input: dict[str, Any] = Field(default_factory=dict)


class CreateRunRequest(BaseModel):
    intent: str
    conversation_id: str | None = None
    client_request_id: str | None = None
    workspace_id: str | None = None
    # S07 (D-02): 可选的结构化交付契约 — 调用方显式声明，不依赖 LLM 猜意图。
    # 未提供 → 系统从 intent 抽取（source=extracted）或空契约（D-04 unverified）。
    required_operations: list[RequiredOperationInput] | None = None


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    scope: WorkspaceScope


class WorkspaceResponse(BaseModel):
    workspace_id: str
    tenant_id: str
    name: str
    description: str
    scope: WorkspaceScope
    status: str
    run_count: int = 0
    created_at: float
    updated_at: float


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceResponse]
    total: int


class SuccessResponse(BaseModel):
    success: bool


class CreateRunResponse(BaseModel):
    run_id: str


class RunControlResponse(BaseModel):
    success: bool


class ConfirmationResponse(BaseModel):
    success: bool
    message: str | None = None


class FeedbackResponse(BaseModel):
    status: str
    feedback_id: str


class ConfirmRequest(BaseModel):
    confirmation_id: str
    confirmed: bool
    operator_id: str = "operator"


class PauseRequest(BaseModel):
    reason: str = "user_requested"


class RunSummary(BaseModel):
    run_id: str
    intent: str = ""
    status: str = "running"
    event_count: int = 0
    created_at: float = 0
    updated_at: float = 0
    orphaned: bool = False
    workspace_id: str | None = None


class PendingConfirmationItem(BaseModel):
    confirmation_id: str
    tool_name: str
    tool_call_id: str
    input: dict
    risk_level: str


class RunDetailResponse(BaseModel):
    run_id: str
    status: str
    intent: str
    seq: int
    event_count: int
    last_error: str | None = None
    summary: Any = None
    pause_reason: str | None = None
    pending_confirmations: list[PendingConfirmationItem] = []
    conversation_id: str | None = None
    orphaned: bool = False
    workspace_id: str | None = None


class EventResponse(BaseModel):
    run_id: str
    seq: int
    event_type: str
    payload: dict
    idempotency_key: str | None = None
    created_at: float
    tenant_id: str = "default"
    workspace_id: str | None = None


class RunListResponse(BaseModel):
    runs: list[RunSummary]
    total: int


class EventListResponse(BaseModel):
    events: list[EventResponse]
    total: int
