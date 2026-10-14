#!/usr/bin/env bash
# ==============================================================================
# FLASHWEAR Safe Deployment Automation Workflow
#
# Follows the production runbook step-by-step:
#   1. Validate configuration & security deployment checks (check --deploy)
#   2. Build application assets and container images
#   3. Run automated tests and quality gates
#   4. Collect static assets (with manifest storage)
#   5. Apply reviewed database migrations
#   6. Restart services safely with zero-drift ordering
#   7. Verify health and readiness probes
#   8. Report status or failures clearly
#
# Usage:
#   ./scripts/deploy.sh [--dry-run] [--skip-tests]
# ==============================================================================
set -euo pipefail

DRY_RUN=0
SKIP_TESTS=0

for arg in "$@"; do
    case "$arg" in
        --dry-run)
            DRY_RUN=1
            ;;
        --skip-tests)
            SKIP_TESTS=1
            ;;
    esac
done

echo "=================================================================="
echo "          FLASHWEAR Production Deployment Workflow                "
echo "=================================================================="

# ------------------------------------------------------------------------------
# 1. Validate Required Configuration & Security Checks
# ------------------------------------------------------------------------------
echo "==> [Step 1/7] Validating deployment configuration..."
if [ -f "manage.py" ]; then
    python manage.py check --deploy
    python manage.py makemigrations --check --dry-run
else
    echo "Notice: manage.py not in working directory; skipping local check."
fi

# ------------------------------------------------------------------------------
# 2. Build Assets & Images
# ------------------------------------------------------------------------------
echo "==> [Step 2/7] Building frontend stylesheets & container images..."
if [ -f "package.json" ] && command -v npm >/dev/null 2>&1; then
    npm run build:css
fi

if [ -f "docker-compose.prod.yml" ] && command -v docker >/dev/null 2>&1; then
    echo "Building production Docker images..."
    docker compose -f docker-compose.prod.yml build
fi

# ------------------------------------------------------------------------------
# 3. Quality Gates & Tests
# ------------------------------------------------------------------------------
if [ "${SKIP_TESTS}" -eq 1 ]; then
    echo "==> [Step 3/7] Skipping automated test suite (--skip-tests specified)."
else
    echo "==> [Step 3/7] Running test suite and quality gates..."
    if command -v pytest >/dev/null 2>&1; then
        pytest -q --maxfail=1
    elif command -v uv >/dev/null 2>&1; then
        uv run pytest -q --maxfail=1
    fi
fi

# ------------------------------------------------------------------------------
# 4. Collect Static Assets
# ------------------------------------------------------------------------------
echo "==> [Step 4/7] Collecting static assets with WhiteNoise manifest..."
if [ -f "manage.py" ]; then
    python manage.py collectstatic --noinput
fi

if [ "${DRY_RUN}" -eq 1 ]; then
    echo "==> [DRY RUN] Halting before applying migrations and restarting services."
    echo "==> Configuration and build verification PASSED."
    exit 0
fi

# ------------------------------------------------------------------------------
# 5. Database Migrations
# ------------------------------------------------------------------------------
echo "==> [Step 5/7] Applying reviewed database migrations..."
if [ -f "docker-compose.prod.yml" ] && command -v docker >/dev/null 2>&1; then
    docker compose -f docker-compose.prod.yml run --rm web python manage.py migrate --noinput
elif [ -f "manage.py" ]; then
    python manage.py migrate --noinput
fi

# ------------------------------------------------------------------------------
# 6. Safe Service Restart
# ------------------------------------------------------------------------------
echo "==> [Step 6/7] Starting / restarting services..."
if [ -f "docker-compose.prod.yml" ] && command -v docker >/dev/null 2>&1; then
    docker compose -f docker-compose.prod.yml up -d web worker beat
    docker compose -f docker-compose.prod.yml restart nginx || true
fi

# ------------------------------------------------------------------------------
# 7. Health & Readiness Verification
# ------------------------------------------------------------------------------
if [ -z "${HEALTH_URL:-}" ]; then
    if [ -f "docker-compose.prod.yml" ] && command -v docker >/dev/null 2>&1; then
        HEALTH_URL="http://localhost/health/ready/"
    else
        HEALTH_URL="http://localhost:8000/health/ready/"
    fi
fi
if command -v curl >/dev/null 2>&1; then
    # Retry health probe up to 10 times (30 seconds)
    READY=0
    for i in $(seq 1 10); do
        if curl -fsS -H "X-Forwarded-Proto: https" "${HEALTH_URL}" >/dev/null 2>&1; then
            READY=1
            break
        fi
        echo "Waiting for service readiness (${i}/10)..."
        sleep 3
    done

    if [ "${READY}" -ne 1 ]; then
        echo "ERROR: Health check failed at ${HEALTH_URL}. Service is not ready!" >&2
        exit 1
    fi
fi

echo "=================================================================="
echo "          DEPLOYMENT COMPLETED SUCCESSFULLY                       "
echo "=================================================================="
