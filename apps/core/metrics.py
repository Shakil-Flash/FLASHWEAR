"""Operational counters behind the Back Office health panel.

Deliberately small: fixed-name counters in the Django cache (Redis in production,
locmem in tests), bucketed per UTC day with a rolling TTL. Nothing here is
authoritative billing data -- it is a live dashboard for "is traffic healthy right
now?", backed by real instrumentation (the request middleware and the Celery/Django
failure signals) rather than synthetic numbers.

Design notes:

* Counters are shared through the cache, so every gunicorn worker and the beat
  process see one set of numbers. In development the cache is per-process.
* Telemetry must never break a request: every write is best effort and silently
  degrades to a no-op if the cache is unavailable.
* Durations are accumulated as integer milliseconds so the same key works on
  locmem, Redis and every backend that supports integer ``incr``.
* Failed task *names* are tracked through a small JSON index key: the counters
  themselves are race-free ``incr``s, while the index may lose a concurrent first
  failure of a different task name -- a cosmetic edge on a dashboard, and it keeps
  one code path that works on every cache backend.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.core.cache import cache

__all__ = [
    "incr",
    "mark_request",
    "mark_task",
    "queue_depth",
    "snapshot",
]

_TTL_SECONDS = 96 * 60 * 60  # three days: today's bucket plus slack for clock skew


def _today() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def _key(name: str) -> str:
    return f"m:{_today()}:{name}"


def incr(name: str, amount: int = 1) -> None:
    """Best-effort increment of a daily counter."""
    try:
        key = _key(name)
        # ``add`` seeds atomically so concurrent workers never race a missing key;
        # ``incr`` then counts without read-modify-write.
        cache.add(key, 0, _TTL_SECONDS)
        cache.incr(key, amount)
    except ValueError:
        # Key evicted between add and incr: seed it once more.
        try:
            cache.set(key, amount, _TTL_SECONDS)
        except Exception:  # telemetry must never raise
            pass
    except Exception:  # telemetry must never raise
        pass


def mark_request(status: int, duration_ms: int) -> None:
    """Record one HTTP response."""
    bucket = "2xx" if status < 300 else "3xx" if status < 400 else "4xx" if status < 500 else "5xx"
    incr("requests")
    incr(f"requests:{bucket}")
    incr("requests:ms", max(duration_ms, 0))


def mark_task(task: str, ok: bool) -> None:
    """Record one Celery task outcome (signal-driven)."""
    incr("tasks")
    if ok:
        incr("tasks:ok")
        return
    incr("tasks:fail")
    # Task names are a fixed set from the codebase, so this cannot fan out.
    incr(f"tasks:fail:{task}")
    try:
        index_key = _key("tasks:fail:index")
        names = cache.get(index_key)
        if not isinstance(names, list):
            names = []
        if task not in names:
            names.append(task)
            cache.set(index_key, names, _TTL_SECONDS)
    except Exception:  # telemetry must never raise
        pass


def _get(name: str) -> int:
    try:
        return int(cache.get(_key(name)) or 0)
    except Exception:  # telemetry must never raise
        return 0


def _failed_task_names() -> dict[str, int]:
    try:
        names = cache.get(_key("tasks:fail:index"))
    except Exception:  # telemetry must never raise
        names = None
    if not isinstance(names, list):
        names = []
    return {name: _get(f"tasks:fail:{name}") for name in names}


def queue_depth(queue: str = "celery") -> int | None:
    """Messages waiting on the Celery queue, or ``None`` when not measurable.

    Only available when the default cache is Redis-backed (production); locmem in
    tests reports ``None`` rather than lying with a fabricated number.
    """
    try:
        from django_redis import get_redis_connection

        return int(get_redis_connection("default").llen(queue))
    except Exception:  # best effort, None means "not measurable"
        return None


def snapshot() -> dict:
    """Today's counters as a JSON-ready dict for the Back Office health panel."""
    total = _get("requests")
    ms = _get("requests:ms")
    return {
        "date": _today(),
        "requests": {
            "total": total,
            "2xx": _get("requests:2xx"),
            "3xx": _get("requests:3xx"),
            "4xx": _get("requests:4xx"),
            "5xx": _get("requests:5xx"),
            "avg_ms": (ms // total) if total else 0,
        },
        "tasks": {
            "total": _get("tasks"),
            "succeeded": _get("tasks:ok"),
            "failed": _get("tasks:fail"),
            "failed_by_task": _failed_task_names(),
        },
        "queue_depth": queue_depth(),
    }
