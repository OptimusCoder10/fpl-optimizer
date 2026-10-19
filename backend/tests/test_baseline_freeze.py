"""Check the committed baseline identity record without running a benchmark."""

import json
from pathlib import Path

from fpl_optimizer.schemas.prior_archive import PINNED_SHA256


def test_baseline_freeze_record_matches_declared_parameters_and_checksums():
    record_path = (Path(__file__).resolve().parents[2] / "docs" / "benchmarks"
                   / "baseline-freeze-2026-10-19.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))

    assert record["frozen_parameters"] == {
        "lookback": 12, "half_life": 6, "prior_strength": 3,
    }
    assert record["archive_identity"]["sha256"] == PINNED_SHA256
    assert "No GW20-38 forecast, error metric or point comparison has been computed" in record["sealed_holdout"]
    assert "remains sealed until the opponent candidate is also frozen" in record["sealed_holdout"]
