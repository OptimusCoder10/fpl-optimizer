"""Persist the history work due time on existing player checkpoints.

Revision ID: 20261006_0004
Revises: 20261006_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20261006_0004"
down_revision = "20261006_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cache_metadata", sa.Column("history_due_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("cache_metadata", "history_due_at")
