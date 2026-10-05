"""Production settings.

Fails loudly and early when the environment is misconfigured. There is no SQLite
fallback here on purpose: production must run on PostgreSQL.
"""

import copy

from django.core.exceptions import ImproperlyConfigured

from .base import *
from .base import LOG_DIR, REDIS_URL, env
from .base import LOGGING as BASE_LOGGING

DEBUG = False

SECRET_KEY = env("SECRET_KEY", default="")
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY must be set in the environment for production.")

if SECRET_KEY.startswith("django-insecure"):
    raise ImproperlyConfigured("Refusing to start production with the development SECRET_KEY.")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be set in the environment for production.")

CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
if not CSRF_TRUSTED_ORIGINS:
    raise ImproperlyConfigured("CSRF_TRUSTED_ORIGINS must be set in the environment.")

# --------------------------------------------------------------------------------------
# Database: PostgreSQL only
# --------------------------------------------------------------------------------------

_default_db = DATABASES["default"]
if not _default_db.get("ENGINE", "").endswith("postgresql"):
    raise ImproperlyConfigured(
        "Production requires PostgreSQL. Set DATABASE_URL to a postgres:// URL."
    )

_default_db.setdefault("CONN_MAX_AGE", env.int("CONN_MAX_AGE", default=60))
_default_db["OPTIONS"].setdefault("sslmode", env("DB_SSLMODE", default="prefer"))

# --------------------------------------------------------------------------------------
# Redis is mandatory in production (cache + Celery transport)
# --------------------------------------------------------------------------------------

if not REDIS_URL:
    raise ImproperlyConfigured("REDIS_URL must be set in the environment for production.")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env("CACHE_URL", default=REDIS_URL),
        "KEY_PREFIX": env("CACHE_KEY_PREFIX", default="flashwear"),
        "TIMEOUT": env.int("CACHE_TTL_SECONDS", default=300),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": False,
        },
    }
}

SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=60 * 60 * 24 * 14)
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

# --------------------------------------------------------------------------------------
# Payments: the built-in development provider must never reach production
# --------------------------------------------------------------------------------------

if PAYMENT_PROVIDER in ("", "development"):
    raise ImproperlyConfigured(
        "PAYMENT_PROVIDER must name a real provider in production "
        "(the built-in 'development' provider only simulates payments)."
    )
if not PAYMENT_WEBHOOK_SECRET:
    raise ImproperlyConfigured("PAYMENT_WEBHOOK_SECRET must be set in the environment.")

# --------------------------------------------------------------------------------------
# Security hardening
# --------------------------------------------------------------------------------------

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = env.bool("USE_X_FORWARDED_HOST", default=True)
USE_X_FORWARDED_PORT = env.bool("USE_X_FORWARDED_PORT", default=True)

SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 365)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=True)

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = env("SESSION_COOKIE_SAMESITE", default="Lax")

CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = False  # the JS stack must read it for HTMX/AJAX requests
CSRF_COOKIE_SAMESITE = env("CSRF_COOKIE_SAMESITE", default="Lax")

X_FRAME_OPTIONS = "DENY"

SECURE_SERIALIZATION_COOKIE = env.bool("SECURE_SERIALIZATION_COOKIE", default=True)

# CSP: the policy itself lives in base.py so every environment renders the same
# directives; production is the only environment that *enforces* it by default.
# CSP_REPORT_ONLY=True watches violations without blocking (recommended for a first
# deployment, then flipped off).
CSP_REPORT_ONLY = env.bool("CSP_REPORT_ONLY", default=False)

# --------------------------------------------------------------------------------------
# Static assets / optional object storage
# --------------------------------------------------------------------------------------

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

if env("STORAGE_BACKEND", default="local").lower() in {"s3", "minio", "object"}:
    if not env("AWS_STORAGE_BUCKET_NAME", default=""):
        raise ImproperlyConfigured("AWS_STORAGE_BUCKET_NAME is required for S3 media storage.")

    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage"}
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="eu-west-1")
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default=None)
    AWS_S3_CUSTOM_DOMAIN = env("AWS_S3_CUSTOM_DOMAIN", default=None)
    AWS_QUERYSTRING_AUTH = env.bool("AWS_QUERYSTRING_AUTH", default=False)
    # Immutable object keys: a new upload must replace the file at the same path.
    AWS_S3_FILE_OVERWRITE = False
    AWS_DEFAULT_ACL = None

# --------------------------------------------------------------------------------------
# Email / CORS
# --------------------------------------------------------------------------------------

EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
if EMAIL_BACKEND.endswith("smtp.EmailBackend") and not env("EMAIL_HOST", default=""):
    raise ImproperlyConfigured(
        "EMAIL_HOST must be set in the environment when the SMTP email backend is used. "
        "Transactional email would otherwise fail silently at the first order."
    )

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_ALL_ORIGINS = False
CORS_URLS_REGEX = r"^/api/.*$"

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

# Deep copy: ``base.LOGGING`` is a module-level dict shared with the other settings
# modules, and mutating it here would leak production handlers into them.
LOGGING = copy.deepcopy(BASE_LOGGING)

# Production keeps more history than the development default. Both are overridable via
# the environment, so a deployment with an external log shipper can set them to 0.
for _handler in ("app_file", "django_file", "security_file"):
    LOGGING["handlers"][_handler]["class"] = "logging.handlers.RotatingFileHandler"
    LOGGING["handlers"][_handler]["maxBytes"] = env.int("LOG_MAX_BYTES", default=25 * 1024 * 1024)
    LOGGING["handlers"][_handler]["backupCount"] = env.int("LOG_BACKUP_COUNT", default=10)
LOGGING["handlers"]["security_file"]["backupCount"] = env.int(
    "SECURITY_LOG_BACKUP_COUNT", default=50
)

LOG_DIR.mkdir(parents=True, exist_ok=True)

# Structured logs by default: production output is consumed by a log shipper, not a
# terminal. Setting LOG_FORMAT=plain opts back into the human-readable format.
LOG_FORMAT = env("LOG_FORMAT", default="json")
if LOG_FORMAT == "json":
    for _handler in ("console", "app_file", "django_file", "security_file"):
        LOGGING["handlers"][_handler]["formatter"] = "json"
