"""Shared pytest fixtures for PostgreSQL-backed smoke tests."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
import pytest_asyncio
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.engine import make_url

from fpl_optimizer.config import ENV_FILE, Settings
from fpl_optimizer.db.session import create_database_engine


DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://fpl_optimizer:fpl_optimizer_test@"
    "127.0.0.1:5433/fpl_optimizer_test"
)
BACKEND_ROOT = Path(__file__).resolve().parents[1]


class DatabaseTestSettings(BaseSettings):
    """Test-only settings loaded from the root .env and process environment."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    test_database_url: str = DEFAULT_TEST_DATABASE_URL


def create_guarded_test_engine(test_settings: DatabaseTestSettings | None = None):
    """Reject non-test destinations before constructing a database engine."""
    resolved_settings = test_settings or DatabaseTestSettings()
    database_url = make_url(resolved_settings.test_database_url)
    database_name = database_url.database or ""

    if not database_name.endswith("_test"):
        raise RuntimeError(
            "TEST_DATABASE_URL must target a database whose name ends in '_test'"
        )

    return create_database_engine(
        Settings(database_url=resolved_settings.test_database_url)
    )


@pytest.fixture
def publication_settings(test_engine):
    """Use the guarded disposable database for both independent connections."""
    url = test_engine.url.render_as_string(hide_password=False)
    return Settings(database_url=url, session_database_url=url)


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Yield an engine after explicitly validating its test destination."""
    engine = create_guarded_test_engine()

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def migrated_test_engine(test_engine):
    """Rebuild the guarded test schema and apply Alembic from an empty database."""
    async with test_engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))

    environment = os.environ.copy()
    environment["DATABASE_URL"] = DatabaseTestSettings().test_database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Alembic failed to migrate the empty test database:\n"
            f"{result.stdout}\n{result.stderr}"
        )

    yield test_engine
