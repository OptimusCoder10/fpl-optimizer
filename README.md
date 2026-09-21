# FPL Optimizer

A personalized Fantasy Premier League transfer/squad optimizer. Connects to your real FPL team via Team ID and recommends the mathematically optimal transfers, starting XI, and captaincy pick each gameweek — built on a custom prediction formula and a linear-programming optimizer.

> 🚧 Phase 0 repository setup is in progress. See [`HANDOFF.md`](./HANDOFF.md) for current progress.

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
backend/      FastAPI app — prediction engine, optimization engine, API (not scaffolded yet)
docs/spec/    Private local project specification (not version-controlled; may be absent)
```

## Getting started

The application has not been scaffolded yet, so there are no runtime setup instructions. Implementation begins with Phase 1 after the Phase 0 checklist in [`HANDOFF.md`](./HANDOFF.md) is complete.

## Specification

The detailed project specification is maintained privately in the local `docs/spec/` directory when available. It is intentionally excluded from the public repository.

## License

TBD
