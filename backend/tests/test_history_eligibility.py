"""Synthetic §05B solve admission boundaries; no database or network."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fpl_optimizer.services.history_eligibility import (
    HistoryEligibilityView, PlayerHistoryEvidence, RequiredFixture,
    decide_history_eligibility,
)
from fpl_optimizer.services.shared_freshness import check_shared_freshness


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(days=3)


def player(player_id=1, age=timedelta(hours=1), **changes):
    base = PlayerHistoryEvidence(
        player_id=player_id,
        source_observed_at=NOW - age,
        last_success_at=NOW - age + timedelta(minutes=1),
        publication_version=3,
        covered_fixture_ids=frozenset({101}),
        covered_finalized_fixture_ids=frozenset({101}),
    )
    return replace(base, **changes)


def view(*players, shared_age=timedelta(hours=1), shared_deadline=DEADLINE):
    shared = check_shared_freshness(
        source_observed_at=NOW - shared_age,
        last_success_at=NOW - shared_age + timedelta(minutes=1),
        now=NOW, next_deadline=shared_deadline,
    )
    return HistoryEligibilityView(shared, tuple(players), 7, 9)


def decide(*players, mode="transfers", owned=frozenset(), locked=frozenset(),
           shared_age=timedelta(hours=1), shared_deadline=DEADLINE):
    return decide_history_eligibility(
        view(*players, shared_age=shared_age, shared_deadline=shared_deadline),
        now=NOW, mode=mode, owned_player_ids=owned,
        locked_player_ids=locked, target_event=9,
    )


def fixture(at, fixture_id=102, event=8):
    return RequiredFixture(fixture_id, event, at)


def test_history_exactly_at_48h_is_current_and_one_second_over_is_stale():
    at = decide(player(age=timedelta(hours=48))).candidates[0]
    over = decide(player(age=timedelta(hours=48, seconds=1))).candidates[0]
    assert (at.status, at.action) == ("current", "use")
    assert (over.status, over.action) == ("stale", "exclude")


def test_lineup_allows_history_up_to_96h_with_warning_and_blocks_beyond():
    at = decide(player(age=timedelta(hours=96)), mode="lineup", owned=frozenset({1})).candidates[0]
    over = decide(player(age=timedelta(hours=96, seconds=1)), mode="lineup", owned=frozenset({1})).candidates[0]
    assert (at.status, at.warning, at.action) == ("stale", True, "use")
    assert (over.status, over.action) == ("stale", "BLOCK")


def test_lineup_shared_data_allows_24h_not_beyond():
    at = decide(player(), mode="lineup", shared_age=timedelta(hours=24))
    over = decide(player(), mode="lineup", shared_age=timedelta(hours=24, seconds=1))
    assert (at.warning, at.blocked, at.candidates[0].action) == (True, False, "use")
    assert (over.blocked, over.candidates[0].action) == (True, "BLOCK")


def test_finalized_coverage_not_required_before_two_hour_grace():
    p = player(required_fixtures=(fixture(NOW - timedelta(hours=2) + timedelta(seconds=1)),))
    result = decide(p).candidates[0]
    assert (result.status, result.warning, result.action) == ("current", True, "use")


def test_finalized_coverage_required_exactly_at_grace_boundary():
    p = player(required_fixtures=(fixture(NOW - timedelta(hours=2)),))
    result = decide(p).candidates[0]
    assert (result.status, result.action) == ("stale", "exclude")
    assert "102" in result.reason


def test_grace_period_uses_previous_history_with_warning():
    p = player(required_fixtures=(fixture(NOW - timedelta(minutes=30)),))
    result = decide(p).candidates[0]
    assert result.usable_finalized_fixture_ids == (101,)
    assert result.warning is True and "previous finalized" in result.reason


def test_provisional_rows_are_never_used():
    p = player(covered_fixture_ids=frozenset({101, 102}),
               covered_finalized_fixture_ids=frozenset({101}),
               required_fixtures=(fixture(NOW - timedelta(minutes=30)),))
    result = decide(p).candidates[0]
    assert 102 not in result.usable_finalized_fixture_ids
    assert result.usable_finalized_fixture_ids == (101,)


def test_owned_player_stale_blocks_the_solve():
    result = decide(player(age=timedelta(hours=48, seconds=1)), owned=frozenset({1}))
    assert result.blocked and result.candidates[0].action == "BLOCK"


def test_locked_player_stale_blocks_the_solve():
    result = decide(player(age=timedelta(hours=48, seconds=1)), locked=frozenset({1}))
    assert result.blocked and result.candidates[0].action == "BLOCK"


def test_unowned_unlocked_stale_player_is_excluded_with_reason():
    result = decide(player(age=timedelta(hours=48, seconds=1))).candidates[0]
    assert result.action == "exclude" and "older than 48 hours" in result.reason


def test_one_failing_player_does_not_affect_other_players():
    result = decide(player(1), player(2, age=timedelta(hours=49)))
    assert [(p.player_id, p.action) for p in result.candidates] == [(1, "use"), (2, "exclude")]
    assert result.blocked is False


def test_missing_history_is_unknown_not_zero():
    p = player(source_observed_at=None, last_success_at=None)
    result = decide(p).candidates[0]
    assert (result.status, result.action, result.usable_finalized_fixture_ids,
            result.may_use_prior) == ("unknown", "exclude", (), False)


def test_empty_validated_history_new_player_may_use_prior():
    p = player(covered_fixture_ids=frozenset(),
               covered_finalized_fixture_ids=frozenset(), validated_empty_new_player=True)
    result = decide(p).candidates[0]
    assert (result.status, result.action, result.may_use_prior) == ("current", "use", True)


def test_failed_attempt_does_not_reset_success_age():
    p = player(age=timedelta(hours=49), last_attempt_at=NOW)
    assert decide(p).candidates[0].status == "stale"


def test_whole_sweep_timestamp_is_not_used_as_gate():
    p = player(age=timedelta(hours=49), completed_sweep_at=NOW)
    assert decide(p).candidates[0].action == "exclude"


def test_exclusions_report_player_and_reason_for_response():
    result = decide(player(1), player(2, age=timedelta(hours=49)))
    assert result.exclusions == ((2, "player history older than 48 hours"),)


def test_decisions_are_deterministic_for_same_inputs():
    input_view = view(player(2), player(1))
    kwargs = dict(now=NOW, mode="simulator", owned_player_ids=frozenset(),
                  locked_player_ids=frozenset(), target_event=9)
    first = decide_history_eligibility(input_view, **kwargs)
    assert first == decide_history_eligibility(input_view, **kwargs)
    assert [p.player_id for p in first.candidates] == [1, 2]
