# Handoff / Progress Log

This file tracks live build progress — what's actually done, not just what's planned. Read this before starting any task; update it before finishing one.

## Current Phase
Phase 1 — Data Layer (see docs/spec/10-roadmap.html)

The v1 specification redesign was written on 2026-10-05. See `docs/spec/AMENDMENTS.md` for the full decision register and revised sections 01–12 for the implementation contract. Phase 1 slices 1.1, 1.2a and 1.2b are implemented against the disposable PostgreSQL database. Bootstrap and fixtures now form one atomic shared publication guarded by a dedicated session advisory lock, with deterministic shared-data freshness/age checks. HTTP ingestion, history and hosted proof remain pending.

## This Phase — Task Checklist
- [x] Phase 1A backend/data-layer foundation created
- [x] Python 3.14.7 target and uv lockfile configured
- [x] Environment settings validate async PostgreSQL URLs
- [x] Async SQLAlchemy engine and session factories implemented
- [x] Alembic configured against shared declarative metadata; bootstrap catalog revision applied
- [x] Disposable PostgreSQL 17 test service configured with Docker Compose
- [x] Settings and live database-wiring smoke tests passing
- [x] v1 specification and synchronized agent guidance updated after audit/redesign
- [x] Spec follow-ups resolved: checkable GW1 assumption, two-hour final-window freshness, minimum objective stages, resumable history and Phase 1 slices
- [x] Specification presentation refreshed with a shared light, responsive and accessible visual theme; contract text unchanged
- [x] Phase 1A hardening: redacted database credentials, guarded test destinations and verified TLS for remote database hosts
- [x] 1.1 Bootstrap slice: necessary models/migration + validated bootstrap → database → read-back and idempotency; development-only until fixtures complete the shared context
- [x] 1.2a Shared-publication slice: fixture model/migration + full catalog/fixtures atomic publication, deadline/per-team fixture-count read-back, rollback and idempotent replay
- [x] 1.2b Shared-ingestion controls: advisory lock plus 6-hour/2-hour freshness and age-warning checks
- [ ] Before HTTP ingestion: validate fixture-list completeness before treating an empty target schedule as a blank (§05A)
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
- Slice 1.1 has no known implementation deviation. Because `bootstrap-static` does not carry the project's season namespace, rules version or source observation time, the storage service requires those values as explicit validated publication context rather than inferring them from an event number or local clock.
- Slice 1.2a has no known implementation deviation. The fixtures boundary rejects empty collections, duplicate fixture IDs, unknown team/event references and bootstrap clubs absent from the full-season fixture collection. This is structural completeness validation; transport truncation must still be rejected by the future HTTP ingestion layer before constructing the envelope.
- Slice 1.2b has no known implementation deviation within its scope. Freshness uses the successful publication's `source_observed_at` (the §05B note requires actual source age), while separately reporting age since `last_success_at`. The final window includes exactly 24 hours before a strictly future deadline; expired deadlines and impossible/naive timestamps are rejected rather than assuming a normal-window limit. §04B's material context digest remains separate future application work, not the catalog publication counter.

## Known Issues / Gotchas
Things the next session should know before touching this code.

- Node.js and npm are not currently available on this machine; they must be installed before the Next.js scaffold is created.
- The follow-up spec publishes history atomically per player with durable checkpoints; whole-sweep completion is diagnostic, not a global freshness gate. Required owned/locked histories still block if unusable; unrelated candidates are excluded with visible reasons.
- Automatic selling-price reconstruction was verified by the owner for one team's 15 players. Free Hit, repurchase, initial-entry timing and unpublished-transfer cases still require provenance, correction paths and tests.
- Historical evaluation uses a pinned fixture archive and an explicitly approximate cutoff. Exact past availability/deadline knowledge is not claimed. No prediction-quality, hosted performance or storage-capacity result is established yet.
- The Supabase project has not been created yet. Local Phase 1A work uses the disposable PostgreSQL test database; a Supabase connection is required before shared-development migrations or population validation.
- If Supabase requires a project-specific CA file, configure and verify it during Phase 1B; never work around certificate problems by disabling hostname or certificate verification.
- The host's Docker daemon socket is not accessible to the current user and `sudo` requires a password. The exact Compose definition was validated and exercised successfully through Docker Compose using rootless Podman's compatible socket. Normal `docker compose` usage requires fixing local Docker daemon permissions or continuing with the Podman-compatible provider.
- Shared publication now accepts a session factory and owns commit/rollback, rather than accepting a caller-owned session. Configure `SESSION_DATABASE_URL` explicitly with a direct/session endpoint to the same database as the write connection. The dedicated connection uses `NullPool`; this does not make an external transaction pooler safe. There is no automatic fallback to `DATABASE_URL`.
- Publication now verifies the actual lock/write database destinations after acquiring the ingestion lock and before calling the writer. `ingestion_lock` yields its live connection (or `None` on contention); future command/migration integration must call `require_same_database` with that connection and the actual write connection before writing. The verification uses a fresh temporary advisory marker observed through PostgreSQL's lock view, not URL equality or privileged control-file access.
- Future ingestion commands must hold `db.advisory_lock.ingestion_lock` around the entire run, including fetches and history work, then use short write transactions. Migrations must later acquire the same stable `INGESTION_LOCK_KEY` and stop on contention; this slice provides the reusable helper but does not wire it into Alembic yet. Do not nest the self-locking publication wrapper inside an already-held lock.

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

- Codex — 2026-10-06: owner authorized committing all slice 1.2b changes, including the same-database follow-up, with message `Implement shared catalog locking and freshness gates`. Existing verification remains 76 passing tests and a clean Alembic check; no push authorized.
- Codex — inspected the repository and specification, then completed the approved Phase 0 repository-initialization cleanup (`.gitignore`, `.env.example`, synchronized agent guidance, and current-state documentation).
- Codex — implemented and verified the approved Phase 1A backend/data-layer foundation (Python/uv packaging, settings, async SQLAlchemy, Alembic, disposable PostgreSQL, and pytest smoke tests).
- Codex — 2026-10-05: implemented the authorized documentation-only v1 redesign across all twelve specification pages; created `docs/spec/AMENDMENTS.md` with per-file changes, 32 judgment calls, defaults, evidence and open proof obligations; synchronized `AGENTS.md` / `CLAUDE.md`. Preserved the original page styling and the owner's read-only external backup. No application code, tests, dependencies, migrations, commits or Git write commands changed.
- Codex — 2026-10-05: hardened the Phase 1A database foundation with SecretStr redaction, a guarded `_test` destination, driver-native verified TLS for remote asyncpg connections and focused regression tests; reran the full suite and Alembic checks against disposable PostgreSQL.
- Codex — 2026-10-05: added `docs/spec/theme-light.css` and applied it to all twelve HTML specification pages. The shared override introduces a light palette, clearer contrast and spacing, subtle card/table depth, readable status colors, responsive navigation and reduced-motion support. After the owner's local viewer continued showing the old theme, the same rules were embedded as an inline fallback in every page so each HTML file renders correctly by itself. HTML contract content, `AMENDMENTS.md`, application code and configuration were not changed.
- Codex — 2026-10-06: implemented Phase 1 slice 1.1 with season-scoped SQLAlchemy models, one Alembic revision, strict Pydantic parsing for consumed `bootstrap-static` fields, replay-safe PostgreSQL upserts, deterministic read-back, a trimmed non-manager fixture captured from the public endpoint, and disposable-database migration/round-trip/idempotency tests.
- Codex — 2026-10-06: implemented Phase 1 slice 1.2a with the fixtures model and migration, strict `/api/fixtures/` parsing, cross-endpoint validation, one atomic bootstrap/fixtures publication, target-deadline and per-team fixture-count read-back, and PostgreSQL rollback/replay tests. Captured and trimmed the public fixtures response; no manager data, HTTP application calls, advisory lock, freshness gates or player history were added.
- Codex — 2026-10-06: implemented Phase 1 slice 1.2b with a reusable PostgreSQL session advisory lock on a dedicated unpooled connection, publication-owned commit/rollback, explicit session-endpoint configuration, source-age freshness checks and database-backed contention/failure/cancellation/boundary tests. Updated README and environment examples; no HTTP, history, scheduling, schema changes, commits or pushes.
- Codex — 2026-10-06: closed the owner's follow-up same-database validation gap. Added a server-backed check of the actual lock/write connections before publication writes, a credential-free mismatch error, and real-database match/mismatch tests. Changed only the lock helper, its publication call site, controls tests and this handoff; no commit or push.

## Shared Controls Same-Database Follow-up — 2026-10-06
- The earlier same-database requirement was documentation-only. It is now enforced after the ingestion lock is acquired and before `_write_shared_catalog` runs, on the exact connection used by the write transaction.
- Method: take a fresh random two-integer advisory marker on the lock connection, then require PostgreSQL's `pg_locks` view on the writer to report that marker as granted to the lock connection's backend PID in the writer's current database OID. The random marker avoids treating coincident names/OIDs/PIDs on separate servers as identity. Its key namespace is separate from the stable bigint ingestion lock; no persistent data is written, and the marker is released in `finally`.
- Different direct/session-pooler hostnames are allowed. The check relies on live server state and ordinary PostgreSQL lock visibility, not URL strings or superuser-only control-file functions. This is compatible with session pooling; a hosted Supabase run remains part of the pending Phase 1B proof.
- Match test: actual connections using `localhost` and `127.0.0.1` to the same disposable database publish successfully. Mismatch test: the lock connects to a separately created `_test` database while the actual session factory writes to the original guarded database. Even identical configured URL values cannot bypass the actual-connection check. Publication is refused with a fixed credential-free error, the writer is never called, all six publication-table row counts are unchanged, and no advisory locks remain on the mismatched database. Lock reacquisition succeeds; the temporary database is removed during fixture cleanup.
- Targeted match/mismatch tests: **2 passed**. Full `UV_CACHE_DIR=/tmp/fpl-uv-cache uv run --frozen pytest -q --tb=short`: **76 passed**. `alembic check` against the same guarded disposable database: **No new upgrade operations detected**. `git diff --check`: passed.
- Freshness behavior, settings/TLS handling, schema/migrations and all other slice behavior remain unchanged. No specification deviation or unresolved requirement introduced.

## Shared Controls Slice 1.2b Verification — 2026-10-06
- `UV_CACHE_DIR=/tmp/fpl-uv-cache uv run --frozen pytest -q --tb=short`: **74 passed**, including the full existing suite and 42 new controls/freshness cases against the guarded disposable database where applicable.
- `alembic check` against that same guarded `_test` database: passed with “No new upgrade operations detected.” No model or migration changes were needed.
- The competing publisher logs “already running”, returns `None` and never opens a write session. A paused first publisher remains uncommitted until released; database inspection proves lock ownership is on a different PostgreSQL backend from the write transaction. The dedicated backend closes on exit.
- Failure after metadata writes and task cancellation both roll back the publication, preserve source/success timestamps and version, and allow a subsequent publication to succeed. A standalone lock context also survives a separate write transaction and releases after an exception, supporting later migration reuse.
- Database-backed freshness checks cover exactly 1h, 2h and 6h and one microsecond beyond each, at deadlines 1h, exactly 24h, 24h plus one microsecond and 25h away. Limits are inclusive; warnings begin strictly after 1h in the final window. No trusted publication returns `unknown`; old-source replay stays stale despite a later successful replay timestamp. A daylight-saving regression confirms elapsed-time arithmetic.
- The gate reports source age, success age, active limit, final-window status and warning separately. Failed-attempt timestamps are not inputs. No lineup-specific allowance or material context digest was implemented.
- Verification setup: the sandbox initially blocked database sockets; approved local-database execution then found the existing disposable Podman container stopped. Started `fpl-optimizer-test-db-1` and reran successfully. The alternate uv cache path avoids the host cache's read-only sandbox restriction.
- `git diff --check`: passed. AGENTS.md and CLAUDE.md remain unchanged and identical. No unresolved specification question was found; boundary/source-age choices are recorded above.

## Shared Publication Slice 1.2a Verification — 2026-10-06
- The saved fixtures sample contains one finished and one future public 2026–27 match and references only the three clubs retained in the bootstrap sample. Unconsumed match-stat detail is omitted; no manager data is present.
- Fixture event and kickoff remain nullable and timezone-aware when present; per-side FDR, scores, status flags, season-scoped team/event references and first-observed gameweek finalization evidence are stored.
- The read path selects the earliest deadline strictly after the supplied aware timestamp and returns a count for every catalog club. Tests cover one-fixture teams, a zero-fixture blank and a synthetic double without collapsing fixture rows.
- Bootstrap and fixtures are accepted only as one cross-validated envelope. A database-backed injected fixture-write failure proved earlier catalog rows, fixture rows, publication version and success timestamp remain unchanged; exact replay keeps its version and row counts.
- `uv run --frozen pytest -q`: 32 passed, including migration from empty, strict boundary/cross-endpoint validation, atomic rollback, exact replay, target read-back, blanks and doubles.
- `alembic check`: passed with “No new upgrade operations detected.”
- Deliberate choices: the existing explicit shared `source_observed_at` remains the provenance/version key for the paired fetch; the first `gameweek_data_checked_at` is preserved while a fixture remains assigned to that event and resets if the source moves it to another event; target means the earliest official deadline greater than `as_of`, not the upstream `is_next` flag.

## Bootstrap Slice 1.1 Verification — 2026-10-06
- The saved fixture contains one club, two players and two gameweeks from the public 2026–27 `bootstrap-static` response. It retains representative nulls and numeric strings and contains no manager data.
- The same boundary model also parsed the complete captured response: 20 teams, 667 players and 38 gameweeks.
- A session-scoped database fixture dropped and recreated the guarded `_test` schema, then Alembic upgraded it from empty before the integration checks.
- `uv run --frozen pytest -q`: 22 passed, including units/null validation, required eligibility rejection, season-scoped primary keys/team references, migration, database round trip, exact-replay counts across all five tables and a changed-price upsert without duplication.
- `alembic check`: passed with “No new upgrade operations detected.”
- `uv lock --check --offline`, Python byte-compilation and `git diff --check`: passed.
- Deliberate choices: unrelated top-level/source fields are ignored while every consumed field is strictly validated; decimal strings must arrive as strings; an exact replay with the same source observation time keeps its publication version; only player rows carry row-level observation/version fields because §04C explicitly assigns those publication fields to players, while the shared cache record versions the catalog as a whole.

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

## Specification Visual Refresh — 2026-10-05
- Each of the twelve HTML pages links the same local `theme-light.css` and contains an identical inline fallback for viewers that do not load local linked stylesheets; no runtime or network dependency was added by the new theme.
- Hashes of every page with style blocks and the new stylesheet link removed match the pre-refresh hashes, confirming that specification text and document structure were preserved.
- The shared stylesheet has balanced rule braces, all twelve links resolve to the local file, and `git diff --check` reports no whitespace errors.
- Browser UI preview was unavailable because the browser security policy blocks local `file:` pages. Static structure, responsive rules and contrast-sensitive overrides were checked directly; application tests were not rerun because only documentation presentation changed.
