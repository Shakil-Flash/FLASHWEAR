"""Staging settings: production behaviour with staging-safe relaxations.

Staging is configured exactly like production -- PostgreSQL, Redis, real proxy headers,
required secrets -- so a deploy rehearsal exercises the same code path. The differences
are deliberate and small:

* HSTS stays short (7 days, no preload) so a mistaken deployment can be undone without
  browsers pinning a broken certificate for a year.
* CSP starts in report-only mode while templates are being verified against it.
* The payment provider guard still applies: staging never runs the fake provider.

Everything inherited from ``config.settings.production`` (SECRET_KEY, ALLOWED_HOSTS,
CSRF_TRUSTED_ORIGINS, DATABASE_URL, REDIS_URL, PAYMENT_WEBHOOK_SECRET, SMTP host) is
mandatory here too: staging must fail loudly on the same missing variables as
production, otherwise the rehearsal proves nothing.
"""

from .production import *
from .production import env

# HSTS: short enough to undo, long enough to be meaningful.
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 7)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)

# Report violations while the staging templates are being checked against the policy;
# set CSP_REPORT_ONLY=False to enforce exactly like production.
CSP_REPORT_ONLY = env.bool("CSP_REPORT_ONLY", default=True)

# Staging gets its own cache key prefix so a shared Redis cannot bleed state between
# environments. ``CACHES`` was already built in production.py from the environment, so
# the prefix has to be spliced in here rather than set as a bare module attribute.
CACHE_KEY_PREFIX = env("CACHE_KEY_PREFIX", default="flashwear-staging")
CACHES = {
    **CACHES,
    "default": {**CACHES["default"], "KEY_PREFIX": CACHE_KEY_PREFIX},
}
