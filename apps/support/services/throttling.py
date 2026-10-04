"""Write limits for the support desk (Phase 15).

Two limits, both per signed-in user:

* how many tickets one person may open in a window -- without it, a script can manufacture
  an unbounded queue and bury every real customer's ticket;
* how many messages one person may post in a window -- without it, the transcript becomes a
  free text-messaging channel at our delivery bill's expense.

The counter is cached (Redis in production, locmem in development), which is the same shape
as :class:`apps.accounts.throttling.LoginThrottle` and reuses its
:class:`~apps.accounts.throttling.ThrottleDecision` so callers see one type. Nothing here
authenticates or authorises -- a throttle only ever answers "not right now", and the caller
decides what to do with the answer.
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.cache import cache

from apps.accounts.throttling import ThrottleDecision

__all__ = ["SupportThrottle"]


def _fingerprint(*parts) -> str:
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return digest[:32]


class SupportThrottle:
    """Counts tickets and messages per user inside a sliding window."""

    def __init__(self, cache_alias: str = "default") -> None:
        self.max_tickets = getattr(settings, "SUPPORT_MAX_TICKETS_PER_WINDOW", 5)
        self.max_messages = getattr(settings, "SUPPORT_MAX_MESSAGES_PER_WINDOW", 30)
        self.window = getattr(settings, "SUPPORT_MESSAGE_WINDOW_SECONDS", 300)
        self._cache = cache
        self.alias = cache_alias

    def _ticket_key(self, user) -> str:
        return f"support:open:{_fingerprint(getattr(user, 'pk', None))}"

    def _message_key(self, user) -> str:
        return f"support:msg:{_fingerprint(getattr(user, 'pk', None))}"

    def _bump(self, key: str) -> int:
        # ``add`` seeds the counter atomically so two concurrent writes cannot both find
        # the key missing; ``incr`` then adds one without a read-modify-write race.
        self._cache.add(key, 0, self.window)
        try:
            return int(self._cache.incr(key))
        except ValueError:  # key evicted between add and incr
            self._cache.set(key, 1, self.window)
            return 1

    def _count(self, key: str) -> int:
        return int(self._cache.get(key) or 0)

    def _decision(self, key: str, limit: int) -> ThrottleDecision:
        if self._count(key) < limit:
            return ThrottleDecision(blocked=False)
        return ThrottleDecision(blocked=True, retry_after=self.window)

    # -- api ----------------------------------------------------------------

    def check_open(self, user) -> ThrottleDecision:
        """May this user open another ticket right now? Records nothing."""
        return self._decision(self._ticket_key(user), self.max_tickets)

    def record_open(self, user) -> None:
        self._bump(self._ticket_key(user))

    def check_message(self, user) -> ThrottleDecision:
        """May this user post another message right now? Records nothing."""
        return self._decision(self._message_key(user), self.max_messages)

    def record_message(self, user) -> None:
        self._bump(self._message_key(user))

    def reset(self, user) -> None:
        """Clear both counters (used by staff tooling and tests)."""
        self._cache.delete(self._ticket_key(user))
        self._cache.delete(self._message_key(user))
