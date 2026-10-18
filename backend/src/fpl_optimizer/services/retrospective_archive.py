"""Pure archive-to-benchmark mapping and a sealed development loader.

The existing pinned loader owns checksum, duplicate and scoring validation.
This adapter reads only the identity, schedule and scoring counts needed by
BenchmarkRow and BenchmarkFixture. It never reads archive forecast, price,
ownership, availability or raw CSV team fields.
"""

from dataclasses import dataclass
from pathlib import Path

from fpl_optimizer.schemas.prior_archive import ValidatedArchive, load_pinned_archive
from fpl_optimizer.services.retrospective_cohort import BenchmarkFixture, BenchmarkRow

DEVELOPMENT_LAST_EVENT = 19


@dataclass(frozen=True)
class ArchiveCounts:
    rows: int
    fixtures: int
    events: int
    dropped_rows: int
    dropped_fixtures: int


@dataclass(frozen=True)
class BenchmarkArchive:
    rows: tuple[BenchmarkRow, ...]
    fixtures: tuple[BenchmarkFixture, ...]
    counts: ArchiveCounts


def adapt_validated_archive(
    archive: ValidatedArchive, *, through_event: int | None = None,
) -> BenchmarkArchive:
    """Map validated rows; optionally construct only events through a ceiling.

    Unassigned fixtures have no BenchmarkFixture representation and are counted
    as dropped. Validated player rows cannot refer to an unassigned fixture.
    """
    if through_event is not None and through_event < 1:
        raise ValueError("through_event must be positive")
    source_fixtures = archive.catalog.fixtures.root
    fixtures = tuple(BenchmarkFixture(
        fixture_id=fixture.id,
        event=fixture.event,
        kickoff_time=fixture.kickoff_time,
        home_team_id=fixture.team_h,
        away_team_id=fixture.team_a,
    ) for fixture in source_fixtures if (
        fixture.event is not None
        and (through_event is None or fixture.event <= through_event)
    ))
    selected_fixtures = {fixture.fixture_id: fixture for fixture in fixtures}
    positions = {player.id: player.element_type
                 for player in archive.catalog.bootstrap.elements}
    rows = tuple(BenchmarkRow(
        player_id=row.element,
        fixture_id=row.fixture,
        event=fixture.event,
        kickoff_time=fixture.kickoff_time,
        was_home=row.was_home,
        position=positions[row.element],
        minutes=row.minutes,
        goals_scored=row.goals_scored,
        assists=row.assists,
        clean_sheets=row.clean_sheets,
        saves=row.saves,
        goals_conceded=row.goals_conceded,
        clearances_blocks_interceptions=row.clearances_blocks_interceptions,
        tackles=row.tackles,
        recoveries=row.recoveries,
        bonus=row.bonus,
        penalties_saved=row.penalties_saved,
        penalties_missed=row.penalties_missed,
        yellow_cards=row.yellow_cards,
        red_cards=row.red_cards,
        own_goals=row.own_goals,
    ) for history in archive.histories.values() for row in history.history
      if (fixture := selected_fixtures.get(row.fixture)) is not None)
    if len(rows) > archive.audit.distinct_keys:
        raise ValueError("benchmark rows exceed validated distinct archive keys")
    return BenchmarkArchive(rows, fixtures, ArchiveCounts(
        rows=len(rows),
        fixtures=len(fixtures),
        events=len({fixture.event for fixture in fixtures}),
        dropped_rows=archive.audit.distinct_keys - len(rows),
        dropped_fixtures=len(source_fixtures) - len(fixtures),
    ))


def load_development_view(directory: Path) -> BenchmarkArchive:
    """Validate the entire checksum-pinned archive before exposing GW1–19 only."""
    archive = load_pinned_archive(directory)
    return adapt_validated_archive(archive, through_event=DEVELOPMENT_LAST_EVENT)
