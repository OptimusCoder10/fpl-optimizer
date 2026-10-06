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

Phase 1 slices 1.1–1.2b provide the season-scoped catalog and fixtures, atomic
publication, a shared advisory lock, and deterministic freshness checks.

Shared publication also requires `SESSION_DATABASE_URL`, using the same
`postgresql+asyncpg://` format and targeting the **same database** through a direct
or session-mode endpoint. Never use a transaction-pooling endpoint for this URL.
The lock opens its own connection with SQLAlchemy's `NullPool` (no local connection
reuse), separately from the write transaction, and closes it after releasing the
lock. Both connection paths retain the existing remote TLS verification.

`publish_shared_catalog` takes a session factory and validated inputs, owns the
commit/rollback, and returns the publication version or `None` after logging
“already running”. The reusable `ingestion_lock` context uses one stable key for
the database; future ingestion commands must hold it around their entire run,
and future migration integration must use the same helper and stop on contention.
Alembic does not acquire this lock yet.

`read_shared_freshness` evaluates the last committed shared publication with
explicit timezone-aware `now` and a strictly future `next_deadline`. It returns
`current`, `stale`, or `unknown`, source/success ages, the applicable limit and an
age-warning flag. Source age is usable through six hours normally, or two hours
starting exactly 24 hours before the deadline; the final-window warning starts
strictly after one hour. A failed attempt or later replay of old source data
cannot make its source age fresh. Publication versions remain provenance, not
the future material-context digest used for manager reconfirmation.

HTTP ingestion, scheduling, player history, lineup-specific age allowances and
the context API are still pending.

## Specification

The detailed project specification is maintained privately in the local `docs/spec/` directory when available. It is intentionally excluded from the public repository.

## License

TBD
