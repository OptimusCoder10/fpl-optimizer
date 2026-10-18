"""Pure §08A per-event baselines and benchmark arithmetic.

Callers supply only 2.5a-eligible earlier rows and accepted target outcomes.
This module never selects cutoffs, reads an archive or runs a benchmark.
"""

from dataclasses import dataclass
import math
from typing import Iterable

from fpl_optimizer.services.prediction_engine import AppearanceProbabilities
from fpl_optimizer.services.retrospective_cohort import BenchmarkRow, PlayerEvent
from fpl_optimizer.services.scoring_components import score_fixture


def _event_mean(player: PlayerEvent, rows: Iterable[BenchmarkRow]) -> float | None:
    """Multiply a per-fixture mean by the same scheduled fixture count for all baselines."""
    if not player.scheduled_fixture_ids:
        return 0.0
    points = [score_fixture(row, row.position).total_points for row in rows]
    if not points:
        return None
    return len(player.scheduled_fixture_ids) * math.fsum(points) / len(points)


def zero_baseline(player: PlayerEvent) -> float:
    return 0.0


def position_mean_baseline(
    player: PlayerEvent, eligible_position_rows: Iterable[BenchmarkRow],
) -> float | None:
    """Earlier same-position rows from *other* players, as in select_prior."""
    return _event_mean(player, (
        row for row in eligible_position_rows
        if row.position == player.position and row.player_id != player.player_id
    ))


def expanding_personal_mean_baseline(player: PlayerEvent) -> float | None:
    """Unweighted mean of every eligible personal fixture, regardless of old position."""
    return _event_mean(player, player.eligible_rows)


def latest_six_personal_mean_baseline(
    player: PlayerEvent, eligible_position_rows: Iterable[BenchmarkRow],
) -> float | None:
    """Newest six personal fixtures; use the other-player position pool if empty."""
    recent = sorted(
        player.eligible_rows,
        key=lambda row: (row.kickoff_time, row.fixture_id),
        reverse=True,
    )[:6]
    if recent:
        return _event_mean(player, recent)
    return position_mean_baseline(player, eligible_position_rows)


@dataclass(frozen=True)
class PointMetricRecord:
    player_id: int
    event: int
    position: int
    active: bool
    prediction: float | None
    outcome: float | None
    outcome_exclusion_reason: str | None = None


@dataclass(frozen=True)
class PointMetrics:
    eligible: int
    predicted: int
    missing: int
    coverage: float | None  # predicted / eligible; undefined for an empty cohort
    mae: float | None
    bias: float | None  # prediction minus outcome; positive means overprediction
    rmse: float | None


@dataclass(frozen=True)
class PointMetricSummary:
    overall: PointMetrics
    by_position: dict[int, PointMetrics]
    active_overall: PointMetrics
    active_by_position: dict[int, PointMetrics]


def _accepted(records: Iterable[PointMetricRecord]) -> tuple[PointMetricRecord, ...]:
    return tuple(record for record in records if (
        record.outcome_exclusion_reason is None and record.outcome is not None
    ))


def _point_metrics(records: Iterable[PointMetricRecord]) -> PointMetrics:
    rows = tuple(records)
    errors = [record.prediction - record.outcome for record in rows
              if record.prediction is not None and record.outcome is not None]
    count = len(errors)
    return PointMetrics(
        eligible=len(rows), predicted=count, missing=len(rows) - count,
        coverage=count / len(rows) if rows else None,
        mae=math.fsum(abs(error) for error in errors) / count if count else None,
        bias=math.fsum(errors) / count if count else None,
        rmse=math.sqrt(math.fsum(error * error for error in errors) / count)
        if count else None,
    )


def summarize_points(records: Iterable[PointMetricRecord]) -> PointMetricSummary:
    """Score accepted player-events, including doubles and blanks.

    Bias is mean(prediction - outcome): positive means overprediction and
    negative means underprediction.
    """
    accepted = _accepted(records)
    positions = sorted({record.position for record in accepted})
    active = tuple(record for record in accepted if record.active)
    return PointMetricSummary(
        overall=_point_metrics(accepted),
        by_position={position: _point_metrics(
            record for record in accepted if record.position == position
        ) for position in positions},
        active_overall=_point_metrics(active),
        active_by_position={position: _point_metrics(
            record for record in active if record.position == position
        ) for position in positions},
    )


@dataclass(frozen=True)
class PlayedFixtureMinutes:
    fixture_id: int
    minutes: int
    probabilities: AppearanceProbabilities | None


@dataclass(frozen=True)
class ModelMinuteRecord:
    outcome_exclusion_reason: str | None
    fixtures: tuple[PlayedFixtureMinutes, ...]
    active: bool = False


@dataclass(frozen=True)
class ModelBrierMetrics:
    played_fixtures: int
    predicted_fixtures: int
    missing_probabilities: int
    binary_appearance_brier: float | None
    three_category_minute_brier: float | None


@dataclass(frozen=True)
class ModelMinuteSummary:
    overall: ModelBrierMetrics
    active: ModelBrierMetrics


def _model_brier(records: Iterable[ModelMinuteRecord]) -> ModelBrierMetrics:
    played = predicted = 0
    binary_total = category_total = 0.0
    for record in records:
        if record.outcome_exclusion_reason is not None:
            continue
        for fixture in record.fixtures:
            played += 1
            probabilities = fixture.probabilities
            if probabilities is None:
                continue
            predicted += 1
            p_zero = probabilities.zero_minutes
            p_under = probabilities.under_60
            p_sixty = probabilities.sixty_plus
            if any(not math.isfinite(p) or not 0 <= p <= 1
                   for p in (p_zero, p_under, p_sixty)) or not math.isclose(
                p_zero + p_under + p_sixty, 1.0, abs_tol=1e-9
            ):
                raise ValueError("minute probabilities must sum to one and lie in [0, 1]")
            observed = (int(fixture.minutes == 0),
                        int(0 < fixture.minutes < 60), int(fixture.minutes >= 60))
            binary_total += ((p_under + p_sixty) - int(fixture.minutes > 0)) ** 2
            category_total += sum(
                (probability - indicator) ** 2
                for probability, indicator in zip(
                    (p_zero, p_under, p_sixty), observed, strict=True
                )
            ) / 2
    return ModelBrierMetrics(
        played, predicted, played - predicted,
        binary_total / predicted if predicted else None,
        category_total / predicted if predicted else None,
    )


def summarize_model_minutes(records: Iterable[ModelMinuteRecord]) -> ModelMinuteSummary:
    """Fixture-level model Brier scores; baseline point values are rejected."""
    checked = tuple(records)
    for record in checked:
        if not isinstance(record, ModelMinuteRecord):
            raise TypeError("Brier records must be ModelMinuteRecord values")
        for fixture in record.fixtures:
            if not isinstance(fixture, PlayedFixtureMinutes):
                raise TypeError("Brier fixtures must be PlayedFixtureMinutes values")
            if fixture.probabilities is not None and not isinstance(
                fixture.probabilities, AppearanceProbabilities
            ):
                raise TypeError("Brier probabilities must be model AppearanceProbabilities")
    accepted = tuple(record for record in checked
                     if record.outcome_exclusion_reason is None)
    return ModelMinuteSummary(
        overall=_model_brier(accepted),
        active=_model_brier(record for record in accepted if record.active),
    )


def gameweek_mae(records: Iterable[PointMetricRecord]) -> dict[int, float]:
    """MAE per event over accepted player-events with a prediction."""
    by_event: dict[int, list[float]] = {}
    for record in _accepted(records):
        if record.prediction is not None:
            assert record.outcome is not None
            by_event.setdefault(record.event, []).append(abs(record.prediction - record.outcome))
    return {event: math.fsum(errors) / len(errors)
            for event, errors in sorted(by_event.items())}


@dataclass(frozen=True)
class PairedGameweekDifference:
    per_gameweek: dict[int, float]  # candidate MAE minus reference MAE
    mean_difference: float | None
    standard_error: float | None  # sample SD (n-1) / sqrt(n); unavailable for n < 2
    gameweek_count: int


def paired_gameweek_mae_difference(
    candidate: Iterable[PointMetricRecord], reference: Iterable[PointMetricRecord],
) -> PairedGameweekDifference:
    """Pair by player-event, then calculate candidate-minus-reference weekly MAE."""
    def index(records: Iterable[PointMetricRecord]) -> dict[tuple[int, int], PointMetricRecord]:
        result = {}
        for record in records:
            key = (record.event, record.player_id)
            if key in result:
                raise ValueError("duplicate player-event metric record")
            result[key] = record
        return result

    candidate_by_key = index(candidate)
    reference_by_key = index(reference)
    weekly: dict[int, list[tuple[float, float]]] = {}
    for key in candidate_by_key.keys() & reference_by_key.keys():
        row, other = candidate_by_key[key], reference_by_key[key]
        if row.outcome_exclusion_reason is not None or other.outcome_exclusion_reason is not None:
            continue
        if row.outcome is None or other.outcome is None:
            continue
        if row.outcome != other.outcome:
            raise ValueError("paired player-events must have the same outcome")
        if row.prediction is None or other.prediction is None:
            continue
        weekly.setdefault(row.event, []).append((
            abs(row.prediction - row.outcome), abs(other.prediction - other.outcome)
        ))
    differences = {
        event: (math.fsum(pair[0] for pair in errors)
                - math.fsum(pair[1] for pair in errors)) / len(errors)
        for event, errors in sorted(weekly.items())
    }
    values = tuple(differences.values())
    n = len(values)
    mean = math.fsum(values) / n if n else None
    standard_error = (
        math.sqrt(math.fsum((value - mean) ** 2 for value in values) / (n - 1))
        / math.sqrt(n) if n >= 2 and mean is not None else None
    )
    return PairedGameweekDifference(differences, mean, standard_error, n)
