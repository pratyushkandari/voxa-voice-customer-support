"""
Async SQLAlchemy engine and session factory. The schema is managed by Alembic
migrations (``python -m app.migrate``).

The engine is created lazily-connecting (no I/O at import time), so importing this
module never requires a running database.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings

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
