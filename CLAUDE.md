# FPL Optimizer — Project Context

Personalized Fantasy Premier League transfer/squad optimizer. The private full specification is kept locally in `docs/spec/` when available. It is intentionally not version-controlled and may be absent from a fresh clone. **When it is available, always check the relevant section before implementing a feature. If it is unavailable, do not invent missing requirements — ask the project owner for the relevant specification or context.**

**Check `HANDOFF.md` for current progress before starting any task.** It tracks what's actually done (not just planned), known issues, and deviations from the spec. Update it before finishing a task — check off what's done, note anything unexpected, log which tool did the work.

## Stack
- **Frontend:** Next.js (React, TypeScript) → deploys to Vercel
- **Backend:** FastAPI (Python) → deploys to Render
- **Optimization:** PuLP (MILP/0-1 integer programming)
- **Database:** PostgreSQL via Supabase
- **ORM:** SQLAlchemy (async) + Alembic for migrations
- **Comms:** REST/JSON over HTTP, CORS restricted to the frontend domain
- **Testing:** pytest (backend), Vitest + React Testing Library (frontend, targeted tests only — see spec §08F)

## Architecture at a glance
Two independently deployed services, not a monolith. The backend owns two distinct engines:
1. **Prediction Engine** (`services/prediction_engine.py`) — weighted formula producing `predicted_points` per player per fixture. See spec §04D.
2. **Optimization Engine** (`services/optimization_engine.py`) — the combined LP that solves squad + starting XI + captain + transfers in one pass. See spec §04E.

These are separate, testable modules. The LP takes predicted points as input — it does not compute them.

## Conventions
- Never hardcode secrets — all config via environment variables, `.env` never committed
- Pydantic validates every FPL API response at the boundary (schema drift is a real risk — FPL's API is unofficial and undocumented)
- Global player/fixture data is cached in Postgres, refreshed on a schedule (§05) — never fetched fresh per user request
- Error handling follows the severity model in spec §06 (block / warn / info) — don't invent new error-handling patterns ad hoc

## Current build phase
When the private specification is available locally, check `docs/spec/10-roadmap.html` for the current phase and what depends on what. If it is unavailable, ask the project owner for the relevant roadmap context before starting phase-dependent work. Notably: **Prediction Engine (Phase 2) must be built before Optimization Engine (Phase 3)** — the LP needs real predicted points to test against, not placeholders. And **the backend must be deployed to Render before the GitHub Actions cron secret can be configured** (Phase 5) — that dependency can't be worked around.

## Working style
I'm learning as I build this — no prior professional dev background beyond a couple of internship/personal projects. Briefly explain what a tool/library does and why it's used when introducing something new, don't just produce code silently. Keep tasks scoped and focused per session rather than open-ended, to use tokens efficiently on the Pro plan.

## Note
Current tool roles: **Codex is primary** (college-provided access, tried first). **Antigravity** is used specifically for browser-based testing and long-running background tasks (e.g. backtesting scripts, bulk data population) — not general implementation. **Claude Code** is the fallback if Codex doesn't work out. `AGENTS.md` and `CLAUDE.md` intentionally contain the same project instructions and must be updated together.
