"""Explicit ordinary, chip-free transfer inputs and optional hit evidence."""

from dataclasses import dataclass

from fpl_optimizer.schemas.optimization import LineupPlayer, LineupSolution, Position, SolveStatus


@dataclass(frozen=True)
class TransferPlayer:
    player_id: int
    position: Position
    club_id: int
    score_m: int | None
    current_price_tenths: int
    season_id: str
    can_select: bool
    can_transact: bool
    removed: bool
    availability_resolved: bool  # upstream policy decision; not injury probability
    appearance_probability: float | None = None

    def lineup_player(self) -> LineupPlayer:
        return LineupPlayer(self.player_id, self.position, self.club_id,
                            self.score_m, self.appearance_probability)


@dataclass(frozen=True)
class OwnedPlayer:
    player_id: int
    selling_price_tenths: int | None  # None forces retention; never assumed income


@dataclass(frozen=True)
class TransferInputs:
    players: tuple[TransferPlayer, ...]
    owned: tuple[OwnedPlayer, ...]
    season_id: str
    bank_tenths: int
    free_transfers: int  # remaining allowance
    transfers_made: int
    locked_ids: tuple[int, ...] = ()
    excluded_ids: tuple[int, ...] = ()
    already_incurred_hit_units: int = 0  # display only; never deducted again


@dataclass(frozen=True)
class CandidateExclusion:
    player_id: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TransferSolution:
    squad: tuple[int, ...]
    lineup: LineupSolution
    bought: tuple[int, ...]
    sold: tuple[int, ...]
    transfer_count: int
    remaining_bank_tenths: int


@dataclass(frozen=True)
class TransferStage:
    # optimal describes this stage only; not_run and not_applicable are distinct.
    status: str
    solution: TransferSolution | None = None
    problem_status: int | None = None
    solution_status: int | None = None
    reason: str | None = None
    budget_seconds: float = 0
    elapsed_seconds: float = 0


@dataclass(frozen=True)
class HitComparison:
    """Scenario evidence, not an automatic recommendation.

    Net score deducts only the additional hit. Gain compares against the best
    available hold/free incumbent; offered requires that baseline to be proven.
    only_feasible_option is diagnostic and never overrides the offer gate.
    """
    primary: TransferStage
    gross_score_units: int | None
    net_score_units: int | None
    net_gain_units: int | None
    additional_hit_units: int
    already_incurred_hit_units: int
    offered: bool
    advisory: bool
    only_feasible_option: bool
    reason: str

    @property
    def gross_score_points(self) -> float | None:
        return None if self.gross_score_units is None else self.gross_score_units / 1000

    @property
    def net_score_points(self) -> float | None:
        return None if self.net_score_units is None else self.net_score_units / 1000

    @property
    def net_gain_points(self) -> float | None:
        return None if self.net_gain_units is None else self.net_gain_units / 1000


@dataclass(frozen=True)
class TransferResult:
    """Hold/free comparison and, when requested, separate hit scenario evidence."""
    status: SolveStatus  # primary comparison; see fewer_transfers_proven separately
    solution: TransferSolution | None
    selected_scenario: str | None
    hold_primary: TransferStage
    transfer_primary: TransferStage
    fewer_transfers: TransferStage
    primary_proven: bool
    fewer_transfers_proven: bool
    eligible_candidate_count: int
    excluded_candidates: tuple[CandidateExclusion, ...]
    retained_unresolved_price_ids: tuple[int, ...]
    time_budget_seconds: float
    elapsed_seconds: float
    model_version: str = "ordinary-free-transfers-v1"
    bench_bank_stages: str = "not_enabled"
    hit: HitComparison | None = None
