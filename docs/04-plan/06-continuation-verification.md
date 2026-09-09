# Claude continuation — verification and remaining work

**Later continuation:** the 2026-09-07 [chunking increment](08-chunking-implementation.md)
adds fixed/recursive/Markdown/parent-child chunking and a real PostgreSQL catalog
round-trip regression. It also fixes dropped chunk metadata in ORM bulk writes.
The worker pipeline, PDF quality evaluation and Office/OCR gates remain open;
the results below describe the earlier verification session.

**Date:** 2026-09-07 · **Session:** `9d9ddebd-3ba8-4607-8976-b90680a04ffb`.

The supplied identifier had an extra trailing `1`. The matching local JSONL
record was under the former `D--code-ZFKS` project. Its last successful response
reported Phase 2b implemented, but integration tests unexecuted; the next
`continue` failed with HTTP 429. Work is now in `D:\code\Cairn`.

## Delivered in this continuation

- M07 parser foundations and TXT/Markdown/JSON/HTML adapters, with 42 tests.
  See [scope and PDF gate](05-parser-spike.md). No worker wiring is claimed.
- Declared the synchronous `psycopg2-binary` dependency used by Alembic/CLI
  migrations. Previously a fresh environment failed before any test could run.
- Aligned pytest tests and async fixtures on a session event loop. The old
  fixture-only setting reused asyncpg connections across different event loops.
- Fixed task claiming: qualified `UPDATE ... FROM ... RETURNING` columns,
  sorted returned tasks by priority/FIFO, limited each workspace's candidates by
  remaining capacity, and retained access to unlocked candidates beyond a locked
  batch prefix. Added a regression test for that last case.
- Corrected test isolation (`task` has no workspace FK and needs explicit
  TRUNCATE), a wrong chunk-config field name, missing logout CSRF header, an
  async-generator timing expression and an invalidation monkeypatch targeting
  the wrong imported function. No auth/CSRF protection was weakened.
- Corrected pgvector contract fixtures to include the document identity required
  by the existing schema, using the suite's existing `point()` factory rather
  than relaxing the production NOT NULL constraint.
- Fixed randomly generated user passwords occasionally violating the password
  policy: generated candidates now pass the same length/class/username checks
  as explicit credentials, with bounded retries and deterministic regression tests.
- Added request-replay, operational-entrypoint and audit-maintenance tests. Fixed
  CI coverage collection to execute the full suite in its service-backed job;
  `--cov-append` could not combine coverage from a different job's filesystem.

Workspace caps remain soft across concurrent claim transactions, as in the
existing queue design; the new limit prevents one workspace consuming another's
slots inside a single batch. This is not a strict global concurrency guarantee.

## Verification environment and commands

Local Python 3.13.5; frontend Node 22.19.0. CI targets Python 3.12 and Node 22.
Docker was initially unavailable, then restarted by the user. After pulling the
images, the disk-backed test database stalled in PostgreSQL `DataFileImmediateSync`.
The completed runs used a dedicated PostgreSQL 16/pgvector container with a tmpfs
data directory and a separate Redis 7.4 container, bound to loopback-only random
ports. No application database or volume was used. These are genuine service
tests, not mocked databases; tmpfs testing does not prove disk/crash durability.

Set `CAIRN_TEST_USE_EXTERNAL_SERVICES=1`, `CAIRN_DATABASE_URL` and `CAIRN_REDIS_URL`
to dedicated disposable test services before running the service-dependent commands.
The integration fixture deliberately truncates its test database.

```powershell
uv sync --locked --dev
uv run python -m pytest tests --cov --cov-report=term-missing --cov-fail-under=80
uv run python -m ruff check src apps tests migrations
uv run python -m ruff format --check src apps tests
uv run python -m mypy
uv run python -c "from importlinter.cli import lint_imports; lint_imports()" --config .importlinter
uv run python scripts/check_route_authz.py
cd apps/web
npm ci
npm run api:check
npm run typecheck
npm test
npm run build
```

Python module invocations avoid stale executable-launcher paths left by renaming
the original local virtual environment. The production package does not depend
on that workaround.

## Final results

| Check | Result |
| --- | --- |
| Full backend suite with PostgreSQL/pgvector and Redis | **400 passed**, no skips; 70.76 seconds |
| Breakdown | 145 unit + 137 contract + 118 integration |
| Combined statement/branch coverage | **81.47%**, unchanged 80% gate passes |
| Ruff lint and format | Pass; 120 Python files checked for formatting |
| Strict mypy | Pass; 89 source files |
| Import boundaries | 4 contracts kept, 0 broken |
| Authorization route declaration audit | 50 routes, 8 intentionally public |
| Frontend | 27 tests, typecheck, API type drift check and production build pass |

One Alembic `path_separator` deprecation warning remains. Frontend tooling warned
about two packages' Node engine ranges and Vite's large vendor chunk; the tested
commands completed successfully. Test services were disposable and cleaned up.

The first combined coverage run measured 78.19%. Additional behavior tests raised
it above the existing gate; no coverage exclusions or reduced threshold were used.

## Later continuation (2026-09-08)

The historical results above are not a claim about the now-expanded working tree. Read the
[execution ledger](15-execution-ledger.md) for current implementation, independent validation,
known failures, active worker ownership and disposable-service state. The knowledge-base E2E
gate remains open. Test containers created by this later continuation are separate from the
already-cleaned services used for the historical results above.

## Gates not closed

- **PDF:** no 30-document ground-truth corpus or quality measurements. T-M07-01
  remains open; the licence checkpoint is not a completed quality evaluation.
- **PQ-6:** the PyPI JSON endpoint for `cairn` returned an existing package
  (version 0.2.3) on 2026-09-07. Do not publish this project under that distribution
  name without resolving the collision. The npm `@cairn/client` metadata endpoint
  returned 404, which is not proof of namespace ownership or a reservation.
  Domain and GitHub ownership still need a product decision. Nothing was registered.
- Docker application-image builds, browser E2E, load/performance SLOs and a
  production rollout are not part of the verification completed here.

Primary package-name checks: `https://pypi.org/pypi/cairn/json` and
`https://registry.npmjs.org/@cairn%2fclient`. Package names are not trademark clearance.
