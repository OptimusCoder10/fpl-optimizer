"""Additive §04D prior means for the minute distribution."""

from dataclasses import dataclass

from fpl_optimizer.services.prior_archive import PriorInputs
from fpl_optimizer.services.prior_selection import select_prior


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
    scoring_counts: dict[str, int | None]


def row(player_id: int, minutes: int | None, **changes: int | None) -> Row:
    return Row(player_id, 2, COUNTS | {"minutes": minutes} | changes)


def previous(*, personal_id=None, personal=(), position=()):
    return PriorInputs("2025-26", personal_id, None, tuple(personal),
                       tuple(position), (), None, None)


def select(current=(), archive=None):
    return select_prior(player_id=42, position=2, current_rows=current,
                        previous=archive or previous())


def test_prior_four_means_are_exact_on_small_known_source():
    result = select([row(50, 0), row(51, 30), row(52, 90)])
    assert (result.p_zero_minutes, result.p_under_60,
            result.p_60_plus, result.expected_minutes) == (1 / 3, 1 / 3, 1 / 3, 40)


def test_prior_three_probabilities_sum_to_one():
    result = select([row(50, 0), row(51, 59), row(52, 60)])
    assert sum((result.p_zero_minutes, result.p_under_60, result.p_60_plus)) == 1


def test_prior_zero_minute_rows_count_toward_p_zero():
    result = select([row(50, 0), row(51, 0), row(52, 90)])
    assert result.row_count == 3
    assert result.p_zero_minutes == 2 / 3
    assert result.expected_minutes == 30


def test_prior_excluded_row_does_not_affect_minute_means():
    result = select([row(50, 0), row(51, None, goals_scored=99), row(52, 60)])
    assert result.row_count == 2
    assert result.p_zero_minutes == result.p_60_plus == 0.5
    assert result.expected_minutes == 30
    assert result.component_means["goals"] == 0
    assert result.exclusions[0].reason == "missing_required_scoring_count:minutes"


def test_prior_unavailable_source_gives_none_for_all_four():
    result = select()
    assert result.source == "unavailable"
    assert (result.p_zero_minutes, result.p_under_60,
            result.p_60_plus, result.expected_minutes) == (None, None, None, None)


def test_prior_same_selected_source_feeds_points_and_minutes():
    result = select(
        [row(50, 30, goals_scored=8)],
        previous(personal_id=817, personal=[row(817, 0), row(817, 60, goals_scored=1)],
                 position=[row(818, 90, goals_scored=9)]),
    )
    assert result.source == "previous_season_personal"
    assert result.row_count == 2
    assert result.component_means["goals"] == 3
    assert result.p_zero_minutes == result.p_60_plus == 0.5
    assert result.p_under_60 == 0
    assert result.expected_minutes == 30
