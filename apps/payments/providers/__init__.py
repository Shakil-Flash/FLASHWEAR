"""Payment provider registry.

``PAYMENT_PROVIDER`` selects the integration; only ``development`` ships in this
phase. :mod:`config.settings.production` refuses to start with it, so the two
sides of that contract -- "always resolvable locally" and "never in production" --
are enforced where they belong.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from apps.payments.providers.base import (
    InvalidSignature,
    PaymentProvider,
    ProviderEvent,
    ProviderIntent,
    UnknownPayment,
    WebhookError,
)
from apps.payments.providers.development import DevelopmentProvider

__all__ = [
    "InvalidSignature",
    "PaymentProvider",
    "ProviderEvent",
    "ProviderIntent",
    "UnknownPayment",
    "WebhookError",
    "get_provider",
    "provider_name",
]

REGISTRY: dict[str, type[PaymentProvider]] = {
    DevelopmentProvider.name: DevelopmentProvider,
}


def provider_name() -> str:
    """The configured provider, treating "unset" as the local fake."""
    return settings.PAYMENT_PROVIDER or DevelopmentProvider.name


def get_provider(name: str | None = None) -> PaymentProvider:
    """Instantiate the configured (or given) provider.

    Deliberately not cached: settings can be swapped per test, and constructing a
    provider is free.
    """
    key = (name or provider_name()).strip()
    try:
        return REGISTRY[key]()
    except KeyError as err:
        raise ImproperlyConfigured(
            f"Unknown payment provider {key!r}. Known: {sorted(REGISTRY)}."
        ) from err
