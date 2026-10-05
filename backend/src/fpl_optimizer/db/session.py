"""Async SQLAlchemy engine and session factories."""

import ssl

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fpl_optimizer.config import Settings, get_settings


LOCAL_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _prepare_database_connection(settings: Settings) -> tuple[URL, ssl.SSLContext | None]:
    """Sanitize the asyncpg URL and require verified TLS for remote hosts."""
    database_url = make_url(settings.database_url.get_secret_value())
    libpq_ssl_options = {
        key for key in database_url.query if key.lower().startswith("ssl")
    }
    sanitized_url = database_url.difference_update_query(libpq_ssl_options)

    host = (sanitized_url.host or "").lower()
    ssl_context = None
    if host not in LOCAL_DATABASE_HOSTS:
        ssl_context = ssl.create_default_context()

    return sanitized_url, ssl_context


def create_database_engine(
    settings: Settings | None = None,
    *,
    echo: bool = False,
) -> AsyncEngine:
    """Create an async engine without opening a connection eagerly."""
    resolved_settings = settings or get_settings()
    database_url, ssl_context = _prepare_database_connection(resolved_settings)
    connect_args = {"ssl": ssl_context} if ssl_context is not None else {}

    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        **({"connect_args": connect_args} if connect_args else {}),
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create the reusable async-session factory for an engine."""
    return async_sessionmaker(engine, expire_on_commit=False)
