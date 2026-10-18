"""Run and report the complete development grid without freezing parameters."""

import argparse
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

from fpl_optimizer.services.retrospective_runner import (
    BASELINE_NAMES, DEFAULT_CANDIDATE, GRID, DevelopmentView, run_development_candidate,
    select_development_candidate, write_development_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the GW6–19 development grid")
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("--output", type=Path, help="aggregate JSON report path (default: /tmp)")
    arguments = parser.parse_args(argv)

    view = DevelopmentView.load(arguments.archive_dir)
    started = perf_counter()
    results = []
    for index, candidate in enumerate(GRID, start=1):
        results.append(run_development_candidate(view, candidate))
        print(f"candidate {index} of {len(GRID)}, elapsed seconds: "
              f"{perf_counter() - started:.2f}", flush=True)

    completed = tuple(results)
    decision = select_development_candidate(completed)
    report = (write_development_report(completed, view.counts, path=arguments.output)
              if arguments.output is not None
              else write_development_report(completed, view.counts))

    for result in completed:
        overall = result.pooled_points["model"].overall
        active = result.pooled_points["model"].active_overall
        print(f"candidate {asdict(result.candidate)}: pooled overall MAE={overall.mae}, "
              f"pooled active-cohort MAE={active.mae}, bias={overall.bias}, "
              f"RMSE={overall.rmse}")

    default_result = next(result for result in completed
                          if result.candidate == DEFAULT_CANDIDATE)
    for label, points in (
        ("pooled", default_result.pooled_points),
        *((f"GW{fold.first_event}-{fold.last_event}", fold.points)
          for fold in default_result.folds),
    ):
        for name in ("model", *BASELINE_NAMES):
            overall = points[name].overall
            active = points[name].active_overall
            print(f"default comparison {label} {name}: overall MAE={overall.mae}, "
                  f"active-cohort MAE={active.mae}, bias={overall.bias}, "
                  f"RMSE={overall.rmse}")

    comparisons = decision.comparisons
    if decision.selected == DEFAULT_CANDIDATE:
        comparison = next(item for item in comparisons
                          if item["candidate"] == asdict(decision.best_challenger))
        blocked = ", ".join({
            "se_bar": "SE bar", "active_cohort_mae": "active-cohort MAE",
        }[reason] for reason in decision.best_challenger_blocked_by)
        print(f"selection: kept defaults {asdict(DEFAULT_CANDIDATE)}; "
              f"best challenger {comparison['candidate']}, "
              f"paired mean difference={comparison['paired_mean_difference']}, "
              f"standard error={comparison['paired_standard_error']}; "
              f"blocked by {blocked}")
    else:
        comparison = next(item for item in comparisons
                          if item["candidate"] == asdict(decision.selected))
        print(f"selection: chosen candidate {asdict(decision.selected)}, "
              f"paired mean difference={comparison['paired_mean_difference']}, "
              f"standard error={comparison['paired_standard_error']}; "
              "passed SE bar and active-cohort MAE")
    print(f"aggregate report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
