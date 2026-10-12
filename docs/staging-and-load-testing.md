# FLASHWEAR Staging & Load Testing Guide

A practical guide for running launch rehearsals, simulated load testing, and disaster drills in a safe, disposable staging environment.

---

## 1. Safety Rules & Scope Constraints

> [!CAUTION]
> **Production Safety Strictures**:
> 1. **NEVER** run load tests against production servers or production databases.
> 2. **NEVER** use real customer data or production database dumps for load testing.
> 3. **NEVER** configure live payment credentials (e.g. live Stripe secret keys) during load tests.
> 4. **NEVER** send real emails to customer inboxes during simulated traffic runs.

Always use synthetic accounts, generated catalogs, and sandboxed/mocked third-party providers.

---

## 2. Disposable Staging Architecture

A staging environment replicates the production stack in an isolated sandbox:

| Component | Staging Specification | Notes |
| --- | --- | --- |
| **Django App** | `config.settings.staging` | Report-only CSP, short HSTS, strict cookie security |
| **Database** | PostgreSQL 16 (isolated container or ephemeral instance) | Sized similarly to production target (e.g. 2 vCPU, 4GB RAM) |
| **Cache & Broker** | Redis 7 | Dedicated database instance, maxmemory 512MB LRU |
| **Celery** | 1 Worker process + 1 Beat scheduler | In-process or single container |
| **Web Server** | Gunicorn (2-4 workers) behind Nginx | Matches production container configuration |
| **Payment Provider** | `stripe` (Test Mode) or `development` provider | Mocked webhooks, sandbox tokens (`tok_visa`) |
| **Email Backend** | `django.core.mail.backends.locmem.EmailBackend` or Mailhog | Captures transactional emails without sending externally |

---

## 3. Synthetic Test Data Generation

To simulate realistic traffic without leaking real customer information, populate the disposable environment with synthetic catalog data:

```bash
# 1. Reset database to fresh schema
python manage.py migrate

# 2. Seed synthetic catalogue (10 brands, 5 categories, 30+ products with variants)
python manage.py seed_catalog --with-images

# 3. Create synthetic operational users
python manage.py shell << 'EOF'
from django.contrib.auth import get_user_model
from apps.accounts.models import Address

User = get_user_model()

# Customer 1
shopper, _ = User.objects.get_or_create(
    email="shopper@flashwear.test",
    defaults={"is_active": True}
)
shopper.set_password("Str0ng-Passw0rd!")
shopper.save()

Address.objects.get_or_create(
    user=shopper,
    full_name="Alex Fashion",
    line1="10 Oxford Street",
    city="London",
    postal_code="W1D 1BS",
    country="GB",
    is_default_shipping=True
)

# Staff operator
staff, _ = User.objects.get_or_create(
    email="staff@flashwear.test",
    defaults={"is_staff": True, "is_active": True}
)
staff.set_password("Str0ng-Passw0rd!")
staff.save()

# Superuser / Admin
admin, _ = User.objects.get_or_create(
    email="admin@flashwear.test",
    defaults={"is_staff": True, "is_superuser": True, "is_active": True}
)
admin.set_password("Str0ng-Passw0rd!")
admin.save()
print("Synthetic accounts seeded successfully.")
EOF
```

---

## 4. Staged Load Testing Profile

Load testing must follow a staged curve to warm caches, identify degradation points, and avoid crashing shared test resources:

```
Concurrency
    ^
 30 |                       [Stage 3: Peak Load]
    |                       (30 users, 150 requests)
 15 |        [Stage 2: Moderate]
    |        (15 users, 75 requests)
  5 | [Stage 1: Warmup]
    | (5 users, 25 req)
  0 +---------------------------------------------> Time
```

### Safe Target Endpoints (Read-Heavy)
- `GET /` (Homepage: hero banners, featured drops, curated carousels)
- `GET /shop/` (Product grid with sorting and facet counts)
- `GET /shop/?q=sneakers` (Catalog search)
- `GET /shop/?category=apparel` (Filtered catalog)
- `GET /products/<slug>/` (Product detail: variant selector, size chart, live stock indicator)
- `GET /health/live/` (Liveness probe)
- `GET /health/ready/` (Readiness probe with database and cache connectivity)

### Write-Heavy Endpoints (Sandboxed)
- `POST /cart/add/` (Adding items with quantity checks)
- `POST /checkout/validate/` (Checkout session snapshot calculation)
- `POST /orders/checkout/` (Order creation with atomic inventory reservation)
- `POST /payments/webhooks/development/` (Signed webhook ingestion)

---

## 5. Executing the Load Test

Use the provided load test script:

```bash
# Basic run against local or staging environment
python scripts/load_test.py --base-url http://localhost:8000 --users 15 --requests 100

# Full staged benchmark
python scripts/load_test.py --base-url http://localhost:8000 --staged
```

### Metrics Recorded
- **Throughput**: Requests per second (RPS).
- **Latency Percentiles**: Median (p50), 95th percentile (p95), 99th percentile (p99), and max latency.
- **Error Rate**: Percentage of HTTP 4xx (excluding expected 404 probes) and 5xx responses.
- **Database Query Count**: Monitored via Django Debug Toolbar or test assertions.

---

## 6. Cleanup & Teardown

After completing the load test:
1. Terminate any background load test runners.
2. Clear the Redis test cache:
   ```bash
   python -c "from django.core.cache import cache; cache.clear()"
   ```
3. Remove ephemeral test databases or restore from clean baseline:
   ```bash
   python manage.py restore_db /var/backups/flashwear/clean_baseline.sqlite3 --confirm-destructive
   ```
4. Verify health endpoints return HTTP 200 before concluding.
