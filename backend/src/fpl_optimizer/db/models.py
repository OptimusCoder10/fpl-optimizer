"""Relational models for shared FPL data."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from fpl_optimizer.db.base import Base


SEASON_ID_LENGTH = 7
RULES_VERSION_LENGTH = 64


class Season(Base):
    """A season-scoped source and scoring-rules namespace."""

    __tablename__ = "seasons"

    id: Mapped[str] = mapped_column(String(SEASON_ID_LENGTH), primary_key=True)
    rules_version: Mapped[str] = mapped_column(
        String(RULES_VERSION_LENGTH), nullable=False
    )


class Team(Base):
    """An FPL club as identified within one season."""

    __tablename__ = "teams"
    __table_args__ = (
        PrimaryKeyConstraint("season_id", "id"),
        CheckConstraint("id > 0", name="ck_teams_id_positive"),
    )

    season_id: Mapped[str] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False
    )
    id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_code: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    short_name: Mapped[str] = mapped_column(String(10), nullable=False)
    strength: Mapped[int | None] = mapped_column(Integer)
    strength_overall_home: Mapped[int | None] = mapped_column(Integer)
    strength_overall_away: Mapped[int | None] = mapped_column(Integer)
    strength_attack_home: Mapped[int | None] = mapped_column(Integer)
    strength_attack_away: Mapped[int | None] = mapped_column(Integer)
    strength_defence_home: Mapped[int | None] = mapped_column(Integer)
    strength_defence_away: Mapped[int | None] = mapped_column(Integer)


class Player(Base):
    """The latest bootstrap catalog state for one season's player identity."""

    __tablename__ = "players"
    __table_args__ = (
        PrimaryKeyConstraint("season_id", "id"),
        ForeignKeyConstraint(
            ["season_id", "team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("id > 0", name="ck_players_id_positive"),
        CheckConstraint("position BETWEEN 1 AND 4", name="ck_players_position"),
        CheckConstraint("now_cost >= 0", name="ck_players_now_cost_nonnegative"),
        CheckConstraint(
            "chance_of_playing_next_round IS NULL OR "
            "chance_of_playing_next_round BETWEEN 0 AND 100",
            name="ck_players_next_round_chance",
        ),
        CheckConstraint(
            "publication_version > 0", name="ck_players_publication_version_positive"
        ),
        Index("ix_players_season_team", "season_id", "team_id"),
    )

    season_id: Mapped[str] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False
    )
    id: Mapped[int] = mapped_column(Integer, nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    second_name: Mapped[str] = mapped_column(String(100), nullable=False)
    web_name: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_code: Mapped[int] = mapped_column(Integer, nullable=False)
    has_temporary_code: Mapped[bool] = mapped_column(Boolean, nullable=False)
    opta_code: Mapped[str | None] = mapped_column(String(32))
    now_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    can_select: Mapped[bool] = mapped_column(Boolean, nullable=False)
    can_transact: Mapped[bool] = mapped_column(Boolean, nullable=False)
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    chance_of_playing_next_round: Mapped[int | None] = mapped_column(Integer)
    form: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    ep_next: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    selected_by_percent: Mapped[Decimal | None] = mapped_column(Numeric(7, 3))
    ict_index: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    expected_goals: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    expected_assists: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    expected_goal_involvements: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3)
    )
    expected_goals_conceded: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    source_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    publication_version: Mapped[int] = mapped_column(Integer, nullable=False)


class Gameweek(Base):
    """A season event and its official deadline/result state."""

    __tablename__ = "gameweeks"
    __table_args__ = (
        PrimaryKeyConstraint("season_id", "id"),
        CheckConstraint("id > 0", name="ck_gameweeks_id_positive"),
        Index("ix_gameweeks_season_deadline", "season_id", "deadline_time"),
    )

    season_id: Mapped[str] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False
    )
    id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    deadline_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_next: Mapped[bool] = mapped_column(Boolean, nullable=False)
    finished: Mapped[bool] = mapped_column(Boolean, nullable=False)
    data_checked: Mapped[bool] = mapped_column(Boolean, nullable=False)


class Fixture(Base):
    """One season-scoped Premier League match from the fixtures endpoint."""

    __tablename__ = "fixtures"
    __table_args__ = (
        PrimaryKeyConstraint("season_id", "id"),
        ForeignKeyConstraint(
            ["season_id", "gameweek_id"],
            ["gameweeks.season_id", "gameweeks.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["season_id", "home_team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["season_id", "away_team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("id > 0", name="ck_fixtures_id_positive"),
        CheckConstraint(
            "home_team_id <> away_team_id", name="ck_fixtures_distinct_teams"
        ),
        CheckConstraint(
            "home_team_score IS NULL OR home_team_score >= 0",
            name="ck_fixtures_home_score_nonnegative",
        ),
        CheckConstraint(
            "away_team_score IS NULL OR away_team_score >= 0",
            name="ck_fixtures_away_score_nonnegative",
        ),
        CheckConstraint(
            "home_team_difficulty BETWEEN 1 AND 5",
            name="ck_fixtures_home_difficulty",
        ),
        CheckConstraint(
            "away_team_difficulty BETWEEN 1 AND 5",
            name="ck_fixtures_away_difficulty",
        ),
        Index("ix_fixtures_season_gameweek", "season_id", "gameweek_id"),
        Index("ix_fixtures_season_home_team", "season_id", "home_team_id"),
        Index("ix_fixtures_season_away_team", "season_id", "away_team_id"),
    )

    season_id: Mapped[str] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False
    )
    id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_code: Mapped[int] = mapped_column(Integer, nullable=False)
    gameweek_id: Mapped[int | None] = mapped_column(Integer)
    kickoff_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started: Mapped[bool] = mapped_column(Boolean, nullable=False)
    finished: Mapped[bool] = mapped_column(Boolean, nullable=False)
    finished_provisional: Mapped[bool] = mapped_column(Boolean, nullable=False)
    home_team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    away_team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    home_team_score: Mapped[int | None] = mapped_column(Integer)
    away_team_score: Mapped[int | None] = mapped_column(Integer)
    home_team_difficulty: Mapped[int] = mapped_column(Integer, nullable=False)
    away_team_difficulty: Mapped[int] = mapped_column(Integer, nullable=False)
    gameweek_data_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class PlayerFixtureHistory(Base):
    """One player's observed scoring counts in one season fixture."""

    __tablename__ = "player_fixture_history"
    __table_args__ = (
        PrimaryKeyConstraint("season_id", "player_id", "fixture_id"),
        ForeignKeyConstraint(
            ["season_id", "player_id"],
            ["players.season_id", "players.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["season_id", "fixture_id"],
            ["fixtures.season_id", "fixtures.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["season_id", "team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["season_id", "opponent_team_id"],
            ["teams.season_id", "teams.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("player_id > 0", name="ck_history_player_id_positive"),
        CheckConstraint("fixture_id > 0", name="ck_history_fixture_id_positive"),
        CheckConstraint("source_round > 0", name="ck_history_source_round_positive"),
        CheckConstraint("position BETWEEN 1 AND 4", name="ck_history_position"),
        CheckConstraint(
            "team_id <> opponent_team_id", name="ck_history_distinct_teams"
        ),
        CheckConstraint(
            "source_value >= 0", name="ck_history_source_value_nonnegative"
        ),
        CheckConstraint("minutes >= 0", name="ck_history_minutes_nonnegative"),
        CheckConstraint("goals_scored >= 0", name="ck_history_goals_nonnegative"),
        CheckConstraint("assists >= 0", name="ck_history_assists_nonnegative"),
        CheckConstraint(
            "clean_sheets >= 0", name="ck_history_clean_sheets_nonnegative"
        ),
        CheckConstraint(
            "goals_conceded >= 0", name="ck_history_conceded_nonnegative"
        ),
        CheckConstraint("saves >= 0", name="ck_history_saves_nonnegative"),
        CheckConstraint(
            "penalties_saved >= 0", name="ck_history_pen_saved_nonnegative"
        ),
        CheckConstraint(
            "penalties_missed >= 0", name="ck_history_pen_missed_nonnegative"
        ),
        CheckConstraint("yellow_cards >= 0", name="ck_history_yellow_nonnegative"),
        CheckConstraint("red_cards >= 0", name="ck_history_red_nonnegative"),
        CheckConstraint("own_goals >= 0", name="ck_history_own_goals_nonnegative"),
        CheckConstraint("bonus >= 0", name="ck_history_bonus_nonnegative"),
        CheckConstraint(
            "clearances_blocks_interceptions >= 0",
            name="ck_history_cbi_nonnegative",
        ),
        CheckConstraint("tackles >= 0", name="ck_history_tackles_nonnegative"),
        CheckConstraint("recoveries >= 0", name="ck_history_recoveries_nonnegative"),
        CheckConstraint(
            "defensive_contribution >= 0",
            name="ck_history_defensive_contribution_nonnegative",
        ),
        CheckConstraint(
            "starts IS NULL OR starts IN (0, 1)",
            name="ck_history_starts_boolean_count",
        ),
        CheckConstraint(
            "publication_version > 0",
            name="ck_history_publication_version_positive",
        ),
        Index("ix_history_season_fixture", "season_id", "fixture_id"),
    )

    season_id: Mapped[str] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(Integer, nullable=False)
    fixture_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_round: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    opponent_team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    was_home: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_value: Mapped[int] = mapped_column(Integer, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    total_points: Mapped[int] = mapped_column(Integer, nullable=False)
    goals_scored: Mapped[int] = mapped_column(Integer, nullable=False)
    assists: Mapped[int] = mapped_column(Integer, nullable=False)
    clean_sheets: Mapped[int] = mapped_column(Integer, nullable=False)
    goals_conceded: Mapped[int] = mapped_column(Integer, nullable=False)
    saves: Mapped[int] = mapped_column(Integer, nullable=False)
    penalties_saved: Mapped[int] = mapped_column(Integer, nullable=False)
    penalties_missed: Mapped[int] = mapped_column(Integer, nullable=False)
    yellow_cards: Mapped[int] = mapped_column(Integer, nullable=False)
    red_cards: Mapped[int] = mapped_column(Integer, nullable=False)
    own_goals: Mapped[int] = mapped_column(Integer, nullable=False)
    bonus: Mapped[int] = mapped_column(Integer, nullable=False)
    clearances_blocks_interceptions: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    tackles: Mapped[int] = mapped_column(Integer, nullable=False)
    recoveries: Mapped[int] = mapped_column(Integer, nullable=False)
    defensive_contribution: Mapped[int] = mapped_column(Integer, nullable=False)
    bps: Mapped[int] = mapped_column(Integer, nullable=False)
    influence: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    creativity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    threat: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    ict_index: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    expected_goals: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    expected_assists: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    expected_goal_involvements: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3)
    )
    expected_goals_conceded: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3)
    )
    starts: Mapped[int | None] = mapped_column(Integer)
    publication_version: Mapped[int] = mapped_column(Integer, nullable=False)


class CacheMetadata(Base):
    """Publication metadata; the shared key covers bootstrap and fixtures."""

    __tablename__ = "cache_metadata"
    __table_args__ = (
        PrimaryKeyConstraint("season_id", "key"),
        CheckConstraint(
            "publication_version > 0",
            name="ck_cache_metadata_publication_version_positive",
        ),
        CheckConstraint(
            "source_shared_publication_version IS NULL OR "
            "source_shared_publication_version > 0",
            name="ck_cache_metadata_shared_version_positive",
        ),
        CheckConstraint(
            "retry_count IS NULL OR retry_count >= 0",
            name="ck_cache_metadata_retry_count_nonnegative",
        ),
    )

    season_id: Mapped[str] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    publication_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_sweep_id: Mapped[str | None] = mapped_column(String(128))
    rules_version: Mapped[str | None] = mapped_column(String(RULES_VERSION_LENGTH))
    source_shared_publication_version: Mapped[int | None] = mapped_column(Integer)
    identity_dependencies: Mapped[dict | None] = mapped_column(JSON)
    fixture_dependencies: Mapped[list | None] = mapped_column(JSON)
    covered_fixture_ids: Mapped[list | None] = mapped_column(JSON)
    covered_finalized_fixture_ids: Mapped[list | None] = mapped_column(JSON)
    missing_fixture_ids: Mapped[list | None] = mapped_column(JSON)
    invalid_fixture_ids: Mapped[list | None] = mapped_column(JSON)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    retry_count: Mapped[int | None] = mapped_column(Integer)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
