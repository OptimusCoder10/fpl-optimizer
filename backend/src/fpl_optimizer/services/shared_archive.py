"""Post-publication archive of public shared HTTP responses only."""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import logging
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from fpl_optimizer.config import Settings

ARCHIVE_SCHEMA_VERSION = "raw-shared-snapshot-v1"
SOURCE_ENDPOINTS = ("/api/bootstrap-static/", "/api/fixtures/")
INTRADAY_RETENTION = timedelta(days=30)
_storage_request_active = ContextVar("storage_request_active", default=False)


class _SuppressStorageTransportLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _storage_request_active.get()


_storage_log_filter = _SuppressStorageTransportLogs()
for _logger_name in ("httpx", "httpcore.connection", "httpcore.http11",
                     "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_logger_name).addFilter(_storage_log_filter)


class ArchiveError(Exception):
    """Only a safe category; never include a URL, token or response body."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    created_at: datetime


@dataclass(frozen=True)
class ArchiveResult:
    status: str
    category: str | None = None
    key: str | None = None
    checksum: str | None = None
    bytes_used: int | None = None
    allowance_warning: bool = False


def expired_intraday_keys(objects: list[StoredObject], now: datetime) -> list[str]:
    """Deadline-window keys have a distinct namespace and are never age-pruned here."""
    return [item.key for item in objects
            if item.key.startswith("raw-shared/intraday/")
            and item.created_at + INTRADAY_RETENTION < now]


def _target_event(bootstrap: dict, collected_at: datetime) -> tuple[int | None, datetime | None]:
    future = []
    for event in bootstrap["events"]:
        value = event.get("deadline_time")
        if value is None:
            continue
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if deadline > collected_at:
            future.append((deadline, event["id"]))
    if not future:
        return None, None
    deadline, event_id = min(future)
    return event_id, deadline


def prepare_shared_archive(
    *, bootstrap: dict, fixtures: list, season_id: str, rules_version: str,
    collected_from: datetime, collected_to: datetime, published_at: datetime,
) -> tuple[bytes, bytes, bool, str]:
    """No manager endpoint or state can enter this fixed two-field envelope."""
    if not isinstance(bootstrap, dict) or not isinstance(fixtures, list):
        raise ArchiveError("raw_payload_unavailable")
    payload = json.dumps(
        {"bootstrap_static": bootstrap, "fixtures": fixtures},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    compressed = gzip.compress(payload, mtime=0)
    checksum = hashlib.sha256(compressed).hexdigest()
    target_event, deadline = _target_event(bootstrap, collected_to)
    deadline_window = (deadline is not None
                       and timedelta(0) < deadline - published_at <= timedelta(hours=24))
    metadata = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "rules_version": rules_version,
        "model_version": None,
        "season_id": season_id,
        "collection_started_at": collected_from.isoformat(),
        "collection_finished_at": collected_to.isoformat(),
        "published_at": published_at.isoformat(),
        "source_endpoints": SOURCE_ENDPOINTS,
        "sha256_gzip": checksum,
        "target_event": target_event,
        "target_deadline": deadline.isoformat() if deadline else None,
        "capture_class": "deadline_window" if deadline_window else "intraday",
    }
    return compressed, json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode(), deadline_window, checksum


async def archive_shared_publication(
    storage, *, bootstrap: dict, fixtures: list, season_id: str, rules_version: str,
    collected_from: datetime, collected_to: datetime, published_at: datetime,
    allowance_bytes: int,
) -> ArchiveResult:
    """Account for the whole bucket; prune only expired intraday objects."""
    compressed, metadata, deadline_window, checksum = prepare_shared_archive(
        bootstrap=bootstrap, fixtures=fixtures, season_id=season_id,
        rules_version=rules_version, collected_from=collected_from,
        collected_to=collected_to, published_at=published_at,
    )
    objects = await storage.list_objects()
    expired = expired_intraday_keys(objects, published_at)
    if expired:
        await storage.delete_objects(expired)
    used = sum(item.size for item in objects if item.key not in expired)
    projected = used + len(compressed) + len(metadata)
    warning = projected >= allowance_bytes * 0.8
    if projected > allowance_bytes:
        return ArchiveResult("missed", "capacity", bytes_used=used,
                             allowance_warning=warning)
    kind = "deadline-window" if deadline_window else "intraday"
    timestamp = published_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = f"raw-shared/{kind}/{season_id}/{timestamp}-{uuid4().hex}"
    await storage.upload(f"{base}.json.gz", compressed, "application/gzip")
    await storage.upload(f"{base}.meta.json", metadata, "application/json")
    return ArchiveResult("captured", key=base, checksum=checksum,
                         bytes_used=projected, allowance_warning=warning)


class SupabaseStorage:
    """Small REST adapter. All transport and server errors become safe categories."""

    def __init__(self, settings: Settings):
        if not all((settings.fpl_storage_url, settings.fpl_storage_bucket,
                    settings.fpl_storage_key, settings.fpl_storage_allowance_bytes)):
            raise ArchiveError("not_configured")
        parsed = urlsplit(settings.fpl_storage_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or not re.fullmatch(
                    r"[a-z0-9][a-z0-9-]{2,62}", settings.fpl_storage_bucket)):
            raise ArchiveError("not_configured")
        self._url = f"{settings.fpl_storage_url.rstrip('/')}/storage/v1"
        self._bucket = settings.fpl_storage_bucket
        self._key = settings.fpl_storage_key.get_secret_value()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        token = _storage_request_active.set(True)
        try:
            async with httpx.AsyncClient(
                base_url=self._url, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(20.0),
                headers={"apikey": self._key},
            ) as client:
                response = await client.request(method, path, **kwargs)
        except (httpx.HTTPError, TimeoutError):
            raise ArchiveError("transport") from None
        finally:
            _storage_request_active.reset(token)
        if response.status_code >= 300:
            if response.status_code == 409:
                raise ArchiveError("duplicate_key")
            if response.status_code == 400:
                try:
                    code = response.json().get("errorCode") or response.json().get("code")
                except (ValueError, AttributeError):
                    code = None
                if code in {"AssetAlreadyExists", "already_exists", "Duplicate"}:
                    raise ArchiveError("duplicate_key")
            raise ArchiveError("storage_response")
        return response

    async def list_objects(self) -> list[StoredObject]:
        found: list[StoredObject] = []
        folders = [""]
        while folders:
            folder = folders.pop()
            offset = 0
            while True:
                response = await self._request(
                    "POST", f"/object/list/{self._bucket}",
                    json={"prefix": folder, "limit": 100, "offset": offset,
                          "sortBy": {"column": "name", "order": "asc"}},
                )
                try:
                    rows = response.json()
                    if not isinstance(rows, list):
                        raise ValueError
                    for row in rows:
                        name = row["name"]
                        if not isinstance(name, str) or not name or "/" in name:
                            raise ValueError
                        path = f"{folder}/{name}" if folder else name
                        if row.get("id") is None:
                            folders.append(path)
                        else:
                            size = int(row["metadata"]["size"])
                            created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                            if size < 0 or created.tzinfo is None:
                                raise ValueError
                            found.append(StoredObject(path, size, created))
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise ArchiveError("listing_invalid") from None
                if len(rows) < 100:
                    break
                offset += len(rows)
        return found

    async def upload(self, key: str, content: bytes, content_type: str) -> None:
        await self._request(
            "POST", f"/object/{self._bucket}/{key}", content=content,
            headers={"Content-Type": content_type, "cache-control": "private, max-age=0"},
        )

    async def delete_objects(self, keys: list[str]) -> None:
        for start in range(0, len(keys), 100):
            await self._request("DELETE", f"/object/{self._bucket}",
                                json={"prefixes": keys[start:start + 100]})
