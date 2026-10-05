# FPL Optimizer — Project Context

Personalized Fantasy Premier League transfer/squad optimizer. The private full specification is kept locally in `docs/spec/` when available. It is intentionally not version-controlled and may be absent from a fresh clone. **When it is available, always check the relevant section before implementing a feature. If it is unavailable, do not invent missing requirements — ask the project owner for the relevant specification or context.**

The v1 redesign and its reasons are recorded in `docs/spec/AMENDMENTS.md`. The twelve HTML sections contain the implementation contract; keep them consistent with that record. A specification requirement is not proof that a feature has been built.

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
1. **Prediction Engine** (`services/prediction_engine.py`) — recency-weighted empirical scoring with explicit priors, producing component and fixture/event estimates. The first release has no opponent adjustment. See spec §04D.
2. **Optimization Engine** (`services/optimization_engine.py`) — a combined MILP for squad, starting XI, captain and transfers, with a required fewer-transfers tie-break preserving the primary score; bench/bank tie-breaks are deferred pending evidence. Vice and bench order use declared heuristics; expected autosubs and captain fallback are deferred. See spec §04E.

These are separate, deterministic, testable modules. The optimizer takes predicted points as input — it does not compute them. Neither engine makes HTTP calls or owns database reads/transactions; the application layer supplies explicit inputs.

## Product contract
- Import the latest public squad snapshot, show its provenance, then ask the manager to confirm current squad, bank, remaining free transfers, transfers already made and chip state.
- Selling prices are auto-calculated, pre-filled and editable. Preserve evidence and mark uncertain ownership/purchase histories; unresolved prices retain only the affected players in transfer solves.
- Target only the next unexpired gameweek. Keep lineup, ordinary transfers and the hypothetical budget simulator distinct. Unsupported chips must not receive purported chip-aware recommendations.
- “Optimal” means proven optimal for the stated model. Report timeouts and feasible unproven results honestly; validate returned solutions independently.

## Conventions
- Never hardcode secrets — all config via environment variables, `.env` never committed
- Pydantic validates every FPL API response at the boundary (schema drift is a real risk — FPL's API is unofficial and undocumented)
- Shared data is ingested directly into Postgres by a scheduled/manual command (§05), never refreshed by an optimize request; there is no `/refresh` route
- Store season-scoped identities and player-fixture history. Publish shared snapshots atomically and history atomically per player with durable resume checkpoints; apply the per-player coverage and shared-data freshness gates in §05
- Use one ingestion advisory lock (also respected by migrations) plus Actions concurrency; manager import calls remain separate from shared ingestion
- Error handling follows the severity model in spec §06 (block / warn / info) — don't invent new error-handling patterns ad hoc

## Current build phase
When the private specification is available locally, check `docs/spec/10-roadmap.html` for dependencies and `HANDOFF.md` for actual progress. If it is unavailable, ask the project owner for the relevant roadmap context before starting phase-dependent work. The optimizer can be built and exhaustively tested against synthetic inputs before the predictor is finished; integration requires both. Scheduled ingestion needs database credentials, not a deployed Render URL. Prove hosted database/TLS and CBC compatibility early. Opponent adjustment is the first experiment once the backtest harness exists; the baseline remains the first-release model.

## Working style
I'm learning as I build this — no prior professional dev background beyond a couple of internship/personal projects. Briefly explain what a tool/library does and why it's used when introducing something new, don't just produce code silently. Keep tasks scoped and focused per session rather than open-ended, to use tokens efficiently on the Pro plan.

## Note
Current tool roles: **Codex is primary** (college-provided access, tried first). **Antigravity** is used specifically for browser-based testing and long-running background tasks (e.g. backtesting scripts, bulk data population) — not general implementation. **Claude Code** is the fallback if Codex doesn't work out. `AGENTS.md` and `CLAUDE.md` intentionally contain the same project instructions and must be updated together.
