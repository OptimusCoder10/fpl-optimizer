"""Synthetic GW20–38 loader and runner checks; no real archive access."""

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpl_optimizer.services import heldout_archive as loader
from fpl_optimizer.services import heldout_runner as runner
from fpl_optimizer.services import retrospective_archive as development_archive
from fpl_optimizer.services import retrospective_runner as development_runner
from fpl_optimizer.services.opponent_profiles import league_mean, team_profile
from fpl_optimizer.services.retrospective_archive import ArchiveCounts, ScoredFixture
from fpl_optimizer.services.retrospective_benchmark import PointMetricRecord, summarize_points
from fpl_optimizer.services.retrospective_cohort import BenchmarkFixture, BenchmarkRow

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FROZEN = runner.FrozenParameters(12, 6, 3, (12, 12))


def fixture(identifier, event, hours, home=10, away=20):
    return BenchmarkFixture(identifier, event, START + timedelta(hours=hours), home, away)


def row(player, identifier, event, hours, *, home=True, goals=0, minutes=90):
    return BenchmarkRow(player, identifier, event, START + timedelta(hours=hours),
                        home, 2, minutes, goals_scored=goals)


def scored(identifier, event, hours, home_score=1, away_score=0, home=10, away=20):
    return ScoredFixture(identifier, event, START + timedelta(hours=hours),
                         home, away, home_score, away_score)


def view():
    fixtures = (fixture(19, 19, 1), fixture(20, 20, 24),
                fixture(21, 21, 48, 10, 30))
    rows = (row(1, 19, 19, 1), row(2, 19, 19, 1, home=False),
            row(1, 20, 20, 24, goals=1), row(1, 21, 21, 48))
    scores = {event: tuple(scored(f.fixture_id, f.event,
                                  (f.kickoff_time - START).total_seconds() / 3600)
                            for f in fixtures if event in (20, 21) and f.event < event)
              for event in range(20, 39)}
    return loader.HeldoutView(rows, fixtures, ArchiveCounts(4, 3, 3, 0, 0),
                              (1, 2), scores)


def source(fixtures):
    return SimpleNamespace(catalog=SimpleNamespace(fixtures=SimpleNamespace(root=fixtures)))


def raw_fixture(identifier, event, hours, *, home_score=1, away_score=0):
    return SimpleNamespace(id=identifier, event=event,
                           kickoff_time=START + timedelta(hours=hours),
                           team_h=10, team_a=20,
                           team_h_score=home_score, team_a_score=away_score)


def test_heldout_view_contains_only_gw20_to_38_targets():
    data = view()
    assert list(data.scored_by_event) == list(range(20, 39))
    with pytest.raises(ValueError, match="GW20–38"):
        data.cohort(19)
    with pytest.raises(ValueError, match="GW20–38"):
        data.cohort(39)
    assert data.cohort(20).cutoff.event == 20


def test_heldout_inputs_include_earlier_events_only_by_cutoff_buffer():
    # t(20) = hour 22.5. The hour-19.5 row is exactly on the three-hour edge.
    fixtures = (fixture(1, 19, 1), fixture(2, 19, 19.5),
                fixture(3, 20, 24), fixture(4, 21, 0))
    data = replace(view(), fixtures=fixtures,
                   rows=(row(1, 1, 19, 1), row(1, 2, 19, 19.5),
                         row(1, 3, 20, 24), row(1, 4, 21, 0)),
                   player_ids=(1,), scored_by_event={event: () for event in range(20, 39)})
    assert [r.fixture_id for r in data.cohort(20).players[0].eligible_rows] == [1]
    scores = loader.heldout_scored_fixtures(source(tuple(raw_fixture(
        f.fixture_id, f.event, (f.kickoff_time - START).total_seconds() / 3600)
        for f in fixtures)), target_event=20)
    assert [f.fixture_id for f in scores] == [1]


def test_heldout_target_outcomes_never_enter_forecast_inputs(monkeypatch):
    seen = []
    actual = runner.predict
    def spy(**kwargs):
        observations = tuple(kwargs["personal_rows"])
        seen.extend(o.value.event for o in observations)
        return actual(**{**kwargs, "personal_rows": observations})
    monkeypatch.setattr(runner, "predict", spy)
    runner.compare_frozen(view(), FROZEN)
    # Event 20's target row can be input to 21, never to 20 itself.
    assert seen and set(seen) <= {19, 20}
    data = view()
    assert [r.event for r in data.cohort(20).players[0].eligible_rows] == [19]
    assert [r.event for r in data.cohort(21).players[0].eligible_rows] == [19, 20]
    # Deliberately inconsistent synthetic row timing makes the event guard
    # observable independently of the kickoff rule.
    early_target = replace(data, rows=tuple(replace(r, kickoff_time=START)
                                            if r.fixture_id == 20 else r
                                            for r in data.rows))
    assert [r.event for r in early_target.cohort(20).players[0].eligible_rows] == [19]


def test_heldout_profile_scores_use_event_wide_cutoff():
    fixtures = (raw_fixture(1, 19, 1, home_score=2),
                raw_fixture(2, 20, 20, home_score=5),
                raw_fixture(3, 20, 24, home_score=99),
                raw_fixture(4, 21, 48, home_score=999))
    # For GW20 the earliest target kickoff is hour 20: even a different
    # match at hour 24 cannot push admission past hour 18.5.
    admitted = loader.heldout_scored_fixtures(source(fixtures), target_event=20)
    assert [f.fixture_id for f in admitted] == [1]
    assert league_mean(admitted).mu == 1
    assert team_profile(10, admitted, league_mean(admitted),
                        lookback=12, prior_matches=12).scored_fixtures == 1
    assert [f.fixture_id for f in loader.heldout_scored_fixtures(
        source(fixtures), target_event=21)] == [1, 2, 3]


def test_heldout_future_perturbation_changes_nothing(monkeypatch):
    base = view()
    changed_rows = tuple(replace(r, goals_scored=999, minutes=0)
                         if r.event >= 20 else r for r in base.rows)
    changed = replace(base, rows=changed_rows + (row(1, 99, 22, 0, goals=999),),
                      fixtures=base.fixtures + (fixture(99, 22, 0),))
    for data in (base, changed, replace(changed, rows=tuple(
            r for r in changed.rows if r.event < 20))):
        cohort = data.cohort(20)
        assert [(p.player_id, p.eligible_rows, p.position, p.active)
                for p in cohort.players] == [(p.player_id, p.eligible_rows,
                                              p.position, p.active)
                                             for p in base.cohort(20).players]
        assert data.scored_by_event[20] == base.scored_by_event[20]
    # Scores of the target and later fixtures are never even read.
    original = (raw_fixture(19, 19, 1), raw_fixture(20, 20, 24),
                raw_fixture(21, 21, 48))
    corrupted = (original[0], raw_fixture(20, 20, 24, home_score=999),
                 raw_fixture(21, 21, 48, home_score=999),
                 raw_fixture(99, 22, 0, home_score=999))
    assert loader.heldout_scored_fixtures(source(original), target_event=20) == (
        loader.heldout_scored_fixtures(source(corrupted), target_event=20))
    assert [p.player_id for p in base.cohort(20).players] == [
        p.player_id for p in changed.cohort(20).players]
    actual_predict = runner.predict
    actual_adjust = runner.heldout_adjust_prediction
    trace = []
    def predict_spy(**kwargs):
        result = actual_predict(**kwargs)
        if kwargs["target_fixture_ids"] == (20,):
            trace.append(("baseline", result.event_mean))
        return result
    def adjust_spy(prediction, **kwargs):
        target_fixtures = tuple(kwargs["target_fixtures"])
        result = actual_adjust(prediction, **{**kwargs, "target_fixtures": target_fixtures})
        if tuple(f.fixture_id for f in target_fixtures) == (20,):
            trace.append(("opponent", result.prediction.event_mean))
        return result
    monkeypatch.setattr(runner, "predict", predict_spy)
    monkeypatch.setattr(runner, "heldout_adjust_prediction", adjust_spy)
    traces = []
    for data in (base, changed, replace(changed, rows=tuple(
            r for r in changed.rows if r.event < 20))):
        trace.clear()
        runner.compare_frozen(data, FROZEN)
        traces.append(tuple(trace))
    assert traces[0] == traces[1] == traces[2]


def test_heldout_runner_refuses_without_guard_success(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise runner.HeldoutGuardError("synthetic refusal")
    monkeypatch.setattr(runner, "authorize_heldout_run", refuse)
    monkeypatch.setattr(runner.HeldoutView, "load", lambda *_: pytest.fail("loader called"))
    with pytest.raises(runner.HeldoutGuardError, match="synthetic refusal"):
        runner.run_heldout_once("unused", tmp_path, repo_root=tmp_path,
                                lock_path=tmp_path / "heldout-run-lock.json")
    monkeypatch.setattr(runner, "authorize_heldout_run", lambda *a, **k: {})
    with pytest.raises(runner.HeldoutGuardError, match="without its one-time lock"):
        runner.run_heldout_once("unused", tmp_path, repo_root=tmp_path,
                                lock_path=tmp_path / "heldout-run-lock.json")
    (tmp_path / "heldout-run-failure.json").unlink()
    lock = tmp_path / "heldout-run-lock.json"
    lock.write_text("synthetic lock")
    monkeypatch.setattr(runner, "authorize_heldout_run",
                        lambda *a, **k: {"baseline_freeze_sha256": "a",
                                         "opponent_freeze_sha256": "b"})
    monkeypatch.setattr(runner, "parameters_from_verified_freezes",
                        lambda *a: (_ for _ in ()).throw(ValueError("synthetic bad freeze")))
    with pytest.raises(ValueError, match="synthetic bad freeze"):
        runner.run_heldout_once("unused", tmp_path, repo_root=tmp_path, lock_path=lock)
    assert json.loads((tmp_path / "heldout-run-failure.json").read_text()) == {
        "category": "invalid_freeze"}
    assert lock.read_text() == "synthetic lock"


def test_heldout_runner_reads_parameters_only_from_freeze_files(tmp_path):
    folder = tmp_path / "docs/benchmarks"
    folder.mkdir(parents=True)
    base = {"lookback": 12, "half_life": 6, "prior_strength": 3}
    (folder / "baseline-freeze-2026-10-19.json").write_text(
        json.dumps({"frozen_parameters": base}))
    (folder / "opponent-freeze-2026-10-20.json").write_text(json.dumps({
        "baseline_parameters": base,
        "frozen_parameters": {"L": 12, "lambda": 12},
    }))
    def hashes():
        return tuple(hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in (
            "baseline-freeze-2026-10-19.json", "opponent-freeze-2026-10-20.json"))
    original_hashes = hashes()
    assert runner.parameters_from_verified_freezes(tmp_path, original_hashes) == FROZEN
    for modified, key in ((base | {"lookback": 11}, "baseline"),
                          ({"L": 6, "lambda": 12}, "opponent")):
        if key == "baseline":
            (folder / "baseline-freeze-2026-10-19.json").write_text(
                json.dumps({"frozen_parameters": modified}))
            (folder / "opponent-freeze-2026-10-20.json").write_text(json.dumps({
                "baseline_parameters": modified,
                "frozen_parameters": {"L": 12, "lambda": 12},
            }))
        else:
            (folder / "baseline-freeze-2026-10-19.json").write_text(
                json.dumps({"frozen_parameters": base}))
            (folder / "opponent-freeze-2026-10-20.json").write_text(json.dumps({
                "baseline_parameters": base, "frozen_parameters": modified,
            }))
        with pytest.raises(ValueError, match="changed after"):
            runner.parameters_from_verified_freezes(tmp_path, original_hashes)
        with pytest.raises(ValueError, match="frozen"):
            runner.parameters_from_verified_freezes(tmp_path, hashes())


def test_baseline_and_opponent_use_identical_cohort():
    report = runner.compare_frozen(view(), FROZEN)
    for scope in ("overall", "active_overall"):
        assert (report["baseline"]["points"][scope]["eligible"] ==
                report["opponent"]["points"][scope]["eligible"])
    assert report["baseline"]["weekly_mae"].keys() == report["opponent"]["weekly_mae"].keys()
    assert report["counts"]["outcome_exclusions"] == {"missing_result_row": 1}


def test_adoption_requires_both_overall_and_active_mae_lower():
    base = summarize_points((PointMetricRecord(1, 20, 2, True, 2, 0),
                             PointMetricRecord(2, 20, 2, False, 2, 0)))
    overall_only = summarize_points((PointMetricRecord(1, 20, 2, True, 3, 0),
                                     PointMetricRecord(2, 20, 2, False, 0, 0)))
    both = summarize_points((PointMetricRecord(1, 20, 2, True, 1, 0),
                             PointMetricRecord(2, 20, 2, False, 1, 0)))
    assert runner.adoption_decision(base, overall_only) == {
        "opponent_adopted": False, "overall_mae_lower": True,
        "active_mae_lower": False}
    assert runner.adoption_decision(base, both)["opponent_adopted"] is True


def test_heldout_report_has_no_player_keys():
    report = runner.compare_frozen(view(), FROZEN)
    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield str(key)
                yield from keys(child)
        elif isinstance(value, (tuple, list)):
            for child in value:
                yield from keys(child)
    forbidden = {"player_id", "player_ids", "player_name", "player_names",
                 "fixture_id", "fixture_ids", "name", "web_name"}
    assert not any(key.lower() in forbidden for key in keys(report))
    assert "1" not in report["baseline"]["points"]["by_position"]


def test_development_entry_points_do_not_import_heldout_modules():
    root = Path(__file__).resolve().parents[1] / "src/fpl_optimizer"
    for path in (root / "development_fold.py", root / "development_grid.py",
                 root / "development_opponent.py",
                 root / "services/retrospective_runner.py",
                 root / "services/retrospective_archive.py"):
        imports = [node for node in ast.walk(ast.parse(path.read_text()))
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        assert "heldout" not in " ".join(ast.unparse(node) for node in imports)


def test_development_ceiling_guards_still_reject_events_after_19():
    assert development_archive.DEVELOPMENT_LAST_EVENT == 19
    assert development_runner.DEVELOPMENT_FOLDS == ((6, 10), (11, 15), (16, 19))
    with pytest.raises(ValueError, match="development folds"):
        development_runner.run_development_fold(
            development_runner.DevelopmentView(
                (row(1, 19, 19, 1),), (fixture(19, 19, 1),),
                ArchiveCounts(1, 1, 1, 0, 0), (1,)),
            development_runner.DEFAULT_CANDIDATE, (20, 24))
    with pytest.raises(ValueError, match="development view"):
        development_runner.DevelopmentView((row(1, 20, 20, 24),),
                                          (fixture(20, 20, 24),),
                                          ArchiveCounts(1, 1, 1, 0, 0), (1,))
