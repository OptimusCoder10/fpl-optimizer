"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPOSITORY_ROOT / ".env"


def _validate_async_postgres_url(value: object) -> str:
    """Require the async PostgreSQL dialect used by the data layer."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("DATABASE_URL must be a non-empty string")

    try:
        url = make_url(value)
    except ArgumentError as error:
        raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from error

    if url.drivername != "postgresql+asyncpg":
        raise ValueError("DATABASE_URL must use the postgresql+asyncpg dialect")

    return value


AsyncPostgresUrl = Annotated[
    SecretStr,
    BeforeValidator(_validate_async_postgres_url),
]


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or the root .env file."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: AsyncPostgresUrl
    # Must address the same database using a direct or session-mode endpoint.
    session_database_url: AsyncPostgresUrl | None = None


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process."""
    return Settings()
