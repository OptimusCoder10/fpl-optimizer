"""Bounded public FPL HTTP boundary; no database access or payload logging."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import random
from time import monotonic

import httpx
from pydantic import BaseModel

from fpl_optimizer.schemas.bootstrap import BootstrapEnvelope
from fpl_optimizer.schemas.element_summary import ElementSummaryEnvelope
from fpl_optimizer.schemas.fetch import FetchResult
from fpl_optimizer.schemas.fixtures import FixturesEnvelope
from fpl_optimizer.schemas.manager_import import Entry, History, Picks, Transfers

BASE_URL = "https://fantasy.premierleague.com/api/"
USER_AGENT = "FPL-Optimizer/0.1 (public-data ingestion; sequential history requests)"
PLAYER_CALL_INTERVAL = 0.2
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _reject_constant(value: str):
    raise ValueError("non-finite JSON number")


class FPLClient:
    def __init__(
        self, *, transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
        elapsed_clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        budget_seconds: float = 660,
    ):
        if not math.isfinite(budget_seconds) or budget_seconds <= 0:
            raise ValueError("HTTP budget must be finite and positive")
        self.clock, self.elapsed_clock = clock, elapsed_clock
        self.sleep, self.jitter = sleep, jitter
        self.deadline = elapsed_clock() + budget_seconds
        self.exhausted = False
        self._retry_not_before = 0.0
        self._last_player_end: float | None = None
        self.player_pacing_seconds = 0.0
        self._player_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=BASE_URL, transport=transport, trust_env=False,
            follow_redirects=False, timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                     "Accept-Encoding": "identity"},
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.elapsed_clock())

    def _budget_failure(self):
        self.exhausted = True
        return FetchResult("transient_failure", self.clock(), category="budget_exhausted",
                           budget_exhausted=True)

    async def _delay(self, seconds: float) -> bool:
        if self.exhausted or seconds >= self.remaining():
            self.exhausted = True
            return False
        if seconds > 0:
            await self.sleep(seconds)
        return self.remaining() > 0

    def _retry_after(self, value: str | None) -> float:
        if value is None:
            return 0.0
        try:
            if value.strip().isdigit():
                return float(value)
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                return 0.0
            return max(0.0, (when - self.clock()).total_seconds())
        except (ValueError, OverflowError, TypeError):
            return 0.0

    async def _fetch[T: BaseModel](self, path: str, model: type[T], *, player=False) -> FetchResult[T]:
        for attempt in range(MAX_ATTEMPTS):
            if self.exhausted or self.remaining() <= 0:
                return self._budget_failure()
            # A Retry-After on the final attempt also delays the next endpoint/player.
            if not await self._delay(max(0.0, self._retry_not_before - self.elapsed_clock())):
                return self._budget_failure()
            if player and self._last_player_end is not None:
                pause = max(0.0, PLAYER_CALL_INTERVAL - (self.elapsed_clock() - self._last_player_end))
                pacing_started = self.elapsed_clock()
                try:
                    pacing_ok = await self._delay(pause)
                finally:
                    if pause > 0:
                        self.player_pacing_seconds += self.elapsed_clock() - pacing_started
                if not pacing_ok:
                    return self._budget_failure()
            retry_after = 0.0
            try:
                # A total deadline also bounds slow streaming, beyond per-read timeouts.
                async with asyncio.timeout(min(REQUEST_TIMEOUT_SECONDS, self.remaining())):
                    async with self._client.stream("GET", path) as response:
                        status = response.status_code
                        if status == 404:
                            return FetchResult("not_found", self.clock(), category="not_found")
                        if status == 429 or 500 <= status <= 599:
                            retry_after = self._retry_after(response.headers.get("Retry-After"))
                        elif status != 200 or "Content-Range" in response.headers:
                            return FetchResult("truncated_or_invalid", self.clock(), category="truncated_or_invalid")
                        else:
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes():
                                chunks.extend(chunk)
                                if len(chunks) > MAX_RESPONSE_BYTES:
                                    raise ValueError("oversized response")
                            length = response.headers.get("Content-Length")
                            # num_bytes_downloaded is encoded wire length for compressed bodies.
                            wire_size = (response.num_bytes_downloaded
                                         if response.headers.get("Content-Encoding") else len(chunks))
                            if length is not None and int(length) != wire_size:
                                raise ValueError("truncated response")
                            raw = json.loads(chunks, parse_constant=_reject_constant)
                            if path == "fixtures/" and raw == []:
                                return FetchResult("truncated_or_invalid", self.clock(), category="incomplete_fixtures")
                            data = model.model_validate(raw)
                            if self.remaining() <= 0:
                                return self._budget_failure()
                            return FetchResult(
                                "success", self.clock(), data=data,
                                raw_shared_payload=(raw if path in {"bootstrap-static/", "fixtures/"}
                                                    else None),
                            )
            except (httpx.RemoteProtocolError, httpx.DecodingError, ValueError):
                # Incomplete framing/JSON/schema never supplies usable evidence.
                return FetchResult("truncated_or_invalid", self.clock(), category="truncated_or_invalid")
            except (httpx.TransportError, TimeoutError):
                pass
            finally:
                if player:
                    self._last_player_end = self.elapsed_clock()
            self._retry_not_before = max(
                self._retry_not_before, self.elapsed_clock() + retry_after,
            )
            if self.remaining() <= 0 or retry_after >= self.remaining():
                return self._budget_failure()
            if attempt + 1 < MAX_ATTEMPTS:
                delay = max(retry_after, 2 ** attempt + self.jitter() * 0.25)
                if not await self._delay(delay):
                    return self._budget_failure()
        return FetchResult("transient_failure", self.clock(), category="transient_failure")

    async def bootstrap(self) -> FetchResult[BootstrapEnvelope]:
        return await self._fetch("bootstrap-static/", BootstrapEnvelope)

    async def fixtures(self) -> FetchResult[FixturesEnvelope]:
        return await self._fetch("fixtures/", FixturesEnvelope)

    async def element_summary(self, player_id: int) -> FetchResult[ElementSummaryEnvelope]:
        _positive_id(player_id)
        async with self._player_lock:
            return await self._fetch(f"element-summary/{player_id}/", ElementSummaryEnvelope, player=True)

    async def entry(self, entry_id: int) -> FetchResult[Entry]:
        _positive_id(entry_id)
        return await self._fetch(f"entry/{entry_id}/", Entry)

    async def history(self, entry_id: int) -> FetchResult[History]:
        _positive_id(entry_id)
        return await self._fetch(f"entry/{entry_id}/history/", History)

    async def transfers(self, entry_id: int) -> FetchResult[Transfers]:
        _positive_id(entry_id)
        return await self._fetch(f"entry/{entry_id}/transfers/", Transfers)

    async def picks(self, entry_id: int, event: int) -> FetchResult[Picks]:
        _positive_id(entry_id)
        _positive_id(event)
        return await self._fetch(f"entry/{entry_id}/event/{event}/picks/", Picks)


def _positive_id(value: int):
    if type(value) is not int or value <= 0:
        raise ValueError("endpoint ID must be a positive integer")
