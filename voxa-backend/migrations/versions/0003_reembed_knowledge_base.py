"""Clear the knowledge base so the gateway re-seeds it with title + body embeddings.

Articles used to be embedded from their body only. The seed data is the only source of
these rows, and the gateway adds any missing article on start-up, so clearing the table
is enough to rebuild every embedding the new way.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM knowledge_base")


def downgrade() -> None:
    # Nothing to restore: the rows are rebuilt from the seed data on the next start.
    pass
