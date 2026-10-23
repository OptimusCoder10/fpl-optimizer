"""Pure J46 research-bundle policy, not a §05B coverage approval.

An empty J42 required-fixture list suppresses only its required-fixture
comparison: recorded missing/invalid IDs and shared freshness still apply.
Target-event and later rows are filtered separately by eligible_current_rows.
No J44 shadow candidate is supplied here or used as an exclusion.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Generic, Literal, Protocol, TypeVar

from fpl_optimizer.services.history_eligibility import (
    HISTORY_AGE_LIMIT,
    HistoryEligibilityView,
    PlayerHistoryEvidence,
    decide_history_eligibility,
)


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class GameweekInput:
    id: int
    deadline_time: datetime | None
    data_checked: bool


@dataclass(frozen=True)
class FixtureInput:
    id: int
    gameweek_id: int | None
    kickoff_time: datetime | None
    finished: bool
    finished_provisional: bool


@dataclass(frozen=True)
class TargetEvent:
    id: int
    deadline_time: datetime


class HistoryRow(Protocol):
    fixture_id: int


RowT = TypeVar("RowT", bound=HistoryRow)


@dataclass(frozen=True)
class EligibleCurrentRows(Generic[RowT]):
    rows: tuple[RowT, ...]


@dataclass(frozen=True)
class PlayerHistoryLabel:
    history_age_status: Literal["current", "stale", "unknown"]
    action: Literal["use", "exclude"]
    reason: str
    coverage_status: Literal["unverified_shadow_only"] = "unverified_shadow_only"


def select_target_event(
    gameweeks: tuple[GameweekInput, ...], *, as_of: datetime,
) -> TargetEvent | None:
    """Choose the first published deadline strictly after the capture time."""
    cutoff = _aware(as_of, "as_of")
    future = []
    for gameweek in gameweeks:
        if gameweek.deadline_time is None:
            continue
        deadline = _aware(gameweek.deadline_time, "deadline_time")
        if deadline > cutoff:
            future.append((deadline, gameweek.id))
    if not future:
        return None
    deadline, event_id = min(future)
    return TargetEvent(event_id, deadline)


def eligible_current_rows(
    rows: tuple[RowT, ...],
    fixtures: tuple[FixtureInput, ...],
    gameweeks: tuple[GameweekInput, ...],
    *,
    target_event_id: int,
    as_of: datetime,
) -> EligibleCurrentRows[RowT]:
    """Filter using Fixture.finished/finished_provisional/gameweek_id/kickoff_time
    and Gameweek.data_checked. Fixture assignments are authoritative, including
    a reschedule into the target event. Zero-minute stored rows remain eligible.
    """
    cutoff = _aware(as_of, "as_of")
    if target_event_id < 1:
        raise ValueError("target_event_id must be positive")
    by_fixture = {fixture.id: fixture for fixture in fixtures}
    by_gameweek = {gameweek.id: gameweek for gameweek in gameweeks}
    eligible: list[tuple[datetime, int, RowT]] = []
    for row in rows:
        fixture = by_fixture.get(row.fixture_id)
        if fixture is None or fixture.gameweek_id is None:
            continue
        if fixture.gameweek_id >= target_event_id:
            continue
        gameweek = by_gameweek.get(fixture.gameweek_id)
        if (gameweek is None or not gameweek.data_checked or not fixture.finished
                or fixture.finished_provisional or fixture.kickoff_time is None):
            continue
        kickoff = _aware(fixture.kickoff_time, "kickoff_time")
        if kickoff < cutoff:
            eligible.append((kickoff, fixture.id, row))
    eligible.sort(key=lambda item: (item[0], item[1]))
    return EligibleCurrentRows(tuple(row for _, _, row in eligible))


def label_player_history(
    view: HistoryEligibilityView,
    player: PlayerHistoryEvidence,
    *,
    target_event_id: int,
    as_of: datetime,
) -> PlayerHistoryLabel:
    """Label one research candidate using J42 transfers mode, without J44 gaps.

    An empty required list suppresses only the required-fixture comparison:
    recorded missing/invalid IDs and shared freshness still apply. Function 2
    separately removes target-event and later rows from predictor inputs.
    """
    cutoff = _aware(as_of, "as_of")
    age_status: Literal["current", "stale", "unknown"] = "unknown"
    if player.source_observed_at is not None and player.last_success_at is not None:
        age_status = (
            "stale" if cutoff - _aware(player.source_observed_at, "source_observed_at")
            > HISTORY_AGE_LIMIT else "current"
        )
    research_view = replace(view, players=(replace(player, required_fixtures=()),))
    decision = decide_history_eligibility(
        research_view,
        now=cutoff,
        mode="transfers",
        owned_player_ids=frozenset(),
        locked_player_ids=frozenset(),
        target_event=target_event_id,
    )
    candidate = decision.candidates[0]
    action: Literal["use", "exclude"] = (
        "use" if candidate.action == "use" and age_status == "current" else "exclude"
    )
    reason = decision.shared_reason if decision.blocked else candidate.reason
    return PlayerHistoryLabel(age_status, action, reason)
