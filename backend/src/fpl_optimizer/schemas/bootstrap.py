"""Pydantic boundary models for FPL's bootstrap-static response."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def _parse_nullable_numeric_string(value: object) -> Decimal | None:
    """Parse FPL decimal strings without silently accepting another source type."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("FPL numeric value must be a string or null")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ValueError("FPL numeric string is not a valid decimal") from error


def require_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value


def _parse_nullable_datetime_string(value: object) -> datetime | None:
    """Parse the ISO-8601 strings supplied by FPL, including its trailing Z."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("FPL timestamp must be a string or null")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("FPL timestamp is not valid ISO-8601") from error


NullableNumericString = Annotated[
    Decimal | None,
    BeforeValidator(_parse_nullable_numeric_string),
]
NullableDatetimeString = Annotated[
    datetime | None,
    BeforeValidator(_parse_nullable_datetime_string),
]
SeasonId = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}$")]


class BootstrapBoundaryModel(BaseModel):
    """Accept unrelated endpoint fields while strictly typing every consumed field."""

    model_config = ConfigDict(extra="ignore", strict=True)


class BootstrapTeam(BootstrapBoundaryModel):
    id: int = Field(gt=0)
    code: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    short_name: str = Field(min_length=1, max_length=10)
    strength: int | None
    strength_overall_home: int | None
    strength_overall_away: int | None
    strength_attack_home: int | None
    strength_attack_away: int | None
    strength_defence_home: int | None
    strength_defence_away: int | None


class BootstrapPlayer(BootstrapBoundaryModel):
    id: int = Field(gt=0)
    first_name: str = Field(max_length=100)
    second_name: str = Field(max_length=100)
    web_name: str = Field(min_length=1, max_length=100)
    element_type: int = Field(ge=1, le=4)
    team: int = Field(gt=0)
    code: int = Field(gt=0)
    has_temporary_code: bool
    opta_code: str | None = Field(max_length=32)
    now_cost: int = Field(ge=0)
    can_select: bool
    can_transact: bool
    removed: bool
    status: str = Field(min_length=1, max_length=8)
    chance_of_playing_next_round: int | None = Field(ge=0, le=100)
    form: NullableNumericString
    ep_next: NullableNumericString
    selected_by_percent: NullableNumericString
    ict_index: NullableNumericString
    expected_goals: NullableNumericString
    expected_assists: NullableNumericString
    expected_goal_involvements: NullableNumericString
    expected_goals_conceded: NullableNumericString


class BootstrapGameweek(BootstrapBoundaryModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    deadline_time: NullableDatetimeString
    is_current: bool
    is_next: bool
    finished: bool
    data_checked: bool

    @model_validator(mode="after")
    def validate_deadline_timezone(self) -> "BootstrapGameweek":
        if self.deadline_time is not None:
            require_aware_datetime(self.deadline_time)
        return self


class BootstrapEnvelope(BootstrapBoundaryModel):
    """The subset of bootstrap-static used by the shared catalog."""

    teams: list[BootstrapTeam]
    elements: list[BootstrapPlayer]
    events: list[BootstrapGameweek]

    @model_validator(mode="after")
    def validate_catalog_relationships(self) -> "BootstrapEnvelope":
        for label, rows in (
            ("team", self.teams),
            ("player", self.elements),
            ("gameweek", self.events),
        ):
            identifiers = [row.id for row in rows]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"duplicate {label} id in bootstrap response")

        team_ids = {team.id for team in self.teams}
        missing_team_ids = sorted(
            {player.team for player in self.elements if player.team not in team_ids}
        )
        if missing_team_ids:
            raise ValueError(
                f"players reference teams missing from bootstrap response: {missing_team_ids}"
            )
        return self


class SharedPublication(BaseModel):
    """Explicit shared-publication context absent from the FPL responses."""

    model_config = ConfigDict(strict=True)

    season_id: SeasonId
    rules_version: str = Field(min_length=1, max_length=64)
    source_observed_at: datetime
    published_at: datetime

    @model_validator(mode="after")
    def validate_timestamp_timezones(self) -> "SharedPublication":
        require_aware_datetime(self.source_observed_at)
        require_aware_datetime(self.published_at)
        if self.published_at < self.source_observed_at:
            raise ValueError("published_at cannot precede source_observed_at")
        return self
