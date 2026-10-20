"""Separate GW20–38 archive view; no development admission rules are changed."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fpl_optimizer.schemas.prior_archive import load_pinned_archive
from fpl_optimizer.services.retrospective_archive import (
    ArchiveCounts, ScoredFixture, adapt_validated_archive,
)
from fpl_optimizer.services.retrospective_cohort import (
    BenchmarkFixture, BenchmarkRow, cutoff_for_event, select_cohort,
)

FIRST_TARGET = 20
LAST_TARGET = 38


@dataclass(frozen=True)
class HeldoutView:
    rows: tuple[BenchmarkRow, ...]
    fixtures: tuple[BenchmarkFixture, ...]
    counts: ArchiveCounts
    player_ids: tuple[int, ...]
    scored_by_event: dict[int, tuple[ScoredFixture, ...]]

    def __post_init__(self) -> None:
        if not self.player_ids or any(not 1 <= fixture.event <= LAST_TARGET for fixture in self.fixtures):
            raise ValueError("held-out view requires a player universe and GW1–38 fixtures")
        if any(not 1 <= row.event <= LAST_TARGET for row in self.rows):
            raise ValueError("held-out view requires GW1–38 rows")
        if set(self.scored_by_event) != set(range(FIRST_TARGET, LAST_TARGET + 1)):
            raise ValueError("held-out view requires GW20–38 score projections")
        for event, scores in self.scored_by_event.items():
            cutoff = cutoff_for_event(event, self.fixtures)
            if cutoff.skipped:
                if scores:
                    raise ValueError("skipped held-out event cannot expose scores")
                continue
            assert cutoff.cutoff_time is not None
            if any(score.event >= event or
                   score.kickoff_time + timedelta(hours=3) >= cutoff.cutoff_time
                   for score in scores):
                raise ValueError("held-out score projection crosses its cutoff")

    @classmethod
    def load(cls, directory: Path) -> "HeldoutView":
        """Validate the pinned archive, then expose only cutoff-scoped scores."""
        archive = load_pinned_archive(directory)
        mapped = adapt_validated_archive(archive, through_event=LAST_TARGET)
        scores = {event: heldout_scored_fixtures(archive, target_event=event)
                  for event in range(FIRST_TARGET, LAST_TARGET + 1)}
        return cls(mapped.rows, mapped.fixtures, mapped.counts,
                   tuple(sorted(player.id for player in archive.catalog.bootstrap.elements)),
                   scores)

    def cohort(self, event: int):
        if not FIRST_TARGET <= event <= LAST_TARGET:
            raise ValueError("held-out targets require GW20–38")
        # The shared selector handles target outcomes, while this boundary keeps
        # even anomalously early-kicking future-event rows out of its inputs.
        return select_cohort(event, self.fixtures,
                             (row for row in self.rows if row.event <= event),
                             self.player_ids)


def heldout_scored_fixtures(archive, *, target_event: int) -> tuple[ScoredFixture, ...]:
    """Read score fields only after all event and kickoff admission checks."""
    if not FIRST_TARGET <= target_event <= LAST_TARGET:
        raise ValueError("held-out scores require a GW20–38 target")
    source = archive.catalog.fixtures.root
    schedule = tuple(BenchmarkFixture(f.id, f.event, f.kickoff_time, f.team_h, f.team_a)
                     for f in source if f.event == target_event)
    cutoff = cutoff_for_event(target_event, schedule)
    if cutoff.skipped:
        return ()
    assert cutoff.cutoff_time is not None
    admitted = []
    for fixture in source:
        if fixture.event is None or fixture.event >= target_event:
            continue
        kickoff = fixture.kickoff_time
        if kickoff is None or kickoff + timedelta(hours=3) >= cutoff.cutoff_time:
            continue
        admitted.append(ScoredFixture(
            fixture.id, fixture.event, kickoff, fixture.team_h, fixture.team_a,
            fixture.team_h_score, fixture.team_a_score,
        ))
    return tuple(sorted(admitted, key=lambda f: (f.kickoff_time, f.fixture_id)))
