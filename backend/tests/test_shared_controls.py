"""Shared lock ownership, failure cleanup and deterministic freshness gates."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from conftest import DatabaseTestSettings, create_guarded_test_engine
from fpl_optimizer.config import Settings

from fpl_optimizer.db.advisory_lock import INGESTION_LOCK_KEY, ingestion_lock
from fpl_optimizer.db.models import CacheMetadata, Fixture, Gameweek, Player, Season, Team
from fpl_optimizer.db.session import create_session_factory
from fpl_optimizer.services import shared_catalog
from fpl_optimizer.services.shared_freshness import check_shared_freshness
from test_shared_catalog import load_shared_envelope, publication


NOW = datetime(2026, 10, 6, 15, tzinfo=timezone.utc)
TICK = timedelta(microseconds=1)


@pytest_asyncio.fixture
async def other_test_engine(migrated_test_engine):
    """Create a separate disposable database on the already guarded server."""
    name = f"fpl_identity_{uuid4().hex}_test"
    url = migrated_test_engine.url.set(database=name)
    engine = create_guarded_test_engine(DatabaseTestSettings(
        test_database_url=url.render_as_string(hide_password=False),
    ))
    async with migrated_test_engine.connect() as admin:
        await admin.execution_options(isolation_level="AUTOCOMMIT")
        # The identifier is generated locally from a fixed prefix and UUID hex.
        await admin.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            yield engine
        finally:
            await engine.dispose()
            await admin.execute(text(f'DROP DATABASE "{name}"'))


@pytest.mark.database
async def test_matching_database_accepts_different_endpoint_names(
    migrated_test_engine, publication_settings,
):
    # Both hostnames resolve to the local disposable server; no URL equality.
    write_host = migrated_test_engine.url.host
    lock_host = "localhost" if write_host == "127.0.0.1" else "127.0.0.1"
    assert write_host in {"localhost", "127.0.0.1"}, "This test needs local PostgreSQL"
    lock_url = migrated_test_engine.url.set(host=lock_host)
    settings = Settings(
        database_url=publication_settings.database_url.get_secret_value(),
        session_database_url=lock_url.render_as_string(hide_password=False),
    )
    factory = create_session_factory(migrated_test_engine)
    context = publication("2036-37")
    assert await shared_catalog.publish_shared_catalog(
        factory, load_shared_envelope(), context, settings=settings,
    ) == 1
    async with factory() as session:
        metadata = await session.get(CacheMetadata, (context.season_id, "shared"))
        assert metadata.last_success_at == context.published_at
    async with ingestion_lock(settings) as acquired:
        assert acquired


@pytest.mark.database
async def test_database_mismatch_refuses_without_writes_and_releases_lock(
    migrated_test_engine, other_test_engine, monkeypatch,
):
    factory = create_session_factory(migrated_test_engine)
    other_url = other_test_engine.url.render_as_string(hide_password=False)
    # Even identical configured URLs must be checked against the ACTUAL writer.
    settings = Settings(database_url=other_url, session_database_url=other_url)
    writer = AsyncMock(wraps=shared_catalog._write_shared_catalog)
    monkeypatch.setattr(shared_catalog, "_write_shared_catalog", writer)

    async def row_counts():
        async with factory() as session:
            return [await session.scalar(select(func.count()).select_from(model))
                    for model in (Season, Team, Player, Gameweek, Fixture, CacheMetadata)]

    before = await row_counts()
    with pytest.raises(ValueError) as error:
        await shared_catalog.publish_shared_catalog(
            factory, load_shared_envelope(), publication("2037-38"), settings=settings,
        )
    assert str(error.value) == (
        "SESSION_DATABASE_URL and the write connection do not reach the "
        "same PostgreSQL database; publication refused"
    )
    writer.assert_not_awaited()
    assert await row_counts() == before
    async with other_test_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM pg_stat_user_tables")) == 0
        # No ingestion lock or temporary identity marker remains in this DB.
        assert await connection.scalar(text("""
            SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'
              AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
        """)) == 0
    async with ingestion_lock(settings) as acquired:
        assert acquired


@pytest.mark.database
async def test_second_run_exits_without_opening_write_session(
    migrated_test_engine, publication_settings, monkeypatch, caplog,
):
    factory = create_session_factory(migrated_test_engine)
    writer = Mock(wraps=factory)
    writer.begin = Mock(wraps=factory.begin)
    written = asyncio.Event()
    finish = asyncio.Event()
    original = shared_catalog._write_shared_catalog

    async def hold_before_commit(session, envelope, context):
        version = await original(session, envelope, context)
        write_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        # Session lock survives its own transaction, on a different backend.
        lock_pids = (await session.scalars(text(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid = :high AND objid = :low AND objsubid = 1 AND granted"
        ), {"high": INGESTION_LOCK_KEY >> 32, "low": INGESTION_LOCK_KEY & 0xFFFFFFFF})).all()
        assert len(lock_pids) == 1
        assert lock_pids[0] != write_pid
        written.set()
        await finish.wait()
        return version

    monkeypatch.setattr(shared_catalog, "_write_shared_catalog", hold_before_commit)
    first = asyncio.create_task(shared_catalog.publish_shared_catalog(
        writer, load_shared_envelope(), publication("2030-31"),
        settings=publication_settings,
    ))
    try:
        await asyncio.wait_for(written.wait(), timeout=5)
        with caplog.at_level("INFO"):
            second = await asyncio.wait_for(shared_catalog.publish_shared_catalog(
                writer, load_shared_envelope(), publication("2031-32"),
                settings=publication_settings,
            ), timeout=5)
        assert second is None
        assert "already running" in caplog.text
        assert writer.begin.call_count == 1
        async with factory() as session:
            assert await session.get(Season, "2030-31") is None  # Still uncommitted.
            assert await session.get(Season, "2031-32") is None
    finally:
        finish.set()
        await asyncio.wait_for(first, timeout=5)
    assert first.result() == 1
    async with factory() as session:
        assert await session.get(Season, "2030-31") is not None
        assert await session.get(Season, "2031-32") is None
    async with ingestion_lock(publication_settings) as acquired:
        assert acquired


@pytest.mark.database
@pytest.mark.parametrize("cancel", [False, True], ids=["failure", "cancellation"])
async def test_failed_publication_preserves_age_and_releases_lock(
    migrated_test_engine, publication_settings, monkeypatch, cancel,
):
    factory = create_session_factory(migrated_test_engine)
    season = "2032-33" if cancel else "2033-34"
    envelope = load_shared_envelope()
    baseline = publication(season)
    assert await shared_catalog.publish_shared_catalog(
        factory, envelope, baseline, settings=publication_settings,
    ) == 1
    written = asyncio.Event()
    original = shared_catalog._write_shared_catalog

    async def fail_after_metadata(session, envelope, context):
        await original(session, envelope, context)
        written.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("failure after metadata write")

    monkeypatch.setattr(shared_catalog, "_write_shared_catalog", fail_after_metadata)
    task = asyncio.create_task(shared_catalog.publish_shared_catalog(
        factory, envelope, publication(season, observed_hour=14),
        settings=publication_settings,
    ))
    await asyncio.wait_for(written.wait(), timeout=5)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await asyncio.wait_for(task, timeout=5)

    async with factory() as session:
        metadata = await session.get(CacheMetadata, (season, "shared"))
        assert metadata.last_success_at == baseline.published_at
        assert metadata.source_observed_at == baseline.source_observed_at
        assert metadata.publication_version == 1
        freshness = await shared_catalog.read_shared_freshness(
            session, season, now=NOW, next_deadline=NOW + timedelta(hours=25),
        )
        assert freshness.status == "stale"
        assert freshness.source_age == timedelta(hours=7)
    monkeypatch.setattr(shared_catalog, "_write_shared_catalog", original)
    assert await shared_catalog.publish_shared_catalog(
        factory, envelope, publication(season, observed_hour=14),
        settings=publication_settings,
    ) == 2


@pytest.mark.database
async def test_lock_helper_spans_transactions_and_releases_after_failure(
    migrated_test_engine, publication_settings,
):
    with pytest.raises(RuntimeError, match="migration failed"):
        async with ingestion_lock(publication_settings) as acquired:
            assert acquired
            async with migrated_test_engine.begin() as connection:
                await connection.execute(text("SELECT 1"))
            async with ingestion_lock(publication_settings) as contender:
                assert not contender
            raise RuntimeError("migration failed")
    async with ingestion_lock(publication_settings) as acquired:
        assert acquired
        async with migrated_test_engine.connect() as connection:
            lock_pid = await connection.scalar(text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = :high AND objid = :low AND objsubid = 1 AND granted"
            ), {"high": INGESTION_LOCK_KEY >> 32, "low": INGESTION_LOCK_KEY & 0xFFFFFFFF})
        assert lock_pid is not None
    async with migrated_test_engine.connect() as connection:
        assert not await connection.scalar(text(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = :pid)"
        ), {"pid": lock_pid})


@pytest.mark.database
@pytest.mark.parametrize("until_deadline", [
    timedelta(hours=1), timedelta(hours=24), timedelta(hours=24) + TICK,
    timedelta(hours=25),
])
@pytest.mark.parametrize("age", [
    timedelta(hours=1), timedelta(hours=1) + TICK,
    timedelta(hours=2), timedelta(hours=2) + TICK,
    timedelta(hours=6), timedelta(hours=6) + TICK,
])
async def test_freshness_boundaries_from_stored_publication(
    migrated_test_engine, publication_settings, until_deadline, age,
):
    factory = create_session_factory(migrated_test_engine)
    baseline = publication("2034-35")
    await shared_catalog.publish_shared_catalog(
        factory, load_shared_envelope(), baseline, settings=publication_settings,
    )
    now = baseline.source_observed_at + age
    async with factory() as session:
        result = await shared_catalog.read_shared_freshness(
            session, baseline.season_id, now=now, next_deadline=now + until_deadline,
        )
    near = until_deadline <= timedelta(hours=24)
    limit = timedelta(hours=2 if near else 6)
    assert result.status == ("current" if age <= limit else "stale")
    assert result.age_warning == (near and age > timedelta(hours=1))
    assert result.age_limit == limit
    assert result.final_window == near
    assert result.source_age == age
    assert result.success_age == age - timedelta(minutes=1)


@pytest.mark.database
async def test_absent_catalog_is_unknown(migrated_test_engine):
    factory = create_session_factory(migrated_test_engine)
    async with factory() as session:
        result = await shared_catalog.read_shared_freshness(
            session, "2099-00", now=NOW, next_deadline=NOW + timedelta(hours=1),
        )
    assert result.status == "unknown"
    assert result.source_age is result.success_age is None
    assert not result.age_warning


@pytest.mark.database
async def test_replay_cannot_rejuvenate_old_source(migrated_test_engine, publication_settings):
    factory = create_session_factory(migrated_test_engine)
    baseline = publication("2035-36")
    for published_at in (baseline.published_at, NOW):
        version = await shared_catalog.publish_shared_catalog(
            factory, load_shared_envelope(),
            baseline.model_copy(update={"published_at": published_at}),
            settings=publication_settings,
        )
        assert version == 1
    async with factory() as session:
        result = await shared_catalog.read_shared_freshness(
            session, baseline.season_id, now=NOW, next_deadline=NOW + timedelta(hours=1),
        )
    assert result.status == "stale"
    assert result.source_age == timedelta(hours=7)
    assert result.success_age == timedelta(0)


@pytest.mark.parametrize("field,value", [
    ("now", NOW.replace(tzinfo=None)),
    ("next_deadline", NOW.replace(tzinfo=None)),
    ("source_observed_at", NOW.replace(tzinfo=None)),
    ("last_success_at", NOW.replace(tzinfo=None)),
    ("next_deadline", NOW),
    ("next_deadline", NOW - TICK),
    ("source_observed_at", NOW + TICK),
    ("last_success_at", NOW + TICK),
])
def test_freshness_rejects_ambiguous_or_impossible_times(field, value):
    inputs = dict(source_observed_at=NOW, last_success_at=NOW, now=NOW,
                  next_deadline=NOW + timedelta(hours=1))
    inputs[field] = value
    with pytest.raises(ValueError):
        check_shared_freshness(**inputs)


@pytest.mark.parametrize("missing", ["source_observed_at", "last_success_at"])
def test_incomplete_publication_evidence_is_unknown(missing):
    inputs = dict(source_observed_at=NOW, last_success_at=NOW, now=NOW,
                  next_deadline=NOW + timedelta(hours=1))
    inputs[missing] = None
    assert check_shared_freshness(**inputs).status == "unknown"


async def test_lock_requires_explicit_session_endpoint(publication_settings):
    settings = publication_settings.model_copy(update={"session_database_url": None})
    with pytest.raises(ValueError, match="SESSION_DATABASE_URL"):
        async with ingestion_lock(settings):
            pytest.fail("Missing session endpoint must not open a connection")


def test_freshness_uses_elapsed_hours_across_daylight_saving_change():
    london = ZoneInfo("Europe/London")
    # The clock moves back: these are two elapsed hours, not one.
    observed = datetime(2026, 10, 25, 0, 30, tzinfo=london)
    now = datetime(2026, 10, 25, 1, 30, tzinfo=london, fold=1)
    result = check_shared_freshness(
        source_observed_at=observed, last_success_at=observed,
        now=now, next_deadline=now + timedelta(hours=1),
    )
    assert result.source_age == timedelta(hours=2)
    assert result.status == "current"
    assert result.age_warning
