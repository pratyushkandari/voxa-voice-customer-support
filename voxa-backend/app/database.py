"""
Async SQLAlchemy engine and session factory. The schema is managed by Alembic
migrations (``python -m app.migrate``).

The engine is created lazily-connecting (no I/O at import time), so importing this
module never requires a running database.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings

# pool_pre_ping replaces connections that died (e.g. after a PostgreSQL restart) before
# they are handed out, instead of failing the next request.
engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=10)

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
