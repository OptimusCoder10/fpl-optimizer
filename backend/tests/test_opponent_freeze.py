"""Check the committed J35 identity record without running a benchmark."""

import hashlib
import json
from pathlib import Path

from fpl_optimizer.schemas.prior_archive import PINNED_SHA256


def test_opponent_freeze_record_matches_declared_parameters_and_checksums():
    benchmarks = Path(__file__).resolve().parents[2] / "docs" / "benchmarks"
    record = json.loads((benchmarks / "opponent-freeze-2026-10-20.json").read_text())
    report = benchmarks / "opponent-development-report-2026-10-20.json"
    baseline_freeze = benchmarks / "baseline-freeze-2026-10-19.json"

    assert record["frozen_parameters"]["lambda"] == 12, "J35 frozen lambda must be 12"
    assert record["frozen_parameters"] == {
        "L": 12,
        "lambda": 12,
        "shared_home_term": True,
        "home_term_prior_match_equivalents": 20,
        "minimum_player_observations": 4,
        "minimum_scored_team_fixtures": 3,
        "minimum_complete_league_fixtures_for_home_term": 20,
        "team_profile_requires_complete_latest_window": True,
        "home_term_requires_positive_league_mean": True,
    }
    assert record["archive_identity"]["sha256"] == PINNED_SHA256
    assert (record["selection_evidence"]["development_report_sha256"]
            == hashlib.sha256(report.read_bytes()).hexdigest())
    assert (record["baseline_freeze_sha256"]
            == hashlib.sha256(baseline_freeze.read_bytes()).hexdigest())
