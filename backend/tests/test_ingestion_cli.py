"""Exercise CLI environment/budget wiring and safe exits without database/network."""

import logging

import pytest

from fpl_optimizer import ingest
from fpl_optimizer.config import Settings
from fpl_optimizer.services.shared_archive import ArchiveResult


@pytest.mark.parametrize("status,expected", [
    ("success", 0), ("already_running", 0), ("budget_exhausted", 0),
    ("failed", 1), ("partial", 1),
])
async def test_cli_exit_codes_and_environment_wiring(monkeypatch, status, expected):
    settings = Settings(_env_file=None,
        database_url="postgresql+asyncpg://test@127.0.0.1/fpl_test",
        fpl_season_id="2026-27", fpl_rules_version="synthetic-v1",
        fpl_run_budget_seconds=60, fpl_history_budget_seconds=30)
    monkeypatch.setattr(ingest, "Settings", lambda: settings)
    disposed = []
    class Engine:
        async def dispose(self):
            disposed.append(True)
    engine = Engine()
    monkeypatch.setattr(ingest, "create_database_engine", lambda s: engine)
    monkeypatch.setattr(ingest, "create_session_factory", lambda e: "factory")
    class Client:
        def __init__(self, *, budget_seconds):
            assert budget_seconds == 60
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(ingest, "FPLClient", Client)
    async def run(factory, client, **kwargs):
        assert factory == "factory"
        assert kwargs["season_id"] == "2026-27"
        assert kwargs["rules_version"] == "synthetic-v1"
        assert kwargs["history_budget"].total_seconds() == 30
        archive = ArchiveResult("missed", "not_configured") if status == "success" else None
        return ingest.IngestionResult(status, archive=archive)
    monkeypatch.setattr(ingest, "run_ingestion", run)
    assert await ingest._main() == expected
    assert disposed == [True]


async def test_cli_configuration_errors_do_not_expose_credentials(monkeypatch, caplog):
    def fail():
        raise ValueError("SECRET database URL")
    monkeypatch.setattr(ingest, "Settings", fail)
    with caplog.at_level(logging.ERROR):
        assert await ingest._main() == 1
    assert "SECRET" not in caplog.text and "Traceback" not in caplog.text
    assert "success=0 failed=1" in caplog.text
