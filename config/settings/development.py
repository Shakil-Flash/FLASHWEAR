"""Local development settings.

Deliberately developer friendly: verbose errors, local media serving and an in-process
cache so the app boots without Redis or PostgreSQL running. PostgreSQL via Docker Compose
remains the recommended local database (see README).
"""

import copy

from .base import *
from .base import LOGGING as BASE_LOGGING
from .base import REDIS_URL, USE_REDIS, env

DEBUG = env.bool("DEBUG", default=True)

ALLOWED_HOSTS = env.list(
    "ALLOWED_HOSTS",
    default=[
        "localhost",
        "127.0.0.1",
        "[::1]",
        "0.0.0.0",
        "testserver",
        "web",
        ".ngrok-free.dev",
        ".ngrok.io",
        ".ngrok-free.app",
    ],
)
for _host in (".ngrok-free.dev", ".ngrok.io", ".ngrok-free.app"):
    if _host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_host)

CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[
        "https://*.ngrok-free.dev",
        "https://*.ngrok.io",
        "https://*.ngrok-free.app",
        "http://*.ngrok-free.dev",
        "http://*.ngrok.io",
        "http://*.ngrok-free.app",
    ],
)
for _origin in (
    "https://*.ngrok-free.dev",
    "https://*.ngrok.io",
    "https://*.ngrok-free.app",
):
    if _origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_origin)

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Serve assets straight from the source tree, no collectstatic required.
WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# Payments: the deterministic, network-free provider unless explicitly overridden.
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", default="development")
if not env("PAYMENT_WEBHOOK_SECRET", default=""):
    # Local webhooks still have to be signed and verified; fall back to the project secret
    # so the flow works without ceremony. Production requires an explicit secret.
    PAYMENT_WEBHOOK_SECRET = SECRET_KEY

# Development logs to the console only (no log files written to disk).
# ``copy.deepcopy`` keeps the shared ``base.LOGGING`` dict intact so that tests can
# assert the production shape of the logging configuration.
LOGGING = copy.deepcopy(BASE_LOGGING)
for _handler in ("app_file", "django_file", "security_file"):
    LOGGING["handlers"][_handler] = {
        "class": "logging.StreamHandler",
        "formatter": LOGGING["handlers"][_handler]["formatter"],
    }

if USE_REDIS:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "flashwear-dev",
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "IGNORE_EXCEPTIONS": True,
            },
        }
    }
