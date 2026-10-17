"""Fake transport and clocks only; never make network requests."""

import asyncio
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest

from fpl_optimizer.services.fpl_http import FPLClient, USER_AGENT
from synthetic_fpl import Clock, full_season, history


def client_for(handler, clock=None, **kwargs):
    clock = clock or Clock()
    return FPLClient(transport=httpx.MockTransport(handler), clock=clock.now,
                     elapsed_clock=clock.monotonic, sleep=clock.sleep,
                     jitter=lambda: 0.5, **kwargs)


@pytest.mark.parametrize("error,attempts,status", [
    (httpx.ConnectError, 3, "transient_failure"),
    (httpx.ReadTimeout, 3, "transient_failure"),
    (httpx.ReadError, 3, "transient_failure"),
    (429, 3, "transient_failure"), (500, 3, "transient_failure"),
    (503, 3, "transient_failure"), (599, 3, "transient_failure"),
    (404, 1, "not_found"), (400, 1, "truncated_or_invalid"),
    (403, 1, "truncated_or_invalid"), (302, 1, "truncated_or_invalid"),
    (httpx.RemoteProtocolError, 1, "truncated_or_invalid"),
])
async def test_retry_counts_per_error_type(error, attempts, status):
    calls = []
    clock = Clock()
    def handler(request):
        calls.append(request)
        if isinstance(error, type):
            raise error("SENSITIVE injected upstream exception")
        return httpx.Response(error)
    async with client_for(handler, clock) as client:
        result = await client.transfers(1)
    assert result.status == status and result.data is None
    assert len(calls) == attempts
    assert clock.sleeps == ([1.125, 2.125] if attempts == 3 else [])
    assert calls[0].headers["User-Agent"] == USER_AGENT
    assert all(value is not None and value > 0 for value in calls[0].extensions["timeout"].values())
    assert result.manager_response().status != "complete"


@pytest.mark.parametrize("as_date", [False, True])
async def test_retry_after_over_budget_ends_cleanly(as_date):
    clock = Clock()
    calls = []
    value = format_datetime(clock.now() + timedelta(seconds=60), usegmt=True) if as_date else "60"
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": value})
    async with client_for(handler, clock, budget_seconds=10) as client:
        result = await client.transfers(1)
        again = await client.entry(1)
    assert result.budget_exhausted and again.budget_exhausted
    assert len(calls) == 1 and clock.sleeps == []


@pytest.mark.parametrize("as_date", [False, True])
async def test_retry_after_is_honoured_before_success(as_date):
    clock = Clock()
    count = 0
    value = format_datetime(clock.now() + timedelta(seconds=5), usegmt=True) if as_date else "5"
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(503, headers={"Retry-After": value}) if count == 1 else httpx.Response(200, json=[])
    async with client_for(handler, clock) as client:
        result = await client.transfers(1)
    assert result.status == "success" and result.data.root == []
    assert clock.sleeps == [5.0]


@pytest.mark.parametrize("response", [
    lambda: httpx.Response(200, content=b"["),
    lambda: httpx.Response(200, json={}),
    lambda: httpx.Response(200, content=b"[]", headers={"Content-Length": "10"}),
    lambda: httpx.Response(206, json=[]),
    lambda: httpx.Response(200, json=[], headers={"Content-Range": "bytes 0-1/10"}),
    lambda: httpx.Response(200, content=b"[NaN]"),
])
async def test_invalid_or_truncated_payload_is_not_retried(response):
    calls = []
    def handler(request):
        calls.append(request)
        return response()
    async with client_for(handler) as client:
        result = await client.transfers(1)
    assert result.status == "truncated_or_invalid" and result.data is None
    assert len(calls) == 1


async def test_player_pacing_is_sequential_including_concurrent_callers_and_retries():
    clock = Clock()
    starts, ends = [], []
    async def handler(request):
        starts.append(clock.monotonic())
        await asyncio.sleep(0)
        clock.advance(0.05)
        ends.append(clock.monotonic())
        if len(starts) == 1:
            return httpx.Response(500)
        return httpx.Response(200, json=history(int(request.url.path.split("/")[-2])))
    async with client_for(handler, clock) as client:
        results = await asyncio.gather(client.element_summary(1), client.element_summary(2))
    assert all(r.status == "success" for r in results)
    assert len(starts) == 3
    assert all(start - end >= 0.2 - 1e-9 for start, end in zip(starts[1:], ends))


async def test_player_pacing_counter_excludes_retry_waits():
    clock = Clock()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=history(int(request.url.path.split("/")[-2])))

    async with client_for(handler, clock) as client:
        assert (await client.element_summary(1)).status == "success"
        assert (await client.element_summary(2)).status == "success"
        assert calls == 3
        assert abs(client.player_pacing_seconds - 0.2) < 1e-9
        assert clock.elapsed > client.player_pacing_seconds


async def test_all_shared_endpoint_boundaries_and_paths():
    bootstrap, fixtures = full_season()
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=bootstrap if "bootstrap" in request.url.path else fixtures)
    async with client_for(handler) as client:
        assert (await client.bootstrap()).status == "success"
        assert (await client.fixtures()).status == "success"
    assert paths == ["/api/bootstrap-static/", "/api/fixtures/"]


async def test_empty_fixture_list_has_its_own_failure_category():
    async with client_for(lambda r: httpx.Response(200, json=[])) as client:
        assert (await client.fixtures()).category == "incomplete_fixtures"


async def test_truncated_stream_never_becomes_empty_transfers():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"[]"
            raise httpx.RemoteProtocolError("incomplete body SECRET")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=BrokenStream())
    async with client_for(handler) as client:
        result = await client.transfers(1)
    assert result.status == "truncated_or_invalid"
    assert result.manager_response().payload is None
    assert len(calls) == 1


async def test_total_request_timeout_is_finite(monkeypatch):
    from fpl_optimizer.services import fpl_http
    monkeypatch.setattr(fpl_http, "REQUEST_TIMEOUT_SECONDS", 0.01)
    calls = []
    async def handler(request):
        calls.append(request)
        await asyncio.Event().wait()
    async with client_for(handler) as client:
        result = await client.transfers(1)
    assert result.status == "transient_failure" and len(calls) == 3


async def test_oversized_response_is_not_retried(monkeypatch):
    from fpl_optimizer.services import fpl_http
    monkeypatch.setattr(fpl_http, "MAX_RESPONSE_BYTES", 1)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])
    async with client_for(handler) as client:
        assert (await client.transfers(1)).status == "truncated_or_invalid"
    assert len(calls) == 1


async def test_final_attempt_retry_after_delays_the_next_player():
    clock, starts = Clock(), []
    def handler(request):
        starts.append(clock.monotonic())
        if len(starts) <= 3:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json=history(2))
    async with client_for(handler, clock) as client:
        assert (await client.element_summary(1)).status == "transient_failure"
        assert (await client.element_summary(2)).status == "success"
    assert starts == [0, 5, 10, 15]
