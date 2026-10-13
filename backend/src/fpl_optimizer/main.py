"""Minimal runtime health app; data readiness belongs to the future context API."""

import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from fpl_optimizer.config import Settings, get_settings
from fpl_optimizer.db.session import create_database_engine
from fpl_optimizer.services.solver_smoke import check_solver


logger = logging.getLogger(__name__)
DATABASE_CHECK_SECONDS = 2


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the app without connecting to the database or running a solver."""
    try:
        resolved = settings if settings is not None else get_settings()
    except Exception:
        logger.error("Backend configuration unavailable")
        resolved = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.database_engine = None
        app.state.solver_proof = None
        if resolved is not None:
            try:
                # Reuse the verified-TLS path, including the explicit CA policy.
                app.state.database_engine = create_database_engine(resolved)
            except Exception:
                logger.error("Database configuration unavailable")
        try:
            app.state.solver_proof = await check_solver()
        except Exception:
            logger.error("Solver runtime proof unavailable")
        try:
            yield
        finally:
            if app.state.database_engine is not None:
                try:
                    await app.state.database_engine.dispose()
                except Exception:
                    logger.error("Database shutdown unavailable")

    app = FastAPI(lifespan=lifespan, debug=False)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=(
            [resolved.frontend_origin]
            if resolved and resolved.frontend_origin else []
        ),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=[],
    )

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        database_available = False
        engine = app.state.database_engine
        if engine is not None:
            try:
                async with asyncio.timeout(DATABASE_CHECK_SECONDS):
                    async with engine.connect() as connection:
                        database_available = (
                            await connection.scalar(text("SELECT 1"))
                        ) == 1
            except Exception:
                # Never expose or log driver exception text: it may contain secrets.
                logger.warning("Database readiness check unavailable")
        proof = app.state.solver_proof
        healthy = database_available and proof is not None
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ready" if healthy else "unavailable",
                "database": "available" if database_available else "unavailable",
                "solver": (
                    {
                        "status": proof.status,
                        "objective": proof.objective,
                        "selection": list(proof.selection),
                    }
                    if proof else {"status": "unavailable"}
                ),
            },
        )

    return app
