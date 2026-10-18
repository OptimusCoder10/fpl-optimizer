"""Pure observation-window, prior-mixture and event prediction arithmetic from §04D."""

from dataclasses import dataclass, fields
from datetime import datetime
import math
from typing import Generic, Iterable, Mapping, TypeVar

from fpl_optimizer.services.prior_selection import PriorSelection
from fpl_optimizer.services.scoring_components import (
    FixtureScoringCounts, ScoringComponents, score_fixture,
)


T = TypeVar("T")
WINDOW_SIZE = 12
MODEL_VERSION = "empirical-fixture-v1"


@dataclass(frozen=True)
class FixtureObservation(Generic[T]):
    """An already eligible player-fixture outcome, before the caller's cutoff."""

    fixture_id: int
    kickoff_time: datetime | None
    value: T


@dataclass(frozen=True)
class SeriesEstimate:
    """A mean may be unavailable; zero is an available, numeric mean."""

    mean: float | None
    raw_count: int
    effective_sample_size: float

    @property
    def available(self) -> bool:
        return self.mean is not None


def _recent_window(
    rows: Iterable[FixtureObservation[T]], window_size: int,
) -> tuple[FixtureObservation[T], ...]:
    if isinstance(window_size, bool) or not isinstance(window_size, int) or window_size <= 0:
        raise ValueError("window_size must be a positive integer")
    observations = tuple(rows)
    if any(row.kickoff_time is None for row in observations):
        raise ValueError("every fixture observation requires a kickoff_time")
    return tuple(sorted(
        observations,
        key=lambda row: (row.kickoff_time, row.fixture_id),
        reverse=True,
    )[:window_size])


def estimate_series(
    rows: Iterable[FixtureObservation[int | float]],
    *,
    prior_mean: int | float | None = None,
    half_life: float = 6,
    prior_strength: float = 3,
    window_size: int = WINDOW_SIZE,
) -> SeriesEstimate:
    """Weight the newest ``window_size`` outcomes and optionally shrink toward a prior.

    Eligibility, observed threshold scoring and prior selection belong to callers.
    The numeric values can also be 0/1 indicators or observed minutes.
    """
    if not math.isfinite(half_life) or half_life <= 0:
        raise ValueError("half_life must be finite and positive")
    if not math.isfinite(prior_strength) or prior_strength < 0:
        raise ValueError("prior_strength must be finite and nonnegative")
    if prior_mean is not None and not math.isfinite(prior_mean):
        raise ValueError("prior_mean must be finite")

    recent = _recent_window(rows, window_size)
    if any(not math.isfinite(row.value) for row in recent):
        raise ValueError("observation values must be finite")

    weights = [2 ** (-rank / half_life) for rank in range(len(recent))]
    weight_sum = math.fsum(weights)
    effective_sample_size = (
        weight_sum**2 / math.fsum(weight**2 for weight in weights)
        if weights else 0.0
    )
    if not recent and (prior_mean is None or prior_strength == 0):
        return SeriesEstimate(None, 0, 0.0)

    weighted_total = math.fsum(
        weight * row.value for weight, row in zip(weights, recent, strict=True)
    )
    if prior_mean is None:
        mean = weighted_total / weight_sum
    else:
        mean = (weighted_total + prior_strength * prior_mean) / (
            weight_sum + prior_strength
        )
    return SeriesEstimate(mean, len(recent), effective_sample_size)


def estimate_components(
    rows: Iterable[FixtureObservation[ScoringComponents]],
    *,
    prior_means: Mapping[str, int | float] | None = None,
    half_life: float = 6,
    prior_strength: float = 3,
    window_size: int = WINDOW_SIZE,
) -> dict[str, SeriesEstimate]:
    """Apply the same window and mixture to each already scored point component."""
    observations = tuple(rows)
    component_names = tuple(field.name for field in fields(ScoringComponents))
    if prior_means is not None and set(prior_means) != set(component_names):
        raise ValueError("prior_means must contain exactly the scoring components")
    return {
        name: estimate_series(
            (FixtureObservation(row.fixture_id, row.kickoff_time, getattr(row.value, name))
             for row in observations),
            prior_mean=None if prior_means is None else prior_means[name],
            half_life=half_life,
            prior_strength=prior_strength,
            window_size=window_size,
        )
        for name in component_names
    }


@dataclass(frozen=True)
class AppearanceProbabilities:
    """Marginal probabilities for one fixture, not an event-level joint model."""

    zero_minutes: float
    under_60: float
    sixty_plus: float


@dataclass(frozen=True)
class FixturePrediction:
    fixture_id: int
    mean_points: float
    components: dict[str, float]
    appearance_probabilities: AppearanceProbabilities
    expected_minutes: float


@dataclass(frozen=True)
class Prediction:
    status: str  # available, blank, or unavailable
    reason: str | None
    per_fixture: tuple[FixturePrediction, ...]
    event_mean: float | None
    event_components: dict[str, float] | None
    appearance_probabilities: AppearanceProbabilities | None  # per-fixture marginal
    expected_minutes_per_fixture: float | None
    expected_minutes_event: float | None
    raw_sample_count: int
    effective_sample_size: float
    prior_source: str
    prior_row_count: int
    availability_factor: float | None
    availability_assumption: bool
    model_version: str
    rules_version: str
    input_version: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class AvailabilityResolution:
    factor: float | None
    assumption: bool
    reason: str | None


def resolve_availability(
    *, chance: int | float | None, status: str | None, owned: bool,
    assumed_factor: float | None = None,
) -> AvailabilityResolution:
    """Apply the §06C signal policy; a user assumption overrides unresolved signals."""
    if chance is not None and (
        isinstance(chance, bool) or not isinstance(chance, (int, float))
        or not math.isfinite(chance) or not 0 <= chance <= 100
    ):
        raise ValueError("chance must be a finite number from 0 to 100")
    if assumed_factor is not None:
        if not owned:
            raise ValueError("availability assumption requires an owned player")
        if (isinstance(assumed_factor, bool) or not isinstance(assumed_factor, (int, float))
                or not math.isfinite(assumed_factor) or not 0 <= assumed_factor <= 1):
            raise ValueError("assumed_factor must be a finite number from 0 to 1")
        return AvailabilityResolution(float(assumed_factor), True, None)
    if status not in {"a", "d", "i", "s", "u", "n"}:
        return AvailabilityResolution(None, False, "unrecognized_status")
    if chance is None:
        if status == "a":
            return AvailabilityResolution(1.0, False, None)
        return AvailabilityResolution(None, False, "missing_chance_for_status")
    if status in {"i", "s", "u"} and chance == 100:
        return AvailabilityResolution(None, False, "contradictory_chance_and_status")
    return AvailabilityResolution(float(chance) / 100, False, None)


def predict(
    *, personal_rows: Iterable[FixtureObservation[FixtureScoringCounts]],
    prior: PriorSelection, position: int, target_fixture_ids: Iterable[int],
    catalog_trusted: bool, chance: int | float | None, status: str | None,
    owned: bool, rules_version: str, input_version: str,
    assumed_factor: float | None = None, half_life: float = 6,
    prior_strength: float = 3, window_size: int = WINDOW_SIZE,
) -> Prediction:
    """Predict one player's target event from already eligible fixture outcomes.

    The caller applies per-player age/coverage and cutoff gates before this call.
    Target fixtures are a trusted captured catalog, not inferred from history.
    """
    if position not in {1, 2, 3, 4}:
        raise ValueError("position must be an FPL position from 1 to 4")
    if not rules_version or not input_version:
        raise ValueError("rules_version and input_version are required")
    fixture_ids = tuple(sorted(target_fixture_ids))
    if any(not isinstance(fid, int) or isinstance(fid, bool) or fid <= 0 for fid in fixture_ids):
        raise ValueError("target fixture IDs must be positive integers")
    if len(set(fixture_ids)) != len(fixture_ids):
        raise ValueError("target fixture IDs must be distinct")

    availability = resolve_availability(
        chance=chance, status=status, owned=owned, assumed_factor=assumed_factor,
    )
    rows = tuple(personal_rows)
    minutes_rows = tuple(FixtureObservation(row.fixture_id, row.kickoff_time, row.value.minutes)
                         for row in rows)
    # Use the selected window to report evidence even on unavailable results.
    evidence = estimate_series(minutes_rows, half_life=half_life,
                               prior_strength=prior_strength, window_size=window_size)
    warning_list = []
    if prior.exclusions:
        warning_list.append("prior_rows_excluded")
    if availability.assumption:
        warning_list.append("availability_assumption")
    if availability.reason:
        warning_list.append("availability_unresolved")
    warnings = tuple(warning_list)

    def result(
        state: str, reason: str | None, *, fixtures: tuple[FixturePrediction, ...] = (),
        event_mean: float | None = None, event_components: dict[str, float] | None = None,
        appearance: AppearanceProbabilities | None = None,
        expected_minutes_per_fixture: float | None = None,
        expected_minutes_event: float | None = None,
    ) -> Prediction:
        return Prediction(
            state, reason, fixtures, event_mean, event_components, appearance,
            expected_minutes_per_fixture, expected_minutes_event,
            evidence.raw_count, evidence.effective_sample_size,
            prior.source, prior.row_count, availability.factor, availability.assumption,
            MODEL_VERSION, rules_version, input_version, warnings,
        )

    if not catalog_trusted:
        return result("unavailable", "untrusted_fixture_catalog")
    if not fixture_ids:
        zeros = {field.name: 0.0 for field in fields(ScoringComponents)}
        return result("blank", None, event_mean=0.0, event_components=zeros,
                      expected_minutes_event=0.0)
    if availability.factor is None:
        return result("unavailable", availability.reason)

    scored_rows = tuple(FixtureObservation(row.fixture_id, row.kickoff_time,
                                           score_fixture(row.value, position)) for row in rows)
    components = estimate_components(scored_rows, prior_means=prior.component_means,
                                     half_life=half_life, prior_strength=prior_strength,
                                     window_size=window_size)
    if not all(estimate.available for estimate in components.values()):
        return result("unavailable", "no_prior_or_personal_observations")

    minute_priors = (
        prior.p_zero_minutes, prior.p_under_60, prior.p_60_plus, prior.expected_minutes
    )
    if prior.component_means is not None and any(value is None for value in minute_priors):
        return result("unavailable", "missing_prior_minute_means")
    minutes = tuple(row.value.minutes for row in rows)
    series = (
        tuple(1 if value == 0 else 0 for value in minutes),
        tuple(1 if 0 < value < 60 else 0 for value in minutes),
        tuple(1 if value >= 60 else 0 for value in minutes),
        minutes,
    )
    minute_estimates = tuple(
        estimate_series(
            (FixtureObservation(row.fixture_id, row.kickoff_time, value)
             for row, value in zip(rows, values, strict=True)),
            prior_mean=prior_mean, half_life=half_life, prior_strength=prior_strength,
            window_size=window_size,
        )
        for values, prior_mean in zip(series, minute_priors, strict=True)
    )
    if not all(estimate.available for estimate in minute_estimates):
        return result("unavailable", "no_prior_or_personal_observations")

    factor = availability.factor
    component_means = {name: factor * estimate.mean for name, estimate in components.items()}
    historical_zero, historical_under, historical_sixty, historical_minutes = (
        estimate.mean for estimate in minute_estimates
    )
    appearance = AppearanceProbabilities(
        (1 - factor) + factor * historical_zero,
        factor * historical_under,
        factor * historical_sixty,
    )
    fixture_mean = math.fsum(component_means.values())
    fixture_predictions = tuple(
        FixturePrediction(fid, fixture_mean, dict(component_means), appearance,
                          factor * historical_minutes)
        for fid in fixture_ids
    )
    event_components = {
        name: math.fsum(fixture.components[name] for fixture in fixture_predictions)
        for name in component_means
    }
    return result(
        "available", None, fixtures=fixture_predictions,
        event_mean=math.fsum(fixture.mean_points for fixture in fixture_predictions),
        event_components=event_components, appearance=appearance,
        expected_minutes_per_fixture=factor * historical_minutes,
        expected_minutes_event=math.fsum(fixture.expected_minutes for fixture in fixture_predictions),
    )
