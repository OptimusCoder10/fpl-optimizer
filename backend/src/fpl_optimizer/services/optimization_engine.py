"""Fixed-squad XI/captain MILP, using explicit synthetic inputs only.

No application state, network, database or async work. CBC runs serially in a
bounded child process. Application admission/cancellation remains Phase 4 work.
Only the primary objective is enabled; repeatability is scoped to the pinned
solver, not canonical choices among tied optima across versions or timeouts.
The caller retains unrounded predictions for later presentation (§04E).
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import json
import math
import os
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import pulp

from fpl_optimizer.schemas.optimization import LineupPlayer, LineupResult, LineupSolution
from fpl_optimizer.services.optimization_validation import validate_lineup_solution


WORKER_MODULE = "fpl_optimizer.services.optimization_engine"


def prediction_to_m(prediction: Decimal | float | int | str) -> int:
    """Round the event mean once, half-up to .001, then convert to units."""
    if isinstance(prediction, bool) or not isinstance(prediction, (Decimal, float, int, str)):
        raise ValueError("prediction must be a finite decimal number")
    try:
        value = Decimal(str(prediction))
        if not value.is_finite():
            raise ValueError("prediction must be finite")
        with localcontext() as context:
            context.prec = max(28, len(value.as_tuple().digits), value.adjusted() + 5)
            rounded = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            return int(rounded * 1000)
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("prediction must be a finite decimal number") from exc


def _validate_inputs(players, time_budget_seconds, excluded_player_ids):
    if len(players) != 15:
        raise ValueError("lineup requires exactly 15 owned players")
    for player in players:
        if not isinstance(player, LineupPlayer):
            raise ValueError("each owned player must be a LineupPlayer")
        if type(player.player_id) is not int or player.player_id <= 0:
            raise ValueError("player ID must be a positive integer")
        if type(player.club_id) is not int or player.club_id <= 0:
            raise ValueError("club ID must be a positive integer")
        if type(player.score_m) is not int:
            raise ValueError("M must be an integer, not a boolean")
        if player.position not in ("GK", "DEF", "MID", "FWD"):
            raise ValueError("owned positions must be GK / DEF / MID / FWD")
        probability = player.appearance_probability
        if probability is not None and (
            type(probability) not in (int, float) or not 0 <= probability <= 1
            or not math.isfinite(probability)
        ):
            raise ValueError("appearance probability must be null or finite in [0, 1]")
    if len({p.player_id for p in players}) != 15:
        raise ValueError("duplicate player IDs")
    if Counter(p.position for p in players) != {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}:
        raise ValueError("owned positions must be 2 GK / 5 DEF / 5 MID / 3 FWD")
    if excluded_player_ids:
        raise ValueError("lineup cannot exclude owned players or accept foreign exclusions")
    if (type(time_budget_seconds) not in (int, float)
            or not math.isfinite(time_budget_seconds) or time_budget_seconds <= 0):
        raise ValueError("time budget must be positive finite seconds")


def _build_model(players):
    """Caller supplies a validated, ID-sorted fixed squad; x=o=1 implicitly."""
    problem = pulp.LpProblem("lineup", pulp.LpMaximize)
    y = {p.player_id: pulp.LpVariable(f"y_{p.player_id}", cat="Binary") for p in players}
    c = {p.player_id: pulp.LpVariable(f"c_{p.player_id}", cat="Binary") for p in players}
    problem += pulp.lpSum(p.score_m * (y[p.player_id] + c[p.player_id]) for p in players)
    by_position = {pos: pulp.lpSum(y[p.player_id] for p in players if p.position == pos)
                   for pos in ("GK", "DEF", "MID", "FWD")}
    problem += pulp.lpSum(y.values()) == 11, "xi_count"
    problem += by_position["GK"] == 1, "gk_count"
    problem += by_position["DEF"] >= 3, "def_min"
    problem += by_position["DEF"] <= 5, "def_max"
    problem += by_position["MID"] >= 2, "mid_min"
    problem += by_position["MID"] <= 5, "mid_max"
    problem += by_position["FWD"] >= 1, "fwd_min"
    problem += by_position["FWD"] <= 3, "fwd_max"
    problem += pulp.lpSum(c.values()) == 1, "captain_count"
    for player in players:
        problem += c[player.player_id] <= y[player.player_id], f"captain_starts_{player.player_id}"
    return problem, y, c


class _CBC(pulp.PULP_CBC_CMD):
    """Keep the stop reason that PuLP's pair of status codes alone loses."""
    stopped_on_time = False

    def get_status(self, filename):
        with open(filename) as source:
            self.stopped_on_time = source.readline().startswith("Stopped on time")
        return super().get_status(filename)


def _solve_model(players, deadline, scratch_directory):
    problem, y, c = _build_model(players)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"problem_status": 0, "solution_status": 0, "timed_out": True}
    solver = _CBC(
        msg=False, timeLimit=remaining, timeMode="elapsed", gapRel=0, gapAbs=0,
        options=["randomSeed 1", "randomCbcSeed 1"],
    )  # No threads argument: retain the repository's default serial setup.
    solver.tmpDir = scratch_directory
    if not solver.available():
        return {"failure": "solver_unavailable"}
    problem.solve(solver)
    objective = pulp.value(problem.objective)
    # PuLP's zero-objective dummy can have no value even after an optimal solve.
    if objective is None and all(p.score_m == 0 for p in players):
        objective = 0
    return {
        "problem_status": problem.status, "solution_status": problem.sol_status,
        "timed_out": solver.stopped_on_time, "objective": objective,
        "y": [y[p.player_id].value() for p in players],
        "c": [c[p.player_id].value() for p in players],
    }


def _run_worker(players, deadline):
    # TemporaryDirectory is owned by the parent, so even a killed CBC leaves no
    # model/solution files. The whole worker/CBC process group is always reaped.
    with TemporaryDirectory(prefix="fpl-lineup-") as scratch:
        payload = json.dumps({"players": [asdict(p) for p in players],
                              "deadline": deadline, "scratch": scratch})
        if time.monotonic() >= deadline:
            return {"problem_status": 0, "solution_status": 0, "timed_out": True}
        process = subprocess.Popen(
            [sys.executable, "-m", WORKER_MODULE, "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True,
        )
        try:
            output, _ = process.communicate(payload, timeout=max(0, deadline - time.monotonic()))
            if process.returncode != 0:
                return {"failure": "worker_failure"}
            return json.loads(output)
        except subprocess.TimeoutExpired:
            return {"problem_status": 0, "solution_status": 0, "timed_out": True}
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


def _status(problem_status, solution_status, timed_out):
    if timed_out and (problem_status == pulp.LpStatusInfeasible
                      or solution_status == pulp.LpSolutionInfeasible):
        return "solver_failure"  # Contradictory stop evidence cannot prove infeasibility.
    if (problem_status == pulp.LpStatusOptimal and solution_status == pulp.LpSolutionOptimal
            and not timed_out):
        return "optimal"
    if problem_status in (pulp.LpStatusOptimal, pulp.LpStatusNotSolved) and solution_status in (
        pulp.LpSolutionIntegerFeasible, pulp.LpSolutionOptimal,
    ):
        return "feasible_unproven"
    if (problem_status == pulp.LpStatusInfeasible
            and solution_status == pulp.LpSolutionInfeasible):
        return "infeasible"
    if (problem_status == pulp.LpStatusNotSolved
            and solution_status == pulp.LpSolutionNoSolutionFound and timed_out):
        return "timeout_no_solution"
    return "solver_failure"


def _binary_values(values, count):
    if not isinstance(values, list) or len(values) != count:
        raise ValueError("missing decision values")
    # Do not silently round a fractional relaxation or missing incumbent.
    if any(type(v) not in (int, float) or v not in (0, 1) for v in values):
        raise ValueError("nonbinary decision value")
    return values


def _read_solution(players, raw):
    starts = _binary_values(raw["y"], len(players))
    captains = _binary_values(raw["c"], len(players))
    if sum(captains) != 1:
        raise ValueError("invalid captain count")
    objective = raw["objective"]
    if (type(objective) not in (int, float) or not math.isfinite(objective)
            or objective != int(objective)):
        raise ValueError("invalid objective")
    xi = tuple(p.player_id for p, selected in zip(players, starts, strict=True) if selected)
    captain = next(p.player_id for p, selected in zip(players, captains, strict=True) if selected)
    def rank(p):
        return (-p.score_m, p.appearance_probability is None,
                -p.appearance_probability if p.appearance_probability is not None else 0,
                p.player_id)
    vice = min((p for p in players if p.player_id in xi and p.player_id != captain), key=rank)
    outfield = sorted((p for p in players if p.player_id not in xi and p.position != "GK"), key=rank)
    goalkeeper = next(p for p in players if p.player_id not in xi and p.position == "GK")
    solution = LineupSolution(xi, captain, int(objective), vice.player_id,
                              tuple(p.player_id for p in outfield), goalkeeper.player_id)
    validate_lineup_solution(players, solution)
    return solution


def solve_lineup(
    players: Sequence[LineupPlayer], time_budget_seconds: float,
    *, excluded_player_ids: Sequence[int] = (),
) -> LineupResult:
    """Solve one primary stage within the caller's remaining wall-time budget.

    Invalid inputs raise ValueError. Solver failures are safe domain results,
    never raw exceptions/logs. Elapsed time includes startup and validation;
    process cleanup may take a small amount of time beyond the solve deadline.
    """
    started = time.monotonic()
    players = tuple(players)
    _validate_inputs(players, time_budget_seconds, excluded_player_ids)
    players = tuple(sorted(players, key=lambda p: p.player_id))
    def result(status, solution=None, problem_status=None, solution_status=None, reason=None):
        return LineupResult(status, solution, problem_status, solution_status, reason,
                            time_budget_seconds, time.monotonic() - started)
    # CBC uses doubles. Never claim exact integer optimality outside the range
    # where all possible objective sums can be represented exactly.
    if sum(abs(p.score_m) for p in players) + max(abs(p.score_m) for p in players) > 2**53 - 1:
        return result("solver_failure", reason="unsafe_numeric_range")
    try:
        raw = _run_worker(players, started + time_budget_seconds)
        if "failure" in raw:
            return result("solver_failure", reason=raw["failure"])
        ps, ss = raw["problem_status"], raw["solution_status"]
        status = _status(ps, ss, raw.get("timed_out", False))
        solution = None
        if status in ("optimal", "feasible_unproven"):
            try:
                solution = _read_solution(players, raw)
            except (KeyError, TypeError, ValueError, StopIteration, OverflowError):
                return result("solver_failure", problem_status=ps, solution_status=ss,
                              reason="invalid_solution")
        return result(status, solution, ps, ss,
                      "unrecognized_solver_status" if status == "solver_failure" else None)
    except (OSError, ValueError, TypeError, KeyError):
        return result("solver_failure", reason="worker_failure")


def _worker_main():
    try:
        request = json.load(sys.stdin)
        players = tuple(LineupPlayer(**p) for p in request["players"])
        output = _solve_model(players, request["deadline"], request["scratch"])
    except Exception:
        output = {"failure": "solver_execution_failure"}
    print(json.dumps(output))


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker_main()
