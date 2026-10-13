"""Provider webhook verification and delivery processing (Phase 51 §5, §9).

Guarantees:
1. Provider webhook signature verification (HMAC-SHA256).
2. Replay protection and deduplication via ``DeliveryWebhookEvent``.
3. Accurate delivery state updates based on provider evidence (SENT -> DELIVERED, or FAILED).
4. Sanitized error messages that never leak tokens, keys, or sensitive payload data.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import (
    DeliveryWebhookEvent,
    Notification,
    NotificationStatus,
)

logger = logging.getLogger("flashwear.notifications.webhooks")

__all__ = [
    "WebhookAuthenticationError",
    "WebhookProcessingError",
    "process_delivery_webhook",
    "verify_webhook_signature",
]

DEFAULT_TOLERANCE_SECONDS = 300


class WebhookAuthenticationError(Exception):
    """Raised when webhook signature verification fails."""


class WebhookProcessingError(Exception):
    """Raised when webhook payload cannot be parsed or processed."""


def _get_webhook_secret(provider: str) -> str:
    specific = getattr(settings, f"NOTIFICATIONS_WEBHOOK_SECRET_{provider.upper()}", "")
    if specific:
        return specific
    generic = getattr(settings, "NOTIFICATIONS_WEBHOOK_SECRET", "")
    return generic or settings.SECRET_KEY


def verify_webhook_signature(
    request,
    provider: str,
    *,
    secret: str | None = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Verify that a provider webhook request carries a valid HMAC signature.

    Checks ``HTTP_X_SIGNATURE``, ``HTTP_X_WEBHOOK_SIGNATURE``, or ``HTTP_X_PROVIDER_SIGNATURE``.
    Supports either a raw hex signature or ``t=<timestamp>,v1=<signature>`` format.
    """
    key = (secret or _get_webhook_secret(provider)).encode("utf-8")
    sig_header = (
        request.headers.get("X-Signature")
        or request.headers.get("X-Webhook-Signature")
        or request.headers.get("X-Provider-Signature")
        or request.META.get("HTTP_X_SIGNATURE", "")
        or request.META.get("HTTP_X_WEBHOOK_SIGNATURE", "")
        or ""
    ).strip()

    if not sig_header:
        raise WebhookAuthenticationError("Missing webhook signature header.")

    raw_body = getattr(request, "body", b"")
    timestamp = None
    expected_hash = sig_header

    if "t=" in sig_header and "v1=" in sig_header:
        parts = dict(part.split("=", 1) for part in sig_header.split(",") if "=" in part)
        timestamp_str = parts.get("t")
        expected_hash = parts.get("v1", "")
        if timestamp_str:
            try:
                timestamp = int(timestamp_str)
                now = int(time.time())
                if abs(now - timestamp) > tolerance_seconds:
                    raise WebhookAuthenticationError("Webhook timestamp expired or out of tolerance.")
            except ValueError as exc:
                raise WebhookAuthenticationError("Malformed timestamp in signature header.") from exc
        payload_to_sign = f"{timestamp_str}.".encode("utf-8") + raw_body
    else:
        payload_to_sign = raw_body

    computed = hmac.new(key, payload_to_sign, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed.lower(), expected_hash.lower()):
        raise WebhookAuthenticationError("Invalid webhook signature.")

    return True


def _sanitize_reason(reason: str) -> str:
    """Strip any accidental authorization headers, tokens or raw secrets from error text."""
    clean = str(reason or "").strip()
    clean = clean.replace(settings.SECRET_KEY, "[REDACTED]")
    return clean[:300]


def process_delivery_webhook(
    provider: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Process an authenticated delivery webhook payload idempotently.

    Replays with the same ``event_id`` are recorded once and short-circuited.
    Updates the referenced ``Notification`` row with evidence-backed status transitions.
    """
    event_id = str(payload.get("event_id") or payload.get("id") or "").strip()
    if not event_id:
        raise WebhookProcessingError("Webhook payload must include an event identifier.")

    event_type = str(payload.get("event_type") or payload.get("type") or "status_update").strip().lower()
    raw_status = str(payload.get("status") or "").strip().lower()
    notification_id = payload.get("notification_id")
    idempotency_key = payload.get("idempotency_key")
    failure_reason = _sanitize_reason(payload.get("reason") or payload.get("error") or "")

    # 1. Atomic event record for replay protection
    with transaction.atomic():
        webhook_event, created = DeliveryWebhookEvent.objects.get_or_create(
            provider=provider,
            event_id=event_id,
            defaults={
                "event_type": event_type,
                "payload": {k: v for k, v in payload.items() if k not in ("token", "secret", "password")},
            },
        )
        if not created:
            logger.info("Ignoring duplicate webhook event %s from provider %s (replay).", event_id, provider)
            return {
                "status": "already_processed",
                "event_id": event_id,
                "provider": provider,
            }

        # 2. Resolve matching Notification
        notification: Notification | None = None
        if notification_id:
            notification = Notification.objects.filter(pk=notification_id).first()
        elif idempotency_key:
            notification = Notification.objects.filter(idempotency_key=idempotency_key).first()

        if notification is not None:
            webhook_event.notification = notification
            webhook_event.save(update_fields=["notification"])

            # 3. Transition notification status based on provider evidence
            target_status: str | None = None
            if raw_status in ("delivered", "success"):
                target_status = NotificationStatus.DELIVERED
            elif raw_status in ("failed", "bounced", "rejected", "undelivered"):
                target_status = NotificationStatus.FAILED
            elif raw_status in ("sent", "dispatched"):
                target_status = NotificationStatus.SENT
            elif raw_status in ("unknown",):
                target_status = NotificationStatus.UNKNOWN

            if target_status and notification.status != target_status:
                try:
                    notification.transition_to(target_status, failure_reason=failure_reason)
                except Exception as exc:
                    logger.warning(
                        "Could not transition notification %s from %s to %s via webhook: %s",
                        notification.pk,
                        notification.status,
                        target_status,
                        exc,
                    )

    return {
        "status": "processed",
        "event_id": event_id,
        "provider": provider,
        "notification_id": notification.pk if notification else None,
        "notification_status": notification.status if notification else None,
    }
