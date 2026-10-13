"""Health checks never fetch FPL or use a hosted database."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from fpl_optimizer.config import Settings
from fpl_optimizer.main import create_app
from fpl_optimizer.services.solver_smoke import SolverProof


PROOF = SolverProof("optimal", 7, (0, 1, 1))
FAKE_SECRET = "synthetic-password-do-not-expose"


@asynccontextmanager
async def client_for(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


@pytest.fixture
def synthetic_settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://dummy:dummy@127.0.0.1/dummy_test",
        database_ssl_ca_file=None,
        frontend_origin="",
    )


@pytest.fixture
def mock_dependencies(monkeypatch):
    """No real connection: returning/raising in the engine context drives checks."""
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.scalar.return_value = 1
    engine = AsyncMock()
    engine.connect = lambda: connection
    monkeypatch.setattr("fpl_optimizer.main.create_database_engine", lambda settings: engine)
    monkeypatch.setattr("fpl_optimizer.main.check_solver", AsyncMock(return_value=PROOF))
    return engine, connection


@pytest.mark.database
async def test_readiness_with_local_database_and_actual_cbc(publication_settings):
    # publication_settings derives exclusively from the guarded local _test engine.
    app = create_app(publication_settings.model_copy(update={"frontend_origin": ""}))
    async with client_for(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready", "database": "available",
        "solver": {"status": "optimal", "objective": 7, "selection": [0, 1, 1]},
    }


async def test_database_failure_is_safe_and_liveness_independent(
    synthetic_settings, mock_dependencies, caplog,
):
    engine, connection = mock_dependencies
    connection.__aenter__.side_effect = RuntimeError(f"connection string {FAKE_SECRET}")
    app = create_app(synthetic_settings)
    async with client_for(app) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 503
    assert ready.json()["database"] == "unavailable"
    assert FAKE_SECRET not in ready.text + caplog.text
    assert "Traceback" not in ready.text + caplog.text
    engine.dispose.assert_awaited_once()


async def test_database_check_has_a_wall_limit(synthetic_settings, mock_dependencies, monkeypatch):
    _, connection = mock_dependencies
    entered = asyncio.Event()
    async def stall():
        entered.set()
        await asyncio.Event().wait()
    connection.__aenter__.side_effect = stall
    monkeypatch.setattr("fpl_optimizer.main.DATABASE_CHECK_SECONDS", 0.02)
    async with client_for(create_app(synthetic_settings)) as client:
        response = await asyncio.wait_for(client.get("/health/ready"), timeout=1)
        assert entered.is_set()
        assert response.status_code == 503


@pytest.mark.parametrize("failure", [None, RuntimeError(FAKE_SECRET)])
async def test_solver_failure_is_safe(
    failure, synthetic_settings, mock_dependencies, monkeypatch, caplog,
):
    probe = AsyncMock(return_value=None) if failure is None else AsyncMock(side_effect=failure)
    monkeypatch.setattr("fpl_optimizer.main.check_solver", probe)
    async with client_for(create_app(synthetic_settings)) as client:
        response = await client.get("/health/ready")
        assert (await client.get("/health/live")).status_code == 200
    assert response.status_code == 503
    assert response.json()["solver"] == {"status": "unavailable"}
    assert FAKE_SECRET not in response.text + caplog.text
    probe.assert_awaited_once()


async def test_bad_ca_or_engine_config_does_not_escape(
    synthetic_settings, mock_dependencies, monkeypatch, caplog,
):
    def fail(settings):
        raise RuntimeError(FAKE_SECRET)
    monkeypatch.setattr("fpl_optimizer.main.create_database_engine", fail)
    async with client_for(create_app(synthetic_settings)) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert FAKE_SECRET not in response.text + caplog.text


async def test_invalid_settings_have_safe_health_responses(mock_dependencies, monkeypatch, caplog):
    def fail():
        raise ValueError(FAKE_SECRET)
    monkeypatch.setattr("fpl_optimizer.main.get_settings", fail)
    async with client_for(create_app()) as client:
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert FAKE_SECRET not in response.text + caplog.text


async def test_shutdown_failure_is_logged_without_exception_details(
    synthetic_settings, mock_dependencies, caplog,
):
    engine, _ = mock_dependencies
    engine.dispose.side_effect = RuntimeError(FAKE_SECRET)
    async with client_for(create_app(synthetic_settings)) as client:
        assert (await client.get("/health/live")).status_code == 200
    assert "Database shutdown unavailable" in caplog.text
    assert FAKE_SECRET not in caplog.text


@pytest.mark.parametrize("allowed", ["", "https://frontend.example"])
async def test_cors_only_allows_the_configured_origin(allowed, synthetic_settings, mock_dependencies):
    app = create_app(synthetic_settings.model_copy(update={"frontend_origin": allowed}))
    async with client_for(app) as client:
        for origin in ("https://frontend.example", "https://other.example", "null"):
            response = await client.get("/health/live", headers={"Origin": origin})
            preflight = await client.options("/health/ready", headers={
                "Origin": origin, "Access-Control-Request-Method": "GET",
            })
            if origin == allowed:
                assert response.headers["access-control-allow-origin"] == origin
                assert preflight.status_code == 200
            else:
                assert "access-control-allow-origin" not in response.headers
                assert "access-control-allow-origin" not in preflight.headers
                assert preflight.status_code == 400
        assert (await client.get("/health/live")).status_code == 200


async def test_readiness_does_not_repeat_solver_work(synthetic_settings, mock_dependencies, monkeypatch):
    probe = AsyncMock(return_value=PROOF)
    monkeypatch.setattr("fpl_optimizer.main.check_solver", probe)
    async with client_for(create_app(synthetic_settings)) as client:
        for _ in range(3):
            assert (await client.get("/health/ready")).status_code == 200
    probe.assert_awaited_once()
