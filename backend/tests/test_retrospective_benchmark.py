"""Hand-computed §08A baseline and metric examples."""

from datetime import datetime, timedelta, timezone
import math

import pytest

from fpl_optimizer.services.prediction_engine import AppearanceProbabilities
from fpl_optimizer.services.retrospective_benchmark import (
    ModelMinuteRecord, PlayedFixtureMinutes, PointMetricRecord,
    expanding_personal_mean_baseline, gameweek_mae,
    latest_six_personal_mean_baseline, paired_gameweek_mae_difference,
    position_mean_baseline, summarize_model_minutes, summarize_points, zero_baseline,
)
from fpl_optimizer.services.retrospective_cohort import BenchmarkRow, PlayerEvent


START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def row(fixture: int, *, player: int = 1, position: int = 2,
        minutes: int = 90, goals: int = 0) -> BenchmarkRow:
    return BenchmarkRow(player, fixture, 1, START + timedelta(days=fixture), True,
                        position, minutes, goals_scored=goals)


def event(rows=(), fixtures=(100,), *, player=1, position=2) -> PlayerEvent:
    return PlayerEvent(player, tuple(rows), position, 10, True, tuple(fixtures),
                       tuple(fixtures), 0, None, False)


def metric(player, gw, position, active, prediction, outcome, exclusion=None):
    return PointMetricRecord(player, gw, position, active, prediction, outcome, exclusion)


def minutes(value, zero, under, sixty):
    return PlayedFixtureMinutes(value + 100, value,
                                AppearanceProbabilities(zero, under, sixty))


def test_zero_baseline_predicts_zero():
    assert zero_baseline(event((row(1, goals=1),))) == 0.0


def test_position_mean_baseline_uses_only_earlier_same_position_rows():
    player = event((row(1, player=1, goals=3),))
    # Other defenders scored 2 and 8; focal defender and midfielder are excluded.
    pool = (row(1, player=1, goals=3), row(2, player=2),
            row(3, player=3, goals=1), row(4, player=4, position=3, goals=4))
    assert position_mean_baseline(player, pool) == 5.0
    assert position_mean_baseline(player, (pool[0], pool[3])) is None


def test_expanding_personal_mean_uses_all_eligible_rows():
    player = event((row(1), row(2, goals=1), row(3, minutes=0)))
    # (2 + 8 + 0) / 3, including a real zero-minute observation.
    assert expanding_personal_mean_baseline(player) == pytest.approx(10 / 3)


def test_latest_six_uses_six_newest_and_position_fallback():
    rows = tuple(row(i, goals=i - 1) for i in range(1, 8))
    player = event(reversed(rows))
    # Newest six: 8, 14, 20, 26, 32, 38 -> mean 23; oldest 2 is excluded.
    assert latest_six_personal_mean_baseline(player, ()) == 23.0
    no_personal = event((), player=9)
    assert latest_six_personal_mean_baseline(no_personal, (row(1, player=2),)) == 2.0


def test_baselines_sum_over_double_and_zero_on_blank():
    double = event((row(1), row(2, goals=1)), (100, 101))
    pool = (row(3, player=2), row(4, player=3, goals=1))
    assert position_mean_baseline(double, pool) == 10.0  # mean 5 twice
    assert expanding_personal_mean_baseline(double) == 10.0
    assert latest_six_personal_mean_baseline(double, pool) == 10.0
    blank = event((), ())
    assert [zero_baseline(blank), position_mean_baseline(blank, ()),
            expanding_personal_mean_baseline(blank),
            latest_six_personal_mean_baseline(blank, ())] == [0.0] * 4


def test_mae_by_position_and_overall():
    result = summarize_points((metric(1, 6, 2, True, 3, 1),
                               metric(2, 6, 2, False, 5, 4),
                               metric(3, 6, 3, True, 1, 4)))
    assert result.by_position[2].mae == 1.5  # (2 + 1) / 2
    assert result.by_position[3].mae == 3.0
    assert result.overall.mae == 2.0  # (2 + 1 + 3) / 3


def test_bias_sign_and_rmse():
    result = summarize_points((metric(1, 6, 2, True, 5, 3),
                               metric(2, 6, 2, True, 1, 4))).overall
    assert result.bias == -0.5  # (+2 - 3) / 2; negative is underprediction
    assert result.rmse == pytest.approx(math.sqrt(6.5))  # sqrt((4 + 9) / 2)


def test_coverage_counts_missing_predictions():
    result = summarize_points((metric(1, 6, 2, True, 2, 1),
                               metric(2, 6, 2, False, None, 5))).overall
    assert (result.eligible, result.predicted, result.missing) == (2, 1, 1)
    assert result.coverage == .5
    assert result.mae == 1.0


def test_active_cohort_metrics_use_active_players_only():
    result = summarize_points((metric(1, 6, 2, True, 3, 1),
                               metric(2, 6, 2, False, 20, 0),
                               metric(3, 6, 3, True, None, 4)))
    assert result.overall.mae == 11.0  # (2 + 20) / 2
    assert result.active_overall.mae == 2.0
    assert (result.active_overall.eligible, result.active_overall.missing) == (2, 1)
    assert result.active_by_position[3].mae is None
    model = summarize_model_minutes((
        ModelMinuteRecord(None, (minutes(0, .8, .1, .1),), active=True),
        ModelMinuteRecord(None, (minutes(90, 1, 0, 0),), active=False),
    ))
    assert model.overall.binary_appearance_brier == pytest.approx(.52)
    assert model.active.binary_appearance_brier == pytest.approx(.04)


def test_binary_appearance_brier_on_played_fixtures():
    result = summarize_model_minutes((ModelMinuteRecord(None, (
        minutes(0, .8, .1, .1), minutes(90, .2, .3, .5),
    )),)).overall
    assert result.binary_appearance_brier == pytest.approx(.04)  # (.2² + (1-.8)²)/2
    assert (result.played_fixtures, result.predicted_fixtures) == (2, 2)


def test_three_category_brier_normalized_by_two():
    result = summarize_model_minutes((ModelMinuteRecord(None, (
        minutes(30, .2, .5, .3),
    )),)).overall
    # ((.2-0)² + (.5-1)² + (.3-0)²) / 2 = .19.
    assert result.three_category_minute_brier == pytest.approx(.19)


def test_blank_has_no_minute_probability_in_brier():
    result = summarize_model_minutes((ModelMinuteRecord(None, ()),
                                      ModelMinuteRecord(None, (
                                          PlayedFixtureMinutes(1, 0, None),
                                          minutes(90, .2, .3, .5),
                                      )))).overall
    assert (result.played_fixtures, result.predicted_fixtures,
            result.missing_probabilities) == (2, 1, 1)
    assert result.binary_appearance_brier == pytest.approx(.04)


def test_paired_gameweek_difference_and_standard_error():
    candidate = (metric(1, 6, 2, True, 4, 0), metric(2, 6, 2, True, 2, 0),
                 metric(1, 7, 2, True, 5, 0), metric(1, 8, 2, True, 4, 0))
    reference = (metric(1, 6, 2, True, 1, 0), metric(2, 6, 2, True, 1, 0),
                 metric(1, 7, 2, True, 3, 0), metric(1, 8, 2, True, 2, 0))
    assert gameweek_mae(candidate) == {6: 3.0, 7: 5.0, 8: 4.0}
    paired = paired_gameweek_mae_difference(candidate, reference)
    assert paired.per_gameweek == {6: 2.0, 7: 2.0, 8: 2.0}
    assert paired.mean_difference == 2.0
    assert paired.standard_error == 0.0
    changed = (*candidate[:2], metric(1, 7, 2, True, 6, 0), candidate[3])
    paired = paired_gameweek_mae_difference(changed, reference)
    assert paired.per_gameweek == {6: 2.0, 7: 3.0, 8: 2.0}
    assert paired.mean_difference == pytest.approx(7 / 3)
    assert paired.standard_error == pytest.approx(1 / 3)
    assert paired_gameweek_mae_difference((), ()).standard_error is None
    one_week = paired_gameweek_mae_difference(candidate[:2], reference[:2])
    assert one_week.gameweek_count == 1
    assert one_week.standard_error is None


def test_baseline_predictions_cannot_enter_brier():
    baseline = zero_baseline(event((row(1),)))
    with pytest.raises(TypeError, match="ModelMinuteRecord"):
        summarize_model_minutes((baseline,))
    with pytest.raises(TypeError, match="model AppearanceProbabilities"):
        summarize_model_minutes((ModelMinuteRecord(None, (
            PlayedFixtureMinutes(100, 90, baseline),
        )),))


def test_excluded_outcome_rows_never_counted():
    accepted = metric(1, 6, 2, True, 3, 1)
    excluded = metric(2, 7, 3, True, 100, 0, "different_fixture_set")
    result = summarize_points((accepted, excluded))
    assert (result.overall.eligible, result.overall.mae) == (1, 2.0)
    assert 3 not in result.by_position
    assert gameweek_mae((accepted, excluded)) == {6: 2.0}
    reference = (metric(1, 6, 2, True, 2, 1), metric(2, 7, 3, True, 1, 0))
    assert paired_gameweek_mae_difference((accepted, excluded), reference).per_gameweek == {6: 1.0}
    brier = summarize_model_minutes((ModelMinuteRecord(None, (minutes(0, .8, .1, .1),)),
                                     ModelMinuteRecord("different_fixture_set", (
                                         minutes(90, 1, 0, 0),)))).overall
    assert brier.played_fixtures == 1
    assert brier.binary_appearance_brier == pytest.approx(.04)
