"""Check the committed aggregate result without opening the pinned archive."""

import hashlib
import json
from pathlib import Path


def test_heldout_result_record_matches_report_checksum():
    benchmarks = Path(__file__).resolve().parents[2] / "docs" / "benchmarks"
    report_path = benchmarks / "heldout-joint-comparison-2026-10-21.json"
    provenance_path = benchmarks / "heldout-joint-comparison-2026-10-21.meta.json"

    report_bytes = report_path.read_bytes()
    provenance = json.loads(provenance_path.read_text())
    assert hashlib.sha256(report_bytes).hexdigest() == provenance["report_sha256"]

    report = json.loads(report_bytes)
    baseline = report["baseline"]["points"]
    opponent = report["opponent"]["points"]
    both_maes_strictly_lower = (
        opponent["overall"]["mae"] < baseline["overall"]["mae"]
        and opponent["active_overall"]["mae"] < baseline["active_overall"]["mae"]
    )
    assert report["adoption"]["opponent_adopted"] is both_maes_strictly_lower
