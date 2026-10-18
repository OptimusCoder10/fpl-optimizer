"""Pure §08A cutoff, input cohort and outcome-quality selection.

The caller supplies a validated fixture archive, player IDs and scoring rows.
This module does not fetch data, predict points or calculate benchmark metrics.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from fpl_optimizer.services.scoring_components import score_fixture


@dataclass(frozen=True)
class BenchmarkFixture:
    fixture_id: int
    event: int
    kickoff_time: datetime | None
    home_team_id: int
    away_team_id: int


@dataclass(frozen=True)
class BenchmarkRow:
    """Only identity, timing and the counts needed to reconstruct actual points."""

    player_id: int
    fixture_id: int
    event: int
    kickoff_time: datetime | None
    was_home: bool
    position: int
    minutes: int
    goals_scored: int = 0
    assists: int = 0
    clean_sheets: int = 0
    saves: int = 0
    goals_conceded: int = 0
    clearances_blocks_interceptions: int = 0
    tackles: int = 0
    recoveries: int = 0
    bonus: int = 0
    penalties_saved: int = 0
    penalties_missed: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    own_goals: int = 0


@dataclass(frozen=True)
class EventCutoff:
    event: int
    cutoff_time: datetime | None
    missing_kickoff_fixture_ids: tuple[int, ...]
    skip_reason: str | None

    @property
    def skipped(self) -> bool:
        return self.skip_reason is not None


@dataclass(frozen=True)
class PlayerEvent:
    player_id: int
    eligible_rows: tuple[BenchmarkRow, ...]
    position: int
    last_eligible_club_id: int
    active: bool
    scheduled_fixture_ids: tuple[int, ...]
    result_fixture_ids: tuple[int, ...]
    outcome_points: int | None
    outcome_exclusion_reason: str | None


@dataclass(frozen=True)
class CohortSelection:
    cutoff: EventCutoff
    players: tuple[PlayerEvent, ...]
    first_time_player_ids: tuple[int, ...]

    @property
    def first_time_count(self) -> int:
        return len(self.first_time_player_ids)

    @property
    def candidate_count(self) -> int:
        return len(self.players)

    @property
    def active_count(self) -> int:
        return sum(player.active for player in self.players)

    @property
    def accepted_outcome_count(self) -> int:
        return sum(player.outcome_exclusion_reason is None for player in self.players)

    @property
    def outcome_exclusion_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for player in self.players:
            if player.outcome_exclusion_reason is not None:
                reason = player.outcome_exclusion_reason
                counts[reason] = counts.get(reason, 0) + 1
        return counts


def cutoff_for_event(event: int, fixtures: Iterable[BenchmarkFixture]) -> EventCutoff:
    """Refuse a partial schedule because an unknown kickoff could be earliest."""
    target = tuple(fixture for fixture in fixtures if fixture.event == event)
    missing = tuple(sorted(fixture.fixture_id for fixture in target if fixture.kickoff_time is None))
    known = [fixture.kickoff_time for fixture in target if fixture.kickoff_time is not None]
    if not known:
        return EventCutoff(event, None, missing, "no_target_kickoff")
    if missing:
        return EventCutoff(event, None, missing, "partial_target_kickoff")
    return EventCutoff(event, min(known) - timedelta(minutes=90), (), None)


def select_cohort(
    event: int,
    fixtures: Iterable[BenchmarkFixture],
    rows: Iterable[BenchmarkRow],
    player_ids: Iterable[int],
) -> CohortSelection:
    """Select pre-cutoff inputs, then assess each candidate's target outcome.

    ``player_ids`` is an explicit universe independent of history rows. A
    target result row can affect outcome quality, but never input eligibility.
    """
    fixture_list = tuple(fixtures)
    cutoff = cutoff_for_event(event, fixture_list)
    if cutoff.skipped:
        return CohortSelection(cutoff, (), ())
    assert cutoff.cutoff_time is not None
    fixture_by_id = {fixture.fixture_id: fixture for fixture in fixture_list}
    rows_by_player: dict[int, list[BenchmarkRow]] = {}
    for row in rows:
        rows_by_player.setdefault(row.player_id, []).append(row)
    selected: list[PlayerEvent] = []
    first_time: list[int] = []
    for player_id in sorted(set(player_ids)):
        player_rows = rows_by_player.get(player_id, ())
        eligible = tuple(sorted((row for row in player_rows if (
            row.event != event
            and row.kickoff_time is not None
            and row.kickoff_time + timedelta(hours=3) < cutoff.cutoff_time
        )), key=lambda row: (row.kickoff_time, row.fixture_id)))
        if not eligible:
            first_time.append(player_id)
            continue
        latest = eligible[-1]
        fixture = fixture_by_id.get(latest.fixture_id)
        if fixture is None:
            raise ValueError(f"eligible row references unknown fixture {latest.fixture_id}")
        club_id = fixture.home_team_id if latest.was_home else fixture.away_team_id
        scheduled = tuple(sorted(fixture.fixture_id for fixture in fixture_list if (
            fixture.event == event
            and club_id in {fixture.home_team_id, fixture.away_team_id}
        )))
        results = tuple(row for row in player_rows if row.event == event)
        result_ids = tuple(sorted(row.fixture_id for row in results))
        if result_ids == scheduled:
            exclusion = None
            points = sum(score_fixture(row, row.position).total_points for row in results)
        elif set(result_ids) < set(scheduled):
            exclusion = "missing_result_row"
            points = None
        else:
            exclusion = "different_fixture_set"
            points = None
        selected.append(PlayerEvent(
            player_id=player_id,
            eligible_rows=eligible,
            position=latest.position,
            last_eligible_club_id=club_id,
            active=sum(row.minutes for row in eligible[-4:]) >= 60,
            scheduled_fixture_ids=scheduled,
            result_fixture_ids=result_ids,
            outcome_points=points,
            outcome_exclusion_reason=exclusion,
        ))
    return CohortSelection(cutoff, tuple(selected), tuple(first_time))
