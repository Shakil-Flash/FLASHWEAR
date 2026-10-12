#!/bin/sh
set -e

# Determine whether this container is acting as the primary web application
COMMAND="$1"
IS_WEB=0
case "$COMMAND" in
    gunicorn|runserver|python)
        IS_WEB=1
        ;;
    sh|bash)
        if echo "$*" | grep -q "gunicorn\|runserver\|config.wsgi"; then
            IS_WEB=1
        fi
        ;;
esac

# Auto-migrate: run if AUTO_MIGRATE=true, or if primary web service and not explicitly disabled
if [ "$AUTO_MIGRATE" = "true" ] || [ "$AUTO_MIGRATE" = "1" ] || ( [ "$IS_WEB" = "1" ] && [ "$AUTO_MIGRATE" != "false" ] && [ "$AUTO_MIGRATE" != "0" ] ); then
    echo "==> Running database migrations..."
    python manage.py migrate --noinput
else
    echo "==> Skipping automatic database migrations (AUTO_MIGRATE=${AUTO_MIGRATE:-false})."
fi

# Seeding demo data: NEVER run automatically in production.
# Only run if explicitly requested (SEED_DEMO_DATA=true) or in development when not opted out.
if [ "$SEED_DEMO_DATA" = "true" ] || [ "$SEED_DEMO_DATA" = "1" ] || ( [ "$DJANGO_SETTINGS_MODULE" = "config.settings.development" ] && [ "$SEED_DEMO_DATA" != "false" ] && [ "$SEED_DEMO_DATA" != "0" ] ); then
    echo "==> Seeding demo catalog..."
    python manage.py seed_catalog --with-images || true
fi

echo "==> Starting application..."
exec "$@"
