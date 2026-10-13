"""Fulfillment, shipping, and delivery tracking intelligence services (Phase 54).

Provides:
- Partial and multi-package fulfillment per order with item-level tracking.
- Warehouse picking and packing workflow with staff attribution and notes.
- Dispatch with courier, service, tracking numbers, and safe external URL generation.
- Tracking event updates, out-of-order protection, delivery exceptions, and idempotency.
- Transactional delivery notifications (mandatory category) with deduplication.
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import NotificationType
from apps.notifications.services.events import emit as notify_emit
from apps.orders.models import (
    InvalidTransition,
    Order,
    OrderEvent,
    OrderItem,
    Shipment,
    ShipmentEvent,
    ShipmentItem,
)

logger = logging.getLogger("flashwear.fulfillment")

__all__ = [
    "cancel_fulfillment",
    "create_fulfillment",
    "dispatch_shipment",
    "generate_shipment_reference",
    "get_order_fulfillment_summary",
    "get_unfulfilled_order_items",
    "mark_packed",
    "record_delivery_exception",
    "start_picking",
    "update_tracking_event",
]


def generate_shipment_reference(order: Order) -> str:
    """Generate a unique human-friendly handle: ``SHP-<order.number>-<idx>``."""
    existing_count = order.shipments.count()
    candidate = f"SHP-{order.number}-{existing_count + 1}"
    if not Shipment.objects.filter(reference=candidate).exists():
        return candidate
    suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"SHP-{order.number}-{suffix}"


def get_unfulfilled_order_items(order: Order) -> list[dict[str, Any]]:
    """Return line items for ``order`` with ordered, already-fulfilled, and remaining quantities."""
    items_summary = []
    for item in order.items.select_related("variant__product").order_by("pk"):
        fulfilled_qty = (
            ShipmentItem.objects.filter(shipment__order=order, order_item=item)
            .exclude(shipment__status=Shipment.Status.CANCELLED)
            .aggregate(total=models.Sum("quantity"))["total"]
            or 0
        )
        remaining_qty = max(0, item.quantity - fulfilled_qty)
        items_summary.append(
            {
                "order_item_id": item.pk,
                "order_item": item,
                "item": item,
                "sku": item.sku,
                "product_name": item.product_name,
                "option_label": item.option_label,
                "unit_price": item.unit_price,
                "ordered_quantity": item.quantity,
                "fulfilled_quantity": fulfilled_qty,
                "remaining_quantity": remaining_qty,
                "unfulfilled_qty": remaining_qty,
                "is_fully_fulfilled": remaining_qty == 0,
            }
        )
    return items_summary


def get_order_fulfillment_summary(order: Order) -> dict[str, Any]:
    """Calculate overall fulfillment progress for an order."""
    items = get_unfulfilled_order_items(order)
    total_ordered = sum(i["ordered_quantity"] for i in items)
    total_fulfilled = sum(i["fulfilled_quantity"] for i in items)
    total_remaining = sum(i["remaining_quantity"] for i in items)
    all_fulfilled = total_remaining == 0 and total_ordered > 0
    partially_fulfilled = 0 < total_fulfilled < total_ordered
    unfulfilled_items = [i for i in items if i["remaining_quantity"] > 0]
    can_fulfill = (
        order.status in (Order.Status.PAID, Order.Status.PROCESSING)
        and len(unfulfilled_items) > 0
    )

    return {
        "items": items,
        "unfulfilled_items": unfulfilled_items,
        "total_ordered": total_ordered,
        "total_fulfilled": total_fulfilled,
        "total_remaining": total_remaining,
        "all_fulfilled": all_fulfilled,
        "is_fully_fulfilled": all_fulfilled,
        "partially_fulfilled": partially_fulfilled,
        "unfulfilled": total_fulfilled == 0,
        "can_fulfill": can_fulfill,
    }


def _notify_customer(
    order: Order,
    shipment: Shipment,
    notification_type: str,
    key_suffix: str,
    context: dict[str, Any] | None = None,
) -> None:
    """Safe, idempotent transactional notification helper for delivery updates."""
    ctx = {
        "order_number": order.number,
        "shipment_reference": shipment.reference or f"#{shipment.pk}",
        "carrier": shipment.carrier or "",
        "tracking_number": shipment.tracking_number or "",
        "tracking_url": shipment.safe_tracking_url or "",
        "status": shipment.get_status_display(),
        **(context or {}),
    }
    notify_emit(
        notification_type=notification_type,
        user=order.user,
        idempotency_key=f"shipment:{shipment.pk}:{key_suffix}",
        context=ctx,
        action_url=reverse("account:order-detail", args=[order.number]),
        related_object_type="shipment",
        related_object_id=shipment.pk,
    )


# =============================================================================
# Fulfillment Creation & Multi-Package Support
# =============================================================================


@transaction.atomic
def create_fulfillment(
    order: Order,
    *,
    items_data: list[dict[str, Any]] | None = None,
    staff_user=None,
    method: str = "",
    carrier: str = "",
    carrier_service: str = "",
    tracking_number: str = "",
    estimated_delivery: date | None = None,
    internal_notes: str = "",
    customer_notes: str = "",
) -> Shipment:
    """Create a new shipment package for eligible order items.

    Validates:
    - Order is in an eligible state (PAID or PROCESSING).
    - Unpaid, cancelled, or refunded orders are rejected.
    - Shipment quantities cannot exceed remaining unfulfilled quantities.
    - Prevents overshipment or empty fulfillments.
    """
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status in (Order.Status.PENDING_PAYMENT, Order.Status.CANCELLED, Order.Status.REFUNDED):
        raise ValidationError(
            f"Order {order.number} in status '{order.get_status_display()}' is not eligible for fulfillment."
        )

    # Calculate current unfulfilled quantities per line item
    unfulfilled_items = {
        item["order_item_id"]: item for item in get_unfulfilled_order_items(order)
    }

    to_fulfill: list[tuple[OrderItem, int]] = []
    if items_data is not None:
        if not items_data:
            raise ValidationError("At least one order item must be selected for fulfillment.")
        for entry in items_data:
            item_id = int(entry.get("order_item_id") or 0)
            qty = int(entry.get("quantity") or 0)
            if qty <= 0:
                raise ValidationError(f"Requested quantity for item {item_id} must be greater than zero.")
            if item_id not in unfulfilled_items:
                raise ValidationError(f"Order item {item_id} does not belong to order {order.number}.")
            remaining = unfulfilled_items[item_id]["remaining_quantity"]
            if qty > remaining:
                order_item = unfulfilled_items[item_id]["order_item"]
                raise ValidationError(
                    f"Requested quantity {qty} for {order_item.sku} exceeds unfulfilled quantity {remaining}."
                )
            to_fulfill.append((unfulfilled_items[item_id]["order_item"], qty))
    else:
        # Default: fulfill all remaining eligible items
        for entry in unfulfilled_items.values():
            if entry["remaining_quantity"] > 0:
                to_fulfill.append((entry["order_item"], entry["remaining_quantity"]))

    if not to_fulfill:
        raise ValidationError(f"All items for order {order.number} have already been fulfilled.")

    reference = generate_shipment_reference(order)
    shipment = Shipment.objects.create(
        order=order,
        reference=reference,
        status=Shipment.Status.PENDING,
        method=method or order.shipping_code or "standard",
        carrier=carrier.strip(),
        carrier_service=carrier_service.strip(),
        tracking_number=tracking_number.strip(),
        estimated_delivery=estimated_delivery,
        internal_notes=internal_notes.strip(),
        customer_notes=customer_notes.strip(),
    )

    # Create ShipmentItem records
    for order_item, qty in to_fulfill:
        ShipmentItem.objects.create(
            shipment=shipment,
            order_item=order_item,
            quantity=qty,
        )

    # Synchronize order status to PROCESSING if currently PAID
    if order.status == Order.Status.PAID:
        order.transition_to(
            Order.Status.PROCESSING,
            actor=staff_user,
            note=_("Fulfillment started for shipment %(ref)s") % {"ref": reference},
            metadata={"shipment": shipment.pk, "reference": reference},
        )

    ShipmentEvent.objects.create(
        shipment=shipment,
        event_type=Shipment.Status.PENDING,
        actor=staff_user,
        source=ShipmentEvent.Source.STAFF,
        note=_("Fulfillment package created with %(count)s item(s).") % {"count": len(to_fulfill)},
        metadata={"items": [{"sku": it.sku, "qty": q} for it, q in to_fulfill]},
    )

    OrderEvent.objects.create(
        order=order,
        event_type=OrderEvent.Type.SHIPMENT_CREATED,
        actor=staff_user,
        note=f"Shipment package {reference} created.",
        metadata={"shipment": shipment.pk, "reference": reference},
    )

    _notify_customer(order, shipment, NotificationType.SHIPMENT_CREATED, "created")
    return shipment


# =============================================================================
# Warehouse Picking & Packing Operations
# =============================================================================


@transaction.atomic
def start_picking(
    shipment: Shipment,
    *,
    staff_user=None,
    note: str = "",
) -> Shipment:
    """Advance shipment to PICKING state, recording picking staff and timestamp."""
    shipment = Shipment.objects.select_for_update().get(pk=shipment.pk)
    if not shipment.can_transition_to(Shipment.Status.PICKING):
        raise InvalidTransition("shipment", shipment.status, Shipment.Status.PICKING)

    shipment.picked_by = staff_user
    shipment.picked_at = timezone.now()
    shipment.transition_to(
        Shipment.Status.PICKING,
        actor=staff_user,
        note=note or _("Warehouse picking started."),
    )
    return shipment


@transaction.atomic
def mark_packed(
    shipment: Shipment,
    *,
    staff_user=None,
    package_weight_kg: Decimal | None = None,
    package_dimensions: str = "",
    internal_notes: str = "",
    customer_notes: str = "",
    note: str = "",
) -> Shipment:
    """Advance shipment to PACKED state with weights, dimensions, and staff notes."""
    shipment = Shipment.objects.select_for_update().get(pk=shipment.pk)
    if not shipment.can_transition_to(Shipment.Status.PACKED):
        raise InvalidTransition("shipment", shipment.status, Shipment.Status.PACKED)

    shipment.packed_by = staff_user
    shipment.packed_at = timezone.now()
    if package_weight_kg is not None:
        shipment.package_weight_kg = package_weight_kg
    if package_dimensions:
        shipment.package_dimensions = package_dimensions.strip()
    if internal_notes:
        shipment.internal_notes = internal_notes.strip()
    if customer_notes:
        shipment.customer_notes = customer_notes.strip()

    metadata: dict[str, Any] = {}
    if package_weight_kg:
        metadata["weight_kg"] = str(package_weight_kg)
    if package_dimensions:
        metadata["dimensions"] = package_dimensions

    shipment.transition_to(
        Shipment.Status.PACKED,
        actor=staff_user,
        note=note or _("Package picked, verified, and sealed."),
        metadata=metadata,
    )
    return shipment


# =============================================================================
# Dispatch & Carrier Hand-Off
# =============================================================================


@transaction.atomic
def dispatch_shipment(
    shipment: Shipment,
    *,
    staff_user=None,
    carrier: str = "",
    carrier_service: str = "",
    tracking_number: str = "",
    tracking_url: str = "",
    estimated_delivery: date | None = None,
    customer_notes: str = "",
    internal_notes: str = "",
    note: str = "",
) -> Shipment:
    """Hand the parcel to the carrier, moving shipment to SHIPPED / Dispatched."""
    shipment = Shipment.objects.select_for_update().get(pk=shipment.pk)
    order = Order.objects.select_for_update().get(pk=shipment.order_id)

    if not shipment.can_transition_to(Shipment.Status.SHIPPED):
        raise InvalidTransition("shipment", shipment.status, Shipment.Status.SHIPPED)

    if carrier:
        shipment.carrier = carrier.strip()
    if carrier_service:
        shipment.carrier_service = carrier_service.strip()
    if tracking_number:
        shipment.tracking_number = tracking_number.strip()
    if tracking_url:
        shipment.tracking_url = tracking_url.strip()
    if estimated_delivery is not None:
        shipment.estimated_delivery = estimated_delivery
    if customer_notes:
        shipment.customer_notes = customer_notes.strip()
    if internal_notes:
        shipment.internal_notes = internal_notes.strip()

    shipment.dispatched_by = staff_user
    shipment.shipped_at = timezone.now()

    meta = {
        "carrier": shipment.carrier,
        "tracking_number": shipment.tracking_number,
        "carrier_service": shipment.carrier_service,
    }
    shipment.transition_to(
        Shipment.Status.SHIPPED,
        actor=staff_user,
        note=note or _("Parcel dispatched and handed to carrier %(carrier)s.") % {"carrier": shipment.carrier or "courier"},
        metadata=meta,
    )

    # Align order status: if all items or shipments are dispatched, order moves to SHIPPED
    summary = get_order_fulfillment_summary(order)
    active_shipments = order.shipments.exclude(status=Shipment.Status.CANCELLED)
    all_dispatched = all(s.is_dispatched for s in active_shipments)
    if summary["all_fulfilled"] and all_dispatched and order.status != Order.Status.SHIPPED:
        if order.can_transition_to(Order.Status.SHIPPED):
            order.transition_to(
                Order.Status.SHIPPED,
                actor=staff_user,
                note=f"All packages dispatched (latest: {shipment.reference or shipment.pk}).",
                metadata={"shipment": shipment.pk},
            )

    _notify_customer(
        order,
        shipment,
        NotificationType.ORDER_SHIPPED,
        "shipped",
        context={"carrier": shipment.carrier, "tracking_number": shipment.tracking_number},
    )
    return shipment


# =============================================================================
# Tracking Updates, Courier Webhooks & Exception Handling
# =============================================================================


@transaction.atomic
def update_tracking_event(
    shipment: Shipment,
    *,
    event_type: str,
    status: str | None = None,
    location: str = "",
    note: str = "",
    occurred_at: datetime | None = None,
    source: str = ShipmentEvent.Source.STAFF,
    actor=None,
    metadata: dict[str, Any] | None = None,
) -> Shipment:
    """Record an operational or carrier tracking event with out-of-order protection.

    Guarantees:
    - Never regresses a DELIVERED shipment back to IN_TRANSIT or OUT_FOR_DELIVERY.
    - Idempotent and safe against repeated webhook deliveries.
    - Emits accurate notifications for OUT_FOR_DELIVERY, DELIVERED, and EXCEPTION states.
    """
    shipment = Shipment.objects.select_for_update().get(pk=shipment.pk)
    order = Order.objects.select_for_update().get(pk=shipment.order_id)
    now = timezone.now()
    event_time = occurred_at or now

    # Out-of-order check: do not regress DELIVERED back to in-transit states
    target_status = status or event_type
    if shipment.status == Shipment.Status.DELIVERED and target_status in (
        Shipment.Status.SHIPPED,
        Shipment.Status.IN_TRANSIT,
        Shipment.Status.OUT_FOR_DELIVERY,
    ):
        logger.info(
            "Ignored out-of-order status change to %s for already-delivered shipment %s",
            target_status,
            shipment.pk,
        )
        ShipmentEvent.objects.create(
            shipment=shipment,
            event_type=event_type or shipment.status,
            actor=actor,
            source=source,
            location=location,
            occurred_at=event_time,
            note=note or _("Carrier scan received (out-of-order; delivery already confirmed)."),
            metadata={
                "ignored_target_status": target_status,
                **(metadata or {}),
            },
        )
        return shipment

    # Perform valid transition if requested
    if target_status and target_status != shipment.status and shipment.can_transition_to(target_status):
        shipment.transition_to(
            target_status,
            actor=actor,
            note=note,
            location=location,
            occurred_at=event_time,
            source=source,
            metadata=metadata,
        )
    else:
        # Record tracking event scan without status change
        ShipmentEvent.objects.create(
            shipment=shipment,
            event_type=target_status or shipment.status,
            actor=actor,
            source=source,
            location=location,
            occurred_at=event_time,
            note=note or _("Tracking checkpoint: %(location)s") % {"location": location or "In transit"},
            metadata=metadata or {},
        )

    # Post-transition notifications and order alignments
    if shipment.status == Shipment.Status.OUT_FOR_DELIVERY:
        _notify_customer(
            order,
            shipment,
            NotificationType.SHIPMENT_OUT_FOR_DELIVERY,
            "out_for_delivery",
            context={"location": location},
        )
    elif shipment.status == Shipment.Status.DELIVERED:
        # Check if all active shipments are delivered
        all_delivered = all(
            s.status == Shipment.Status.DELIVERED
            for s in order.shipments.exclude(status=Shipment.Status.CANCELLED)
        )
        if all_delivered and order.status != Order.Status.DELIVERED:
            if order.can_transition_to(Order.Status.DELIVERED):
                order.transition_to(
                    Order.Status.DELIVERED,
                    actor=actor,
                    note=_("All packages delivered."),
                    metadata={"shipment": shipment.pk},
                )
        _notify_customer(
            order,
            shipment,
            NotificationType.ORDER_DELIVERED,
            "delivered",
            context={"location": location},
        )
    elif shipment.is_exception_state:
        _notify_customer(
            order,
            shipment,
            NotificationType.SHIPMENT_EXCEPTION,
            f"exception_{event_time.strftime('%Y%m%d%H%M')}",
            context={"reason": note, "location": location},
        )

    return shipment


@transaction.atomic
def record_delivery_exception(
    shipment: Shipment,
    *,
    staff_user=None,
    exception_type: str = Shipment.Status.EXCEPTION,
    reason: str = "",
    note: str = "",
    location: str = "",
) -> Shipment:
    """Record a delivery exception (failed attempt, delay, returned to sender).

    Does NOT automatically refund or restock -- routes to support / staff resolution.
    """
    shipment = Shipment.objects.select_for_update().get(pk=shipment.pk)
    if exception_type not in (
        Shipment.Status.EXCEPTION,
        Shipment.Status.ATTEMPT_FAILED,
        Shipment.Status.RETURNED_TO_SENDER,
    ):
        exception_type = Shipment.Status.EXCEPTION

    if not shipment.can_transition_to(exception_type):
        raise InvalidTransition("shipment", shipment.status, exception_type)

    full_note = note or reason or _("Delivery exception reported by carrier.")
    return update_tracking_event(
        shipment,
        event_type=exception_type,
        status=exception_type,
        location=location,
        note=full_note,
        source=ShipmentEvent.Source.STAFF,
        actor=staff_user,
        metadata={"reason": reason},
    )


@transaction.atomic
def cancel_fulfillment(
    shipment: Shipment,
    *,
    staff_user=None,
    reason: str = "",
) -> Shipment:
    """Cancel a shipment package before it has been dispatched to the carrier."""
    shipment = Shipment.objects.select_for_update().get(pk=shipment.pk)
    if shipment.is_dispatched or shipment.status == Shipment.Status.DELIVERED:
        raise ValidationError(
            f"Shipment {shipment.reference or shipment.pk} is already dispatched or delivered and cannot be cancelled."
        )

    if not shipment.can_transition_to(Shipment.Status.CANCELLED):
        raise InvalidTransition("shipment", shipment.status, Shipment.Status.CANCELLED)

    shipment.transition_to(
        Shipment.Status.CANCELLED,
        actor=staff_user,
        note=reason or _("Fulfillment cancelled before dispatch."),
    )
    return shipment
