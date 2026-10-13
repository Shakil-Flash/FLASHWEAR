"""Carrier and courier tracking webhook verification and processing (Phase 54 §5).

Guarantees:
1. Provider webhook signature verification (HMAC-SHA256) with replay tolerance.
2. Replay protection and deduplication using event ID or body hash.
3. Out-of-order event handling: never regresses a DELIVERED shipment to IN_TRANSIT.
4. Idempotent processing: repeated webhook calls return success without duplicating events or notifications.
5. Sanitized metadata: never leaks raw credentials or sensitive private payloads.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.orders.fulfillment_services import update_tracking_event
from apps.orders.models import Shipment, ShipmentEvent

logger = logging.getLogger("flashwear.shipping.webhooks")

DEFAULT_TOLERANCE_SECONDS = 300


class WebhookAuthenticationError(Exception):
    """Raised when webhook signature verification fails."""


def _get_webhook_secret(provider: str) -> str:
    specific = (
        getattr(settings, f"SHIPPING_WEBHOOK_SECRET_{provider.upper()}", "")
        or getattr(settings, f"CARRIER_WEBHOOK_SECRET_{provider.upper()}", "")
    )
    if specific:
        return specific
    generic = (
        getattr(settings, "SHIPPING_WEBHOOK_SECRET", "")
        or getattr(settings, "CARRIER_WEBHOOK_SECRET", "")
    )
    return generic or settings.SECRET_KEY


def verify_webhook_signature(
    request,
    provider: str,
    *,
    secret: str | None = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Verify that a courier webhook request carries a valid HMAC signature.

    Checks ``HTTP_X_SIGNATURE``, ``HTTP_X_WEBHOOK_SIGNATURE``, or ``HTTP_X_CARRIER_SIGNATURE``.
    Supports either a raw hex signature or ``t=<timestamp>,v1=<signature>`` format.
    """
    key = (secret or _get_webhook_secret(provider)).encode("utf-8")
    sig_header = (
        request.headers.get("X-Signature")
        or request.headers.get("X-Webhook-Signature")
        or request.headers.get("X-Carrier-Signature")
        or request.META.get("HTTP_X_SIGNATURE", "")
        or request.META.get("HTTP_X_WEBHOOK_SIGNATURE", "")
        or request.META.get("HTTP_X_CARRIER_SIGNATURE", "")
        or ""
    ).strip()

    if not sig_header:
        raise WebhookAuthenticationError("Missing webhook signature header.")

    timestamp_header = (
        request.headers.get("X-Carrier-Timestamp")
        or request.headers.get("X-Timestamp")
        or request.headers.get("X-Webhook-Timestamp")
        or request.META.get("HTTP_X_CARRIER_TIMESTAMP", "")
        or request.META.get("HTTP_X_TIMESTAMP", "")
        or request.META.get("HTTP_X_WEBHOOK_TIMESTAMP", "")
        or ""
    ).strip()

    raw_body = getattr(request, "body", b"")

    if "t=" in sig_header and "v1=" in sig_header:
        parts = dict(part.split("=", 1) for part in sig_header.split(",") if "=" in part)
        timestamp_str = parts.get("t")
        expected_hash = parts.get("v1", "")
    else:
        timestamp_str = timestamp_header
        expected_hash = sig_header

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

    computed_hash = hmac.new(key, payload_to_sign, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, expected_hash):
        raise WebhookAuthenticationError("Invalid webhook signature.")
    return True


# Status normalization map from common carrier strings
STATUS_MAP = {
    "in_transit": Shipment.Status.IN_TRANSIT,
    "transit": Shipment.Status.IN_TRANSIT,
    "picked_up": Shipment.Status.SHIPPED,
    "dispatched": Shipment.Status.SHIPPED,
    "out_for_delivery": Shipment.Status.OUT_FOR_DELIVERY,
    "with_courier": Shipment.Status.OUT_FOR_DELIVERY,
    "delivered": Shipment.Status.DELIVERED,
    "delivery_success": Shipment.Status.DELIVERED,
    "failed_attempt": Shipment.Status.ATTEMPT_FAILED,
    "attempt_failed": Shipment.Status.ATTEMPT_FAILED,
    "exception": Shipment.Status.EXCEPTION,
    "delayed": Shipment.Status.EXCEPTION,
    "returned_to_sender": Shipment.Status.RETURNED_TO_SENDER,
    "return_to_sender": Shipment.Status.RETURNED_TO_SENDER,
}


@method_decorator(csrf_exempt, name="dispatch")
class CarrierTrackingWebhookView(View):
    """Receive and process courier tracking webhook events with signature and replay protection."""

    def post(self, request, provider: str, *args, **kwargs):
        # 1. Verify signature
        try:
            verify_webhook_signature(request, provider)
        except WebhookAuthenticationError as exc:
            logger.warning("Carrier webhook auth rejected (%s): %s", provider, exc)
            status_code = 400 if "timestamp" in str(exc).lower() else 401
            return JsonResponse({"detail": str(exc), "error": str(exc)}, status=status_code)

        # 2. Parse JSON payload
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"detail": "Invalid JSON payload."}, status=400)

        # 3. Locate shipment by tracking number or reference
        tracking_num = (payload.get("tracking_number") or "").strip()
        reference = (payload.get("reference") or payload.get("shipment_reference") or "").strip()
        order_number = (payload.get("order_number") or "").strip()

        shipment = None
        if tracking_num:
            shipment = Shipment.objects.filter(tracking_number=tracking_num).first()
        if not shipment and reference:
            shipment = Shipment.objects.filter(reference=reference).first()
        if not shipment and order_number:
            shipment = Shipment.objects.filter(order__number=order_number).first()

        if not shipment:
            return JsonResponse(
                {"detail": "Shipment not found for provided tracking/reference identifier."},
                status=404,
            )

        # 4. Replay protection / Deduplication via event_id
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            # Deterministic hash of payload
            event_id = hashlib.sha256(request.body).hexdigest()[:32]

        # Check if this exact event_id was already recorded for this shipment
        already_processed = shipment.events.filter(metadata__event_id=event_id).exists()
        if already_processed:
            return JsonResponse(
                {
                    "status": "already_processed",
                    "shipment": shipment.reference or str(shipment.pk),
                    "current_status": shipment.status,
                },
                status=200,
            )

        # 5. Extract event attributes and normalize status
        raw_status = (payload.get("status") or "").strip().lower()
        target_status = STATUS_MAP.get(raw_status)
        location = str(payload.get("location") or "").strip()[:128]
        description = str(payload.get("description") or payload.get("note") or "").strip()[:300]
        raw_time = payload.get("timestamp")

        occurred_at = timezone.now()
        if raw_time:
            try:
                if isinstance(raw_time, (int, float)):
                    occurred_at = datetime.fromtimestamp(raw_time, tz=timezone.utc)
                elif isinstance(raw_time, str):
                    from django.utils.dateparse import parse_datetime
                    parsed = parse_datetime(raw_time)
                    if parsed:
                        occurred_at = parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)
            except Exception:
                pass

        # 6. Apply tracking update with out-of-order protection
        safe_meta = {
            "event_id": event_id,
            "provider": provider,
            "raw_status": raw_status,
            "carrier_code": str(payload.get("carrier_code") or "")[:32],
        }

        updated_shipment = update_tracking_event(
            shipment,
            event_type=target_status or raw_status or "tracking_update",
            status=target_status,
            location=location,
            note=description or f"Carrier update: {raw_status or 'Scan checkpoint'}",
            occurred_at=occurred_at,
            source=ShipmentEvent.Source.CARRIER_WEBHOOK,
            metadata=safe_meta,
        )

        return JsonResponse(
            {
                "status": "processed",
                "shipment": updated_shipment.reference or str(updated_shipment.pk),
                "current_status": updated_shipment.status,
            },
            status=200,
        )
