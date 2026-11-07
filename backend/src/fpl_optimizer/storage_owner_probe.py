"""Manual synthetic-only observation; never qualifies prospective evidence."""

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import os
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
from pydantic import SecretStr

from fpl_optimizer.services.research_storage import aware, timestamp
from fpl_optimizer.services.shared_archive import SupabaseStorage
from fpl_optimizer.services.storage_http import storage_client


PREFIX = "research-bundles/self-test/owner-gates/"
A = b'{"probe":"A"}'.ljust(64)
B = b'{"probe":"B"}'.ljust(64)
FAILURES = frozenset({
    "arguments_rejected", "config_missing", "config_invalid", "client_setup_failed",
    "clock_failed", "first_upload_failed", "first_upload_rejected", "clock_jump",
    "upload_timestamp_invalid", "overwrite_check_failed", "overwrite_not_rejected",
    "readback_failed", "readback_timestamp_invalid", "listing_failed", "listing_invalid",
    "provider_timestamp_invalid", "probe_timeout", "internal_error",
    "fallback_upload_failed", "fallback_upload_rejected",
    "object_timestamp_changed",
})
STREAM_FAILURES = frozenset({
    "stream_timeout", "stream_interrupted", "stream_rejected", "stream_connection_failed",
})
DUPLICATE_CODES = frozenset({
    "AssetAlreadyExists", "already_exists", "Duplicate", "Asset Already Exists",
    "ResourceAlreadyExists", "KeyAlreadyExists",
})


def overwrite_response_kind(response):
    """Return fixed labels only; status alone never establishes a duplicate."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = body.get("errorCode") or body.get("code") or body.get("error")
        if isinstance(code, str) and code in DUPLICATE_CODES:
            return "duplicate_error"
    return "success" if 200 <= response.status_code < 300 else "other_error"


def empty_report():
    return dict(failure_category="internal_error", stream_failure_category=None, first_upload_status=None,
                fallback_upload_status=None,
                overwrite_status=None, overwrite_response_kind=None, updated_unchanged="no",
                upload_mode="not_started", gate2_timing_observation="untested",
                overwrite_rejected="no", a_intact="no",
                created_minus_before_seconds=None, created_minus_after_seconds=None,
                created_minus_runner_seconds=None, runner_minus_provider_seconds=None,
                upload_date_minus_after_seconds=None, get_date_minus_runner_seconds=None,
                last_modified_minus_created_seconds=None, updated_minus_created_seconds=None,
                last_accessed_minus_created_seconds=None)


async def probe(report):
    # No caller-supplied destination: neither arguments nor environment select it.
    folder = PREFIX + uuid4().hex
    key = folder + "/probe.bin"
    report["failure_category"] = "config_missing"
    settings = SimpleNamespace(
        fpl_storage_url=os.environ["FPL_STORAGE_URL"],
        fpl_storage_bucket=os.environ["FPL_STORAGE_BUCKET"],
        fpl_storage_key=SecretStr(os.environ["FPL_STORAGE_KEY"]),
        fpl_storage_allowance_bytes=1,
    )
    # Reuse configuration validation only; expose no general storage operations.
    report["failure_category"] = "config_invalid"
    config = SupabaseStorage(settings)
    wall = lambda: datetime.now(timezone.utc)

    async def slow_bytes():
        yield A[:32]
        await asyncio.sleep(30)
        yield A[32:]

    async def request(method, path, *, streamed=False, **kwargs):
        # Same client policy as capture; never reuse a failed stream's connection.
        phase = report["failure_category"]
        report["failure_category"] = "client_setup_failed"
        async with storage_client(config._url, config._key, timeout=60 if streamed else 20) as client:
            report["failure_category"] = phase
            return await client.request(method, path, **kwargs)

    async def listed_object():
        # Read only this newly generated folder; never derive write targets from listings.
        report["failure_category"] = "listing_failed"
        listing = await request("POST", f"/object/list/{config._bucket}", json={
            "prefix": folder, "limit": 2, "offset": 0,
            "sortBy": {"column": "name", "order": "asc"},
        })
        runner = wall()
        listing.raise_for_status()
        report["failure_category"] = "listing_invalid"
        rows = listing.json()
        if len(rows) != 1 or rows[0]["name"] != "probe.bin" or rows[0]["metadata"]["size"] != 64:
            raise ValueError("unexpected synthetic listing")
        return rows[0], listing, runner

    headers = {"Content-Type": "application/json", "cache-control": "private, max-age=0"}
    headers["x-upsert"] = "false"
    report["failure_category"] = "clock_failed"
    before, tick = wall(), time.monotonic()
    report["failure_category"] = "first_upload_failed"
    report["upload_mode"] = "streamed"
    uploaded = None
    try:
        uploaded = await request(
            "POST", f"/object/{config._bucket}/{key}", content=slow_bytes(),
            headers={**headers, "Content-Length": str(len(A))}, streamed=True,
        )
    except httpx.TimeoutException:
        report["stream_failure_category"] = "stream_timeout"
    except httpx.RemoteProtocolError:
        report["stream_failure_category"] = "stream_interrupted"
    except httpx.TransportError:
        report["stream_failure_category"] = "stream_connection_failed"
    if uploaded is not None:
        report["first_upload_status"] = uploaded.status_code
    if uploaded is not None and uploaded.status_code in {408, 411, 501}:
        report["stream_failure_category"] = "stream_rejected"
    if report["stream_failure_category"] is not None:
        # The first upload may have persisted. Never reuse its destination.
        folder = PREFIX + uuid4().hex
        key = folder + "/probe.bin"
        report["upload_mode"] = "normal_fallback"
        report["failure_category"] = "clock_failed"
        before, tick = wall(), time.monotonic()
        report["failure_category"] = "fallback_upload_failed"
        uploaded = await request("POST", f"/object/{config._bucket}/{key}", content=A, headers=headers)
        report["fallback_upload_status"] = uploaded.status_code
    report["failure_category"] = "clock_failed"
    after, tock = wall(), time.monotonic()
    report["failure_category"] = (
        "fallback_upload_rejected" if report["upload_mode"] == "normal_fallback" else "first_upload_rejected"
    )
    uploaded.raise_for_status()
    report["failure_category"] = "clock_jump"
    if abs((after - before).total_seconds() - (tock - tick)) > 2:
        raise ValueError("clock jump")
    report["failure_category"] = "upload_timestamp_invalid"
    report["upload_date_minus_after_seconds"] = (
        aware(parsedate_to_datetime(uploaded.headers["Date"])) - after).total_seconds()
    initial_object, _, _ = await listed_object()
    report["failure_category"] = "provider_timestamp_invalid"
    updated_before = timestamp(initial_object["updated_at"])
    report["failure_category"] = "overwrite_check_failed"
    duplicate = await request("POST", f"/object/{config._bucket}/{key}", content=B, headers=headers)
    report["overwrite_status"] = duplicate.status_code
    report["overwrite_response_kind"] = overwrite_response_kind(duplicate)
    rejected = report["overwrite_response_kind"] == "duplicate_error"
    report["overwrite_rejected"] = "yes" if rejected else "no"
    report["failure_category"] = "readback_failed"
    fetched = await request(
        "GET", f"/object/authenticated/{config._bucket}/{key}",
        headers={"cache-control": "no-cache, no-store"},
    )
    get_runner = wall()
    fetched.raise_for_status()
    report["a_intact"] = "yes" if fetched.content == A else "no"
    if fetched.content != A:
        return False
    report["failure_category"] = "readback_timestamp_invalid"
    report["get_date_minus_runner_seconds"] = (
        aware(parsedate_to_datetime(fetched.headers["Date"])) - get_runner).total_seconds()
    final_object, listing, runner = await listed_object()
    report["failure_category"] = "provider_timestamp_invalid"
    created = timestamp(final_object["created_at"])
    updated_after = timestamp(final_object["updated_at"])
    report["updated_unchanged"] = "yes" if updated_after == updated_before else "no"
    for label, bound in (("before", before), ("after", after), ("runner", runner)):
        report[f"created_minus_{label}_seconds"] = (created - bound).total_seconds()
    report["runner_minus_provider_seconds"] = (
        runner - aware(parsedate_to_datetime(listing.headers["Date"]))).total_seconds()
    for field, label in (("updated_at", "updated"), ("last_accessed_at", "last_accessed")):
        if final_object.get(field):
            report[f"{label}_minus_created_seconds"] = (timestamp(final_object[field]) - created).total_seconds()
    if fetched.headers.get("Last-Modified"):
        report["last_modified_minus_created_seconds"] = (
            aware(parsedate_to_datetime(fetched.headers["Last-Modified"])) - created).total_seconds()
    if report["upload_mode"] == "streamed":
        report["gate2_timing_observation"] = "delayed_upload_observed"
    if updated_after != updated_before:
        report["failure_category"] = "object_timestamp_changed"
        return False
    report["failure_category"] = None if rejected else "overwrite_not_rejected"
    return rejected


async def bounded(report):
    async with asyncio.timeout(150):
        return await probe(report)


def main():
    logging.disable(logging.CRITICAL)
    report = empty_report()
    ok = False
    try:
        report["failure_category"] = "arguments_rejected"
        if len(sys.argv) != 1: raise ValueError("no caller-supplied destination")
        report["failure_category"] = "internal_error"
        ok = asyncio.run(bounded(report))
    except TimeoutError:
        report["failure_category"] = "probe_timeout"
    except Exception:
        pass  # Never print provider bodies, settings, request URLs or tracebacks.
    if ok:
        report["failure_category"] = None
    elif report["failure_category"] not in FAILURES:
        report["failure_category"] = "internal_error"
    if report["stream_failure_category"] not in STREAM_FAILURES:
        report["stream_failure_category"] = None
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
