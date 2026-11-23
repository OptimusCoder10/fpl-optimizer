"""Synthetic slice 3.1 proof, including real serial CBC and fake solver stops."""

from collections import Counter
from dataclasses import replace
from decimal import Decimal, localcontext
import json
import math
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time

import pulp
import pytest

from fpl_optimizer.schemas.optimization import LineupPlayer, LineupSolution
from fpl_optimizer.services import optimization_engine as engine
from fpl_optimizer.services.optimization_validation import validate_lineup_solution
from lineup_oracle import exhaustive_lineup_score


POSITIONS = ("GK",) * 2 + ("DEF",) * 5 + ("MID",) * 5 + ("FWD",) * 3
FIXED_XI = (1, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14)


def squad(scores=None):
    scores = [1000] * 15 if scores is None else scores
    return tuple(LineupPlayer(i, pos, (i - 1) // 3 + 1, score)
                 for i, (pos, score) in enumerate(zip(POSITIONS, scores, strict=True), 1))


def solve(players):
    result = engine.solve_lineup(players, 5)
    assert result.status == "optimal", result
    assert result.solution is not None
    validate_lineup_solution(players, result.solution)
    return result.solution


def raw_solve(players, tmp_path):
    return engine._solve_model(players, time.monotonic() + 3, str(tmp_path))


@pytest.mark.parametrize("prediction,expected", [
    ("0.0005", 1), (Decimal("-0.0005"), -1), ("0.0004999", 0),
    ("-0.0004999", 0), ("1.2345", 1235), ("-1.2345", -1235),
    (0.0005, 1), (-0.0005, -1), ("1.2344999", 1234), (2, 2000),
    ("0", 0), ("-0", 0), ("999999999.9995", 1000000000000),
])
def test_prediction_rounds_once_half_up(prediction, expected):
    with localcontext() as context:
        context.prec = 3
        assert engine.prediction_to_m(prediction) == expected


@pytest.mark.parametrize("value", [True, None, "bad", "NaN", "Infinity", float("-inf")])
def test_prediction_rejects_nonfinite_or_invalid_values(value):
    with pytest.raises(ValueError):
        engine.prediction_to_m(value)


@pytest.mark.parametrize("scores", [[1000]*15, [0]*15, [-i*100 for i in range(1, 16)],
                                   [-1000]*15, [0, -10, 20, 20, 20, -1, -2, 20, 20, 20, 0, 0, 20, 0, -2]])
def test_handwritten_ties_zeros_and_negative_scores(scores):
    players = squad(scores)
    result = solve(players)
    assert result.score_units == exhaustive_lineup_score(players)
    assert result.score_points == Decimal(f"{result.score_units}e-3")


@pytest.mark.parametrize("fourth_defender,expected", [(400, (3, 5, 2)), (600, (4, 4, 2))])
def test_352_versus_442(fourth_defender, expected):
    players = squad([1000, 0, 1000, 1000, 1000, fourth_defender, -1000,
                     1000, 1000, 1000, 1000, 500, 1000, 1000, -1000])
    result = solve(players)
    counts = Counter(players[i-1].position for i in result.starting_xi)
    assert (counts["DEF"], counts["MID"], counts["FWD"]) == expected
    assert result.score_units == exhaustive_lineup_score(players)


def test_formation_changes_top_eleven_but_keeps_highest_scoring_captain():
    players = squad([10000, 9000, 800, 700, 600, 500, 400,
                     8000, 7000, 6000, 5000, 4000, 3000, 2000, 1000])
    naive = tuple(p.player_id for p in sorted(players, key=lambda p: -p.score_m)[:11])
    assert Counter(players[i-1].position for i in naive)["GK"] == 2
    result = solve(players)
    assert set(result.starting_xi) != set(naive)
    assert result.captain == 1
    assert result.score_units == exhaustive_lineup_score(players)


def test_inherited_fourth_club_player_is_legal_when_holding():
    players = tuple(replace(p, club_id=1) if p.player_id == 4 else p for p in squad())
    assert sum(p.club_id == 1 for p in players) == 4
    assert solve(players).score_units == 12000


@pytest.fixture(scope="module")
def random_timings():
    timings = []
    yield timings
    if timings:
        ordered = sorted(timings)
        print("\nLINEUP_RANDOM_TIMINGS " + json.dumps({
            "cases": len(timings), "seconds_total": sum(timings),
            "seconds_min": min(timings), "seconds_median": statistics.median(timings),
            "seconds_p95": ordered[math.ceil(len(ordered)*.95)-1],
            "seconds_max": max(timings),
            "includes": "worker startup, CBC, result validation and cleanup",
        }, sort_keys=True))


@pytest.mark.parametrize("seed", range(200))
def test_seeded_random_squad_matches_exhaustive_optimum(seed, random_timings):
    rng = random.Random(seed)
    players = tuple(replace(p, club_id=rng.randint(1, 20),
                            score_m=rng.choice([-3000, 0, 1000]) if seed % 3 == 0
                            else rng.randint(-10000, 20000),
                            appearance_probability=rng.choice([None, 0, .25, .75, 1]))
                    for p in squad())
    shuffled = list(players)
    rng.shuffle(shuffled)
    result = engine.solve_lineup(shuffled, 5)
    random_timings.append(result.elapsed_seconds)
    assert result.status == "optimal", (seed, result)
    validate_lineup_solution(players, result.solution)
    assert result.solution.score_units == exhaustive_lineup_score(players), seed


@pytest.mark.parametrize("size", [0, 14, 16])
def test_rejects_wrong_squad_size(size):
    players = (squad() + (replace(squad()[0], player_id=16),))[:size]
    with pytest.raises(ValueError, match="exactly 15"):
        engine.solve_lineup(players, 1)


def test_rejects_wrong_position_counts():
    players = (replace(squad()[0], position="MID"), *squad()[1:])
    with pytest.raises(ValueError, match="owned positions"):
        engine.solve_lineup(players, 1)


def test_rejects_duplicate_ids():
    players = (replace(squad()[0], player_id=2), *squad()[1:])
    with pytest.raises(ValueError, match="duplicate"):
        engine.solve_lineup(players, 1)


@pytest.mark.parametrize("value", [1.0, True, None, "1000", Decimal("1"), float("nan")])
def test_rejects_noninteger_m(value):
    players = (replace(squad()[0], score_m=value), *squad()[1:])
    with pytest.raises(ValueError, match="M must"):
        engine.solve_lineup(players, 1)


@pytest.mark.parametrize("excluded", [(1,), (99,)])
def test_rejects_excluded_owned_or_foreign_players(excluded):
    with pytest.raises(ValueError, match="exclude owned"):
        engine.solve_lineup(squad(), 1, excluded_player_ids=excluded)


@pytest.mark.parametrize("field,value", [("player_id", 0), ("player_id", True),
    ("club_id", -1), ("club_id", 2.5), ("position", "UNK"),
    ("appearance_probability", -0.1), ("appearance_probability", 1.1),
    ("appearance_probability", float("nan")), ("appearance_probability", float("inf")),
    ("appearance_probability", True)])
def test_rejects_invalid_player_fields(field, value):
    with pytest.raises(ValueError):
        engine.solve_lineup((replace(squad()[0], **{field: value}), *squad()[1:]), 1)


@pytest.mark.parametrize("budget", [0, -1, True, "1", None, float("inf"), float("nan")])
def test_rejects_invalid_time_budgets(budget):
    with pytest.raises(ValueError, match="time budget"):
        engine.solve_lineup(squad(), budget)


def test_constraint_xi_count(tmp_path):
    assert sum(raw_solve(squad(), tmp_path)["y"]) == 11


def test_constraint_gk_count(tmp_path):
    raw = raw_solve(squad([10000, 10000] + [1000]*13), tmp_path)
    assert sum(raw["y"][:2]) == 1


def test_constraint_def_min(tmp_path):
    raw = raw_solve(squad([1000]*2 + [-1000]*5 + [1000]*8), tmp_path)
    assert sum(raw["y"][2:7]) >= 3


def test_constraint_fwd_min(tmp_path):
    raw = raw_solve(squad([1000]*12 + [-1000]*3), tmp_path)
    assert sum(raw["y"][12:]) >= 1


def test_constraint_captain_count(tmp_path):
    assert sum(raw_solve(squad(), tmp_path)["c"]) == 1


def test_constraint_captain_starts():
    players = squad([1000, 10000] + [1000]*13)
    problem, y, c = engine._build_model(players)
    for player_id, var in y.items():
        problem += var == int(player_id in FIXED_XI)
    problem.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=2, timeMode="elapsed"))
    assert problem.status == pulp.LpStatusOptimal
    assert c[2].value() == 0, "a benched player cannot captain"


@pytest.mark.parametrize("name,position,sense,rhs", [
    ("def_max", "DEF", pulp.LpConstraintLE, 5),
    ("mid_min", "MID", pulp.LpConstraintGE, 2),
    ("mid_max", "MID", pulp.LpConstraintLE, 5),
    ("fwd_max", "FWD", pulp.LpConstraintLE, 3),
])
def test_redundant_formation_constraint_is_explicit(name, position, sense, rhs):
    # Removing these rows leaves the legal feasible set unchanged. Check the
    # actual algebra, not just their names or a solved lineup's formation.
    players = squad()
    problem, y, _ = engine._build_model(players)
    assert name in problem.constraints, f"missing explicit constraint: {name}"
    row = problem.constraints[name]
    assert dict(row.items()) == {y[p.player_id]: 1 for p in players if p.position == position}
    assert (row.sense, row.constant) == (sense, -rhs)


@pytest.mark.parametrize("group", ["y", "c"])
def test_decision_variable_domains_are_binary(group):
    _, y, c = engine._build_model(squad())
    assert all(v.isBinary() for v in {"y": y, "c": c}[group].values())


def fake_solver(monkeypatch, ps, ss, *, timed_out=False, mutate=None):
    def fake_solve(problem, solver):
        for var in problem.variables():
            if var.name.startswith("y_"):
                var.varValue = int(int(var.name[2:]) in FIXED_XI)
            elif var.name.startswith("c_"):
                var.varValue = int(var.name == "c_1")
        problem.status, problem.sol_status = ps, ss
        solver.stopped_on_time = timed_out
        if mutate:
            mutate(problem)
    monkeypatch.setattr(pulp.LpProblem, "solve", fake_solve)
    def in_process(players, deadline):
        try:
            return engine._solve_model(players, deadline, "/tmp")
        except pulp.PulpSolverError:
            return {"failure": "solver_execution_failure"}
    monkeypatch.setattr(engine, "_run_worker", in_process)


@pytest.mark.parametrize("ps,ss,timed_out,expected", [
    (1, 1, False, "optimal"), (1, 2, True, "feasible_unproven"),
    (0, 2, True, "feasible_unproven"), (0, 1, False, "feasible_unproven"),
    (-1, -1, False, "infeasible"), (0, 0, True, "timeout_no_solution"),
    (0, 0, False, "solver_failure"), (-3, 0, False, "solver_failure"),
    (-2, -2, False, "solver_failure"), (1, 0, False, "solver_failure"),
    (-1, 2, False, "solver_failure"), (0, -1, True, "timeout_no_solution"),
    (-1, 1, False, "solver_failure"), (999, 999, False, "solver_failure"),
    (-1, -1, True, "timeout_no_solution"), (1, 1, True, "feasible_unproven"),
])
def test_dual_solver_status_mapping(monkeypatch, ps, ss, timed_out, expected):
    fake_solver(monkeypatch, ps, ss, timed_out=timed_out)
    result = engine.solve_lineup(squad(), 1)
    assert result.status == expected
    assert (result.problem_status, result.solution_status) == (ps, ss)
    assert (result.solution is not None) == (expected in ("optimal", "feasible_unproven"))


@pytest.mark.parametrize("header,ps,ss,timeout", [
    ("Optimal - objective value 12000", 1, 1, False),
    ("Stopped on time - objective value 12000", 1, 2, True),
    ("Stopped on time (no integer solution - continuous used) - objective value 12000", 0, 0, True),
    ("Infeasible - objective value 12000", -1, -1, False),
    ("Stopped on iterations - objective value 12000", 1, 2, False),
])
def test_cbc_solution_header_preserves_timeout_reason(tmp_path, header, ps, ss, timeout):
    path = tmp_path / "solution.sol"
    path.write_text(header + "\n")
    solver = engine._CBC(msg=False)
    assert solver.get_status(path) == (ps, ss)
    assert solver.stopped_on_time is timeout


@pytest.mark.parametrize("name,value", [("y_1", .5), ("y_1", None), ("y_1", float("nan")),
    ("y_2", 1), ("c_2", 1), ("c_1", 0), ("c_1", .5)])
@pytest.mark.parametrize("solution_status", [1, 2])
def test_invalid_solver_decisions_are_discarded(monkeypatch, name, value, solution_status):
    fake_solver(monkeypatch, 1, solution_status,
                mutate=lambda p: setattr(p.variablesDict()[name], "varValue", value))
    result = engine.solve_lineup(squad(), 1)
    assert result.status == "solver_failure"
    assert result.solution is None
    assert result.reason == "invalid_solution"


def test_wrong_solver_objective_is_discarded(monkeypatch):
    fake_solver(monkeypatch, 1, 1)
    original = engine._run_worker
    def wrong_score(players, deadline):
        raw = original(players, deadline)
        raw["objective"] += 1
        return raw
    monkeypatch.setattr(engine, "_run_worker", wrong_score)
    result = engine.solve_lineup(squad(), 1)
    assert (result.status, result.solution, result.reason) == ("solver_failure", None, "invalid_solution")


def test_solver_unavailable(monkeypatch):
    fake_solver(monkeypatch, 1, 1)
    monkeypatch.setattr(engine._CBC, "available", lambda self: False)
    result = engine.solve_lineup(squad(), 1)
    assert (result.status, result.reason) == ("solver_failure", "solver_unavailable")


def test_solver_exception_is_a_safe_failure(monkeypatch):
    fake_solver(monkeypatch, 1, 1)
    def broken_solve(*args):
        raise pulp.PulpSolverError("synthetic private detail")
    monkeypatch.setattr(pulp.LpProblem, "solve", broken_solve)
    result = engine.solve_lineup(squad(), 1)
    assert (result.status, result.reason) == ("solver_failure", "solver_execution_failure")
    assert "synthetic private detail" not in repr(result)


def test_serial_cbc_uses_remaining_budget_and_zero_gap(monkeypatch):
    fake_solver(monkeypatch, 1, 1)
    captured = []
    original = engine._CBC
    def make_solver(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(engine, "_CBC", make_solver)
    result = engine.solve_lineup(squad(), 1)
    assert result.status == "optimal"
    assert 0 < captured[0]["timeLimit"] < 1
    assert captured[0]["timeMode"] == "elapsed"
    assert "threads" not in captured[0]
    assert captured[0]["gapRel"] == captured[0]["gapAbs"] == 0


def test_expired_budget_never_launches_cbc(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("expired budget launched a process")
    monkeypatch.setattr(engine.subprocess, "Popen", forbidden)
    result = engine.solve_lineup(squad(), 1e-12)
    assert (result.status, result.solution) == ("timeout_no_solution", None)


def test_budget_expired_during_model_build_never_launches_cbc(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("expired model-build budget launched CBC")
    monkeypatch.setattr(engine, "_CBC", forbidden)
    raw = engine._solve_model(squad(), time.monotonic() - 1, "/tmp")
    assert raw == {"problem_status": 0, "solution_status": 0, "timed_out": True}


def test_worker_launch_failure_is_reported_without_exception_details(monkeypatch):
    def failed_launch(*args, **kwargs):
        raise OSError("synthetic private path")
    monkeypatch.setattr(engine.subprocess, "Popen", failed_launch)
    result = engine.solve_lineup(squad(), 1)
    assert (result.status, result.reason) == ("solver_failure", "worker_failure")
    assert "synthetic private path" not in repr(result)


def test_hung_worker_and_child_are_stopped_within_budget(monkeypatch, tmp_path):
    original = subprocess.Popen
    pid_file = tmp_path / "child.pid"
    processes = []
    def launch(*args, **kwargs):
        script = ("import subprocess,sys,time; "
                  "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                  "open(sys.argv[1],'w').write(str(child.pid)); time.sleep(60)")
        process = original([sys.executable, "-c", script, str(pid_file)], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(engine.subprocess, "Popen", launch)
    started = time.monotonic()
    result = engine.solve_lineup(squad(), .5)
    assert result.status == "timeout_no_solution"
    assert time.monotonic() - started < 2
    assert processes[0].returncode is not None
    child_state = Path(f"/proc/{pid_file.read_text()}/stat")
    for _ in range(100):
        try:
            stopped = child_state.read_text().split()[2] == "Z"
        except (FileNotFoundError, ProcessLookupError):
            stopped = True
        if stopped:
            break
        time.sleep(.005)
    assert stopped, "child solver is still running"


def test_repeated_tied_solves_and_input_permutations_are_repeatable():
    players = tuple(replace(p, appearance_probability=[None, 0, .9][p.player_id % 3])
                    for p in squad())
    expected = solve(players)
    for seed in range(5):
        shuffled = list(players)
        random.Random(seed).shuffle(shuffled)
        assert solve(shuffled) == expected


def test_vice_and_bench_rank_score_then_appearance_then_id(monkeypatch):
    fake_solver(monkeypatch, 1, 1)
    players = tuple(replace(p, appearance_probability=0 if p.player_id in (3, 4, 6, 7) else None)
                    for p in squad())
    first = engine.solve_lineup(players, 1).solution
    assert first.vice_captain == 3  # zero beats null; lower ID breaks equal zero
    assert first.outfield_bench == (6, 7, 15)
    assert first.reserve_goalkeeper == 2
    players = tuple(replace(p, appearance_probability=1) if p.player_id in (4, 7) else p for p in players)
    second = engine.solve_lineup(players, 1).solution
    assert second.vice_captain == 4
    assert second.outfield_bench == (7, 6, 15)
    players = tuple(replace(p, score_m=1001) if p.player_id in (3, 15) else p for p in players)
    third = engine.solve_lineup(players, 1).solution
    assert third.vice_captain == 3
    assert third.outfield_bench == (15, 7, 6)


def test_disabled_stages_and_candidate_scope_are_explicit():
    result = engine.solve_lineup(squad(), 3)
    assert result.status == "optimal"
    assert result.fewer_transfers_stage == "not_applicable"
    assert result.bench_bank_stages == "not_enabled"
    assert result.eligible_candidate_count == 15
    assert result.excluded_player_ids == ()


def test_unsafe_numeric_range_cannot_be_called_optimal():
    result = engine.solve_lineup(squad([2**60]*15), 1)
    assert (result.status, result.reason) == ("solver_failure", "unsafe_numeric_range")


def valid_solution():
    return LineupSolution(FIXED_XI, 1, 12000, 3, (6, 7, 15), 2)


@pytest.mark.parametrize("change,reason", [
    ({"starting_xi": FIXED_XI[:-1]}, "eleven distinct"),
    ({"starting_xi": FIXED_XI[:-1] + (1,)}, "eleven distinct"),
    ({"starting_xi": FIXED_XI[:-1] + (99,)}, "unowned"),
    ({"starting_xi": (True,) + FIXED_XI[1:]}, "invalid ID"),
    ({"starting_xi": (2,) + FIXED_XI[:-1]}, "formation"),
    ({"captain": 2}, "captain must start"),
    ({"score_units": 11999}, "objective does not match"),
    ({"score_units": 12000.0}, "integer units"),
    ({"vice_captain": 1}, "vice-captain heuristic"),
    ({"outfield_bench": (7, 6, 15)}, "bench-order heuristic"),
    ({"outfield_bench": (6, 6, 15)}, "three outfield"),
    ({"outfield_bench": (6, 7, 14)}, "remaining owned"),
    ({"outfield_bench": (2, 7, 15), "reserve_goalkeeper": 6}, "goalkeeper must be separate"),
])
def test_independent_validator_rejects_invalid_output(change, reason):
    with pytest.raises(ValueError, match=reason):
        validate_lineup_solution(squad(), replace(valid_solution(), **change))


def test_independent_validator_accepts_valid_output():
    validate_lineup_solution(squad(), valid_solution())


def test_validator_and_oracle_have_no_model_builder_dependency():
    import ast
    import inspect
    from fpl_optimizer.services import optimization_validation
    import lineup_oracle
    for module in (optimization_validation, lineup_oracle):
        tree = ast.parse(inspect.getsource(module))
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        assert all("optimization_engine" not in ast.unparse(n) and "pulp" not in ast.unparse(n)
                   for n in imports)
