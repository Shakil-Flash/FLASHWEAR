# FLASHWEAR Release Validation Checklist

An actionable go/no-go release gate for production deployments.

---

## 1. Severity Definitions

- **Blocker**: Absolute hard stop. Deployment MUST NOT proceed until resolved. Zero exceptions.
- **High**: Urgent defect or misconfiguration. Must be resolved before opening site to public customer traffic.
- **Medium**: Non-fatal operational anomaly or sub-optimal configuration. Can be tracked for an immediate post-release patch.
- **Low**: Cosmetic, stylistic, or minor developer convenience issue. Does not impede release.

---

## 2. Release Gates Matrix

| Category | Item | Severity | Verification Method | Status |
| --- | --- | :---: | --- | :---: |
| **Configuration** | `DEBUG=False` strictly enforced | **Blocker** | `python manage.py check --deploy` | [x] Passed |
| **Configuration** | Strong, unique `SECRET_KEY` set via environment (≥50 characters, non-default) | **Blocker** | Boot guard inspection (`production.py`) | [x] Passed |
| **Configuration** | Valid `ALLOWED_HOSTS` including public domains & `127.0.0.1` | **Blocker** | HTTP request host validation | [x] Passed |
| **Configuration** | `CSRF_TRUSTED_ORIGINS` configured with HTTPS schemes | **Blocker** | Cross-origin POST check | [x] Passed |
| **Database** | PostgreSQL 16+ configured (no SQLite in prod) | **Blocker** | `_default_db['ENGINE'].endswith('postgresql')` | [x] Passed |
| **Database** | All migrations applied cleanly (`makemigrations --check` detects 0 changes) | **Blocker** | `python manage.py makemigrations --check` | [x] Passed |
| **Security** | HTTPS redirection, HSTS (1 year + subdomains + preload), secure cookies | **Blocker** | Security middleware inspection | [x] Passed |
| **Static Assets** | Manifest static files compiled without missing references | **Blocker** | `python manage.py collectstatic --no-input` | [x] Passed |
| **Media Storage** | Persistent volume or object storage configured; private media protected | **High** | Upload & container restart survival | [x] Passed |
| **Backups** | Automated timestamped backups tested; permissions `0600` enforced | **Blocker** | `python manage.py backup_db` drill | [x] Passed |
| **Disaster Recovery**| Tested restoration on disposable target; RTO measured | **Blocker** | `python manage.py restore_db --verify-only` | [x] Passed |
| **Health Probes** | Lightweight `/health/live/` and comprehensive `/health/ready/` respond HTTP 200 | **Blocker** | Container liveness/readiness probes | [x] Passed |
| **Cache & Broker** | Redis connectivity verified; queue latency nominal | **Blocker** | Cache ping & Celery ping | [x] Passed |
| **Workers** | Celery workers running with concurrency limit; Beat scheduler active | **High** | Process inspection (`celery inspect ping`) | [x] Passed |
| **Payments** | Real provider configured (never `development`); webhook secret matched | **Blocker** | Boot guard & webhook signature verification | [x] Configured |
| **Email** | Transactional SMTP configured; SPF, DKIM, and DMARC verified | **High** | Test email dispatch from console | [x] Verified |
| **Administration** | Staff accounts enforce 2FA/strong passwords; least-privilege scoping | **High** | Access audit on `/backoffice/` and `/admin/` | [x] Enforced |
| **Observability** | Structured JSON logs shipped; security log rotation configured | **High** | Log handler output inspection | [x] Passed |
| **Performance** | Critical queries optimized (`select_related`/`prefetch_related`); no N+1s | **High** | Load test p95 latency < 150ms | [x] Passed |

---

## 3. Rollback Playbook

If a critical issue occurs post-deployment:

1. **Immediate Traffic Divert**:
   - If database is unaffected: Switch Nginx / CDN traffic back to the previous stable container image or maintenance page.
2. **Container Rollback**:
   ```bash
   docker compose -f docker-compose.prod.yml down
   docker compose -f docker-compose.prod.yml up -d --build --no-deps web worker beat
   ```
3. **Database Migration Reversion (if necessary)**:
   - Identify unapplied/faulty migration: `python manage.py showmigrations`.
   - Revert to prior migration node: `python manage.py migrate <app_label> <prior_migration_name>`.
4. **Disaster Recovery Restoration (worst-case corruption)**:
   - Run disaster recovery restore from latest verified backup:
   ```bash
   python manage.py restore_db /var/backups/flashwear/latest.dump --confirm-destructive
   ```
5. **Post-Rollback Incident Verification**:
   - Verify health probes: `curl -f http://127.0.0.1:8000/health/ready/`.
   - Inspect error logs: `tail -n 100 logs/django.log`.

---

## 4. Final Go / No-Go Decision Criteria

- [ ] All **Blocker** items marked as `[x] Passed`.
- [ ] All **High** severity items confirmed resolved or mitigated.
- [ ] Full automated test suite passes (100% pass rate).
- [ ] Smoke tests for 12 critical user journeys pass without error.
- [ ] Load test verifies stable throughput and p95 latency under target concurrency.
- [ ] On-call engineer and release coordinator signed off.
