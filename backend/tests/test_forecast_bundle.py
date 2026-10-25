"""Synthetic J48 replay and leak guards; all inputs are in-memory."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpl_optimizer.services import forecast_bundle as bundle
from fpl_optimizer.services import forecast_bundle_policy as policy
from fpl_optimizer.services.forecast_bundle_inputs import assemble_player_inputs
from fpl_optimizer.services.history_eligibility import (
    HistoryEligibilityView, PlayerHistoryEvidence, RequiredFixture, decide_history_eligibility,
)
from fpl_optimizer.services.prediction_engine import FixtureObservation, predict
from fpl_optimizer.services.prior_selection import PriorExclusion, PriorSelection, select_prior
from fpl_optimizer.services.shared_freshness import check_shared_freshness
from test_forecast_bundle_inputs import AS_OF, EMPTY_PREVIOUS, row, sample


INPUT_VERSION = "synthetic-input-v1"


def prepared(view=None):
    if view is None:
        view, _ = sample()
    shared = check_shared_freshness(
        source_observed_at=AS_OF - timedelta(minutes=80), last_success_at=AS_OF - timedelta(minutes=79),
        now=AS_OF, next_deadline=view.target.deadline_time,
    )
    evidence = tuple(PlayerHistoryEvidence(
        p.id, None if p.id == 4 else AS_OF - timedelta(hours=49 if p.id == 3 else 1),
        None if p.id == 4 else AS_OF - timedelta(minutes=20), None if p.id == 4 else p.id,
        frozenset(r.fixture_id for r in view.history_rows if r.player_id == p.id),
        frozenset(r.fixture_id for r in view.history_rows if r.player_id == p.id),
    ) for p in view.players)
    return replace(view, shared_freshness=shared, player_history=evidence)


def inputs_for(view):
    history = HistoryEligibilityView(view.shared_freshness, view.player_history,
                                     view.history_publication_version, view.shared_publication.publication_version)
    labels = {p.player_id: policy.label_player_history(
        history, p, target_event_id=view.target.id, as_of=view.as_of,
    ) for p in view.player_history}
    return labels, assemble_player_inputs(view, labels)


def build(view=None):
    view = prepared() if view is None else view
    labels, inputs = inputs_for(view)
    return bundle.assemble_research_bundle(view, labels, inputs, input_version=INPUT_VERSION)


def records(payload):
    return {p["player_id"]: p for p in payload["players"]}


def restored_prior(saved):
    return PriorSelection(**(saved | {"exclusions": tuple(PriorExclusion(**x) for x in saved["exclusions"])}))


def test_bundle_replays_predictions_from_saved_inputs():
    result = build()
    saved = json.loads(result.canonical_bytes)
    rows = {tuple(r["key"]): SimpleNamespace(**r["counts"]) for r in saved["evidence"]["rows"]}
    fixtures = {f["id"]: f for f in saved["evidence"]["fixtures"]}
    for record in saved["players"]:
        if record["prediction"] is None:
            continue
        replay = predict(
            personal_rows=tuple(FixtureObservation(key[1], datetime.fromisoformat(fixtures[key[1]]["kickoff_time"]),
                                                  rows[tuple(key)]) for key in record["personal_window"]),
            prior=restored_prior(record["prior"]), position=record["position"],
            target_fixture_ids=record["target_fixture_ids"], catalog_trusted=True,
            **record["availability"], owned=False,
            rules_version=saved["versions"]["rules"], input_version=saved["versions"]["input"],
            **saved["baseline_parameters"],
        )
        assert json.loads(json.dumps(asdict(replay))) == record["prediction"]
    # Also compare to direct prediction using the original, untruncated B2 rows.
    view = prepared()
    _, inputs = inputs_for(view)
    fixture_by_id = {f.id: f for f in view.fixtures}
    for item in inputs:
        if item.prior is None:
            continue
        p = next(p for p in view.players if p.id == item.player_id)
        direct = predict(
            personal_rows=tuple(FixtureObservation(r.fixture_id, fixture_by_id[r.fixture_id].kickoff_time, r)
                                for r in item.personal_rows), prior=item.prior, position=p.position,
            target_fixture_ids=item.target_fixture_ids, catalog_trusted=True,
            chance=p.chance_of_playing_next_round, status=p.status, owned=False,
            rules_version=view.rules_version, input_version=INPUT_VERSION,
            window_size=12, half_life=6, prior_strength=3,
        )
        assert json.loads(json.dumps(asdict(direct))) == records(saved)[p.id]["prediction"]


def test_bundle_embeds_replayable_current_position_prior_evidence():
    saved = json.loads(build().canonical_bytes)
    by_player = records(saved)
    rows = {tuple(r["key"]): r["counts"] for r in saved["evidence"]["rows"]}
    pools = {p["position"]: p["row_keys"] for p in saved["evidence"]["position_pools"]}
    assert len(rows) == len(saved["evidence"]["rows"]) == 4
    assert all(pid not in (3, 4) for pid, _ in rows)
    for record in saved["players"]:
        if record["prior"] is None:
            continue
        pool = tuple(SimpleNamespace(player_id=pid, position=by_player[pid]["position"],
                                     scoring_counts=rows[pid, fid])
                     for pid, fid in pools[record["prior_pool_position"]])
        selected = select_prior(player_id=record["player_id"], position=record["position"],
                                current_rows=pool, previous=EMPTY_PREVIOUS)
        assert selected == restored_prior(record["prior"])
    assert by_player[1]["prior"]["row_count"] == 2
    assert by_player[1]["prior"]["component_means"]["goals"] == 6
    assert saved["evidence"]["prior_rule"] == "current_season_same_position_excluding_self_unweighted"


def test_bundle_blanks_and_doubles():
    p = records(build().bundle)
    assert p[6]["prediction"]["status"] == "blank"
    assert p[6]["prediction"]["event_mean"] == 0
    assert p[6]["prediction"]["appearance_probabilities"] is None
    double = p[1]["prediction"]
    assert [f["fixture_id"] for f in double["per_fixture"]] == [20, 21]
    assert double["event_mean"] == sum(f["mean_points"] for f in double["per_fixture"])
    assert p[6]["exclusion_reasons"] == []


def test_bundle_availability_for_unowned_candidates():
    for chance, status, factor, reason in (
        (None, "a", 1, None), (75, "d", 0.75, None), (0, "i", 0, None),
        (None, "d", None, "missing_chance_for_status"),
        (100, "i", None, "contradictory_chance_and_status"),
        (75, "mystery", None, "unrecognized_status"),
    ):
        view = prepared()
        view = replace(view, players=tuple(replace(p, chance_of_playing_next_round=chance, status=status)
                                           if p.id in (1, 6) else p for p in view.players))
        p = records(build(view).bundle)
        for pid in (6, 1):
            if reason:
                assert p[pid]["prediction"] is None, "unresolved availability must be excluded, including blanks"
                assert p[pid]["exclusion_reasons"] == [reason]
                assert p[pid]["warnings"] == ["availability_unresolved"]
            else:
                assert p[pid]["prediction"]["availability_factor"] == factor
                assert not p[pid]["prediction"]["availability_assumption"]
        if factor == 0:
            assert p[1]["prediction"]["event_mean"] == 0  # Known 0% is a legitimate zero.


def long_history_view():
    view = prepared()
    fixtures = tuple(replace(view.fixtures[0], id=100 + i, kickoff_time=AS_OF - timedelta(days=20-i//2))
                     for i in range(15))
    history = tuple(row(pid, f.id, minutes=0 if i % 3 == 0 else 90, goals=i % 4)
                    for pid in (1, 2) for i, f in enumerate(fixtures))
    return prepared(replace(view, fixtures=view.fixtures + fixtures, history_rows=history))


def test_bundle_uses_newest_twelve_with_fixture_id_ties():
    saved = build(long_history_view()).bundle
    p = records(saved)
    assert p[1]["personal_window"] == [[1, fid] for fid in range(114, 102, -1)], "save newest twelve, not oldest twelve"
    assert p[1]["prediction"]["raw_sample_count"] == 12
    # Unwindowed pool evidence is still complete, including older genuine zeros.
    assert p[1]["prior"]["row_count"] == 15
    assert len(saved["evidence"]["rows"]) == 30
    assert [1, 100] in saved["evidence"]["position_pools"][1]["row_keys"]


def test_bundle_preserves_history_exclusions_and_ignores_shadow_gaps():
    view = prepared()
    original = build(view)
    p = records(original.bundle)
    assert p[3]["exclusion_reasons"] == ["player history older than 48 hours"]
    assert p[4]["exclusion_reasons"] == ["no validated player history publication"]
    assert all(p[pid]["prediction"] is None and p[pid]["personal_window"] == [] for pid in (3, 4))
    assert {e["player_id"] for e in original.bundle["exclusions"]} == {3, 4}
    view = replace(view, player_history=tuple(replace(e, required_fixtures=(
        RequiredFixture(999, 1, AS_OF - timedelta(days=1)),
    )) for e in view.player_history))
    assert build(view).canonical_bytes == original.canonical_bytes


def test_bundle_is_deterministic_and_checksum_matches_canonical_bytes():
    view = long_history_view()
    labels, inputs = inputs_for(view)
    before = deepcopy((view, labels, inputs))
    result = bundle.assemble_research_bundle(view, labels, inputs, input_version=INPUT_VERSION)
    assert (view, labels, inputs) == before
    shuffled = replace(view, players=tuple(reversed(view.players)), history_rows=tuple(reversed(view.history_rows)),
                       fixtures=tuple(reversed(view.fixtures)), gameweeks=tuple(reversed(view.gameweeks)),
                       player_history=tuple(reversed(view.player_history)),
                       as_of=view.as_of.astimezone(timezone(timedelta(hours=5, minutes=30))))
    again = bundle.assemble_research_bundle(shuffled, dict(reversed(tuple(labels.items()))),
                                            tuple(reversed(inputs)), input_version=INPUT_VERSION)
    assert again.canonical_bytes == result.canonical_bytes
    assert result.sha256 == hashlib.sha256(result.canonical_bytes).hexdigest()
    assert result.canonical_bytes == json.dumps(result.bundle, sort_keys=True, separators=(",", ":"),
                                                ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_bundle_has_no_manager_confirmed_data():
    view = prepared()
    clean = build(view).canonical_bytes
    private = {"manager_id": 123, "bank": 100, "owned_player_ids": [1], "selling_prices": {1: 50},
               "captain": 1, "chip": "wildcard", "free_transfers": 5}
    object.__setattr__(view, "manager_state", private)
    labels, inputs = inputs_for(view)
    for item in inputs:
        object.__setattr__(item, "manager_state", private)
    result = bundle.assemble_research_bundle(view, labels, inputs, input_version=INPUT_VERSION)
    assert result.canonical_bytes == clean
    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(x) for x in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(x) for x in value))
        return set()
    assert keys(result.bundle).isdisjoint({"manager_state", *private})


def test_bundle_unavailable_prediction_is_excluded_not_zero():
    view = prepared()
    view = replace(view, players=tuple(replace(p, team_id=1) if p.id == 6 else p for p in view.players))
    result = build(view).bundle
    p = records(result)[6]
    assert p["prediction"]["status"] == "unavailable"
    assert p["prediction"]["event_mean"] is None, "unavailable predictions must never become zero"
    assert p["prediction"]["event_components"] is None
    assert p["exclusion_reasons"] == ["no_prior_or_personal_observations"]
    assert {e["player_id"] for e in result["exclusions"]} == {3, 4, 6}


def test_bundle_rejects_mismatched_or_ineligible_player_inputs():
    view = prepared()
    labels, inputs = inputs_for(view)
    for bad in (
        replace(inputs[0], position=3), replace(inputs[0], team_id=2),
        replace(inputs[0], target_fixture_ids=()), replace(inputs[0], previous_season_prior="loaded"),
        replace(inputs[0], coverage_status="verified"),
        replace(inputs[0], exclusion_reasons=("invented exclusion",)),
        replace(inputs[0], personal_rows=inputs[0].personal_rows + (row(1, 20),)),
    ):
        with pytest.raises(ValueError, match="assembled inputs do not match"):
            bundle.assemble_research_bundle(view, labels, (bad, *inputs[1:]), input_version=INPUT_VERSION)
    with pytest.raises(ValueError, match="prior or exclusion"):
        bad = replace(inputs[0], prior=replace(inputs[0].prior, source="previous_season_personal"))
        bundle.assemble_research_bundle(view, labels, (bad, *inputs[1:]), input_version=INPUT_VERSION)
    with pytest.raises(ValueError, match="one input, label and checkpoint"):
        bundle.assemble_research_bundle(view, labels, (*inputs, inputs[0]), input_version=INPUT_VERSION)
    forged = labels | {1: replace(labels[1], action="exclude", reason="forged exclusion")}
    forged_inputs = assemble_player_inputs(view, forged)
    with pytest.raises(ValueError, match="label does not match"):
        bundle.assemble_research_bundle(view, forged, forged_inputs, input_version=INPUT_VERSION)


def test_bundle_requires_future_target_and_versions():
    view = prepared()
    labels, inputs = inputs_for(view)
    for bad in (replace(view, target=None), replace(view, as_of=view.target.deadline_time)):
        with pytest.raises(ValueError, match="requires the captured future target"):
            bundle.assemble_research_bundle(bad, labels, inputs, input_version=INPUT_VERSION)
    # No predictor runs for an untrusted catalog: the bundle must still require versions.
    untrusted = replace(view, shared_freshness=replace(view.shared_freshness, status="unknown"))
    untrusted_labels, untrusted_inputs = inputs_for(untrusted)
    for bad, version in ((replace(untrusted, rules_version=None), INPUT_VERSION), (untrusted, "")):
        with pytest.raises(ValueError, match="rules and input versions"):
            bundle.assemble_research_bundle(bad, untrusted_labels, untrusted_inputs, input_version=version)
    naive_source = replace(view, shared_publication=replace(
        view.shared_publication, source_observed_at=AS_OF.replace(tzinfo=None),
    ))
    with pytest.raises(ValueError, match="bundle timestamps must include a UTC offset"):
        bundle.assemble_research_bundle(naive_source, labels, inputs, input_version=INPUT_VERSION)


def test_bundle_records_versions_provenance_and_research_scope():
    saved = build().bundle
    freeze = json.loads((Path(__file__).resolve().parents[2] / "docs/benchmarks/baseline-freeze-2026-10-19.json").read_text())
    params = freeze["frozen_parameters"]
    assert saved["baseline_parameters"] == {"window_size": params["lookback"], "half_life": params["half_life"],
                                            "prior_strength": params["prior_strength"]}
    assert saved["versions"] == {"model": "empirical-fixture-v1", "rules": "synthetic-rules",
                                 "scoring_adapter": "fpl-defensive-contribution-v1", "input": INPUT_VERSION}
    assert saved["capture_time"] == AS_OF.isoformat()
    assert saved["target"] == {"id": 4, "deadline_time": (AS_OF + timedelta(hours=5)).isoformat()}
    assert saved["schema_version"] == "research-bundle-v1"
    assert saved["previous_season_prior"] == "not_loaded" and saved["opponent_adjustment"] == "none"
    assert saved["coverage_status"] == "unverified_shadow_only"
    assert all(p["coverage_status"] == "unverified_shadow_only" and p["previous_season_prior"] == "not_loaded"
               for p in saved["players"])
    assert records(saved)[1]["history_publication"]["publication_version"] == 1
    assert saved["warnings"] == ["shared_catalog_age_warning"]


def test_bundle_rejects_nonfinite_json(monkeypatch):
    original = bundle.predict
    def nonfinite(**kwargs):
        return replace(original(**kwargs), event_mean=float("nan"))
    monkeypatch.setattr(bundle, "predict", nonfinite)
    with pytest.raises(ValueError, match="Out of range float values"):
        build()


def test_policy_uses_j42_history_age_status(monkeypatch):
    view = prepared()
    history = HistoryEligibilityView(view.shared_freshness, view.player_history, 1, 1)
    missing = replace(view.player_history[0], missing_fixture_ids=(999,))
    actual = decide_history_eligibility(replace(history, players=(missing,)), now=AS_OF,
        mode="transfers", owned_player_ids=frozenset(), locked_player_ids=frozenset(), target_event=4)
    assert actual.candidates[0].status == "stale" and actual.candidates[0].history_age_status == "current"
    label = policy.label_player_history(history, missing, target_event_id=4, as_of=AS_OF)
    assert label.history_age_status == "current" and label.action == "exclude"
    original = policy.decide_history_eligibility
    def override(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(result, candidates=(replace(result.candidates[0], history_age_status="stale"),))
    monkeypatch.setattr(policy, "decide_history_eligibility", override)
    label = policy.label_player_history(history, view.player_history[0], target_event_id=4, as_of=AS_OF)
    assert label.history_age_status == "stale", "research policy must use J42's age status"
    assert label.action == "exclude"


def test_bundle_excludes_untrusted_shared_catalog():
    view = prepared()
    view = replace(view, shared_freshness=replace(view.shared_freshness, status="unknown"))
    result = build(view).bundle
    assert len(result["exclusions"]) == len(view.players)
    assert result["evidence"]["rows"] == []
    assert all(p["prediction"] is None and p["exclusion_reasons"] == ["no trusted shared catalog publication"]
               for p in result["players"])
