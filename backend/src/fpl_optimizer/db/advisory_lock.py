"""One database-wide session lock shared by ingestion and future migrations."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from secrets import randbits

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fpl_optimizer.config import Settings, get_settings
from fpl_optimizer.db.session import create_database_engine


# Stable signed bigint (ASCII "FPLOPT01"). Never vary by season or caller.
INGESTION_LOCK_KEY = int.from_bytes(b"FPLOPT01", "big")


async def require_same_database(
    lock_connection: AsyncConnection, write_connection: AsyncConnection,
) -> None:
    """Prove both live connections reach the same database before any writes.

    A fresh two-int advisory marker is visible in pg_locks only on its server.
    Require its owning PID and the writer's current database OID to match too.
    This avoids URL comparisons, coincident database names/OIDs on other servers,
    and privileged control-file functions. Direct and session-pooler endpoints
    work identically. The marker's two-int namespace is separate from the fixed
    bigint ingestion key, and no persistent data is written by this check.
    """
    keys = {"first": randbits(32) - 2**31, "second": randbits(32) - 2**31}
    marker_acquired = False
    try:
        marker_acquired = bool(await lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:first, :second)"), keys,
        ))
        if not marker_acquired:
            raise RuntimeError("Database identity check unavailable; publication refused")
        owner_pid = await lock_connection.scalar(text("SELECT pg_backend_pid()"))
        matches = await write_connection.scalar(text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_locks
                WHERE locktype = 'advisory' AND granted
                  AND mode = 'ExclusiveLock' AND pid = :owner_pid
                  AND classid = :first AND objid = :second AND objsubid = 2
                  AND database = (
                      SELECT oid FROM pg_catalog.pg_database
                      WHERE datname = current_database()
                  )
            )
        """), {
            "owner_pid": owner_pid,
            "first": keys["first"] & 0xFFFFFFFF,
            "second": keys["second"] & 0xFFFFFFFF,
        })
        if not matches:
            raise ValueError(
                "SESSION_DATABASE_URL and the write connection do not reach the "
                "same PostgreSQL database; publication refused"
            )
    finally:
        if marker_acquired:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:first, :second)"), keys,
            )


@asynccontextmanager
async def ingestion_lock(
    settings: Settings | None = None,
) -> AsyncIterator[AsyncConnection | None]:
    """Yield the held lock connection, or None on contention; release on exit.

    SESSION_DATABASE_URL must use the SAME database as DATABASE_URL, through a
    direct/session endpoint. NullPool disables local connection reuse; it cannot
    turn an external transaction pooler into a session endpoint. Future commands
    must wrap their entire run (including fetches) in this context. Migrations
    must use this same helper/key and abort if it yields None. Before writing,
    call require_same_database with this connection and the write connection.
    """
    resolved = settings or get_settings()
    if resolved.session_database_url is None:
        raise ValueError("SESSION_DATABASE_URL requires a direct/session endpoint")
    lock_settings = resolved.model_copy(
        update={"database_url": resolved.session_database_url}
    )
    engine = create_database_engine(lock_settings, pooled=False)
    connection = None
    acquired = False

    async def release() -> None:
        try:
            if connection is not None:
                try:
                    if acquired:
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:key)"),
                            {"key": INGESTION_LOCK_KEY},
                        )
                finally:
                    # NullPool physically closes the session, also releasing a
                    # lock if acquisition/unlock was interrupted or failed.
                    await connection.close()
        finally:
            await engine.dispose()

    try:
        connection = await engine.connect()
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        acquired = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": INGESTION_LOCK_KEY},
            )
        )
        yield connection if acquired else None
    finally:
        cleanup = asyncio.create_task(release())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
