# FPL Optimizer

A personalized Fantasy Premier League transfer/squad optimizer. Connects to your real FPL team via Team ID and recommends the mathematically optimal transfers, starting XI, and captaincy pick each gameweek — built on a custom prediction formula and a linear-programming optimizer.

> 🚧 Phase 1 data-layer work is in progress. The Phase 1A backend foundation is complete; see [`HANDOFF.md`](./HANDOFF.md) for current progress.

## Planned stack

| Layer | Technology |
|---|---|
| Frontend | Next.js (React, TypeScript) |
| Backend | FastAPI (Python) |
| Optimization | PuLP (MILP) |
| Database | PostgreSQL (Supabase) |
| ORM | SQLAlchemy (async) + Alembic |

## Planned project structure

```
frontend/     Next.js app (not scaffolded yet)
backend/      Python package, async database wiring, Alembic, and tests
docs/spec/    Private local project specification (not version-controlled; may be absent)
compose.yaml  Disposable PostgreSQL 17 test database
```

## Backend foundation

Prerequisites: Python 3.14.7, [uv](https://docs.astral.sh/uv/), and Docker Compose.

```bash
docker compose up -d --wait test-db
cd backend
uv sync --frozen --all-groups
uv run --frozen pytest
```

The test suite connects only to the dedicated `fpl_optimizer_test` database exposed on local port `5433`. Override its URL with `TEST_DATABASE_URL` when needed. Stop and remove the disposable database with `docker compose down` from the repository root.

Application database settings come from `DATABASE_URL` in the untracked repository-root `.env`. The backend requires an async SQLAlchemy URL beginning with `postgresql+asyncpg://`.

Phase 1 slice 1.1 adds the season-scoped bootstrap catalog tables and the first
Alembic revision. Fixture completeness, shared-snapshot freshness, and production
publication remain intentionally deferred to slice 1.2.

## Specification

The detailed project specification is maintained privately in the local `docs/spec/` directory when available. It is intentionally excluded from the public repository.

## License

TBD
