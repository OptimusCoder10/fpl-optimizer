"""Development-only retrospective folds; no final-period evaluation entry point."""

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from fpl_optimizer.schemas.prior_archive import (
    ARCHIVE_RULES, SCORING_ADAPTER, load_pinned_archive,
)
from fpl_optimizer.services.prediction_engine import (
    MODEL_VERSION, FixtureObservation, predict,
)
from fpl_optimizer.services.opponent_adjustment import adjust_prediction
from fpl_optimizer.services.opponent_profiles import SETTINGS as OPPONENT_SETTINGS
from fpl_optimizer.services.prior_selection import REQUIRED_COUNTS, select_prior
from fpl_optimizer.services.retrospective_archive import (
    DEVELOPMENT_LAST_EVENT, ArchiveCounts, ScoredFixture, adapt_validated_archive,
    eligible_scored_fixtures,
)
from fpl_optimizer.services.retrospective_benchmark import (
    ModelMinuteRecord, PlayedFixtureMinutes, PointMetricRecord,
    expanding_personal_mean_baseline, gameweek_mae,
    latest_six_personal_mean_baseline, paired_gameweek_mae_difference,
    position_mean_baseline, summarize_model_minutes, summarize_points, zero_baseline,
)
from fpl_optimizer.services.retrospective_cohort import (
    BenchmarkFixture, BenchmarkRow, select_cohort,
)


DEVELOPMENT_FOLDS = ((6, 10), (11, 15), (16, 19))
BASELINE_NAMES = ("zero", "position_mean", "personal_mean", "latest_six")
REPORT_VERSION = "development-fold-v1"


@dataclass(frozen=True)
class DevelopmentView:
    """Only scored-development data, with the full catalog player universe."""

    rows: tuple[BenchmarkRow, ...]
    fixtures: tuple[BenchmarkFixture, ...]
    counts: ArchiveCounts
    player_ids: tuple[int, ...]
    scored_by_event: dict[int, tuple[ScoredFixture, ...]] | None = None

    def __post_init__(self) -> None:
        if (not self.player_ids or any(row.event < 1 or row.event > DEVELOPMENT_LAST_EVENT
                for row in self.rows) or any(
                    fixture.event < 1 or fixture.event > DEVELOPMENT_LAST_EVENT
                    for fixture in self.fixtures
                )):
            raise ValueError("development view requires player IDs and GW1–19 only")
        if self.scored_by_event is not None and (
            any(not 6 <= event <= DEVELOPMENT_LAST_EVENT
                for event in self.scored_by_event)
            or any(fixture.event > DEVELOPMENT_LAST_EVENT
                   for fixtures in self.scored_by_event.values() for fixture in fixtures)
        ):
            raise ValueError("development opponent scores require GW1–19 only")

    @classmethod
    def load(cls, directory: Path) -> "DevelopmentView":
        """Validate the pin once, then project only the fixed development ceiling."""
        source = load_pinned_archive(directory)
        mapped = adapt_validated_archive(source, through_event=DEVELOPMENT_LAST_EVENT)
        return cls(mapped.rows, mapped.fixtures, mapped.counts,
                   tuple(sorted(player.id for player in source.catalog.bootstrap.elements)))

    @classmethod
    def load_opponent(cls, directory: Path) -> "DevelopmentView":
        """Keep only cutoff eligible score projections for development events."""
        source = load_pinned_archive(directory)
        mapped = adapt_validated_archive(source, through_event=DEVELOPMENT_LAST_EVENT)
        scores = {event: eligible_scored_fixtures(source, target_event=event)
                  for event in range(6, DEVELOPMENT_LAST_EVENT + 1)}
        return cls(mapped.rows, mapped.fixtures, mapped.counts,
                   tuple(sorted(player.id for player in source.catalog.bootstrap.elements)),
                   scores)


@dataclass(frozen=True, order=True)
class Candidate:
    lookback: int
    half_life: int
    prior_strength: int


DEFAULT_CANDIDATE = Candidate(12, 6, 3)
GRID = tuple(Candidate(lookback, half_life, strength)
             for lookback in (6, 12)
             for half_life in (3, 6, 12)
             for strength in (1, 3, 6))


@dataclass(frozen=True)
class FoldResult:
    first_event: int
    last_event: int
    events_scored: int
    skipped_events: dict[str, int]
    first_time_excluded: int
    outcome_exclusions: dict[str, int]
    target_position_differences: int
    points: dict[str, object]
    model_minutes: object
    per_gameweek_mae: dict[int, float]
    runtime_seconds: float
    opponent_context_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateResult:
    candidate: Candidate
    folds: tuple[FoldResult, ...]
    pooled_points: dict[str, object]
    pooled_model_minutes: object
    pooled_per_gameweek_mae: dict[int, float]
    runtime_seconds: float
    # Private comparison records are deliberately omitted from serialized reports.
    model_records: tuple[PointMetricRecord, ...]


@dataclass(frozen=True)
class SelectionDecision:
    selected: Candidate
    default: Candidate
    comparisons: tuple[dict[str, object], ...]
    reason: str
    best_challenger: Candidate | None
    best_challenger_blocked_by: tuple[str, ...]


@dataclass(frozen=True)
class OpponentResult:
    setting: tuple[int, int]
    folds: tuple[FoldResult, ...]
    pooled_points: dict[str, object]
    pooled_model_minutes: object
    pooled_per_gameweek_mae: dict[int, float]
    model_records: tuple[PointMetricRecord, ...]


@dataclass(frozen=True)
class OpponentSelection:
    selected: tuple[int, int] | None
    comparisons: tuple[dict[str, object], ...]
    reason: str


def _prior_row(row: BenchmarkRow) -> SimpleNamespace:
    return SimpleNamespace(
        player_id=row.player_id, position=row.position,
        scoring_counts={name: getattr(row, name) for name in REQUIRED_COUNTS},
    )


_NO_PREVIOUS = SimpleNamespace(
    personal_player_id=None, personal_reason=None, personal_rows=(),
    position_rows=(), exclusions=(),
)


def _run_events(view: DevelopmentView, candidate: Candidate, first: int, last: int,
                opponent_setting: tuple[int, int] | None = None):
    if not isinstance(view, DevelopmentView):
        raise TypeError("runner requires a DevelopmentView")
    if (first, last) not in DEVELOPMENT_FOLDS:
        raise ValueError("only declared development folds can be scored")
    if candidate not in GRID:
        raise ValueError("candidate must be in the declared development grid")
    if opponent_setting is not None and (
        opponent_setting not in OPPONENT_SETTINGS or view.scored_by_event is None
    ):
        raise ValueError("opponent setting requires declared setting and cutoff scores")
    started = perf_counter()
    point_records: dict[str, list[PointMetricRecord]] = {
        name: [] for name in ("model", *BASELINE_NAMES)
    }
    minute_records: list[ModelMinuteRecord] = []
    skipped: dict[str, int] = {}
    exclusions: dict[str, int] = {}
    context_reasons: dict[str, int] = {}
    first_time = position_differences = events_scored = 0
    target_rows: dict[tuple[int, int], BenchmarkRow] = {
        (row.player_id, row.fixture_id): row for row in view.rows
    }

    for event in range(first, last + 1):
        cohort = select_cohort(event, view.fixtures, view.rows, view.player_ids)
        if cohort.cutoff.skipped:
            reason = cohort.cutoff.skip_reason
            assert reason is not None
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        events_scored += 1
        first_time += cohort.first_time_count
        position_differences += sum(player.target_position_differs for player in cohort.players)
        for reason, count in cohort.outcome_exclusion_counts.items():
            exclusions[reason] = exclusions.get(reason, 0) + count
        # This is exactly the cohort's cutoff-eligible row set, including other
        # players with usable earlier history, with no target outcomes in it.
        eligible = tuple(row for player in cohort.players for row in player.eligible_rows)
        prior_rows = tuple(_prior_row(row) for row in eligible)

        for player in cohort.players:
            if player.outcome_exclusion_reason is not None:
                continue
            prior = select_prior(player_id=player.player_id, position=player.position,
                                 current_rows=prior_rows, previous=_NO_PREVIOUS)
            forecast = predict(
                personal_rows=(FixtureObservation(row.fixture_id, row.kickoff_time, row)
                               for row in player.eligible_rows),
                prior=prior, position=player.position,
                target_fixture_ids=player.scheduled_fixture_ids,
                catalog_trusted=True, chance=None, status="a", owned=False,
                rules_version=ARCHIVE_RULES, input_version=REPORT_VERSION,
                half_life=candidate.half_life,
                prior_strength=candidate.prior_strength,
                window_size=candidate.lookback,
            )
            if opponent_setting is not None:
                adjustment = adjust_prediction(
                    forecast, player_rows=player.eligible_rows, prior=prior,
                    target_fixtures=(fixture for fixture in view.fixtures
                                     if fixture.fixture_id in player.scheduled_fixture_ids),
                    club_id=player.last_eligible_club_id,
                    scored_fixtures=view.scored_by_event[event],
                    event_cutoff=cohort.cutoff.cutoff_time,
                    setting=opponent_setting, half_life=candidate.half_life,
                    prior_strength=candidate.prior_strength,
                    window_size=candidate.lookback,
                )
                forecast = adjustment.prediction
                for detail in adjustment.fixtures:
                    if detail.reason is not None:
                        context_reasons[detail.reason] = context_reasons.get(detail.reason, 0) + 1
                    if detail.home_reason is not None:
                        context_reasons[detail.home_reason] = (
                            context_reasons.get(detail.home_reason, 0) + 1)
                if adjustment.kappa_reason is not None:
                    context_reasons[adjustment.kappa_reason] = (
                        context_reasons.get(adjustment.kappa_reason, 0) + 1)
            values = {
                "model": forecast.event_mean,
                "zero": zero_baseline(player),
                "position_mean": position_mean_baseline(player, eligible),
                "personal_mean": expanding_personal_mean_baseline(player),
                "latest_six": latest_six_personal_mean_baseline(player, eligible),
            }
            for name, value in values.items():
                point_records[name].append(PointMetricRecord(
                    player.player_id, event, player.position, player.active,
                    value, player.outcome_points, player.outcome_exclusion_reason,
                ))
            minute_records.append(ModelMinuteRecord(
                None,
                tuple(PlayedFixtureMinutes(
                    fixture_id, target_rows[player.player_id, fixture_id].minutes,
                    forecast.appearance_probabilities if forecast.status == "available" else None,
                ) for fixture_id in player.result_fixture_ids),
                player.active,
            ))

    summaries = {name: summarize_points(records)
                 for name, records in point_records.items()}
    fold = FoldResult(
        first, last, events_scored, skipped, first_time, exclusions,
        position_differences, summaries, summarize_model_minutes(minute_records),
        gameweek_mae(point_records["model"]), perf_counter() - started,
        context_reasons,
    )
    return fold, point_records, minute_records


def run_development_fold(view: DevelopmentView, candidate: Candidate,
                         fold: tuple[int, int]) -> FoldResult:
    """Run one declared fold, suitable for the required first runtime measurement."""
    return _run_events(view, candidate, *fold)[0]


def run_development_candidate(view: DevelopmentView, candidate: Candidate) -> CandidateResult:
    """Run all three development folds for one candidate."""
    folds = []
    all_points = {name: [] for name in ("model", *BASELINE_NAMES)}
    all_minutes = []
    for first, last in DEVELOPMENT_FOLDS:
        fold, points, minutes = _run_events(view, candidate, first, last)
        folds.append(fold)
        for name, records in points.items():
            all_points[name].extend(records)
        all_minutes.extend(minutes)
    return CandidateResult(
        candidate, tuple(folds),
        {name: summarize_points(records) for name, records in all_points.items()},
        summarize_model_minutes(all_minutes), gameweek_mae(all_points["model"]),
        math.fsum(fold.runtime_seconds for fold in folds),
        tuple(all_points["model"]),
    )


def run_development_grid(view: DevelopmentView) -> tuple[CandidateResult, ...]:
    """Run the declared grid; call only after the single-fold timing is reviewed."""
    return tuple(run_development_candidate(view, candidate) for candidate in GRID)


def run_opponent_candidate(view: DevelopmentView,
                           setting: tuple[int, int]) -> OpponentResult:
    """Development-only J35 candidate on the frozen baseline parameters."""
    if setting not in OPPONENT_SETTINGS:
        raise ValueError("setting must be one of the four declared J35 settings")
    folds = []
    all_points = {name: [] for name in ("model", *BASELINE_NAMES)}
    all_minutes = []
    for first, last in DEVELOPMENT_FOLDS:
        fold, points, minutes = _run_events(view, DEFAULT_CANDIDATE, first, last,
                                            opponent_setting=setting)
        folds.append(fold)
        for name, records in points.items():
            all_points[name].extend(records)
        all_minutes.extend(minutes)
    return OpponentResult(
        setting, tuple(folds),
        {name: summarize_points(records) for name, records in all_points.items()},
        summarize_model_minutes(all_minutes), gameweek_mae(all_points["model"]),
        tuple(all_points["model"]),
    )


def select_opponent_candidate(baseline: CandidateResult,
                              candidates: tuple[OpponentResult, ...]) -> OpponentSelection:
    """Require both J35 gates, then rank by overall, active, L and lambda."""
    if baseline.candidate != DEFAULT_CANDIDATE:
        raise ValueError("opponent comparison requires frozen 12/6/3 baseline")
    if len(candidates) != len(OPPONENT_SETTINGS) or {
        candidate.setting for candidate in candidates
    } != OPPONENT_SETTINGS:
        raise ValueError("all four declared opponent settings are required")
    base_points = baseline.pooled_points["model"]
    comparisons = []
    qualified = []
    for result in sorted(candidates, key=lambda item: item.setting):
        points = result.pooled_points["model"]
        paired = paired_gameweek_mae_difference(result.model_records,
                                                baseline.model_records)
        overall = points.overall.mae
        active = points.active_overall.mae
        base_overall = base_points.overall.mae
        base_active = base_points.active_overall.mae
        se_gate = (overall is not None and base_overall is not None
                   and paired.standard_error is not None
                   and base_overall - overall > paired.standard_error)
        active_gate = (active is not None and base_active is not None
                       and active < base_active)
        qualifies = se_gate and active_gate
        comparisons.append({
            "setting": result.setting, "pooled_overall_mae": overall,
            "pooled_active_mae": active, "baseline_overall_mae": base_overall,
            "baseline_active_mae": base_active,
            "paired_per_gameweek_differences": paired.per_gameweek,
            "paired_standard_error": paired.standard_error,
            "passes_se_gate": se_gate, "passes_active_gate": active_gate,
            "qualifies": qualifies,
        })
        if qualifies:
            qualified.append(result)
    selected = min(qualified, key=lambda item: (
        item.pooled_points["model"].overall.mae,
        item.pooled_points["model"].active_overall.mae,
        item.setting,
    )).setting if qualified else None
    return OpponentSelection(selected, tuple(comparisons),
                             "lowest_qualifying_mae" if qualified else "no_qualifier")


def write_opponent_report(baseline: CandidateResult,
                          candidates: tuple[OpponentResult, ...], counts: ArchiveCounts,
                          *, path: Path) -> Path:
    """Write only aggregate development metrics; never player predictions."""
    selection = select_opponent_candidate(baseline, candidates)
    if tuple((fold.first_event, fold.last_event) for fold in baseline.folds) != DEVELOPMENT_FOLDS:
        raise ValueError("baseline requires all three development folds")
    if any(tuple((fold.first_event, fold.last_event) for fold in result.folds)
           != DEVELOPMENT_FOLDS for result in candidates):
        raise ValueError("each opponent setting requires all three development folds")
    def aggregate(result):
        return {
            "folds": [asdict(fold) for fold in result.folds],
            "pooled_points": {name: asdict(summary)
                              for name, summary in result.pooled_points.items()},
            "pooled_model_minutes": asdict(result.pooled_model_minutes),
            "pooled_per_gameweek_mae": result.pooled_per_gameweek_mae,
        }
    payload = {
        "report_version": "opponent-development-v1",
        "baseline_parameters": asdict(DEFAULT_CANDIDATE),
        "development_counts": asdict(counts),
        "selection": asdict(selection),
        "baseline": aggregate(baseline),
        "candidates": [{"setting": result.setting, **aggregate(result)}
                       for result in sorted(candidates, key=lambda item: item.setting)],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def select_development_candidate(results: tuple[CandidateResult, ...]) -> SelectionDecision:
    """Apply the predeclared paired one-SE and active-MAE gate, without freezing."""
    by_candidate = {result.candidate: result for result in results}
    if len(by_candidate) != len(results) or DEFAULT_CANDIDATE not in by_candidate:
        raise ValueError("unique results including the default are required")
    default = by_candidate[DEFAULT_CANDIDATE]
    default_mae = default.pooled_points["model"].overall.mae
    default_active = default.pooled_points["model"].active_overall.mae
    if default_mae is None or default_active is None:
        raise ValueError("default needs pooled overall and active MAE")
    comparisons = []
    qualifiers = []
    for candidate, result in sorted(by_candidate.items()):
        if candidate == DEFAULT_CANDIDATE:
            continue
        overall = result.pooled_points["model"].overall.mae
        active = result.pooled_points["model"].active_overall.mae
        paired = paired_gameweek_mae_difference(result.model_records, default.model_records)
        improvement = None if overall is None else default_mae - overall
        clears_se_bar = (improvement is not None and paired.standard_error is not None
                         and improvement > paired.standard_error)
        preserves_active_mae = active is not None and active <= default_active
        qualifies = clears_se_bar and preserves_active_mae
        blocked_by = tuple(reason for reason, passed in (
            ("se_bar", clears_se_bar), ("active_cohort_mae", preserves_active_mae)
        ) if not passed)
        comparisons.append({
            "candidate": asdict(candidate), "pooled_overall_mae": overall,
            "pooled_active_mae": active, "default_overall_mae": default_mae,
            "default_active_mae": default_active, "pooled_improvement": improvement,
            "paired_per_gameweek_differences": paired.per_gameweek,
            "paired_mean_difference": paired.mean_difference,
            "paired_standard_error": paired.standard_error,
            "paired_gameweeks": paired.gameweek_count, "qualifies": qualifies,
            "blocked_by": blocked_by,
        })
        if qualifies:
            qualifiers.append(result)
    selected = min(qualifiers, key=lambda result: (
        result.pooled_points["model"].overall.mae, result.candidate
    )).candidate if qualifiers else DEFAULT_CANDIDATE
    best_challenger = min(comparisons, key=lambda item: (
        float("inf") if item["pooled_overall_mae"] is None
        else item["pooled_overall_mae"], item["candidate"]["lookback"],
        item["candidate"]["half_life"], item["candidate"]["prior_strength"],
    ), default=None)
    return SelectionDecision(selected, DEFAULT_CANDIDATE, tuple(comparisons),
                             "lowest_qualifying_mae" if qualifiers else "keep_default",
                             Candidate(**best_challenger["candidate"])
                             if best_challenger is not None else None,
                             best_challenger["blocked_by"] if best_challenger is not None else ())


def write_development_report(
    results: tuple[CandidateResult, ...], counts: object, *,
    path: Path = Path("/tmp/fpl-development-fold-report.json"),
) -> Path:
    """Write aggregate candidate results outside tracked paths by default."""
    if {result.candidate for result in results} != set(GRID) or len(results) != len(GRID):
        raise ValueError("complete eighteen-candidate grid required for the report")
    if any(tuple((fold.first_event, fold.last_event) for fold in result.folds)
           != DEVELOPMENT_FOLDS for result in results):
        raise ValueError("each candidate requires all three development folds")
    payload = {
        "report_version": REPORT_VERSION, "model_version": MODEL_VERSION,
        "rules_version": ARCHIVE_RULES, "scoring_adapter_version": SCORING_ADAPTER,
        "prior_scope": "eligible current-season position rows only",
        "availability": "neutral: chance null, status a",
        "development_counts": asdict(counts),
        "selection": asdict(select_development_candidate(results)),
        "candidates": [{
            "parameters": asdict(result.candidate),
            "folds": [asdict(fold) for fold in result.folds],
            "pooled_points": {name: asdict(summary) for name, summary in result.pooled_points.items()},
            "pooled_model_minutes": asdict(result.pooled_model_minutes),
            "pooled_per_gameweek_mae": result.pooled_per_gameweek_mae,
            "runtime_seconds": result.runtime_seconds,
        } for result in sorted(results, key=lambda item: item.candidate)],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
