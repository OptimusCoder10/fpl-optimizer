"""§04D observation-window and recency-mixture checks with eligible synthetic rows."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fpl_optimizer.services.prediction_engine import (
    FixtureObservation, estimate_components, estimate_series,
)
from fpl_optimizer.services.scoring_components import score_fixture


START = datetime(2026, 9, 1, tzinfo=timezone.utc)


def row(fixture_id: int, value: int | float, *, day: int | None = None):
    return FixtureObservation(
        fixture_id=fixture_id,
        kickoff_time=START + timedelta(days=fixture_id if day is None else day),
        value=value,
    )


def test_exact_shrinkage_three_observations_with_prior():
    # Newest to oldest: 2, 4, 8. With h=1: weights 1, 1/2, 1/4.
    # Numerator = 2 + 2 + 2 + 3*1 = 9; denominator = 1.75 + 3 = 4.75.
    result = estimate_series([row(1, 8), row(3, 2), row(2, 4)],
                             prior_mean=1, half_life=1, prior_strength=3)
    assert result.mean == pytest.approx(36 / 19, rel=1e-14)
    assert result.raw_count == 3


def test_recency_order_newest_weight_one_then_half_life_six():
    decay = 2 ** (-1 / 6)
    result = estimate_series([row(1, 0), row(3, 4), row(2, 1)])
    assert result.mean == pytest.approx((4 + decay) / (1 + decay + decay**2),
                                        rel=1e-14)


def test_more_than_twelve_uses_only_newest_twelve():
    shuffled_ids = [7, 2, 15, 4, 11, 1, 9, 14, 3, 5, 13, 8, 6, 12, 10]
    rows = [row(i, 100 + i if i <= 3 else 2) for i in shuffled_ids]
    result = estimate_series(rows)
    assert result.raw_count == 12
    assert result.mean == pytest.approx(2)


def test_kickoff_tie_broken_by_fixture_id():
    rows = [row(i, 0) for i in range(1, 12)]
    rows += [row(12, 0, day=50), row(13, 1, day=50)]
    result = estimate_series(rows, half_life=0.01)
    assert result.raw_count == 12
    assert result.mean == pytest.approx(1, rel=1e-12)


def test_row_without_kickoff_is_rejected():
    with pytest.raises(ValueError, match="kickoff_time"):
        estimate_series([row(2, 2), FixtureObservation(1, None, 1)])


def test_genuine_zero_minute_rows_count_without_invented_rows():
    counts = dict(minutes=0, goals_scored=0, assists=0, clean_sheets=0,
                  saves=0, goals_conceded=0, clearances_blocks_interceptions=0,
                  tackles=0, recoveries=0, bonus=0, penalties_saved=0,
                  penalties_missed=0, yellow_cards=0, red_cards=0, own_goals=0)
    zero = score_fixture(SimpleNamespace(**counts), 2)
    played = score_fixture(SimpleNamespace(**(counts | {"minutes": 60})), 2)
    result = estimate_components([
        row(1, played), row(2, zero),
    ], half_life=1)
    assert result["appearance"].raw_count == 2
    assert result["appearance"].mean == pytest.approx(2 / 3)
    assert result["goals"].mean == 0


def test_no_prior_with_personal_rows_omits_kappa():
    result = estimate_series([row(1, 4), row(2, 2)], half_life=1,
                             prior_strength=100)
    assert result.mean == pytest.approx((2 + 0.5 * 4) / 1.5)


def test_no_prior_and_no_rows_is_unavailable_not_zero():
    result = estimate_series([])
    assert result.mean is None
    assert not result.available
    assert result.raw_count == 0


def test_prior_only_supplies_mean_with_zero_personal_sample():
    result = estimate_series([], prior_mean=1.25)
    assert result.mean == 1.25
    assert result.available
    assert result.raw_count == 0
    assert result.effective_sample_size == 0


def test_effective_sample_size_exact_and_zero_for_no_rows():
    result = estimate_series([row(1, 0), row(2, 0)], half_life=1)
    assert result.effective_sample_size == pytest.approx((1.5**2) / 1.25)
    assert estimate_series([], prior_mean=2).effective_sample_size == 0


def test_prior_free_single_observation_returns_its_value():
    result = estimate_series([row(1, -3)])
    assert result.mean == -3
    assert result.raw_count == 1
    assert result.effective_sample_size == 1


def test_indicator_series_uses_same_mixture():
    # Two observed 0/1 indicators and a prior probability of 1/4.
    result = estimate_series([row(1, 0), row(2, 1)],
                             prior_mean=0.25, half_life=1, prior_strength=2)
    assert result.mean == pytest.approx((1 + 0.5 * 0 + 2 * 0.25) / 3.5)


def test_same_input_is_deterministic():
    rows = [row(3, 2), row(1, 4), row(2, 0)]
    assert estimate_series(rows, prior_mean=1) == estimate_series(
        list(reversed(rows)), prior_mean=1
    )


def test_component_wrapper_requires_complete_prior():
    with pytest.raises(ValueError, match="exactly the scoring components"):
        estimate_components([], prior_means={"appearance": 1})


@pytest.mark.parametrize("parameter,value", [
    ("half_life", 0), ("half_life", float("inf")),
    ("prior_strength", -1), ("prior_strength", float("nan")),
])
def test_invalid_mixture_parameters_are_rejected(parameter, value):
    with pytest.raises(ValueError, match=parameter):
        estimate_series([row(1, 2)], **{parameter: value})
