"""Development runner contract on small, inspectable synthetic fixtures."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpl_optimizer import development_fold, development_grid
from fpl_optimizer.services import retrospective_runner as runner
from fpl_optimizer.services.retrospective_archive import ArchiveCounts, BenchmarkArchive
from fpl_optimizer.services.retrospective_benchmark import PointMetricRecord, summarize_points
from fpl_optimizer.services.retrospective_cohort import (
    BenchmarkFixture, BenchmarkRow, select_cohort,
)


DAY = datetime(2026, 8, 1, tzinfo=timezone.utc)


def fixture(identifier, event, day, home=10, away=20):
    return BenchmarkFixture(identifier, event, DAY + timedelta(days=day), home, away)


def row(player, identifier, event, day, *, home=True, goals=0, minutes=90):
    return BenchmarkRow(player, identifier, event, DAY + timedelta(days=day),
                        home, 2, minutes, goals_scored=goals)


def view():
    fixtures = (fixture(101, 1, 1), fixture(501, 5, 20),
                fixture(601, 6, 30, 10, 30))
    rows = (row(1, 101, 1, 1), row(2, 101, 1, 1, goals=1),
            row(3, 101, 1, 1, home=False),
            row(1, 501, 5, 20, goals=1), row(2, 501, 5, 20),
            row(1, 601, 6, 30, goals=2), row(2, 601, 6, 30))
    return runner.DevelopmentView(rows, fixtures, ArchiveCounts(7, 3, 3, 0, 0),
                                  (1, 2, 3))


def run(data=None):
    return runner._run_events(data or view(), runner.DEFAULT_CANDIDATE, 6, 10)


def test_runner_uses_only_development_view_and_has_no_held_out_path(monkeypatch):
    data = view()
    assert runner.DEVELOPMENT_FOLDS == ((6, 10), (11, 15), (16, 19))
    assert set(inspect.signature(runner.DevelopmentView.load).parameters) == {"directory"}
    assert set(inspect.signature(runner.run_development_fold).parameters) == {
        "view", "candidate", "fold"}
    assert set(inspect.signature(runner.run_development_grid).parameters) == {"view"}
    with pytest.raises(TypeError, match="DevelopmentView"):
        runner.run_development_fold(
            BenchmarkArchive(data.rows, data.fixtures, data.counts),
            runner.DEFAULT_CANDIDATE, (6, 10))
    with pytest.raises(ValueError, match="development folds"):
        runner.run_development_fold(data, runner.DEFAULT_CANDIDATE, (20, 24))
    with pytest.raises(ValueError, match="development view"):
        replace(data, fixtures=data.fixtures + (fixture(999, 20, 90),))
    with pytest.raises(ValueError, match="development view"):
        replace(data, rows=data.rows + (row(1, 999, 20, 90),))
    source = SimpleNamespace(catalog=SimpleNamespace(
        bootstrap=SimpleNamespace(elements=[SimpleNamespace(id=3), SimpleNamespace(id=1)])))
    seen = []
    monkeypatch.setattr(runner, "load_pinned_archive", lambda _directory: source)

    def project(archive, *, through_event):
        seen.append((archive, through_event))
        return BenchmarkArchive(data.rows, data.fixtures, data.counts)

    monkeypatch.setattr(runner, "adapt_validated_archive", project)
    loaded = runner.DevelopmentView.load(Path("unused"))
    assert isinstance(loaded, runner.DevelopmentView)
    assert loaded.player_ids == (1, 3)
    assert seen == [(source, 19)]


def test_runner_calls_predict_with_neutral_availability_and_no_previous_season_rows(monkeypatch):
    actual_predict = runner.predict
    actual_prior = runner.select_prior
    calls = []

    def spy_predict(**kwargs):
        calls.append(kwargs)
        return actual_predict(**kwargs)

    def spy_prior(**kwargs):
        assert kwargs["previous"].personal_rows == ()
        assert kwargs["previous"].position_rows == ()
        return actual_prior(**kwargs)

    monkeypatch.setattr(runner, "predict", spy_predict)
    monkeypatch.setattr(runner, "select_prior", spy_prior)
    run()
    assert calls
    assert all(call["chance"] is None and call["status"] == "a"
               and call["owned"] is False and "assumed_factor" not in call
               for call in calls)
    assert all(call["window_size"] == 12 for call in calls)
    calls.clear()
    runner.run_development_fold(view(), runner.Candidate(6, 3, 1), (6, 10))
    assert calls and all(call["window_size"] == 6 and call["half_life"] == 3
                         and call["prior_strength"] == 1 for call in calls)


def test_grid_has_eighteen_distinct_candidates():
    assert len(runner.GRID) == len(set(runner.GRID)) == 18
    assert runner.DEFAULT_CANDIDATE in runner.GRID


def test_warmup_gameweeks_are_not_scored():
    fold, points, _ = run()
    assert fold.first_event == 6 and fold.last_event == 10
    assert {record.event for record in points["model"]} == {6}
    assert fold.events_scored == 1


def test_blank_decision_uses_schedule_not_results():
    _, points, _ = run()
    blank = next(record for record in points["model"] if record.player_id == 3)
    assert blank.prediction == blank.outcome == 0
    assert blank.outcome_exclusion_reason is None
    # No result for player 2 is a missing scheduled row, never a blank.
    data = view()
    changed = replace(data, rows=tuple(r for r in data.rows
                                       if not (r.player_id == 2 and r.event == 6)))
    missing = next(r for r in select_cohort(6, changed.fixtures, changed.rows,
                                             changed.player_ids).players if r.player_id == 2)
    assert missing.outcome_exclusion_reason == "missing_result_row"


def test_position_prior_uses_only_eligible_rows_and_excludes_focal_player(monkeypatch):
    actual = runner.select_prior
    seen = []

    def spy(**kwargs):
        selected = actual(**kwargs)
        seen.append((kwargs, selected))
        return selected

    monkeypatch.setattr(runner, "select_prior", spy)
    run()
    player_one = next(pair for pair in seen if pair[0]["player_id"] == 1)
    assert player_one[1].source == "current_season_position"
    assert player_one[1].row_count == 3  # two from player 2, one from player 3
    assert {r.player_id for r in player_one[0]["current_rows"]} == {1, 2, 3}
    assert len(player_one[0]["current_rows"]) == 5


def test_excluded_outcome_events_absent_from_model_and_every_baseline():
    data = view()
    changed = replace(data, rows=tuple(r for r in data.rows
                                       if not (r.player_id == 2 and r.event == 6)))
    fold, points, minutes = run(changed)
    assert fold.outcome_exclusions == {"missing_result_row": 1}
    for records in points.values():
        assert summarize_points(records).overall.eligible == 2
    assert runner.summarize_model_minutes(minutes).overall.played_fixtures == 1


def test_forecasts_and_cohort_identical_when_invisible_rows_are_perturbed():
    base = view()
    first_fold, first_points, _ = run(base)
    # Target row scores are invisible to forecasts; a late earlier-event row is
    # also invisible to every cutoff-eligible pool.
    changed_rows = tuple(replace(r, goals_scored=99, minutes=0)
                         if r.event == 6 else r for r in base.rows)
    changed_rows += (row(1, 502, 5, 29.95, goals=999),)
    changed_fixtures = base.fixtures + (fixture(502, 5, 29.95),)
    changed = replace(base, rows=changed_rows, fixtures=changed_fixtures)
    second_fold, second_points, _ = run(changed)
    assert first_fold.first_time_excluded == second_fold.first_time_excluded
    for name in first_points:
        assert [(r.player_id, r.prediction, r.active, r.position)
                for r in first_points[name]] == [
                    (r.player_id, r.prediction, r.active, r.position)
                    for r in second_points[name]
                ]
    assert first_fold.points["model"].overall.mae != second_fold.points["model"].overall.mae
    # Removing that same invisible row restores the original forecasts.
    removed = replace(changed, rows=tuple(r for r in changed.rows if r.fixture_id != 502),
                      fixtures=tuple(f for f in changed.fixtures if f.fixture_id != 502))
    _, removed_points, _ = run(removed)
    for name in first_points:
        assert [r.prediction for r in removed_points[name]] == [
            r.prediction for r in first_points[name]]


def synthetic_result(candidate, active_error, inactive_error):
    records = tuple(PointMetricRecord(player, gw, 2, player == 1, error, 0)
                    for gw in (6, 7, 8) for player, error in
                    ((1, active_error), (2, inactive_error)))
    return runner.CandidateResult(candidate, (),
                                  {"model": summarize_points(records)}, None, {}, 0.1,
                                  records)


def test_selection_keeps_defaults_unless_one_se_and_active_conditions_met():
    default = synthetic_result(runner.DEFAULT_CANDIDATE, 4, 6)
    worse_active = synthetic_result(runner.Candidate(6, 3, 1), 5, 2)
    assert runner.select_development_candidate((default, worse_active)).selected == default.candidate
    assert not runner.select_development_candidate((default, worse_active)).comparisons[0]["qualifies"]
    equal = synthetic_result(runner.Candidate(6, 3, 3), 4, 6)
    assert runner.select_development_candidate((default, equal)).selected == default.candidate
    # Pooled improvement is positive, but weekly swings make one SE larger.
    fluctuating = tuple(PointMetricRecord(player, gw, 2, player == 1, error, 0)
                        for gw, inactive in ((6, 1), (7, 9), (8, 5))
                        for player, error in ((1, 3), (2, inactive)))
    close = replace(synthetic_result(runner.Candidate(6, 6, 3), 3, 5),
                    pooled_points={"model": summarize_points(fluctuating)},
                    model_records=fluctuating)
    decision = runner.select_development_candidate((default, close))
    assert decision.selected == default.candidate
    assert decision.comparisons[0]["pooled_improvement"] > 0
    assert decision.comparisons[0]["pooled_improvement"] < decision.comparisons[0]["paired_standard_error"]
    one_week_default = replace(default, model_records=default.model_records[:2])
    better = synthetic_result(runner.Candidate(6, 12, 6), 2, 4)
    one_week_better = replace(better, model_records=better.model_records[:2])
    single = runner.select_development_candidate((one_week_default, one_week_better))
    assert single.selected == runner.DEFAULT_CANDIDATE
    assert single.comparisons[0]["paired_gameweeks"] == 1
    assert single.comparisons[0]["paired_standard_error"] is None
    assert single.comparisons[0]["paired_per_gameweek_differences"] == {6: -2.0}


def test_selection_reports_which_condition_blocked_the_challenger():
    default = synthetic_result(runner.DEFAULT_CANDIDATE, 4, 6)
    cases = (
        (runner.Candidate(6, 3, 1), 5, 2, ("active_cohort_mae",)),
        (runner.Candidate(6, 3, 3), 3, 7, ("se_bar",)),
        (runner.Candidate(6, 6, 1), 5, 5, ("se_bar", "active_cohort_mae")),
    )
    for candidate, active_error, inactive_error, expected in cases:
        challenger = synthetic_result(candidate, active_error, inactive_error)
        decision = runner.select_development_candidate((default, challenger))
        assert decision.selected == runner.DEFAULT_CANDIDATE
        assert decision.best_challenger == candidate
        assert decision.best_challenger_blocked_by == expected
        assert decision.comparisons[0]["blocked_by"] == expected
        assert not decision.comparisons[0]["qualifies"]


def test_selection_picks_lowest_overall_mae_among_qualifiers():
    default = synthetic_result(runner.DEFAULT_CANDIDATE, 4, 6)
    good = synthetic_result(runner.Candidate(6, 3, 1), 3, 5)
    best = synthetic_result(runner.Candidate(6, 3, 3), 2, 4)
    decision = runner.select_development_candidate((default, good, best))
    assert decision.selected == best.candidate
    assert all(item["qualifies"] for item in decision.comparisons)
    assert all(item["paired_standard_error"] == 0 for item in decision.comparisons)
    assert all(item["blocked_by"] == () for item in decision.comparisons)


def test_report_has_parameters_versions_runtime_and_no_player_identifiers(tmp_path):
    default_path = inspect.signature(runner.write_development_report).parameters["path"].default
    assert default_path == Path("/tmp/fpl-development-fold-report.json")
    fold, _, _ = run()
    folds = tuple(replace(fold, first_event=first, last_event=last)
                  for first, last in runner.DEVELOPMENT_FOLDS)
    reports = tuple(replace(synthetic_result(candidate, 4, 6), folds=folds,
                            pooled_model_minutes=fold.model_minutes)
                    for candidate in runner.GRID)
    path = runner.write_development_report(reports, view().counts,
                                           path=tmp_path / "development.json")
    data = json.loads(path.read_text())
    assert len(data["candidates"]) == 18
    assert data["model_version"] and data["rules_version"]
    assert all(len(item["folds"]) == 3 and item["runtime_seconds"] == .1
               and set(item["parameters"]) == {"lookback", "half_life", "prior_strength"}
               for item in data["candidates"])
    serialized = path.read_text()
    assert "player_id" not in serialized and "player_name" not in serialized
    assert "model_records" not in serialized
    assert data["selection"]["best_challenger_blocked_by"] == ["se_bar"]


def test_single_fold_entrypoint_prints_only_requested_aggregates(monkeypatch, capsys):
    monkeypatch.setattr(development_fold.DevelopmentView, "load",
                        lambda _directory: view())
    assert development_fold.main(["unused"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert set(payload) == {"scored_player_events", "excluded_count", "coverage",
                            "overall_mae", "elapsed_seconds"}
    assert set(payload["coverage"]) == {"model", *runner.BASELINE_NAMES}
    assert set(payload["overall_mae"]) == {"model", *runner.BASELINE_NAMES}
    assert payload["scored_player_events"] == 3
    assert payload["excluded_count"] == 0
    assert "player_id" not in output.out and "player_name" not in output.out


def test_grid_entry_point_runs_18_candidates_and_writes_aggregate_report(
    monkeypatch, capsys, tmp_path,
):
    monkeypatch.setattr(development_grid.DevelopmentView, "load",
                        lambda _directory: view())
    report_path = tmp_path / "development-grid.json"
    assert development_grid.main(["unused", "--output", str(report_path)]) == 0

    output = capsys.readouterr()
    assert output.err == ""
    lines = output.out.splitlines()
    assert len(lines) == 18 + 18 + 20 + 2
    assert all(lines[index].startswith(f"candidate {index + 1} of 18, elapsed seconds: ")
               for index in range(18))
    assert all("pooled overall MAE=" in line and "pooled active-cohort MAE=" in line
               and "bias=" in line and "RMSE=" in line for line in lines[18:36])
    assert all(f"default comparison {label} {name}: overall MAE=" in lines[36 + 5 * index + offset]
               and "active-cohort MAE=" in lines[36 + 5 * index + offset]
               and "bias=" in lines[36 + 5 * index + offset]
               and "RMSE=" in lines[36 + 5 * index + offset]
               for index, label in enumerate(("pooled", "GW6-10", "GW11-15", "GW16-19"))
               for offset, name in enumerate(("model", *runner.BASELINE_NAMES)))
    assert lines[56].startswith("selection: kept defaults ")
    assert ("paired mean difference=" in lines[56] and "standard error=" in lines[56]
            and "blocked by SE bar" in lines[56])
    assert lines[57] == f"aggregate report: {report_path}"

    report = json.loads(report_path.read_text())
    assert len(report["candidates"]) == 18
    assert {tuple(candidate["parameters"][name] for name in
                  ("lookback", "half_life", "prior_strength"))
            for candidate in report["candidates"]} == {
                (candidate.lookback, candidate.half_life, candidate.prior_strength)
                for candidate in runner.GRID
            }
    assert all(len(candidate["folds"]) == 3 for candidate in report["candidates"])
    assert report["selection"]["reason"] == "keep_default"
    assert report["selection"]["selected"] == {
        "lookback": 12, "half_life": 6, "prior_strength": 3,
    }
    assert "se_bar" in report["selection"]["best_challenger_blocked_by"]
    assert "player_id" not in output.out + report_path.read_text()
    assert "player_name" not in output.out + report_path.read_text()
