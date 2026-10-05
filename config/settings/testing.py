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

# Support evidence is written by the model's own storage (not STORAGES["default"]), so it
# needs its own test override: without it every upload test would leave files in
# ``private/support`` inside the checkout.
SUPPORT_ATTACHMENT_ROOT = BASE_DIR / "test-media" / "support"

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

# The suite makes thousands of API calls from one address inside a minute; the real
# budgets would 429 the tests themselves. Throttle *behaviour* is tested by
# overriding these rates down (or using the per-view throttle classes directly), so
# raising them here hides nothing.
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"].update(
    {
        "anon": "100000/min",
        "user": "100000/min",
        "sensitive": "100000/min",
        "expensive": "100000/min",
    }
)

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Deterministic fake provider: tests exercise the full payment pipeline (including
# signed webhooks) without any network access, and the signing secret is fixed so
# fixtures can build valid signatures.
PAYMENT_PROVIDER = "development"
PAYMENT_WEBHOOK_SECRET = "test-webhook-secret"
