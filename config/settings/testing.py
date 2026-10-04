"""Settings used by the automated test suite.

Tests are fast and hermetic by default: an in-memory SQLite database, an in-process
locmem cache and no Celery eager execution required. Point ``TEST_DATABASE_URL`` at a
throwaway PostgreSQL database when you want to exercise PostgreSQL-specific behaviour.
"""

from .base import BASE_DIR, env
from .development import *

DEBUG = False

# Never leak the developer's ALLOWED_HOSTS into tests.
ALLOWED_HOSTS = ["testserver", "localhost"]

SECRET_KEY = "test-secret-key-not-used-in-production"

DATABASES = {
    "default": env.db_url(
        "TEST_DATABASE_URL",
        default="sqlite:///:memory:",
    )
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "flashwear-tests",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True

MEDIA_ROOT = BASE_DIR / "test-media"

# Tests log to the console only; no rotating log files are written to disk.
# ``tests/test_infrastructure.py`` asserts the production shape of the logging
# configuration against ``config.settings.base``.
for _handler in ("app_file", "django_file", "security_file"):
    LOGGING["handlers"][_handler] = {
        "class": "logging.StreamHandler",
        "formatter": LOGGING["handlers"][_handler]["formatter"],
    }

LOGGING["loggers"]["django.request"]["handlers"] = ["console"]
LOGGING["loggers"]["django.security"]["handlers"] = ["console"]
LOGGING["loggers"]["django.security"]["level"] = "ERROR"

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Deterministic fake provider: tests exercise the full payment pipeline (including
# signed webhooks) without any network access, and the signing secret is fixed so
# fixtures can build valid signatures.
PAYMENT_PROVIDER = "development"
PAYMENT_WEBHOOK_SECRET = "test-webhook-secret"
