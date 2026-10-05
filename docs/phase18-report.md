# Phase 18 final report — production hardening, observability, deployment readiness

Date: 2026-10-05. Scope: whole-system audit and the Phase 18 programme (settings/secrets
hardening, logging/request-ids/health/monitoring, Celery/webhook/concurrency review,
Docker/CI/runbook/docs, all quality gates, and the deferred Phase 17 notification test
suite folded in as planned).

## 1. Quality gates (§60)

| Gate | Command | Result |
| --- | --- | --- |
| Tests | `pytest` | **1794 passed, 2 skipped** (50.8s) |
| Coverage | `pytest --cov=apps --cov=config` | **77%** statements (16,049 stmts, 118 files at 100%) |
| Django check | `manage.py check` | 0 issues |
| Deploy check | `manage.py check --deploy` (production settings, throwaway key) | 0 issues, 0 warnings |
| Migrations | `manage.py makemigrations --check` | No changes detected |
| Lint | `ruff check .` | All checks passed |
| Format | `ruff format --check .` | 387 files already formatted |
| Templates | `python -m djlint --lint .` | 118 files, 0 errors |
| Static | `manage.py collectstatic --noinput --dry-run` (production settings) | 158 files, 0 errors |
| Dependencies | `python -m pip_audit` | No known vulnerabilities found |
| Frontend | `npm run build` | Success (Tailwind minified build) |
| Docker build | CI `docker` job | Not run locally — Docker unavailable in this environment (see §6) |

The 2 skips are PostgreSQL-only semantics (`select_for_update` visibility) in
`test_inventory_phase6.py` and `test_loyalty_phase7.py`; they run when
`TEST_DATABASE_URL` points at PostgreSQL.

## 2. What was built and verified

### Configuration and secrets
- Settings split extended: `development`, `staging` (production behaviour, short HSTS,
  report-only CSP), `production`, `testing`. Production refuses to boot on a development
  `SECRET_KEY`, empty `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`, non-PostgreSQL
  `DATABASE_URL`, missing `REDIS_URL`, the development payment provider, a missing
  `PAYMENT_WEBHOOK_SECRET`, or SMTP without `EMAIL_HOST` — each with a message that
  names the variable.
- `.env.example` completed: **400 lines**, every variable the code reads now documented
  with its default and the reasoning (observability, TLS/CSP, DB connection behaviour,
  gunicorn sizing, account throttles, Celery retry/expiry, all 16 `NOTIFICATIONS_*`
  knobs, support, Loop, quests, back-office alert thresholds, S3 credentials).
  Deliberately-commented entries (`LOG_FORMAT`, `CSP_REPORT_ONLY`,
  `SUPPORT_ATTACHMENT_ROOT`) explain why an explicit empty/offset value would override a
  per-environment default.
- `tests/test_project_config.py` (36 tests) guards the guards.

### Observability
- `RequestContextMiddleware`: mints/adopts `X-Request-ID` (pattern-validated),
  propagates a ContextVar into every log line, times the request, emits one access-log
  line, echoes the header on the response.
- Structured logging: `RedactingFormatter` strips credentials/tokens/cookies/emails
  after interpolation; JSON formatter behind `LOG_FORMAT=json` (production default);
  three rotating streams — `flashwear.log`, `django.log`, `security.log` (separate
  rotation policy for the security stream).
- Health probes: `/health/` (fixed payload smoke test), `/health/live/` (process only —
  used by the container `HEALTHCHECK`, immune to a database outage),
  `/health/ready/` (database and cache must answer, broker advisory; `503` = stop
  routing, not restart).
- Error handlers for 400/403/404/500 plus CSRF failures, rendered from CSP-clean
  templates.
- `apps/core/monitoring.py`: optional Sentry (`SENTRY_DSN`,
  `SENTRY_TRACES_SAMPLE_RATE`) with logging fallback when the SDK is absent.

### Security hardening
- CSP middleware (script-src 'self', no third-party origins) with `CSP_REPORT_ONLY`
  rollout path; secure cookies/HSTS/SSL redirect settings wired for production.
- Attempt throttles with 429 + `Retry-After` on sign-in (per account and per address,
  failures only), register, resend-verification and password reset
  (`ACCOUNT_ATTEMPT_*`); DRF `throttle_scope` on the API views.
- Upload validation by content (Pillow format/extension/dimension checks) on avatar,
  six catalogue/support image fields; support attachments stored outside `MEDIA_ROOT`
  and served only through the ownership-checked download route.
- Payment webhooks: HMAC signature + timestamp tolerance + unique
  `(provider, event_id)` idempotency (Phase 6, re-reviewed).
- Back-office capabilities answer 404, never 403; audit log append-only from every
  surface.

### Celery / concurrency review
- Retry policy with bounded exponential backoff, hard/soft time limits, JSON-only
  serialization, result expiry (`CELERY_RESULT_EXPIRES=3600` — nothing polls results).
- 11 beat entries, each with an interval setting; every sweep idempotent (documented
  in `TestCeleryConfiguration`). Event-driven tasks fire from `transaction.on_commit`
  and are never scheduled.
- Documented lock ordering for order placement (checkout → payment → order →
  reservation → stock asc → promotion → user) and the inventory lock graph (no cycles).

### Database index review
- 11 index edits across 10 models with explanatory comments; 9 new migrations
  (`accounts`, `catalog`, `engagement`, `inventory`, `loop`, `orders`, `payments`,
  `quests`, `support`).
- `EXPLAIN QUERY PLAN` probe executed against a seeded, throwaway database:
  order queue, shipment list and support queue resolve via `SEARCH ... USING INDEX`
  (planner-confirmed); the promotions alert table is small enough to `SCAN` — the index
  exists in the schema and takes over as the table grows (honestly recorded rather
  than over-claimed).
- `TestHotPathIndexes` (6 tests) asserts the index definitions survive refactors.

### Phase 17 notification tests (deferred work, folded in)
- `tests/test_notifications_phase17.py`: **121 tests** — model rules, dispatcher,
  preferences/unsubscribe, throttling, Celery tasks and sweeps, broadcast batching,
  email content, HTML + JSON surfaces, selectors, retry, domain integrations
  (orders, payments, support, loyalty, drops, Loop, quests, account security) and
  failure isolation.
- Back-office delivery inspection coverage in `tests/test_backoffice_phase16.py`:
  access-matrix row (`notifications`: ORDERS may read, FINANCE gets 404), body shown
  only on the detail route, retry permission (`notifications.manage` required, POST
  only), unknown-detail 404 — 6 new tests (file now 135).
- **Bug found and fixed while testing:** `apps/drops/api/views.py` referenced
  nonexistent `FlashDrop.LIVE`/`FlashDrop.SCHEDULED` (correct: module-level
  `DropStatus`), which 500'd the drop detail page and the interest POST.

### Performance baseline
- Measured (median of 5 runs, SQLite test database): product list 11 queries/16ms,
  category list 1/4ms, notification center 10/11ms, notifications API 5/3ms,
  back-office dashboard 44/18ms, back-office notifications 13/10ms, `/health/live/`
  0 queries/1ms.
- Locked in as regression budgets in `tests/test_performance_baseline.py` (measured +
  headroom); dashboard/queue budgets already lived in the Phase 16 suite.

### Deployment and CI
- `Dockerfile` (multi-stage: Tailwind build → Python runtime, `collectstatic` at build
  with placeholder env, non-root user, `HEALTHCHECK` against `/health/live/` with the
  `X-Forwarded-Proto` trick so `SECURE_SSL_REDIRECT` cannot mask a dead process),
  `.dockerignore`, `gunicorn.conf.py` (env-sized workers, graceful recycling), 
  `deploy/nginx.conf`, `docker-compose.prod.yml` (db/redis unpublished ports,
  web/worker/beat/nginx with healthchecks).
- `.github/workflows/ci.yml`: 4 jobs — Python gates (lint, format, check, migrations,
  tests), assets (build + djlint), Docker build, dependency audit.
- `tests/test_deployment_config.py` (17 tests) pins the compose/Dockerfile contracts.

### Documentation
- `docs/production-runbook.md` (312 lines): environments, boot-guard table, first
  deploy, routine deploys and the **migration write-lock window** (Phase 18 index set
  named, `CREATE INDEX CONCURRENTLY` recipe), health probes, service failure modes,
  Celery operations and the 11-entry beat table, logging/request-id/Sentry, backups and
  a restore drill, rollback, seven incident playbooks, the performance baseline table,
  security operations, and the local Docker limitation.
- `README.md` refreshed: intro through Phase 18, full app inventory, health/back-office
  endpoints, Operations section linking the runbook, updated project tree (new apps,
  docs/, deploy/, CI, prod compose), "Later phases & current surface" table replacing
  the stale "Future Phases" list, expanded security notes and test-count/baseline
  pointers.

## 3. Test totals

| Suite | Tests |
| --- | --- |
| Full suite | **1794 passed, 2 skipped** |
| New this session: notifications (Phase 17) | 121 |
| New this session: delivery-inspection screens | 6 |
| New this session: performance baselines | 5 |
| New this session: hot-path indexes | 6 |
| Existing Phase 16 back office | 135 |
| Existing deployment config | 17 |
| Existing project config | 36 |

## 4. Notable findings

1. **Drops detail/interest 500 (fixed).** Nonexistent enum references
   (`FlashDrop.LIVE`/`SCHEDULED`) in `apps/drops/api/views.py`; corrected to
   `DropStatus` — caught by writing the notification integration tests, not by the
   earlier suites.
2. **Promotions alert index is dormant at small scale.** `EXPLAIN` shows a `SCAN` at
   ~150 rows/50% selectivity; recorded honestly in the runbook — the index takes over
   with growth.
3. **`apps/creator` is dead code** (not in `INSTALLED_APPS`); documented in README as
   not-yet-live rather than silently half-wired.
4. **`apps/loop/services/__init__.py` is empty** — Loop code imports the module
   directly; noted for future package hygiene.
5. **`ALLOWED_HOSTS` must include `127.0.0.1`** for the container healthcheck;
   called out in `.env.example`, the Dockerfile comment and the runbook boot-guard
   table, because the failure mode (healthy process marked dead) is invisible.

## 5. Verification

```powershell
pytest                                          # 1794 passed, 2 skipped
pytest --cov=apps --cov=config                  # 77%
ruff check . ; ruff format --check .            # clean
python -m djlint --lint .                       # 118 files, 0 errors
python manage.py check ; manage.py makemigrations --check
$env:DJANGO_SETTINGS_MODULE="config.settings.production"; # plus the 8 guard vars
python manage.py check --deploy                 # 0 issues
python -m pip_audit                             # no known vulnerabilities
npm run build                                   # success
```

## 6. Known limitations

- **Docker build not run locally** — Docker is unavailable in this environment; the CI
  `docker` job covers it, and the runbook documents first-deploy rehearsal expectations.
- Two PostgreSQL-only tests skip on SQLite (they run against `TEST_DATABASE_URL`).
- `npm run build` emits a stale `caniuse-lite` warning (cosmetic; update with
  `npx update-browserslist-db@latest` when convenient).
- Coverage is 77% overall; the thin areas are the recommendation/stylist services
  (`apps/recommendations`, `apps/styling`) whose tests live in their own phase suites
  or are still to be written — outside Phase 18 scope.

## 7. Files changed in this session

- Modified: `.env.example`, `README.md`, `tests/test_backoffice_phase16.py`,
  `tests/test_phase18_infrastructure.py`, `static/css/app.css` (build artifact),
  10 model files (index edits), `apps/drops/api/views.py` (bug fix).
- New: `docs/production-runbook.md`, `tests/test_notifications_phase17.py`,
  `tests/test_performance_baseline.py`, 9 migration files.
- Nothing has been committed; all changes are staged-ready in the working tree.
