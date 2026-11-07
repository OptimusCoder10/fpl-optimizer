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


PREFIX = "research-bundles/self-test/owner-gates/"
A = b"A" * 64
B = b"B" * 64


def empty_report():
    return dict(overwrite_rejected="no", a_intact="no",
                created_minus_before_seconds=None, created_minus_after_seconds=None,
                created_minus_runner_seconds=None, runner_minus_provider_seconds=None,
                upload_date_minus_after_seconds=None, get_date_minus_runner_seconds=None,
                last_modified_minus_created_seconds=None, updated_minus_created_seconds=None,
                last_accessed_minus_created_seconds=None)


async def probe(report):
    # No caller-supplied destination: neither arguments nor environment select it.
    folder = PREFIX + uuid4().hex
    key = folder + "/probe.bin"
    settings = SimpleNamespace(
        fpl_storage_url=os.environ["FPL_STORAGE_URL"],
        fpl_storage_bucket=os.environ["FPL_STORAGE_BUCKET"],
        fpl_storage_key=SecretStr(os.environ["FPL_STORAGE_KEY"]),
        fpl_storage_allowance_bytes=1,
    )
    # Reuse configuration validation only; expose no general storage operations.
    config = SupabaseStorage(settings)
    wall = lambda: datetime.now(timezone.utc)

    async def slow_bytes():
        yield A[:32]
        await asyncio.sleep(30)
        yield A[32:]

    async with httpx.AsyncClient(
        base_url=config._url, headers={"apikey": config._key},
        trust_env=False, follow_redirects=False, timeout=60,
    ) as client:
        headers = {"Content-Type": "application/octet-stream", "Content-Length": "64"}
        headers["x-upsert"] = "false"
        before, tick = wall(), time.monotonic()
        uploaded = await client.post(f"/object/{config._bucket}/{key}",
                                     content=slow_bytes(), headers=headers)
        after, tock = wall(), time.monotonic()
        uploaded.raise_for_status()
        if abs((after - before).total_seconds() - (tock - tick)) > 2:
            raise ValueError("clock jump")
        report["upload_date_minus_after_seconds"] = (
            aware(parsedate_to_datetime(uploaded.headers["Date"])) - after).total_seconds()
        duplicate = await client.post(f"/object/{config._bucket}/{key}", content=B, headers=headers)
        rejected = duplicate.status_code == 409
        if duplicate.status_code == 400:
            body = duplicate.json()
            rejected = (body.get("errorCode") or body.get("code") or body.get("error")) in {
                "AssetAlreadyExists", "already_exists", "Duplicate", "Asset Already Exists",
            }
        report["overwrite_rejected"] = "yes" if rejected else "no"
        fetched = await client.get(f"/object/authenticated/{config._bucket}/{key}",
                                   headers={"cache-control": "no-cache, no-store"})
        get_runner = wall()
        fetched.raise_for_status()
        report["a_intact"] = "yes" if fetched.content == A else "no"
        report["get_date_minus_runner_seconds"] = (
            aware(parsedate_to_datetime(fetched.headers["Date"])) - get_runner).total_seconds()
        # Read just this new synthetic folder; listing never chooses a write target.
        listing = await client.post(f"/object/list/{config._bucket}", json={
            "prefix": folder, "limit": 2, "offset": 0,
            "sortBy": {"column": "name", "order": "asc"},
        })
        runner = wall()
        listing.raise_for_status()
        rows = listing.json()
        if len(rows) != 1 or rows[0]["name"] != "probe.bin" or rows[0]["metadata"]["size"] != 64:
            raise ValueError("unexpected synthetic listing")
        created = timestamp(rows[0]["created_at"])
        for label, bound in (("before", before), ("after", after), ("runner", runner)):
            report[f"created_minus_{label}_seconds"] = (created - bound).total_seconds()
        report["runner_minus_provider_seconds"] = (
            runner - aware(parsedate_to_datetime(listing.headers["Date"]))).total_seconds()
        for field, label in (("updated_at", "updated"), ("last_accessed_at", "last_accessed")):
            if rows[0].get(field):
                report[f"{label}_minus_created_seconds"] = (timestamp(rows[0][field]) - created).total_seconds()
        if fetched.headers.get("Last-Modified"):
            report["last_modified_minus_created_seconds"] = (
                aware(parsedate_to_datetime(fetched.headers["Last-Modified"])) - created).total_seconds()
        return rejected and fetched.content == A


async def bounded(report):
    async with asyncio.timeout(150):
        return await probe(report)


def main():
    logging.disable(logging.CRITICAL)
    report = empty_report()
    ok = False
    try:
        if len(sys.argv) != 1: raise ValueError("no caller-supplied destination")
        ok = asyncio.run(bounded(report))
    except Exception:
        pass  # Never print provider bodies, settings, request URLs or tracebacks.
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
