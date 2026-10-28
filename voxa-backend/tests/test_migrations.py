"""
Migration scripts, checked without a database by rendering their SQL (offline mode).
CI also applies them to a real PostgreSQL and runs ``alembic check`` against the models.
"""
import io

from alembic import command
from alembic.config import Config

from app import models  # noqa: F401  (registers the tables)
from app.database import Base
from app.migrate import ALEMBIC_INI, needs_baseline_stamp


def render_upgrade_sql() -> str:
    buffer = io.StringIO()
    command.upgrade(Config(str(ALEMBIC_INI), output_buffer=buffer), "head", sql=True)
    return buffer.getvalue()


def test_migrations_create_every_model_table_and_the_hnsw_index():
    sql = render_upgrade_sql()
    for table in Base.metadata.tables:
        assert f"CREATE TABLE {table} " in sql, table
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "USING hnsw (embedding vector_cosine_ops)" in sql
    assert "DROP TABLE IF EXISTS feedback_ratings" in sql


def test_only_databases_built_by_create_all_are_stamped():
    assert needs_baseline_stamp({"users", "orders", "knowledge_base"})          # pre-migration database
    assert not needs_baseline_stamp(set())                                      # fresh database
    assert not needs_baseline_stamp({"users", "orders", "alembic_version"})     # already migrated
