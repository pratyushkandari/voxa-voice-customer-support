"""Pydantic request/response contracts for the public REST API."""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ----------------- Auth Schemas -----------------
class UserCreate(BaseModel):
    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=6, max_length=128)


class UserLogin(BaseModel):
    email: str = Field(..., description="User email address")
    password: str


class UserResponse(BaseModel):
    id: UUID
    email: str
    role: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


# ----------------- Chat Schemas -----------------
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    # Continue an existing conversation owned by the caller; omit to start a new one.
    conversation_id: Optional[UUID] = None


# ----------------- Ticket Schemas -----------------
class TicketResponse(BaseModel):
    id: UUID
    issue_summary: str
    priority: str
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ----------------- Feedback Schemas -----------------
class FeedbackCreate(BaseModel):
    message_id: UUID
    score: int = Field(..., ge=1, le=5)
    feedback_text: Optional[str] = Field(default=None, max_length=2000)


class FeedbackResponse(BaseModel):
    id: UUID
    message_id: UUID
    score: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ----------------- Admin SRE Schemas -----------------
class AdminMetricsResponse(BaseModel):
    active_connections: int  # open WebSockets on the gateway process that served this request
    queue_depth: int  # pending + undelivered entries across the STT and LLM consumer groups
    cache_hit_ratio: float  # semantic cache hits / (hits + misses); 0.0 when no lookups yet
    avg_ttft_ms: float  # mean time-to-first-token over persisted assistant messages
    p95_latency_ms: float  # 95th percentile end-to-end latency over persisted assistant messages
    active_workers: int  # workers with a live heartbeat key (10s TTL)
    total_conversations: int
    total_messages: int
