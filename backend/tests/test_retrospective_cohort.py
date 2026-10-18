"""Synthetic §08A cutoff, cohort and outcome-quality acceptance cases."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fpl_optimizer.services.retrospective_cohort import (
    BenchmarkFixture, BenchmarkRow, cutoff_for_event, select_cohort,
)


KICKOFF = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
CUTOFF = KICKOFF - timedelta(minutes=90)


def fixture(
    fixture_id: int, event: int, kickoff: datetime | None,
    home: int = 10, away: int = 20,
) -> BenchmarkFixture:
    return BenchmarkFixture(fixture_id, event, kickoff, home, away)


def row(
    fixture_id: int, event: int, kickoff: datetime | None,
    *, player: int = 1, position: int = 2, was_home: bool = True,
    minutes: int = 90, goals: int = 0,
) -> BenchmarkRow:
    return BenchmarkRow(
        player_id=player, fixture_id=fixture_id, event=event,
        kickoff_time=kickoff, was_home=was_home, position=position,
        minutes=minutes, goals_scored=goals,
    )


def standard():
    old_time = KICKOFF - timedelta(days=7)
    return (
        [fixture(101, 1, old_time), fixture(201, 2, KICKOFF)],
        [row(101, 1, old_time), row(201, 2, KICKOFF, minutes=0)],
    )


def chosen(fixtures, rows, player_ids=(1,)):
    return select_cohort(2, fixtures, rows, player_ids)


def test_cutoff_is_earliest_kickoff_minus_90_minutes():
    fixtures = [fixture(202, 2, KICKOFF + timedelta(hours=4)),
                fixture(201, 2, KICKOFF), fixture(101, 1, KICKOFF - timedelta(days=7))]
    result = cutoff_for_event(2, fixtures)
    assert result.cutoff_time == CUTOFF
    assert not result.skipped


def test_event_without_any_kickoff_is_skipped_and_reported():
    result = chosen([fixture(201, 2, None)], [])
    assert result.cutoff.skipped
    assert result.cutoff.skip_reason == "no_target_kickoff"
    assert result.cutoff.missing_kickoff_fixture_ids == (201,)
    assert result.players == ()


def test_partly_missing_target_kickoffs_are_flagged():
    fixtures, rows = standard()
    fixtures.append(fixture(202, 2, None))
    result = chosen(fixtures, rows)
    assert result.cutoff.skipped
    assert result.cutoff.cutoff_time is None
    assert result.cutoff.skip_reason == "partial_target_kickoff"
    assert result.cutoff.missing_kickoff_fixture_ids == (202,)


def test_row_exactly_three_hours_before_cutoff_is_not_eligible():
    fixtures, rows = standard()
    boundary = CUTOFF - timedelta(hours=3)
    fixtures.append(fixture(102, 1, boundary))
    rows.append(row(102, 1, boundary, goals=3))
    result = chosen(fixtures, rows).players[0]
    assert tuple(item.fixture_id for item in result.eligible_rows) == (101,)


def test_row_just_under_three_hours_is_not_eligible_boundary_pair():
    fixtures, rows = standard()
    just_too_late = CUTOFF - timedelta(hours=3) + timedelta(microseconds=1)
    just_early_enough = CUTOFF - timedelta(hours=3) - timedelta(microseconds=1)
    fixtures.extend([fixture(102, 1, just_too_late), fixture(103, 1, just_early_enough)])
    rows.extend([row(102, 1, just_too_late), row(103, 1, just_early_enough)])
    assert tuple(item.fixture_id for item in chosen(fixtures, rows).players[0].eligible_rows) == (101, 103)


def test_target_event_rows_never_eligible():
    fixtures, rows = standard()
    rows[1] = replace(rows[1], kickoff_time=KICKOFF - timedelta(days=30),
                      position=4, minutes=90)
    result = chosen(fixtures, rows).players[0]
    assert tuple(item.fixture_id for item in result.eligible_rows) == (101,)
    assert result.position == 2


def test_position_and_club_come_from_latest_eligible_row():
    fixtures, rows = standard()
    later = KICKOFF - timedelta(days=2)
    fixtures.append(fixture(102, 1, later, home=30, away=40))
    fixtures.append(fixture(202, 2, KICKOFF + timedelta(hours=4), home=40, away=60))
    rows.extend([row(102, 1, later, position=3, was_home=False),
                 row(202, 2, KICKOFF + timedelta(hours=4), position=4)])
    result = chosen(fixtures, rows).players[0]
    assert result.position == 3
    assert result.last_eligible_club_id == 40
    assert result.scheduled_fixture_ids == (202,)
    # Equal kickoffs resolve by fixture ID, independent of input order.
    fixtures.extend([fixture(103, 1, later, home=50, away=60),
                     fixture(203, 2, KICKOFF + timedelta(hours=5), home=50, away=70)])
    rows.insert(0, row(103, 1, later, position=4))
    tied = chosen(fixtures, rows).players[0]
    assert tuple(item.fixture_id for item in tied.eligible_rows) == (101, 102, 103)
    assert tied.position == 4
    assert tied.last_eligible_club_id == 50
    assert tied.scheduled_fixture_ids == (203,)


def test_club_derived_from_was_home_and_fixture_sides():
    fixtures, rows = standard()
    fixtures.append(fixture(202, 2, KICKOFF + timedelta(hours=4), home=20, away=30))
    rows[0] = replace(rows[0], was_home=False)
    rows[1] = replace(rows[1], fixture_id=202)
    result = chosen(fixtures, rows).players[0]
    assert result.last_eligible_club_id == 20
    assert result.scheduled_fixture_ids == (201, 202)
    assert result.outcome_exclusion_reason == "missing_result_row"


def test_first_time_players_excluded_and_counted():
    fixtures, rows = standard()
    rows.append(row(201, 2, KICKOFF, player=2))
    result = chosen(fixtures, rows, (1, 2, 3))
    assert tuple(player.player_id for player in result.players) == (1,)
    assert result.first_time_player_ids == (2, 3)
    assert result.first_time_count == 2
    assert result.candidate_count == 1


def test_active_cohort_uses_last_up_to_four_rows():
    fixtures, rows = standard()
    rows[0] = replace(rows[0], minutes=20)
    prior = [KICKOFF - timedelta(days=35 - i * 7) for i in range(5)]
    fixtures.extend(fixture(i, 1, kickoff) for i, kickoff in enumerate(prior, 102))
    rows.extend(row(i, 1, kickoff, minutes=20 if i == 102 else 10)
                for i, kickoff in enumerate(prior, 102))
    # Last four: 10 + 10 + 10 + 20 = 50; the older rows do not help.
    result = chosen(fixtures, rows).players[0]
    assert len(result.eligible_rows) == 6
    assert not result.active
    assert chosen(fixtures, rows).active_count == 0
    # Fewer than four rows use all available rows.
    short = chosen(fixtures[:2], [replace(rows[0], minutes=60), rows[1]]).players[0]
    assert short.active


def test_genuine_blank_scores_zero():
    fixtures, rows = standard()
    rows = rows[:1]
    fixtures[1] = replace(fixtures[1], home_team_id=30, away_team_id=40)
    result = chosen(fixtures, rows).players[0]
    assert result.scheduled_fixture_ids == result.result_fixture_ids == ()
    assert result.outcome_points == 0
    assert result.outcome_exclusion_reason is None
    assert chosen(fixtures, rows).accepted_outcome_count == 1


def test_missing_result_row_for_scheduled_fixture_excluded_with_reason():
    fixtures, rows = standard()
    rows = rows[:1]
    result = chosen(fixtures, rows)
    assert result.players[0].outcome_points is None
    assert result.players[0].outcome_exclusion_reason == "missing_result_row"
    assert result.outcome_exclusion_counts == {"missing_result_row": 1}


def test_different_fixture_set_excluded_with_reason():
    fixtures, rows = standard()
    fixtures.append(fixture(202, 2, KICKOFF + timedelta(hours=4), home=30, away=40))
    rows[1] = replace(rows[1], fixture_id=202)
    result = chosen(fixtures, rows)
    assert result.players[0].scheduled_fixture_ids == (201,)
    assert result.players[0].result_fixture_ids == (202,)
    assert result.players[0].outcome_exclusion_reason == "different_fixture_set"
    assert result.outcome_exclusion_counts == {"different_fixture_set": 1}


def test_double_gameweek_outcome_is_sum():
    fixtures, rows = standard()
    fixtures.append(fixture(202, 2, KICKOFF + timedelta(days=2), home=10, away=30))
    rows[1] = replace(rows[1], minutes=90, goals_scored=1)
    rows.append(row(202, 2, KICKOFF + timedelta(days=2), minutes=90, goals=0))
    result = chosen(fixtures, rows).players[0]
    assert result.scheduled_fixture_ids == (201, 202)
    assert result.outcome_points == 6 + 2 + 2  # Defender goal + two appearances.


def test_perturbing_values_of_invisible_rows_changes_nothing():
    fixtures, rows = standard()
    late = CUTOFF - timedelta(hours=2)
    fixtures.append(fixture(102, 1, late, home=30, away=40))
    rows.extend([row(102, 1, late, position=4, minutes=90),
                 row(103, 1, None, position=4, minutes=90)])
    original = chosen(fixtures, rows)
    mutated = [replace(item, position=1, minutes=0, was_home=not item.was_home,
                       goals_scored=10, bonus=3)
               if item.fixture_id in {102, 103, 201} else item for item in rows]
    changed = chosen(fixtures, mutated)
    def decisions(selection):
        player = selection.players[0]
        return (tuple(item.fixture_id for item in player.eligible_rows),
                player.position, player.last_eligible_club_id, player.active,
                player.scheduled_fixture_ids, player.result_fixture_ids,
                player.outcome_exclusion_reason, selection.first_time_player_ids)
    assert decisions(original) == decisions(changed)


def test_adding_or_removing_invisible_rows_changes_nothing():
    fixtures, rows = standard()
    late = CUTOFF - timedelta(hours=2)
    fixtures.append(fixture(102, 1, late))
    invisible = [row(102, 1, late), row(103, 1, None),
                 row(102, 1, late, player=2)]
    assert chosen(fixtures, rows, (1, 2)) == chosen(fixtures, rows + invisible, (1, 2))
