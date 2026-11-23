"""Regression for model-export precision and confirmed-timeout precedence."""

from decimal import Decimal

import pulp
import pytest

from fpl_optimizer.services import optimization_engine as engine
from test_optimization_engine import fake_solver, squad


@pytest.mark.parametrize("base", [10**13, 10**14])
def test_unexportable_integer_scores_never_claim_optimal(base):
    result = engine.solve_lineup(squad([base, base+1] + [0]*13), 3)
    assert (result.status, result.solution, result.reason) == (
        "solver_failure", None, "unsafe_numeric_range")


@pytest.mark.parametrize("place", ["objective", "coefficient", "rhs"])
def test_actual_mps_precision_guard_covers_every_expression(tmp_path, place):
    variable = pulp.LpVariable("x", cat="Binary")
    problem = pulp.LpProblem("precision", pulp.LpMaximize)
    large = 10**13 + 1
    problem += (large if place == "objective" else 1) * variable
    problem += (large if place == "coefficient" else 1) * variable <= (
        large if place == "rhs" else 1)
    path = tmp_path / "model.mps"
    problem.writeMPS(str(path))
    assert "1.000000000000e+13" in path.read_text()
    assert Decimal("1.000000000000e+13") != large
    assert not engine._numeric_export_is_exact(problem)


def test_exact_large_values_are_not_arbitrarily_capped():
    problem = pulp.LpProblem("exact", pulp.LpMaximize)
    variable = pulp.LpVariable("x", cat="Binary")
    problem += 10**13 * variable
    problem += variable <= 1
    assert engine._numeric_export_is_exact(problem)


def test_precision_failure_precedes_solver_launch(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unsafe coefficients reached CBC")
    monkeypatch.setattr(engine, "_CBC", forbidden)
    assert engine._solve_model(squad([10**13, 10**13+1]+[0]*13), 1e20, "/tmp") == {
        "failure": "unsafe_numeric_range"}


def test_confirmed_timeout_with_infeasible_codes_keeps_diagnostics(monkeypatch):
    fake_solver(monkeypatch, -1, -1, timed_out=True)
    result = engine.solve_lineup(squad(), 1)
    assert result.status == "timeout_no_solution"
    assert result.solution is None
    assert (result.problem_status, result.solution_status) == (-1, -1)
    assert result.reason == "timeout_status_conflict"


@pytest.mark.parametrize("invalid", [False, True])
def test_timeout_claimed_incumbent_is_independently_validated(monkeypatch, invalid):
    fake_solver(monkeypatch, -1, 2, timed_out=True,
                mutate=(lambda p: setattr(p.variablesDict()["y_1"], "varValue", .5))
                if invalid else None)
    result = engine.solve_lineup(squad(), 1)
    assert result.status == ("solver_failure" if invalid else "feasible_unproven")
    assert (result.solution is None) == invalid
    assert result.reason == ("invalid_solution" if invalid else "timeout_status_conflict")
