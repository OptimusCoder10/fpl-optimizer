"""J35 slice 1, using tiny synthetic fixtures and no archive or benchmark run."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sqrt
from types import SimpleNamespace

import pytest

from fpl_optimizer.services.opponent_profiles import (
    home_term, league_mean, team_profile,
)
from fpl_optimizer.services.prediction_engine import FixtureObservation, predict
from fpl_optimizer.services.prior_selection import PriorSelection
from fpl_optimizer.services.retrospective_archive import (
    ScoredFixture, eligible_scored_fixtures,
)
from fpl_optimizer.services.retrospective_cohort import (
    BenchmarkFixture, BenchmarkRow, select_cohort,
)

T = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


def scored(identifier, kickoff, home, away, *, event=5, home_team=1, away_team=2):
    return ScoredFixture(identifier, event, kickoff, home_team, away_team, home, away)


def source_fixture(identifier, event, kickoff, home, away):
    return SimpleNamespace(id=identifier, event=event, kickoff_time=kickoff,
                           team_h=1, team_a=2, team_h_score=home, team_a_score=away)


def source_archive(fixtures):
    return SimpleNamespace(catalog=SimpleNamespace(fixtures=SimpleNamespace(root=fixtures)))


def cutoff_fixtures():
    # Target kickoff T gives a cutoff T-90m; T-4h40m is eligible.
    return (source_fixture(1, 5, T - timedelta(hours=4, minutes=40), 2, 1),
            source_fixture(2, 6, T, 9, 8))


def test_adapter_exposes_scores_only_for_cutoff_eligible_fixtures():
    fixtures = cutoff_fixtures() + (
        source_fixture(3, 4, T - timedelta(hours=4, minutes=29), 5, 5),
        source_fixture(4, 7, T + timedelta(days=1), 7, 7),
        source_fixture(5, 7, T - timedelta(days=2), 6, 4),
    )
    result = eligible_scored_fixtures(source_archive(fixtures), target_event=6)
    assert [(f.fixture_id, f.home_score, f.away_score) for f in result] == [
        (5, 6, 4), (1, 2, 1),
    ]


def test_fixture_at_exactly_three_hours_before_cutoff_is_excluded():
    exact = source_fixture(3, 5, T - timedelta(hours=4, minutes=30), 4, 3)
    result = eligible_scored_fixtures(source_archive(cutoff_fixtures() + (exact,)),
                                      target_event=6)
    assert [f.fixture_id for f in result] == [1]


def test_adapter_hides_target_event_and_later_scores():
    class ScoreTrap:
        def __init__(self, fixture):
            self.fixture = fixture

        def __getattr__(self, name):
            if name in {"team_h_score", "team_a_score"}:
                raise AssertionError("ineligible score was read")
            return getattr(self.fixture, name)

    target = source_fixture(3, 6, T + timedelta(hours=2), 90, 80)
    # A later kickoff stays hidden regardless of its event number.
    later = source_fixture(4, 7, T + timedelta(days=3), 70, 60)
    result = eligible_scored_fixtures(source_archive(cutoff_fixtures() +
                                                      (ScoreTrap(target), ScoreTrap(later))),
                                      target_event=6)
    assert [(f.fixture_id, f.home_score, f.away_score) for f in result] == [(1, 2, 1)]
    assert all(f.event != 6 for f in result)


def test_mu_is_total_goals_over_two_times_scored_fixtures():
    fixtures = (scored(1, T, 2, 1), scored(2, T, 0, 3),
                scored(3, T, 1, 1), scored(4, T, None, 9))
    mean = league_mean(fixtures)
    assert (mean.mu, mean.scored_fixtures, mean.reason) == (8 / 6, 3, None)
    assert league_mean((fixtures[-1],)).reason == "no_scored_fixtures"


def test_team_profile_shrinks_toward_mu_hand_computed():
    fixtures = (scored(1, T, 2, 1), scored(2, T, 0, 1), scored(3, T, 3, 0))
    profile = team_profile(1, fixtures, league_mean(fixtures), lookback=6, prior_matches=4)
    # μ=7/6; attack=(5+4×7/6)/7=29/21; defence=(2+4×7/6)/7=20/21.
    assert profile.attack == pytest.approx(29 / 21)
    assert profile.defence == pytest.approx(20 / 21)
    assert (profile.scored_fixtures, profile.reason) == (3, None)


def test_profile_uses_latest_L_fixtures_by_kickoff_then_id():
    fixtures = tuple(scored(i, T + timedelta(days=max(i, 2)), i, 0)
                     for i in (7, 6, 5, 4, 3, 2, 1))
    # ID 1 and 2 share the oldest kickoff; ID 1 leaves the six-fixture window.
    profile = team_profile(1, fixtures, league_mean(fixtures),
                           lookback=6, prior_matches=4)
    # μ=28/14=2; selected goals=2+3+4+5+6+7=27.
    assert profile.attack == (27 + 4 * 2) / 10
    assert profile.defence == 8 / 10


def test_profile_unavailable_under_three_scored_fixtures():
    fixtures = (scored(1, T, 1, 0), scored(2, T, 0, 1))
    profile = team_profile(1, fixtures, league_mean(fixtures), lookback=6, prior_matches=4)
    assert (profile.attack, profile.defence, profile.scored_fixtures, profile.reason) == (
        None, None, 2, "under_three_scored_fixtures",
    )
    assert team_profile(1, fixtures, league_mean(()), lookback=6,
                        prior_matches=4).reason == "mu_unavailable"


def test_profile_unavailable_when_any_score_missing_in_window():
    fixtures = tuple(scored(i, T + timedelta(days=i), 1, 0) for i in range(1, 8))
    old_missing = (replace(fixtures[0], home_score=None), *fixtures[1:])
    assert team_profile(1, old_missing, league_mean(old_missing),
                        lookback=6, prior_matches=4).reason is None
    recent_missing = (*fixtures[:-1], replace(fixtures[-1], away_score=None))
    profile = team_profile(1, recent_missing, league_mean(recent_missing),
                           lookback=6, prior_matches=4)
    assert (profile.attack, profile.defence, profile.reason) == (
        None, None, "missing_score_in_window",
    )


def test_home_term_hand_computed():
    fixtures = tuple(scored(i, T + timedelta(days=i), 2, 1) for i in range(20))
    mean = league_mean(fixtures)
    # μ=60/40=1.5; H=sqrt((40+30)/(20+30))=sqrt(7/5).
    assert mean.mu == 1.5
    assert home_term(fixtures, mean).value == sqrt(7 / 5)
    assert home_term(fixtures, mean).reason is None


def test_home_term_neutral_under_20_fixtures():
    fixtures = tuple(scored(i, T, 2, 1) for i in range(19))
    term = home_term(fixtures, league_mean(fixtures))
    assert (term.value, term.scored_fixtures, term.reason) == (
        1.0, 19, "under_twenty_scored_fixtures",
    )


def test_home_term_neutral_when_mu_not_positive():
    fixtures = tuple(scored(i, T, 0, 0) for i in range(20))
    mean = league_mean(fixtures)
    assert mean.mu == 0
    term = home_term(fixtures, mean)
    assert (term.value, term.scored_fixtures, term.reason) == (1.0, 20, "mu_not_positive")


def _future_view(fixtures):
    source = source_archive(fixtures)
    scores = eligible_scored_fixtures(source, target_event=6)
    benchmark = tuple(BenchmarkFixture(f.id, f.event, f.kickoff_time, f.team_h, f.team_a)
                      for f in fixtures)
    early = fixtures[0]
    row = BenchmarkRow(7, early.id, early.event, early.kickoff_time, True, 2, 90,
                       goals_scored=1)
    cohort = select_cohort(6, benchmark, (row,), (7,))
    player = cohort.players[0]
    forecast = predict(
        personal_rows=(FixtureObservation(row.fixture_id, row.kickoff_time, row),),
        prior=PriorSelection("personal_current_only", 0, None, (), 1),
        position=player.position, target_fixture_ids=player.scheduled_fixture_ids,
        catalog_trusted=True, chance=None, status="a", owned=False,
        rules_version="synthetic", input_version="synthetic",
    )
    assert forecast.status == "available" and forecast.event_mean == 8.0
    mean = league_mean(scores)
    context = (mean, team_profile(1, scores, mean, lookback=6, prior_matches=4),
               home_term(scores, mean))
    return scores, context, cohort, forecast


def test_future_perturbation_corrupt_target_and_later_scores_changes_nothing():
    base = cutoff_fixtures() + (source_fixture(3, 7, T + timedelta(days=1), 3, 1),)
    corrupted = (base[0], source_fixture(2, 6, T, 999, 888),
                 source_fixture(3, 7, T + timedelta(days=1), 777, 666))
    assert _future_view(base) == _future_view(corrupted)


def test_future_perturbation_add_and_remove_later_scores_changes_nothing():
    base = cutoff_fixtures() + (source_fixture(3, 7, T + timedelta(days=1), 4, 2),)
    added = (base[0], source_fixture(2, 6, T, 8, 7),
             source_fixture(3, 7, T + timedelta(days=1), 8, 7))
    removed = (base[0], source_fixture(2, 6, T, None, None),
               source_fixture(3, 7, T + timedelta(days=1), None, None))
    assert _future_view(base) == _future_view(added) == _future_view(removed)


def test_live_predict_output_unchanged():
    row = BenchmarkRow(7, 1, 5, T - timedelta(days=1), True, 2, 90,
                       goals_scored=1)
    result = predict(
        personal_rows=(FixtureObservation(1, row.kickoff_time, row),),
        prior=PriorSelection("personal_current_only", 0, None, (), 1),
        position=2, target_fixture_ids=(2,), catalog_trusted=True,
        chance=None, status="a", owned=False,
        rules_version="synthetic", input_version="synthetic",
    )
    # Defender: 2 appearance + 6 goal; no clean sheet or other scoring.
    assert (result.status, result.event_mean, result.availability_factor) == (
        "available", 8.0, 1.0,
    )
