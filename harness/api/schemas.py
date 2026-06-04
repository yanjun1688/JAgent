"""Request/Response Pydantic models for REST API and WebSocket.

All response models define the OpenAPI schema shapes consumed by the frontend.
Every publicly readable field has an explicit type — no bare dicts crossing the API boundary.
"""

from pydantic import BaseModel


class CreateRunRequest(BaseModel):
    intent: str


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
    summary: str | None = None
    pause_reason: str | None = None
    pending_confirmations: list[PendingConfirmationItem] = []


class EventResponse(BaseModel):
    run_id: str
    seq: int
    event_type: str
    payload: dict
    idempotency_key: str | None = None
    created_at: float


class RunListResponse(BaseModel):
    runs: list[RunSummary]
    total: int


class EventListResponse(BaseModel):
    events: list[EventResponse]
    total: int
