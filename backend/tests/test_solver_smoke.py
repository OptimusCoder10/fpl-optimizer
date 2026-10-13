"""Actual CBC proof and regressions for unproven/invalid solver outputs."""

import asyncio
from itertools import product
from pathlib import Path
import sys

import pulp
import pytest

from fpl_optimizer.services import solver_smoke


def test_actual_cbc_proves_the_exhaustively_known_optimum():
    feasible = [
        (sum(v * x for v, x in zip((6, 4, 3), choices, strict=True)), choices)
        for choices in product((0, 1), repeat=3)
        if sum(w * x for w, x in zip((4, 3, 2), choices, strict=True)) <= 5
    ]
    optimum = max(feasible)
    proof = solver_smoke.solve_knapsack()
    assert proof.status == "optimal"
    assert (proof.objective, proof.selection) == optimum == (7, (0, 1, 1))


@pytest.mark.parametrize("status,solution_status", [
    (pulp.LpStatusOptimal, pulp.LpSolutionIntegerFeasible),
    (pulp.LpStatusNotSolved, pulp.LpSolutionNoSolutionFound),
    (pulp.LpStatusInfeasible, pulp.LpSolutionInfeasible),
])
def test_unproven_status_is_never_accepted(monkeypatch, status, solution_status):
    def fake_solve(problem, solver):
        problem.assignStatus(status, solution_status)
    monkeypatch.setattr(pulp.LpProblem, "solve", fake_solve)
    with pytest.raises(RuntimeError, match="did not prove optimality"):
        solver_smoke.solve_knapsack()


def test_invalid_optimal_output_is_discarded(monkeypatch):
    def fake_solve(problem, solver):
        problem.assignStatus(pulp.LpStatusOptimal, pulp.LpSolutionOptimal)
        for variable in problem.variables():
            variable.varValue = 1
    monkeypatch.setattr(pulp.LpProblem, "solve", fake_solve)
    with pytest.raises(RuntimeError, match="invalid proof"):
        solver_smoke.solve_knapsack()


def test_missing_solver_is_rejected(monkeypatch):
    monkeypatch.setattr(pulp.PULP_CBC_CMD, "available", lambda self: False)
    with pytest.raises(RuntimeError, match="unavailable"):
        solver_smoke.solve_knapsack()


async def test_solver_probe_runs_outside_the_event_loop():
    task = asyncio.create_task(solver_smoke.check_solver())
    ticks = 0
    while not task.done():
        await asyncio.sleep(0.001)
        ticks += 1
    assert await task == solver_smoke.SolverProof("optimal", 7, (0, 1, 1))
    assert ticks > 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_outer_limit_and_cancellation_kill_the_worker_and_child(tmp_path, monkeypatch, cancel):
    """Use an actual hanging process tree; do not wait for CBC's internal limit."""
    pid_file = tmp_path / "child.pid"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []
    async def start_worker(*args, **kwargs):
        process = await spawn(sys.executable, str(worker), **kwargs)
        processes.append(process)
        started.set()
        return process
    monkeypatch.setattr(solver_smoke.asyncio, "create_subprocess_exec", start_worker)
    monkeypatch.setattr(solver_smoke, "PROOF_WALL_LIMIT_SECONDS", 0.5)
    task = asyncio.create_task(solver_smoke.check_solver())
    await started.wait()
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.005)
    assert pid_file.exists()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await asyncio.wait_for(task, 2) is None
    assert processes[0].returncode is not None
    child_status = Path(f"/proc/{pid_file.read_text()}/stat")
    for _ in range(100):
        if not child_status.exists() or child_status.read_text().split()[2] == "Z":
            break
        await asyncio.sleep(0.005)
    assert not child_status.exists() or child_status.read_text().split()[2] == "Z"
