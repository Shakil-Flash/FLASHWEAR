"""Failed-login throttling for the storefront sign-in form.

The API surface gets DRF throttles for free; the HTML sign-in form does not. Without a limit,
``/accounts/login/`` is an open password-guessing oracle. This module adds a small cache-backed
counter so brute force and credential stuffing are both bounded.

Design notes:

* Only **failures** are counted. Counting successes would let an attacker lock a real customer
  out of their own account just by spamming the form.
* Two keys are tracked: one for the client address and one for the targeted account. The
  per-IP limit stops one host grinding many accounts; the per-account limit stops a botnet
  grinding one account. Either one tripping blocks the request.
* Keys are hashed, so raw emails and IP addresses never land in Redis.
* The cache is Redis in production (shared by every worker) and locmem in development, where the
  counter is per-process — good enough for a single-process dev server.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

__all__ = ["LoginThrottle", "ThrottleDecision"]


@dataclass(frozen=True)
class ThrottleDecision:
    """Whether a sign-in attempt may proceed."""

    blocked: bool
    retry_after: int = 0

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return not self.blocked


def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def _client_ip(request) -> str:
    """Resolve the client address, trusting ``X-Forwarded-For`` only behind a proxy.

    ``X-Forwarded-For`` is client-controlled, so honouring it unconditionally would let an
    attacker mint a fresh counter per request. ``USE_X_FORWARDED_FOR`` is therefore left off in
    production and only enabled deliberately behind a trusted proxy.
    """
    if getattr(settings, "USE_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


class LoginThrottle:
    """Counts failed sign-ins per client address and per account."""

    def __init__(self, cache_alias: str = "default") -> None:
        self.max_per_ip = getattr(settings, "LOGIN_MAX_FAILURES_PER_IP", 20)
        self.max_per_account = getattr(settings, "LOGIN_MAX_FAILURES_PER_ACCOUNT", 5)
        self.window = getattr(settings, "LOGIN_FAILURE_WINDOW_SECONDS", 300)
        self._cache = cache
        self.alias = cache_alias

    # -- keys ---------------------------------------------------------------

    def _ip_key(self, request) -> str:
        return f"login-fail:ip:{_fingerprint(_client_ip(request))}"

    def _account_key(self, username: str) -> str:
        normalised = (username or "").strip().casefold()
        return f"login-fail:acct:{_fingerprint(normalised)}"

    # -- counters -----------------------------------------------------------

    def _bump(self, key: str) -> int:
        # ``add`` seeds the counter atomically so two concurrent failures cannot both find the
        # key missing; ``incr`` then adds one without a read-modify-write race.
        self._cache.add(key, 0, self.window)
        try:
            return int(self._cache.incr(key))
        except ValueError:  # key evicted between add and incr
            self._cache.set(key, 1, self.window)
            return 1

    def _count(self, key: str) -> int:
        return int(self._cache.get(key) or 0)

    # -- api ----------------------------------------------------------------

    def check(self, request, username: str) -> ThrottleDecision:
        """Return whether this attempt may proceed, without recording anything."""
        ip_count = self._count(self._ip_key(request))
        account_count = self._count(self._account_key(username))
        if ip_count < self.max_per_ip and account_count < self.max_per_account:
            return ThrottleDecision(blocked=False)
        return ThrottleDecision(blocked=True, retry_after=self.window)

    def record_failure(self, request, username: str) -> None:
        self._bump(self._ip_key(request))
        self._bump(self._account_key(username))

    def reset(self, request, username: str) -> None:
        """Clear both counters after a successful sign-in."""
        self._cache.delete(self._ip_key(request))
        self._cache.delete(self._account_key(username))
