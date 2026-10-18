"""Run and report the complete development grid without freezing parameters."""

import argparse
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

from fpl_optimizer.services.retrospective_runner import (
    DEFAULT_CANDIDATE, GRID, DevelopmentView, run_development_candidate,
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

    # The default is its own reference. If it stays selected, show the best
    # challenger's paired comparison so the decision remains inspectable.
    comparisons = decision.comparisons
    if decision.selected == DEFAULT_CANDIDATE:
        comparison = min(comparisons, key=lambda item: (
            float("inf") if item["pooled_overall_mae"] is None
            else item["pooled_overall_mae"], item["candidate"]["lookback"],
            item["candidate"]["half_life"], item["candidate"]["prior_strength"],
        ))
        print(f"selection: kept defaults {asdict(DEFAULT_CANDIDATE)}; "
              f"best challenger {comparison['candidate']}, "
              f"paired mean difference={comparison['paired_mean_difference']}, "
              f"standard error={comparison['paired_standard_error']}")
    else:
        comparison = next(item for item in comparisons
                          if item["candidate"] == asdict(decision.selected))
        print(f"selection: chosen candidate {asdict(decision.selected)}, "
              f"paired mean difference={comparison['paired_mean_difference']}, "
              f"standard error={comparison['paired_standard_error']}")
    print(f"aggregate report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
