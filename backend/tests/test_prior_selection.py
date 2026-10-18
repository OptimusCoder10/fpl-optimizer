"""§04D step 3: first complete prior source and explicit provenance."""

from dataclasses import dataclass

import pytest

from fpl_optimizer.services.prior_archive import PriorInputs
from fpl_optimizer.services.prior_selection import select_prior
from fpl_optimizer.services.scoring_components import ScoringComponents


COUNTS = dict(
    minutes=0, goals_scored=0, assists=0, clean_sheets=0, saves=0,
    goals_conceded=0, clearances_blocks_interceptions=0, tackles=0,
    recoveries=0, bonus=0, penalties_saved=0, penalties_missed=0,
    yellow_cards=0, red_cards=0, own_goals=0,
)


@dataclass(frozen=True)
class Row:
    player_id: int
    position: int = 2
    scoring_counts: dict | None = None
    name: str = ""
    external_code: int = 0


def row(pid, *, position=2, name="", code=0, **changes):
    return Row(pid, position, COUNTS | changes, name, code)


def prior(*, personal_id=None, reason=None, personal=(), position=(), exclusions=()):
    return PriorInputs("2025-26", personal_id, reason, tuple(personal),
                       tuple(position), tuple(exclusions), None, None)


def select(current=(), previous=None):
    return select_prior(player_id=42, position=2, current_rows=current,
                        previous=previous or prior())


def reasons(result):
    return {(item.source, item.reason): item.count for item in result.exclusions}


def test_a_wins_when_identity_validated_and_position_matches():
    result = select([row(50, minutes=60, goals_scored=5)], prior(
        personal_id=817, personal=[row(817, minutes=60, goals_scored=1)],
        position=[row(818, minutes=60, goals_scored=3)],
    ))
    assert result.source == "previous_season_personal"
    assert result.row_count == 1
    assert result.component_means["goals"] == 6


@pytest.mark.parametrize("reason", [
    "temporary_or_invalid_current_code", "temporary_or_invalid_prior_code",
    "external_code_match_not_one_to_one",
])
def test_a_skipped_for_unvalidated_temporary_or_non_unique_identity(reason):
    result = select([row(50, minutes=60)], prior(
        reason=reason, position=[row(817, minutes=60)],
    ))
    assert result.source == "current_season_position"
    assert result.component_means["appearance"] == 2
    assert reasons(result)[("previous_season_personal", reason)] == 1


def test_a_skipped_on_position_change_reclassified_player():
    result = select([row(50, minutes=60)], prior(
        reason="position_changed", position=[row(817, minutes=60)],
    ))
    assert result.source == "current_season_position"
    assert reasons(result)[("previous_season_personal", "position_changed")] == 1


def test_b_used_when_a_unavailable_and_target_rows_excluded():
    result = select([row(42, minutes=60, goals_scored=9),
                     row(50, minutes=60, goals_scored=1),
                     row(51, minutes=60, goals_scored=3)], prior(
        reason="external_code_match_not_one_to_one",
        position=[row(817, minutes=60, goals_scored=8)],
    ))
    assert result.source == "current_season_position"
    assert result.row_count == 2
    assert result.personal_current_count == 1
    assert result.component_means["goals"] == 12


def test_b_mean_includes_genuine_zeros():
    result = select([row(50), row(51, minutes=60)])
    assert result.source == "current_season_position"
    assert result.row_count == 2
    assert result.component_means["appearance"] == 1


def test_c_used_when_a_and_b_empty():
    result = select([row(42, minutes=60)], prior(
        reason="position_changed", position=[row(817, minutes=60)],
    ))
    assert result.source == "previous_season_position"
    assert result.row_count == 1
    assert result.component_means["appearance"] == 2


def test_all_three_empty_is_unavailable_never_zero():
    result = select()
    assert result.source == "unavailable"
    assert result.row_count == result.personal_current_count == 0
    assert result.component_means is None


def test_missing_required_count_excluded_with_reason_not_zero():
    incomplete = row(50, minutes=None)
    result = select([incomplete, row(51, minutes=60)])
    assert result.source == "current_season_position"
    assert result.row_count == 1
    assert result.component_means["appearance"] == 2
    assert reasons(result)[("current_season_position", "missing_required_scoring_count:minutes")] == 1


def test_exclusion_empties_source_then_falls_through():
    result = select([row(50, minutes=60)], prior(
        personal_id=817, personal=[row(817, assists=None)],
        position=[row(818, minutes=60)],
    ))
    assert result.source == "current_season_position"
    assert reasons(result)[("previous_season_personal", "missing_required_scoring_count:assists")] == 1


def test_missing_optional_xg_ict_does_not_exclude_row():
    counts = COUNTS | {"minutes": 60, "expected_goals": None, "ict_index": None}
    result = select([Row(50, scoring_counts=counts)])
    assert result.source == "current_season_position"
    assert result.row_count == 1
    assert result.exclusions == ()


def test_same_name_different_external_codes_never_match():
    current = row(42, name="Same Name", code=100)
    other = row(817, name="Same Name", code=200, minutes=60)
    # The prior reader reports no validated personal match; names/codes on rows
    # are deliberately irrelevant to this selector.
    result = select([current], prior(reason="external_code_match_not_one_to_one",
                                     position=[other]))
    assert result.source == "previous_season_position"
    assert result.row_count == 1
    assert result.component_means["appearance"] == 2


def test_result_independent_of_input_row_order():
    current = [row(50, minutes=60, goals_scored=1), row(51),
               row(52, minutes=90, goals_scored=3)]
    previous = prior(position=[row(817, minutes=60), row(818)])
    assert select(current, previous) == select(reversed(current), previous)
    assert select([], previous) == select([], prior(position=reversed(previous.position_rows)))


def test_source_label_row_counts_and_all_component_means_are_exact():
    result = select([row(50, minutes=60, goals_scored=1, yellow_cards=1), row(51)])
    assert result.source == "current_season_position"
    assert result.row_count == 2
    assert result.personal_current_count == 0
    assert set(result.component_means) == set(ScoringComponents.__dataclass_fields__)
    assert result.component_means["appearance"] == 1
    assert result.component_means["goals"] == 3
    assert result.component_means["yellow_cards"] == -0.5


def test_personal_current_only_has_no_prior_and_reports_observations():
    result = select([row(42, minutes=60), row(42, minutes=0)])
    assert result.source == "personal_current_only"
    assert result.row_count == 0
    assert result.personal_current_count == 2
    assert result.component_means is None


def test_incomplete_personal_current_rows_do_not_create_fallback():
    result = select([row(42, minutes=None)])
    assert result.source == "unavailable"
    assert reasons(result)[("personal_current", "missing_required_scoring_count:minutes")] == 1


def test_prior_reader_exclusion_reasons_are_counted_once():
    result = select(previous=prior(
        personal_id=817, reason="prior_history_coverage_gap",
        exclusions=((817, "prior_history_coverage_gap"),
                    (818, "prior_history_coverage_gap")),
    ))
    assert reasons(result) == {("previous_season_reader", "prior_history_coverage_gap"): 2}
