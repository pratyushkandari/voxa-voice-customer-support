from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime
from uuid import UUID

# ----------------- Auth Schemas -----------------
class UserCreate(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., min_length=6)

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

# ----------------- Chat & Stream Schemas -----------------
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    conversation_id: Optional[UUID] = None
    stream: bool = True

class ChatChunk(BaseModel):
    token: str
    is_final: bool = False
    ttft_ms: Optional[int] = None
    total_latency_ms: Optional[int] = None
    trace_id: Optional[str] = None

class MessageSchema(BaseModel):
    id: UUID
    conversation_id: UUID
    sender: str
    content: str
    ttft_ms: Optional[int] = None
    total_latency_ms: Optional[int] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class ConversationSchema(BaseModel):
    id: UUID
    title: str
    created_at: datetime
    messages: List[MessageSchema] = []

    model_config = ConfigDict(from_attributes=True)

# ----------------- Telemetry HUD Schemas -----------------
class TelemetryData(BaseModel):
    ttft_ms: float
    tokens_per_sec: float
    stt_latency_ms: Optional[float] = None
    total_latency_ms: float
    trace_id: str
    cache_hit: bool = False

# ----------------- Business Tools Schemas -----------------
class OrderLookupResponse(BaseModel):
    order_id: str
    item_name: str
    order_date: datetime
    status: str
    tracking_number: Optional[str]

class RefundEligibilityResponse(BaseModel):
    order_id: str
    eligible: bool
    days_since_purchase: int
    reason: str

class TicketCreateRequest(BaseModel):
    issue_summary: str
    priority: str = "medium"

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
    feedback_text: Optional[str] = None

class FeedbackResponse(BaseModel):
    id: UUID
    message_id: UUID
    score: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

# ----------------- Admin SRE Schemas -----------------
class AdminMetricsResponse(BaseModel):
    active_connections: int
    queue_depth: int
    cache_hit_ratio: float
    avg_ttft_ms: float
    p95_latency_ms: float
    active_workers: int
    total_conversations: int
    total_messages: int
