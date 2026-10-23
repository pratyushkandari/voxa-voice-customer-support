"""
Async SQLAlchemy engine, session factory and schema bootstrap.

The engine is created lazily-connecting (no I/O at import time), so importing this
module never requires a running database.
"""
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings

logger = logging.getLogger("voxa.database")

# Connection-pool sizing only applies to real server databases; SQLite (used by some
# local experiments) rejects these arguments.
_engine_kwargs = {"echo": False, "pool_pre_ping": True}
if not settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs.update(pool_size=20, max_overflow=10)

# SQLAlchemy 2.0 async engine with connection pooling
engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

Base = declarative_base()


async def get_db():
    """FastAPI dependency yielding a request-scoped session (closed by the context manager)."""
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    """
    Idempotently creates the pgvector extension, all ORM tables, and the HNSW index.

    Safe to call on every gateway start (``CREATE ... IF NOT EXISTS`` everywhere).
    The HNSW index is created separately because SQLAlchemy has no portable DDL for it.
    """
    # Import models so they are registered on Base.metadata before create_all().
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        logger.info("Enabling pgvector extension if not exists...")
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))

        await conn.run_sync(Base.metadata.create_all)

        # HNSW gives approximate nearest-neighbour search in roughly O(log N) instead of
        # a sequential scan; vector_cosine_ops matches the `<=>` operator used in queries.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_base_embedding "
            "ON knowledge_base USING hnsw (embedding vector_cosine_ops);"
        ))
        logger.info("Database schema, pgvector extension and HNSW index verified.")
