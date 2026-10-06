"""Pydantic boundary models for one FPL element-summary response."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fpl_optimizer.schemas.bootstrap import (
    BootstrapBoundaryModel,
    NullableDatetimeString,
    NullableNumericString,
    SeasonId,
    require_aware_datetime,
)


class ElementFixtureHistory(BootstrapBoundaryModel):
    """Every element-summary history field used by storage or scoring."""

    element: int = Field(gt=0)
    fixture: int = Field(gt=0)
    opponent_team: int = Field(gt=0)
    total_points: int
    was_home: bool
    kickoff_time: NullableDatetimeString
    round: int = Field(gt=0)
    minutes: int = Field(ge=0)
    goals_scored: int = Field(ge=0)
    assists: int = Field(ge=0)
    clean_sheets: int = Field(ge=0)
    goals_conceded: int = Field(ge=0)
    own_goals: int = Field(ge=0)
    penalties_saved: int = Field(ge=0)
    penalties_missed: int = Field(ge=0)
    yellow_cards: int = Field(ge=0)
    red_cards: int = Field(ge=0)
    saves: int = Field(ge=0)
    bonus: int = Field(ge=0)
    bps: int
    influence: NullableNumericString
    creativity: NullableNumericString
    threat: NullableNumericString
    ict_index: NullableNumericString
    clearances_blocks_interceptions: int = Field(ge=0)
    recoveries: int = Field(ge=0)
    tackles: int = Field(ge=0)
    defensive_contribution: int = Field(ge=0)
    starts: int | None = Field(ge=0, le=1)
    expected_goals: NullableNumericString
    expected_assists: NullableNumericString
    expected_goal_involvements: NullableNumericString
    expected_goals_conceded: NullableNumericString
    value: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_kickoff_timezone(self) -> "ElementFixtureHistory":
        if self.kickoff_time is not None:
            require_aware_datetime(self.kickoff_time)
        return self


class ElementSummaryEnvelope(BootstrapBoundaryModel):
    """The endpoint envelope; only current-season history is consumed here."""

    fixtures: list[dict[str, object]]
    history: list[ElementFixtureHistory]
    history_past: list[dict[str, object]]

    @model_validator(mode="after")
    def validate_history_fixture_ids(self) -> "ElementSummaryEnvelope":
        fixture_ids = [row.fixture for row in self.history]
        if len(fixture_ids) != len(set(fixture_ids)):
            raise ValueError("duplicate fixture id in element-summary history")
        return self


class PlayerHistoryPublication(BaseModel):
    """Explicit per-player publication context absent from element-summary."""

    model_config = ConfigDict(strict=True)

    season_id: SeasonId
    player_id: int = Field(gt=0)
    completed_sweep_id: str = Field(min_length=1, max_length=128)
    source_observed_at: datetime
    published_at: datetime

    @model_validator(mode="after")
    def validate_timestamp_timezones(self) -> "PlayerHistoryPublication":
        require_aware_datetime(self.source_observed_at)
        require_aware_datetime(self.published_at)
        if self.published_at < self.source_observed_at:
            raise ValueError("published_at cannot precede source_observed_at")
        return self
