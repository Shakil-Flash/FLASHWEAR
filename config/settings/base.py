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
    "django.contrib.sitemaps",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    # Phase 17: notifications & customer communications -- a cross-cutting layer whose
    # only model dependency is the user; domains import its services when emitting, and
    # nothing below imports it back.
    "apps.notifications",
    "apps.catalog",
    "apps.shop",
    # Phase 6: orders, inventory and payments. Orders sits between the shop
    # (checkout handoff) and inventory/payments, which both hang off it.
    "apps.orders",
    "apps.inventory",
    "apps.payments",
    # Phase 7: reviews, FLASH Points and promotions. A leaf app: it reads the shop,
    # orders and catalogue but nothing above imports it directly (the checkout reaches
    # engagement through lazy service calls), which keeps the migration graph a tree.
    "apps.engagement",
    # Phase 8: the private wardrobe and outfit builder. Also a leaf, and deliberately
    # signal-free -- closet rows only ever appear through its own explicit services.
    "apps.styling",
    "apps.recommendations",
    "apps.drops",
    "apps.closet",
    # Phase 10: personalized discovery and recommendations.
    # Phase 12 / Phase 32: creator economy, community & shoppable UGC.
    "apps.creator",
    # Phase 13: FLASH Loop -- resale, trade-in and recycling. A leaf app: it reads
    # the catalogue, closet, orders and engagement but nothing above imports it.
    "apps.loop",
    # Phase 14: FLASH Quests & Rewards -- deterministic quests, progress and badges.
    # Also a leaf: it measures rows written by every app below it and writes only to its
    # own tables plus idempotent BONUS ledger entries.
    "apps.quests",
    # Phase 15: FLASH Support & Customer Care -- tickets, transcripts and the staff desk.
    # The last leaf: it references every domain above so a ticket can point at the row it
    # is about, and nothing imports it back.
    "apps.support",
    # Phase 20: analytics -- an append-only event log plus per-visitor attribution. Written
    # through its own service from every domain below (never the other way round), read by
    # the back office; only foreign key is the user.
    "apps.analytics",
    # Phase 16: FLASHWEAR Back Office -- /operations/. Reads every domain above and calls
    # their services; its only table is the staff audit log, and nothing imports it back.
    "apps.backoffice",
]




INSTALLED_APPS = [*LOCAL_APPS, *THIRD_PARTY_APPS, *DJANGO_APPS]

SITE_ID = 1

# --------------------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------------------

MIDDLEWARE = [
    # Outermost on purpose: every log line (including exceptions raised further in)
    # carries the correlation id this middleware mints, and it is the last middleware
    # to touch the response, so ``X-Request-ID`` is always present.
    "apps.core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # After SecurityMiddleware (so a TLS redirect never needs the header) and before
    # WhiteNoise (which short-circuits static files below this point).
    "apps.core.middleware.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After AuthenticationMiddleware: resolves the first-party visitor id cookie and persists
    # UTM/referrer attribution for storefront GETs. Never writes for /api/, /static/ or the
    # back office, and a failure degrades to "no analytics row", never a broken page.
    "apps.analytics.middleware.AnalyticsMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# --------------------------------------------------------------------------------------
# Content Security Policy
# --------------------------------------------------------------------------------------

# Baseline CSP: all first-party code (Tailwind build, vendored HTMX/Alpine, app.js) is
# served from this origin, so no third-party script or style source is required. The
# policy is attached by ``apps.core.middleware.ContentSecurityPolicyMiddleware``.
#
# Report-only everywhere except production: development must never block the Django
# debug page (which uses inline script), and staging rehearses templates against the
# policy before production enforces it (``config.settings.production`` flips the default).
CSP_REPORT_ONLY = env.bool("CSP_REPORT_ONLY", default=True)
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ["'self'"],
        "base-uri": ["'self'"],
        "object-src": ["'none'"],
        "frame-ancestors": ["'none'"],
        "form-action": ["'self'"],
        # Product and editorial imagery may be served from a CDN over HTTPS.
        "img-src": ["'self'", "data:", "https:"],
        "font-src": ["'self'", "data:"],
        # 'unsafe-inline' is required by Django form/error widgets; drop it if avoidable.
        "style-src": ["'self'", "'unsafe-inline'"],
        "script-src": ["'self'"],
        "connect-src": ["'self'"],
        "worker-src": ["'self'", "blob:"],
        "frame-src": ["'self'"],
        "manifest-src": ["'self'"],
        "upgrade-insecure-requests": [],
    }
}

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
                "apps.notifications.context_processors.unread_count",
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
    if "upstash.io" in REDIS_URL:
        return REDIS_URL
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

# Retry policy for anything that asks for a retry (``task.retry`` / ``autoretry_for``):
# bounded exponential backoff, so a transient Redis or SMTP blip does not burn the queue
# while a poison task cannot loop forever. Sweepers do not rely on this -- beat fires them
# again on the next interval regardless of how the previous run ended.
CELERY_TASK_DEFAULT_RETRY_DELAY = env.int("CELERY_TASK_DEFAULT_RETRY_DELAY", default=30)
CELERY_TASK_RETRY_BACKOFF = env.int("CELERY_TASK_RETRY_BACKOFF", default=2)
CELERY_TASK_RETRY_BACKOFF_MAX = env.int("CELERY_TASK_RETRY_BACKOFF_MAX", default=600)
CELERY_TASK_MAX_RETRIES = env.int("CELERY_TASK_MAX_RETRIES", default=3)
# Results are short-lived on purpose: nothing in the codebase polls the result backend
# (callers read database state or wait on ``transaction.on_commit``), so an hour of
# history is purely hygiene against unbounded Redis growth.
CELERY_RESULT_EXPIRES = env.int("CELERY_RESULT_EXPIRES", default=3600)

# How often (seconds) the sweeper returns abandoned holds to the shop floor.
# Kept as a named setting so operators, the beat schedule and the tests all read
# the same number; hold lifetime itself is INVENTORY_RESERVATION_MINUTES below.
INVENTORY_SWEEP_INTERVAL_SECONDS = env.int("INVENTORY_SWEEP_INTERVAL_SECONDS", default=60)

# --------------------------------------------------------------------------------------
# Engagement (Phase 7): FLASH Points economics and sweeper cadence
# --------------------------------------------------------------------------------------

# Earning: whole points per currency unit of (subtotal - discount) on a paid order.
# Shipping is not merchandise and redeemed points do not earn points.
LOYALTY_EARN_RATE = env.int("LOYALTY_EARN_RATE", default=10)
# Redemption: points per currency unit of discount, so 100 points = 1.00 off.
LOYALTY_REDEEM_RATE = env.int("LOYALTY_REDEEM_RATE", default=100)
# Customers redeem in these steps (100-point grid keeps the money maths whole).
LOYALTY_REDEEM_INCREMENT = env.int("LOYALTY_REDEEM_INCREMENT", default=100)
# Hard ceiling: at most this share of the eligible subtotal may be paid with points.
LOYALTY_MAX_REDEEM_PERCENT = Decimal(env("LOYALTY_MAX_REDEEM_PERCENT", default="50"))
# Below this eligible subtotal, points redemption is refused outright.
LOYALTY_MIN_ORDER_AMOUNT = Decimal(env("LOYALTY_MIN_ORDER_AMOUNT", default="0.00"))
# Earned points expire this many days after the order that earned them.
LOYALTY_EXPIRY_DAYS = env.int("LOYALTY_EXPIRY_DAYS", default=365)
# A checkout's points hold lives this long without being re-validated.
LOYALTY_RESERVATION_MINUTES = env.int("LOYALTY_RESERVATION_MINUTES", default=30)
# Review pagination: the product page shows this many per page; the API has its own size.
REVIEWS_PER_PAGE = env.int("REVIEWS_PER_PAGE", default=6)

# How often (seconds) the engagement sweeper releases stale points holds and expires due
# points. Same pattern as the inventory sweep: one named setting, one beat entry.
LOYALTY_SWEEP_INTERVAL_SECONDS = env.int("LOYALTY_SWEEP_INTERVAL_SECONDS", default=60)

# How often (seconds) the quest sweeper re-derives progress for users holding an active
# participation row -- the no-signal half of quest progress (see apps.quests.tasks).
QUESTS_SWEEP_INTERVAL_SECONDS = env.int("QUESTS_SWEEP_INTERVAL_SECONDS", default=300)

# --------------------------------------------------------------------------------------
# Notifications (Phase 17)
# --------------------------------------------------------------------------------------

# Delivery attempts for one email row before it fails permanently (back office can retry
# by hand after that). Transient failures back off exponentially from the base below.
NOTIFICATIONS_MAX_EMAIL_ATTEMPTS = env.int("NOTIFICATIONS_MAX_EMAIL_ATTEMPTS", default=3)
NOTIFICATIONS_RETRY_BACKOFF_SECONDS = env.int("NOTIFICATIONS_RETRY_BACKOFF_SECONDS", default=60)
NOTIFICATIONS_RETRY_MAX_SECONDS = env.int("NOTIFICATIONS_RETRY_MAX_SECONDS", default=3600)
# A queued row older than this with no outcome is presumed orphaned (worker died mid-send)
# and gets re-queued; the age gate is what stops a double-send while a task still runs.
NOTIFICATIONS_STUCK_SECONDS = env.int("NOTIFICATIONS_STUCK_SECONDS", default=600)

# Retention: read in-app rows age out (unread ones never do); terminal email rows outlive
# them so a year-old "was this sent?" audit question still has an answer.
NOTIFICATIONS_RETENTION_DAYS = env.int("NOTIFICATIONS_RETENTION_DAYS", default=90)
NOTIFICATIONS_EMAIL_RETENTION_DAYS = env.int("NOTIFICATIONS_EMAIL_RETENTION_DAYS", default=365)

# How far ahead the points-expiring sweeper warns before FLASH Points vanish.
NOTIFICATIONS_EXPIRING_WARNING_DAYS = env.int("NOTIFICATIONS_EXPIRING_WARNING_DAYS", default=7)
# Slice size for a broadcast fan-out (one task argument list per slice).
NOTIFICATIONS_BROADCAST_BATCH_SIZE = env.int("NOTIFICATIONS_BROADCAST_BATCH_SIZE", default=500)

# The delivery seam: "django" uses django.core.mail (console/locmem/SMTP); a hosted
# provider arrives as a second EmailProvider plus a value here.
NOTIFICATIONS_EMAIL_PROVIDER = env("NOTIFICATIONS_EMAIL_PROVIDER", default="django")

# Write limits for the notification endpoints (see apps.notifications.throttling).
NOTIFICATIONS_READ_ALL_MAX = env.int("NOTIFICATIONS_READ_ALL_MAX", default=10)
NOTIFICATIONS_PREFERENCES_MAX = env.int("NOTIFICATIONS_PREFERENCES_MAX", default=20)
NOTIFICATIONS_UNSUBSCRIBE_MAX = env.int("NOTIFICATIONS_UNSUBSCRIBE_MAX", default=10)
NOTIFICATIONS_THROTTLE_WINDOW_SECONDS = env.int(
    "NOTIFICATIONS_THROTTLE_WINDOW_SECONDS", default=300
)

# Sweep cadences (seconds): queue recovery/promotion, retention, drop announcements,
# points-expiry warnings.
NOTIFICATIONS_QUEUE_SWEEP_INTERVAL_SECONDS = env.int(
    "NOTIFICATIONS_QUEUE_SWEEP_INTERVAL_SECONDS", default=300
)
NOTIFICATIONS_RETENTION_SWEEP_INTERVAL_SECONDS = env.int(
    "NOTIFICATIONS_RETENTION_SWEEP_INTERVAL_SECONDS", default=21600
)
NOTIFICATIONS_DROP_SWEEP_INTERVAL_SECONDS = env.int(
    "NOTIFICATIONS_DROP_SWEEP_INTERVAL_SECONDS", default=300
)
NOTIFICATIONS_POINTS_SWEEP_INTERVAL_SECONDS = env.int(
    "NOTIFICATIONS_POINTS_SWEEP_INTERVAL_SECONDS", default=86400
)

# Cadences for the housekeeping tasks whose own sections sit further down the file
# (support, Loop) -- declared here so every beat interval exists before the schedule
# that reads it.
SUPPORT_SWEEP_INTERVAL_SECONDS = env.int("SUPPORT_SWEEP_INTERVAL_SECONDS", default=3600)
LOOP_SWEEP_INTERVAL_SECONDS = env.int("LOOP_SWEEP_INTERVAL_SECONDS", default=3600)

CELERY_BEAT_SCHEDULE = {
    "inventory-sweep-expired-reservations": {
        "task": "inventory.sweep_expired_reservations",
        "schedule": INVENTORY_SWEEP_INTERVAL_SECONDS,
    },
    "engagement-sweep-loyalty": {
        "task": "engagement.sweep_loyalty",
        "schedule": LOYALTY_SWEEP_INTERVAL_SECONDS,
    },
    "quests-sweep-progress": {
        "task": "quests.sweep_progress",
        "schedule": QUESTS_SWEEP_INTERVAL_SECONDS,
    },
    "notifications-sweep-queue": {
        "task": "notifications.sweep_queue",
        "schedule": NOTIFICATIONS_QUEUE_SWEEP_INTERVAL_SECONDS,
    },
    "notifications-sweep-retention": {
        "task": "notifications.sweep_retention",
        "schedule": NOTIFICATIONS_RETENTION_SWEEP_INTERVAL_SECONDS,
    },
    "notifications-sweep-drop-events": {
        "task": "notifications.sweep_drop_events",
        "schedule": NOTIFICATIONS_DROP_SWEEP_INTERVAL_SECONDS,
    },
    "notifications-sweep-points-expiring": {
        "task": "notifications.sweep_points_expiring",
        "schedule": NOTIFICATIONS_POINTS_SWEEP_INTERVAL_SECONDS,
    },
    # (when SUPPORT_SEND_REMINDERS is on) re-nudge tickets parked on the customer.
    "support-close-abandoned": {
        "task": "support.close_abandoned_tickets",
        "schedule": SUPPORT_SWEEP_INTERVAL_SECONDS,
    },
    "support-remind-pending": {
        "task": "support.remind_pending_tickets",
        "schedule": SUPPORT_SWEEP_INTERVAL_SECONDS,
    },
    # Loop housekeeping (Phase 13): expired listings and past-due credits. Without these
    # entries the tasks existed but nothing ever fired them.
    "loop-expire-stale-listings": {
        "task": "loop.expire_stale_listings",
        "schedule": LOOP_SWEEP_INTERVAL_SECONDS,
    },
    "loop-expire-credits": {
        "task": "loop.expire_loop_credits",
        "schedule": LOOP_SWEEP_INTERVAL_SECONDS,
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

# Django sends CSRF rejections straight to this view (not through handler403),
# so it must point at our branded 403 page instead of the technical default.
CSRF_FAILURE_VIEW = "apps.core.views.csrf_failure"

# Per-address attempt caps for mail-sending / sign-up endpoints
# (apps.accounts.throttling.AttemptThrottle). Counted regardless of outcome --
# the abuse is the volume itself (mailbox bombing, mass sign-ups).
ACCOUNT_ATTEMPT_MAX = env.int("ACCOUNT_ATTEMPT_MAX", default=10)
ACCOUNT_ATTEMPT_WINDOW_SECONDS = env.int("ACCOUNT_ATTEMPT_WINDOW_SECONDS", default=3600)

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

# Forward-compatibility: assume https for forms.URLField
FORMS_URLFIELD_ASSUME_HTTPS = True

# Server-rendered storefront grid size, and the API defaults. The API caps ``?page_size`` at
# ``CATALOG_API_MAX_PAGE_SIZE`` so a client cannot ask for the whole catalog in one response.
CATALOG_PRODUCTS_PER_PAGE = env.int("CATALOG_PRODUCTS_PER_PAGE", default=12)
CATALOG_API_PAGE_SIZE = env.int("CATALOG_API_PAGE_SIZE", default=24)
CATALOG_API_MAX_PAGE_SIZE = env.int("CATALOG_API_MAX_PAGE_SIZE", default=100)

# How many sibling products the product page offers underneath the main one. "You may also like"
# is merchandising, not search, so it stays a small, cheap query.
CATALOG_RELATED_PRODUCTS_LIMIT = env.int("CATALOG_RELATED_PRODUCTS_LIMIT", default=4)

# Visual discovery (Phase 22)
VISUAL_SEARCH_PROVIDER = env(
    "VISUAL_SEARCH_PROVIDER",
    default="apps.catalog.visual_discovery.LocalDeterministicVisualProvider",
)
VISUAL_SEARCH_MAX_RESULTS = env.int("VISUAL_SEARCH_MAX_RESULTS", default=24)

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
# FLASH Loop (Phase 13): resale, trade-in and recycling
# --------------------------------------------------------------------------------------

# Resale pricing guard rails. A seller proposes an asking price; it is validated
# against these bounds and against the project's money conventions (plain Decimal,
# two places, storefront currency). The platform never guarantees to buy at the
# asking price.
LOOP_RESALE_MIN_PRICE = Decimal(env("LOOP_RESALE_MIN_PRICE", default="1.00"))
LOOP_RESALE_MAX_PRICE = Decimal(env("LOOP_RESALE_MAX_PRICE", default="50000.00"))

# How many photos a seller may attach, and how long a listing stays active before
# the sweeper expires it (a Celery task, not a correctness dependency).
LOOP_MAX_IMAGES = env.int("LOOP_MAX_IMAGES", default=5)
LOOP_LISTING_TTL_DAYS = env.int("LOOP_LISTING_TTL_DAYS", default=90)

# Only in-house FLASHWEAR merchandise enters the Loop in this phase. A product is
# eligible when it carries no external brand label (in-house) or one of these brand
# slugs. Third-party goods are rejected with a clear message; marketplace support for
# them is a later phase.
LOOP_ELIGIBLE_BRAND_SLUGS = env.list("LOOP_ELIGIBLE_BRAND_SLUGS", default=["flashwear"])

# Deterministic trade-in valuation inputs (see ``apps.loop.services.valuation``).
# The estimate is a policy output, not an AI prediction and not a guarantee: the
# final credit is set by a reviewer after inspection.
LOOP_TRADE_IN_BASE_VALUE = Decimal(env("LOOP_TRADE_IN_BASE_VALUE", default="20.00"))
# An estimate may never exceed this fraction of the item's original unit price, so
# the policy cannot out-value the garment it is valuing.
LOOP_TRADE_IN_MAX_FRACTION_OF_PRICE = Decimal(
    env("LOOP_TRADE_IN_MAX_FRACTION_OF_PRICE", default="0.50")
)
# Age depreciation brackets: (age in days or None for "and everything older",
# multiplier). Applied to the purchase/add-to-closet date.
LOOP_TRADE_IN_AGE_BRACKETS = (
    (180, Decimal("1.00")),
    (365, Decimal("0.90")),
    (None, Decimal("0.80")),
)

# Recycling programme scope. A product is recyclable when any of its materials, or
# its most specific category slug, is listed here. Products outside the programme
# get a friendly, honest reason instead of a fake acceptance.
LOOP_RECYCLABLE_MATERIALS = env.list(
    "LOOP_RECYCLABLE_MATERIALS",
    default=[
        "organic-cotton",
        "cotton",
        "linen",
        "polyester",
        "recycled-polyester",
        "wool",
        "denim",
    ],
)
LOOP_RECYCLABLE_CATEGORIES = env.list(
    "LOOP_RECYCLABLE_CATEGORIES", default=["t-shirts", "hoodies", "jeans", "dresses"]
)

# --------------------------------------------------------------------------------------
# FLASH Support & Customer Care (Phase 15)
# --------------------------------------------------------------------------------------

# Attachments are validated by content, never by name (the same rule as avatars and
# product imagery). SVG and HTML are absent on purpose: they are script containers, and a
# ticket has no legitimate reason to carry one.
SUPPORT_ATTACHMENT_MAX_BYTES = env.int("SUPPORT_ATTACHMENT_MAX_BYTES", default=5 * 1024 * 1024)
SUPPORT_ATTACHMENT_MAX_PIXELS = env.int("SUPPORT_ATTACHMENT_MAX_PIXELS", default=8000)
SUPPORT_ATTACHMENT_ALLOWED_EXTENSIONS = env.list(
    "SUPPORT_ATTACHMENT_ALLOWED_EXTENSIONS",
    default=[".jpg", ".jpeg", ".png", ".webp", ".pdf", ".txt"],
)

# Where ticket evidence is stored. Deliberately **outside** ``MEDIA_ROOT``: the web server
# serves MEDIA directly, and screenshots of a customer's order are not public assets. The
# download route (/support/attachments/<pk>/) is the only reader, and it checks ownership.
SUPPORT_ATTACHMENT_ROOT = env(
    "SUPPORT_ATTACHMENT_ROOT", default=str(BASE_DIR / "private" / "support")
)

# Per-user write limits for the help desk. Tickets are limited hard (queue flooding) and
# messages more loosely (a real conversation can be long, an abuse run cannot wait 5
# minutes between sends).
SUPPORT_MAX_TICKETS_PER_WINDOW = env.int("SUPPORT_MAX_TICKETS_PER_WINDOW", default=5)
SUPPORT_MAX_MESSAGES_PER_WINDOW = env.int("SUPPORT_MAX_MESSAGES_PER_WINDOW", default=30)
SUPPORT_MAX_ATTACHMENTS_PER_MESSAGE = env.int("SUPPORT_MAX_ATTACHMENTS_PER_MESSAGE", default=5)
SUPPORT_MESSAGE_WINDOW_SECONDS = env.int("SUPPORT_MESSAGE_WINDOW_SECONDS", default=300)
SUPPORT_TICKETS_PER_PAGE = env.int("SUPPORT_TICKETS_PER_PAGE", default=20)

# Housekeeping windows for the support Celery tasks. Both tasks are conveniences: no
# correctness depends on a worker being up.
SUPPORT_CLOSE_AFTER_DAYS = env.int("SUPPORT_CLOSE_AFTER_DAYS", default=14)
SUPPORT_REMIND_AFTER_DAYS = env.int("SUPPORT_REMIND_AFTER_DAYS", default=3)
# Off by default: sending from a worker needs ACCOUNT_EMAIL_BASE_URL configured, and a
# guessed link is worse than no nudge at all.
SUPPORT_SEND_REMINDERS = env.bool("SUPPORT_SEND_REMINDERS", default=False)

# --------------------------------------------------------------------------------------
# FLASHWEAR Back Office (Phase 16)
# --------------------------------------------------------------------------------------

# Operational thresholds. Every one is a *count* the dashboard can compute from rows that
# already exist -- there is no modelled "anomaly score" behind any of them, and a rule
# that cannot be stated as a query is not an alert.
BACKOFFICE_PAGE_SIZE = env.int("BACKOFFICE_PAGE_SIZE", default=25)
BACKOFFICE_LOW_STOCK_THRESHOLD = env.int("BACKOFFICE_LOW_STOCK_THRESHOLD", default=5)
BACKOFFICE_ALERT_PAYMENT_FAILURES = env.int("BACKOFFICE_ALERT_PAYMENT_FAILURES", default=3)
BACKOFFICE_STALE_PAYMENT_MINUTES = env.int("BACKOFFICE_STALE_PAYMENT_MINUTES", default=60)
BACKOFFICE_ALERT_CANCELLED_ORDERS = env.int("BACKOFFICE_ALERT_CANCELLED_ORDERS", default=5)
BACKOFFICE_REVIEW_BACKLOG = env.int("BACKOFFICE_REVIEW_BACKLOG", default=20)
BACKOFFICE_SLA_WAITING_DAYS = env.int("BACKOFFICE_SLA_WAITING_DAYS", default=3)
BACKOFFICE_DROP_ENDING_HOURS = env.int("BACKOFFICE_DROP_ENDING_HOURS", default=24)
# A promotion is "approaching its limit" at this fraction of ``usage_limit``.
BACKOFFICE_PROMOTION_LIMIT_FRACTION = env.float("BACKOFFICE_PROMOTION_LIMIT_FRACTION", default=0.9)


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
    # Every API response must pass the anonymous/member budget, and any view that
    # declares a ``throttle_scope`` is additionally bounded by that scope's rate.
    # Views without a scope are still covered by the anon/user budgets.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("API_ANON_THROTTLE_RATE", default="60/min"),
        "user": env("API_USER_THROTTLE_RATE", default="120/min"),
        # Reserved scopes for sensitive (credential) and expensive (AI/search) endpoints;
        # views opt in with ``throttle_scope = "sensitive"`` / ``"expensive"``.
        "sensitive": env("API_SENSITIVE_THROTTLE_RATE", default="10/min"),
        "expensive": env("API_EXPENSIVE_THROTTLE_RATE", default="30/min"),
        # Phase 17: notification reads vs. notification writes get separate budgets so a
        # write-heavy client cannot exhaust its own read allowance (and vice versa).
        "notifications": env("API_NOTIFICATIONS_THROTTLE_RATE", default="120/min"),
        "notifications_write": env("API_NOTIFICATIONS_WRITE_THROTTLE_RATE", default="60/min"),
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
# Monitoring (Phase 18)
# --------------------------------------------------------------------------------------

# Set SENTRY_DSN (and install the optional ``sentry-sdk``) to forward captured errors
# to Sentry; empty keeps the built-in logging fallback. See apps.core.monitoring.
SENTRY_DSN = env("SENTRY_DSN", default="")
SENTRY_TRACES_SAMPLE_RATE = env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0)

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

LOG_DIR = Path(env("LOG_DIR", default=str(BASE_DIR / "logs")))
LOG_LEVEL = env("LOG_LEVEL", default="INFO")

# "plain" is one human-readable line; "json" is one JSON object per line for a log
# shipper. Production settings default to json (config.settings.production).
LOG_FORMAT = env("LOG_FORMAT", default="plain")

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
        "json": {
            "()": "apps.core.logging_filters.JsonFormatter",
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

if LOG_FORMAT == "json":
    # Machine-readable lines on every stream that leaves the process. ``mail_admins``
    # keeps the plain formatter: an alert email rendered as JSON helps nobody.
    for _handler in ("console", "app_file", "django_file", "security_file"):
        LOGGING["handlers"][_handler]["formatter"] = "json"

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

# --------------------------------------------------------------------------------------
# Phase 23: Returns, Exchanges & Refunds
# --------------------------------------------------------------------------------------

RETURN_WINDOW_DAYS = env.int("RETURN_WINDOW_DAYS", default=30)
