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

If the provider requires an additional trusted CA, set `DATABASE_SSL_CA_FILE`
to the absolute path of its PEM CA certificate; an empty value is treated as unset.
Both remote connection paths
load it alongside the system trust store, preserving certificate and hostname
verification. An unreadable or invalid CA file stops engine creation; verification
is never disabled as a fallback. Local disposable connections do not use this file.
Only contexts with this explicit CA setting clear `VERIFY_X509_STRICT`, because
the Supabase 2021 intermediate CA omits Key Usage. Chain/signature and hostname
verification remain enabled; all other default verification flags are preserved.

`publish_shared_catalog` takes a session factory and validated inputs, owns the
commit/rollback, and returns the publication version or `None` after logging
“already running”. The reusable `ingestion_lock` context uses one stable key for
the database; future ingestion commands must hold it around their entire run,
and online Alembic commands use the same lock and stop on contention. Both paths
verify the actual lock and write connections reach the same database before writing.

`read_shared_freshness` evaluates the last committed shared publication with
explicit timezone-aware `now` and a strictly future `next_deadline`. It returns
`current`, `stale`, or `unknown`, source/success ages, the applicable limit and an
age-warning flag. Source age is usable through six hours normally, or two hours
starting exactly 24 hours before the deadline; the final-window warning starts
strictly after one hour. A failed attempt or later replay of old source data
cannot make its source age fresh. Publication versions remain provenance, not
the future material-context digest used for manager reconfirmation.

HTTP ingestion, Actions scheduling, broader history generation/eligibility gates,
lineup-specific age allowances and the context API are still pending.

## Minimal backend and Render settings (Phase 1B step 2)

FastAPI provides the health routes; Uvicorn serves the app with one worker.
Run locally from `backend/`, after installing the locked dependencies:

```bash
.venv/bin/uvicorn fpl_optimizer.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
.venv/bin/python -m fpl_optimizer.services.solver_smoke
```

- `GET /health/live`: process response only, HTTP 200.
- `GET /health/ready`: a read-only `SELECT 1` through the existing verified-TLS
  engine (two-second check limit), plus the startup CBC proof. HTTP 200 when both
  succeed, otherwise HTTP 503 with fixed safe statuses. Driver exceptions and
  configuration values are never returned or logged by these checks.
- CBC runs once at startup in a separate process, with a two-second CBC limit
  and a five-second outer wall limit. Timeout/cancellation kills the process group,
  including CBC. Readiness polls reuse that result instead of starting more solves.
  Restart the service to retry a failed startup proof. CBC uses its default serial
  mode: explicitly requesting an internal thread produced intermittent local
  unproven/over-budget results; serial mode passed 20 repeated bounded proofs.
- The proof maximizes `6x + 4y + 3z`, subject to `4x + 3y + 2z <= 5`, with binary
  variables. It requires both PuLP's optimal problem and optimal solution statuses,
  then independently checks the unique answer `(0, 1, 1)`, objective **7**.
- Readiness establishes runtime connectivity and solver execution, not populated
  FPL data, prediction availability or the future context API's freshness gates.
- `FRONTEND_ORIGIN` permits exactly one HTTP(S) origin without a trailing slash.
  Empty means no cross-origin access. The current routes permit only GET; requests
  without an Origin header still work for health probes.

These are **documented dashboard settings**, not an existing Render deployment:

| Setting | Value |
|---|---|
| Service type / runtime | Web Service / Python 3 |
| Root directory | `backend` |
| Python version | **3.14.7**, explicitly selected with `PYTHON_VERSION` |
| uv version | **0.12.19**, explicitly selected with `UV_VERSION` |
| Build command | `uv sync --frozen --no-dev --no-python-downloads && .venv/bin/python -m fpl_optimizer.services.solver_smoke` |
| Start command | `.venv/bin/uvicorn fpl_optimizer.main:create_app --factory --host 0.0.0.0 --port $PORT --workers 1` |
| Health check path | `/health/ready` |
| Automatic deploys | Disabled for this manual proof; CI-gated releases remain future work |
| Plan / region | Owner selects when provisioning; not chosen or verified here |

Environment variable **names only**: `PYTHON_VERSION`, `UV_VERSION`, `DATABASE_URL`,
`DATABASE_SSL_CA_FILE`, `FRONTEND_ORIGIN`. Render supplies `PORT`.
`SESSION_DATABASE_URL` is additionally required for separate online Alembic and
ingestion commands; the health app itself neither migrates nor ingests. Never put
`TEST_DATABASE_URL` on Render. Supply actual database/origin values privately in the
dashboard. Do not run migrations in this build/start command.

[Render's current Python documentation](https://render.com/docs/python-version)
says any released version from 3.7.3 onward is supported, and
[Python 3.14.7 was released on August 5, 2026](https://www.python.org/downloads/release/python-3147/).
Thus the exact pin is supported according to documentation; actual hosted execution
is still unproved. The repository pin remains unchanged. If Render rejects it,
stop and report the build result: the owner must choose a supported replacement
pin (a separate runtime-change task) or a Docker deploy that supplies 3.14.7.
[Render health checks](https://render.com/docs/health-checks) require a successful
HTTP response within five seconds; using readiness also means a database outage
can cause Render to stop traffic and eventually restart the service.

**CBC installation is verified for this package, not assumed.** `uv.lock` pins
**PuLP 3.3.0** and its wheel checksum. The installed wheel actually contains CBC
executables for Linux `i64`, `i32` and `arm64`; `PULP_CBC_CMD` selects the package's
binary without relying on a system `cbc`. The inspected Linux `i64` executable
reports **CBC 2.10.3**, SHA-256
`2e17077752aa52b06385ad248c9e90bb4f1ce34038c34c94e1012ca6adea5cc7`.
Its ordinary Linux runtime library dependencies resolve locally, and the actual
solve passes locally. Installing the frozen lock therefore supplies CBC; no apt
install or root permission is planned. The build proof fails if Render's architecture,
libraries or executable permissions are incompatible; startup checks again in the
runtime environment. This packaging claim is specific to
[PuLP 3.3.0](https://pypi.org/project/PuLP/3.3.0/); reevaluate it before upgrading.

**Proposed CA delivery:** paste the project's public Supabase CA PEM into a
[Render secret file](https://render.com/docs/configure-environment-variables#secret-files)
and privately set `DATABASE_SSL_CA_FILE` to its `/etc/secrets/<filename>` path.
This uses the existing file setting without a code change; no inline PEM environment
setting was added. Both remote engines retain `CERT_REQUIRED`, hostname verification
and the already-authorized explicit-CA strict-bit compatibility policy described
above. A missing/invalid file makes readiness fail safely. Never substitute an
unverified connection. Keep the actual file/path and connection values out of Git.

After the owner deploys: check both health paths, confirm readiness shows database
available and solver `optimal`, objective 7, selection `[0, 1, 1]`; verify CORS from
the intended frontend and another origin. Record Render's actual Python/CBC versions
and cold/warm behavior. No hosted application, TLS/CBC execution, provisioning or
deployment outcome is claimed by these local checks.

## Pinned prior-season archive (slice 1.5)

The offline data-layer API imports the four local files from commit
`9779cdbc0c07f6c900c2d0c181ddf6bb9c800f88`, season `2025-26`:
`gws/merged_gw.csv`, `players_raw.csv`, `teams.csv` and `fixtures.csv`.
Place them in one directory, using the basename `merged_gw.csv` for the history.
No application HTTP fetch is involved.

- `schemas.prior_archive.load_pinned_archive(Path(directory))` verifies every
  SHA-256 checksum (a fingerprint of the file contents), validates identities and
  fixture assignments, reconciles scoring, and asserts the pinned audit counts.
- `services.prior_archive.publish_prior_archive(factory, archive, publication,
  settings=settings)` uses a `SharedPublication` with season `2025-26`, rules
  `fpl-2025-v1`, and aware collection/publication timestamps. It holds the existing
  ingestion lock, publishes the catalog atomically, and commits history and
  source evidence atomically per player. Repeating or restarting the same import
  skips committed players. Different source evidence cannot replace that archive.
- `services.prior_archive.read_prior_inputs(factory, "2026-27", player_id)` returns
  separate personal and same-position fixture pools with coverage reasons and
  provenance, from one consistent database view. Personal identity requires a
  non-temporary external code unique in both seasons and an unchanged position.
  Compatibility explicitly allows the existing `fpl-2025-v1`/`fpl-2026-v1`
  count-based adapter; unknown rules are excluded. Awarded bonus remains the
  specification's proxy. No current-data age limit applies to this pinned source.

Only exact whole-row duplicates are removed. `ArchiveConflictError` exposes the
conflicting keys, audit and rejected raw row variants; its
`write_quarantine(Path(path))` method saves a JSON report outside the database.
Conflicts stop validation before publication. Missing required scoring counts
fail; optional xG/ICT/starts gaps remain null. Catalog players without history are
reported as gaps rather than receiving manufactured zero rows.

Official archived deadlines and event `data_checked` evidence are unavailable;
deadlines stay null and live finalization timestamps are not invented. Historical
club comes from fixture sides; historical position comes from the pinned season's
validated player metadata. Prices and diagnostics may remain in existing storage,
but the prior reader exposes only historical context and complete scoring counts,
excluding archive xP, prices and ownership. It computes no predictions or means.

Reduced saved samples are labelled `sample` and require explicit
`allow_sample=True` on publication/read-back for testing. The production defaults
require the full pin. See `backend/tests/fixtures/prior_archive/README.md` for the
small saved double/duplicate/conflict sample. Full source files remain outside the
repository; rerunning the full test suite rebuilds the disposable database.

## Specification

The detailed project specification is maintained privately in the local `docs/spec/` directory when available. It is intentionally excluded from the public repository.

## License

TBD

### Manual FPL ingestion (Task 4a)

The ingestion command uses the existing verified-TLS database settings and requires
`SESSION_DATABASE_URL` to address the same database through a direct/session endpoint.
Set `FPL_SEASON_ID` and `FPL_RULES_VERSION` explicitly in the environment. The season
must have a reviewed entry in `SEASON_CLUB_COUNTS` (currently `2026-27`); fixture
completeness requires every ordered pair of its clubs exactly once. Postponed
fixtures with null event/kickoff count toward that total.

From `backend`, run:

```bash
uv run --frozen python -m fpl_optimizer.ingest
```

This command makes real public FPL requests when deliberately invoked. Task 4a was
verified using fake HTTP responses only. It holds the ingestion advisory lock
through fetching and all publication work; a contender logs `already running`
and exits without writes. Bootstrap and fixtures publish together when due
(every four hours, or thirty minutes in the final day before the next deadline).
Pending/due player histories resume even when no shared refresh is due.

`FPL_RUN_BUDGET_SECONDS` defaults to 660; `FPL_HISTORY_BUDGET_SECONDS` defaults to
600 and is capped by the remaining run budget. Requests have finite connection,
read and total timeouts. Player requests are sequential, with at least 200ms
between attempts. Transient network failures, HTTP 429 and 5xx receive at most
three attempts with backoff/jitter and Retry-After support. An excessive wait
ends the run cleanly; committed player checkpoints survive for the next run.

Logs contain fixed categories and counts, without upstream bodies or credentials.
Invalid/incomplete shared responses preserve the last good catalog and success
time; failed attempts are recorded separately on an existing shared checkpoint.
Before the first trusted publication, failures are logged without creating a
season or success checkpoint. Exit code 1 means failure or partial player failure;
lock contention and a clean budget stop exit 0. Scheduling and archives are still
separate work. The existing runner's daily due times remain in use; sweep generations
and broader coverage eligibility remain deferred as recorded in `HANDOFF.md`.

Manager HTTP imports use `fetch_manager_import` with captured catalog deadlines
and a client bounded to the caller's import budget. Only successful, fully read,
validated responses become complete evidence. A failed/truncated transfers fetch
never becomes an empty list; latest/GW1 picks are reused when they are the same event.
