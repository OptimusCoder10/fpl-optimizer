"""Run with python -m fpl_optimizer.ingest; refresh shared data outside requests."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging

from pydantic import ValidationError
from sqlalchemy import select

from fpl_optimizer.config import Settings
from fpl_optimizer.db.advisory_lock import ingestion_lock, require_same_database
from fpl_optimizer.db.models import CacheMetadata, Gameweek
from fpl_optimizer.db.session import create_database_engine, create_session_factory
from fpl_optimizer.schemas.bootstrap import SharedPublication
from fpl_optimizer.schemas.fetch import FetchFailure
from fpl_optimizer.schemas.fixtures import (
    IncompleteFixtures, SEASON_CLUB_COUNTS, validate_full_season,
)
from fpl_optimizer.services.fpl_http import FPLClient
from fpl_optimizer.services.history_coverage_shadow import collect_history_coverage_shadow
from fpl_optimizer.services.history_population import PopulationResult, _populate_history_locked
from fpl_optimizer.services.shared_catalog import _write_shared_catalog
from fpl_optimizer.services.shared_archive import (
    ArchiveError, ArchiveResult, SupabaseStorage, archive_shared_publication,
)

logger = logging.getLogger(__name__)
SHADOW_MIN_BUDGET_SECONDS = 5.0


@dataclass(frozen=True)
class IngestionResult:
    status: str
    shared_published: bool = False
    history: PopulationResult | None = None
    category: str | None = None
    archive: ArchiveResult | None = None


async def _shared_due(session, season_id, now):
    state = await session.get(CacheMetadata, (season_id, "shared"))
    if state is None or state.last_success_at is None or state.source_observed_at is None:
        return True
    deadline = await session.scalar(select(Gameweek.deadline_time).where(
        Gameweek.season_id == season_id, Gameweek.deadline_time > now,
    ).order_by(Gameweek.deadline_time).limit(1))
    interval = (timedelta(minutes=30) if deadline and deadline - now <= timedelta(hours=24)
                else timedelta(hours=4))
    return now - state.source_observed_at >= interval


async def _record_shared_failure(factory, connection, season_id, attempted_at, category):
    # Without a prior trusted catalog, no season/metadata is fabricated on failure.
    async with factory.begin() as session:
        await require_same_database(connection, await session.connection())
        state = await session.get(CacheMetadata, (season_id, "shared"))
        if state is not None:
            state.last_attempt_at = attempted_at
            state.last_error_category = category
            state.last_error = "Shared catalog fetch or validation failed"
            state.retry_count = (state.retry_count or 0) + 1
    logger.info("Shared ingestion: category=%s success=0 failed=1", category)


async def run_ingestion(
    session_factory, client: FPLClient, *, season_id: str, rules_version: str,
    settings: Settings, history_budget: timedelta = timedelta(minutes=10),
    archive_storage=None,
) -> IngestionResult:
    if season_id not in SEASON_CLUB_COUNTS:
        raise ValueError("season has no reviewed fixture completeness policy")
    if history_budget <= timedelta(0):
        raise ValueError("history budget must be positive")
    async with ingestion_lock(settings) as connection:
        if connection is None:
            logger.info("already running")
            return IngestionResult("already_running")
        async with session_factory() as session:
            await require_same_database(connection, await session.connection())
            due = await _shared_due(session, season_id, client.clock())
        published = False
        archive_result = None
        if due:
            observed_at = client.clock()
            try:
                bootstrap_fetch = await client.bootstrap()
                bootstrap = bootstrap_fetch.require_data()
                fixtures_fetch = await client.fixtures()
                fixtures = fixtures_fetch.require_data()
                envelope = validate_full_season(season_id, bootstrap, fixtures)
            except (FetchFailure, IncompleteFixtures, ValidationError) as error:
                category = (error.category if isinstance(error, FetchFailure)
                            else "incomplete_fixtures" if isinstance(error, IncompleteFixtures)
                            else "truncated_or_invalid")
                await _record_shared_failure(
                    session_factory, connection, season_id, observed_at, category,
                )
                return IngestionResult(
                    "budget_exhausted" if client.exhausted else "failed", category=category,
                )
            try:
                async with session_factory.begin() as session:
                    await require_same_database(connection, await session.connection())
                    published_at = client.clock()
                    await _write_shared_catalog(session, envelope, SharedPublication(
                        season_id=season_id, rules_version=rules_version,
                        source_observed_at=observed_at, published_at=published_at,
                    ))
                    state = await session.get(CacheMetadata, (season_id, "shared"))
                    state.last_attempt_at = observed_at
                    state.last_error_category = state.last_error = state.next_retry_at = None
                    state.retry_count = 0
            except Exception:
                await _record_shared_failure(
                    session_factory, connection, season_id, observed_at, "publication",
                )
                return IngestionResult("failed", category="publication")
            published = True
            logger.info("Shared ingestion: success=1 failed=0 clubs=%d players=%d fixtures=%d",
                        len(bootstrap.teams), len(bootstrap.elements), len(fixtures.root))
            if archive_storage is not None or any((
                settings.fpl_storage_url, settings.fpl_storage_key,
                settings.fpl_storage_bucket, settings.fpl_storage_allowance_bytes,
            )):
                try:
                    if settings.fpl_storage_allowance_bytes is None:
                        raise ArchiveError("not_configured")
                    try:
                        allowance_bytes = int(settings.fpl_storage_allowance_bytes)
                    except (TypeError, ValueError):
                        raise ArchiveError("not_configured") from None
                    if allowance_bytes <= 0:
                        raise ArchiveError("not_configured")
                    storage = archive_storage or SupabaseStorage(settings)
                    async with asyncio.timeout(60):
                        archive_result = await archive_shared_publication(
                            storage, bootstrap=bootstrap_fetch.raw_shared_payload,
                            fixtures=fixtures_fetch.raw_shared_payload,
                            season_id=season_id, rules_version=rules_version,
                            collected_from=observed_at,
                            collected_to=fixtures_fetch.fetched_at,
                            published_at=published_at,
                            allowance_bytes=allowance_bytes,
                        )
                except ArchiveError as error:
                    safe_category = error.category if error.category in {
                        "transport", "storage_response", "duplicate_key", "listing_invalid",
                        "raw_payload_unavailable", "not_configured",
                    } else "archive_internal"
                    archive_result = ArchiveResult("missed", safe_category)
                except Exception:
                    # Storage and serialization errors cannot invalidate committed data.
                    archive_result = ArchiveResult("missed", "archive_internal")
                if archive_result.status == "missed":
                    logger.warning("Shared archive: captured=0 missed=1 category=%s",
                                   archive_result.category)
                else:
                    logger.info("Shared archive: captured=1 missed=0")
                if archive_result.allowance_warning:
                    logger.warning("Shared archive: allowance_at_or_above_80_percent=1")
            else:
                archive_result = ArchiveResult("missed", "not_configured")
                logger.warning("Shared archive: captured=0 missed=1 category=not_configured")
        if client.exhausted or client.remaining() <= 0:
            return IngestionResult("budget_exhausted", shared_published=published,
                                   archive=archive_result)
        client.deadline = min(client.deadline, client.elapsed_clock() + history_budget.total_seconds())

        async def fetch_history(player_id):
            return (await client.element_summary(player_id)).require_data()

        history = await _populate_history_locked(
            session_factory, season_id, fetch_history, lock_connection=connection,
            clock=client.clock, budget=timedelta(seconds=client.remaining()),
            elapsed_clock=client.elapsed_clock,
            pacing_elapsed=lambda: client.player_pacing_seconds,
            db_diagnostics=True,
        )
        # History has emitted its count line and detached statement diagnostics.
        # This read remains under the same ingestion lock and the remaining budget.
        remaining = client.remaining()
        if remaining < SHADOW_MIN_BUDGET_SECONDS:
            logger.info("Coverage shadow: category=insufficient_budget")
        else:
            try:
                async with asyncio.timeout(remaining):
                    counts = await collect_history_coverage_shadow(
                        session_factory, season_id, now=client.clock(),
                    )
                logger.info(counts.log_line())
            except TimeoutError:
                logger.warning("Coverage shadow: category=timeout")
            except Exception:
                # Diagnostic reads cannot change ingestion's published result.
                logger.warning("Coverage shadow: category=read_failure")
        status = ("budget_exhausted" if history.budget_exhausted
                  else "partial" if history.failed else "success")
        return IngestionResult(status, published, history, archive=archive_result)


async def _main() -> int:
    engine = None
    exit_code = 1
    try:
        settings = Settings()
        if settings.fpl_season_id is None or settings.fpl_rules_version is None:
            raise ValueError("ingestion season and rules version are required")
        engine = create_database_engine(settings)
        async with FPLClient(budget_seconds=settings.fpl_run_budget_seconds) as client:
            result = await run_ingestion(
                create_session_factory(engine), client, season_id=settings.fpl_season_id,
                rules_version=settings.fpl_rules_version, settings=settings,
                history_budget=timedelta(seconds=settings.fpl_history_budget_seconds),
            )
        exit_code = 1 if result.status in {"failed", "partial"} else 0
    except Exception:
        # No upstream bodies, URLs, credentials, exception text or traceback.
        logger.error("Ingestion command: success=0 failed=1")
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                logger.error("Ingestion cleanup: success=0 failed=1")
                exit_code = 1
    return exit_code


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
