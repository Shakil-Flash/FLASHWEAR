"""Provider interface every payment integration must satisfy.

Three concrete pieces make up a provider:

* :class:`ProviderIntent` -- what registering a payment returned (its reference
  and the state the provider put it in).
* :class:`ProviderEvent` -- one webhook, normalised: which payment, which state,
  how much, and the provider's own event id (the idempotency key).
* :meth:`PaymentProvider.verify_webhook` -- authenticate the delivery *before*
  it is parsed into :class:`ProviderEvent`; unsigned or stale deliveries raise.

Nothing in here touches the database or the network, which is the point: the
services layer can call a provider outside any transaction and still be sure the
DB rules hold.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

__all__ = [
    "InvalidSignature",
    "PaymentProvider",
    "ProviderEvent",
    "ProviderIntent",
    "WebhookError",
]


class InvalidSignature(Exception):
    """Webhook authentication failed (bad, missing or mismatched signature)."""


class WebhookError(Exception):
    """Webhook was authenticated but could not be understood or applied."""


class UnknownPayment(WebhookError):
    """No payment matches the event's provider reference."""


@dataclass(frozen=True)
class ProviderIntent:
    """The provider's answer to "register/cancel this payment"."""

    reference: str
    status: str  # a Payment.Status value
    confirm_url: str = ""  # where the customer completes payment (dev provider)


@dataclass(frozen=True)
class ProviderEvent:
    """One authenticated provider event, normalised for the services layer."""

    event_id: str
    event_type: str  # a Payment.Status value: succeeded / failed / cancelled / pending
    payment_reference: str
    amount: Decimal
    currency: str
    order_number: str = ""
    reason: str = ""
    payload: dict = field(default_factory=dict)

    def as_payload(self) -> dict:
        """The redacted dict stored on the payment event row."""
        return {
            "event_id": self.event_id,
            "type": self.event_type,
            "reference": self.payment_reference,
            "amount": str(self.amount),
            "currency": self.currency,
            "order": self.order_number,
            "reason": self.reason,
        }


class PaymentProvider(ABC):
    """Contract for one payment integration."""

    #: Registry key and the value stored on ``Payment.provider``.
    name: str = ""

    @abstractmethod
    def create_payment(self, payment) -> ProviderIntent:
        """Register ``payment`` with the provider. Never called inside a transaction."""

    @abstractmethod
    def cancel_payment(self, payment) -> ProviderIntent:
        """Cancel a not-yet-succeeded payment. Never called inside a transaction."""

    @abstractmethod
    def verify_webhook(self, request) -> ProviderEvent:
        """Authenticate ``request`` and parse it into a :class:`ProviderEvent`.

        Raises:
            InvalidSignature: authentication failed.
            WebhookError: authenticated but malformed / unsupported.
        """

    @abstractmethod
    def refund_payment(
        self, payment, amount: Decimal, *, reason: str = "", idempotency_key: str = ""
    ) -> ProviderIntent:
        """Issue a refund against a succeeded payment. Never called inside a transaction."""
