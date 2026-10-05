"""Readiness probes for load balancers and container orchestrators.

Liveness (``/health/live/``) means "the process answers HTTP" and touches nothing
else. Readiness (``/health/ready/``) means "this instance can serve traffic":
database and cache must answer; the Celery broker is reported as an advisory check
because the web tier does not need it to answer a page, but operators want to see it
turn red before checkout tasks start backing up.

Each check is isolated in its own function so tests can fail one dependency without
monkeypatching the view.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.db import connection

__all__ = ["check_broker", "check_cache", "check_database"]


def check_database() -> str:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return "ok"
    except Exception:  # any driver error means "not ready"
        return "down"


def check_cache() -> str:
    try:
        key = "health:ready-probe"
        cache.set(key, 1, 10)
        return "ok" if cache.get(key) == 1 else "down"
    except Exception:
        return "down"


def check_broker() -> str:
    """Advisory: ping the broker only when it is Redis and nothing runs eagerly."""
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return "skipped"
    url = settings.CELERY_BROKER_URL
    if not url.startswith(("redis://", "rediss://")):
        return "skipped"
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        return "ok" if client.ping() else "down"
    except Exception:  # connection refused, auth failure, DNS, ...
        return "down"
