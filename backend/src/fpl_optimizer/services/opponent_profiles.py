"""Pure, cutoff-scoped J35 league and team context for the opponent experiment."""

from dataclasses import dataclass
from math import fsum, sqrt
from typing import Iterable

from fpl_optimizer.services.retrospective_archive import ScoredFixture


SETTINGS = frozenset({(6, 4), (6, 12), (12, 4), (12, 12)})


def _complete(fixture: ScoredFixture) -> bool:
    return fixture.home_score is not None and fixture.away_score is not None


@dataclass(frozen=True)
class LeagueMean:
    mu: float | None
    scored_fixtures: int
    reason: str | None = None


@dataclass(frozen=True)
class TeamProfile:
    team_id: int
    attack: float | None
    defence: float | None
    scored_fixtures: int
    reason: str | None = None


@dataclass(frozen=True)
class HomeTerm:
    value: float
    scored_fixtures: int
    reason: str | None = None


def league_mean(fixtures: Iterable[ScoredFixture]) -> LeagueMean:
    """μ uses every complete scored earlier league fixture, not team windows."""
    scored = tuple(fixture for fixture in fixtures if _complete(fixture))
    if not scored:
        return LeagueMean(None, 0, "no_scored_fixtures")
    goals = fsum(fixture.home_score + fixture.away_score for fixture in scored)
    return LeagueMean(goals / (2 * len(scored)), len(scored))


def team_profile(
    team_id: int, fixtures: Iterable[ScoredFixture], mean: LeagueMean, *,
    lookback: int, prior_matches: int,
) -> TeamProfile:
    """Use the latest L team fixtures by kickoff and ID, then shrink to μ."""
    if (lookback, prior_matches) not in SETTINGS:
        raise ValueError("team profile requires a declared J35 setting")
    if mean.mu is None:
        return TeamProfile(team_id, None, None, 0, "mu_unavailable")
    window = tuple(sorted((fixture for fixture in fixtures if team_id in (
        fixture.home_team_id, fixture.away_team_id
    )), key=lambda fixture: (fixture.kickoff_time, fixture.fixture_id))[-lookback:])
    scored_count = sum(_complete(fixture) for fixture in window)
    if any(not _complete(fixture) for fixture in window):
        return TeamProfile(team_id, None, None, scored_count, "missing_score_in_window")
    if scored_count < 3:
        return TeamProfile(team_id, None, None, scored_count, "under_three_scored_fixtures")
    goals_for = fsum(fixture.home_score if fixture.home_team_id == team_id
                     else fixture.away_score for fixture in window)
    goals_against = fsum(fixture.away_score if fixture.home_team_id == team_id
                         else fixture.home_score for fixture in window)
    denominator = scored_count + prior_matches
    return TeamProfile(team_id,
                       (goals_for + prior_matches * mean.mu) / denominator,
                       (goals_against + prior_matches * mean.mu) / denominator,
                       scored_count)


def home_term(fixtures: Iterable[ScoredFixture], mean: LeagueMean) -> HomeTerm:
    """One shared venue term; unavailable evidence returns visible neutral 1."""
    scored = tuple(fixture for fixture in fixtures if _complete(fixture))
    count = len(scored)
    if count < 20:
        return HomeTerm(1.0, count, "under_twenty_scored_fixtures")
    if mean.mu is None or mean.mu <= 0:
        return HomeTerm(1.0, count, "mu_not_positive")
    home_goals = fsum(fixture.home_score for fixture in scored)
    away_goals = fsum(fixture.away_score for fixture in scored)
    return HomeTerm(sqrt((home_goals + 20 * mean.mu) /
                         (away_goals + 20 * mean.mu)), count)
