# FLASHWEAR production runbook

How to deploy, operate, observe and recover a FLASHWEAR installation. The examples
assume the Compose stack in `docker-compose.prod.yml`; a Kubernetes or VM deployment
follows the same order with different orchestration.

## Contents

1. [Environments](#environments)
2. [Configuration and boot guards](#configuration-and-boot-guards)
3. [First deploy](#first-deploy)
4. [Routine deploys and the migration window](#routine-deploys-and-the-migration-window)
5. [Health probes](#health-probes)
6. [Services](#services)
7. [Celery: workers, beat and sweeps](#celery-workers-beat-and-sweeps)
8. [Observability](#observability)
9. [Backups and restore](#backups-and-restore)
10. [Rollback](#rollback)
11. [Incident playbooks](#incident-playbooks)
12. [Performance baseline](#performance-baseline)
13. [Security operations](#security-operations)

## Environments

Settings modules live under `config/settings/`:

| Module | Purpose |
| --- | --- |
| `development` | Local work. SQLite/Redis fallbacks, console email, development payment provider. |
| `staging` | Production behaviour with short HSTS and **report-only** CSP — a deploy rehearsal against real infrastructure. |
| `production` | Enforces CSP, TLS redirects, HSTS, SMTP, the real payment provider. Refuses to boot when misconfigured (see below). |
| `testing` | pytest suite: locmem email, eager Celery, in-memory SQLite. |

Select an environment with `DJANGO_SETTINGS_MODULE`. The Docker image sets
`config.settings.production` as its default; override it in `.env` for staging.

## Configuration and boot guards

Every supported variable is documented, with its default and the reasoning behind it,
in `.env.example`. Copy it once, edit the empty values, never commit `.env`.

Production and staging fail fast at startup instead of failing at the first request:

| Refusal | What it means | Fix |
| --- | --- | --- |
| `SECRET_KEY must be set` | No signing key, or the development placeholder | Generate one: `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"` |
| `ALLOWED_HOSTS must be set` | Django would refuse every request | List the public hostname. **Include `127.0.0.1`** — the container healthcheck calls `http://127.0.0.1:8000/health/live/`, and a missing entry turns a healthy process into a dead one |
| `CSRF_TRUSTED_ORIGINS must be set` | Checkout, login and every other POST would be rejected | Full origins, e.g. `https://flashwear.example` |
| `DATABASE_URL` not `postgres://` | SQLite in production is refused | Point at PostgreSQL |
| `REDIS_URL must be set` | No cache and no broker | Point at Redis |
| `PAYMENT_PROVIDER` is `development` or unknown | The simulator would take real money, or the name is a typo | Set the real provider; unknown names are refused at boot, not at the first checkout |
| `PAYMENT_WEBHOOK_SECRET must be set` | Webhook signatures would fall back to a development secret | Random 32+ bytes, mirrored at the provider |
| `EMAIL_HOST must be set` (SMTP backend) | Mail would fail on the first transactional send | Your relay or ESP host |

List-valued variables (`ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, format lists) are
comma-separated in `.env`.

## First deploy

```bash
# 1. Configure (every value in the boot-guard table above must change).
cp .env.example .env && ${EDITOR} .env

# 2. Build. collectstatic runs inside the build with placeholder env, so no
#    secret is baked into the image.
docker compose -f docker-compose.prod.yml build

# 3. Infrastructure first.
docker compose -f docker-compose.prod.yml up -d db redis

# 4. Schema, then the application services.
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
docker compose -f docker-compose.prod.yml up -d

# 5. First admin account and (optionally) catalogue seed data.
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
docker compose -f docker-compose.prod.yml exec web python manage.py seed_catalog

# 6. Verify.
curl -fsS https://your-host/health/ready/
```

Static files are served by WhiteNoise from the manifest baked into the image; `media/`
and `logs/` are named volumes. Nginx owns the public port and proxies to `web:8000`.

## Routine deploys and the migration window

Order matters — application code and schema must never be more than one step apart:

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d db redis
docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate
docker compose -f docker-compose.prod.yml up -d web worker beat
docker compose -f docker-compose.prod.yml restart nginx   # only when nginx.conf changed
```

### Migrations lock writes

Django runs every migration as DDL inside a transaction. Two kinds of statement in this
codebase have real production impact:

- **`CREATE INDEX`** blocks *writes* to the table for the duration of the build. The
  Phase 18 index set touches `orders_order`, `orders_shipment`, `support_ticket`,
  `engagement_promotion`, `engagement_pointstransaction`, `accounts_user`,
  `inventory_stock`, `quests_quest`, `catalog_product` and `loop_loopitem`. On a small
  table that is milliseconds; on a hot `orders_order` in live trade it is the difference
  between a hiccup and a stall.
- **`ALTER TABLE` / `ADD COLUMN` with a default** validates or rewrites existing rows.

For a busy database, deploy in a quiet window. For one large index that cannot wait,
create it yourself first — `migrate` skips an index that already exists with the same
definition:

```sql
CREATE INDEX CONCURRENTLY orders_order_created_idx ON orders_order (created_at DESC);
```

then run `migrate` normally. `migrate --fake` is the escape hatch only when you are
certain the database object already matches the migration.

Migrations are **forward-only**. Rolling back code does not roll back schema; keep the
previous image available so you can redeploy it against the current (backward-
compatible) schema.

## Health probes

| Endpoint | Answers 200 when | Use for |
| --- | --- | --- |
| `/health/` | The process can render a view | Human smoke test; fixed payload, no infrastructure detail |
| `/health/live/` | The process is up | Container `HEALTHCHECK` and orchestrator liveness. No dependencies, so a database outage does not restart the app |
| `/health/ready/` | **Database and cache answer**; broker is advisory | Load-balancer readiness. A `503` here means "stop routing new traffic", not "restart me" |

The image's `HEALTHCHECK` runs `curl -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/health/live/`
every 30 s. The header keeps `SECURE_SSL_REDIRECT` from answering `301` and masking a
dead process behind a healthy-looking redirect — which is also why `ALLOWED_HOSTS`
must include `127.0.0.1`.

## Services

| Service | Command | When it dies |
| --- | --- | --- |
| `web` | gunicorn (`gunicorn.conf.py`) | Site down; healthcheck fails and the orchestrator restarts it |
| `worker` | `celery -A config worker` | Event-driven tasks queue up (emails, broadcasts, views counters); the 11 beat sweeps run late. Nothing corrupts — sweeps and tasks are idempotent |
| `beat` | `celery -A config beat` | Housekeeping stops: expired holds are not released, points do not expire, notifications are not swept. Restarting beat resumes from the current second; no catch-up storm |
| `nginx` | `nginx:1.27-alpine` | Public traffic stops; `nginx -t` before reloads, `restart: always` |
| `db` | PostgreSQL 16 | `/health/ready/` goes `503`; the app degrades to errors on every query |
| `redis` | Redis 7 | Cache misses fall through to the database (slow but correct, `CACHE_IGNORE_EXCEPTIONS=True`); the broker is down, so Celery stops accepting new tasks |

```bash
docker compose -f docker-compose.prod.yml ps                     # who is healthy
docker compose -f docker-compose.prod.yml logs -f web worker beat # follow logs
docker compose -f docker-compose.prod.yml restart worker           # bounce one service
docker compose -f docker-compose.prod.yml exec worker celery -A config inspect ping
```

Sizing: `WEB_CONCURRENCY` (gunicorn workers) defaults to 3 — the usual rule is
`(2 x CPU cores) + 1`, and each worker is its own memory budget. `GUNICORN_MAX_REQUESTS`
recycles workers after 1000 requests to cap leak growth; recycling is graceful, so
in-flight requests finish first.

## Celery: workers, beat and sweeps

Eleven beat entries, all interval-tunable from `.env`:

| Entry | Task | Setting | Default |
| --- | --- | --- | --- |
| `inventory-sweep-expired-reservations` | `inventory.sweep_expired_reservations` | `INVENTORY_SWEEP_INTERVAL_SECONDS` | 60s |
| `engagement-sweep-loyalty` | `engagement.sweep_loyalty` | `LOYALTY_SWEEP_INTERVAL_SECONDS` | 60s |
| `quests-sweep-progress` | `quests.sweep_progress` | `QUESTS_SWEEP_INTERVAL_SECONDS` | 300s |
| `notifications-sweep-queue` | `notifications.sweep_queue` | `NOTIFICATIONS_QUEUE_SWEEP_INTERVAL_SECONDS` | 300s |
| `notifications-sweep-retention` | `notifications.sweep_retention` | `NOTIFICATIONS_RETENTION_SWEEP_INTERVAL_SECONDS` | 6h |
| `notifications-sweep-drop-events` | `notifications.sweep_drop_events` | `NOTIFICATIONS_DROP_SWEEP_INTERVAL_SECONDS` | 300s |
| `notifications-sweep-points-expiring` | `notifications.sweep_points_expiring` | `NOTIFICATIONS_POINTS_SWEEP_INTERVAL_SECONDS` | 24h |
| `support-close-abandoned` | `support.close_abandoned_tickets` | `SUPPORT_SWEEP_INTERVAL_SECONDS` | 1h |
| `support-remind-pending` | `support.remind_pending_tickets` | `SUPPORT_SWEEP_INTERVAL_SECONDS` | 1h |
| `loop-expire-stale-listings` | `loop.expire_stale_listings` | `LOOP_SWEEP_INTERVAL_SECONDS` | 1h |
| `loop-expire-credits` | `loop.expire_loop_credits` | `LOOP_SWEEP_INTERVAL_SECONDS` | 1h |

Rules that make this safe to operate:

- **Every sweep is idempotent.** Database state is authoritative; skipping a pass
  delays housekeeping, running one twice changes nothing twice.
- **Event-driven tasks are never scheduled.** `notifications.send_email`,
  `notifications.broadcast_batch` and the like fire from `transaction.on_commit`, so
  they exist exactly when their triggering row committed.
- **Retries are bounded** (`CELERY_TASK_MAX_RETRIES`, exponential backoff capped at
  `CELERY_TASK_RETRY_BACKOFF_MAX`) and tasks are killed at `CELERY_TASK_TIME_LIMIT`.
  A poison message cannot occupy a worker forever.
- **Results expire** after `CELERY_RESULT_EXPIRES` (1h): nothing polls the result
  backend, so it costs Redis memory and buys nothing.
- **Notifications that exhausted `NOTIFICATIONS_MAX_EMAIL_ATTEMPTS`** park as `failed`
  and are requeued by the queue sweep or manually from
  `/operations/notifications/` (permission `notifications.manage`).

Inspect and drain:

```bash
celery -A config inspect active            # what is running right now
celery -A config inspect reserved          # what is prefetched
celery -A config purge                     # discard queued tasks (irreversible!)
```

Purging loses work. Sweeps and notifications rebuild themselves on the next pass; a
purge during a payment task is not something you can undo, so prefer draining: stop
routing new tasks, let the worker finish, restart.

## Observability

### Logs

Three rotating files under `LOG_DIR` (default `logs/`), plus the container stdout:

| File | Contents |
| --- | --- |
| `flashwear.log` | Application log (`flashwear.*` loggers), including task and domain errors |
| `django.log` | Django's own request/handler log |
| `security.log` | Sign-in attempts, permission failures and the security stream — separate so it can be shipped, alerted on and retained differently |

- `LOG_FORMAT=json` (production default) emits one JSON object per line;
  `plain` is the development default. Leave the variable unset in `.env` so each
  environment keeps its default.
- Every line passes `RedactingFormatter`: passwords, tokens, API keys, session
  cookies and email addresses are stripped **after** interpolation, so an accidental
  `logger.info("user %s", user)` cannot leak a credential.
- Rotation is `LOG_MAX_BYTES` x `LOG_BACKUP_COUNT` (security: `SECURITY_LOG_BACKUP_COUNT`).

### Request ids

`RequestContextMiddleware` mints (or adopts a well-formed incoming) `X-Request-ID`,
attaches it to every log line for that request and echoes it on the response. When a
customer reports an error, ask for the id from the response header — it selects the
exact request in the logs without time-range guessing.

### Access log

One line per request: method, path, status, duration, request id. Use it for latency
spot-checks (`grep` the p95 of a status) before reaching for an APM.

### Sentry

Set `SENTRY_DSN` and install `sentry-sdk`; with it unset errors go to `flashwear.log`
instead. `SENTRY_TRACES_SAMPLE_RATE` samples performance traces — `0.0` records errors
only, `0.05`-`0.2` is a sane production trace rate. The DSN is write-only: treat it
like a password.

### Back-office operations

`/operations/` holds the operator surface: dashboard, queue health at
`/operations/health/` (live db/cache/broker probes plus the same alert rules as
`/operations/alerts/`), the delivery inspection at `/operations/notifications/` with
per-row requeue, and the audit log (append-only, readable, never writable from any
surface). Capability groups are in `apps/backoffice/permissions.py`; a missing
capability answers **404, never 403**.

## Backups and restore

| Data | Where | Backup |
| --- | --- | --- |
| PostgreSQL | `db` volume (`pg_data`) | `pg_dump` on a schedule — this is the order, customer and payment record |
| Media uploads | `media` volume | File-level snapshot; support attachments live outside it in `private/support/` and need their own copy |
| Redis | `redis` volume | **Do not back up.** It holds cache and broker state; both rebuild themselves |
| Logs | `logs` volume | Ship off-box (or to Sentry); a volume snapshot is a stopgap |

Nightly dump, retained to your policy:

```bash
docker compose -f docker-compose.prod.yml exec db \
  pg_dump -U flashwear -Fc flashwear > "backup-$(date +%F).dump"
```

Restore drill (do this before you need it):

```bash
docker compose -f docker-compose.prod.yml stop web worker beat
docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore -U flashwear -d flashwear --clean --if-exists < backup-2026-10-05.dump
docker compose -f docker-compose.prod.yml up -d web worker beat
curl -fsS https://your-host/health/ready/
```

A restore is a point-in-time rollback of everything: orders placed after the dump are
gone, which is exactly why the drill matters and why payment records should be
reconciled against the provider after any restore.

## Rollback

1. Redeploy the previous image — configuration and schema stay where they are:

   ```bash
   docker compose -f docker-compose.prod.yml up -d web worker beat
   ```

2. If the release migrated schema that the previous code cannot tolerate, stop there
   and read the migration: this codebase writes migrations to be backward-compatible
   with the previous release (add columns, then backfill, then switch reads — never
   drop in the same deploy). A breaking migration is a bug; report it rather than
   inventing an `migrate` backwards.
3. `/health/ready/` must return 200 and a checkout walk-through must pass before the
   rollback is declared done.

## Incident playbooks

**Site down (5xx or no answer).**
`docker compose ps` → is `web` healthy? If the healthcheck is failing, read
`docker compose logs web`: a boot-guard refusal names the missing variable verbatim.
If `/health/live/` is fine but `/health/ready/` is 503, the database or cache is the
problem, not the app — check `db`/`redis` logs first.

**Requests hanging, workers piling up.**
`docker compose logs --tail=100 worker`. A run of `Task timed out` means an external
call is exceeding `CELERY_TASK_TIME_LIMIT`; the worker kills and retries it. Check the
provider's status page; nothing in the codebase waits forever inside a transaction.

**Emails or notifications stuck.**
1. `docker compose logs worker | grep notifications` — is the worker running at all?
2. `/operations/notifications/?status=failed` — rows that exhausted their attempts show
   up here; select them and POST requeue (permission `notifications.manage`).
3. Broker down? `celery -A config inspect ping` against the worker.

**Queue backing up (beats not firing).**
`docker compose logs beat --tail=100`. Beat is a single process with no database
state; if it exited, restart it. Missed intervals are **not** replayed — the next
pass runs normally, so housekeeping is late, never doubled.

**Payments failing in a burst.**
`/operations/alerts/` fires at `BACKOFFICE_ALERT_PAYMENT_FAILURES` failures in the
window and `BACKOFFICE_STALE_PAYMENT_MINUTES` for payments that never resolve.
Webhook signature failures mean `PAYMENT_WEBHOOK_SECRET` differs from the provider's
value — a deploy that changed the variable without changing the provider will show
every delivery as rejected in `security.log`.

**Disk filling.**
Watch `logs/` and `media/`. Log rotation bounds the log files (`LOG_MAX_BYTES` x
`LOG_BACKUP_COUNT`); if the disk is still growing, the next suspect is media uploads
or an unrotated process writing elsewhere. `df -h` inside the container, then the
volume.

**Database unreachable.**
`/health/ready/` returns 503, new traffic stops at the load balancer, the app keeps
its liveness (no restart loop). Fix connectivity, then confirm `pg_isready` from the
`web` container and wait for ready to recover.

## Performance baseline

Measured 2026-10-05 (SQLite test database, small fixtures) — median of five runs:

| Endpoint | Queries | ms (median) |
| --- | --- | --- |
| `/health/live/` | 0 | 1 |
| `/products/` | 11 | 16 |
| `/categories/` | 1 | 4 |
| `/notifications/` (center) | 10 | 11 |
| `/api/v1/notifications/` | 5 | 3 |
| `/operations/` (dashboard) | 44 | 18 |
| `/operations/notifications/` | 13 | 10 |

These counts are locked in as regression tests in
`tests/test_performance_baseline.py` (budgets = measured + headroom, so an N+1 or a
dropped `select_related` fails CI while harmless drift does not). The dashboard and
orders queue carry their own budgets in `tests/test_backoffice_phase16.py`.

The Phase 18 index review also ran `EXPLAIN QUERY PLAN` against a seeded database:
the order queue, shipment list and support queue all resolve through their new indexes
(`SEARCH ... USING INDEX`), while the promotions alert scans at the current row count
because the table is small — the index exists in the schema and takes over as the
table grows. Re-run the probe (documented in `tests/test_phase18_infrastructure.py`)
after large data migrations.

## Security operations

- **CSP rollout.** Staging runs `CSP_REPORT_ONLY=True`: violations surface in the
  browser console without breaking pages. Watch a release cycle, then let production
  enforce (`CSP_REPORT_ONLY=False`, the production default).
- **Secrets.** No secret is committed; `.env` is git-ignored. Rotate `SECRET_KEY`
  (signs sessions — everyone is signed out), `PAYMENT_WEBHOOK_SECRET` (mirror at the
  provider in the same window) and the database password (update `.env`, restart).
- **Dependency audit.** `python -m pip_audit` in CI on every push; treat a new CVE in
  the web stack as a same-week deploy.
- **Uploads are validated by content, not by name**, on every image field: Pillow
  opens the file, the real format must be on the allow-list, dimensions are capped,
  and filenames are replaced with UUIDs. Support attachments live outside `MEDIA_ROOT`
  and are only readable through the ownership-checked download route. Do not "fix" a
  rejected upload by widening a format list without a sanitiser — SVG stays out.
- **Throttling.** Sign-in failures are counted per account and per address
  (`LOGIN_MAX_FAILURES_*`); the register/resend/reset forms carry their own
  (`ACCOUNT_ATTEMPT_*`); anonymous API traffic is rate-limited at the DRF level.
  `USE_X_FORWARDED_FOR=True` only behind a proxy that overwrites the header.
- **Audit trail.** Security-relevant writes land in the back-office audit log and the
  account event stream (digests and field names, never values). The audit log is
  append-only from every surface, including the API.

## Known environment limitations

Docker was unavailable in the environment where this repository was developed, so the
image build and Compose stack are covered by the CI `docker` job rather than a local
run. Treat the first `docker compose up` on a fresh host as a rehearsal: the boot
guards, healthchecks and migration steps above are what to expect.
