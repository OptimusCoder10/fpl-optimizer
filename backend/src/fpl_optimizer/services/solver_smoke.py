"""Synthetic CBC runtime proof; no database reads or HTTP requests."""

import asyncio
from dataclasses import asdict, dataclass
import json
import os
import signal
import sys

import pulp


CBC_TIME_LIMIT_SECONDS = 2
PROOF_WALL_LIMIT_SECONDS = 5
WORKER_MODULE = "fpl_optimizer.services.solver_smoke"


@dataclass(frozen=True)
class SolverProof:
    status: str
    objective: int
    selection: tuple[int, int, int]


def solve_knapsack() -> SolverProof:
    """Prove the unique optimum (0, 1, 1), value 7, weight 5."""
    solver = pulp.PULP_CBC_CMD(
        # Leave CBC in its default serial mode. This bundled build can stall
        # when explicitly starting its internal thread even for this tiny model.
        msg=False, timeLimit=CBC_TIME_LIMIT_SECONDS, timeMode="elapsed"
    )
    if not solver.available():
        raise RuntimeError("CBC unavailable")
    problem = pulp.LpProblem("runtime_proof", pulp.LpMaximize)
    choices = [pulp.LpVariable(f"item_{i}", cat="Binary") for i in range(3)]
    problem += pulp.lpDot((6, 4, 3), choices)
    problem += pulp.lpDot((4, 3, 2), choices) <= 5
    problem.solve(solver)
    # PuLP may label a time-limited incumbent Optimal at the problem level.
    # Require its separate solution status to establish a proven optimum too.
    if (
        problem.status != pulp.LpStatusOptimal
        or problem.sol_status != pulp.LpSolutionOptimal
    ):
        raise RuntimeError(
            f"CBC did not prove optimality ({problem.status}, {problem.sol_status})"
        )
    values = tuple(choice.value() for choice in choices)
    if (
        values != (0, 1, 1)
        or pulp.value(problem.objective) != 7
        or sum(w * v for w, v in zip((4, 3, 2), values, strict=True)) > 5
    ):
        raise RuntimeError("CBC returned an invalid proof")
    return SolverProof("optimal", 7, (0, 1, 1))


async def check_solver() -> SolverProof | None:
    """Run once off the event loop; kill the worker and CBC on timeout/cancel.

    Render is Linux: a new process group lets cancellation reap CBC as well as
    its PuLP parent. The CBC budget alone cannot bound a hung executable.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", WORKER_MODULE, "--worker",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(PROOF_WALL_LIMIT_SECONDS):
            output, _ = await process.communicate()
        if process.returncode != 0:
            return None
        expected = SolverProof("optimal", 7, (0, 1, 1))
        if json.loads(output) != {**asdict(expected), "selection": [0, 1, 1]}:
            return None
        return expected
    except (TimeoutError, ValueError):
        return None
    finally:
        # The process group might still contain CBC after the worker exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def main() -> int:
    """Build/start proof command emits only synthetic results or a safe failure."""
    try:
        proof = (
            solve_knapsack()
            if sys.argv[1:] == ["--worker"] else asyncio.run(check_solver())
        )
        if proof is None:
            raise RuntimeError("CBC runtime proof unavailable")
        print(json.dumps(asdict(proof)))
        return 0
    except Exception:
        print('{"status": "unavailable"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
