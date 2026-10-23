"""Synthetic J46 research-bundle boundaries; no database or network."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from fpl_optimizer.services.forecast_bundle_policy import (
    FixtureInput,
    GameweekInput,
    eligible_current_rows,
    label_player_history,
    select_target_event,
)
from fpl_optimizer.services.history_eligibility import (
    HistoryEligibilityView,
    PlayerHistoryEvidence,
    RequiredFixture,
)
from fpl_optimizer.services.shared_freshness import check_shared_freshness


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Row:
    fixture_id: int
    minutes: int


def gw(event=8, deadline=None, checked=True):
    return GameweekInput(event, deadline, checked)


def fixture(fixture_id=101, event=8, kickoff=None, finished=True, provisional=False):
    return FixtureInput(
        fixture_id, event, NOW - timedelta(days=1) if kickoff is None else kickoff,
        finished, provisional,
    )


def eligible(rows, fixtures, gameweeks=(gw(),), target=9, as_of=NOW):
    return eligible_current_rows(
        tuple(rows), tuple(fixtures), tuple(gameweeks),
        target_event_id=target, as_of=as_of,
    ).rows


def history(age=timedelta(hours=1), **changes):
    player = PlayerHistoryEvidence(
        player_id=1,
        source_observed_at=NOW - age,
        last_success_at=NOW - age + timedelta(minutes=1),
        publication_version=3,
        covered_fixture_ids=frozenset({101}),
        covered_finalized_fixture_ids=frozenset({101}),
    )
    player = replace(player, **changes)
    shared = check_shared_freshness(
        source_observed_at=NOW - timedelta(hours=1),
        last_success_at=NOW - timedelta(minutes=59),
        now=NOW,
        next_deadline=NOW + timedelta(days=1),
    )
    return HistoryEligibilityView(shared, (player,), 7, 9), player


def label(age=timedelta(hours=1), **changes):
    view, player = history(age, **changes)
    return label_player_history(view, player, target_event_id=9, as_of=NOW)


def test_target_event_is_earliest_future_deadline():
    result = select_target_event((gw(10, NOW + timedelta(days=2)),
                                  gw(9, NOW + timedelta(days=1)),
                                  gw(8, NOW + timedelta(days=1))), as_of=NOW)
    assert (result.id, result.deadline_time) == (8, NOW + timedelta(days=1))


def test_deadline_equal_to_as_of_is_not_future():
    result = select_target_event((gw(8, NOW), gw(9, NOW + timedelta(hours=1))), as_of=NOW)
    assert result is not None and result.id == 9


def test_no_future_deadline_returns_none():
    assert select_target_event((gw(8, NOW - timedelta(seconds=1)), gw(9)), as_of=NOW) is None


def test_naive_datetimes_raise():
    with pytest.raises(ValueError, match="as_of must include a UTC offset"):
        select_target_event((gw(9, NOW + timedelta(days=1)),), as_of=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="deadline_time must include a UTC offset"):
        select_target_event((gw(9, (NOW + timedelta(days=1)).replace(tzinfo=None)),), as_of=NOW)
    with pytest.raises(ValueError, match="as_of must include a UTC offset"):
        eligible((Row(101, 0),), (fixture(),), as_of=NOW.replace(tzinfo=None))


def test_target_and_later_event_rows_never_enter_inputs():
    rows = (Row(101, 90), Row(102, 90), Row(103, 90))
    fixtures = (fixture(101, 8), fixture(102, 9), fixture(103, 10))
    assert eligible(rows, fixtures, (gw(8), gw(9), gw(10))) == (Row(101, 90),)


def test_rescheduled_fixture_moved_into_target_event_excluded():
    assert eligible((Row(102, 90),), (fixture(102, 9),), (gw(8), gw(9))) == ()


def test_unfinished_or_unchecked_or_provisional_rows_excluded():
    rows = tuple(Row(i, 90) for i in (101, 102, 103, 104))
    fixtures = (fixture(101), fixture(102, finished=False),
                fixture(103, provisional=True), fixture(104, event=7))
    assert eligible(rows, fixtures, (gw(8), gw(7, checked=False))) == (Row(101, 90),)


def test_zero_minute_rows_retained_and_order_is_kickoff_then_fixture_id():
    rows = (Row(103, 90), Row(102, 0), Row(101, 15))
    fixtures = (fixture(103), fixture(102), fixture(101, kickoff=NOW - timedelta(days=2)))
    assert eligible(rows, fixtures) == (Row(101, 15), Row(102, 0), Row(103, 90))


def test_kickoff_equal_to_as_of_excluded_strictly_before_required():
    assert eligible((Row(101, 90),), (fixture(kickoff=NOW),)) == ()


def test_label_never_claims_coverage_verified():
    required = (RequiredFixture(102, 8, NOW - timedelta(hours=3)),)
    result = label(required_fixtures=required)
    assert (result.coverage_status, result.action) == ("unverified_shadow_only", "use")


def test_stale_player_is_excluded_with_visible_reason():
    result = label(age=timedelta(hours=48, seconds=1))
    assert (result.history_age_status, result.action) == ("stale", "exclude")
    assert result.reason == "player history older than 48 hours"


def test_unknown_player_is_excluded_with_visible_reason():
    result = label(source_observed_at=None, last_success_at=None)
    assert (result.history_age_status, result.action) == ("unknown", "exclude")
    assert result.reason == "no validated player history publication"


def test_current_player_is_used_with_unverified_coverage_label():
    result = label(age=timedelta(hours=48))
    assert (result.history_age_status, result.action, result.coverage_status) == (
        "current", "use", "unverified_shadow_only"
    )


def test_inputs_not_mutated_and_output_deterministic():
    rows = (Row(102, 0), Row(101, 90))
    fixtures = (fixture(102), fixture(101))
    gameweeks = (gw(),)
    original = (rows, fixtures, gameweeks)
    first = eligible_current_rows(rows, fixtures, gameweeks, target_event_id=9, as_of=NOW)
    assert first == eligible_current_rows(rows, fixtures, gameweeks, target_event_id=9, as_of=NOW)
    assert (rows, fixtures, gameweeks) == original
    view, player = history()
    assert label_player_history(view, player, target_event_id=9, as_of=NOW) == (
        label_player_history(view, player, target_event_id=9, as_of=NOW)
    )
    assert view.players[0].required_fixtures == ()
