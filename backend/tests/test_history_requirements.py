"""Synthetic interim fixture derivation; no database or network."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fpl_optimizer.services.history_eligibility import (
    HistoryEligibilityView, PlayerHistoryEvidence, RequiredFixture,
    decide_history_eligibility,
)
from fpl_optimizer.services.history_requirements import (
    PublishedHistoryRow, SeasonFixture, derive_history_requirements,
)
from fpl_optimizer.services.shared_freshness import check_shared_freshness


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
ANCHOR_TIME = NOW - timedelta(days=2)
OBSERVED = NOW - timedelta(hours=3)


def row(fixture_id=100, *, kickoff=ANCHOR_TIME, club=1, minutes=90):
    return PublishedHistoryRow(fixture_id, kickoff, club, minutes)


def fixture(fixture_id=200, *, club=1, opponent=2, gameweek=8,
            kickoff=ANCHOR_TIME + timedelta(days=1), finished=True,
            observed=OBSERVED, checked=True):
    return SeasonFixture(fixture_id, club, opponent, gameweek, kickoff,
                         finished, observed, checked)


def derive(*, rows=None, fixtures=None, catalog_team_id=1, now=NOW):
    return derive_history_requirements(
        player_id=5, catalog_team_id=catalog_team_id,
        published_rows=(row(),) if rows is None else rows,
        season_fixtures=(fixture(),) if fixtures is None else fixtures,
        now=now,
    )


def ids(result):
    return tuple(item.fixture_id for item in result.required_fixtures)


def test_no_rows_is_unresolved_not_required():
    result = derive(rows=())
    assert (result.status, result.reason, result.anchor_fixture_id) == (
        "unresolved", "no_rows", None,
    )
    assert result.required_fixtures == result.within_grace == ()


def test_catalog_club_differs_from_last_row_club_is_unresolved_club_change():
    rows = (row(99, kickoff=ANCHOR_TIME - timedelta(days=1)), row(club=2))
    result = derive(rows=rows)
    assert (result.status, result.reason, result.anchor_fixture_id) == (
        "unresolved", "club_change", 100,
    )
    assert result.required_fixtures == result.within_grace == ()


def test_required_includes_finalized_club_fixtures_after_last_row_past_grace():
    result = derive()
    assert (result.status, result.reason, result.anchor_fixture_id) == (
        "provisional", "anchor_club_matches_catalog", 100,
    )
    assert [(item.fixture_id, item.gameweek_id, item.first_data_checked_at)
            for item in result.required_fixtures] == [(200, 8, OBSERVED)]
    assert result.within_grace == ()


def test_grace_boundary_exactly_two_hours_is_required_one_second_before_is_not():
    at = derive(now=OBSERVED + timedelta(hours=2))
    before = derive(now=OBSERVED + timedelta(hours=2, seconds=-1))
    assert ids(at) == (200,)
    assert ids(before) == ()
    assert [(item.fixture_id, item.due_at) for item in before.within_grace] == [
        (200, OBSERVED + timedelta(hours=2)),
    ]


def test_fixture_without_observation_or_unchecked_gameweek_not_required():
    fixtures = (fixture(200, observed=None), fixture(201, checked=False))
    result = derive(fixtures=fixtures)
    assert result.required_fixtures == result.within_grace == ()


def test_unfinished_fixture_not_required():
    result = derive(fixtures=(fixture(finished=False),))
    assert result.required_fixtures == result.within_grace == ()


def test_double_gameweek_requires_both_fixtures():
    result = derive(fixtures=(fixture(201, kickoff=ANCHOR_TIME + timedelta(days=1, hours=2)),
                              fixture(200)))
    assert ids(result) == (200, 201)
    assert [item.gameweek_id for item in result.required_fixtures] == [8, 8]


def test_blank_gameweek_requires_nothing():
    result = derive(fixtures=())
    assert result.status == "provisional"
    assert result.required_fixtures == result.within_grace == ()


def test_postponed_fixture_without_gameweek_not_required():
    result = derive(fixtures=(fixture(gameweek=None),))
    assert result.required_fixtures == result.within_grace == ()


def test_fixtures_before_last_row_never_required():
    earlier = fixture(kickoff=ANCHOR_TIME - timedelta(hours=1))
    same_kickoff_lower_id = fixture(99, kickoff=ANCHOR_TIME)
    result = derive(fixtures=(earlier, same_kickoff_lower_id))
    assert result.required_fixtures == result.within_grace == ()


def test_anchor_fixture_itself_is_never_required():
    result = derive(fixtures=(fixture(100, kickoff=ANCHOR_TIME),))
    assert result.anchor_fixture_id == 100
    assert result.required_fixtures == result.within_grace == ()


def test_other_clubs_fixtures_never_required():
    result = derive(fixtures=(fixture(club=2, opponent=3),))
    assert result.required_fixtures == result.within_grace == ()


def test_last_row_ordering_uses_kickoff_then_fixture_id():
    rows = (row(101, club=2), row(100, club=1),
            row(99, kickoff=ANCHOR_TIME - timedelta(hours=1), club=1))
    result = derive(rows=rows)
    assert (result.status, result.reason, result.anchor_fixture_id) == (
        "unresolved", "club_change", 101,
    )


def test_rescheduled_fixture_new_observation_time_is_used():
    new_observation = NOW - timedelta(minutes=30)
    result = derive(fixtures=(fixture(observed=new_observation),))
    assert result.required_fixtures == ()
    assert [(item.fixture_id, item.first_data_checked_at, item.due_at)
            for item in result.within_grace] == [
        (200, new_observation, new_observation + timedelta(hours=2)),
    ]


def test_zero_minute_last_row_still_anchors_club():
    result = derive(rows=(row(minutes=0),))
    assert (result.status, result.anchor_fixture_id, ids(result)) == (
        "provisional", 100, (200,),
    )


def test_result_is_deterministic_sorted_and_inputs_not_mutated():
    rows = (row(98, kickoff=ANCHOR_TIME - timedelta(days=1)), row())
    fixtures = (fixture(202), fixture(200), fixture(201))
    original_rows, original_fixtures = rows[:], fixtures[:]
    first = derive(rows=rows, fixtures=fixtures)
    second = derive(rows=tuple(reversed(rows)), fixtures=tuple(reversed(fixtures)))
    assert first == second
    assert ids(first) == (200, 201, 202)
    assert rows == original_rows and fixtures == original_fixtures


def test_output_maps_to_j42_gate_required_fixture_input():
    derived = derive()
    required = tuple(RequiredFixture(
        item.fixture_id, item.gameweek_id, item.first_data_checked_at,
    ) for item in derived.required_fixtures)
    assert required == (RequiredFixture(200, 8, OBSERVED),)
    shared = check_shared_freshness(
        source_observed_at=NOW - timedelta(hours=1),
        last_success_at=NOW - timedelta(minutes=59),
        now=NOW, next_deadline=NOW + timedelta(days=1),
    )
    checkpoint = PlayerHistoryEvidence(
        player_id=5, source_observed_at=NOW - timedelta(hours=1),
        last_success_at=NOW - timedelta(minutes=59), publication_version=3,
        covered_fixture_ids=frozenset({100}),
        covered_finalized_fixture_ids=frozenset({100}),
        required_fixtures=required,
    )

    def decision(evidence):
        return decide_history_eligibility(
            HistoryEligibilityView(shared, (evidence,), 3, 2), now=NOW,
            mode="transfers", owned_player_ids=frozenset(),
            locked_player_ids=frozenset(), target_event=9,
        ).candidates[0]

    missing = decision(checkpoint)
    present = decision(replace(
        checkpoint, covered_fixture_ids=frozenset({100, 200}),
        covered_finalized_fixture_ids=frozenset({100, 200}),
    ))
    assert (missing.status, missing.action) == ("stale", "exclude")
    assert "200" in missing.reason
    assert (present.status, present.action) == ("current", "use")
