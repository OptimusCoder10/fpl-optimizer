"""Bounded, sequential history population using durable player checkpoints.

Checkpoint keys themselves are the required-ID set: discovery only adds keys.
Success is due again after 24 hours; explicit sweep generations and dependency
invalidation are deferred. Fetchers perform no database work and must return a
fresh complete envelope; the HTTP adapter supplies safe failure categories.
"""

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from statistics import median
from time import monotonic
from typing import Literal

from sqlalchemy import Integer, cast, event, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fpl_optimizer.config import Settings
from fpl_optimizer.db.advisory_lock import ingestion_lock, require_same_database
from fpl_optimizer.db.models import CacheMetadata, Player
from fpl_optimizer.schemas.bootstrap import require_aware_datetime
from fpl_optimizer.schemas.fetch import FetchFailure
from fpl_optimizer.schemas.element_summary import (
    ElementSummaryEnvelope,
    PlayerHistoryPublication,
)
from fpl_optimizer.services import player_history


logger = logging.getLogger(__name__)
REFRESH_INTERVAL = timedelta(days=1)
HISTORY_AGE_LIMIT = timedelta(hours=48)


class _StatementCounts:
    """Count executed statements only; never retain SQL or bound values."""

    def __init__(self, session_factory, lock_connection):
        self.engines = (
            session_factory.kw["bind"].sync_engine,
            lock_connection.sync_connection.engine,
        )
        self.phase = "other"
        self.other = 0
        self.commit = 0
        self.player_commits: list[int] = []

    def _count(self, _connection, _cursor, _statement, _parameters, _context,
               _executemany):
        if self.phase == "other":
            self.other += 1
        elif self.phase == "commit":
            self.commit += 1

    @contextmanager
    def attached(self):
        for engine in self.engines:
            event.listen(engine, "before_cursor_execute", self._count)
        try:
            yield
        finally:
            for engine in self.engines:
                event.remove(engine, "before_cursor_execute", self._count)

    async def ping_ms(self, session_factory, elapsed_clock):
        samples = []
        self.phase = "ping"
        try:
            async with session_factory() as session:
                for _ in range(5):
                    started = elapsed_clock()
                    await session.execute(text("SELECT 1"))
                    samples.append(max(0.0, elapsed_clock() - started) * 1000)
        finally:
            self.phase = "other"
        return median(samples)


def _utc(value: datetime) -> datetime:
    require_aware_datetime(value)
    return value.astimezone(timezone.utc)


def _player_checkpoints(season_id: str):
    return select(CacheMetadata).where(
        CacheMetadata.season_id == season_id,
        CacheMetadata.key.startswith(player_history.PLAYER_HISTORY_KEY_PREFIX),
    )


def _player_id(checkpoint: CacheMetadata) -> int:
    return int(checkpoint.key.removeprefix(player_history.PLAYER_HISTORY_KEY_PREFIX))


@dataclass(frozen=True)
class PopulationResult:
    succeeded: int
    failed: int
    pending: int
    required: int
    budget_exhausted: bool


async def _discover_required_players(
    session: AsyncSession, season_id: str, now: datetime,
) -> None:
    """Add the catalog's IDs, retaining old failures and their retry schedule."""
    shared = await session.get(CacheMetadata, (season_id, "shared"))
    if shared is None or shared.last_success_at is None:
        raise ValueError("history population requires a published shared catalog")
    ids = (await session.scalars(
        select(Player.id).where(Player.season_id == season_id).order_by(Player.id)
    )).all()
    if ids:
        await session.execute(insert(CacheMetadata).values([
            {
                "season_id": season_id,
                "key": player_history.player_history_metadata_key(player_id),
                # Existing schema requires positive versions even for pending keys.
                # Only last_success_at proves that a publication exists.
                "publication_version": 1,
                "history_due_at": now,
            }
            for player_id in ids
        ]).on_conflict_do_nothing(index_elements=("season_id", "key")))
    # Adopt pre-runner checkpoints without re-fetching recently committed work.
    await session.execute(
        update(CacheMetadata)
        .where(
            CacheMetadata.season_id == season_id,
            CacheMetadata.key.startswith(player_history.PLAYER_HISTORY_KEY_PREFIX),
            CacheMetadata.history_due_at.is_(None),
        )
        .values(history_due_at=func.coalesce(
            CacheMetadata.last_success_at + REFRESH_INTERVAL, now,
        ))
    )


async def populate_history(
    session_factory: async_sessionmaker[AsyncSession],
    season_id: str,
    fetcher: Callable[[int], Awaitable[object]],
    *,
    clock: Callable[[], datetime],
    budget: timedelta,
    elapsed_clock: Callable[[], float] = monotonic,
    pacing_elapsed: Callable[[], float] = lambda: 0.0,
    settings: Settings | None = None,
    db_diagnostics: bool = False,
) -> PopulationResult | None:
    """Hold the ingestion lock for the run, with one atomic publish per player.

    Budget includes setup and is checked between players using a monotonic clock;
    an in-flight player finishes even if it crosses the budget. At most one attempt
    per player per run, from the work due at the start of the run. Cancellation
    propagates, leaving committed work intact.
    None means another ingestion already holds the lock.
    """
    if budget < timedelta(0):
        raise ValueError("history budget must be nonnegative")
    started = elapsed_clock()
    async with ingestion_lock(settings) as lock_connection:
        if lock_connection is None:
            logger.info("Shared ingestion already running")
            return None
        return await _populate_history_locked(
            session_factory, season_id, fetcher, lock_connection=lock_connection,
            clock=clock, budget=budget, elapsed_clock=elapsed_clock, started=started,
            pacing_elapsed=pacing_elapsed, db_diagnostics=db_diagnostics,
        )


async def _populate_history_locked(
    session_factory, season_id, fetcher, *, lock_connection,
    clock, budget, elapsed_clock=monotonic, started=None,
    pacing_elapsed=lambda: 0.0,
    db_diagnostics=False,
) -> PopulationResult:
    """Existing runner under a caller-held ingestion lock (no nested acquisition)."""
    counts = _StatementCounts(session_factory, lock_connection) if db_diagnostics else None
    if counts is None:
        return await _populate_history_body(
            session_factory, season_id, fetcher, lock_connection=lock_connection,
            clock=clock, budget=budget, elapsed_clock=elapsed_clock,
            started=started, pacing_elapsed=pacing_elapsed,
        )
    with counts.attached():
        ping_ms = await counts.ping_ms(session_factory, elapsed_clock)
        return await _populate_history_body(
            session_factory, season_id, fetcher, lock_connection=lock_connection,
            clock=clock, budget=budget, elapsed_clock=elapsed_clock,
            started=started, pacing_elapsed=pacing_elapsed,
            counts=counts, ping_ms=ping_ms,
        )


async def _populate_history_body(
    session_factory, season_id, fetcher, *, lock_connection,
    clock, budget, elapsed_clock=monotonic, started=None,
    pacing_elapsed=lambda: 0.0, counts=None, ping_ms=None,
) -> PopulationResult:
    if budget < timedelta(0):
        raise ValueError("history budget must be nonnegative")
    if started is None:
        started = elapsed_clock()
    run_started = elapsed_clock()
    fetch_seconds = pacing_seconds = commit_seconds = 0.0
    max_fetch = max_commit = 0.0
    attempted = 0
    now = _utc(clock())
    async with session_factory.begin() as session:
        await require_same_database(lock_connection, await session.connection())
        await _discover_required_players(session, season_id, now)

    async with session_factory() as session:
        due = func.greatest(
            CacheMetadata.history_due_at, CacheMetadata.next_retry_at,
        )
        numeric_id = cast(func.substr(
            CacheMetadata.key, len(player_history.PLAYER_HISTORY_KEY_PREFIX) + 1,
        ), Integer)
        work = (await session.scalars(
            _player_checkpoints(season_id)
            .where(due <= _utc(clock()))
            .order_by(due, numeric_id)
        )).all()

    succeeded = failed = 0
    budget_exhausted = False
    for checkpoint in work:
        if elapsed_clock() - started >= budget.total_seconds():
            budget_exhausted = True
            break
        player_id = _player_id(checkpoint)
        attempted += 1
        attempted_at = _utc(clock())
        async with session_factory.begin() as session:
            await require_same_database(lock_connection, await session.connection())
            await session.execute(
                update(CacheMetadata)
                .where(
                    CacheMetadata.season_id == season_id,
                    CacheMetadata.key == checkpoint.key,
                )
                .values(last_attempt_at=attempted_at)
            )

        category = "fetch"
        stop_run = False
        commit_started = None
        try:
            fetch_started = elapsed_clock()
            pacing_started = pacing_elapsed()
            if counts is not None:
                counts.phase = "fetch"
            try:
                raw = await fetcher(player_id)
            finally:
                if counts is not None:
                    counts.phase = "other"
                elapsed = max(0.0, elapsed_clock() - fetch_started)
                pacing = max(0.0, pacing_elapsed() - pacing_started)
                pacing = min(pacing, elapsed)
                pacing_seconds += pacing
                fetch = elapsed - pacing
                fetch_seconds += fetch
                max_fetch = max(max_fetch, fetch)
            commit_started = elapsed_clock()
            category = "validation"
            envelope = ElementSummaryEnvelope.model_validate(raw)
            publication = PlayerHistoryPublication(
                season_id=season_id,
                player_id=player_id,
                # Provenance only, not a sweep completion/generation claim.
                completed_sweep_id=f"population-{attempted_at.isoformat()}",
                source_observed_at=attempted_at,
                published_at=_utc(clock()),
            )
        except FetchFailure as error:
            category = error.category
            stop_run = error.stop_run
            envelope = None
        except Exception:
            # Persist the safe category below; cancellation is a BaseException
            # and intentionally bypasses this handler.
            envelope = None
        else:
            committed = False
            if counts is not None:
                counts.phase = "commit"
                commit_statements_before = counts.commit
            async with session_factory() as session:
                # Configuration/lock errors stop the run, not player retries.
                await require_same_database(
                    lock_connection, await session.connection(),
                )
                try:
                    await player_history._write_player_history(
                        session, envelope, publication,
                    )
                    await session.execute(
                        update(CacheMetadata)
                        .where(
                            CacheMetadata.season_id == season_id,
                            CacheMetadata.key == checkpoint.key,
                        )
                        .values(
                            history_due_at=publication.published_at + REFRESH_INTERVAL,
                            last_attempt_at=attempted_at,
                        )
                    )
                    await session.commit()
                except Exception as error:
                    await session.rollback()
                    category = (
                        "validation" if isinstance(error, ValueError)
                        else "publication"
                    )
                else:
                    succeeded += 1
                    committed = True
            if counts is not None:
                counts.player_commits.append(counts.commit - commit_statements_before)
                counts.phase = "other"
            if committed:
                continue
        finally:
            if commit_started is not None:
                commit = max(0.0, elapsed_clock() - commit_started)
                commit_seconds += commit
                max_commit = max(max_commit, commit)
            if counts is not None and counts.phase == "commit":
                counts.player_commits.append(counts.commit - commit_statements_before)
                counts.phase = "other"

        # Separate short transaction after rollback; never overwrite success,
        # source time, coverage, rows, or publication versions on failure.
        async with session_factory.begin() as session:
            await require_same_database(lock_connection, await session.connection())
            state = await session.get(CacheMetadata, (season_id, checkpoint.key))
            state.retry_count = (state.retry_count or 0) + 1
            delay = min(
                timedelta(minutes=30 * 2 ** min(state.retry_count - 1, 4)),
                timedelta(hours=6),
            )
            state.next_retry_at = _utc(clock()) + delay
            state.last_error_category = category
            # Never persist exception text, which may contain URLs/credentials.
            state.last_error = f"Player history {category} failed"
        failed += 1
        if stop_run:
            budget_exhausted = True
            break

    async with session_factory() as session:
        states = (await session.scalars(_player_checkpoints(season_id))).all()
    ended_at = _utc(clock())
    result = PopulationResult(
        succeeded=succeeded,
        failed=failed,
        pending=sum(
            state.last_success_at is None
            or state.next_retry_at is not None
            or state.history_due_at <= ended_at
            for state in states
        ),
        required=len(states),
        budget_exhausted=budget_exhausted,
    )
    other_seconds = max(0.0, elapsed_clock() - run_started - fetch_seconds
                        - pacing_seconds - commit_seconds)
    message = (
        "History population: success=%d failed=%d pending=%d required=%d "
        "fetch_s=%.3f pacing_s=%.3f commit_s=%.3f other_s=%.3f "
        "fetch_avg_s=%.3f fetch_max_s=%.3f commit_avg_s=%.3f commit_max_s=%.3f"
    )
    values = (
        result.succeeded, result.failed, result.pending, result.required,
        fetch_seconds, pacing_seconds, commit_seconds, other_seconds,
        fetch_seconds / attempted if attempted else 0.0, max_fetch,
        commit_seconds / attempted if attempted else 0.0, max_commit,
    )
    if counts is not None:
        message += (
            " db_ping_ms=%.3f stmts_commit_avg=%.3f stmts_commit_max=%d "
            "stmts_other_avg=%.3f"
        )
        values += (
            ping_ms,
            sum(counts.player_commits) / len(counts.player_commits)
            if counts.player_commits else 0.0,
            max(counts.player_commits, default=0),
            counts.other / attempted if attempted else 0.0,
        )
    logger.info(message, *values)
    return result


@dataclass(frozen=True)
class PlayerHistoryStatus:
    player_id: int
    status: Literal["current", "stale", "unknown"]
    source_observed_at: datetime | None
    last_success_at: datetime | None
    source_age: timedelta | None
    success_age: timedelta | None
    missing_fixture_ids: tuple[int, ...]
    invalid_fixture_ids: tuple[int, ...]
    last_attempt_at: datetime | None
    retry_count: int
    next_retry_at: datetime | None
    error_category: str | None


@dataclass(frozen=True)
class HistoryPopulationStatus:
    publication_version: int | None
    players: tuple[PlayerHistoryStatus, ...]


async def read_population_status(
    session_factory: async_sessionmaker[AsyncSession],
    season_id: str,
    *,
    now: datetime,
    required_finalized_fixture_ids: dict[int, set[int]],
) -> HistoryPopulationStatus:
    """Read one consistent per-player view using the 48-hour age limit.

    Callers supply known required finalized fixtures AFTER the grace period.
    This slice does not infer historical membership/coverage from current clubs.
    Status is relative to that supplied coverage, not production solve eligibility.
    """
    now = _utc(now)
    async with session_factory() as session:
        await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"},
        )
        summary = await session.get(CacheMetadata, (season_id, "history"))
        states = {_player_id(state): state for state in (
            await session.scalars(_player_checkpoints(season_id))
        ).all()}
    players = []
    for player_id in sorted(states.keys() | required_finalized_fixture_ids.keys()):
        state = states.get(player_id)
        source = state.source_observed_at if state else None
        success = state.last_success_at if state else None
        covered = set(state.covered_finalized_fixture_ids or []) if state else set()
        recorded_missing = set(state.missing_fixture_ids or []) if state else set()
        missing = tuple(sorted(
            (required_finalized_fixture_ids.get(player_id, set()) - covered)
            | recorded_missing
        ))
        invalid = tuple(sorted(state.invalid_fixture_ids or [])) if state else ()
        source_age = success_age = None
        status = "unknown"
        if source is not None and success is not None:
            if not source <= success <= now:
                raise ValueError("history publication must satisfy source <= success <= now")
            source_age, success_age = now - source, now - success
            status = (
                "current"
                if source_age <= HISTORY_AGE_LIMIT and not missing and not invalid
                else "stale"
            )
        players.append(PlayerHistoryStatus(
            player_id=player_id,
            status=status,
            source_observed_at=source,
            last_success_at=success,
            source_age=source_age,
            success_age=success_age,
            missing_fixture_ids=missing,
            invalid_fixture_ids=invalid,
            last_attempt_at=state.last_attempt_at if state else None,
            retry_count=(state.retry_count or 0) if state else 0,
            next_retry_at=state.next_retry_at if state else None,
            error_category=state.last_error_category if state else None,
        ))
    return HistoryPopulationStatus(
        summary.publication_version if summary else None, tuple(players),
    )
