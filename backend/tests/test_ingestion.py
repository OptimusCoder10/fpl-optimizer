"""Command integration with real disposable PostgreSQL and fake HTTP only."""

from datetime import timedelta
from itertools import count
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from fpl_optimizer import ingest
from fpl_optimizer.db.advisory_lock import ingestion_lock
from fpl_optimizer.db.models import (
    CacheMetadata, Fixture, Gameweek, Player, PlayerFixtureHistory, Season, Team,
)
from fpl_optimizer.db.session import create_session_factory
from fpl_optimizer.ingest import run_ingestion
from fpl_optimizer.schemas.fixtures import SEASON_CLUB_COUNTS
from fpl_optimizer.services.player_history import read_player_history
from fpl_optimizer.services.shared_catalog import read_target_gameweek_context
from synthetic_fpl import Clock, full_season, history, SEASON
from test_fpl_http import client_for

pytestmark = pytest.mark.database
SEASONS = count(2200)


@pytest_asyncio.fixture
async def ingestion(migrated_test_engine, publication_settings, monkeypatch):
    year = next(SEASONS)
    season = f"{year}-{str(year+1)[-2:]}"
    monkeypatch.setitem(SEASON_CLUB_COUNTS, season, SEASON_CLUB_COUNTS[SEASON])
    factory = create_session_factory(migrated_test_engine)
    clock = Clock()
    bootstrap, fixtures = full_season()
    calls = []
    overrides = {}
    async def handler(request):
        path = request.url.path
        calls.append(path)
        if path in overrides:
            override = overrides[path]
            return override() if callable(override) else httpx.Response(200, json=override)
        if "bootstrap" in path:
            return httpx.Response(200, json=bootstrap)
        if path == "/api/fixtures/":
            return httpx.Response(200, json=fixtures)
        return httpx.Response(200, json=history(int(path.split("/")[-2])))
    async def run(*, budget=660, factory_override=None, transport_handler=handler, history_budget=600):
        async with client_for(transport_handler, clock, budget_seconds=budget) as client:
            return await run_ingestion(factory_override or factory, client, season_id=season,
                                       rules_version="synthetic-v1", settings=publication_settings,
                                       history_budget=timedelta(seconds=history_budget))
    return factory, season, clock, bootstrap, fixtures, calls, overrides, run


async def state(factory, season, key="shared"):
    async with factory() as session:
        return await session.get(CacheMetadata, (season, key))


async def stored(factory, season):
    async with factory() as session:
        return (
            [(r.id, r.now_cost) for r in (await session.scalars(select(Player).where(Player.season_id == season).order_by(Player.id))).all()],
            [(r.id, r.home_team_id, r.away_team_id) for r in (await session.scalars(select(Fixture).where(Fixture.season_id == season).order_by(Fixture.id))).all()],
        )


async def test_failed_validation_preserves_last_good_publication_and_success_time(ingestion, caplog):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    assert (await run()).status == "success"
    before, rows = await state(factory, season), await stored(factory, season)
    clock.advance(4*3600)
    bootstrap["elements"][0]["now_cost"] = 999
    overrides["/api/fixtures/"] = fixtures[:-1]
    with caplog.at_level(logging.INFO, logger="fpl_optimizer"):
        result = await run()
    after = await state(factory, season)
    assert result.status == "failed" and result.category == "incomplete_fixtures"
    assert await stored(factory, season) == rows
    assert after.last_success_at == before.last_success_at
    assert after.source_observed_at == before.source_observed_at
    assert after.publication_version == before.publication_version
    assert after.last_attempt_at == clock.now() and after.last_error_category == "incomplete_fixtures"
    assert after.retry_count == 1
    assert "category=incomplete_fixtures success=0 failed=1" in caplog.text


async def test_empty_target_schedule_is_blank_only_after_validation_passes(ingestion):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    overrides["/api/fixtures/"] = fixtures[:-1]
    assert (await run()).category == "incomplete_fixtures"
    assert await state(factory, season) is None
    async with factory() as session:
        assert await read_target_gameweek_context(session, season, as_of=clock.now()) is None
    overrides.clear()
    assert (await run()).shared_published
    async with factory() as session:
        target = await read_target_gameweek_context(session, season, as_of=clock.now())
    assert target.gameweek_id == 2 and set(target.fixture_counts_by_team.values()) == {0}


async def test_lock_contention_writes_nothing_and_fetches_nothing(ingestion, publication_settings, caplog):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    def forbidden_factory():
        pytest.fail("contended command must not open a write/read session")
    with caplog.at_level(logging.INFO, logger="fpl_optimizer"):
        async with ingestion_lock(publication_settings) as held:
            assert held is not None
            result = await run(factory_override=forbidden_factory)
    assert result.status == "already_running" and calls == []
    assert await state(factory, season) is None
    assert "already running" in caplog.text


async def test_command_holds_same_lock_through_all_fetches(ingestion, publication_settings):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    observed = []
    async def handler(request):
        async with ingestion_lock(publication_settings) as competing:
            assert competing is None
        observed.append(request.url.path)
        path = request.url.path
        payload = (bootstrap if "bootstrap" in path else fixtures if path == "/api/fixtures/"
                   else history(int(path.split("/")[-2])))
        return httpx.Response(200, json=payload)
    assert (await run(transport_handler=handler)).status == "success"
    assert len(observed) == 4
    async with ingestion_lock(publication_settings) as released:
        assert released is not None


async def test_player_a_http_failure_b_commits_a_success_unchanged(ingestion):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    await run()
    before = await state(factory, season, "history/player/1")
    clock.advance(86400)
    calls.clear()
    overrides["/api/element-summary/1/"] = lambda: httpx.Response(503, text="SECRET")
    healthy = history(2)
    healthy["history"][0]["value"] = 55
    overrides["/api/element-summary/2/"] = healthy
    result = await run()
    assert result.history.failed == 1 and result.history.succeeded == 1
    assert calls.count("/api/element-summary/1/") == 3
    assert calls.count("/api/element-summary/2/") == 1
    after = await state(factory, season, "history/player/1")
    assert after.last_success_at == before.last_success_at
    assert after.source_observed_at == before.source_observed_at
    assert after.last_error_category == "transient_failure"
    async with factory() as session:
        assert (await read_player_history(session, season, 1)).rows[0].source_value == 50
        assert (await read_player_history(session, season, 2)).rows[0].source_value == 55


@pytest.mark.parametrize("endpoint", ["bootstrap-static", "fixtures", "element-summary/1"])
async def test_truncated_json_preserves_last_good_scope(ingestion, endpoint):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    await run()
    key = "history/player/1" if "element" in endpoint else "shared"
    before, rows = await state(factory, season, key), await stored(factory, season)
    clock.advance(86400)
    calls.clear()
    path = f"/api/{endpoint}/"
    overrides[path] = lambda: httpx.Response(200, content=b'{"SECRET":')
    await run()
    after = await state(factory, season, key)
    assert after.last_success_at == before.last_success_at
    assert after.source_observed_at == before.source_observed_at
    assert after.publication_version == before.publication_version
    assert after.last_error_category == "truncated_or_invalid"
    assert calls.count(path) == 1
    assert "SECRET" not in after.last_error
    assert await stored(factory, season) == rows


async def test_history_retry_after_over_budget_stops_run_cleanly(ingestion):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    overrides["/api/element-summary/1/"] = lambda: httpx.Response(429, headers={"Retry-After": "9999"})
    result = await run()
    assert result.status == "budget_exhausted" and result.shared_published
    assert result.history.budget_exhausted and result.history.pending == 2
    assert calls.count("/api/element-summary/1/") == 1
    assert "/api/element-summary/2/" not in calls and clock.sleeps == []
    assert (await state(factory, season, "history/player/1")).last_success_at is None


async def test_resume_history_when_shared_not_due(ingestion):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    overrides["/api/element-summary/1/"] = lambda: httpx.Response(503)
    await run()
    clock.advance(1801)
    calls.clear()
    overrides.clear()
    result = await run()
    assert not result.shared_published and result.history.succeeded == 1
    assert calls == ["/api/element-summary/1/"]


async def test_shared_write_failure_rolls_back_pair_and_records_separate_attempt(ingestion, monkeypatch, caplog):
    from fpl_optimizer.services import shared_catalog
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    await run()
    before, rows = await state(factory, season), await stored(factory, season)
    clock.advance(4*3600)
    bootstrap["elements"][0]["now_cost"] = 99
    async def fail(*args):
        raise RuntimeError("SECRET upstream payload or database credential")
    monkeypatch.setattr(shared_catalog, "_write_fixture_rows", fail)
    with caplog.at_level(logging.INFO, logger="fpl_optimizer"):
        result = await run()
    assert result.category == "publication"
    assert await stored(factory, season) == rows
    after = await state(factory, season)
    assert after.last_success_at == before.last_success_at
    assert after.last_error_category == "publication"
    assert "SECRET" not in caplog.text and "Traceback" not in caplog.text


async def test_valid_json_missing_known_history_row_keeps_player_data(ingestion):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    await run()
    before = await state(factory, season, "history/player/1")
    clock.advance(86400)
    overrides["/api/element-summary/1/"] = dict(fixtures=[], history=[], history_past=[])
    result = await run()
    assert result.history.failed == 1 and result.history.succeeded == 1
    after = await state(factory, season, "history/player/1")
    assert after.last_success_at == before.last_success_at
    assert after.last_error_category == "validation"
    async with factory() as session:
        assert len((await read_player_history(session, season, 1)).rows) == 1


async def test_null_schedules_count_in_published_full_season(ingestion):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    fixtures[-1].update(event=None, kickoff_time=None)
    assert (await run()).shared_published
    assert len((await stored(factory, season))[1]) == len(fixtures)


async def test_final_window_refreshes_after_thirty_minutes(ingestion):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    bootstrap["events"][1]["deadline_time"] = (clock.now()+timedelta(hours=12)).isoformat()
    await run()
    clock.advance(1799)
    calls.clear()
    assert not (await run()).shared_published
    assert calls == []
    clock.advance(1)
    assert (await run()).shared_published
    assert calls == ["/api/bootstrap-static/", "/api/fixtures/"]


async def test_shared_retry_after_budget_keeps_previous_success(ingestion):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    await run()
    before = await state(factory, season)
    clock.advance(4*3600)
    calls.clear()
    overrides["/api/bootstrap-static/"] = lambda: httpx.Response(503, headers={"Retry-After": "9999"})
    result = await run(budget=10)
    assert result.status == "budget_exhausted"
    assert calls == ["/api/bootstrap-static/"]
    assert (await state(factory, season)).last_success_at == before.last_success_at


@pytest.mark.parametrize("headers,attempts,status,category,expected_sleeps", [
    pytest.param({}, 3, "failed", "transient_failure", [1.125, 2.125],
                 id="three_5xx_attempts"),
    pytest.param({"Retry-After": "9999"}, 1, "budget_exhausted", "budget_exhausted", [],
                 id="retry_after_over_budget"),
])
async def test_fixtures_http_failure_preserves_shared_catalog(
    ingestion, monkeypatch, caplog, headers, attempts, status, category, expected_sleeps,
):
    factory, season, clock, bootstrap, fixtures, calls, overrides, run = ingestion
    assert (await run()).status == "success"

    async def published_rows():
        # Compare every stored column, including teams/events and history, not
        # only player prices. Shared failure bookkeeping is checked separately.
        async with factory() as session:
            rows = {}
            for model in (Season, Team, Gameweek, Player, Fixture, PlayerFixtureHistory):
                table = model.__table__
                scope = model.id if model is Season else model.season_id
                rows[table.name] = (await session.execute(
                    select(table).where(scope == season).order_by(*table.primary_key.columns)
                )).all()
            rows["history_checkpoints"] = (await session.execute(
                select(CacheMetadata.__table__).where(
                    CacheMetadata.season_id == season, CacheMetadata.key != "shared",
                ).order_by(CacheMetadata.key)
            )).all()
            return rows

    before, rows = await state(factory, season), await published_rows()
    # Both shared refresh and existing player histories are due, so stopping
    # history cannot pass merely because there was no eligible history work.
    clock.advance(86400)
    attempted_at = clock.now()
    calls.clear()
    clock.sleeps.clear()
    caplog.clear()
    bootstrap["elements"][0]["now_cost"] = 99
    bootstrap["elements"].append(bootstrap["elements"][0] | {"id": 3, "code": 2003})
    bootstrap["teams"][0]["name"] = "Updated synthetic club"
    bootstrap["events"][1]["deadline_time"] = "2030-11-02T12:00:00Z"
    overrides["/api/fixtures/"] = lambda: httpx.Response(
        503, headers=headers, text="SENSITIVE synthetic upstream body",
    )
    history_runner = AsyncMock(wraps=ingest._populate_history_locked)
    monkeypatch.setattr(ingest, "_populate_history_locked", history_runner)

    with caplog.at_level(logging.INFO, logger="fpl_optimizer"):
        result = await run(budget=10)

    assert result.status == status and result.category == category
    assert not result.shared_published and result.history is None
    assert calls == ["/api/bootstrap-static/"] + ["/api/fixtures/"] * attempts
    assert clock.sleeps == expected_sleeps
    history_runner.assert_not_awaited()
    assert await published_rows() == rows
    after = await state(factory, season)
    failure_fields = {"last_attempt_at", "last_error", "last_error_category", "retry_count"}
    for column in CacheMetadata.__table__.columns:
        if column.name not in failure_fields:
            assert getattr(after, column.name) == getattr(before, column.name)
    assert after.last_success_at == before.last_success_at
    assert after.last_attempt_at == attempted_at
    assert after.last_error_category == category
    assert after.retry_count == (before.retry_count or 0) + 1
    assert f"category={category} success=0 failed=1" in caplog.text
    assert "Traceback" not in caplog.text and "SENSITIVE" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
