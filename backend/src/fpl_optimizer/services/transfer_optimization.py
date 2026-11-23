"""Hold versus ordinary free transfers, with a proven-score-preserving second stage.

The caller supplies confirmed ordinary, chip-free state after the first deadline.
No HTTP, database, async, hit, simulator, bench/bank objective or fallback model.
"""

from collections import Counter
from dataclasses import asdict, replace
import math
import time

import pulp

from fpl_optimizer.schemas.transfers import (
    CandidateExclusion, OwnedPlayer, TransferInputs, TransferPlayer,
    TransferResult, TransferSolution, TransferStage,
)
from fpl_optimizer.services import optimization_engine as runtime
from fpl_optimizer.services.transfer_validation import validate_transfer_solution


def _prepare(inputs, budget):
    if not isinstance(inputs, TransferInputs):
        raise ValueError("expected TransferInputs")
    if (type(budget) not in (int, float) or not 0 < budget <= float("1e308")
            or not math.isfinite(budget)):
        raise ValueError("time budget must be positive finite seconds")
    if not isinstance(inputs.season_id, str) or not inputs.season_id:
        raise ValueError("season ID is required")
    inputs = replace(inputs, players=tuple(inputs.players), owned=tuple(inputs.owned),
                     locked_ids=tuple(inputs.locked_ids), excluded_ids=tuple(inputs.excluded_ids))
    for value, name, maximum in ((inputs.bank_tenths, "bank", None),
                                  (inputs.free_transfers, "F", 5), (inputs.transfers_made, "T", 20)):
        if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
            raise ValueError(f"invalid {name}")
    for p in inputs.players:
        if not isinstance(p, TransferPlayer):
            raise ValueError("expected TransferPlayer")
        if type(p.player_id) is not int or p.player_id <= 0 or type(p.club_id) is not int or p.club_id <= 0:
            raise ValueError("player and club IDs must be positive integers")
        if p.position not in ("GK", "DEF", "MID", "FWD"):
            raise ValueError("invalid position")
        if p.score_m is not None and type(p.score_m) is not int:
            raise ValueError("M must be an integer or unavailable")
        if type(p.current_price_tenths) is not int or p.current_price_tenths < 0:
            raise ValueError("current prices must be nonnegative integer tenths")
        if not isinstance(p.season_id, str) or not p.season_id:
            raise ValueError("player season is required")
        if any(type(v) is not bool for v in (p.can_select, p.can_transact, p.removed, p.availability_resolved)):
            raise ValueError("eligibility decisions must be explicit booleans")
        a = p.appearance_probability
        if a is not None and (type(a) not in (int, float) or not 0 <= a <= 1):
            raise ValueError("invalid appearance probability")
    players = {p.player_id: p for p in inputs.players}
    if len(players) != len(inputs.players):
        raise ValueError("duplicate candidate IDs")
    if len(inputs.owned) != 15 or any(not isinstance(o, OwnedPlayer) for o in inputs.owned):
        raise ValueError("exactly 15 owned players required")
    if any(type(o.player_id) is not int or o.player_id not in players for o in inputs.owned):
        raise ValueError("owned player missing from candidate inputs")
    owned = {o.player_id: o.selling_price_tenths for o in inputs.owned}
    if len(owned) != 15:
        raise ValueError("duplicate owned IDs")
    if Counter(players[i].position for i in owned) != {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}:
        raise ValueError("invalid owned position counts")
    for i, price in owned.items():
        if players[i].season_id != inputs.season_id or players[i].score_m is None:
            raise ValueError("owned player needs current-season identity and prediction")
        if price is not None and (type(price) is not int or not 0 <= price <= players[i].current_price_tenths):
            raise ValueError("invalid confirmed selling price")
    for ids in (inputs.locked_ids, inputs.excluded_ids):
        if any(type(i) is not int or i not in players for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid lock/exclusion IDs")
    if set(inputs.locked_ids) & set(inputs.excluded_ids):
        raise ValueError("lock/exclude conflict")
    if any(owned[i] is None for i in set(inputs.excluded_ids) & owned.keys()):
        raise ValueError("cannot exclude unresolved-price owned player")
    exclusions = []
    active = []
    for p in sorted(inputs.players, key=lambda p: p.player_id):
        reasons = []
        if p.player_id not in owned:
            for failed, reason in (
                (p.season_id != inputs.season_id, "wrong_season"),
                (not p.can_select, "cannot_select"), (not p.can_transact, "cannot_transact"),
                (p.removed, "removed"), (p.score_m is None, "missing_prediction"),
                (not p.availability_resolved, "unresolved_availability"),
            ):
                if failed:
                    reasons.append(reason)
        if reasons and p.player_id in inputs.locked_ids:
            raise ValueError("locked candidate is not purchase eligible")
        if not reasons:
            active.append(p)
        if p.player_id in inputs.excluded_ids:
            reasons.append("user_excluded")
        if reasons:
            exclusions.append(CandidateExclusion(p.player_id, tuple(reasons)))
    return inputs, tuple(active), tuple(exclusions)


def _build_transfer_model(inputs, players, fixed_score=None):
    """Ownership is fixed data; incoming/outgoing counts are derived from x."""
    problem = pulp.LpProblem("free_transfers", pulp.LpMaximize if fixed_score is None else pulp.LpMinimize)
    x = {p.player_id: pulp.LpVariable(f"x_{p.player_id}", cat="Binary") for p in players}
    y = {p.player_id: pulp.LpVariable(f"y_{p.player_id}", cat="Binary") for p in players}
    c = {p.player_id: pulp.LpVariable(f"c_{p.player_id}", cat="Binary") for p in players}
    owned = {o.player_id: o.selling_price_tenths for o in inputs.owned}
    n = pulp.lpSum(x[i] for i in x if i not in owned)
    score = pulp.lpSum(p.score_m * (y[p.player_id] + c[p.player_id]) for p in players)
    bank = (inputs.bank_tenths + pulp.lpSum(price * (1-x[i]) for i, price in owned.items() if price is not None)
            - pulp.lpSum(p.current_price_tenths * x[p.player_id] for p in players if p.player_id not in owned))
    problem += score if fixed_score is None else n
    problem += pulp.lpSum(x.values()) == 15, "squad_count"
    for pos, count in (("GK", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)):
        problem += pulp.lpSum(x[p.player_id] for p in players if p.position == pos) == count, f"squad_{pos}"
    problem += pulp.lpSum(y.values()) == 11, "xi_count"
    by_pos = {pos: pulp.lpSum(y[p.player_id] for p in players if p.position == pos)
              for pos in ("GK", "DEF", "MID", "FWD")}
    problem += by_pos["GK"] == 1, "gk_count"
    problem += by_pos["DEF"] >= 3, "def_min"
    problem += by_pos["DEF"] <= 5, "def_max"
    problem += by_pos["MID"] >= 2, "mid_min"
    problem += by_pos["MID"] <= 5, "mid_max"
    problem += by_pos["FWD"] >= 1, "fwd_min"
    problem += by_pos["FWD"] <= 3, "fwd_max"
    problem += pulp.lpSum(c.values()) == 1, "captain_count"
    for i in x:
        problem += y[i] <= x[i], f"starter_owned_{i}"
        problem += c[i] <= y[i], f"captain_starts_{i}"
    problem += bank >= 0, "money"
    problem += n >= 1, "positive_transfers"
    problem += n <= inputs.free_transfers, "free_cap"
    problem += inputs.transfers_made + n <= 20, "ordinary_cap"
    for club in sorted({p.club_id for p in players}):
        problem += pulp.lpSum(x[p.player_id] for p in players if p.club_id == club) <= 3, f"club_{club}"
    for i in inputs.locked_ids:
        problem += x[i] == 1, f"lock_{i}"
    for i in set(inputs.excluded_ids) & x.keys():
        problem += x[i] == 0, f"exclude_{i}"
    for i, price in owned.items():
        if price is None:
            problem += x[i] == 1, f"unresolved_{i}"
    if fixed_score is not None:
        problem += score == fixed_score, "fixed_primary_score"
    return problem, x, y, c, score, n, bank


def _solve_stage_worker(request):
    data = request["inputs"]
    inputs = TransferInputs(**{**data, "players": tuple(TransferPlayer(**p) for p in data["players"]),
                               "owned": tuple(OwnedPlayer(**p) for p in data["owned"])})
    players = tuple(TransferPlayer(**p) for p in request["active_players"])
    problem, x, y, c, score, n, bank = _build_transfer_model(inputs, players, request["fixed_score"])
    if not runtime._numeric_export_is_exact(problem):
        return {"failure": "unsafe_numeric_range"}
    remaining = request["deadline"] - time.monotonic()
    if remaining <= 0:
        return {"problem_status": 0, "solution_status": 0, "timed_out": True}
    solver = runtime._CBC(msg=False, timeLimit=remaining, timeMode="elapsed", gapRel=0, gapAbs=0,
                          options=["randomSeed 1", "randomCbcSeed 1"])
    solver.tmpDir = request["scratch"]
    if not solver.available():
        return {"failure": "solver_unavailable"}
    problem.solve(solver)
    def value(expression):
        # PuLP can attach an unset zero-coefficient dummy to a constant objective.
        return pulp.value(expression) if any(expression.values()) else expression.constant
    return {"problem_status": problem.status, "solution_status": problem.sol_status,
            "timed_out": solver.stopped_on_time,
            "x": [x[p.player_id].value() for p in players],
            "y": [y[p.player_id].value() for p in players],
            "c": [c[p.player_id].value() for p in players],
            "score": value(score), "transfers": value(n), "bank": value(bank),
            "objective": value(problem.objective)}


def _integer(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value != int(value):
        raise ValueError("invalid integer solver value")
    return int(value)


def _decode_solution(inputs, players, raw, fixed_score):
    xs = runtime._binary_values(raw["x"], len(players))
    ys = runtime._binary_values(raw["y"], len(players))
    cs = runtime._binary_values(raw["c"], len(players))
    if any((y or c) and not x for x, y, c in zip(xs, ys, cs, strict=True)):
        raise ValueError("unowned starting or captain decision")
    selected = tuple(p for p, x in zip(players, xs, strict=True) if x)
    lineup = runtime._read_solution(tuple(p.lineup_player() for p in selected), {
        "y": [y for x, y in zip(xs, ys, strict=True) if x],
        "c": [c for x, c in zip(xs, cs, strict=True) if x], "objective": raw["score"]})
    owned_ids = {o.player_id for o in inputs.owned}
    selected_ids = tuple(p.player_id for p in selected)
    solution = TransferSolution(selected_ids, lineup,
        tuple(sorted(set(selected_ids) - owned_ids)), tuple(sorted(owned_ids - set(selected_ids))),
        _integer(raw["transfers"]), _integer(raw["bank"]))
    validate_transfer_solution(inputs, solution, scenario="transfers", fixed_score=fixed_score)
    expected = lineup.score_units if fixed_score is None else solution.transfer_count
    if _integer(raw["objective"]) != expected:
        raise ValueError("stage objective does not match solution")
    return solution


def _transfer_stage(inputs, players, deadline, fixed_score=None):
    started = time.monotonic()
    remaining = max(0, deadline - started)
    try:
        raw = runtime._run_problem({"mode": "transfers", "inputs": asdict(inputs),
            "active_players": [asdict(p) for p in players], "fixed_score": fixed_score}, deadline)
        if "failure" in raw:
            return TransferStage("solver_failure", reason=raw["failure"], budget_seconds=remaining,
                                 elapsed_seconds=time.monotonic()-started)
        ps, ss = raw["problem_status"], raw["solution_status"]
        status = runtime._status(ps, ss, raw.get("timed_out", False))
        solution = None
        if status in ("optimal", "feasible_unproven"):
            try:
                solution = _decode_solution(inputs, players, raw, fixed_score)
            except (ValueError, KeyError, TypeError, OverflowError, StopIteration):
                return TransferStage("solver_failure", problem_status=ps, solution_status=ss,
                    reason="invalid_solution", budget_seconds=remaining, elapsed_seconds=time.monotonic()-started)
        return TransferStage(status, solution, ps, ss, runtime._status_reason(raw, status),
                             remaining, time.monotonic()-started)
    except (OSError, ValueError, TypeError, KeyError):
        return TransferStage("solver_failure", reason="worker_failure", budget_seconds=remaining,
                             elapsed_seconds=time.monotonic()-started)


def _hold_stage(inputs, players, deadline):
    started = time.monotonic()
    owned = {o.player_id for o in inputs.owned}
    if set(inputs.locked_ids) - owned or set(inputs.excluded_ids) & owned:
        return TransferStage("infeasible", reason="hold_conflicts_with_preferences")
    remaining = deadline - started
    if remaining <= 0:
        return TransferStage("timeout_no_solution", reason="budget_exhausted")
    selected = tuple(p.lineup_player() for p in players if p.player_id in owned)
    try:
        raw = runtime._run_worker(selected, deadline)
        if "failure" in raw:
            return TransferStage("solver_failure", reason=raw["failure"], budget_seconds=remaining,
                                 elapsed_seconds=time.monotonic()-started)
        ps, ss = raw["problem_status"], raw["solution_status"]
        status = runtime._status(ps, ss, raw.get("timed_out", False))
        solution = None
        if status in ("optimal", "feasible_unproven"):
            try:
                lineup = runtime._read_solution(selected, raw)
                solution = TransferSolution(tuple(sorted(owned)), lineup, (), (), 0, inputs.bank_tenths)
                validate_transfer_solution(inputs, solution, scenario="hold")
            except (ValueError, KeyError, TypeError, OverflowError, StopIteration):
                return TransferStage("solver_failure", problem_status=ps, solution_status=ss,
                                     reason="invalid_solution", budget_seconds=remaining,
                                     elapsed_seconds=time.monotonic()-started)
        return TransferStage(status, solution, ps, ss, runtime._status_reason(raw, status),
                             remaining, time.monotonic()-started)
    except (OSError, ValueError, TypeError, KeyError):
        return TransferStage("solver_failure", reason="worker_failure", budget_seconds=remaining,
                             elapsed_seconds=time.monotonic()-started)


def solve_transfers(inputs: TransferInputs, time_budget_seconds: float) -> TransferResult:
    """One deadline for hold primary, transfer primary, then fewer transfers.

    Stage-one solutions live in the parent, outside each bounded solver worker.
    A failed/expired second stage cannot erase the proven primary solution.
    """
    started = time.monotonic()
    inputs, players, exclusions = _prepare(inputs, time_budget_seconds)
    deadline = started + time_budget_seconds
    # Bound original integer inputs before PuLP converts any compound constant.
    magnitudes = [inputs.bank_tenths, *(abs(p.score_m) * 2 for p in players),
                  *(p.current_price_tenths for p in players),
                  *(o.selling_price_tenths for o in inputs.owned if o.selling_price_tenths is not None)]
    unsafe = sum(magnitudes) > 2**53 - 1
    hold = _hold_stage(inputs, players, deadline)
    if inputs.free_transfers == 0 or inputs.transfers_made == 20:
        free = TransferStage("infeasible", reason="no_positive_transfer_allowance")
    elif unsafe:
        free = TransferStage("solver_failure", reason="unsafe_numeric_range")
    else:
        free = _transfer_stage(inputs, players, deadline)
    fewer = TransferStage("not_run", reason="primary_not_proven")
    free_solution = free.solution
    if free.status == "infeasible":
        fewer = TransferStage("not_applicable", reason="transfer_scenario_infeasible")
    elif free.status == "optimal":
        if time.monotonic() >= deadline:
            fewer = TransferStage("not_run", reason="budget_exhausted")
        else:
            fewer = _transfer_stage(inputs, players, deadline, free.solution.lineup.score_units)
            if fewer.status == "infeasible":
                # The saved primary solution is a witness that fixed S is feasible.
                fewer = replace(fewer, status="solver_failure", reason="contradicts_primary_witness")
            if fewer.solution is not None:
                if fewer.solution.transfer_count <= free.solution.transfer_count:
                    free_solution = fewer.solution
                elif fewer.status == "optimal":
                    fewer = replace(fewer, status="solver_failure", solution=None,
                                    reason="worse_than_primary_witness")
                # A legitimate unproven incumbent may use more transfers than
                # the saved solution. Keep the better saved result and its proof.
    available = [("hold", hold.solution), ("transfers", free_solution)]
    available = [(name, solution) for name, solution in available if solution is not None]
    scenario, chosen = max(available, key=lambda item: (
        item[1].lineup.score_units, -item[1].transfer_count)) if available else (None, None)
    proven = bool(chosen) and all(stage.status in ("optimal", "infeasible") for stage in (hold, free))
    fewer_proven = proven and (scenario == "hold" or fewer.status == "optimal")
    if chosen is not None:
        status = "optimal" if proven else "feasible_unproven"
    elif hold.status == free.status == "infeasible":
        status = "infeasible"
    elif "solver_failure" in (hold.status, free.status):
        status = "solver_failure"
    else:
        status = "timeout_no_solution"
    eligible = sum(p.player_id not in inputs.excluded_ids for p in players)
    retained = tuple(sorted(o.player_id for o in inputs.owned if o.selling_price_tenths is None))
    return TransferResult(status, chosen, scenario, hold, free, fewer, proven, fewer_proven,
        eligible, exclusions, retained, time_budget_seconds, time.monotonic()-started)
