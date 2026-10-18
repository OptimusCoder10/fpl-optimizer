"""Focused checks for the configurable §04D observation window."""

from datetime import datetime, timedelta, timezone
import math
from types import SimpleNamespace

import pytest

from fpl_optimizer.services.prediction_engine import (
    FixtureObservation, estimate_series, predict,
)
from fpl_optimizer.services.prior_selection import PriorSelection


START = datetime(2026, 9, 1, tzinfo=timezone.utc)
ZERO_COUNTS = dict(
    minutes=0, goals_scored=0, assists=0, clean_sheets=0, saves=0,
    goals_conceded=0, clearances_blocks_interceptions=0, tackles=0,
    recoveries=0, bonus=0, penalties_saved=0, penalties_missed=0,
    yellow_cards=0, red_cards=0, own_goals=0,
)


def observation(fixture_id: int, value):
    return FixtureObservation(fixture_id, START + timedelta(days=fixture_id), value)


def personal_only_prior():
    return PriorSelection("personal_current_only", 0, None, (), 8)


def prediction(rows, *, window_size=None):
    arguments = {} if window_size is None else {"window_size": window_size}
    return predict(
        personal_rows=rows, prior=personal_only_prior(), position=2,
        target_fixture_ids=(100,), catalog_trusted=True, chance=None, status="a",
        owned=False, rules_version="fpl-2026-v1", input_version="synthetic-lookback",
        **arguments,
    )


def test_default_window_matches_explicit_twelve():
    rows = tuple(observation(i, SimpleNamespace(**(
        ZERO_COUNTS | {"minutes": 0 if i <= 2 else 60}
    ))) for i in range(1, 15))

    default = prediction(rows)
    explicit = prediction(rows, window_size=12)

    assert default == explicit
    assert default.raw_sample_count == 12
    assert default.event_mean == pytest.approx(2)


def test_six_uses_only_six_most_recent_observations():
    shuffled_ids = (7, 2, 9, 4, 1, 8, 3, 6, 5)
    rows = [observation(i, 100 + i if i <= 3 else 2) for i in shuffled_ids]

    result = estimate_series(rows, window_size=6)

    assert result.raw_count == 6
    assert result.mean == pytest.approx(2)


def test_six_applies_prior_and_reports_ess_from_selected_rows_only():
    rows = [observation(i, 100 if i <= 2 else 2) for i in range(1, 9)]
    weights = [2 ** (-rank) for rank in range(6)]
    weight_sum = sum(weights)

    result = estimate_series(
        rows, prior_mean=10, half_life=1, prior_strength=2, window_size=6,
    )

    assert result.raw_count == 6
    assert result.mean == pytest.approx((2 * weight_sum + 2 * 10) / (weight_sum + 2))
    assert result.effective_sample_size == pytest.approx(
        weight_sum**2 / sum(weight**2 for weight in weights)
    )


def test_predict_passes_six_to_points_minutes_and_evidence():
    rows = tuple(observation(i, SimpleNamespace(**(
        ZERO_COUNTS | {"minutes": 0 if i <= 2 else 60}
    ))) for i in range(1, 9))

    six = prediction(rows, window_size=6)
    default = prediction(rows)

    assert six.raw_sample_count == 6
    assert six.event_mean == pytest.approx(2)
    assert six.appearance_probabilities.zero_minutes == pytest.approx(0)
    assert six.expected_minutes_per_fixture == pytest.approx(60)
    assert six.effective_sample_size == pytest.approx(
        sum(2 ** (-rank / 6) for rank in range(6))**2
        / sum(2 ** (-2 * rank / 6) for rank in range(6))
    )
    assert default.raw_sample_count == 8
    assert default.event_mean < six.event_mean


def test_predict_window_six_applies_to_all_point_and_minute_outputs():
    outcomes = {
        1: (15, 1), 2: (70, 2), 3: (90, 3),
        4: (0, 0), 5: (30, 1), 6: (60, 0),
        7: (0, 0), 8: (45, 0), 9: (90, 1),
    }
    rows = tuple(
        observation(i, SimpleNamespace(**(
            ZERO_COUNTS | {"minutes": outcomes[i][0], "goals_scored": outcomes[i][1]}
        )))
        for i in (7, 2, 9, 4, 1, 8, 3, 6, 5)
    )
    weights = [2 ** (-rank / 6) for rank in range(6)]
    total_weight = math.fsum(weights)

    six = prediction(rows, window_size=6)
    nine = prediction(rows, window_size=9)
    appearance = six.appearance_probabilities
    goals = 6 * (weights[0] + weights[4]) / total_weight

    assert six.raw_sample_count == 6
    assert appearance.zero_minutes == pytest.approx((weights[2] + weights[5]) / total_weight)
    assert appearance.under_60 == pytest.approx((weights[1] + weights[4]) / total_weight)
    assert appearance.sixty_plus == pytest.approx((weights[0] + weights[3]) / total_weight)
    assert six.expected_minutes_per_fixture == pytest.approx(
        (90 * weights[0] + 45 * weights[1] + 60 * weights[3] + 30 * weights[4])
        / total_weight
    )
    assert six.per_fixture[0].components["goals"] == pytest.approx(goals)
    assert six.event_components["goals"] == pytest.approx(goals)
    assert nine.raw_sample_count == 9
    assert nine.per_fixture[0].components["goals"] != pytest.approx(goals)
    assert nine.appearance_probabilities.zero_minutes != pytest.approx(appearance.zero_minutes)


def test_window_larger_than_available_rows_uses_all_rows():
    values = [observation(i, value) for i, value in ((1, 1), (2, 4), (3, 8))]
    weights = [1, 2 ** (-1 / 2), 1 / 2]

    result = estimate_series(values, half_life=2, window_size=20)

    assert result.raw_count == 3
    assert result.mean == pytest.approx(
        (8 * weights[0] + 4 * weights[1] + weights[2]) / sum(weights)
    )
    assert result == estimate_series(values, half_life=2, window_size=3)


@pytest.mark.parametrize("window_size", [0, -1, 1.5, True, "6"])
def test_window_size_requires_a_positive_integer(window_size):
    with pytest.raises(ValueError, match="window_size"):
        estimate_series([observation(1, 2)], window_size=window_size)
    with pytest.raises(ValueError, match="window_size"):
        prediction((), window_size=window_size)
