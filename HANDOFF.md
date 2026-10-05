# Handoff / Progress Log

This file tracks live build progress — what's actually done, not just what's planned. Read this before starting any task; update it before finishing one.

## Current Phase
Phase 1 — Data Layer (see docs/spec/10-roadmap.html)

The v1 specification redesign was written on 2026-10-05. See `docs/spec/AMENDMENTS.md` for the full decision register and revised sections 01–12 for the implementation contract. The implementation is still at the Phase 1A foundation; this documentation pass did not build the proposed features.

## This Phase — Task Checklist
- [x] Phase 1A backend/data-layer foundation created
- [x] Python 3.14.7 target and uv lockfile configured
- [x] Environment settings validate async PostgreSQL URLs
- [x] Async SQLAlchemy engine and session factories implemented
- [x] Alembic configured against the shared declarative metadata (no revisions yet)
- [x] Disposable PostgreSQL 17 test service configured with Docker Compose
- [x] Settings and live database-wiring smoke tests passing
- [x] v1 specification and synchronized agent guidance updated after audit/redesign
- [x] Spec follow-ups resolved: checkable GW1 assumption, two-hour final-window freshness, minimum objective stages, resumable history and Phase 1 slices
- [x] Phase 1A hardening: redacted database credentials, guarded test destinations and verified TLS for remote database hosts
- [ ] 1.1 Bootstrap slice: necessary models/migration + validated bootstrap → database → read-back and idempotency; development-only until fixtures complete the shared context
- [ ] 1.2 Shared-context slice: fixture model/migration + full catalog/fixtures atomic publication, advisory lock, deadline/fixture read-back, rollback and 6-hour/2-hour freshness checks
- [ ] 1.3 Single-player slice: history model/checkpoint metadata + element-summary → atomic player rows/checkpoint → totals; double-gameweek and failure checks
- [ ] 1.4 Resumable-population slice: durable required IDs/generations, per-player retry/backoff, bounded runs, interruption/restart, independent success, honest age/coverage and candidate/prior eligibility
- [ ] 1.5 Prior-season slice: pinned fixture archive → validated season identities/history → scoring reconciliation and compatible prior inputs
- [ ] 1.6 Manager-import slice: entry/history/transfers/latest picks/GW1 picks → typed snapshot and sale-price evidence; checkable assumed-held-since-GW1 path and failure cases
- [ ] Controlled real-FPL probes validate the same parsing/publication paths after saved-fixture tests pass
- [ ] Phase 1B: Supabase project and private database/TLS configuration, early hosted health/CBC proof, Actions ingestion that resumes across separate runs, and raw snapshot retention
- [ ] Phase 2: capture pre-deadline forecast bundles once the predictor exists

## Completed Phases
- Phase 0 — Repository initialization

## Deviations From Spec
Anything implemented differently than `docs/spec/` says goes here immediately. Small deviations stay logged here; anything significant enough to change an actual spec decision gets reflected back into the real spec file, not just noted here.

- No new implementation deviations were introduced by the documentation-only redesign. The proposed v1 design changed substantially; use the updated spec rather than the pre-amendment six-table plan, inline-refresh flow or old prediction/objective equations.

## Known Issues / Gotchas
Things the next session should know before touching this code.

- Node.js and npm are not currently available on this machine; they must be installed before the Next.js scaffold is created.
- The follow-up spec publishes history atomically per player with durable checkpoints; whole-sweep completion is diagnostic, not a global freshness gate. Required owned/locked histories still block if unusable; unrelated candidates are excluded with visible reasons.
- Automatic selling-price reconstruction was verified by the owner for one team's 15 players. Free Hit, repurchase, initial-entry timing and unpublished-transfer cases still require provenance, correction paths and tests.
- Historical evaluation uses a pinned fixture archive and an explicitly approximate cutoff. Exact past availability/deadline knowledge is not claimed. No prediction-quality, hosted performance or storage-capacity result is established yet.
- The Supabase project has not been created yet. Local Phase 1A work uses the disposable PostgreSQL test database; a Supabase connection is required before shared-development migrations or population validation.
- If Supabase requires a project-specific CA file, configure and verify it during Phase 1B; never work around certificate problems by disabling hostname or certificate verification.
- The host's Docker daemon socket is not accessible to the current user and `sudo` requires a password. The exact Compose definition was validated and exercised successfully through Docker Compose using rootless Podman's compatible socket. Normal `docker compose` usage requires fixing local Docker daemon permissions or continuing with the Podman-compatible provider.

## Phase 1A Verification
- `uv lock --check`: passed; lockfile resolves on Python 3.14.7
- PostgreSQL container: `postgres:17-alpine` started healthy (PostgreSQL 17.11)
- Podman-compatible start: run `podman system service --time=0 "unix://${XDG_RUNTIME_DIR}/podman/podman.sock"`, then `podman compose up -d --wait test-db`
- Pre-hardening `uv run --frozen pytest -q`: 6 passed
- Post-hardening `uv run --frozen pytest -q`: 14 passed
- Alembic `heads`: empty, as required before the model task
- Alembic `current`: connected successfully using the async PostgreSQL URL
- Alembic `check`: passed with "No new upgrade operations detected."

## Tool Log
Which tool did which piece of work — useful for knowing where to look first if something breaks.

- Codex — inspected the repository and specification, then completed the approved Phase 0 repository-initialization cleanup (`.gitignore`, `.env.example`, synchronized agent guidance, and current-state documentation).
- Codex — implemented and verified the approved Phase 1A backend/data-layer foundation (Python/uv packaging, settings, async SQLAlchemy, Alembic, disposable PostgreSQL, and pytest smoke tests).
- Codex — 2026-10-05: implemented the authorized documentation-only v1 redesign across all twelve specification pages; created `docs/spec/AMENDMENTS.md` with per-file changes, 32 judgment calls, defaults, evidence and open proof obligations; synchronized `AGENTS.md` / `CLAUDE.md`. Preserved the original page styling and the owner's read-only external backup. No application code, tests, dependencies, migrations, commits or Git write commands changed.
- Codex — 2026-10-05: hardened the Phase 1A database foundation with SecretStr redaction, a guarded `_test` destination, driver-native verified TLS for remote asyncpg connections and focused regression tests; reran the full suite and Alembic checks against disposable PostgreSQL.

## Specification Pass Verification — 2026-10-05
- All twelve HTML pages: balanced tags, unique IDs and valid local file/anchor links.
- Original CSS blocks are byte-for-byte unchanged; all twelve pages rendered in a local browser without horizontal overflow at its normal desktop viewport.
- `AGENTS.md` and `CLAUDE.md` are byte-for-byte identical.
- Hash comparison confirms the external read-only backup and all existing out-of-scope files (including application code, tests and configuration) were preserved.
- The requested `diff -rq` reports twelve changed HTML pages and only one new spec file, `AMENDMENTS.md`.
- Application tests were not rerun: this pass changed documentation only; previous Phase 1A test results above are historical results, not a fresh execution.

## Spec Follow-ups — 2026-10-05
- Codex — updated §§04/05/06/08/10, the amendments record and both identical agent files. GW1 imports now have explicit fallback conditions and assumption labels; near-deadline shared data remains usable for two hours with a warning after one; only primary/fewer-transfer optimization stages are initially required; history resumes from atomic per-player checkpoints.
- Replaced the broad Phase 1 checklist with six end-to-end slices matching §10. No implementation checkbox was marked complete, and no application code, tests, configuration, migrations or read-only backup files were changed.
