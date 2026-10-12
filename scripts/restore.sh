#!/usr/bin/env bash
# ==============================================================================
# FLASHWEAR Disaster Recovery & Database Restoration Script
#
# Restores a verified PostgreSQL custom dump.
# Enforces safety guards against accidental production database overwrites.
#
# Usage:
#   # Disaster recovery drill into disposable test database:
#   ./scripts/restore.sh path/to/backup.dump --target-db=disposable_test
#
#   # Explicit primary database restoration:
#   ./scripts/restore.sh path/to/backup.dump --confirm-destructive
# ==============================================================================
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <backup_file> [--target-db=<dbname>] [--confirm-destructive] [--verify-only]" >&2
    exit 1
fi

BACKUP_FILE="$1"
shift

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "ERROR: Backup file not found: ${BACKUP_FILE}" >&2
    exit 1
fi

# If running inside Django environment, forward to restore_db management command
if command -v python >/dev/null 2>&1 && [ -f "manage.py" ]; then
    python manage.py restore_db "${BACKUP_FILE}" "$@"
    exit 0
fi

# Standalone execution
TARGET_DB="${POSTGRES_DB:-flashwear}"
CONFIRM_DESTRUCTIVE=0
VERIFY_ONLY=0

for arg in "$@"; do
    case "$arg" in
        --target-db=*)
            TARGET_DB="${arg#*=}"
            ;;
        --confirm-destructive)
            CONFIRM_DESTRUCTIVE=1
            ;;
        --verify-only)
            VERIFY_ONLY=1
            ;;
    esac
done

# Step 1: Pre-flight verify
echo "==> [FLASHWEAR Restore] Verifying archive header and index..."
pg_restore -l "${BACKUP_FILE}" >/dev/null
echo "==> Archive integrity verified: valid PostgreSQL custom dump."

if [ "${VERIFY_ONLY}" -eq 1 ]; then
    echo "==> Verification mode only. Exiting without database modification."
    exit 0
fi

# Step 2: Safety guard
if [ "${TARGET_DB}" = "${POSTGRES_DB:-flashwear}" ] && [ "${CONFIRM_DESTRUCTIVE}" -ne 1 ]; then
    echo "ERROR: Refusing to restore over default database '${TARGET_DB}' without --confirm-destructive." >&2
    echo "To restore into a disposable/test database, use --target-db=my_test_db." >&2
    exit 1
fi

POSTGRES_USER="${POSTGRES_USER:-flashwear}"
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    echo "ERROR: POSTGRES_PASSWORD environment variable is required." >&2
    exit 1
fi

export PGPASSWORD="${POSTGRES_PASSWORD}"

START_TIME=$(date +%s)
echo "==> [FLASHWEAR Restore] Restoring ${BACKUP_FILE} into ${TARGET_DB}..."

pg_restore \
    -h "${POSTGRES_HOST}" \
    -p "${POSTGRES_PORT}" \
    -U "${POSTGRES_USER}" \
    -d "${TARGET_DB}" \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    "${BACKUP_FILE}"

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo "==> [FLASHWEAR Restore] Restore completed in ${DURATION}s (RTO)."
echo "==> [FLASHWEAR Restore] Running post-restore schema check..."
psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${TARGET_DB}" -c "SELECT count(*) AS applied_migrations FROM django_migrations;"

echo "==> [FLASHWEAR Restore] Disaster recovery procedure completed successfully."
