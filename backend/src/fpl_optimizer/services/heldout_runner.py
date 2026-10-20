"""One guarded joint comparison of two frozen models on GW20–38."""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fpl_optimizer.schemas.prior_archive import ARCHIVE_RULES, SCORING_ADAPTER
from fpl_optimizer.services.heldout_adjustment import heldout_adjust_prediction
from fpl_optimizer.services.heldout_archive import FIRST_TARGET, LAST_TARGET, HeldoutView
from fpl_optimizer.services.heldout_run_guard import HeldoutGuardError, authorize_heldout_run
from fpl_optimizer.services.prediction_engine import MODEL_VERSION, FixtureObservation, predict
from fpl_optimizer.services.prior_selection import REQUIRED_COUNTS, select_prior
from fpl_optimizer.services.retrospective_benchmark import (
    ModelMinuteRecord, PlayedFixtureMinutes, PointMetricRecord,
    expanding_personal_mean_baseline, gameweek_mae,
    latest_six_personal_mean_baseline, paired_gameweek_mae_difference,
    position_mean_baseline, summarize_model_minutes, summarize_points, zero_baseline,
)

REPORT_NAME = "heldout-joint-comparison.json"
BASELINE_NAMES = ("zero", "position_mean", "personal_mean", "latest_six")
_NO_PREVIOUS = SimpleNamespace(personal_player_id=None, personal_reason=None,
                               personal_rows=(), position_rows=(), exclusions=())


@dataclass(frozen=True)
class FrozenParameters:
    lookback: int
    half_life: int
    prior_strength: int
    opponent_setting: tuple[int, int]


def parameters_from_verified_freezes(root: Path, expected_hashes: tuple[str, str]) -> FrozenParameters:
    """Recheck the guard's exact verified bytes immediately before parsing."""
    base = root / "docs/benchmarks/baseline-freeze-2026-10-19.json"
    opponent = root / "docs/benchmarks/opponent-freeze-2026-10-20.json"
    base_bytes, opponent_bytes = base.read_bytes(), opponent.read_bytes()
    if (hashlib.sha256(base_bytes).hexdigest(),
            hashlib.sha256(opponent_bytes).hexdigest()) != expected_hashes:
        raise ValueError("freeze bytes changed after held-out guard verification")
    baseline = json.loads(base_bytes)["frozen_parameters"]
    frozen = json.loads(opponent_bytes)
    candidate = frozen["frozen_parameters"]
    if frozen["baseline_parameters"] != baseline:
        raise ValueError("frozen baseline parameter records disagree")
    parameters = FrozenParameters(baseline["lookback"], baseline["half_life"],
                                  baseline["prior_strength"],
                                  (candidate["L"], candidate["lambda"]))
    if parameters != FrozenParameters(12, 6, 3, (12, 12)):
        raise ValueError("frozen records differ from the predeclared comparison")
    return parameters


def _prior_row(row):
    return SimpleNamespace(player_id=row.player_id, position=row.position,
                           scoring_counts={name: getattr(row, name) for name in REQUIRED_COUNTS})


def compare_frozen(view: HeldoutView, parameters: FrozenParameters) -> dict:
    """Pure synthetic-testable comparison; both forecasts share one cohort per event."""
    if not isinstance(view, HeldoutView):
        raise TypeError("held-out comparison requires HeldoutView")
    if parameters != FrozenParameters(12, 6, 3, (12, 12)):
        raise ValueError("only the two frozen parameter sets may be compared")
    records = {name: [] for name in ("baseline", "opponent", *BASELINE_NAMES)}
    minutes = {"baseline": [], "opponent": []}
    exclusions: dict[str, int] = {}
    neutral: dict[str, int] = {}
    skipped: dict[str, int] = {}
    first_time = position_differences = 0
    events_scored = 0
    for event in range(FIRST_TARGET, LAST_TARGET + 1):
        cohort = view.cohort(event)
        if cohort.cutoff.skipped:
            reason = cohort.cutoff.skip_reason
            assert reason is not None
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        events_scored += 1
        first_time += cohort.first_time_count
        position_differences += sum(p.target_position_differs for p in cohort.players)
        for reason, count in cohort.outcome_exclusion_counts.items():
            exclusions[reason] = exclusions.get(reason, 0) + count
        eligible = tuple(row for player in cohort.players for row in player.eligible_rows)
        prior_rows = tuple(_prior_row(row) for row in eligible)
        target_rows = {(row.player_id, row.fixture_id): row for row in view.rows
                       if row.event == event}
        for player in cohort.players:
            prior = select_prior(player_id=player.player_id, position=player.position,
                                 current_rows=prior_rows, previous=_NO_PREVIOUS)
            baseline = predict(
                personal_rows=(FixtureObservation(row.fixture_id, row.kickoff_time, row)
                               for row in player.eligible_rows),
                prior=prior, position=player.position,
                target_fixture_ids=player.scheduled_fixture_ids,
                catalog_trusted=True, chance=None, status="a", owned=False,
                rules_version=ARCHIVE_RULES, input_version="heldout-joint-v1",
                half_life=parameters.half_life,
                prior_strength=parameters.prior_strength,
                window_size=parameters.lookback,
            )
            adjusted = heldout_adjust_prediction(
                baseline, player_rows=player.eligible_rows, prior=prior,
                target_fixtures=(fixture for fixture in view.fixtures
                                 if fixture.fixture_id in player.scheduled_fixture_ids),
                club_id=player.last_eligible_club_id,
                scored_fixtures=view.scored_by_event[event],
                event_cutoff=cohort.cutoff.cutoff_time,
                setting=parameters.opponent_setting, half_life=parameters.half_life,
                prior_strength=parameters.prior_strength,
                window_size=parameters.lookback,
            )
            # Forecast even when a target result row is missing. Its acceptance
            # remains excluded below, but target outcomes cannot select who is
            # forecast or alter the input and profile computation.
            if player.outcome_exclusion_reason is not None:
                continue
            for detail in adjusted.fixtures:
                for reason in (detail.reason, detail.home_reason):
                    if reason is not None:
                        neutral[reason] = neutral.get(reason, 0) + 1
            if adjusted.kappa_reason is not None:
                neutral[adjusted.kappa_reason] = neutral.get(adjusted.kappa_reason, 0) + 1
            forecasts = {"baseline": baseline, "opponent": adjusted.prediction}
            values = {
                "baseline": baseline.event_mean,
                "opponent": adjusted.prediction.event_mean,
                "zero": zero_baseline(player),
                "position_mean": position_mean_baseline(player, eligible),
                "personal_mean": expanding_personal_mean_baseline(player),
                "latest_six": latest_six_personal_mean_baseline(player, eligible),
            }
            for name, value in values.items():
                records[name].append(PointMetricRecord(
                    player.player_id, event, player.position, player.active,
                    value, player.outcome_points, player.outcome_exclusion_reason,
                ))
            for name, forecast in forecasts.items():
                minutes[name].append(ModelMinuteRecord(None, tuple(PlayedFixtureMinutes(
                    fixture_id, target_rows[player.player_id, fixture_id].minutes,
                    forecast.appearance_probabilities if forecast.status == "available" else None,
                ) for fixture_id in player.result_fixture_ids), player.active))

    baseline_records = records["baseline"]
    opponent_records = records["opponent"]
    base_summary = summarize_points(baseline_records)
    opponent_summary = summarize_points(opponent_records)
    paired = paired_gameweek_mae_difference(opponent_records, baseline_records)
    adoption = adoption_decision(base_summary, opponent_summary)
    return {
        "report_version": "heldout-joint-v1",
        "model_version": MODEL_VERSION,
        "rules_version": ARCHIVE_RULES,
        "scoring_adapter_version": SCORING_ADAPTER,
        "baseline_parameters": {"lookback": parameters.lookback,
                                "half_life": parameters.half_life,
                                "prior_strength": parameters.prior_strength},
        "opponent_parameters": {"L": parameters.opponent_setting[0],
                                "lambda": parameters.opponent_setting[1]},
        "target_events": [FIRST_TARGET, LAST_TARGET],
        "scope": {"availability": "neutral", "prior": "current-season position only",
                  "historical_deadline": "earliest kickoff minus 90 minutes",
                  "input_buffer_hours": 3,
                  "earlier_fixture_scores": "strictly before cutoff, earlier events only",
                  "archive_limitation": "final fixture assignments and corrected results; earlier read-only assignment audit"},
        "counts": {"events_scored": events_scored,
                   "skipped_events": skipped,
                   "first_time_excluded": first_time,
                   "outcome_exclusions": exclusions,
                   "target_position_differences": position_differences},
        "baseline": _aggregate(records["baseline"], minutes["baseline"]),
        "opponent": _aggregate(records["opponent"], minutes["opponent"]),
        "point_baselines": {name: _aggregate(records[name]) for name in BASELINE_NAMES},
        "paired_weekly_difference": asdict(paired),
        "position_mae_differences": {
            position: (metric.mae - base_summary.by_position[position].mae
                       if metric.mae is not None
                       and base_summary.by_position[position].mae is not None else None)
            for position, metric in opponent_summary.by_position.items()
        },
        "neutral_reasons": neutral,
        "adoption": adoption,
    }


def _aggregate(records, minute_records=None):
    result = {"points": asdict(summarize_points(records)),
              "weekly_mae": gameweek_mae(records)}
    if minute_records is not None:
        result["model_minutes"] = asdict(summarize_model_minutes(minute_records))
    return result


def adoption_decision(baseline, opponent) -> dict:
    """J34 held-out gate: strictly lower overall AND active-cohort MAE."""
    base_overall = baseline.overall.mae
    base_active = baseline.active_overall.mae
    other_overall = opponent.overall.mae
    other_active = opponent.active_overall.mae
    overall_lower = (base_overall is not None and other_overall is not None
                     and other_overall < base_overall)
    active_lower = (base_active is not None and other_active is not None
                    and other_active < base_active)
    return {"opponent_adopted": overall_lower and active_lower,
            "overall_mae_lower": overall_lower,
            "active_mae_lower": active_lower}


def run_heldout_once(authorisation: str, archive_dir: Path, *,
                     repo_root: Path | None = None, lock_path: Path | None = None) -> Path:
    """Guard first, leave its lock on success or failure, write aggregate output."""
    root = (repo_root or Path(__file__).resolve().parents[4]).resolve()
    lock = (lock_path or Path.home() / "fpl-benchmark-reports/heldout-run-lock.json").resolve()
    admission = authorize_heldout_run(authorisation, archive_dir, repo_root=root, lock_path=lock)
    try:
        if not lock.is_file():
            raise HeldoutGuardError("Held-out guard returned without its one-time lock.")
        parameters = parameters_from_verified_freezes(root, (
            admission["baseline_freeze_sha256"], admission["opponent_freeze_sha256"]))
        view = HeldoutView.load(archive_dir)
        report = compare_frozen(view, parameters)
        destination = Path.home() / "fpl-benchmark-reports" / REPORT_NAME
        incomplete = destination.with_suffix(".json.incomplete")
        if destination.exists() or incomplete.exists():
            raise FileExistsError("held-out report already exists")
        with incomplete.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(incomplete, destination)
        return destination
    except BaseException as exc:
        # Failure is deliberately category-only: no archive data, IDs or paths.
        category = ("interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit))
                    else "report_exists" if isinstance(exc, FileExistsError)
                    else "invalid_freeze" if isinstance(exc, (ValueError, KeyError, TypeError))
                    else "execution_failure")
        failure = lock.with_name("heldout-run-failure.json")
        with failure.open("x", encoding="utf-8") as output:
            json.dump({"category": category}, output, sort_keys=True)
            output.write("\n")
        raise
