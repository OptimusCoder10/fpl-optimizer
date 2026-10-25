"""J49 bounded capture protocol. Caller holds the ingestion advisory lock.

A post-read-back immutable receipt is necessary to persist qualification. The
sidecar is the last bundle-content upload; it cannot contain its own future
verification time. Receipts are evidence, never a mutable latest pointer.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import pickle
import sys
import time
from uuid import uuid4

from fpl_optimizer.services.research_storage import aware, timestamp
from fpl_optimizer.services.shared_archive import ArchiveError

BUDGET_SECONDS = 180
MARGIN = timedelta(minutes=10)
WINDOW = timedelta(hours=24)
PROTECTED = "research-bundles/deadline-window/"
SELF_TEST = "research-bundles/self-test/"
RECEIPT_RESERVE = 4096
INPUT_VERSION = "research-inputs-j47-v1"
SAFE_CATEGORIES = {
    "ok", "self_test_ok", "no_target", "outside_window", "margin", "slot_exists",
    "already_running", "not_configured", "capacity", "listing_incomplete", "timestamp_invalid",
    "timestamp_missing", "clock_uncertain", "checksum", "invalid_evidence", "timeout",
    "transport", "storage_response", "duplicate_key", "internal", "worker_failed",
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


class CaptureClock:
    """Detect wall-clock jumps and disagreement with authenticated server Date."""

    def __init__(self, wall=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.wall, self.monotonic = wall, monotonic
        self.start = aware(wall())
        self.tick = monotonic()

    def now(self):
        now = aware(self.wall())
        if abs((now - self.start).total_seconds() - (self.monotonic() - self.tick)) > 2:
            raise ArchiveError("clock_uncertain")
        return now

    def observe(self, response):
        now = self.now()
        if abs((now - aware(response.server_date)).total_seconds()) > 120:
            raise ArchiveError("clock_uncertain")
        return now


@dataclass(frozen=True)
class CaptureResult:
    status: str
    category: str
    completion: datetime | None = None
    bytes_used: int = 0
    margin_seconds: float | None = None
    players: int = 0
    exclusions: int = 0


@dataclass
class Attempt:
    started_upload: bool = False


async def build_in_process(view):
    """Only a plain captured view crosses into a killable, credential-free worker."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fpl_optimizer.services.research_capture_worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        # No database/storage credentials or project settings are inherited.
        env={"PATH": "/usr/bin:/bin"},
    )
    try:
        output, _ = await process.communicate(pickle.dumps(view))
        if process.returncode != 0:
            raise ArchiveError("worker_failed")
        # Output is from our own child, never from storage or HTTP.
        return pickle.loads(output)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def inventory(storage, clock):
    objects, dates = await storage.inventory()
    if not dates:
        raise ArchiveError("listing_incomplete")
    now = clock.now()
    if any(abs((now - aware(date)).total_seconds()) > 120 for date in dates):
        raise ArchiveError("clock_uncertain")
    if len({item.key for item in objects}) != len(objects):
        raise ArchiveError("listing_incomplete")
    if any(type(item.size) is not int or item.size < 0 or not item.times for item in objects):
        raise ArchiveError("listing_incomplete")
    return {item.key: item for item in objects}


async def read(storage, key, clock):
    response = await storage.get(key)
    clock.observe(response)
    return response


async def verified_records(storage, objects, prefix, deadline, clock):
    """Derive qualified historical records; never re-date an old verification.

    Missing receipts are incomplete attempts. A corrupt receipt is not qualified;
    a transport failure aborts discovery because slot membership is uncertain.
    """
    qualified = []
    for key, info in sorted(objects.items()):
        if not key.startswith(prefix) or not key.endswith(".verified.json"):
            continue
        raw = (await read(storage, key, clock)).content
        try:
            receipt = json.loads(raw)
            base = key.removesuffix(".verified.json")
            completed = timestamp(receipt["completion_at"])
            observed = tuple(timestamp(t) for t in receipt["sidecar_storage_times"])
            runner_verified = timestamp(receipt["runner_verified_at"])
            if not observed or completed != max(runner_verified, *observed):
                continue
            if receipt["schema"] != "research-verification-v1" or receipt["deadline"] != deadline.isoformat():
                continue
            meta_key, payload_key = base + ".meta.json", base + ".json.gz"
            if meta_key not in objects or payload_key not in objects:
                continue
            meta_raw = (await read(storage, meta_key, clock)).content
            compressed = (await read(storage, payload_key, clock)).content
            meta = json.loads(meta_raw)
            if (digest(meta_raw) != receipt["sha256_metadata"]
                    or digest(compressed) != receipt["sha256_gzip"]
                    or meta["sha256_gzip"] != receipt["sha256_gzip"]
                    or digest(gzip.decompress(compressed)) != meta["sha256_canonical"]):
                continue
            captured = timestamp(meta["capture_time"])
            slot = int(captured.timestamp()) // 1800
            if (meta["schema"] != "research-capture-v1" or meta["prospective"] is not True
                    or meta["deadline"] != deadline.isoformat() or receipt["slot"] != slot
                    or not timedelta(0) < deadline - captured <= WINDOW
                    or completed < captured
                    or max(completed, *info.times, *objects[meta_key].times) + MARGIN >= deadline):
                continue
            qualified.append((completed, key, slot))
        except (ValueError, KeyError, TypeError, OSError, EOFError, ArchiveError):
            continue
    return tuple(qualified)


def prospective_record(records):
    return max(records, key=lambda record: (record[0], record[1]), default=None)


async def capture(view, storage, *, allowance, clock, attempt, build=build_in_process,
                  self_test=False):
    """Run inside the caller's total 180-second timeout and held lock."""
    captured = clock.now() if self_test else aware(view.as_of)
    if not self_test:
        target = view.target
        if target is None:
            return CaptureResult("skipped", "no_target")
        deadline = aware(target.deadline_time)
        if not timedelta(0) < deadline - captured <= WINDOW:
            return CaptureResult("skipped", "outside_window")
        if clock.now() + MARGIN >= deadline:
            return CaptureResult("skipped", "margin")
        prefix = f"{PROTECTED}{view.season_id}/{target.id}/"
    else:
        deadline = None
        prefix = SELF_TEST
    objects = await inventory(storage, clock)
    slot = int(captured.timestamp()) // 1800
    if not self_test:
        records = await verified_records(storage, objects, prefix, deadline, clock)
        if any(record[2] == slot for record in records):
            return CaptureResult("skipped", "slot_exists")
        canonical, compressed = await build(view)
    else:
        canonical = encoded({"schema": "research-storage-self-test-v1", "synthetic": True})
        compressed = gzip.compress(canonical, mtime=0)
    document = json.loads(canonical)
    meta = {
        "schema": "research-capture-v1", "prospective": not self_test,
        "capture_time": captured.isoformat(), "deadline": deadline.isoformat() if deadline else None,
        "sha256_canonical": digest(canonical), "sha256_gzip": digest(compressed),
        "canonical_bytes": len(canonical), "gzip_bytes": len(compressed), "slot": slot,
        "versions": document.get("versions", {}) if not self_test else {},
    }
    metadata = encoded(meta)
    used = sum(item.size for item in objects.values())
    if used + len(compressed) + len(metadata) + RECEIPT_RESERVE > allowance:
        return CaptureResult("missed", "capacity", bytes_used=used)
    if not self_test and clock.now() + MARGIN >= deadline:
        return CaptureResult("skipped", "margin")
    base = prefix + captured.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
    attempt.started_upload = True
    clock.observe(await storage.put(base + ".json.gz", compressed, "application/gzip"))
    sidecar_write = await storage.put(base + ".meta.json", metadata, "application/json")
    clock.observe(sidecar_write)
    payload_read = await read(storage, base + ".json.gz", clock)
    sidecar_read = await read(storage, base + ".meta.json", clock)
    if (sidecar_read.content != metadata or digest(payload_read.content) != meta["sha256_gzip"]
            or digest(gzip.decompress(payload_read.content)) != meta["sha256_canonical"]):
        raise ArchiveError("checksum")
    objects = await inventory(storage, clock)
    sidecar = objects.get(base + ".meta.json")
    payload = objects.get(base + ".json.gz")
    if (sidecar is None or payload is None or sidecar.size != len(metadata)
            or payload.size != len(compressed)):
        raise ArchiveError("listing_incomplete")
    runner_verified = clock.now()
    sidecar_times = sidecar_write.times + sidecar_read.times + sidecar.times
    completed = max(runner_verified, *sidecar_times)
    if not self_test and completed + MARGIN >= deadline:
        return CaptureResult("unqualified", "margin", completed)
    receipt = encoded({
        "schema": "research-verification-v1", "completion_at": completed.isoformat(),
        "deadline": deadline.isoformat() if deadline else None, "slot": slot,
        "sha256_metadata": digest(metadata), "sha256_gzip": meta["sha256_gzip"],
        "runner_verified_at": runner_verified.isoformat(),
        "sidecar_storage_times": [value.isoformat() for value in sidecar_times],
        "upload_server_date": sidecar_write.server_date.isoformat(),
        "readback_server_date": sidecar_read.server_date.isoformat(),
        "listing_timestamps": [value.isoformat() for value in sidecar.times],
    })
    if len(receipt) > RECEIPT_RESERVE:
        raise ArchiveError("invalid_evidence")
    receipt_write = await storage.put(base + ".verified.json", receipt, "application/json")
    clock.observe(receipt_write)
    receipt_read = await read(storage, base + ".verified.json", clock)
    if receipt_read.content != receipt:
        raise ArchiveError("checksum")
    persisted = max(clock.now(), *receipt_write.times, *receipt_read.times)
    if not self_test and persisted + MARGIN >= deadline:
        return CaptureResult("unqualified", "margin", completed)
    return CaptureResult("captured", "self_test_ok" if self_test else "ok", completed,
                         used + len(compressed) + len(metadata) + len(receipt),
                         (deadline - persisted).total_seconds() if deadline else None,
                         len(document.get("players", ())), len(document.get("exclusions", ())))


async def isolated(operation, attempt, *, budget=BUDGET_SECONDS):
    try:
        async with asyncio.timeout(budget):
            return await operation()
    except TimeoutError:
        category = "timeout"
    except ArchiveError as error:
        category = error.category if error.category in SAFE_CATEGORIES else "internal"
    except Exception:
        category = "internal"
    return CaptureResult("unqualified" if attempt.started_upload else "missed", category)
