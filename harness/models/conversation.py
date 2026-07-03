from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Conversation(BaseModel):
    conversation_id: str
    user_id: str = "default"
    title: str
    status: ConversationStatus = ConversationStatus.ACTIVE
    created_at: float
    updated_at: float
    message_count: int = 0


class ConversationMessageItem(BaseModel):
    seq: int
    run_id: str
    role: str
    content: str
    created_at: float
    status: str


class ConversationDetail(BaseModel):
    conversation: Conversation
    messages: list[ConversationMessageItem]


class CreateConversationRequest(BaseModel):
    title: Optional[str] = None


class SendMessageRequest(BaseModel):
    message: str


class UpdateConversationRequest(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None


class ConversationListResponse(BaseModel):
    conversations: list[Conversation]
    total: int


class CreateConversationResponse(BaseModel):
    conversation_id: str
    title: str
    created_at: float


class SendMessageResponse(BaseModel):
    run_id: str
    conversation_id: str
    seq: int


class DeleteConversationResponse(BaseModel):
    success: bool


class UpdateConversationResponse(BaseModel):
    success: bool


async def _build_conversation_context(store, conversation_id: str, max_rounds: int = 3) -> str:
    from harness.models.events import EventType

    events = await store.get_events_for_conversation(conversation_id)
    messages: list[str] = []
    for e in events:
        if e.event_type == EventType.CONVERSATION_MESSAGE:
            p = e.payload
            role = p.get("role", "unknown")
            content = p.get("content", "")
            messages.append(f"[{role}] {content[:500]}")
        elif e.event_type == EventType.CONVERSATION_STARTED:
            pass

    max_messages = max_rounds * 2
    recent = messages[-max_messages:] if len(messages) > max_messages else messages
    return "\n".join(recent)
