"""Saved-object audit boundaries; synthetic bytes only, no live service access."""

from dataclasses import replace
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr
import pytest

from fpl_optimizer import audit_research as audit
from fpl_optimizer.services.shared_archive import ArchiveError, SupabaseStorage
from test_forecast_bundle import build, prepared
from test_research_capture import run


def test_aggregate_distinguishes_availability_history_and_event_nulls():
    view = prepared()
    view = replace(view, players=tuple(
        replace(p, status="d", chance_of_playing_next_round=None) if p.id == 1 else p
        for p in view.players
    ))
    report = audit.aggregate(build(view).bundle, 0)
    assert report["players"] == 6
    assert report["event_prediction_nonnull"] == 3
    assert report["event_prediction_null"] == 3
    assert report["prediction_status_counts"] == {"available": 2, "blank": 1, "null": 3}
    assert report["stored_exclusions"] == 3
    assert report["exclusion_reason_counts"] == {
        "history_age": 1, "missing_chance_for_status": 1, "missing_history_publication": 1,
    }
    assert report["excluded_null_chance_non_active"] == report["excluded_history_age"] == 1
    assert report["supplied_log_excluded_matches"] is False
    assert report["log_to_bundle_pairing_verified"] is False


def test_aggregate_never_prints_unknown_reason_text_or_identity():
    document = build().bundle
    sentinel = "private-canary-value"
    entry = document["exclusions"][0]
    entry["reasons"] = [sentinel]
    player = next(p for p in document["players"] if p["player_id"] == entry["player_id"])
    player["exclusion_reasons"] = [sentinel]
    player["name"] = sentinel
    rendered = json.dumps(audit.aggregate(document, 0))
    assert sentinel not in rendered
    assert '"other": 1' in rendered
    assert "player_id" not in rendered


@pytest.mark.asyncio
async def test_audit_reads_latest_triplet_and_rejects_corruption_without_fallback():
    result, store, timer = await run()
    assert result.status == "captured"
    timer.advance(1801)
    view = replace(prepared(), as_of=timer.clock.now())
    result, _, _ = await run(view, store=store, timer=timer)
    assert result.status == "captured"
    store.calls.clear()
    before = dict(store.data)
    report = await audit.audit(store, season=view.season_id, target=view.target.id,
                               reported_excluded=result.exclusions, clock=timer.clock)
    assert report["capture_time_utc"] == view.as_of.isoformat()
    assert report["gzip_checksum_verified"] and report["canonical_checksum_verified"]
    assert report["receipt_binding_verified"] and report["supplied_log_excluded_matches"]
    assert len(store.calls) == 3 and all(op == "get" for op, _ in store.calls)
    assert store.data == before
    newest_payload = max(key for key in store.data if key.endswith(".json.gz"))
    raw = store.data[newest_payload]
    store.data[newest_payload] = bytes([raw[0] ^ 1]) + raw[1:]
    with pytest.raises(ArchiveError, match="latest_capture_not_verified"):
        await audit.audit(store, season=view.season_id, target=view.target.id,
                          reported_excluded=0, clock=timer.clock)


@pytest.mark.asyncio
async def test_transport_allows_only_listing_and_research_get(monkeypatch):
    settings = SimpleNamespace(fpl_storage_url="https://example.invalid",
        fpl_storage_key=SecretStr("synthetic"), fpl_storage_bucket="synthetic",
        fpl_storage_allowance_bytes="1000000")
    storage = audit.ReadOnlyStorage(settings)
    forwarded = []
    async def fake(_self, method, path, **kwargs):
        forwarded.append((method, path))
    monkeypatch.setattr(SupabaseStorage, "_request", fake)
    for method, path in (
        ("POST", "/object/synthetic/file"), ("DELETE", "/object/synthetic"),
        ("GET", "/object/authenticated/synthetic/raw-shared/file"),
        ("GET", "https://example.invalid/elsewhere"),
    ):
        with pytest.raises(ArchiveError, match="read_only_violation"):
            await storage._request(method, path)
    assert forwarded == []
    await storage._request("POST", "/object/list/synthetic")
    await storage._request("GET", "/object/authenticated/synthetic/research-bundles/deadline-window/file")
    assert len(forwarded) == 2


def test_main_suppresses_exception_details(monkeypatch, capsys):
    async def fail():
        raise RuntimeError("private-canary-value")
    monkeypatch.setattr(audit, "run", fail)
    disabled = logging.root.manager.disable
    try:
        assert audit.main() == 1
    finally:
        logging.disable(disabled)
    captured = capsys.readouterr()
    assert captured.out == '{"status":"audit_failed"}\n' and captured.err == ""


def test_audit_workflow_is_manual_storage_only_and_has_no_artifact_upload():
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/audit-research.yml").read_text()
    trigger_block = workflow.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    assert trigger_block.startswith("  workflow_dispatch:\n")
    assert all(not line.startswith("  ") or line.startswith("    ")
               or line == "  workflow_dispatch:" for line in trigger_block.splitlines())
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "permissions:\n  contents: read\n" in workflow
    assert "group: fpl-research-audit" in workflow
    for forbidden in ("DATABASE_", "capture_research", "upload-artifact", "schedule:", "pull_request:"):
        assert forbidden not in workflow
    assert "run: .venv/bin/python -m fpl_optimizer.audit_research" in workflow
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in workflow
