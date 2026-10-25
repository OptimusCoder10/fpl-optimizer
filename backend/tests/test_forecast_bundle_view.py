"""Synthetic one-snapshot research view proofs against disposable PostgreSQL."""

from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fpl_optimizer.db.models import (
    CacheMetadata, Fixture, Gameweek, Player, PlayerFixtureHistory, Season, Team,
)
from fpl_optimizer.db.session import create_session_factory
from fpl_optimizer.services import forecast_bundle_view as reader
from fpl_optimizer.services.forecast_bundle_policy import (
    eligible_current_rows, label_player_history, select_target_event,
)
from fpl_optimizer.services.history_eligibility import HistoryEligibilityView, PlayerHistoryEvidence


pytestmark = pytest.mark.database
AS_OF = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
TABLES = (Season, Team, Gameweek, Fixture, Player, PlayerFixtureHistory, CacheMetadata)
COUNTS = {
    "minutes": 0, "goals_scored": 1, "assists": 2, "clean_sheets": 3,
    "saves": 4, "goals_conceded": 5, "clearances_blocks_interceptions": 6,
    "tackles": 7, "recoveries": 8, "bonus": 2, "penalties_saved": 9,
    "penalties_missed": 10, "yellow_cards": 11, "red_cards": 12, "own_goals": 13,
}


async def seed(factory, season, *, player_count=5):
    """Intentionally distinct counts verify projection order; these are not real matches."""
    async with factory.begin() as session:
        await session.execute(insert(Season).values(id=season, rules_version="synthetic-rules"))
        await session.execute(insert(Team), [dict(
            season_id=season, id=i, external_code=100 + i, name=f"Club {i}", short_name=f"C{i}",
        ) for i in range(1, 5)])
        await session.execute(insert(Player), [dict(
            season_id=season, id=i, first_name="Synthetic", second_name=str(i), web_name=f"P{i}",
            position=1 + i % 4, team_id=1 + i % 4, external_code=1000 + i,
            has_temporary_code=i == 2, opta_code=f"p{1000 + i}", now_cost=50,
            can_select=i != 2, can_transact=i != 3, removed=i == 4,
            status="a" if i != 2 else "u", chance_of_playing_next_round=75 if i == 1 else None,
            source_observed_at=AS_OF - timedelta(minutes=10), publication_version=7,
        ) for i in range(1, player_count + 1)])
        await session.execute(insert(Gameweek), [dict(
            season_id=season, id=i, name=f"Event {i}", deadline_time=deadline,
            is_current=False, is_next=i == 3, finished=False, data_checked=i != 2,
        ) for i, deadline in (
            (1, AS_OF - timedelta(days=7)), (2, AS_OF),
            (3, AS_OF + timedelta(hours=5)), (4, AS_OF + timedelta(days=7)), (5, None),
        )])
        await session.execute(insert(Fixture), [dict(
            season_id=season, id=i, external_code=2000 + i, gameweek_id=gw,
            kickoff_time=kickoff, started=True, finished=finished,
            finished_provisional=provisional, home_team_id=home, away_team_id=away,
            home_team_score=0, away_team_score=0, home_team_difficulty=2, away_team_difficulty=3,
        ) for i, gw, kickoff, finished, provisional, home, away in (
            (11, 1, AS_OF - timedelta(days=2), True, False, 1, 2),
            (12, 1, AS_OF + timedelta(hours=1), True, False, 2, 3),
            (13, 3, AS_OF - timedelta(hours=1), True, False, 1, 2),
            (14, 3, AS_OF + timedelta(days=1), False, False, 3, 1),
            (15, 1, AS_OF - timedelta(days=1), True, True, 1, 2),
            (16, 1, AS_OF - timedelta(days=1), False, False, 1, 2),
            (17, 2, AS_OF - timedelta(hours=2), True, False, 1, 2),
            (18, None, None, True, False, 1, 2),
            (19, 4, AS_OF - timedelta(hours=1), True, False, 1, 2),
        )])
        await session.execute(insert(PlayerFixtureHistory), [dict(
            season_id=season, player_id=1, fixture_id=i, source_round=1, position=2,
            team_id=1, opponent_team_id=2, was_home=True, source_value=50,
            total_points=0, defensive_contribution=0, bps=0, publication_version=8,
            **COUNTS,
        ) for i in range(11, 20)])
        await session.execute(insert(CacheMetadata).values(
            season_id=season, key="shared", publication_version=7,
            source_observed_at=AS_OF - timedelta(minutes=10),
            last_success_at=AS_OF - timedelta(minutes=9),
        ))
        await session.execute(insert(CacheMetadata).values(
            season_id=season, key="history", publication_version=8,
        ))
        for i in range(1, 5):
            await session.execute(insert(CacheMetadata).values(
                season_id=season, key=f"history/player/{i}", publication_version=8 if i != 3 else 1,
                source_observed_at=AS_OF - timedelta(hours=2) if i != 3 else None,
                last_success_at=AS_OF - timedelta(hours=1) if i != 3 else None,
                last_attempt_at=AS_OF - timedelta(minutes=1),
                covered_fixture_ids=list(range(11, 20)) if i == 1 else [],
                covered_finalized_fixture_ids=[11, 12, 13, 17, 19] if i == 1 else [],
                missing_fixture_ids=[91] if i == 2 else [],
                invalid_fixture_ids=[92] if i == 2 else [],
            ))


@pytest.fixture
async def bundle_db(migrated_test_engine):
    factory = create_session_factory(migrated_test_engine)
    season = "5100-01"
    await seed(factory, season)
    try:
        yield factory, season, migrated_test_engine
    finally:
        # All synthetic namespaces used here; remove child dependencies first.
        async with factory.begin() as session:
            for model in reversed(TABLES):
                column = model.id if model is Season else model.season_id
                await session.execute(delete(model).where(column.in_((season, "5101-02"))))


def assert_plain_frozen(value):
    if is_dataclass(value):
        assert value.__dataclass_params__.frozen
        for field in fields(value):
            assert_plain_frozen(getattr(value, field.name))
    elif isinstance(value, (tuple, frozenset)):
        for item in value:
            assert_plain_frozen(item)
    else:
        assert value is None or isinstance(value, (str, int, bool, datetime, timedelta))


async def test_view_reads_in_one_repeatable_read_only_transaction(bundle_db, monkeypatch):
    factory, season, engine = bundle_db
    transactions = []
    original = AsyncSession.execute

    async def inspect_transaction(session, statement, *args, **kwargs):
        result = await original(session, statement, *args, **kwargs)
        # Inspect using the original method to avoid recursive instrumentation.
        if getattr(statement, "is_select", False):
            settings = (await original(session, text(
                "SELECT current_setting('transaction_isolation'), "
                "current_setting('transaction_read_only'), pg_current_snapshot()::text, pg_backend_pid()"
            ))).one()
            assert settings[0] == "repeatable read"
            assert settings[1] == "on"
            transactions.append((session.sync_session.get_transaction(), *settings[2:]))
        return result

    monkeypatch.setattr(AsyncSession, "execute", inspect_transaction)
    view = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert len(transactions) >= 4
    assert len(set(transactions)) == 1
    assert_plain_frozen(view)
    with pytest.raises(FrozenInstanceError):
        view.season_id = "changed"
    assert engine.pool.checkedout() == 0


async def test_view_ignores_commits_made_after_snapshot_start(bundle_db, monkeypatch):
    factory, season, _ = bundle_db
    before = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    original = reader._read_history_rows
    commits = 0

    async def commit_between_reads(session, season_id):
        nonlocal commits
        assert session.in_transaction()
        async with factory.begin() as writer:
            assert await writer.scalar(text("SELECT pg_backend_pid()")) != await session.scalar(
                text("SELECT pg_backend_pid()")
            )
            await writer.execute(update(Fixture).where(
                Fixture.season_id == season, Fixture.id == 11,
            ).values(finished=False))
            await writer.execute(update(PlayerFixtureHistory).where(
                PlayerFixtureHistory.season_id == season, PlayerFixtureHistory.fixture_id == 11,
            ).values(minutes=90))
        commits += 1
        return await original(session, season_id)

    monkeypatch.setattr(reader, "_read_history_rows", commit_between_reads)
    during = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert commits == 1
    assert during.history_rows[0].minutes == 0, "snapshot must hide a later committed history row"
    assert during == before
    monkeypatch.setattr(reader, "_read_history_rows", original)
    after = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert after.history_rows[0].minutes == 90
    assert not after.fixtures[0].finished


async def test_view_is_season_isolated(bundle_db):
    factory, season, _ = bundle_db
    before = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    other = "5101-02"
    await seed(factory, other, player_count=50)
    async with factory.begin() as session:
        await session.execute(update(Season).where(Season.id == other).values(rules_version="other"))
        await session.execute(update(CacheMetadata).where(
            CacheMetadata.season_id == other,
        ).values(publication_version=99))
        await session.execute(update(PlayerFixtureHistory).where(
            PlayerFixtureHistory.season_id == other,
        ).values(minutes=99))
    assert await reader.read_bundle_view(factory, season, as_of=AS_OF) == before
    other_view = await reader.read_bundle_view(factory, other, as_of=AS_OF)
    assert other_view.rules_version == "other" and len(other_view.players) == 50
    assert other_view.shared_publication.publication_version == 99
    assert other_view.history_publication_version == 99
    assert all(row.minutes == 99 for row in other_view.history_rows)
    assert all(item.publication_version == 99 for item in other_view.player_history[:4])


async def test_view_target_is_none_when_no_future_deadline_and_equal_deadline_is_not_future(bundle_db):
    factory, season, engine = bundle_db
    with pytest.raises(ValueError, match="as_of must include a UTC offset"):
        await reader.read_bundle_view(factory, season, as_of=AS_OF.replace(tzinfo=None))
    assert engine.pool.checkedout() == 0
    view = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert view.target.id == 3  # Event 2 is exactly at the cutoff.
    async with factory.begin() as session:
        await session.execute(update(Gameweek).where(
            Gameweek.season_id == season, Gameweek.id.in_((3, 4)),
        ).values(deadline_time=AS_OF))
    view = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert view.target is None and view.shared_freshness is None
    assert len(view.history_rows) == 9 and len(view.players) == 5
    assert all(item.fixture_ids == () for item in view.target_fixtures_by_club)


async def test_target_fixture_ids_include_doubles_and_blanks_by_club(bundle_db):
    factory, season, _ = bundle_db
    view = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert tuple((item.team_id, item.fixture_ids) for item in view.target_fixtures_by_club) == (
        (1, (13, 14)), (2, (13,)), (3, (14,)), (4, ()),
    )
    assert [(f.id, f.home_team_id, f.away_team_id) for f in view.fixtures if f.gameweek_id == 3] == [
        (13, 1, 2), (14, 3, 1),
    ]


async def test_view_statement_count_is_constant_in_player_count(bundle_db):
    factory, season, engine = bundle_db
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    async def counted(season_id):
        statements.clear()
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            view = await reader.read_bundle_view(factory, season_id, as_of=AS_OF)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        assert statements[0] == "SET TRANSACTION READ ONLY"
        assert all(s.startswith(("SELECT", "SET TRANSACTION")) for s in statements)
        return len(view.players), len(statements)

    five = await counted(season)
    await seed(factory, "5101-02", player_count=50)
    fifty = await counted("5101-02")
    assert (five[0], fifty[0]) == (5, 50)
    assert five[1] == fifty[1], "SQL statement count must not grow with player count"
    assert five[1] == 9
    assert fifty[1] <= 10
    # A missing shared identity is not cached by SQLAlchemy; freshness retries it.
    async with factory.begin() as session:
        await session.execute(delete(CacheMetadata).where(
            CacheMetadata.season_id == season, CacheMetadata.key == "shared",
        ))
    assert await counted(season) == (5, 10)
    view = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert view.shared_freshness.status == "unknown"
    assert view.shared_publication.publication_version is None


async def test_players_without_checkpoint_appear_as_unknown_evidence(bundle_db):
    factory, season, _ = bundle_db
    view = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert tuple(p.player_id for p in view.player_history) == tuple(p.id for p in view.players)
    assert view.player_history[4] == PlayerHistoryEvidence(5, None, None, None, frozenset(), frozenset())
    assert view.player_history[0] == PlayerHistoryEvidence(
        1, AS_OF - timedelta(hours=2), AS_OF - timedelta(hours=1), 8,
        frozenset(range(11, 20)), frozenset((11, 12, 13, 17, 19)),
        last_attempt_at=AS_OF - timedelta(minutes=1),
    )
    assert view.player_history[1].missing_fixture_ids == (91,)
    assert view.player_history[1].invalid_fixture_ids == (92,)
    assert all(p.required_fixtures == () and not p.validated_empty_new_player for p in view.player_history)
    eligibility = HistoryEligibilityView(
        view.shared_freshness, view.player_history, view.history_publication_version,
        view.shared_publication.publication_version,
    )
    for index in (2, 4):  # Pending checkpoint and no checkpoint are both unknown.
        label = label_player_history(eligibility, view.player_history[index], target_event_id=3, as_of=AS_OF)
        assert (label.history_age_status, label.action) == ("unknown", "exclude")
    assert view.players[0] == reader.CatalogPlayer(1, 2, 2, "a", 75, True, True, False, 1001, False, "p1001")
    assert not view.players[1].can_select and view.players[1].has_temporary_code
    assert not view.players[2].can_transact and view.players[3].removed


async def test_view_returns_zero_minute_and_unfiltered_rows(bundle_db):
    factory, season, _ = bundle_db
    view = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert tuple(row.fixture_id for row in view.history_rows) == tuple(range(11, 20))
    for row in view.history_rows:
        assert row.player_id == 1
        assert {key: getattr(row, key) for key in COUNTS} == COUNTS
        assert {field.name for field in fields(row)} == {"player_id", "fixture_id", *COUNTS}


async def test_view_writes_nothing(bundle_db):
    factory, season, _ = bundle_db

    async def snapshot():
        async with factory() as session:
            snapshots = []
            for model in TABLES:
                rows = (await session.execute(
                    select(model.__table__).order_by(*model.__table__.primary_key.columns)
                )).all()
                snapshots.append(tuple(tuple(row) for row in rows))
            return tuple(snapshots)

    before = await snapshot()
    await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert await snapshot() == before


async def test_no_open_transaction_or_checked_out_connection_after_return(bundle_db, monkeypatch):
    _, season, engine = bundle_db
    sessions = []

    class TrackedSession(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

    factory = async_sessionmaker(engine, class_=TrackedSession, expire_on_commit=False)
    await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert len(sessions) == 1 and not sessions[0].in_transaction()
    assert engine.pool.checkedout() == 0

    async def fail(*_args):
        raise RuntimeError("synthetic read failure")

    monkeypatch.setattr(reader, "_read_history_rows", fail)
    with pytest.raises(RuntimeError, match="synthetic read failure"):
        await reader.read_bundle_view(factory, season, as_of=AS_OF)
    assert all(not session.in_transaction() for session in sessions)
    assert engine.pool.checkedout() == 0


async def test_view_output_feeds_policy_functions(bundle_db):
    factory, season, _ = bundle_db
    view = await reader.read_bundle_view(factory, season, as_of=AS_OF)
    target = select_target_event(view.gameweeks, as_of=view.as_of)
    assert target == view.target and target.id == 3
    filtered = eligible_current_rows(
        view.history_rows, view.fixtures, view.gameweeks, target_event_id=target.id, as_of=view.as_of,
    )
    assert tuple(row.fixture_id for row in filtered.rows) == (11,)
    assert filtered.rows[0].minutes == 0
    for row in filtered.rows:
        fixture = next(f for f in view.fixtures if f.id == row.fixture_id)
        assert fixture.kickoff_time < AS_OF and fixture.gameweek_id < target.id
    eligibility = HistoryEligibilityView(
        view.shared_freshness, view.player_history, view.history_publication_version,
        view.shared_publication.publication_version,
    )
    labels = tuple(label_player_history(
        eligibility, player, target_event_id=target.id, as_of=view.as_of,
    ) for player in view.player_history)
    assert labels[0].action == "use"
    assert labels[1].action == "exclude" and "91, 92" in labels[1].reason
    assert all(label.coverage_status == "unverified_shadow_only" for label in labels)
    assert all(labels[i].action == "exclude" for i in (2, 4))
