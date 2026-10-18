"""Pure §04D prior selection over caller-eligible scoring rows.

The prior-season reader supplies the validated cross-season identity decision.
This module never attempts to reconstruct that decision from IDs or names.
"""

from collections import Counter
from dataclasses import dataclass, fields
import math
from types import SimpleNamespace
from typing import Iterable, Literal, Mapping, Protocol

from fpl_optimizer.services.scoring_components import (
    FixtureScoringCounts, ScoringComponents, score_fixture,
)


REQUIRED_COUNTS = tuple(FixtureScoringCounts.__annotations__)
PriorSource = Literal[
    "previous_season_personal", "current_season_position",
    "previous_season_position", "personal_current_only", "unavailable",
]


class ScoringRow(Protocol):
    player_id: int
    position: int
    scoring_counts: Mapping[str, int | None]


class ValidatedPriorInputs(Protocol):
    """The identity/compatibility result supplied by read_prior_inputs (slice 1.5)."""

    personal_player_id: int | None
    personal_reason: str | None
    personal_rows: tuple[ScoringRow, ...]
    position_rows: tuple[ScoringRow, ...]
    exclusions: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class PriorExclusion:
    source: str
    reason: str
    count: int


@dataclass(frozen=True)
class PriorSelection:
    source: PriorSource
    row_count: int  # Prior rows; zero when there is no prior.
    component_means: dict[str, float] | None
    exclusions: tuple[PriorExclusion, ...]
    personal_current_count: int
    p_zero_minutes: float | None = None
    p_under_60: float | None = None
    p_60_plus: float | None = None
    expected_minutes: float | None = None


def _score_rows(
    rows: Iterable[ScoringRow], position: int, source: str,
    excluded: Counter[tuple[str, str]],
) -> list[tuple[ScoringComponents, int]]:
    scored = []
    for row in rows:
        if row.position != position:
            excluded[source, "position_mismatch"] += 1
            continue
        missing = next(
            (name for name in REQUIRED_COUNTS
             if name not in row.scoring_counts or row.scoring_counts[name] is None),
            None,
        )
        if missing is not None:
            excluded[source, f"missing_required_scoring_count:{missing}"] += 1
            continue
        counts = {name: row.scoring_counts[name] for name in REQUIRED_COUNTS}
        scored.append((score_fixture(SimpleNamespace(**counts), position), counts["minutes"]))
    return scored


def _means(scored: list[tuple[ScoringComponents, int]]) -> dict[str, float]:
    return {
        field.name: math.fsum(sorted(getattr(points, field.name) for points, _ in scored)) / len(scored)
        for field in fields(ScoringComponents)
    }


def select_prior(
    *, player_id: int, position: int, current_rows: Iterable[ScoringRow],
    previous: ValidatedPriorInputs,
) -> PriorSelection:
    """Choose the first complete prior source; rows are already cutoff/coverage eligible.

    Only ``previous.personal_player_id`` from the slice 1.5 reader authorizes
    a personal cross-season prior. A no-prior result tells the caller whether
    it can use personal current observations without shrinkage.
    """
    if position not in {1, 2, 3, 4}:
        raise ValueError("position must be an FPL position from 1 to 4")
    current = tuple(current_rows)
    excluded: Counter[tuple[str, str]] = Counter()
    for _, reason in previous.exclusions:
        excluded["previous_season_reader", reason] += 1
    if previous.personal_reason is not None and not (
        previous.personal_player_id is not None
        and (previous.personal_player_id, previous.personal_reason) in previous.exclusions
    ):
        excluded["previous_season_personal", previous.personal_reason] += 1

    personal_current = _score_rows(
        (row for row in current if row.player_id == player_id),
        position, "personal_current", excluded,
    )

    def result(source: PriorSource, scored: list[tuple[ScoringComponents, int]]) -> PriorSelection:
        minutes = [value for _, value in scored]
        count = len(minutes)
        return PriorSelection(
            source, count, _means(scored) if scored else None,
            tuple(PriorExclusion(src, reason, count)
                  for (src, reason), count in sorted(excluded.items())),
            len(personal_current),
            p_zero_minutes=sum(value == 0 for value in minutes) / count if count else None,
            p_under_60=sum(0 < value < 60 for value in minutes) / count if count else None,
            p_60_plus=sum(value >= 60 for value in minutes) / count if count else None,
            expected_minutes=math.fsum(sorted(minutes)) / count if count else None,
        )

    if previous.personal_player_id is not None and previous.personal_reason is None:
        personal = (row for row in previous.personal_rows
                    if row.player_id == previous.personal_player_id)
        scored = _score_rows(personal, position, "previous_season_personal", excluded)
        if scored:
            return result("previous_season_personal", scored)

    current_position = (row for row in current
                        if row.player_id != player_id and row.position == position)
    scored = _score_rows(current_position, position, "current_season_position", excluded)
    if scored:
        return result("current_season_position", scored)

    scored = _score_rows(previous.position_rows, position,
                         "previous_season_position", excluded)
    if scored:
        return result("previous_season_position", scored)

    return result("personal_current_only" if personal_current else "unavailable", [])
