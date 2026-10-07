"""Keep pinned archive evidence on existing publication checkpoints.

Revision ID: 20261007_0005
Revises: 20261006_0004
"""

from alembic import op
import sqlalchemy as sa

revision = "20261007_0005"
down_revision = "20261006_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cache_metadata", sa.Column("source_provenance", sa.JSON()))


def downgrade() -> None:
    op.drop_column("cache_metadata", "source_provenance")
