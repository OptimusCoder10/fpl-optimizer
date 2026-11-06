"""Manual aggregate-only inspection of saved research objects; never capture."""

import asyncio
from collections import Counter
import gzip
import json
import logging
import os
import re
from types import SimpleNamespace

from pydantic import SecretStr

from fpl_optimizer.services.research_capture import (
    CaptureClock, PROTECTED, inventory, receipt_slot, verified_records,
)
from fpl_optimizer.services.research_storage import ResearchStorage, timestamp
from fpl_optimizer.services.shared_archive import ArchiveError


class ReadOnlyStorage(ResearchStorage):
    """Enforce the read boundary at transport, including inherited methods."""

    async def _request(self, method, path, **kwargs):
        if not (
            method == "POST" and path == f"/object/list/{self._bucket}"
            or method == "GET" and path.startswith(
                f"/object/authenticated/{self._bucket}/{PROTECTED}"
            )
        ):
            raise ArchiveError("read_only_violation")
        return await super()._request(method, path, **kwargs)


REASONS = {
    "no validated player history publication": "missing_history_publication",
    "player history older than 48 hours": "history_age",
    "no trusted shared catalog publication": "missing_shared_publication",
    "shared catalog outside current freshness limit": "shared_freshness",
    "missing_chance_for_status": "missing_chance_for_status",
    "unrecognized_status": "unrecognized_status",
    "contradictory_chance_and_status": "contradictory_chance_and_status",
    "no_prior_or_personal_observations": "no_prior_or_personal_observations",
    "missing_prior_minute_means": "missing_prior_minute_means",
    "untrusted_fixture_catalog": "untrusted_fixture_catalog",
}


def category(reason):
    if reason.startswith("missing or invalid finalized fixture coverage:"):
        return "history_coverage"
    return REASONS.get(reason, "other")


def aggregate(document, reported_excluded):
    """Only fixed labels, counts and normalized timestamps leave this function."""
    players = document["players"]
    exclusions = document["exclusions"]
    by_id = {p["player_id"]: p for p in players}
    excluded_ids = {e["player_id"] for e in exclusions}
    if (len(by_id) != len(players) or len(excluded_ids) != len(exclusions)
            or not excluded_ids <= by_id.keys()):
        raise ArchiveError("invalid_bundle")
    for entry in exclusions:
        if not entry["reasons"] or entry["reasons"] != by_id[entry["player_id"]]["exclusion_reasons"]:
            raise ArchiveError("invalid_bundle")
    if excluded_ids != {p["player_id"] for p in players if p["exclusion_reasons"]}:
        raise ArchiveError("invalid_bundle")
    reasons = Counter(category(reason) for e in exclusions for reason in set(e["reasons"]))
    nonnull = sum(p["prediction"] is not None and p["prediction"]["event_mean"] is not None
                  for p in players)
    statuses = Counter(
        "null" if p["prediction"] is None else
        p["prediction"]["status"] if p["prediction"]["status"] in {"available", "blank", "unavailable"}
        else "other" for p in players
    )
    return {
        "players": len(players),
        "deadline_utc": timestamp(document["target"]["deadline_time"]).isoformat(),
        "capture_time_utc": timestamp(document["capture_time"]).isoformat(),
        "embedded_historical_rows": len(document["evidence"]["rows"]),
        "event_prediction_nonnull": nonnull,
        "event_prediction_null": len(players) - nonnull,
        "prediction_status_counts": dict(sorted(statuses.items())),
        "stored_exclusions": len(exclusions),
        "exclusion_reason_counts": dict(sorted(reasons.items())),
        "excluded_null_chance_non_active": sum(
            by_id[pid]["availability"]["chance"] is None
            and by_id[pid]["availability"]["status"] != "a" for pid in excluded_ids
        ),
        "excluded_history_age": sum(
            any(category(r) == "history_age" for r in e["reasons"]) for e in exclusions
        ),
        "supplied_log_excluded": reported_excluded,
        "supplied_log_excluded_matches": reported_excluded == len(exclusions),
        "log_to_bundle_pairing_verified": False,
        "log_comparison_basis": "manual_input_not_fetched_log",
        "capture_bytes_meaning": "whole_bucket_before_capture_plus_new_gzip_metadata_receipt",
    }


async def audit(storage, *, season, target, reported_excluded, clock):
    if not re.fullmatch(r"\d{4}-\d{2}", season) or target < 1 or reported_excluded < 0:
        raise ArchiveError("invalid_input")
    prefix = f"{PROTECTED}{season}/{target}/"
    objects = await inventory(storage, clock)
    keys = [key for key in objects if key.startswith(prefix) and key.endswith(".verified.json")]
    if not keys:
        raise ArchiveError("no_saved_capture")
    for key in keys:
        receipt_slot(key)  # Reject unidentifiable keys rather than guess ordering.
    newest = max(keys)  # Generated UTC capture timestamps have fixed-width ordering.
    base = newest.removesuffix(".verified.json")
    selected = {key: objects[key] for key in (
        newest, base + ".meta.json", base + ".json.gz",
    )}
    # Cache this triplet so verification and reporting inspect the same bytes.
    responses = {key: await storage.get(key) for key in selected}
    if any(len(responses[key].content) != info.size for key, info in selected.items()):
        raise ArchiveError("invalid_bundle")
    meta = json.loads(responses[base + ".meta.json"].content)
    deadline = timestamp(meta["deadline"])
    if meta["season_id"] != season or meta["target_event"] != target:
        raise ArchiveError("invalid_bundle")

    class SavedReads:
        async def get(self, key):
            return responses[key]

    records = await verified_records(SavedReads(), selected, prefix, deadline, clock)
    if len(records) != 1:
        # Never silently fall back to an older capture when the latest fails.
        raise ArchiveError("latest_capture_not_verified")
    document = json.loads(gzip.decompress(responses[base + ".json.gz"].content))
    if (document["schema_version"] != "research-bundle-v1"
            or document["season_id"] != season or document["target"]["id"] != target
            or timestamp(document["target"]["deadline_time"]) != deadline
            or timestamp(document["capture_time"]) != timestamp(meta["capture_time"])):
        raise ArchiveError("invalid_bundle")
    return {"status": "verified", "gzip_checksum_verified": True,
            "canonical_checksum_verified": True, "receipt_binding_verified": True,
            **aggregate(document, reported_excluded)}


async def run():
    # Environment only: do not load a local dotenv file or any database credential.
    settings = SimpleNamespace(
        fpl_storage_url=os.environ.get("FPL_STORAGE_URL"),
        fpl_storage_key=SecretStr(os.environ.get("FPL_STORAGE_KEY", "")),
        fpl_storage_bucket=os.environ.get("FPL_STORAGE_BUCKET"),
        fpl_storage_allowance_bytes=os.environ.get("FPL_STORAGE_ALLOWANCE_BYTES"),
    )
    async with asyncio.timeout(180):
        return await audit(
            ReadOnlyStorage(settings), season=os.environ.get("FPL_SEASON_ID", ""),
            target=int(os.environ["AUDIT_GAMEWEEK"]),
            reported_excluded=int(os.environ["AUDIT_REPORTED_EXCLUDED"]), clock=CaptureClock(),
        )


def main():
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(run())
    except Exception:
        # Exception strings, object contents and transport details never reach logs.
        print('{"status":"audit_failed"}')
        return 1
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
