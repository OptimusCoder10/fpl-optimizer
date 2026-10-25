"""Separate command: python -m fpl_optimizer.capture_research [--self-test].

Self-test uses synthetic bytes only, never reads player data, and never produces
prospective evidence. Neither mode changes ingestion outcomes or its exit code.
"""

import argparse
import asyncio
import logging
import time

from fpl_optimizer.config import Settings
from fpl_optimizer.db.advisory_lock import ingestion_lock, require_same_database
from fpl_optimizer.db.session import create_database_engine, create_session_factory
from fpl_optimizer.services.forecast_bundle_view import read_bundle_view
from fpl_optimizer.services.research_capture import (
    Attempt, CaptureClock, CaptureResult, capture, isolated,
)
from fpl_optimizer.services.research_storage import ResearchStorage
from fpl_optimizer.services.shared_archive import ArchiveError

logger = logging.getLogger(__name__)


async def run(*, self_test=False):
    attempt = Attempt()

    async def operation():
        settings = Settings()
        if not settings.fpl_season_id and not self_test:
            raise ArchiveError("not_configured")
        try:
            allowance = int(settings.fpl_storage_allowance_bytes)
            if allowance <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ArchiveError("not_configured") from None
        storage = ResearchStorage(settings)
        clock = CaptureClock()
        async with ingestion_lock(settings) as connection:
            if connection is None:
                return CaptureResult("skipped", "already_running")
            engine = create_database_engine(settings)
            try:
                factory = create_session_factory(engine)
                async with factory() as session:
                    await require_same_database(connection, await session.connection())
                view = None if self_test else await read_bundle_view(
                    factory, settings.fpl_season_id, as_of=clock.now(),
                )
                return await capture(view, storage, allowance=allowance, clock=clock,
                                     attempt=attempt, self_test=self_test)
            finally:
                await engine.dispose()
    return await isolated(operation, attempt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    started = time.monotonic()
    result = asyncio.run(run(self_test=args.self_test))
    logger.info("Research capture: mode=%s status=%s category=%s bytes=%d duration_s=%.3f margin_s=%s players=%d excluded=%d",
                "self_test" if args.self_test else "prospective", result.status, result.category,
                result.bytes_used, time.monotonic() - started, result.margin_seconds, result.players, result.exclusions)
    # Capture failures are conveyed by safe status, never an ingestion exit change.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
