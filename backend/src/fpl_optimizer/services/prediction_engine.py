"""Pure observation-window and prior-mixture arithmetic from §04D steps 1–2."""

from dataclasses import dataclass, fields
from datetime import datetime
import math
from typing import Generic, Iterable, Mapping, TypeVar

from fpl_optimizer.services.scoring_components import ScoringComponents


T = TypeVar("T")
WINDOW_SIZE = 12


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


def _recent_window(rows: Iterable[FixtureObservation[T]]) -> tuple[FixtureObservation[T], ...]:
    observations = tuple(rows)
    if any(row.kickoff_time is None for row in observations):
        raise ValueError("every fixture observation requires a kickoff_time")
    return tuple(sorted(
        observations,
        key=lambda row: (row.kickoff_time, row.fixture_id),
        reverse=True,
    )[:WINDOW_SIZE])


def estimate_series(
    rows: Iterable[FixtureObservation[int | float]],
    *,
    prior_mean: int | float | None = None,
    half_life: float = 6,
    prior_strength: float = 3,
) -> SeriesEstimate:
    """Weight the newest 12 outcomes and optionally shrink toward an input prior.

    Eligibility, observed threshold scoring and prior selection belong to callers.
    The numeric values can also be 0/1 indicators or observed minutes.
    """
    if not math.isfinite(half_life) or half_life <= 0:
        raise ValueError("half_life must be finite and positive")
    if not math.isfinite(prior_strength) or prior_strength < 0:
        raise ValueError("prior_strength must be finite and nonnegative")
    if prior_mean is not None and not math.isfinite(prior_mean):
        raise ValueError("prior_mean must be finite")

    recent = _recent_window(rows)
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
        )
        for name in component_names
    }
