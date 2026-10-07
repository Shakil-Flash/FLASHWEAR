#!/bin/sh
set -e

echo "==> Running database migrations..."
python manage.py migrate --noinput

echo "==> Seeding demo catalog..."
python manage.py seed_catalog --with-images || true

echo "==> Starting application..."
exec "$@"
