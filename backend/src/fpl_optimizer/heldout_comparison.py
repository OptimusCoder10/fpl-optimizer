"""Owner-authorized one-time held-out entry point; never runs on import."""

import argparse
from pathlib import Path

from fpl_optimizer.services.heldout_runner import run_heldout_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One guarded frozen GW20–38 comparison")
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("--authorisation", required=True)
    arguments = parser.parse_args(argv)
    report = run_heldout_once(arguments.authorisation, arguments.archive_dir)
    print(f"Aggregate report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
