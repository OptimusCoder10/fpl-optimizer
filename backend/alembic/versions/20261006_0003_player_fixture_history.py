"""Add per-fixture player history and per-player checkpoint fields.

Revision ID: 20261006_0003
Revises: 20261006_0002
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261006_0003"
down_revision: str | None = "20261006_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("cache_metadata", "last_success_at", nullable=True)
    op.alter_column("cache_metadata", "source_observed_at", nullable=True)
    op.add_column(
        "cache_metadata", sa.Column("completed_sweep_id", sa.String(128))
    )
    op.add_column("cache_metadata", sa.Column("rules_version", sa.String(64)))
    op.add_column(
        "cache_metadata",
        sa.Column("source_shared_publication_version", sa.Integer()),
    )
    op.add_column("cache_metadata", sa.Column("identity_dependencies", sa.JSON()))
    op.add_column("cache_metadata", sa.Column("fixture_dependencies", sa.JSON()))
    op.add_column("cache_metadata", sa.Column("covered_fixture_ids", sa.JSON()))
    op.add_column(
        "cache_metadata", sa.Column("covered_finalized_fixture_ids", sa.JSON())
    )
    op.add_column("cache_metadata", sa.Column("missing_fixture_ids", sa.JSON()))
    op.add_column("cache_metadata", sa.Column("invalid_fixture_ids", sa.JSON()))
    op.add_column(
        "cache_metadata", sa.Column("last_attempt_at", sa.DateTime(timezone=True))
    )
    op.add_column("cache_metadata", sa.Column("last_error", sa.Text()))
    op.add_column(
        "cache_metadata", sa.Column("last_error_category", sa.String(64))
    )
    op.add_column("cache_metadata", sa.Column("retry_count", sa.Integer()))
    op.add_column(
        "cache_metadata", sa.Column("next_retry_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_cache_metadata_shared_version_positive",
        "cache_metadata",
        "source_shared_publication_version IS NULL OR "
        "source_shared_publication_version > 0",
    )
    op.create_check_constraint(
        "ck_cache_metadata_retry_count_nonnegative",
        "cache_metadata",
        "retry_count IS NULL OR retry_count >= 0",
    )

    op.create_table(
        "player_fixture_history",
        sa.Column("season_id", sa.String(length=7), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("fixture_id", sa.Integer(), nullable=False),
        sa.Column("source_round", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("opponent_team_id", sa.Integer(), nullable=False),
        sa.Column("was_home", sa.Boolean(), nullable=False),
        sa.Column("source_value", sa.Integer(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("total_points", sa.Integer(), nullable=False),
        sa.Column("goals_scored", sa.Integer(), nullable=False),
        sa.Column("assists", sa.Integer(), nullable=False),
        sa.Column("clean_sheets", sa.Integer(), nullable=False),
        sa.Column("goals_conceded", sa.Integer(), nullable=False),
        sa.Column("saves", sa.Integer(), nullable=False),
        sa.Column("penalties_saved", sa.Integer(), nullable=False),
        sa.Column("penalties_missed", sa.Integer(), nullable=False),
        sa.Column("yellow_cards", sa.Integer(), nullable=False),
        sa.Column("red_cards", sa.Integer(), nullable=False),
        sa.Column("own_goals", sa.Integer(), nullable=False),
        sa.Column("bonus", sa.Integer(), nullable=False),
        sa.Column(
            "clearances_blocks_interceptions", sa.Integer(), nullable=False
        ),
        sa.Column("tackles", sa.Integer(), nullable=False),
        sa.Column("recoveries", sa.Integer(), nullable=False),
        sa.Column("defensive_contribution", sa.Integer(), nullable=False),
        sa.Column("bps", sa.Integer(), nullable=False),
        sa.Column("influence", sa.Numeric(12, 3)),
        sa.Column("creativity", sa.Numeric(12, 3)),
        sa.Column("threat", sa.Numeric(12, 3)),
        sa.Column("ict_index", sa.Numeric(12, 3)),
        sa.Column("expected_goals", sa.Numeric(12, 3)),
        sa.Column("expected_assists", sa.Numeric(12, 3)),
        sa.Column("expected_goal_involvements", sa.Numeric(12, 3)),
        sa.Column("expected_goals_conceded", sa.Numeric(12, 3)),
        sa.Column("starts", sa.Integer()),
        sa.Column("publication_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("player_id > 0", name="ck_history_player_id_positive"),
        sa.CheckConstraint("fixture_id > 0", name="ck_history_fixture_id_positive"),
        sa.CheckConstraint(
            "source_round > 0", name="ck_history_source_round_positive"
        ),
        sa.CheckConstraint("position BETWEEN 1 AND 4", name="ck_history_position"),
        sa.CheckConstraint(
            "team_id <> opponent_team_id", name="ck_history_distinct_teams"
        ),
        sa.CheckConstraint(
            "source_value >= 0", name="ck_history_source_value_nonnegative"
        ),
        sa.CheckConstraint("minutes >= 0", name="ck_history_minutes_nonnegative"),
        sa.CheckConstraint(
            "goals_scored >= 0", name="ck_history_goals_nonnegative"
        ),
        sa.CheckConstraint("assists >= 0", name="ck_history_assists_nonnegative"),
        sa.CheckConstraint(
            "clean_sheets >= 0", name="ck_history_clean_sheets_nonnegative"
        ),
        sa.CheckConstraint(
            "goals_conceded >= 0", name="ck_history_conceded_nonnegative"
        ),
        sa.CheckConstraint("saves >= 0", name="ck_history_saves_nonnegative"),
        sa.CheckConstraint(
            "penalties_saved >= 0", name="ck_history_pen_saved_nonnegative"
        ),
        sa.CheckConstraint(
            "penalties_missed >= 0", name="ck_history_pen_missed_nonnegative"
        ),
        sa.CheckConstraint(
            "yellow_cards >= 0", name="ck_history_yellow_nonnegative"
        ),
        sa.CheckConstraint("red_cards >= 0", name="ck_history_red_nonnegative"),
        sa.CheckConstraint(
            "own_goals >= 0", name="ck_history_own_goals_nonnegative"
        ),
        sa.CheckConstraint("bonus >= 0", name="ck_history_bonus_nonnegative"),
        sa.CheckConstraint(
            "clearances_blocks_interceptions >= 0",
            name="ck_history_cbi_nonnegative",
        ),
        sa.CheckConstraint(
            "tackles >= 0", name="ck_history_tackles_nonnegative"
        ),
        sa.CheckConstraint(
            "recoveries >= 0", name="ck_history_recoveries_nonnegative"
        ),
        sa.CheckConstraint(
            "defensive_contribution >= 0",
            name="ck_history_defensive_contribution_nonnegative",
        ),
        sa.CheckConstraint(
            "starts IS NULL OR starts IN (0, 1)",
            name="ck_history_starts_boolean_count",
        ),
        sa.CheckConstraint(
            "publication_version > 0",
            name="ck_history_publication_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["season_id"], ["seasons.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["season_id", "player_id"],
            ["players.season_id", "players.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["season_id", "fixture_id"],
            ["fixtures.season_id", "fixtures.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["season_id", "team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["season_id", "opponent_team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("season_id", "player_id", "fixture_id"),
    )
    op.create_index(
        "ix_history_season_fixture",
        "player_fixture_history",
        ["season_id", "fixture_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_history_season_fixture", table_name="player_fixture_history"
    )
    op.drop_table("player_fixture_history")

    op.drop_constraint(
        "ck_cache_metadata_retry_count_nonnegative",
        "cache_metadata",
        type_="check",
    )
    op.drop_constraint(
        "ck_cache_metadata_shared_version_positive",
        "cache_metadata",
        type_="check",
    )
    for column in (
        "next_retry_at",
        "retry_count",
        "last_error_category",
        "last_error",
        "last_attempt_at",
        "invalid_fixture_ids",
        "missing_fixture_ids",
        "covered_finalized_fixture_ids",
        "covered_fixture_ids",
        "fixture_dependencies",
        "identity_dependencies",
        "source_shared_publication_version",
        "rules_version",
        "completed_sweep_id",
    ):
        op.drop_column("cache_metadata", column)
    op.alter_column("cache_metadata", "source_observed_at", nullable=False)
    op.alter_column("cache_metadata", "last_success_at", nullable=False)
