"""Resumption and failure isolation against disposable PostgreSQL, without HTTP."""

import asyncio
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fpl_optimizer.db.advisory_lock import ingestion_lock
from fpl_optimizer.db.models import CacheMetadata, Player
from fpl_optimizer.services import player_history
from fpl_optimizer.services.history_population import populate_history, read_population_status
from fpl_optimizer.services.shared_catalog import publish_shared_catalog
from test_player_history import load_json_fixture, publish_catalog, shared_envelope, shared_publication


pytestmark = pytest.mark.database
SEASONS = count(2060)


class FakeClock:
    def __init__(self):
        self.time = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def now(self):
        return self.time

    def monotonic(self):
        return self.elapsed

    def advance(self, delta):
        self.time += delta
        self.elapsed += delta.total_seconds()


class FakeFetcher:
    def __init__(self):
        self.calls = []
        self.fail_ids = set()
        self.duration = timedelta(0)
        self.clock = None

    async def __call__(self, player_id):
        self.calls.append(player_id)
        if self.clock:
            self.clock.advance(self.duration)
        if player_id in self.fail_ids:
            raise RuntimeError("secret upstream URL must not be stored")
        payload = load_json_fixture("element_summary_5.json")
        for row in payload["history"]:
            row["element"] = player_id
        return payload


@pytest_asyncio.fixture
async def population(migrated_test_engine, publication_settings):
    year = next(SEASONS)
    season = f"{year}-{str(year + 1)[-2:]}"
    factory = await publish_catalog(migrated_test_engine, publication_settings, season)
    clock = FakeClock()
    fetcher = FakeFetcher()

    async def run(*, budget=timedelta(minutes=10), fetch=fetcher):
        return await populate_history(
            factory, season, fetch, clock=clock.now, budget=budget,
            elapsed_clock=clock.monotonic, settings=publication_settings,
        )

    return factory, season, clock, fetcher, run


async def checkpoints(factory, season):
    async with factory() as session:
        return {int(row.key.rsplit("/", 1)[1]): row for row in (
            await session.scalars(select(CacheMetadata).where(
                CacheMetadata.season_id == season,
                CacheMetadata.key.startswith("history/player/"),
            ))
        ).all()}


async def test_a_fails_b_commits_and_restart_honours_backoff(population):
    factory, season, clock, fetcher, run = population
    fetcher.fail_ids = {1}
    result = await run()
    assert fetcher.calls == [1, 5]
    assert (result.succeeded, result.failed, result.pending, result.required) == (1, 1, 1, 2)
    states = await checkpoints(factory, season)
    assert states[1].last_success_at is None
    assert states[1].last_attempt_at == clock.now()
    assert states[1].retry_count == 1
    assert states[1].next_retry_at == clock.now() + timedelta(minutes=30)
    assert states[1].last_error_category == "fetch"
    assert "secret" not in states[1].last_error
    async with factory() as session:
        assert await player_history.read_player_history(session, season, 1) is None
        healthy = await player_history.read_player_history(session, season, 5)
    assert healthy.checkpoint.last_success_at == clock.now()
    assert len(healthy.rows) == 1
    fetcher.calls.clear()
    assert (await run()).pending == 1
    assert fetcher.calls == []
    clock.advance(timedelta(minutes=30))
    fetcher.fail_ids.clear()
    assert (await run()).succeeded == 1
    assert fetcher.calls == [1]
    states = await checkpoints(factory, season)
    assert states[1].retry_count == 0
    assert states[1].next_retry_at is None
    assert states[1].last_error_category is None


@pytest.mark.parametrize("where", ["before", "after"])
async def test_interrupt_at_commit_then_resume(population, monkeypatch, where):
    factory, season, clock, fetcher, run = population
    with monkeypatch.context() as patch:
        original = AsyncSession.commit

        async def interrupt(session):
            if where == "after":
                await original(session)
            raise asyncio.CancelledError()

        patch.setattr(AsyncSession, "commit", interrupt)
        with pytest.raises(asyncio.CancelledError):
            await run()

    states = await checkpoints(factory, season)
    assert set(states) == {1, 5}
    assert states[5].last_success_at is None
    async with factory() as session:
        stored = await player_history.read_player_history(session, season, 1)
    if where == "before":
        assert stored is None
        assert states[1].last_success_at is None
        assert states[1].last_attempt_at == clock.now()
    else:
        assert len(stored.rows) == 1
        assert states[1].last_success_at == clock.now()
    fetcher.calls.clear()
    result = await run()
    assert fetcher.calls == ([1, 5] if where == "before" else [5])
    assert result.pending == 0
    async with factory() as session:
        summary = await session.get(CacheMetadata, (season, "history"))
    assert summary.publication_version == 2


async def test_backoff_doubles_caps_and_never_refreshes_prior_success(population):
    factory, season, clock, fetcher, run = population
    await run()
    baseline = (await checkpoints(factory, season))[1]
    clock.advance(timedelta(days=1))
    fetcher.fail_ids = {1}
    for retry, minutes in enumerate([30, 60, 120, 240, 360, 360], start=1):
        fetcher.calls.clear()
        await run()
        state = (await checkpoints(factory, season))[1]
        assert state.retry_count == retry
        assert state.next_retry_at == clock.now() + timedelta(minutes=minutes)
        assert state.last_success_at == baseline.last_success_at
        assert state.source_observed_at == baseline.source_observed_at
        assert state.publication_version == baseline.publication_version
        assert state.covered_fixture_ids == baseline.covered_fixture_ids
        assert 1 in fetcher.calls
        if retry == 1:
            assert fetcher.calls == [1, 5]
        fetcher.calls.clear()
        clock.advance(timedelta(minutes=minutes) - timedelta(microseconds=1))
        await run()
        assert 1 not in fetcher.calls
        clock.advance(timedelta(microseconds=1))


async def test_budget_stops_between_players_and_restart_keeps_pending(population):
    factory, season, clock, fetcher, run = population
    fetcher.clock, fetcher.duration = clock, timedelta(minutes=11)
    result = await run(budget=timedelta(minutes=10))
    assert fetcher.calls == [1]
    assert result.budget_exhausted
    assert (result.succeeded, result.pending) == (1, 1)
    assert (await checkpoints(factory, season))[1].last_success_at == clock.now()
    fetcher.calls.clear()
    result = await run()
    assert fetcher.calls == [5]
    assert result.pending == 0


async def test_zero_budget_discovers_without_fetching(population):
    factory, season, clock, fetcher, run = population
    result = await run(budget=timedelta(0))
    assert (result.succeeded, result.pending, result.required) == (0, 2, 2)
    assert result.budget_exhausted
    assert fetcher.calls == []
    assert set(await checkpoints(factory, season)) == {1, 5}


async def add_player(factory, season, player_id):
    async with factory.begin() as session:
        source = await session.get(Player, (season, 5))
        values = {column.name: getattr(source, column.name) for column in Player.__table__.columns}
        values.update(id=player_id, external_code=900000 + player_id)
        session.add(Player(**values))


async def test_oldest_due_then_numeric_id_and_no_starvation(population):
    factory, season, clock, fetcher, run = population
    await run(budget=timedelta(0))
    await add_player(factory, season, 10)
    clock.advance(timedelta(minutes=1))
    await run(budget=timedelta(0))
    async with factory.begin() as session:
        await session.execute(update(CacheMetadata).where(
            CacheMetadata.season_id == season, CacheMetadata.key == "history/player/10",
        ).values(history_due_at=clock.now() - timedelta(hours=1)))
    fetcher.fail_ids = {10}
    fetcher.clock, fetcher.duration = clock, timedelta(minutes=1)
    await run(budget=timedelta(minutes=1))
    assert fetcher.calls == [10]
    clock.advance(timedelta(minutes=30))
    fetcher.calls.clear()
    await run(budget=timedelta(minutes=1))
    assert fetcher.calls == [1]
    fetcher.calls.clear()
    await run(budget=timedelta(minutes=1))
    assert fetcher.calls == [5]
    clock.advance(timedelta(days=1))
    async with factory.begin() as session:
        await session.execute(update(CacheMetadata).where(
            CacheMetadata.season_id == season,
            CacheMetadata.key.startswith("history/player/"),
        ).values(history_due_at=clock.now(), next_retry_at=None))
    fetcher.calls.clear()
    await run()
    assert fetcher.calls == [1, 5, 10]


async def test_new_ids_added_missing_failed_ids_never_forgotten(population):
    factory, season, clock, fetcher, run = population
    fetcher.fail_ids = {1}
    await run()
    failed = (await checkpoints(factory, season))[1]
    async with factory.begin() as session:
        await session.execute(delete(Player).where(Player.season_id == season, Player.id == 1))
    await add_player(factory, season, 10)
    fetcher.calls.clear()
    result = await run()
    assert fetcher.calls == [10]
    assert result.required == 3
    states = await checkpoints(factory, season)
    assert states[1].next_retry_at == failed.next_retry_at
    clock.advance(timedelta(minutes=30))
    fetcher.fail_ids.clear()
    fetcher.calls.clear()
    assert (await run()).failed == 1
    assert fetcher.calls == [1]
    assert (await checkpoints(factory, season))[1].last_error_category == "validation"


@pytest.mark.parametrize("failure", ["boundary", "publication"])
async def test_invalid_or_failed_write_only_affects_one_player(population, monkeypatch, failure):
    factory, season, clock, fetcher, run = population
    if failure == "publication":
        original = player_history._write_history_rows

        async def fail(session, rows):
            await original(session, rows)
            if rows[0]["player_id"] == 1:
                raise RuntimeError("write failed")

        monkeypatch.setattr(player_history, "_write_history_rows", fail)

    async def fetch(player_id):
        payload = await fetcher(player_id)
        if failure == "boundary" and player_id == 1:
            payload["history"][0].pop("minutes")
        return payload

    result = await run(fetch=fetch)
    assert (result.succeeded, result.failed) == (1, 1)
    async with factory() as session:
        assert await player_history.read_player_history(session, season, 1) is None
        healthy = await player_history.read_player_history(session, season, 5)
    assert len(healthy.rows) == 1
    assert healthy.history_publication_version == 1
    assert (await checkpoints(factory, season))[1].last_error_category == (
        "validation" if failure == "boundary" else "publication"
    )


async def test_per_player_current_stale_unknown_and_actual_source_age(population):
    factory, season, clock, fetcher, run = population
    fetcher.fail_ids = {1}
    await run()
    required = {1: {1}, 5: {1}, 999: set()}

    async def read():
        return await read_population_status(factory, season, now=clock.now(),
                                            required_finalized_fixture_ids=required)

    result = await read()
    assert [(p.player_id, p.status) for p in result.players] == [(1, "unknown"), (5, "current"), (999, "unknown")]
    assert result.publication_version == 1
    assert result.players[0].error_category == "fetch"
    assert result.players[1].source_age == timedelta(0)
    required[5].add(999)
    result = await read()
    assert result.players[1].status == "stale"
    assert result.players[1].missing_fixture_ids == (999,)
    required[5].remove(999)
    clock.advance(timedelta(hours=48))
    assert (await read()).players[1].status == "current"
    clock.advance(timedelta(microseconds=1))
    fetcher.fail_ids = {1, 5}
    await run()
    result = await read()
    stale = result.players[1]
    assert stale.status == "stale"
    assert stale.source_age == stale.success_age == timedelta(hours=48, microseconds=1)
    assert stale.last_attempt_at == clock.now()
    assert stale.last_success_at < stale.last_attempt_at


async def test_lock_covers_fetch_and_contender_never_discovers(population, publication_settings):
    factory, season, clock, fetcher, run = population
    async with ingestion_lock(publication_settings) as connection:
        assert connection is not None
        assert await run() is None
    assert await checkpoints(factory, season) == {}

    async def fetch(player_id):
        async with ingestion_lock(publication_settings) as connection:
            assert connection is None
        return await fetcher(player_id)

    assert (await run(fetch=fetch)).succeeded == 2


async def test_price_only_update_preserves_checkpoints(population, publication_settings):
    factory, season, clock, fetcher, run = population
    await run()
    before = await checkpoints(factory, season)
    catalog = shared_envelope()
    catalog.bootstrap.elements[0].now_cost += 1
    context = shared_publication(season).model_copy(update={
        "source_observed_at": clock.now(), "published_at": clock.now(),
    })
    await publish_shared_catalog(factory, catalog, context, settings=publication_settings)
    fetcher.calls.clear()
    await run()
    assert fetcher.calls == []
    after = await checkpoints(factory, season)
    assert {p: row.last_success_at for p, row in after.items()} == {
        p: row.last_success_at for p, row in before.items()
    }


async def test_adopts_existing_success_without_refetch(population, publication_settings):
    from test_player_history import history_envelope, history_publication

    factory, season, clock, fetcher, run = population
    publication = history_publication(season)
    await player_history.publish_player_history(
        factory, history_envelope(), publication, settings=publication_settings,
    )
    assert (await run()).succeeded == 1
    assert fetcher.calls == [1]
    states = await checkpoints(factory, season)
    assert states[5].last_success_at == publication.published_at
    assert states[5].history_due_at == publication.published_at + timedelta(days=1)


async def test_status_read_keeps_one_database_snapshot(population, monkeypatch):
    factory, season, clock, fetcher, run = population
    fetcher.fail_ids = {1}
    await run()
    clock.advance(timedelta(minutes=30))
    fetcher.fail_ids.clear()
    original_get = AsyncSession.get
    resumed = False

    async def get_then_resume(session, entity, ident, **kwargs):
        nonlocal resumed
        value = await original_get(session, entity, ident, **kwargs)
        if not resumed and entity is CacheMetadata and ident == (season, "history"):
            resumed = True
            assert (await run()).succeeded == 1
        return value

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "get", get_then_resume)
        before = await read_population_status(
            factory, season, now=clock.now(), required_finalized_fixture_ids={1: {1}, 5: {1}},
        )
    assert resumed
    assert before.publication_version == 1
    assert [p.status for p in before.players] == ["unknown", "current"]
    after = await read_population_status(
        factory, season, now=clock.now(), required_finalized_fixture_ids={1: {1}, 5: {1}},
    )
    assert after.publication_version == 2
    assert [p.status for p in after.players] == ["current", "current"]
