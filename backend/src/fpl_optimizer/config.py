"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BeforeValidator, SecretStr, field_validator
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
    # Additional trusted PEM CA for both remote write and session connections.
    database_ssl_ca_file: Path | None = None
    frontend_origin: str = ""

    @field_validator("frontend_origin")
    @classmethod
    def validate_frontend_origin(cls, value: str) -> str:
        """Allow one explicit HTTP(S) origin; an empty value denies CORS."""
        value = value.strip()
        if not value:
            return ""
        origin = urlsplit(value)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path
            or origin.query
            or origin.fragment
            or "*" in value
            or any(character.isspace() for character in value)
            or "," in value
        ):
            raise ValueError("FRONTEND_ORIGIN must be one HTTP(S) origin without a path")
        # Accessing port also rejects malformed/out-of-range ports.
        origin.port
        return value

    @field_validator("database_ssl_ca_file", mode="before")
    @classmethod
    def empty_ca_file_is_unset(cls, value: object) -> object:
        """An empty environment-example placeholder uses the default TLS path."""
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process."""
    return Settings()
