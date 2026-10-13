"""Push notification delivery seam (Phase 51 §5).

Provides push provider abstraction, unconfigured provider safety, and sending interface.
If a provider is not configured, push notifications are treated as unavailable and delivery is never faked.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger("flashwear.notifications.push")

__all__ = [
    "ConsolePushProvider",
    "PermanentPushError",
    "PushProvider",
    "PushSendError",
    "PushSendResult",
    "UnconfiguredPushProvider",
    "get_push_provider",
    "is_push_configured",
]


class PushSendError(Exception):
    """Transient push delivery failure (retryable)."""


class PermanentPushError(Exception):
    """Non-retryable push failure (unconfigured provider, invalid registration, etc.)."""


@dataclass(frozen=True)
class PushSendResult:
    accepted: int
    provider: str


class PushProvider(ABC):
    name = "abstract"
    is_configured: bool = False

    @abstractmethod
    def send(
        self,
        *,
        title: str,
        body: str,
        recipient_user_id: int,
        action_url: str = "",
    ) -> PushSendResult: ...


class UnconfiguredPushProvider(PushProvider):
    name = "unconfigured"
    is_configured = False

    def send(
        self,
        *,
        title: str,
        body: str,
        recipient_user_id: int,
        action_url: str = "",
    ) -> PushSendResult:
        logger.warning("Push delivery attempted but push provider is not configured.")
        raise PermanentPushError("Push notification provider is not configured.")


class ConsolePushProvider(PushProvider):
    name = "console"
    is_configured = True

    def send(
        self,
        *,
        title: str,
        body: str,
        recipient_user_id: int,
        action_url: str = "",
    ) -> PushSendResult:
        logger.info(
            "[CONSOLE PUSH] User #%d | Title: %s | Body: %s | URL: %s",
            recipient_user_id,
            title,
            body,
            action_url,
        )
        return PushSendResult(accepted=1, provider=self.name)


def get_push_provider() -> PushProvider:
    name = getattr(settings, "NOTIFICATIONS_PUSH_PROVIDER", "unconfigured") or "unconfigured"
    if name == "console":
        return ConsolePushProvider()
    if name in ("unconfigured", "none", "", None):
        return UnconfiguredPushProvider()
    raise ValueError(f"Unknown push provider: {name!r}")


def is_push_configured() -> bool:
    try:
        return get_push_provider().is_configured
    except Exception:
        return False
