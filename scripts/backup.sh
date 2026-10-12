#!/usr/bin/env bash
# ==============================================================================
# FLASHWEAR Automated Database Backup Script
#
# Creates a timestamped, integrity-verified PostgreSQL custom dump.
# Enforces safe password handling, restrictive permissions (0600),
# and configurable retention cleanup.
#
# Usage:
#   ./scripts/backup.sh
#   BACKUP_RETENTION_DAYS=14 BACKUP_DIR=/mnt/backups ./scripts/backup.sh
# ==============================================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
PREFIX="flashwear_backup"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
FILENAME="${PREFIX}_${TIMESTAMP}.dump"
TARGET="${BACKUP_DIR}/${FILENAME}"

mkdir -p "${BACKUP_DIR}"

echo "==> [FLASHWEAR Backup] Target: ${TARGET}"

# If running inside the Django container, use the management command
if command -v python >/dev/null 2>&1 && [ -f "manage.py" ]; then
    python manage.py backup_db --backup-dir="${BACKUP_DIR}" --retention-days="${BACKUP_RETENTION_DAYS}"
    exit 0
fi

# Standalone / Docker host execution fallback
POSTGRES_DB="${POSTGRES_DB:-flashwear}"
POSTGRES_USER="${POSTGRES_USER:-flashwear}"
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    echo "ERROR: POSTGRES_PASSWORD environment variable is required for standalone backup." >&2
    exit 1
fi

export PGPASSWORD="${POSTGRES_PASSWORD}"

echo "==> [FLASHWEAR Backup] Running pg_dump..."
pg_dump \
    -h "${POSTGRES_HOST}" \
    -p "${POSTGRES_PORT}" \
    -U "${POSTGRES_USER}" \
    -d "${POSTGRES_DB}" \
    -Fc \
    --no-owner \
    --no-privileges \
    -f "${TARGET}"

# Restrict permissions
chmod 0600 "${TARGET}"

# Integrity verification
echo "==> [FLASHWEAR Backup] Verifying archive integrity..."
if [ ! -s "${TARGET}" ]; then
    echo "ERROR: Backup file is empty!" >&2
    rm -f "${TARGET}"
    exit 1
fi

pg_restore -l "${TARGET}" >/dev/null

echo "==> [FLASHWEAR Backup] Backup successfully created and verified: ${TARGET}"

# Prune older backups
if [ "${BACKUP_RETENTION_DAYS}" -gt 0 ]; then
    echo "==> [FLASHWEAR Backup] Pruning backups older than ${BACKUP_RETENTION_DAYS} days..."
    find "${BACKUP_DIR}" -name "${PREFIX}_*.dump" -type f -mtime "+${BACKUP_RETENTION_DAYS}" -delete || true
fi
