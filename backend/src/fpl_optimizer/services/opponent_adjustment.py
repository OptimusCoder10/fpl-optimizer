"""Pure J35 adjustment of an already computed retrospective prediction."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import fsum
from typing import Iterable

from fpl_optimizer.services.opponent_profiles import (
    SETTINGS, home_term, league_mean, team_profile,
)
from fpl_optimizer.services.prediction_engine import (
    FixtureObservation, Prediction, _recent_window,
)
from fpl_optimizer.services.prior_selection import PriorSelection
from fpl_optimizer.services.retrospective_archive import ScoredFixture
from fpl_optimizer.services.retrospective_cohort import BenchmarkFixture, BenchmarkRow


@dataclass(frozen=True)
class RawFactors:
    attack: float
    clean_sheet: float
    reason: str | None = None
    home_reason: str | None = None


@dataclass(frozen=True)
class FixtureAdjustment:
    fixture_id: int
    attack: float
    clean_sheet: float
    reason: str | None = None
    home_reason: str | None = None


@dataclass(frozen=True)
class AdjustedPrediction:
    prediction: Prediction
    fixtures: tuple[FixtureAdjustment, ...]
    historical_attack_B: float | None
    historical_clean_sheet_B: float | None
    kappa_eff: float
    kappa_reason: str | None = None


def raw_factors(*, opponent_id: int, was_home: bool,
                scored_fixtures: Iterable[ScoredFixture],
                setting: tuple[int, int]) -> RawFactors:
    """Compute both raw groups from the same caller-supplied cutoff context."""
    if setting not in SETTINGS:
        raise ValueError("setting must be one of the four declared J35 settings")
    fixtures = tuple(scored_fixtures)
    mean = league_mean(fixtures)
    if mean.mu is None or mean.mu <= 0:
        return RawFactors(1.0, 1.0, "mu_unavailable")
    profile = team_profile(opponent_id, fixtures, mean,
                           lookback=setting[0], prior_matches=setting[1])
    if profile.reason is not None:
        return RawFactors(1.0, 1.0, profile.reason)
    assert profile.attack is not None and profile.defence is not None
    if profile.attack <= 0:
        return RawFactors(1.0, 1.0, "profile_attack_nonpositive")
    home = home_term(fixtures, mean)
    venue = home.value if was_home else 1 / home.value
    return RawFactors(venue * profile.defence / mean.mu,
                      venue * mean.mu / profile.attack, home_reason=home.reason)


def historical_context_B(factors: Iterable[float], *, half_life: float,
                         kappa_eff: float) -> float:
    """Newest-first factors share the baseline's recency weights; prior is neutral 1."""
    values = tuple(factors)
    weights = tuple(2 ** (-rank / half_life) for rank in range(len(values)))
    return (fsum(weight * value for weight, value in zip(weights, values, strict=True))
            + kappa_eff) / (fsum(weights) + kappa_eff)


def adjust_prediction(
    prediction: Prediction, *, player_rows: Iterable[BenchmarkRow],
    prior: PriorSelection, target_fixtures: Iterable[BenchmarkFixture],
    club_id: int, scored_fixtures: Iterable[ScoredFixture],
    event_cutoff: datetime,
    setting: tuple[int, int] | None = None, half_life: float = 6,
    prior_strength: float = 3, window_size: int = 12,
) -> AdjustedPrediction:
    """Adjust only point components; neutral and blank results preserve identity."""
    if setting is None or prediction.status != "available":
        return AdjustedPrediction(prediction, (), None, None, 0.0)
    if setting not in SETTINGS:
        raise ValueError("setting must be one of the four declared J35 settings")
    # Reuse the exact baseline window order, including its fixture-ID tie-break.
    recent = _recent_window((FixtureObservation(row.fixture_id, row.kickoff_time, row)
                             for row in player_rows), window_size)
    rows = tuple(observation.value for observation in recent)
    target_list = tuple(target_fixtures)
    targets = {fixture.fixture_id: fixture for fixture in target_list}
    target_events = {fixture.event for fixture in target_list}
    admitted = []
    for fixture in scored_fixtures:
        if fixture.event in target_events:
            continue
        if (fixture.event > 19
                or fixture.kickoff_time + timedelta(hours=3) >= event_cutoff):
            continue
        admitted.append(fixture)
    scored = tuple(admitted)
    historical = {fixture.fixture_id: fixture for fixture in scored}
    if len(rows) < 4:
        adjustments = tuple(FixtureAdjustment(f.fixture_id, 1.0, 1.0,
                                               "under_four_player_observations")
                            for f in prediction.per_fixture)
        return AdjustedPrediction(prediction, adjustments, None, None, 0.0,
                                  "no_prior" if prior.component_means is None else None)
    kappa = prior_strength if prior.component_means is not None else 0.0
    kappa_reason = None if prior.component_means is not None else "no_prior"
    old = []
    for row in rows:
        fixture = historical.get(row.fixture_id)
        if fixture is None or fixture.home_score is None or fixture.away_score is None:
            old.append(RawFactors(1.0, 1.0, "missing_historical_score"))
            continue
        opponent = fixture.away_team_id if row.was_home else fixture.home_team_id
        old.append(raw_factors(opponent_id=opponent, was_home=row.was_home,
                               scored_fixtures=scored, setting=setting))
    attack_B = historical_context_B((f.attack for f in old),
                                    half_life=half_life, kappa_eff=kappa)
    clean_B = historical_context_B((f.clean_sheet for f in old),
                                   half_life=half_life, kappa_eff=kappa)
    adjustments = []
    revised = []
    for forecast in prediction.per_fixture:
        fixture = targets.get(forecast.fixture_id)
        if fixture is None or club_id not in (fixture.home_team_id, fixture.away_team_id):
            raw = RawFactors(1.0, 1.0, "missing_target_fixture")
        else:
            was_home = fixture.home_team_id == club_id
            opponent = fixture.away_team_id if was_home else fixture.home_team_id
            raw = raw_factors(opponent_id=opponent, was_home=was_home,
                              scored_fixtures=scored, setting=setting)
        if raw.reason is None and (attack_B <= 0 or clean_B <= 0):
            raw = RawFactors(1.0, 1.0, "historical_context_nonpositive")
        attack = raw.attack / attack_B if raw.reason is None else 1.0
        clean = raw.clean_sheet / clean_B if raw.reason is None else 1.0
        adjustments.append(FixtureAdjustment(forecast.fixture_id, attack, clean,
                                              raw.reason, raw.home_reason))
        components = dict(forecast.components)
        for name in ("goals", "assists"):
            components[name] *= attack
        components["clean_sheet"] *= clean
        revised.append(replace(forecast, components=components,
                               mean_points=fsum(components.values())))
    event_components = {name: fsum(f.components[name] for f in revised)
                        for name in revised[0].components}
    return AdjustedPrediction(replace(
        prediction, per_fixture=tuple(revised),
        event_mean=fsum(f.mean_points for f in revised),
        event_components=event_components,
    ), tuple(adjustments), attack_B, clean_B, kappa, kappa_reason)
