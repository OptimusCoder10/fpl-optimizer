"""Create the bootstrap catalog tables.

Revision ID: 20261006_0001
Revises:
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261006_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seasons",
        sa.Column("id", sa.String(length=7), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "cache_metadata",
        sa.Column("season_id", sa.String(length=7), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("publication_version", sa.Integer(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "publication_version > 0",
            name="ck_cache_metadata_publication_version_positive",
        ),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("season_id", "key"),
    )
    op.create_table(
        "gameweeks",
        sa.Column("season_id", sa.String(length=7), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("deadline_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("is_next", sa.Boolean(), nullable=False),
        sa.Column("finished", sa.Boolean(), nullable=False),
        sa.Column("data_checked", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id > 0", name="ck_gameweeks_id_positive"),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("season_id", "id"),
    )
    op.create_index(
        "ix_gameweeks_season_deadline",
        "gameweeks",
        ["season_id", "deadline_time"],
        unique=False,
    )
    op.create_table(
        "teams",
        sa.Column("season_id", sa.String(length=7), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_code", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("short_name", sa.String(length=10), nullable=False),
        sa.Column("strength", sa.Integer(), nullable=True),
        sa.Column("strength_overall_home", sa.Integer(), nullable=True),
        sa.Column("strength_overall_away", sa.Integer(), nullable=True),
        sa.Column("strength_attack_home", sa.Integer(), nullable=True),
        sa.Column("strength_attack_away", sa.Integer(), nullable=True),
        sa.Column("strength_defence_home", sa.Integer(), nullable=True),
        sa.Column("strength_defence_away", sa.Integer(), nullable=True),
        sa.CheckConstraint("id > 0", name="ck_teams_id_positive"),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("season_id", "id"),
    )
    op.create_table(
        "players",
        sa.Column("season_id", sa.String(length=7), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("second_name", sa.String(length=100), nullable=False),
        sa.Column("web_name", sa.String(length=100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("external_code", sa.Integer(), nullable=False),
        sa.Column("has_temporary_code", sa.Boolean(), nullable=False),
        sa.Column("opta_code", sa.String(length=32), nullable=True),
        sa.Column("now_cost", sa.Integer(), nullable=False),
        sa.Column("can_select", sa.Boolean(), nullable=False),
        sa.Column("can_transact", sa.Boolean(), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("chance_of_playing_next_round", sa.Integer(), nullable=True),
        sa.Column("form", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("ep_next", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column(
            "selected_by_percent", sa.Numeric(precision=7, scale=3), nullable=True
        ),
        sa.Column("ict_index", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("expected_goals", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("expected_assists", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column(
            "expected_goal_involvements",
            sa.Numeric(precision=12, scale=3),
            nullable=True,
        ),
        sa.Column(
            "expected_goals_conceded",
            sa.Numeric(precision=12, scale=3),
            nullable=True,
        ),
        sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publication_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("id > 0", name="ck_players_id_positive"),
        sa.CheckConstraint("now_cost >= 0", name="ck_players_now_cost_nonnegative"),
        sa.CheckConstraint("position BETWEEN 1 AND 4", name="ck_players_position"),
        sa.CheckConstraint(
            "chance_of_playing_next_round IS NULL OR "
            "chance_of_playing_next_round BETWEEN 0 AND 100",
            name="ck_players_next_round_chance",
        ),
        sa.CheckConstraint(
            "publication_version > 0",
            name="ck_players_publication_version_positive",
        ),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["season_id", "team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("season_id", "id"),
    )
    op.create_index(
        "ix_players_season_team",
        "players",
        ["season_id", "team_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_players_season_team", table_name="players")
    op.drop_table("players")
    op.drop_table("teams")
    op.drop_index("ix_gameweeks_season_deadline", table_name="gameweeks")
    op.drop_table("gameweeks")
    op.drop_table("cache_metadata")
    op.drop_table("seasons")
