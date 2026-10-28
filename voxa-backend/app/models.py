"""SQLAlchemy ORM models for users, conversations, orders, the RAG knowledge base and tickets."""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    DateTime,
    ForeignKey,
    Index
)
from sqlalchemy.dialects.postgresql import UUID
from pgvector.sqlalchemy import Vector

from app.database import Base

def generate_uuid():
    return uuid.uuid4()

def utc_now():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=generate_uuid)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(50), default="user", nullable=False) # 'user' or 'admin'
    created_at = Column(DateTime(timezone=True), default=utc_now)


class Conversation(Base):
    """A chat thread. Only created for authenticated users (user_id is NOT NULL)."""
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=generate_uuid)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), default="New Support Chat")
    created_at = Column(DateTime(timezone=True), default=utc_now)


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=generate_uuid)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    sender = Column(String(20), nullable=False) # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    ttft_ms = Column(Integer, nullable=True) # Time to first token in ms
    total_latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("idx_messages_conversation", "conversation_id", "created_at"),
    )


class Order(Base):
    __tablename__ = "orders"

    id = Column(String(50), primary_key=True) # e.g. 'ORD-88412'
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    item_name = Column(String(255), nullable=False)
    order_date = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(50), nullable=False) # 'Shipped', 'Delivered', 'Cancelled', 'Processing'
    tracking_number = Column(String(100), nullable=True)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_base"

    id = Column(Integer, primary_key=True, autoincrement=True)
    topic = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(384), nullable=False) # all-MiniLM-L6-v2 384-dimensional vector

    __table_args__ = (
        # HNSW gives approximate nearest-neighbour search instead of a sequential scan;
        # vector_cosine_ops matches the `<=>` operator used in queries.
        Index("idx_knowledge_base_embedding", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=generate_uuid)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    issue_summary = Column(Text, nullable=False)
    priority = Column(String(20), default="medium") # 'low', 'medium', 'high', 'urgent'
    status = Column(String(50), default="open") # 'open', 'in_progress', 'resolved'
    created_at = Column(DateTime(timezone=True), default=utc_now)

