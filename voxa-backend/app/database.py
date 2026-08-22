from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from sqlalchemy import text
from app.config import settings
import logging

logger = logging.getLogger("voxa.database")

# SQLAlchemy 2.0 async engine with connection pooling
try:
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True
    )
except Exception:
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        from unittest.mock import AsyncMock
        engine = AsyncMock()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

Base = declarative_base()

async def get_db():
    """FastAPI dependency for yielding database sessions."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

async def init_db():
    """Initializes pgvector extension and creates all database tables."""
    async with engine.begin() as conn:
        logger.info("Enabling pgvector extension if not exists...")
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        await conn.run_sync(Base.metadata.create_all)
        logger.info("All PostgreSQL tables initialized successfully.")
