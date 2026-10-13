"""Settings validation tests."""

import pytest
from pydantic import ValidationError

from fpl_optimizer.config import Settings


@pytest.mark.parametrize("origin", [
    "*", "https://*.example", "https://example/path", "https://example/",
    "https://user:pw@example", "https://example?q=1", "https://example#fragment",
    "https://example:99999", "null", "https://one,https://two",
])
def test_frontend_origin_rejects_non_origins(origin):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://dummy:dummy@localhost/dummy_test",
            frontend_origin=origin,
        )


@pytest.mark.parametrize("origin", ["", "https://frontend.example", "http://localhost:3000"])
def test_frontend_origin_accepts_empty_or_one_exact_origin(origin):
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://dummy:dummy@localhost/dummy_test",
        frontend_origin=origin,
    )
    assert settings.frontend_origin == origin


def test_additional_ca_is_optional_and_loaded_from_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_SSL_CA_FILE", raising=False)
    assert Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:password@localhost/database",
    ).database_ssl_ca_file is None
    ca_file = tmp_path / "provider-ca.pem"
    monkeypatch.setenv("DATABASE_SSL_CA_FILE", str(ca_file))
    assert Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:password@localhost/database",
    ).database_ssl_ca_file == ca_file


def test_settings_accept_async_postgres_url() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@localhost/database"
    )

    assert settings.database_url.get_secret_value().startswith(
        "postgresql+asyncpg://"
    )


def test_database_password_is_hidden_from_repr_and_validation_errors() -> None:
    fake_password = "FAKE_PW_12345"
    settings = Settings(
        database_url=(
            f"postgresql+asyncpg://user:{fake_password}@localhost/database"
        )
    )

    assert fake_password not in repr(settings)

    with pytest.raises(ValidationError) as error:
        Settings(
            database_url=f"postgresql://u:{fake_password}@x/d"
        )

    assert fake_password not in str(error.value)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://user:password@localhost/database",
        "sqlite+aiosqlite:///test.db",
        "not-a-database-url",
    ],
)
def test_settings_reject_non_async_postgres_urls(database_url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=database_url)
