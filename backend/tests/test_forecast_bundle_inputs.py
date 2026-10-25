"""Synthetic J47 assembly proofs; no database, archive or prediction calls."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fpl_optimizer.services import forecast_bundle_inputs as assembly
from fpl_optimizer.services.forecast_bundle_policy import GameweekInput, PlayerHistoryLabel, TargetEvent
from fpl_optimizer.services.forecast_bundle_view import (
    BundleFixture, BundleView, CatalogPlayer, ClubTargetFixtures, CurrentHistoryRow, SharedPublication,
)
from fpl_optimizer.services.prior_selection import REQUIRED_COUNTS, select_prior
from fpl_optimizer.services.retrospective_benchmark import position_mean_baseline


AS_OF = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
USE = PlayerHistoryLabel("current", "use", "validated history current")
EMPTY_PREVIOUS = SimpleNamespace(
    personal_player_id=None, personal_reason=None, personal_rows=(), position_rows=(), exclusions=(),
)


def row(player_id, fixture_id, *, minutes=0, goals=0):
    return CurrentHistoryRow(**{
        **{name: 0 for name in REQUIRED_COUNTS},
        "player_id": player_id, "fixture_id": fixture_id, "minutes": minutes, "goals_scored": goals,
    })


def player(player_id, position, team_id):
    return CatalogPlayer(player_id, position, team_id, "a", None, True, True, False,
                         1000 + player_id, False, f"p{player_id}")


def sample():
    players = (player(1, 2, 1), player(2, 2, 2), player(3, 2, 1),
               player(4, 2, 2), player(5, 3, 1), player(6, 4, 3))
    fixtures = tuple(BundleFixture(fid, gw, AS_OF + timedelta(hours=hours), finished, provisional, 1, 2)
                     for fid, gw, hours, finished, provisional in (
                         (1, 1, -72, True, False), (2, 1, -48, True, False),
                         (3, 2, -24, True, False), (4, 1, 1, True, False),
                         (20, 4, -24, True, False), (21, 4, 48, False, False),
                         (6, 5, -24, True, False), (7, 1, -24, False, False),
                         (8, 1, -24, True, True), (9, 3, -24, True, False),
                     )) + (BundleFixture(10, None, None, True, False, 1, 2),)
    history = (row(1, 1, minutes=90, goals=10), row(2, 2), row(2, 3, minutes=60, goals=2),
               row(3, 1, goals=1000), row(4, 2, goals=2000), row(5, 2, minutes=90, goals=5))
    history += tuple(row(pid, fid, minutes=90, goals=100 + fid)
                     for pid in (1, 2) for fid in (4, 20, 6, 7, 8, 9, 10))
    target = TargetEvent(4, AS_OF + timedelta(hours=5))
    view = BundleView(
        "2026-27", AS_OF, "synthetic-rules", SharedPublication(AS_OF, AS_OF, 1), None, 1,
        tuple(GameweekInput(i, target.deadline_time if i == 4 else AS_OF - timedelta(days=1), i != 3)
              for i in range(1, 6)),
        fixtures, target,
        (ClubTargetFixtures(1, (20, 21)), ClubTargetFixtures(2, (20,)), ClubTargetFixtures(3, ())),
        players, (), history,
    )
    labels = {p.id: USE for p in players}
    labels[3] = PlayerHistoryLabel("stale", "exclude", "player history older than 48 hours")
    labels[4] = PlayerHistoryLabel("unknown", "exclude", "no validated player history publication")
    return view, labels


def by_id(records):
    return {record.player_id: record for record in records}


def capture_selection(monkeypatch):
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        return select_prior(**kwargs)

    monkeypatch.setattr(assembly, "select_prior", capture)
    return calls


def test_pool_contains_only_policy_eligible_rows_of_use_players(monkeypatch):
    view, labels = sample()
    calls = capture_selection(monkeypatch)
    records = by_id(assembly.assemble_player_inputs(view, labels))
    call = next(call for call in calls if call["player_id"] == 1)
    assert [(r.player_id, r.position, r.scoring_counts["goals_scored"], r.scoring_counts["minutes"])
            for r in call["current_rows"]] == [(1, 2, 10, 90), (2, 2, 0, 0), (2, 2, 2, 60)]
    assert records[1].prior.row_count == 2
    assert records[1].prior.p_zero_minutes == 0.5
    # The same grouped tuple is reused for both players of this position.
    assert call["current_rows"] is next(c for c in calls if c["player_id"] == 2)["current_rows"]
    assert all(r.scoring_counts["goals_scored"] < 100 for c in calls for r in c["current_rows"])


def test_stale_and_unknown_players_rows_never_enter_any_pool(monkeypatch):
    view, labels = sample()
    calls = capture_selection(monkeypatch)
    records = by_id(assembly.assemble_player_inputs(view, labels))
    assert {r.player_id for call in calls for r in call["current_rows"]}.isdisjoint({3, 4}), (
        "stale and unknown players must contribute no pool rows"
    )
    for pid in (3, 4):
        assert records[pid].personal_rows == () and records[pid].prior is None
        assert records[pid].exclusion_reasons == (labels[pid].reason,)
    altered = replace(view, history_rows=tuple(
        replace(r, goals_scored=999999) if r.player_id in (3, 4) else r for r in view.history_rows
    ))
    assert assembly.assemble_player_inputs(altered, labels) == tuple(records.values())
    with pytest.raises(ValueError, match="use requires a current"):
        assembly.assemble_player_inputs(view, labels | {3: replace(labels[3], action="use")})


def test_target_event_and_later_rows_never_enter_pool_or_personal_rows(monkeypatch):
    view, labels = sample()
    calls = capture_selection(monkeypatch)
    records = assembly.assemble_player_inputs(view, labels)
    assert {r.fixture_id for record in records for r in record.personal_rows} == {1, 2, 3}
    assert all(r.scoring_counts["goals_scored"] < 100 for call in calls for r in call["current_rows"])
    # Target/later event rows already have pre-capture kickoffs and finished flags.
    for fid in (20, 6):
        fixture = next(f for f in view.fixtures if f.id == fid)
        assert fixture.kickoff_time < AS_OF and fixture.finished
    removed = replace(view, history_rows=tuple(r for r in view.history_rows if r.fixture_id not in (20, 6)))
    assert assembly.assemble_player_inputs(removed, labels) == records


def test_pools_are_grouped_by_catalog_position(monkeypatch):
    view, labels = sample()
    calls = capture_selection(monkeypatch)
    records = by_id(assembly.assemble_player_inputs(view, labels))
    assert records[2].prior.source == "current_season_position", "same-position players across clubs must share a pool"
    assert records[2].prior.component_means["goals"] == 60
    assert records[5].prior.source == "personal_current_only"
    positions = {p.id: p.position for p in view.players}
    assert all(r.position == positions[r.player_id] == call["position"]
               for call in calls for r in call["current_rows"])
    reclassified = replace(view, players=tuple(replace(p, position=3) if p.id == 2 else p for p in view.players))
    result = by_id(assembly.assemble_player_inputs(reclassified, labels))
    assert result[1].prior.source == "personal_current_only"
    assert result[2].prior.component_means["goals"] == 25
    assert result[5].prior.row_count == 2


def test_own_rows_handling_matches_benchmark_definition():
    view, labels = sample()
    record = by_id(assembly.assemble_player_inputs(view, labels))[1]
    assert record.prior.row_count == 2, "own rows must be excluded from the position prior"
    assert record.prior.personal_current_count == 1
    assert record.prior.component_means["goals"] == 6
    benchmark_rows = tuple(SimpleNamespace(**asdict(r), position=2)
                           for r in view.history_rows if r.player_id in (1, 2) and r.fixture_id in (1, 2, 3))
    benchmark_player = SimpleNamespace(player_id=1, position=2, scheduled_fixture_ids=record.target_fixture_ids)
    assert sum(record.prior.component_means.values()) * len(record.target_fixture_ids) == position_mean_baseline(
        benchmark_player, benchmark_rows,
    )
    changed = replace(view, history_rows=tuple(
        replace(r, goals_scored=100000) if r.player_id == 1 else r for r in view.history_rows
    ))
    assert by_id(assembly.assemble_player_inputs(changed, labels))[1].prior == record.prior


def test_assembled_prior_equals_direct_select_prior_call_on_small_case():
    view, labels = sample()
    records = by_id(assembly.assemble_player_inputs(view, labels))
    positions = {p.id: p.position for p in view.players}
    # Independent small oracle: explicit accepted fixture IDs, all positions.
    direct_rows = tuple(SimpleNamespace(
        player_id=r.player_id, position=positions[r.player_id],
        scoring_counts={name: getattr(r, name) for name in REQUIRED_COUNTS},
    ) for r in view.history_rows if r.player_id in (1, 2, 5) and r.fixture_id in (1, 2, 3))
    for pid in (1, 2, 5, 6):
        assert records[pid].prior == select_prior(
            player_id=pid, position=positions[pid], current_rows=direct_rows, previous=EMPTY_PREVIOUS,
        )


def test_previous_season_is_recorded_not_loaded_and_never_used(monkeypatch):
    view, labels = sample()
    calls = capture_selection(monkeypatch)
    records = assembly.assemble_player_inputs(view, labels)
    assert all(record.previous_season_prior == "not_loaded" for record in records), (
        "every record must declare previous_season_prior=not_loaded"
    )
    assert calls
    for call in calls:
        previous = call["previous"]
        assert previous.personal_player_id is None and previous.personal_reason is None
        assert previous.personal_rows == previous.position_rows == previous.exclusions == ()
    assert all(record.prior is None or not record.prior.source.startswith("previous_season") for record in records)
    # There is no previous-season input path, including an optional override.
    with pytest.raises(TypeError, match="unexpected keyword argument 'previous'"):
        assembly.assemble_player_inputs(view, labels, previous=SimpleNamespace(personal_rows=(object(),)))
    recoded = replace(view, players=tuple(replace(p, external_code=77, opta_code="same") for p in view.players))
    assert assembly.assemble_player_inputs(recoded, labels) == records


def test_player_without_rows_or_pool_is_unavailable_not_zero():
    view, labels = sample()
    record = by_id(assembly.assemble_player_inputs(view, labels))[6]
    assert record.personal_rows == ()
    assert record.prior.source == "unavailable"
    assert record.prior.row_count == record.prior.personal_current_count == 0
    assert record.prior.component_means is None and record.prior.expected_minutes is None
    assert record.prior.p_zero_minutes is None
    personal_only = by_id(assembly.assemble_player_inputs(view, labels))[5]
    assert personal_only.prior.source == "personal_current_only"
    assert personal_only.prior.component_means is None and personal_only.prior.personal_current_count == 1


def test_blank_player_has_no_target_fixtures_and_double_has_two():
    view, labels = sample()
    records = by_id(assembly.assemble_player_inputs(view, labels))
    assert records[6].target_fixture_ids == ()
    assert records[1].target_fixture_ids == (20, 21)
    assert records[2].target_fixture_ids == (20,)
    # The mapping supplied by B1 is authoritative; assembly does not recount fixtures.
    altered = replace(view, target_fixtures_by_club=(
        ClubTargetFixtures(1, (21,)), ClubTargetFixtures(2, ()), ClubTargetFixtures(3, ()),
    ))
    assert by_id(assembly.assemble_player_inputs(altered, labels))[1].target_fixture_ids == (21,)
    assert assembly.assemble_player_inputs(replace(view, target=None), labels) == ()


def test_assembly_is_deterministic_and_does_not_mutate_inputs():
    view, labels = sample()
    before = deepcopy((view, labels))
    first = assembly.assemble_player_inputs(view, labels)
    assert assembly.assemble_player_inputs(view, labels) == first
    shuffled = replace(view, players=tuple(reversed(view.players)), fixtures=tuple(reversed(view.fixtures)),
                       gameweeks=tuple(reversed(view.gameweeks)), history_rows=tuple(reversed(view.history_rows)),
                       target_fixtures_by_club=tuple(reversed(view.target_fixtures_by_club)))
    assert assembly.assemble_player_inputs(shuffled, dict(reversed(tuple(labels.items())))) == first
    assert (view, labels) == before
    with pytest.raises(FrozenInstanceError):
        first[0].player_id = 99
    assert all(record.__dataclass_params__.frozen for record in first)
    with pytest.raises(ValueError, match="labels must cover exactly"):
        assembly.assemble_player_inputs(view, {pid: label for pid, label in labels.items() if pid != 1})
    with pytest.raises(ValueError, match="labels must cover exactly"):
        assembly.assemble_player_inputs(view, labels | {999: USE})
    # Keep all 13 eligible observations; the later observation-window code owns truncation.
    many_fixtures = tuple(replace(view.fixtures[0], id=100 + i, kickoff_time=AS_OF - timedelta(days=20-i))
                          for i in range(13))
    many = replace(view, fixtures=many_fixtures, history_rows=tuple(row(1, f.id) for f in many_fixtures))
    assert len(by_id(assembly.assemble_player_inputs(many, labels))[1].personal_rows) == 13


def test_assembled_records_carry_coverage_unverified_label():
    view, labels = sample()
    records = assembly.assemble_player_inputs(view, labels)
    assert len(records) == len(view.players)
    assert all(record.coverage_status == "unverified_shadow_only" for record in records)
    assert all("coverage_status" in {field.name for field in fields(record)} for record in records)
    with pytest.raises(ValueError, match="require unverified_shadow_only"):
        assembly.assemble_player_inputs(view, labels | {1: replace(USE, coverage_status="verified")})
