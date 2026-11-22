"""Explicit, immutable inputs and results for the synthetic lineup engine."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


Position = Literal["GK", "DEF", "MID", "FWD"]
SolveStatus = Literal[
    "optimal", "feasible_unproven", "infeasible", "timeout_no_solution",
    "solver_failure",
]


@dataclass(frozen=True)
class LineupPlayer:
    player_id: int
    position: Position
    club_id: int
    score_m: int
    # P(minutes > 0) for one fixture, not a joint event/DGW probability.
    appearance_probability: float | None = None


@dataclass(frozen=True)
class LineupSolution:
    starting_xi: tuple[int, ...]  # ascending IDs; not an on-pitch ordering
    captain: int
    score_units: int
    vice_captain: int
    outfield_bench: tuple[int, ...]
    reserve_goalkeeper: int

    @property
    def score_points(self) -> Decimal:
        """Exact S/1000, independent of the caller's decimal context."""
        return Decimal(f"{self.score_units}e-3")


@dataclass(frozen=True)
class LineupResult:
    status: SolveStatus
    solution: LineupSolution | None
    problem_status: int | None
    solution_status: int | None
    reason: str | None
    time_budget_seconds: float
    elapsed_seconds: float
    eligible_candidate_count: int = 15
    excluded_player_ids: tuple[int, ...] = ()
    model_version: str = "lineup-primary-v1"
    fewer_transfers_stage: str = "not_applicable"
    bench_bank_stages: str = "not_enabled"
