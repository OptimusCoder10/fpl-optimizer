"""HTTP outcomes feed the existing manager evidence service without fake empties."""

from copy import deepcopy
from datetime import timedelta

import httpx
import pytest

from fpl_optimizer.services.manager_fetch import fetch_manager_import
from synthetic_fpl import Clock
from test_fpl_http import client_for
from test_manager_import import payloads, price, SEASON, SYNTHETIC_ENTRY_ID


def router(payloads, calls, transfer_response=None, picks_status=None):
    def handler(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/history/"):
            return httpx.Response(200, json=payloads["history"])
        if path.endswith("/transfers/"):
            return transfer_response() if transfer_response else httpx.Response(200, json=[])
        if path.endswith("/picks/"):
            event = int(path.split("/")[-3])
            if picks_status and event in picks_status:
                return httpx.Response(picks_status[event])
            data = deepcopy(payloads["gw1_picks"] if event == 1 else payloads["latest_picks"])
            data["entry_history"]["event"] = event
            return httpx.Response(200, json=data)
        return httpx.Response(200, json=payloads["entry"])
    return handler


async def fetch(client):
    return await fetch_manager_import(client, season_id=SEASON, entry_id=SYNTHETIC_ENTRY_ID,
                                      deadlines={i: client.clock()-timedelta(days=10-i) for i in range(1, 6)})


@pytest.mark.parametrize("response", [
    lambda: httpx.Response(503), lambda: httpx.Response(404),
    lambda: httpx.Response(200, content=b"["), lambda: httpx.Response(200, json={}),
    lambda: httpx.Response(200, content=b"[]", headers={"Content-Length": "99"}),
])
async def test_transfers_fetch_failure_is_never_empty_history(payloads, response):
    clock, calls = Clock(), []
    async with client_for(router(payloads, calls, response), clock) as client:
        snapshot = await fetch(client)
    assert snapshot.transfers.data is None
    assert snapshot.transfers.status != "complete"
    evidence = price(snapshot, calculated_at=clock.now())
    assert not evidence.checks.complete_transfers_without_permanent_records
    assert evidence.source == "unresolved"
    assert not any("element-summary" in p or "bootstrap" in p or "fixtures" in p for p in calls)


async def test_valid_empty_transfers_can_support_gw1_fallback(payloads):
    clock, calls = Clock(), []
    async with client_for(router(payloads, calls), clock) as client:
        snapshot = await fetch(client)
    assert snapshot.transfers.data.root == [] and snapshot.transfers.status == "complete"
    assert price(snapshot, calculated_at=clock.now()).source == "assumed held since GW1"
    assert calls.count(f"/api/entry/{SYNTHETIC_ENTRY_ID}/event/1/picks/") == 1


async def test_gw1_404_disables_only_fallback(payloads):
    calls = []
    async with client_for(router(payloads, calls, picks_status={1: 404})) as client:
        snapshot = await fetch(client)
    assert snapshot.latest_public.event == 5
    assert snapshot.picks_by_event[1].status == "unavailable"
    assert price(snapshot).source == "unresolved"
    assert calls.count(f"/api/entry/{SYNTHETIC_ENTRY_ID}/event/1/picks/") == 1


async def test_latest_404_tries_earlier_picks(payloads):
    calls = []
    async with client_for(router(payloads, calls, picks_status={5: 404})) as client:
        snapshot = await fetch(client)
    assert snapshot.latest_public.event == 4
    assert calls.count(f"/api/entry/{SYNTHETIC_ENTRY_ID}/event/5/picks/") == 1


async def test_latest_gw1_reuses_one_fetch(payloads):
    calls = []
    payloads["entry"]["current_event"] = 1
    payloads["history"]["current"] = payloads["history"]["current"][:1]
    async with client_for(router(payloads, calls)) as client:
        snapshot = await fetch(client)
    assert snapshot.latest_public.event == 1
    assert sum(p.endswith("/picks/") for p in calls) == 1


async def test_free_hit_latest_fetches_permanent_earlier_snapshot(payloads):
    calls = []
    payloads["latest_picks"]["active_chip"] = "freehit"
    payloads["history"]["chips"] = [dict(name="freehit", event=5, time="2030-09-01T12:00:00Z")]
    base = router(payloads, calls)
    def handler(request):
        response = base(request)
        if "/event/4/" in request.url.path:
            data = response.json()
            data["active_chip"] = None
            return httpx.Response(200, json=data)
        return response
    async with client_for(handler) as client:
        snapshot = await fetch(client)
    assert snapshot.latest_public.event == 5
    assert snapshot.permanent_snapshot.event == 4
