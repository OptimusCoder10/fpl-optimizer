"""One fixed development-fold timing probe for the pinned archive."""

import argparse
import json
from pathlib import Path

from fpl_optimizer.services.retrospective_runner import (
    DEFAULT_CANDIDATE, DevelopmentView, run_development_fold,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the default on development GW6–10")
    parser.add_argument("archive_dir", type=Path)
    arguments = parser.parse_args(argv)
    view = DevelopmentView.load(arguments.archive_dir)
    fold = run_development_fold(view, DEFAULT_CANDIDATE, (6, 10))
    names = ("model", "zero", "position_mean", "personal_mean", "latest_six")
    payload = {
        "scored_player_events": fold.points["model"].overall.eligible,
        "excluded_count": sum(fold.outcome_exclusions.values()),
        "coverage": {name: fold.points[name].overall.coverage for name in names},
        "overall_mae": {name: fold.points[name].overall.mae for name in names},
        "elapsed_seconds": fold.runtime_seconds,
    }
    print(json.dumps(payload, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
