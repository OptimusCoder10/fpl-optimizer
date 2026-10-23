"""Pure interim derivation of player-specific finalized-fixture candidates.

``provisional`` means only that the last observed club matches the current
catalog club. It does not prove continuous membership: a player who left and
returned to that club after the anchor row could receive a false required
fixture. The rule cannot detect gaps before the last row or for unresolved
players, so it does not satisfy full §05B coverage. No transfer or registration
period is inferred. Any later enforcement must first be assessed in log-only
shadow mode for measured false gaps.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal


@dataclass(frozen=True)
class PublishedHistoryRow:
    fixture_id: int
    kickoff_time: datetime
    club_id: int  # Derived upstream from was_home and the fixture sides.
    minutes: int  # Zero-minute rows are real anchors.


@dataclass(frozen=True)
class SeasonFixture:
    fixture_id: int
    home_team_id: int
    away_team_id: int
    gameweek_id: int | None
    kickoff_time: datetime | None
    finished: bool
    gameweek_data_checked_at: datetime | None
    gameweek_data_checked: bool


@dataclass(frozen=True)
class RequiredFinalizedFixture:
    fixture_id: int
    gameweek_id: int
    first_data_checked_at: datetime


@dataclass(frozen=True)
class FixtureWithinGrace:
    fixture_id: int
    gameweek_id: int
    first_data_checked_at: datetime
    due_at: datetime


@dataclass(frozen=True)
class HistoryRequirements:
    status: Literal["provisional", "unresolved"]
    reason: str
    anchor_fixture_id: int | None
    required_fixtures: tuple[RequiredFinalizedFixture, ...]
    within_grace: tuple[FixtureWithinGrace, ...]


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")


def derive_history_requirements(
    *,
    player_id: int,
    catalog_team_id: int,
    published_rows: tuple[PublishedHistoryRow, ...],
    season_fixtures: tuple[SeasonFixture, ...],
    now: datetime,
) -> HistoryRequirements:
    """Return sorted fixture candidates strictly after the latest row.

    A same-kickoff fixture is after the anchor only when its fixture ID is
    greater. Neither an earlier row nor today's club proves earlier membership.
    Inputs are read without mutation; the caller supplies validated catalog and
    published-row values from one consistent view in a later integration slice.
    """
    if player_id <= 0 or catalog_team_id <= 0:
        raise ValueError("player and catalog club IDs must be positive")
    _require_aware(now, "now")
    for row in published_rows:
        _require_aware(row.kickoff_time, "history row kickoff_time")

    if not published_rows:
        return HistoryRequirements("unresolved", "no_rows", None, (), ())

    anchor = max(published_rows, key=lambda row: (row.kickoff_time, row.fixture_id))
    if catalog_team_id != anchor.club_id:
        return HistoryRequirements("unresolved", "club_change", anchor.fixture_id, (), ())

    anchor_order = (anchor.kickoff_time, anchor.fixture_id)
    required: list[RequiredFinalizedFixture] = []
    within_grace: list[FixtureWithinGrace] = []
    for fixture in season_fixtures:
        if not (
            catalog_team_id in (fixture.home_team_id, fixture.away_team_id)
            and fixture.gameweek_id is not None
            and fixture.finished
            and fixture.gameweek_data_checked
            and fixture.gameweek_data_checked_at is not None
            and fixture.kickoff_time is not None
        ):
            continue
        _require_aware(fixture.kickoff_time, "fixture kickoff_time")
        _require_aware(fixture.gameweek_data_checked_at, "first_data_checked_at")
        if (fixture.kickoff_time, fixture.fixture_id) <= anchor_order:
            continue
        due_at = fixture.gameweek_data_checked_at + timedelta(hours=2)
        if now >= due_at:
            required.append(RequiredFinalizedFixture(
                fixture.fixture_id, fixture.gameweek_id, fixture.gameweek_data_checked_at,
            ))
        else:
            within_grace.append(FixtureWithinGrace(
                fixture.fixture_id, fixture.gameweek_id,
                fixture.gameweek_data_checked_at, due_at,
            ))

    return HistoryRequirements(
        "provisional", "anchor_club_matches_catalog", anchor.fixture_id,
        tuple(sorted(required, key=lambda item: item.fixture_id)),
        tuple(sorted(within_grace, key=lambda item: item.fixture_id)),
    )
