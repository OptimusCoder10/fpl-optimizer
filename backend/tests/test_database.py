"""PostgreSQL wiring smoke tests."""

import ssl
from unittest.mock import patch

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import DatabaseTestSettings, create_guarded_test_engine
from fpl_optimizer.config import Settings
from fpl_optimizer.db.session import create_database_engine, create_session_factory


pytestmark = pytest.mark.database


def test_non_test_database_is_rejected_before_engine_creation() -> None:
    unsafe_settings = DatabaseTestSettings(
        test_database_url=(
            "postgresql+asyncpg://user:password@localhost/fpl_optimizer"
        )
    )

    with patch("conftest.create_database_engine") as engine_factory:
        with pytest.raises(RuntimeError, match="name ends in '_test'"):
            create_guarded_test_engine(unsafe_settings)

    engine_factory.assert_not_called()


@pytest.mark.parametrize("host", ["db.example.com", "localhost.example.com"])
def test_remote_database_uses_certificate_verifying_tls(host: str) -> None:
    settings = Settings(
        database_url=f"postgresql+asyncpg://user:password@{host}/database"
    )

    with patch("fpl_optimizer.db.session.create_async_engine") as engine_factory:
        create_database_engine(settings)

    ssl_context = engine_factory.call_args.kwargs["connect_args"]["ssl"]
    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert ssl_context.check_hostname is True


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_local_database_does_not_use_ssl(host: str) -> None:
    settings = Settings(
        database_url=f"postgresql+asyncpg://user:password@{host}/database"
    )

    with patch("fpl_optimizer.db.session.create_async_engine") as engine_factory:
        create_database_engine(settings)

    assert "connect_args" not in engine_factory.call_args.kwargs


def test_libpq_sslmode_is_removed_before_asyncpg_receives_url() -> None:
    settings = Settings(
        database_url=(
            "postgresql+asyncpg://user:password@db.example.com/database"
            "?sslmode=verify-full&application_name=fpl_optimizer"
        )
    )

    with patch("fpl_optimizer.db.session.create_async_engine") as engine_factory:
        create_database_engine(settings)

    database_url = make_url(engine_factory.call_args.args[0])
    assert "sslmode" not in database_url.query
    assert database_url.query["application_name"] == "fpl_optimizer"


async def test_async_engine_connects_to_test_database(
    test_engine: AsyncEngine,
) -> None:
    async with test_engine.connect() as connection:
        value = await connection.scalar(text("SELECT 1"))

    assert value == 1


async def test_async_session_executes_query(test_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(test_engine)

    async with session_factory() as session:
        database_name = await session.scalar(text("SELECT current_database()"))

    assert database_name == "fpl_optimizer_test"
