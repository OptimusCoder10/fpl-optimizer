"""Add the shared fixtures table.

Revision ID: 20261006_0002
Revises: 20261006_0001
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261006_0002"
down_revision: str | None = "20261006_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fixtures",
        sa.Column("season_id", sa.String(length=7), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_code", sa.Integer(), nullable=False),
        sa.Column("gameweek_id", sa.Integer(), nullable=True),
        sa.Column("kickoff_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started", sa.Boolean(), nullable=False),
        sa.Column("finished", sa.Boolean(), nullable=False),
        sa.Column("finished_provisional", sa.Boolean(), nullable=False),
        sa.Column("home_team_id", sa.Integer(), nullable=False),
        sa.Column("away_team_id", sa.Integer(), nullable=False),
        sa.Column("home_team_score", sa.Integer(), nullable=True),
        sa.Column("away_team_score", sa.Integer(), nullable=True),
        sa.Column("home_team_difficulty", sa.Integer(), nullable=False),
        sa.Column("away_team_difficulty", sa.Integer(), nullable=False),
        sa.Column(
            "gameweek_data_checked_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.CheckConstraint(
            "away_team_difficulty BETWEEN 1 AND 5",
            name="ck_fixtures_away_difficulty",
        ),
        sa.CheckConstraint(
            "away_team_score IS NULL OR away_team_score >= 0",
            name="ck_fixtures_away_score_nonnegative",
        ),
        sa.CheckConstraint(
            "home_team_id <> away_team_id", name="ck_fixtures_distinct_teams"
        ),
        sa.CheckConstraint(
            "home_team_difficulty BETWEEN 1 AND 5",
            name="ck_fixtures_home_difficulty",
        ),
        sa.CheckConstraint(
            "home_team_score IS NULL OR home_team_score >= 0",
            name="ck_fixtures_home_score_nonnegative",
        ),
        sa.CheckConstraint("id > 0", name="ck_fixtures_id_positive"),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["season_id", "away_team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["season_id", "gameweek_id"],
            ["gameweeks.season_id", "gameweeks.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["season_id", "home_team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("season_id", "id"),
    )
    op.create_index(
        "ix_fixtures_season_away_team",
        "fixtures",
        ["season_id", "away_team_id"],
        unique=False,
    )
    op.create_index(
        "ix_fixtures_season_gameweek",
        "fixtures",
        ["season_id", "gameweek_id"],
        unique=False,
    )
    op.create_index(
        "ix_fixtures_season_home_team",
        "fixtures",
        ["season_id", "home_team_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fixtures_season_home_team", table_name="fixtures")
    op.drop_index("ix_fixtures_season_gameweek", table_name="fixtures")
    op.drop_index("ix_fixtures_season_away_team", table_name="fixtures")
    op.drop_table("fixtures")
