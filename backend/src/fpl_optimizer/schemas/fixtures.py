"""Pydantic boundary models for FPL's fixtures response and shared snapshot."""

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from fpl_optimizer.schemas.bootstrap import (
    BootstrapBoundaryModel,
    BootstrapEnvelope,
    NullableDatetimeString,
    require_aware_datetime,
)


class FPLFixture(BootstrapBoundaryModel):
    """Every fixture field consumed by the shared catalog."""

    id: int = Field(gt=0)
    code: int = Field(gt=0)
    event: int | None = Field(gt=0)
    kickoff_time: NullableDatetimeString
    started: bool
    finished: bool
    finished_provisional: bool
    team_h: int = Field(gt=0)
    team_a: int = Field(gt=0)
    team_h_score: int | None = Field(ge=0)
    team_a_score: int | None = Field(ge=0)
    team_h_difficulty: int = Field(ge=1, le=5)
    team_a_difficulty: int = Field(ge=1, le=5)

    @model_validator(mode="after")
    def validate_fixture(self) -> "FPLFixture":
        if self.team_h == self.team_a:
            raise ValueError("fixture home and away teams must differ")
        if self.kickoff_time is not None:
            require_aware_datetime(self.kickoff_time)
        return self


class FixturesEnvelope(RootModel[list[FPLFixture]]):
    """The top-level list returned by /api/fixtures/."""

    @model_validator(mode="after")
    def validate_fixture_ids(self) -> "FixturesEnvelope":
        if not self.root:
            raise ValueError("fixtures response must not be empty")
        identifiers = [fixture.id for fixture in self.root]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate fixture id in fixtures response")
        return self


class SharedCatalogEnvelope(BaseModel):
    """A bootstrap and fixtures pair validated before publication begins."""

    model_config = ConfigDict(strict=True)

    bootstrap: BootstrapEnvelope
    fixtures: FixturesEnvelope

    @model_validator(mode="after")
    def validate_cross_endpoint_relationships(self) -> "SharedCatalogEnvelope":
        team_ids = {team.id for team in self.bootstrap.teams}
        event_ids = {event.id for event in self.bootstrap.events}

        missing_team_ids = sorted(
            {
                team_id
                for fixture in self.fixtures.root
                for team_id in (fixture.team_h, fixture.team_a)
                if team_id not in team_ids
            }
        )
        if missing_team_ids:
            raise ValueError(
                "fixtures reference teams missing from bootstrap response: "
                f"{missing_team_ids}"
            )

        represented_team_ids = {
            team_id
            for fixture in self.fixtures.root
            for team_id in (fixture.team_h, fixture.team_a)
        }
        unrepresented_team_ids = sorted(team_ids - represented_team_ids)
        if unrepresented_team_ids:
            raise ValueError(
                "bootstrap teams have no fixture in the fixtures response: "
                f"{unrepresented_team_ids}"
            )

        missing_event_ids = sorted(
            {
                fixture.event
                for fixture in self.fixtures.root
                if fixture.event is not None and fixture.event not in event_ids
            }
        )
        if missing_event_ids:
            raise ValueError(
                "fixtures reference gameweeks missing from bootstrap response: "
                f"{missing_event_ids}"
            )
        return self
