"""Development-only J35 grid; this module does not run on import."""

import argparse
import json
from pathlib import Path

from fpl_optimizer.services.opponent_profiles import SETTINGS
from fpl_optimizer.services.retrospective_runner import (
    DEFAULT_CANDIDATE, DevelopmentView, run_development_candidate,
    run_opponent_candidate, select_opponent_candidate, write_opponent_report,
)

FREEZE = Path(__file__).resolve().parents[3] / "docs/benchmarks/baseline-freeze-2026-10-19.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run only the declared GW6–19 opponent grid")
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("--output", type=Path,
                        default=Path("/tmp/fpl-opponent-development-report.json"))
    parser.add_argument("--last-event", type=int, default=19)
    arguments = parser.parse_args(argv)
    if arguments.last_event != 19:
        raise ValueError("opponent entry point is fixed to development GW6–19")
    frozen = json.loads(FREEZE.read_text(encoding="utf-8"))
    if frozen["frozen_parameters"] != {
        "lookback": DEFAULT_CANDIDATE.lookback,
        "half_life": DEFAULT_CANDIDATE.half_life,
        "prior_strength": DEFAULT_CANDIDATE.prior_strength,
    }:
        raise ValueError("frozen baseline does not match 12/6/3 implementation")
    view = DevelopmentView.load_opponent(arguments.archive_dir)
    baseline = run_development_candidate(view, DEFAULT_CANDIDATE)
    candidates = tuple(run_opponent_candidate(view, setting)
                       for setting in sorted(SETTINGS))
    selection = select_opponent_candidate(baseline, candidates)
    report = write_opponent_report(baseline, candidates, view.counts,
                                   path=arguments.output)
    print(json.dumps({"selected": selection.selected,
                      "reason": selection.reason, "aggregate_report": str(report)},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
