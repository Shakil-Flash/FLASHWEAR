"""SMS delivery seam (Phase 51 §5).

Provides provider abstraction, unconfigured provider safety, and sending interface.
If a provider is not configured, it treats SMS as unavailable and never simulates delivery.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger("flashwear.notifications.sms")

__all__ = [
    "ConsoleSmsProvider",
    "PermanentSmsError",
    "SmsProvider",
    "SmsSendError",
    "SmsSendResult",
    "UnconfiguredSmsProvider",
    "get_sms_provider",
    "is_sms_configured",
]


class SmsSendError(Exception):
    """Transient SMS delivery failure (provider network hiccup, retryable)."""


class PermanentSmsError(Exception):
    """Non-retryable SMS failure (unconfigured provider, invalid phone, suppressed)."""


@dataclass(frozen=True)
class SmsSendResult:
    accepted: int
    provider: str


class SmsProvider(ABC):
    name = "abstract"
    is_configured: bool = False

    @abstractmethod
    def send(self, *, text_body: str, recipient_phone: str) -> SmsSendResult: ...


class UnconfiguredSmsProvider(SmsProvider):
    name = "unconfigured"
    is_configured = False

    def send(self, *, text_body: str, recipient_phone: str) -> SmsSendResult:
        logger.warning("SMS delivery attempted but SMS provider is not configured.")
        raise PermanentSmsError("SMS provider is not configured.")


class ConsoleSmsProvider(SmsProvider):
    name = "console"
    is_configured = True

    def send(self, *, text_body: str, recipient_phone: str) -> SmsSendResult:
        phone = (recipient_phone or "").strip()
        if not phone:
            raise PermanentSmsError("Recipient phone number is missing.")
        logger.info("[CONSOLE SMS] To: %s | Body: %s", phone, text_body)
        return SmsSendResult(accepted=1, provider=self.name)


def get_sms_provider() -> SmsProvider:
    name = getattr(settings, "NOTIFICATIONS_SMS_PROVIDER", "unconfigured") or "unconfigured"
    if name == "console":
        return ConsoleSmsProvider()
    if name in ("unconfigured", "none", "", None):
        return UnconfiguredSmsProvider()
    raise ValueError(f"Unknown SMS provider: {name!r}")


def is_sms_configured() -> bool:
    try:
        return get_sms_provider().is_configured
    except Exception:
        return False
