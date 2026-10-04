"""Environment-agnostic settings shared by every FLASHWEAR runtime.

Anything environment specific is read from the environment (optionally seeded from a
local ``.env`` file). Secrets must never be hard-coded here.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import environ

# repo-root/config/settings/base.py -> repo-root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

_ENV_FILE = BASE_DIR / ".env"
if _ENV_FILE.exists():
    env.read_env(str(_ENV_FILE), overwrite=False)


def env_bool(name: str, default: bool = False) -> bool:
    return env.bool(name, default=default)


# --------------------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------------------

SECRET_KEY = env(
    "SECRET_KEY",
    default="django-insecure-development-only-key-change-me-in-production",
)

DEBUG = env.bool("DEBUG", default=False)

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]"])

# Trust the proxy/load-balancer in front of the app when behind one.
USE_X_FORWARDED_HOST = env.bool("USE_X_FORWARDED_HOST", default=False)
USE_X_FORWARDED_PORT = env.bool("USE_X_FORWARDED_PORT", default=False)

CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"

APPEND_SLASH = True

# --------------------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.catalog",
    "apps.shop",
    # Phase 6: orders, inventory and payments. Orders sits between the shop
    # (checkout handoff) and inventory/payments, which both hang off it.
    "apps.orders",
    "apps.inventory",
    "apps.payments",
]

INSTALLED_APPS = [*LOCAL_APPS, *THIRD_PARTY_APPS, *DJANGO_APPS]

SITE_ID = 1

# --------------------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# --------------------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site",
                "apps.shop.context_processors.cart_summary",
            ],
            "builtins": ["apps.core.templatetags.flashwear"],
        },
    },
]

# --------------------------------------------------------------------------------------
# Database (PostgreSQL is the intended production database)
# --------------------------------------------------------------------------------------

DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}

_default_db = DATABASES["default"]
if _default_db.get("ENGINE", "").endswith("postgresql"):
    _default_db.setdefault("CONN_MAX_AGE", env.int("CONN_MAX_AGE", default=60))
    _default_db.setdefault("CONN_HEALTH_CHECKS", True)
    _default_db.setdefault("OPTIONS", {})
    _default_db["OPTIONS"].setdefault("connect_timeout", env.int("DB_CONNECT_TIMEOUT", default=10))

# --------------------------------------------------------------------------------------
# Redis / cache / Celery
# --------------------------------------------------------------------------------------

REDIS_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/0")


def _with_redis_db(url: str, db: int) -> str:
    """Point a Redis URL at a specific logical database.

    ``redis://cache:6379/5`` becomes ``redis://cache:6379/2`` and a URL with no database path at
    all gains one. Parsing rather than string surgery is what keeps credentials and query
    arguments (``ssl_cert_reqs`` and friends) from being dropped.
    """
    parsed = urlsplit(url)
    return urlunsplit(parsed._replace(path=f"/{db}"))


def _redis_url_for(name: str, db: int) -> str:
    """Resolve one Redis purpose, falling back to ``REDIS_URL`` on its own database.

    Cache, broker and results each get a distinct logical database so a ``FLUSHDB`` or an
    eviction policy on one cannot take the others down with it. An explicit ``CACHE_URL`` /
    ``CELERY_BROKER_URL`` / ``CELERY_RESULT_BACKEND`` is always used verbatim.
    """
    explicit = env(name, default="")
    if explicit:
        return explicit
    return _with_redis_db(REDIS_URL, db)


# Redis is used for cache + Celery outside of local single-process development.
USE_REDIS = env.bool("USE_REDIS", default=not DEBUG)

CACHE_URL = _redis_url_for("CACHE_URL", 1)
CELERY_BROKER_URL = _redis_url_for("CELERY_BROKER_URL", 2)
CELERY_RESULT_BACKEND = _redis_url_for("CELERY_RESULT_BACKEND", 3)

if USE_REDIS:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": CACHE_URL,
            "KEY_PREFIX": env("CACHE_KEY_PREFIX", default="flashwear"),
            "TIMEOUT": env.int("CACHE_TTL_SECONDS", default=300),
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                # Cache failures must degrade performance, never availability.
                "IGNORE_EXCEPTIONS": env.bool("CACHE_IGNORE_EXCEPTIONS", default=True),
            },
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "flashwear-local",
        }
    }

SESSION_ENGINE = "django.contrib.sessions.backends.db"

CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = env("TZ", default="UTC")
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=300)
CELERY_TASK_SOFT_TIME_LIMIT = env.int("CELERY_TASK_SOFT_TIME_LIMIT", default=240)
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# How often (seconds) the sweeper returns abandoned holds to the shop floor.
# Kept as a named setting so operators, the beat schedule and the tests all read
# the same number; hold lifetime itself is INVENTORY_RESERVATION_MINUTES below.
INVENTORY_SWEEP_INTERVAL_SECONDS = env.int("INVENTORY_SWEEP_INTERVAL_SECONDS", default=60)

# Periodic work (requires `celery beat` alongside the worker). One schedule so far:
# give expired checkout holds back to the shop floor.
CELERY_BEAT_SCHEDULE = {
    "inventory-sweep-expired-reservations": {
        "task": "inventory.sweep_expired_reservations",
        "schedule": INVENTORY_SWEEP_INTERVAL_SECONDS,
    },
}

# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = ["apps.accounts.backends.EmailBackend"]

# A path rather than a route name on purpose: it keeps resolving for callers that have no
# URLconf handy (DRF, Celery tasks, management commands).
LOGIN_URL = env("LOGIN_URL", default="/accounts/login/")
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "core:home"

# Failed sign-in limiting for the storefront login form. The API surface is throttled by DRF;
# the HTML form needs its own guard so /accounts/login/ is not a password-guessing oracle.
LOGIN_MAX_FAILURES_PER_ACCOUNT = env.int("LOGIN_MAX_FAILURES_PER_ACCOUNT", default=5)
LOGIN_MAX_FAILURES_PER_IP = env.int("LOGIN_MAX_FAILURES_PER_IP", default=20)
LOGIN_FAILURE_WINDOW_SECONDS = env.int("LOGIN_FAILURE_WINDOW_SECONDS", default=300)

# Whether the per-IP failure counter may trust X-Forwarded-For. Off by default because the header
# is client-controlled: with it on, anyone could mint a fresh counter per request and brute force
# one account unchecked. Turn it on only behind a proxy that overwrites the header for every
# request -- otherwise the proxy's own address becomes the "IP" and the counter becomes global.
USE_X_FORWARDED_FOR = env.bool("USE_X_FORWARDED_FOR", default=False)

# "Keep me signed in" on the login form. Deliberately longer than SESSION_COOKIE_AGE, so
# unticking the box actually changes something: a session the customer did not ask to be
# remembered ends sooner than one they did.
ACCOUNT_REMEMBER_ME_DAYS = env.int("ACCOUNT_REMEMBER_ME_DAYS", default=30)

# Django's signed-token generator is shared by password reset and email verification; this is
# how long either link stays valid.
PASSWORD_RESET_TIMEOUT = env.int("PASSWORD_RESET_TIMEOUT", default=3 * 24 * 60 * 60)

# Absolute base URL used inside account emails. Empty means "derive it from the incoming
# request", which is right for a single-host deployment and for the console email backend.
# Set it explicitly (https://flashwear.com) when mail is queued and sent by a worker that has
# no request to derive a host from.
ACCOUNT_EMAIL_BASE_URL = env("ACCOUNT_EMAIL_BASE_URL", default="")

# Uploaded avatars are validated by content, not by file name. These are the hard limits.
ACCOUNT_AVATAR_MAX_BYTES = env.int("ACCOUNT_AVATAR_MAX_BYTES", default=2 * 1024 * 1024)
ACCOUNT_AVATAR_MAX_PIXELS = env.int("ACCOUNT_AVATAR_MAX_PIXELS", default=4096)
# SVG is deliberately absent: an SVG is a script container, and sanitising one is out of scope.
ACCOUNT_AVATAR_ALLOWED_FORMATS = tuple(
    item.strip().upper()
    for item in env.list("ACCOUNT_AVATAR_ALLOWED_FORMATS", default=["JPEG", "PNG", "WEBP"])
)

# Deactivated accounts keep their rows because orders, returns and payment records will depend
# on them. This is how long a deactivated account waits before an operator-driven anonymisation
# job may scrub the personal data.
ACCOUNT_DATA_RETENTION_DAYS = env.int("ACCOUNT_DATA_RETENTION_DAYS", default=30)

# --------------------------------------------------------------------------------------
# Catalog (Phase 3)
# --------------------------------------------------------------------------------------

# Product imagery is validated by content with Pillow before it is stored, exactly like avatars.
# The limits are higher than the avatar limits because a product shot is a large JPEG; the pixel
# ceiling is what actually stops a decompression bomb.
CATALOG_IMAGE_MAX_BYTES = env.int("CATALOG_IMAGE_MAX_BYTES", default=6 * 1024 * 1024)
CATALOG_IMAGE_MAX_PIXELS = env.int("CATALOG_IMAGE_MAX_PIXELS", default=6000)
# SVG is deliberately absent for the same reason as on avatars: an SVG is a script container, and
# it would be served from our own origin.
CATALOG_IMAGE_ALLOWED_FORMATS = tuple(
    item.strip().upper()
    for item in env.list("CATALOG_IMAGE_ALLOWED_FORMATS", default=["JPEG", "PNG", "WEBP"])
)

# Server-rendered storefront grid size, and the API defaults. The API caps ``?page_size`` at
# ``CATALOG_API_MAX_PAGE_SIZE`` so a client cannot ask for the whole catalog in one response.
CATALOG_PRODUCTS_PER_PAGE = env.int("CATALOG_PRODUCTS_PER_PAGE", default=12)
CATALOG_API_PAGE_SIZE = env.int("CATALOG_API_PAGE_SIZE", default=24)
CATALOG_API_MAX_PAGE_SIZE = env.int("CATALOG_API_MAX_PAGE_SIZE", default=100)

# How many sibling products the product page offers underneath the main one. "You may also like"
# is merchandising, not search, so it stays a small, cheap query.
CATALOG_RELATED_PRODUCTS_LIMIT = env.int("CATALOG_RELATED_PRODUCTS_LIMIT", default=4)

# Phase 3 prices are plain Decimal amounts with no per-product currency column: one storefront
# trades in one currency until multi-currency pricing arrives with checkout. A customer can still
# record a display preference in their profile (Phase 2).
CATALOG_CURRENCY_CODE = env("CATALOG_CURRENCY_CODE", default="USD")
# Derived rather than configured separately: a symbol that disagreed with the code is a bug waiting
# to happen ("$" next to a BDT price). An unmapped code falls back to the code itself.
CATALOG_CURRENCY_SYMBOL = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "BDT": "৳",
    "INR": "₹",
    "AED": "AED ",
}.get(CATALOG_CURRENCY_CODE, f"{CATALOG_CURRENCY_CODE} ")

# --------------------------------------------------------------------------------------
# Cart and checkout (Phase 5)
# --------------------------------------------------------------------------------------

# Cart guard rail: the most of one variant a single bag may hold. Inventory proper is
# Phase 6; this only stops absurd quantities reaching checkout.
CART_MAX_QUANTITY_PER_ITEM = env.int("CART_MAX_QUANTITY_PER_ITEM", default=99)

# Shipping rates are settings, not rows: a carrier integration is a later phase behind
# the same apps.shop.shipping interface. Standard delivery is free over the threshold.
CHECKOUT_SHIPPING_FLAT_RATE = Decimal(env("CHECKOUT_SHIPPING_FLAT_RATE", default="5.00"))
CHECKOUT_SHIPPING_FREE_OVER = Decimal(env("CHECKOUT_SHIPPING_FREE_OVER", default="100.00"))
CHECKOUT_SHIPPING_EXPRESS_RATE = Decimal(env("CHECKOUT_SHIPPING_EXPRESS_RATE", default="15.00"))

# Depth is not capped: the category tree is arbitrary and validated only for cycles (see
# ``Category.check_tree_integrity``). The storefront presents two levels because that is what a
# shopper navigates by, not because the schema cannot hold more.

# --------------------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------------------

LANGUAGE_CODE = env("LANGUAGE_CODE", default="en-us")
TIME_ZONE = env("TZ", default="UTC")
USE_I18N = True
USE_TZ = True

# Languages a customer can pick in their profile. Django's own LANGUAGES list is deliberately
# not used as the choice set: it includes admin-only and construction locales.
ACCOUNT_LANGUAGES = [
    ("en-us", "English (US)"),
    ("en-gb", "English (UK)"),
    ("de", "Deutsch"),
    ("fr", "Francais"),
    ("it", "Italiano"),
    ("es", "Espanol"),
]

# Currencies offered as a display preference. This is presentation only: no Phase 2 code does
# money maths, and the order currency is decided by the (future) pricing service.
ACCOUNT_CURRENCIES = [
    ("USD", "USD - US Dollar"),
    ("EUR", "EUR - Euro"),
    ("GBP", "GBP - British Pound"),
    ("TRY", "TRY - Turkish Lira"),
]

# --------------------------------------------------------------------------------------
# Static files and media
# --------------------------------------------------------------------------------------

STATIC_URL = env("STATIC_URL", default="/static/")
STATIC_ROOT = Path(env("STATIC_ROOT", default=str(BASE_DIR / "staticfiles")))
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = env("MEDIA_URL", default="/media/")
MEDIA_ROOT = Path(env("MEDIA_ROOT", default=str(BASE_DIR / "media")))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# --------------------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("API_ANON_THROTTLE_RATE", default="60/min"),
    },
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": env.int("API_PAGE_SIZE", default=24),
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

if DEBUG:
    # The browsable API is a development-only convenience.
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ]
else:
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = ["rest_framework.renderers.JSONRenderer"]

# --------------------------------------------------------------------------------------
# CORS (allows a future mobile app / separate SPA frontend to call the API)
# --------------------------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = env.bool("CORS_ALLOW_CREDENTIALS", default=True)
CORS_URLS_REGEX = r"^/api/.*$"

# --------------------------------------------------------------------------------------
# Email (Phase 1: configuration only)
# --------------------------------------------------------------------------------------

EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="FLASHWEAR <no-reply@flashwear.local>")
SERVER_EMAIL = env("SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)

# --------------------------------------------------------------------------------------
# Inventory (Phase 6)
# --------------------------------------------------------------------------------------

# How long a checkout session may hold reserved stock before the sweeper gives it back.
# Longer than a human takes to fill in an address form, short enough that abandoned
# checkouts do not lock stock for the day.
INVENTORY_RESERVATION_MINUTES = env.int("INVENTORY_RESERVATION_MINUTES", default=30)

# --------------------------------------------------------------------------------------
# Payments (Phase 6)
# --------------------------------------------------------------------------------------

# The provider is an interface (``apps.payments.providers``); "development" is a built-in,
# deterministic fake that never talks to the network. Production refuses to start with it
# (see ``config.settings.production``).
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", default="")
PAYMENT_SECRET_KEY = env("PAYMENT_SECRET_KEY", default="")
PAYMENT_WEBHOOK_SECRET = env("PAYMENT_WEBHOOK_SECRET", default="")

# Signed webhooks carry a timestamp; deliveries outside this window are rejected as
# replays. Generous because a provider retry may sit in a queue for a while.
PAYMENT_WEBHOOK_TOLERANCE_SECONDS = env.int("PAYMENT_WEBHOOK_TOLERANCE_SECONDS", default=300)

STORAGE_BACKEND = env("STORAGE_BACKEND", default="local")

AI_PROVIDER = env("AI_PROVIDER", default="")
AI_API_KEY = env("AI_API_KEY", default="")
AI_EMBEDDING_MODEL = env("AI_EMBEDDING_MODEL", default="")

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

LOG_DIR = Path(env("LOG_DIR", default=str(BASE_DIR / "logs")))
LOG_LEVEL = env("LOG_LEVEL", default="INFO")

# Retention is configurable so a deployment can trade disk usage against history depth.
LOG_MAX_BYTES = env.int("LOG_MAX_BYTES", default=5 * 1024 * 1024)
LOG_BACKUP_COUNT = env.int("LOG_BACKUP_COUNT", default=5)
SECURITY_LOG_BACKUP_COUNT = env.int("SECURITY_LOG_BACKUP_COUNT", default=10)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
    },
    "formatters": {
        "verbose": {
            "()": "apps.core.logging_filters.RedactingFormatter",
            "format": "[{asctime}] {levelname} {name} {module}:{lineno} {message}",
            "style": "{",
        },
        "simple": {
            "()": "apps.core.logging_filters.RedactingFormatter",
            "format": "{message}",
            "style": "{",
        },
        "security": {
            "()": "apps.core.logging_filters.RedactingFormatter",
            "format": "[{asctime}] SECURITY {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "app_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "flashwear.log"),
            "maxBytes": LOG_MAX_BYTES,
            "backupCount": LOG_BACKUP_COUNT,
            "formatter": "verbose",
        },
        "django_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "django.log"),
            "maxBytes": LOG_MAX_BYTES,
            "backupCount": LOG_BACKUP_COUNT,
            "formatter": "verbose",
        },
        # The security stream keeps more history than the application stream.
        "security_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "security.log"),
            "maxBytes": LOG_MAX_BYTES,
            "backupCount": SECURITY_LOG_BACKUP_COUNT,
            "formatter": "security",
        },
        "mail_admins": {
            "class": "django.utils.log.AdminEmailHandler",
            "formatter": "verbose",
            "filters": ["require_debug_false"],
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console", "django_file"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "django_file", "mail_admins"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console", "security_file"],
            "level": "WARNING",
            "propagate": False,
        },
        "flashwear": {
            "handlers": ["console", "app_file"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "celery": {
            "handlers": ["console", "app_file"],
            "level": env("CELERY_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
    },
}

if not LOG_DIR.exists() and not env.bool("DISABLE_FILE_LOGGING", default=False):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:  # pragma: no cover - read-only filesystems (e.g. some containers)
        pass

ADMINS = [("Engineering", env("ADMIN_EMAIL", default="engineering@flashwear.local"))]
MANAGERS = ADMINS

# --------------------------------------------------------------------------------------
# Messages / i18n defaults used by the design system
# --------------------------------------------------------------------------------------

MESSAGE_TAGS = {
    10: "info",
    20: "info",
    25: "success",
    30: "warning",
    40: "danger",
}
