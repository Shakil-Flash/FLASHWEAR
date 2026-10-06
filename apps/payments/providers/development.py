"""The built-in, deterministic payment provider.

``PAYMENT_PROVIDER=development`` (the default outside production) simulates a
card processor without a single network call: registering a payment hands back a
local reference, and the "provider" finishes the job by delivering a signed
webhook -- either in-process (the payment page's buttons) or over HTTP to
``/payments/webhook/development/``, exactly as a real processor would.

The signature scheme is deliberately boring and real:

* ``X-Flashwear-Timestamp``: unix seconds at signing time.
* ``X-Flashwear-Signature``: ``sha256=<hex>``, HMAC-SHA256 of
  ``"<timestamp>." + raw_body`` keyed with ``PAYMENT_WEBHOOK_SECRET``.
* Verification rejects anything outside
  ``PAYMENT_WEBHOOK_TOLERANCE_SECONDS`` so a captured delivery cannot be
  replayed forever.

:func:`sign_payload` is the other half of the same coin -- tests and the
simulation endpoint use it to produce exactly what :meth:`verify_webhook`
consumes, so the tested path *is* the production path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.urls import reverse

from apps.payments.models import Payment
from apps.payments.providers.base import (
    InvalidSignature,
    PaymentProvider,
    ProviderEvent,
    ProviderIntent,
    WebhookError,
)

__all__ = [
    "DevelopmentProvider",
    "build_event",
    "make_provider_event",
    "sign_payload",
    "webhook_secret",
]

SIGNATURE_HEADER = "X-Flashwear-Signature"
TIMESTAMP_HEADER = "X-Flashwear-Timestamp"


def webhook_secret() -> str:
    """The HMAC key: explicit secret, falling back to the project secret locally."""
    return settings.PAYMENT_WEBHOOK_SECRET or settings.SECRET_KEY


def sign_payload(
    body: bytes, secret: str | None = None, timestamp: str | None = None
) -> tuple[str, str]:
    """Return ``(timestamp, signature)`` for ``body`` -- what a client must send."""
    stamp = timestamp if timestamp is not None else str(int(time.time()))
    key = (secret or webhook_secret()).encode()
    digest = hmac.new(key, f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return stamp, f"sha256={digest}"


def build_event(payment: Payment, event_type: str, *, reason: str = "") -> dict:
    """The provider's JSON body for ``payment`` moving to ``event_type``.

    Shared by the simulation endpoint and the tests, so both deliver byte-for-byte
    the same shape a real processor would.
    """
    return {
        "event_id": f"dev_{uuid.uuid4().hex}",
        "type": event_type,
        "reference": payment.provider_reference,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "order": payment.order.number,
        "reason": reason,
    }


def make_provider_event(payment: Payment, event_type: str, *, reason: str = "") -> ProviderEvent:
    """Build the event in-process (used by the payment page's simulate buttons)."""
    body = build_event(payment, event_type, reason=reason)
    return _parse_body(body)


def _parse_body(body: dict | bytes) -> ProviderEvent:
    data = body if isinstance(body, dict) else _loads(body)
    try:
        event_type = data["type"]
        event_id = str(data["event_id"])
        reference = str(data["reference"])
        amount = Decimal(str(data["amount"]))
        currency = str(data["currency"])
    except (KeyError, InvalidOperation) as err:
        raise WebhookError(f"Malformed provider event: {err}") from err
    if event_type not in Payment.Status.values:
        raise WebhookError(f"Unsupported event type: {event_type!r}")
    return ProviderEvent(
        event_id=event_id[:64],
        event_type=event_type,
        payment_reference=reference[:64],
        amount=amount,
        currency=currency.upper()[:3],
        order_number=str(data.get("order", "")),
        reason=str(data.get("reason", ""))[:200],
        payload={
            "event_id": event_id,
            "type": event_type,
            "reference": reference,
            "amount": str(amount),
            "currency": currency,
            "order": data.get("order", ""),
            "reason": data.get("reason", ""),
        },
    )


def _loads(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as err:
        raise WebhookError(f"Body is not JSON: {err}") from err
    if not isinstance(data, dict):
        raise WebhookError("Body must be a JSON object.")
    return data


class DevelopmentProvider(PaymentProvider):
    """Deterministic fake: local references, signed local webhooks, no I/O."""

    name = "development"

    def create_payment(self, payment: Payment) -> ProviderIntent:
        # Deterministic on purpose: retrying the registration yields the same
        # reference, which is how idempotency keys work with real processors.
        return ProviderIntent(
            reference=f"dev-{payment.pk}",
            status=Payment.Status.PENDING,
            confirm_url=reverse("payments:simulate", args=[payment.pk]),
        )

    def cancel_payment(self, payment: Payment) -> ProviderIntent:
        return ProviderIntent(
            reference=payment.provider_reference or f"dev-{payment.pk}",
            status=Payment.Status.CANCELLED,
        )

    def verify_webhook(self, request) -> ProviderEvent:
        stamp = request.headers.get(TIMESTAMP_HEADER, "")
        signature = request.headers.get(SIGNATURE_HEADER, "")
        if not stamp or not signature:
            raise InvalidSignature("Missing webhook signature headers.")

        expected = sign_payload(request.body, timestamp=stamp)[1]
        if not hmac.compare_digest(signature, expected):
            raise InvalidSignature("Webhook signature does not match.")

        try:
            sent_at = int(stamp)
        except ValueError as err:
            raise InvalidSignature("Timestamp is not an integer.") from err
        tolerance = settings.PAYMENT_WEBHOOK_TOLERANCE_SECONDS
        if abs(time.time() - sent_at) > tolerance:
            raise WebhookError("Webhook timestamp outside the accepted tolerance.")

        return _parse_body(request.body)

    def refund_payment(
        self, payment: Payment, amount: Decimal, *, reason: str = "", idempotency_key: str = ""
    ) -> ProviderIntent:
        ref_suffix = idempotency_key or f"{payment.pk}-{amount}"
        return ProviderIntent(
            reference=f"dev-ref-{payment.pk}-{ref_suffix}",
            status=Payment.Status.REFUNDED,
        )
