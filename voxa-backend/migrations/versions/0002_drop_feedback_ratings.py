"""Drop feedback_ratings, left behind when the unused feedback endpoint was removed.

Databases created before that change still have the table; new ones never had it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feedback_ratings")


def downgrade() -> None:
    # The table is not part of the 0001 schema (fresh databases never had it), so there
    # is nothing to restore; its rows are gone either way.
    pass
