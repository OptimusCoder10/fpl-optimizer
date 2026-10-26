"""Strict research-only operations over the unchanged J40 transport.

Provider timestamps are observations, not claims of atomic multi-object storage.
No delete or overwrite operation is exposed by the capture service.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from fpl_optimizer.services.shared_archive import ArchiveError, SupabaseStorage


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArchiveError("clock_uncertain")
    return value.astimezone(timezone.utc)


def timestamp(value: str) -> datetime:
    try:
        return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, AttributeError, ValueError):
        raise ArchiveError("timestamp_invalid") from None


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    times: tuple[datetime, ...]
    # Provider object creation, not HTTP Date or a last-access timestamp.
    created_at: datetime | None = None


@dataclass(frozen=True)
class StoredResponse:
    content: bytes
    times: tuple[datetime, ...]
    server_date: datetime


def _body_times(value) -> tuple[datetime, ...]:
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith(("_at", "At")) or key in {"last_modified", "LastModified"}:
                if item is not None:
                    result.append(timestamp(item))
            elif isinstance(item, (dict, list)):
                result.extend(_body_times(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(_body_times(item))
    return tuple(result)


class ResearchStorage(SupabaseStorage):
    """Require explicit 2xx, server Date and complete, stable bucket traversal."""

    async def _observed(self, method, path, **kwargs):
        response = await self._request(method, path, **kwargs)
        if not 200 <= response.status_code < 300:
            raise ArchiveError("storage_response")
        try:
            date = aware(parsedate_to_datetime(response.headers["Date"]))
            times = [date]
            if "Last-Modified" in response.headers:
                times.append(aware(parsedate_to_datetime(response.headers["Last-Modified"])))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ArchiveError("timestamp_missing") from None
        return response, date, tuple(times)

    async def put(self, key, content, content_type):
        response, date, times = await self._observed(
            "POST", f"/object/{self._bucket}/{key}", content=content,
            headers={"Content-Type": content_type, "cache-control": "private, max-age=0", "x-upsert": "false"},
        )
        try:
            body = response.json()
        except ValueError:
            raise ArchiveError("storage_response") from None
        if not isinstance(body, dict):
            raise ArchiveError("storage_response")
        return StoredResponse(b"", times + _body_times(body), date)

    async def get(self, key):
        response, date, times = await self._observed(
            "GET", f"/object/authenticated/{self._bucket}/{key}",
            headers={"cache-control": "no-cache, no-store"},
        )
        return StoredResponse(response.content, times, date)

    async def _scan(self):
        found = {}
        folders = [""]
        visited = set()
        dates = []
        while folders:
            prefix = folders.pop()
            if prefix in visited:
                raise ArchiveError("listing_incomplete")
            visited.add(prefix)
            offset = 0
            while True:
                response, date, _ = await self._observed(
                    "POST", f"/object/list/{self._bucket}",
                    json={"prefix": prefix, "limit": 100, "offset": offset,
                          "sortBy": {"column": "name", "order": "asc"}},
                )
                dates.append(date)
                try:
                    rows = response.json()
                    if not isinstance(rows, list):
                        raise ValueError
                    if not rows:
                        break
                    for row in rows:
                        name = row["name"]
                        if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
                            raise ValueError
                        key = f"{prefix}/{name}" if prefix else name
                        if row.get("id") is None:
                            if row.get("metadata") is not None or key in folders or key in visited:
                                raise ValueError
                            folders.append(key)
                        else:
                            size = row["metadata"]["size"]
                            times = _body_times(row)
                            if (key in found or type(size) is not int or size < 0
                                    or not row.get("created_at") or not times):
                                raise ValueError
                            found[key] = ObjectInfo(key, size, times, timestamp(row["created_at"]))
                    offset += len(rows)
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise ArchiveError("listing_incomplete") from None
        return tuple(sorted(found.values(), key=lambda item: item.key)), tuple(dates)

    async def inventory(self):
        first, dates1 = await self._scan()
        second, dates2 = await self._scan()
        if first != second:
            raise ArchiveError("listing_incomplete")
        return second, dates1 + dates2
