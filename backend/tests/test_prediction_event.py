"""§04D event prediction and §06C availability from pure synthetic inputs."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fpl_optimizer.services.prediction_engine import FixtureObservation, predict
from fpl_optimizer.services.prior_archive import PriorInputs
from fpl_optimizer.services.prior_selection import select_prior


START = datetime(2026, 10, 1, tzinfo=timezone.utc)
COUNTS = dict(
    minutes=0, goals_scored=0, assists=0, clean_sheets=0, saves=0,
    goals_conceded=0, clearances_blocks_interceptions=0, tackles=0,
    recoveries=0, bonus=0, penalties_saved=0, penalties_missed=0,
    yellow_cards=0, red_cards=0, own_goals=0,
)


@dataclass(frozen=True)
class Row:
    player_id: int
    position: int
    scoring_counts: dict[str, int]


def row(player_id: int, minutes: int, **changes: int) -> Row:
    return Row(player_id, 2, COUNTS | {"minutes": minutes} | changes)


def observation(fixture_id: int, source: Row) -> FixtureObservation:
    return FixtureObservation(fixture_id, START + timedelta(days=fixture_id),
                              SimpleNamespace(**source.scoring_counts))


def selection(*, personal=(), pool=None):
    pool = [row(50, 60)] if pool is None else pool
    archive = PriorInputs("2025-26", None, None, (), (), (), None, None)
    return select_prior(player_id=42, position=2, current_rows=[*personal, *pool],
                        previous=archive)


def prediction(*, personal=(), prior=None, fixtures=(100,), chance=None, status="a",
               owned=False, assumed_factor=None, catalog_trusted=True,
               half_life=6, prior_strength=3):
    return predict(
        personal_rows=personal, prior=selection() if prior is None else prior,
        position=2, target_fixture_ids=fixtures, catalog_trusted=catalog_trusted,
        chance=chance, status=status, owned=owned, assumed_factor=assumed_factor,
        rules_version="fpl-2026-v1", input_version="captured-input-7",
        half_life=half_life, prior_strength=prior_strength,
    )


def test_chance_75_scales_to_075_and_chance_0_gives_zero_points_and_certain_zero_minutes():
    reduced = prediction(chance=75)
    zero = prediction(chance=0)
    assert reduced.availability_factor == 0.75
    assert reduced.event_mean == 1.5
    assert zero.availability_factor == 0
    assert zero.event_mean == 0
    assert zero.appearance_probabilities.zero_minutes == 1
    assert zero.expected_minutes_event == 0


def test_null_chance_with_status_a_uses_factor_one():
    result = prediction()
    assert result.availability_factor == 1
    assert result.event_mean == 2


@pytest.mark.parametrize("status", ["d", "i", "s", "u", "n"])
def test_null_chance_with_non_a_status_is_unresolved(status):
    result = prediction(status=status)
    assert result.status == "unavailable"
    assert result.reason == "missing_chance_for_status"
    assert result.event_mean is None
    assert result.availability_factor is None


def test_numeric_chance_with_unrecognized_status_is_unresolved():
    result = prediction(chance=40, status="mystery")
    assert result.status == "unavailable"
    assert result.reason == "unrecognized_status"
    assert result.event_mean is None


@pytest.mark.parametrize("status", ["i", "s", "u"])
def test_status_i_s_u_with_chance_100_is_unresolved(status):
    result = prediction(chance=100, status=status)
    assert result.status == "unavailable"
    assert result.reason == "contradictory_chance_and_status"


@pytest.mark.parametrize("factor", [0, 0.4, 1])
def test_owned_assumption_in_range_is_used_and_labelled(factor):
    result = prediction(chance=None, status="i", owned=True, assumed_factor=factor)
    assert result.status == "available"
    assert result.availability_factor == factor
    assert result.availability_assumption is True
    assert "availability_assumption" in result.warnings
    assert result.event_mean == pytest.approx(2 * factor)


@pytest.mark.parametrize("factor", [-0.01, 1.01, float("nan"), float("inf")])
def test_owned_assumption_out_of_range_is_rejected(factor):
    with pytest.raises(ValueError, match="assumed_factor"):
        prediction(owned=True, assumed_factor=factor)


def test_single_fixture_once_and_double_fixture_exactly_twice():
    single = prediction(fixtures=(101,))
    double = prediction(fixtures=(103, 101))
    assert len(single.per_fixture) == 1
    assert tuple(f.fixture_id for f in double.per_fixture) == (101, 103)
    assert double.event_mean == 2 * single.event_mean
    assert double.event_components == {name: 2 * value
                                       for name, value in single.event_components.items()}
    assert double.expected_minutes_event == 2 * single.expected_minutes_event


def test_trusted_blank_is_exactly_zero_with_null_probabilities_and_no_prior():
    result = prediction(prior=selection(pool=[]), fixtures=(), status="i")
    assert result.status == "blank"
    assert result.event_mean == 0
    assert all(value == 0 for value in result.event_components.values())
    assert result.per_fixture == ()
    assert result.appearance_probabilities is None
    assert result.expected_minutes_per_fixture is None


def test_untrusted_zero_fixtures_are_unavailable_not_blank():
    result = prediction(fixtures=(), catalog_trusted=False)
    assert result.status == "unavailable"
    assert result.reason == "untrusted_fixture_catalog"
    assert result.event_mean is None


def test_untrusted_nonempty_fixture_catalog_is_unavailable():
    result = prediction(fixtures=(100,), catalog_trusted=False)
    assert result.status == "unavailable"
    assert result.reason == "untrusted_fixture_catalog"


def test_no_prior_and_no_personal_rows_are_unavailable_not_zero():
    result = prediction(prior=selection(pool=[]))
    assert result.status == "unavailable"
    assert result.reason == "no_prior_or_personal_observations"
    assert result.event_mean is None
    assert result.raw_sample_count == 0


def test_no_prior_with_personal_rows_uses_weighted_mean_without_kappa():
    old, new = row(42, 0), row(42, 60)
    result = prediction(
        personal=(observation(1, old), observation(2, new)),
        prior=selection(personal=(old, new), pool=[]),
        half_life=1, prior_strength=100,
    )
    assert result.status == "available"
    assert result.prior_source == "personal_current_only"
    assert result.prior_row_count == 0
    assert result.event_mean == pytest.approx(4 / 3)
    assert result.appearance_probabilities.zero_minutes == pytest.approx(1 / 3)
    assert result.expected_minutes_per_fixture == pytest.approx(40)
    assert result.raw_sample_count == 2
    assert result.effective_sample_size == pytest.approx(1.5**2 / 1.25)


def test_scored_components_are_scaled_and_summed_for_the_event():
    player_row = row(42, 60, goals_scored=1, assists=1)
    result = prediction(
        personal=(observation(1, player_row),),
        prior=selection(personal=(player_row,), pool=[]),
        fixtures=(101, 102), chance=75,
    )
    assert result.status == "available"
    assert result.per_fixture[0].components["appearance"] == 1.5
    assert result.per_fixture[0].components["goals"] == 4.5
    assert result.per_fixture[0].components["assists"] == 2.25
    assert result.per_fixture[0].mean_points == 8.25
    assert result.event_components["goals"] == 9
    assert result.event_mean == 16.5


def test_unresolved_availability_propagates_without_reading_unavailable_mean():
    result = prediction(prior=selection(pool=[]), status="i")
    assert result.status == "unavailable"
    assert result.reason == "missing_chance_for_status"
    assert result.event_mean is None


def test_zero_probability_formula_is_exact_on_worked_case_and_categories_sum_to_one():
    result = prediction(prior=selection(pool=[row(50, 0), row(51, 60)]), chance=75)
    probabilities = result.appearance_probabilities
    assert probabilities.zero_minutes == (1 - 0.75) + 0.75 * 0.5 == 0.625
    assert probabilities.under_60 == 0
    assert probabilities.sixty_plus == 0.375
    assert sum((probabilities.zero_minutes, probabilities.under_60,
                probabilities.sixty_plus)) == 1
    assert result.expected_minutes_per_fixture == 22.5


def test_same_availability_factor_is_used_for_every_fixture_of_a_double():
    result = prediction(fixtures=(102, 101), chance=75)
    assert len(result.per_fixture) == 2
    assert result.per_fixture[0].components == result.per_fixture[1].components
    assert result.per_fixture[0].appearance_probabilities == result.per_fixture[1].appearance_probabilities
    assert result.per_fixture[0].expected_minutes == result.per_fixture[1].expected_minutes
    assert result.per_fixture[0].mean_points == result.per_fixture[1].mean_points == 1.5


def test_prediction_is_deterministic_and_contains_model_rules_and_input_versions():
    oldest, newest = row(42, 0), row(42, 60)
    personal = (observation(1, oldest), observation(2, newest))
    prior = selection(personal=(oldest, newest))
    first = prediction(personal=personal, prior=prior, fixtures=(104, 103), chance=75)
    second = prediction(personal=reversed(personal), prior=prior,
                        fixtures=(103, 104), chance=75)
    assert first == second
    assert first.model_version == "empirical-fixture-v1"
    assert first.rules_version == "fpl-2026-v1"
    assert first.input_version == "captured-input-7"
    assert first.raw_sample_count == 2
    assert first.prior_source == "current_season_position"


def test_real_select_prior_result_chains_into_predict():
    prior = selection(pool=[row(50, 0), row(51, 60)])
    result = prediction(prior=prior, chance=75)
    assert result.status == "available"
    assert result.prior_source == prior.source
    assert result.prior_row_count == prior.row_count == 2
    assert result.per_fixture[0].appearance_probabilities.zero_minutes == 0.625


@pytest.mark.parametrize("chance", [-1, 101, float("nan"), float("inf"), True])
def test_invalid_numeric_chance_is_rejected(chance):
    with pytest.raises(ValueError, match="chance"):
        prediction(chance=chance)


def test_unowned_player_cannot_supply_availability_assumption():
    with pytest.raises(ValueError, match="owned player"):
        prediction(assumed_factor=0.5)
