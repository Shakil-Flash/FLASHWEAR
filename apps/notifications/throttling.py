"""Write limits for notification endpoints (Phase 17 §30).

Same shape as ``SupportThrottle`` (cached sliding counter, reusing the shared
``ThrottleDecision``), because the abuse this stops is the same abuse: a script using the
"mark all read", "save preferences" or unsubscribe endpoints as a free write loop -- the
unsubscribe limiter especially, which sits behind no login and would otherwise be a
per-Ip hammer against the preference table.

Throttling is not authorisation: the views still filter by ``request.user`` (or the
opaque token) first; this only decides "not right now".
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache

from apps.accounts.throttling import ThrottleDecision

__all__ = ["NotificationThrottle"]


def _fingerprint(*parts) -> str:
    import hashlib

    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return digest[:32]


class NotificationThrottle:
    """Three counters: read-all, preference writes, and unsubscribe (keyed by IP)."""

    ACTIONS = ("read_all", "preferences", "unsubscribe")

    def __init__(self, cache_alias: str = "default") -> None:
        self.max_read_all = int(getattr(settings, "NOTIFICATIONS_READ_ALL_MAX", 10))
        self.max_preferences = int(getattr(settings, "NOTIFICATIONS_PREFERENCES_MAX", 20))
        self.max_unsubscribe = int(getattr(settings, "NOTIFICATIONS_UNSUBSCRIBE_MAX", 10))
        self.window = int(getattr(settings, "NOTIFICATIONS_THROTTLE_WINDOW_SECONDS", 300))
        self._cache = cache
        self.alias = cache_alias

    def _limit(self, action: str) -> int:
        return {
            "read_all": self.max_read_all,
            "preferences": self.max_preferences,
            "unsubscribe": self.max_unsubscribe,
        }[action]

    def _key(self, action: str, ident) -> str:
        return f"notifications:{action}:{_fingerprint(ident)}"

    def _decision(self, key: str, limit: int) -> ThrottleDecision:
        count = int(self._cache.get(key) or 0)
        if count < limit:
            return ThrottleDecision(blocked=False)
        return ThrottleDecision(blocked=True, retry_after=self.window)

    def check(self, action: str, ident) -> ThrottleDecision:
        """May this actor act again right now? Records nothing."""
        if action not in self.ACTIONS:
            raise ValueError(f"Unknown notification throttle action: {action!r}")
        return self._decision(self._key(action, ident), self._limit(action))

    def record(self, action: str, ident) -> None:
        key = self._key(action, ident)
        self._cache.add(key, 0, self.window)
        try:
            self._cache.incr(key)
        except ValueError:  # key evicted between add and incr
            self._cache.set(key, 1, self.window)

    def reset(self, ident) -> None:
        for action in self.ACTIONS:
            self._cache.delete(self._key(action, ident))
