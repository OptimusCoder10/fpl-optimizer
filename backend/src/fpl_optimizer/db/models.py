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
    Numeric,
    PrimaryKeyConstraint,
    String,
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


class CacheMetadata(Base):
    """Publication metadata; the shared key covers bootstrap and fixtures."""

    __tablename__ = "cache_metadata"
    __table_args__ = (
        PrimaryKeyConstraint("season_id", "key"),
        CheckConstraint(
            "publication_version > 0",
            name="ck_cache_metadata_publication_version_positive",
        ),
    )

    season_id: Mapped[str] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    publication_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_success_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
