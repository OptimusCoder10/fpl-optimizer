"""J35 slice 2: hand-computed synthetic evidence only."""

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import json
from math import sqrt
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpl_optimizer import development_opponent
from fpl_optimizer.services.opponent_adjustment import (
    adjust_prediction, historical_context_B, raw_factors,
)
from fpl_optimizer.services.opponent_profiles import (
    SETTINGS, home_term, league_mean, team_profile,
)
from fpl_optimizer.services.prediction_engine import FixtureObservation, predict
from fpl_optimizer.services.prior_selection import PriorSelection
from fpl_optimizer.services.retrospective_archive import (
    ArchiveCounts, ScoredFixture, eligible_scored_fixtures,
)
from fpl_optimizer.services.retrospective_benchmark import PointMetricRecord, summarize_points
from fpl_optimizer.services.retrospective_cohort import (
    BenchmarkFixture, BenchmarkRow, cutoff_for_event, select_cohort,
)
from fpl_optimizer.services import retrospective_runner as runner


T = datetime(2026, 8, 1, tzinfo=timezone.utc)
SETTING = (6, 4)
H = sqrt(7 / 5)


def scores():
    return tuple(ScoredFixture(i, 5, T + timedelta(days=i), 1, 2, 2, 1)
                 for i in range(1, 21))


def rows():
    return tuple(BenchmarkRow(7, i, 5, T + timedelta(days=i), True, 2, 90,
                              goals_scored=1, assists=1, clean_sheets=1)
                 for i in range(17, 21))


def prior(has_prior=False):
    return PriorSelection("current_season_position" if has_prior else "personal_current_only",
                          1 if has_prior else 0,
                          {"goals": 1.0} if has_prior else None, (), 4)


def prediction(target_ids=(101,)):
    return predict(personal_rows=(FixtureObservation(r.fixture_id, r.kickoff_time, r)
                                  for r in rows()), prior=prior(), position=2,
                   target_fixture_ids=target_ids, catalog_trusted=True,
                   chance=None, status="a", owned=False,
                   rules_version="synthetic", input_version="synthetic")


def target(identifier=101, *, home=2, away=1, event=6):
    return BenchmarkFixture(identifier, event, T + timedelta(days=30), home, away)


def adjusted(*, forecast=None, target_fixtures=None, history=None,
             player_rows=None, selected_prior=None, setting=SETTING,
             event_cutoff=None):
    return adjust_prediction(
        forecast or prediction(), player_rows=rows() if player_rows is None else player_rows,
        prior=selected_prior or prior(), target_fixtures=target_fixtures or (target(),),
        club_id=1, scored_fixtures=scores() if history is None else history,
        event_cutoff=event_cutoff or target().kickoff_time - timedelta(minutes=90),
        setting=setting)


def test_adjustment_default_neutral_leaves_prediction_identical():
    base = prediction()
    result = adjusted(forecast=base, setting=None)
    assert result.prediction is base and result.fixtures == ()


def test_live_predict_unchanged_without_adjustment():
    base = prediction()
    assert base == predict(personal_rows=(FixtureObservation(r.fixture_id, r.kickoff_time, r)
                                          for r in rows()), prior=prior(), position=2,
                           target_fixture_ids=(101,), catalog_trusted=True,
                           chance=None, status="a", owned=False,
                           rules_version="synthetic", input_version="synthetic")
    assert base.per_fixture[0].components["goals"] == 6


def test_baseline_path_unchanged_by_opponent_changes(monkeypatch):
    fixtures = tuple(BenchmarkFixture(f.fixture_id, f.event, f.kickoff_time,
                                      f.home_team_id, f.away_team_id)
                     for f in scores()[-4:]) + (target(),)
    history = rows() + (BenchmarkRow(7, 101, 6, target().kickoff_time,
                                    False, 2, 90),)
    base_view = runner.DevelopmentView(history, fixtures,
                                       ArchiveCounts(5, 5, 2, 0, 0), (7,))
    with_scores = replace(base_view, scored_by_event={6: scores()})
    def forbidden(*_args, **_kwargs):
        raise AssertionError("baseline called opponent adjustment")
    monkeypatch.setattr(runner, "adjust_prediction", forbidden)
    old_fold, old_points, old_minutes = runner._run_events(
        base_view, runner.DEFAULT_CANDIDATE, 6, 10)
    new_fold, new_points, new_minutes = runner._run_events(
        with_scores, runner.DEFAULT_CANDIDATE, 6, 10)
    assert old_points == new_points
    assert old_minutes == new_minutes
    assert old_fold.points == new_fold.points
    assert old_fold.per_gameweek_mae == new_fold.per_gameweek_mae


def test_attack_factor_hand_computed_home_and_away():
    # μ=60/40=3/2; latest-six D₂=(12+4×3/2)/10=9/5; H=√(7/5).
    home = raw_factors(opponent_id=2, was_home=True, scored_fixtures=scores(), setting=SETTING)
    away = raw_factors(opponent_id=2, was_home=False, scored_fixtures=scores(), setting=SETTING)
    assert home.attack == pytest.approx(H * 6 / 5)
    assert away.attack == pytest.approx(6 / (5 * H))


def test_clean_sheet_factor_hand_computed():
    # Latest-six A₂=(6+4×3/2)/10=6/5, so F_cs=H×(3/2)/(6/5).
    factor = raw_factors(opponent_id=2, was_home=True, scored_fixtures=scores(), setting=SETTING)
    assert factor.clean_sheet == pytest.approx(H * 5 / 4)


def test_B_hand_computed_with_kappa_eff_three():
    # Half-life one gives weights 1 and 1/2; κ=3 contributes neutral 3.
    assert historical_context_B((2, 4), half_life=1, kappa_eff=3) == pytest.approx(7 / 4.5)
    result = adjusted(selected_prior=prior(True))
    weights = [2 ** (-rank / 6) for rank in range(4)]
    assert result.kappa_eff == 3
    assert result.historical_attack_B == pytest.approx(
        (sum(weights) * H * 6 / 5 + 3) / (sum(weights) + 3))


def test_B_uses_kappa_eff_zero_when_no_prior():
    assert historical_context_B((2, 4), half_life=1, kappa_eff=0) == pytest.approx(4 / 1.5)
    assert adjusted().kappa_eff == 0
    assert adjusted().kappa_reason == "no_prior"


def test_relative_factor_is_raw_over_B():
    result = adjusted()
    # Every historical fixture was home against team 2; target is away vs team 2.
    assert result.historical_attack_B == pytest.approx(H * 6 / 5)
    assert result.historical_clean_sheet_B == pytest.approx(H * 5 / 4)
    assert result.fixtures[0].attack == pytest.approx(5 / 7)
    assert result.fixtures[0].clean_sheet == pytest.approx(5 / 7)


def test_only_goals_assists_clean_sheets_are_scaled():
    scored_rows = tuple(replace(row, bonus=2) for row in rows())
    base = predict(personal_rows=(FixtureObservation(row.fixture_id, row.kickoff_time, row)
                                  for row in scored_rows), prior=prior(), position=2,
                   target_fixture_ids=(101,), catalog_trusted=True,
                   chance=None, status="a", owned=False,
                   rules_version="synthetic", input_version="synthetic")
    result = adjusted(forecast=base, player_rows=scored_rows).prediction
    before, after = base.per_fixture[0].components, result.per_fixture[0].components
    assert before["bonus"] == 2
    assert after["goals"] == pytest.approx(before["goals"] * 5 / 7)
    assert after["assists"] == pytest.approx(before["assists"] * 5 / 7)
    assert after["clean_sheet"] == pytest.approx(before["clean_sheet"] * 5 / 7)
    assert {name: value for name, value in after.items()
            if name not in {"goals", "assists", "clean_sheet"}} == {
                name: value for name, value in before.items()
                if name not in {"goals", "assists", "clean_sheet"}}


def test_double_gameweek_adjusts_each_fixture_then_sums():
    base = prediction((101, 102))
    result = adjusted(forecast=base, target_fixtures=(target(101, home=1, away=2),
                                                      target(102, home=2, away=1)))
    assert [f.attack for f in result.fixtures] == pytest.approx([1, 5 / 7])
    assert result.prediction.event_mean == pytest.approx(sum(
        f.mean_points for f in result.prediction.per_fixture))
    assert result.prediction.event_components["goals"] == pytest.approx(6 * (1 + 5 / 7))


def test_blank_stays_exactly_zero():
    base = prediction(())
    result = adjusted(forecast=base)
    assert result.prediction is base and result.prediction.event_mean == 0.0


def test_missing_score_or_profile_gives_neutral_with_reason():
    missing = tuple(replace(f, away_score=None) if f.fixture_id == 20 else f for f in scores())
    unavailable = adjusted(history=missing)
    assert unavailable.fixtures[0].reason == "missing_score_in_window"
    assert unavailable.fixtures[0].attack == unavailable.fixtures[0].clean_sheet == 1
    assert adjusted(history=()).fixtures[0].reason == "mu_unavailable"
    short = tuple(f for f in scores() if f.fixture_id in (1, 2))
    assert adjusted(history=short).fixtures[0].reason == "under_three_scored_fixtures"
    assert adjusted(target_fixtures=(target(home=3, away=4),)).fixtures[0].reason == "missing_target_fixture"


def test_fewer_than_four_observations_gives_neutral_with_reason():
    three = rows()[:3]
    base = predict(personal_rows=(FixtureObservation(r.fixture_id, r.kickoff_time, r)
                                  for r in three), prior=prior(), position=2,
                   target_fixture_ids=(101,), catalog_trusted=True,
                   chance=None, status="a", owned=False,
                   rules_version="synthetic", input_version="synthetic")
    result = adjusted(forecast=base, player_rows=three)
    assert result.fixtures[0].reason == "under_four_player_observations"
    assert result.prediction is base


def test_neutral_adjustment_never_drops_player_event_from_cohort():
    fixtures = tuple(BenchmarkFixture(f.fixture_id, f.event, f.kickoff_time,
                                      f.home_team_id, f.away_team_id)
                     for f in scores()[-4:]) + (target(),)
    player_rows = rows() + (BenchmarkRow(7, 101, 6, target().kickoff_time,
                                        False, 2, 90),)
    cohort = select_cohort(6, fixtures, player_rows, (7,))
    player = cohort.players[0]
    base = predict(personal_rows=(FixtureObservation(r.fixture_id, r.kickoff_time, r)
                                  for r in player.eligible_rows), prior=prior(), position=2,
                   target_fixture_ids=player.scheduled_fixture_ids, catalog_trusted=True,
                   chance=None, status="a", owned=False,
                   rules_version="synthetic", input_version="synthetic")
    result = adjust_prediction(base, player_rows=player.eligible_rows, prior=prior(),
                               target_fixtures=(target(),), club_id=1,
                               scored_fixtures=(), event_cutoff=cohort.cutoff.cutoff_time,
                               setting=SETTING)
    assert cohort.accepted_outcome_count == 1
    assert result.prediction.event_mean == base.event_mean
    assert result.fixtures[0].reason == "mu_unavailable"
    view = runner.DevelopmentView(player_rows, fixtures,
                                  ArchiveCounts(5, 5, 2, 0, 0), (7,), {6: ()})
    _, baseline_records, _ = runner._run_events(view, runner.DEFAULT_CANDIDATE, 6, 10)
    opponent_fold, opponent_records, _ = runner._run_events(
        view, runner.DEFAULT_CANDIDATE, 6, 10, opponent_setting=SETTING)
    assert [(r.player_id, r.event, r.prediction) for r in baseline_records["model"]] == [
        (r.player_id, r.event, r.prediction) for r in opponent_records["model"]]
    assert opponent_fold.opponent_context_reasons["mu_unavailable"] == 1


def test_missing_historical_factor_counts_as_one_inside_B():
    history = tuple(f for f in scores() if f.fixture_id != 20)
    result = adjusted(history=history)
    raw = raw_factors(opponent_id=2, was_home=True,
                      scored_fixtures=history, setting=SETTING)
    weights = [2 ** (-rank / 6) for rank in range(4)]
    expected = (weights[0] * 1 + sum(weights[1:]) * raw.attack) / sum(weights)
    assert result.historical_attack_B == pytest.approx(expected)


def metric_result(setting, active_error, inactive_error):
    records = tuple(PointMetricRecord(player, event, 2, player == 7, error, 0)
                    for event in (6, 7, 8) for player, error in
                    ((7, active_error), (8, inactive_error)))
    return runner.OpponentResult(setting, (), {"model": summarize_points(records)},
                                 None, {}, records)


def baseline_result():
    candidate = metric_result(SETTING, 4, 6)
    return runner.CandidateResult(runner.DEFAULT_CANDIDATE, (),
                                  candidate.pooled_points, None, {}, 0.0,
                                  candidate.model_records)


def test_candidate_qualifies_only_with_both_gates():
    candidates = (metric_result((6, 4), 3, 4), metric_result((6, 12), 4, 2),
                  metric_result((12, 4), 4, 6), metric_result((12, 12), 5, 6))
    decision = runner.select_opponent_candidate(baseline_result(), candidates)
    assert [c["qualifies"] for c in decision.comparisons] == [True, False, False, False]
    assert decision.comparisons[1]["passes_se_gate"]
    assert not decision.comparisons[1]["passes_active_gate"]


def test_qualifier_choice_tiebreak_overall_then_active_then_L_lambda():
    candidates = (metric_result((6, 4), 3, 5), metric_result((6, 12), 2, 6),
                  metric_result((12, 4), 2, 6), metric_result((12, 12), 3, 6))
    decision = runner.select_opponent_candidate(baseline_result(), candidates)
    # Settings (6,12) and (12,4) tie on overall 4 and active 2; lower L wins.
    assert decision.selected == (6, 12)


def test_no_qualifier_freezes_nothing():
    candidates = tuple(metric_result(setting, 4, 6) for setting in sorted(SETTINGS))
    decision = runner.select_opponent_candidate(baseline_result(), candidates)
    assert decision.selected is None and decision.reason == "no_qualifier"


def test_four_settings_exactly_as_declared(monkeypatch):
    assert tuple(sorted(SETTINGS)) == ((6, 4), (6, 12), (12, 4), (12, 12))
    assert runner.DEFAULT_CANDIDATE == runner.Candidate(12, 6, 3)
    frozen = json.loads(development_opponent.FREEZE.read_text(encoding="utf-8"))
    assert frozen["frozen_parameters"] == {
        "lookback": 12, "half_life": 6, "prior_strength": 3}
    seen = []
    monkeypatch.setattr(development_opponent.DevelopmentView, "load_opponent",
                        lambda _path: SimpleNamespace(counts=ArchiveCounts(0, 0, 0, 0, 0)))
    monkeypatch.setattr(development_opponent, "run_development_candidate",
                        lambda _view, _candidate: "baseline")
    monkeypatch.setattr(development_opponent, "run_opponent_candidate",
                        lambda _view, setting: seen.append(setting) or setting)
    monkeypatch.setattr(development_opponent, "select_opponent_candidate",
                        lambda _base, _candidates: SimpleNamespace(selected=None,
                                                                   reason="no_qualifier"))
    monkeypatch.setattr(development_opponent, "write_opponent_report",
                        lambda _base, _candidates, _counts, *, path: path)
    assert development_opponent.main(["/unused"]) == 0
    assert tuple(seen) == ((6, 4), (6, 12), (12, 4), (12, 12))


def test_future_perturbation_target_and_later_scores_do_not_change_adjusted_forecast():
    def source(identifier, event, day, home_score, away_score):
        return SimpleNamespace(id=identifier, event=event, kickoff_time=T + timedelta(days=day),
                               team_h=1, team_a=2, team_h_score=home_score,
                               team_a_score=away_score)
    early = tuple(source(i, 5, i, 2, 1) for i in range(1, 21))
    original = early + (source(101, 6, 30, 2, 1), source(201, 7, 31, 1, 0))
    changed = early + (source(101, 6, 30, 999, 999), source(201, 7, 31, None, None),
                       source(202, 8, 32, 999, 999), source(301, 20, 10, 999, 999))
    def result(fixtures):
        archive = SimpleNamespace(catalog=SimpleNamespace(
            fixtures=SimpleNamespace(root=fixtures)))
        historical = eligible_scored_fixtures(archive, target_event=6)
        schedule = tuple(BenchmarkFixture(f.id, f.event, f.kickoff_time, f.team_h, f.team_a)
                         for f in fixtures)
        cohort = select_cohort(6, schedule, rows(), (7,))
        raw = tuple(ScoredFixture(f.id, f.event, f.kickoff_time, f.team_h, f.team_a,
                                  f.team_h_score, f.team_a_score) for f in fixtures)
        forecast = adjusted(history=raw).prediction
        return historical, cohort, forecast
    first = result(original)
    second = result(changed)
    assert first == second


def test_wrapper_uses_event_wide_cutoff_not_player_fixture_kickoff():
    early_target = BenchmarkFixture(102, 6, T + timedelta(days=28), 3, 4)
    event_cutoff = cutoff_for_event(6, (early_target, target())).cutoff_time
    assert event_cutoff == early_target.kickoff_time - timedelta(minutes=90)
    between = ScoredFixture(999, 5, T + timedelta(days=27, hours=23),
                            1, 2, 999, 0)
    assert between.kickoff_time + timedelta(hours=3) >= event_cutoff
    assert between.kickoff_time + timedelta(hours=3) < (
        target().kickoff_time - timedelta(minutes=90))
    original = adjusted(event_cutoff=event_cutoff)
    contaminated = adjusted(history=scores() + (between,), event_cutoff=event_cutoff)
    assert contaminated == original


def test_wrapper_excludes_target_event_fixture_even_with_early_event_cutoff():
    late_cutoff = target().kickoff_time + timedelta(days=2)
    target_score = ScoredFixture(101, 6, target().kickoff_time, 1, 2, 999, 0)
    assert target_score.kickoff_time + timedelta(hours=3) < late_cutoff
    original = adjusted(event_cutoff=late_cutoff)
    contaminated = adjusted(history=scores() + (target_score,), event_cutoff=late_cutoff)
    assert contaminated == original


def test_gw20_plus_rows_and_scores_cannot_change_development_outputs(monkeypatch):
    def fixture(identifier, event, kickoff, home_score, away_score):
        return SimpleNamespace(id=identifier, event=event, kickoff_time=kickoff,
                               team_h=1 if event != 6 else 2,
                               team_a=2 if event != 6 else 1,
                               team_h_score=home_score, team_a_score=away_score)

    scoring = tuple(field.name for field in fields(BenchmarkRow) if field.name not in {
        "player_id", "fixture_id", "event", "kickoff_time", "was_home", "position",
    })

    def archive_row(row):
        return SimpleNamespace(element=row.player_id, fixture=row.fixture_id,
                               was_home=row.was_home,
                               **{name: getattr(row, name) for name in scoring})

    development_fixtures = tuple(fixture(f.fixture_id, f.event, f.kickoff_time,
                                         f.home_score, f.away_score) for f in scores()) + (
        fixture(101, 6, target().kickoff_time, 2, 1),)
    development_rows = rows() + (
        BenchmarkRow(7, 101, 6, target().kickoff_time, False, 2, 90),)
    # GW20+ fixtures deliberately predate the GW6 cutoff. Event number, not
    # chronology alone, must keep their scores out of the development view.
    late_a = fixture(301, 20, T + timedelta(days=10, hours=1), 9, 8)
    late_b = fixture(302, 21, T + timedelta(days=11, hours=1), 7, 6)
    late_c = fixture(303, 22, T + timedelta(days=12, hours=1), None, 999)
    row_a = BenchmarkRow(7, 301, 20, late_a.kickoff_time, True, 2, 90,
                         goals_scored=9)
    row_b = BenchmarkRow(7, 302, 21, late_b.kickoff_time, True, 2, 90,
                         goals_scored=7)
    row_c = BenchmarkRow(7, 303, 22, late_c.kickoff_time, True, 2, 0,
                         goals_scored=99)

    def outputs(extra_fixtures, extra_rows):
        all_rows = tuple(archive_row(row) for row in development_rows + extra_rows)
        source = SimpleNamespace(
            catalog=SimpleNamespace(
                fixtures=SimpleNamespace(root=development_fixtures + extra_fixtures),
                bootstrap=SimpleNamespace(elements=(SimpleNamespace(id=7, element_type=2),)),
            ),
            histories={7: SimpleNamespace(history=all_rows)},
            audit=SimpleNamespace(distinct_keys=len(all_rows)),
        )
        monkeypatch.setattr(runner, "load_pinned_archive", lambda _path: source)
        view = runner.DevelopmentView.load_opponent(Path("/synthetic"))
        cohort = select_cohort(6, view.fixtures, view.rows, view.player_ids)
        player = cohort.players[0]
        scored = view.scored_by_event[6]
        mean = league_mean(scored)
        profile = team_profile(2, scored, mean, lookback=6, prior_matches=4)
        home = home_term(scored, mean)
        base = predict(
            personal_rows=(FixtureObservation(row.fixture_id, row.kickoff_time, row)
                           for row in player.eligible_rows),
            prior=prior(), position=2, target_fixture_ids=player.scheduled_fixture_ids,
            catalog_trusted=True, chance=None, status="a", owned=False,
            rules_version="synthetic", input_version="synthetic",
        )
        adjusted_forecast = adjust_prediction(
            base, player_rows=player.eligible_rows, prior=prior(),
            target_fixtures=(target(),), club_id=1, scored_fixtures=scored,
            event_cutoff=cohort.cutoff.cutoff_time, setting=SETTING,
        )
        fold, records, _ = runner._run_events(
            view, runner.DEFAULT_CANDIDATE, 6, 10, opponent_setting=SETTING)
        return (view.rows, view.fixtures, scored, cohort, mean, profile, home,
                adjusted_forecast, fold.points, fold.per_gameweek_mae,
                fold.opponent_context_reasons, records)

    original = outputs((late_a, late_b), (row_a, row_b))
    changed = outputs((fixture(301, 20, late_a.kickoff_time, 999, None), late_c),
                      (replace(row_a, goals_scored=999, bonus=99), row_c))
    assert changed == original


def test_entry_point_refuses_events_after_19(monkeypatch):
    def forbidden(_path):
        raise AssertionError("event 20 reached archive loading")
    monkeypatch.setattr(development_opponent.DevelopmentView, "load_opponent", forbidden)
    with pytest.raises(ValueError, match="GW6–19"):
        development_opponent.main(["/unused", "--last-event", "20"])


def test_entry_point_rejects_altered_freeze_parameters(monkeypatch):
    changed = {"frozen_parameters": {"lookback": 6, "half_life": 6,
                                     "prior_strength": 3}}
    monkeypatch.setattr(development_opponent, "FREEZE", SimpleNamespace(
        read_text=lambda **_kwargs: json.dumps(changed)))

    def forbidden(_path):
        raise AssertionError("archive loaded before freeze mismatch was rejected")

    monkeypatch.setattr(development_opponent.DevelopmentView, "load_opponent", forbidden)
    with pytest.raises(ValueError, match="frozen baseline does not match 12/6/3"):
        development_opponent.main(["/unused"])


def test_eligible_scores_reject_events_outside_gw6_to_19():
    source = SimpleNamespace(catalog=SimpleNamespace(
        fixtures=SimpleNamespace(root=())))
    for event in (5, 20):
        with pytest.raises(ValueError, match="GW6–19"):
            eligible_scored_fixtures(source, target_event=event)


def test_development_view_rejects_scores_after_gw19():
    scored_twenty = ScoredFixture(301, 20, T + timedelta(days=10), 1, 2, 9, 8)
    counts = ArchiveCounts(0, 0, 0, 0, 0)
    for scores_by_event in ({20: ()}, {6: (scored_twenty,)}):
        with pytest.raises(ValueError, match="GW1–19 only"):
            runner.DevelopmentView((), (), counts, (7,), scores_by_event)
