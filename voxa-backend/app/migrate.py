"""
Brings the database schema up to date: ``python -m app.migrate``.

Runs once per deploy before the gateway and workers start (the ``migrate`` service in
docker-compose.yml), so replicas never race to change the schema. Databases created
before migrations existed already have the tables from the baseline revision, so they
are stamped at it first rather than re-created.
"""
import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

logger = logging.getLogger("voxa.migrate")

BASELINE_REVISION = "0001"
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def needs_baseline_stamp(tables: set[str]) -> bool:
    """True for a database built by the old create_all() start-up: tables, but no history."""
    return "users" in tables and "alembic_version" not in tables


async def _existing_tables() -> set[str]:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))
    finally:
        await engine.dispose()


def main() -> None:
    config = Config(str(ALEMBIC_INI))
    if needs_baseline_stamp(asyncio.run(_existing_tables())):
        logger.info(f"Existing schema without migration history; stamping revision {BASELINE_REVISION}")
        command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
