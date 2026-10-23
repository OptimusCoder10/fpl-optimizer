"""Count-only shadow checks against disposable PostgreSQL and synthetic FPL data."""

from datetime import timedelta
import logging
import re

import pytest
from sqlalchemy import delete, insert, select, text, update

from fpl_optimizer import ingest
from fpl_optimizer.db.models import (
    CacheMetadata, Fixture, Gameweek, Player, PlayerFixtureHistory, Season, Team,
)
from fpl_optimizer.services import history_coverage_shadow as shadow
from test_ingestion import ingestion
from test_player_history import publish_catalog


pytestmark = pytest.mark.database


async def prepared(source):
    factory, season, clock, _, _, _, _, run = source
    result = await run()
    assert result.status == "success"
    # Fixture 1 is each zero-minute anchor. Only fixture 2 can become required.
    async with factory.begin() as session:
        await session.execute(update(Fixture).where(
            Fixture.season_id == season, Fixture.id != 1,
        ).values(finished=False))
        await session.execute(update(Fixture).where(
            Fixture.season_id == season, Fixture.id == 2,
        ).values(finished=True, kickoff_time=clock.now() - timedelta(days=1),
                 gameweek_data_checked_at=clock.now() - timedelta(hours=3)))
    return factory, season, clock


async def test_shadow_reports_gap_when_finalized_club_fixture_has_no_row(ingestion):
    factory, season, clock = await prepared(ingestion)
    async with factory.begin() as session:
        await session.execute(update(Player).where(
            Player.season_id == season, Player.id == 2,
        ).values(status="u"))
    counts = await shadow.collect_history_coverage_shadow(factory, season, now=clock.now())
    assert (counts.players, counts.provisional, counts.players_with_gaps,
            counts.missing_total, counts.gaps_status_u) == (2, 2, 2, 2, 1)


async def test_shadow_reports_no_gap_when_all_required_rows_present(ingestion):
    factory, season, clock = await prepared(ingestion)
    async with factory.begin() as session:
        for player_id in (1, 2):
            original = await session.get(PlayerFixtureHistory, (season, player_id, 1))
            values = {column.name: getattr(original, column.name)
                      for column in PlayerFixtureHistory.__table__.columns}
            values.update(fixture_id=2, opponent_team_id=3)
            await session.execute(insert(PlayerFixtureHistory).values(**values))
    counts = await shadow.collect_history_coverage_shadow(factory, season, now=clock.now())
    assert (counts.players_with_gaps, counts.missing_total, counts.provisional) == (0, 0, 2)


async def test_shadow_counts_unresolved_reasons(ingestion):
    factory, season, clock = await prepared(ingestion)
    async with factory.begin() as session:
        await session.execute(delete(PlayerFixtureHistory).where(
            PlayerFixtureHistory.season_id == season, PlayerFixtureHistory.player_id == 2,
        ))
        await session.execute(update(Player).where(
            Player.season_id == season, Player.id == 1,
        ).values(team_id=2))
    counts = await shadow.collect_history_coverage_shadow(factory, season, now=clock.now())
    assert (counts.players, counts.provisional, counts.unresolved_no_rows,
            counts.unresolved_club_change, counts.players_with_gaps) == (2, 0, 1, 1, 0)


async def test_shadow_excludes_within_grace_from_gaps(ingestion):
    factory, season, clock = await prepared(ingestion)
    async with factory.begin() as session:
        await session.execute(update(Fixture).where(
            Fixture.season_id == season, Fixture.id == 2,
        ).values(gameweek_data_checked_at=clock.now() - timedelta(minutes=30)))
    counts = await shadow.collect_history_coverage_shadow(factory, season, now=clock.now())
    assert (counts.within_grace_total, counts.players_with_gaps,
            counts.missing_total) == (2, 0, 0)


async def test_shadow_never_writes_database(ingestion, monkeypatch):
    factory, season, clock = await prepared(ingestion)
    tables = (Season, Team, Gameweek, Fixture, Player, PlayerFixtureHistory, CacheMetadata)

    async def snapshot():
        async with factory() as session:
            snapshots = []
            for model in tables:
                season_column = model.id if model is Season else model.season_id
                result = await session.execute(select(model.__table__).where(
                    season_column == season,
                ))
                snapshots.append(tuple(tuple(row) for row in result.all()))
            return tuple(snapshots)

    original = shadow._collect_in_transaction

    async def inspect_transaction(session, season_id, now):
        assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        return await original(session, season_id, now)

    monkeypatch.setattr(shadow, "_collect_in_transaction", inspect_transaction)
    before = await snapshot()
    await shadow.collect_history_coverage_shadow(factory, season, now=clock.now())
    assert await snapshot() == before


async def test_shadow_failure_does_not_change_exit_code_or_publication(
    ingestion, monkeypatch, caplog,
):
    factory, season, _, _, _, _, _, run = ingestion

    async def fail(*_args, **_kwargs):
        raise RuntimeError("private player 123 and database detail")

    monkeypatch.setattr(ingest, "collect_history_coverage_shadow", fail)
    with caplog.at_level(logging.INFO, logger="fpl_optimizer.ingest"):
        result = await run()
    assert result.status == "success" and result.history.succeeded == 2
    assert result.shared_published
    assert "Coverage shadow: category=read_failure" in caplog.text
    assert "private player 123" not in caplog.text
    async with factory() as session:
        states = (await session.scalars(select(CacheMetadata).where(
            CacheMetadata.season_id == season,
            CacheMetadata.key.startswith("history/player/"),
        ))).all()
    assert len(states) == 2 and all(state.last_success_at is not None for state in states)
    exit_code = 1 if result.status in {"failed", "partial"} else 0
    assert exit_code == 0


async def test_shadow_log_line_has_counts_only_no_ids(ingestion, caplog):
    _, _, _, _, _, _, _, run = ingestion
    with caplog.at_level(logging.INFO, logger="fpl_optimizer"):
        result = await run()
    assert result.status == "success"
    lines = [record.message for record in caplog.records
             if record.message.startswith("Coverage shadow:")]
    assert len(lines) == 1
    messages = [record.message for record in caplog.records]
    assert next(i for i, message in enumerate(messages) if message.startswith("History population:")) < (
        next(i for i, message in enumerate(messages) if message.startswith("Coverage shadow:"))
    )
    assert re.fullmatch(
        r"Coverage shadow: players=2 provisional=2 unresolved_no_rows=0 "
        r"unresolved_club_change=0 players_with_gaps=0 missing_total=0 "
        r"within_grace_total=74 gaps_status_u=0", lines[0],
    )
    assert not any(word in lines[0] for word in ("player_id", "fixture_id", "name", "http", "SELECT"))


async def test_shadow_isolated_per_season(ingestion, migrated_test_engine, publication_settings):
    factory, season, clock = await prepared(ingestion)
    before = await shadow.collect_history_coverage_shadow(factory, season, now=clock.now())
    other = "4550-51"
    await publish_catalog(migrated_test_engine, publication_settings, other)
    async with factory.begin() as session:
        await session.execute(insert(CacheMetadata).values(
            season_id=other, key="history/player/5", publication_version=1,
            last_success_at=clock.now(),
        ))
    after = await shadow.collect_history_coverage_shadow(factory, season, now=clock.now())
    other_counts = await shadow.collect_history_coverage_shadow(factory, other, now=clock.now())
    assert after == before
    assert (other_counts.players, other_counts.unresolved_no_rows,
            other_counts.players_with_gaps) == (1, 1, 0)
