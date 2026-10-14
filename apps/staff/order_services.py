"""Order processing, fulfillment workspace, and operational transition services (Phase 60).

Connects the dedicated FLASHWEAR Staff Portal to authoritative order, shipment,
payment, inventory, and audit models without duplicating core business logic.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.utils.translation import gettext_lazy as _

from apps.backoffice.services.audit import record as record_audit_event
from apps.orders.fulfillment_services import (
    create_fulfillment,
    dispatch_shipment,
    get_order_fulfillment_summary,
    get_unfulfilled_order_items,
    mark_packed,
    start_picking,
)
from apps.orders.models import (
    InvalidTransition,
    Order,
    Shipment,
)
from apps.payments.models import Payment
from apps.staff.constants import ORDERS_MANAGE, ORDERS_VIEW
from apps.staff.dashboard_services import parse_date_range
from apps.staff.permissions import has_staff_capability

logger = logging.getLogger("flashwear.staff.orders")

PAGE_SIZE = 20


def get_filtered_orders(user: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Retrieve filtered, searched, and paginated orders for the staff order workspace.

    Applies server-side role validation, query optimization (select_related & prefetch_related),
    and aggregates status counts for operational filter tabs.
    """
    if not has_staff_capability(user, ORDERS_VIEW):
        raise ValidationError(_("You do not have permission to view orders."))

    search_query = (params.get("q") or "").strip()
    status_filter = (params.get("status") or "all").strip().lower()
    payment_filter = (params.get("payment") or "").strip().lower()
    fulfillment_filter = (params.get("fulfillment") or "").strip().lower()
    date_range_key = (params.get("range") or "all").strip().lower()
    sort_key = (params.get("sort") or "-created_at").strip()
    page_number = params.get("page", 1)

    # Base Queryset with eagerly loaded relations
    qs = (
        Order.objects.select_related("user", "shipping_address", "payment")
        .prefetch_related("items", "shipments")
        .all()
    )

    # Calculate status counts across all orders for tab badges (cheap aggregation)
    counts = Order.objects.aggregate(
        all_count=Count("id"),
        awaiting_count=Count("id", filter=Q(status=Order.Status.PAID)),
        processing_count=Count("id", filter=Q(status=Order.Status.PROCESSING)),
        shipped_count=Count("id", filter=Q(status=Order.Status.SHIPPED)),
        delivered_count=Count("id", filter=Q(status=Order.Status.DELIVERED)),
        pending_count=Count("id", filter=Q(status=Order.Status.PENDING_PAYMENT)),
        cancelled_count=Count("id", filter=Q(status=Order.Status.CANCELLED)),
        refunded_count=Count("id", filter=Q(status=Order.Status.REFUNDED)),
    )

    # Exceptions count from shipments
    exception_orders_count = (
        Order.objects.filter(
            shipments__status__in=[
                Shipment.Status.ATTEMPT_FAILED,
                Shipment.Status.EXCEPTION,
                Shipment.Status.RETURNED_TO_SENDER,
            ]
        )
        .distinct()
        .count()
    )
    counts["exceptions_count"] = exception_orders_count

    # 1. Search Query Filter
    if search_query:
        qs = qs.filter(
            Q(number__icontains=search_query)
            | Q(user__email__icontains=search_query)
            | Q(user__first_name__icontains=search_query)
            | Q(user__last_name__icontains=search_query)
            | Q(shipping_address__full_name__icontains=search_query)
            | Q(shipping_address__phone__icontains=search_query)
            | Q(shipping_address__city__icontains=search_query)
            | Q(shipping_address__postal_code__icontains=search_query)
        )

    # 2. Order Status Filter
    if status_filter == "awaiting_processing" or status_filter == "paid":
        qs = qs.filter(status=Order.Status.PAID)
    elif status_filter == "processing":
        qs = qs.filter(status=Order.Status.PROCESSING)
    elif status_filter == "shipped":
        qs = qs.filter(status=Order.Status.SHIPPED)
    elif status_filter == "delivered":
        qs = qs.filter(status=Order.Status.DELIVERED)
    elif status_filter == "pending" or status_filter == "pending_payment":
        qs = qs.filter(status=Order.Status.PENDING_PAYMENT)
    elif status_filter == "cancelled":
        qs = qs.filter(status=Order.Status.CANCELLED)
    elif status_filter == "refunded":
        qs = qs.filter(status=Order.Status.REFUNDED)
    elif status_filter == "actionable":
        qs = qs.filter(status__in=[Order.Status.PAID, Order.Status.PROCESSING])
    elif status_filter == "exceptions":
        qs = qs.filter(
            shipments__status__in=[
                Shipment.Status.ATTEMPT_FAILED,
                Shipment.Status.EXCEPTION,
                Shipment.Status.RETURNED_TO_SENDER,
            ]
        ).distinct()

    # 3. Payment Status Filter
    if payment_filter == "succeeded" or payment_filter == "paid":
        qs = qs.filter(
            Q(payment__status=Payment.Status.SUCCEEDED)
            | Q(
                status__in=[
                    Order.Status.PAID,
                    Order.Status.PROCESSING,
                    Order.Status.SHIPPED,
                    Order.Status.DELIVERED,
                ]
            )
        )
    elif payment_filter == "pending":
        qs = qs.filter(
            Q(payment__status=Payment.Status.PENDING)
            | (Q(payment__isnull=True) & Q(status=Order.Status.PENDING_PAYMENT))
        )
    elif payment_filter == "failed":
        qs = qs.filter(payment__status=Payment.Status.FAILED)
    elif payment_filter == "unpaid":
        qs = qs.filter(status=Order.Status.PENDING_PAYMENT)

    # 4. Fulfillment Status Filter
    if fulfillment_filter == "unfulfilled":
        qs = qs.filter(
            Q(status=Order.Status.PAID)
            | (
                Q(status=Order.Status.PROCESSING)
                & Q(shipments__status__in=[Shipment.Status.PENDING])
            )
        ).distinct()
    elif fulfillment_filter == "picking":
        qs = qs.filter(
            shipments__status__in=[Shipment.Status.PICKING, Shipment.Status.PROCESSING]
        ).distinct()
    elif fulfillment_filter == "packed":
        qs = qs.filter(shipments__status=Shipment.Status.PACKED).distinct()
    elif fulfillment_filter == "shipped" or fulfillment_filter == "dispatched":
        qs = qs.filter(shipments__status=Shipment.Status.SHIPPED).distinct()

    # 5. Date Range Filter
    start_date, end_date = parse_date_range(date_range_key)
    if start_date:
        qs = qs.filter(created_at__gte=start_date, created_at__lte=end_date)

    # 6. Sorting
    allowed_sorts = {
        "-created_at": "-created_at",
        "created_at": "created_at",
        "number": "number",
        "-number": "-number",
        "-total": "-total",
        "total": "total",
        "status": "status",
        "-status": "-status",
    }
    order_by = allowed_sorts.get(sort_key, "-created_at")
    qs = qs.order_by(order_by)

    # 7. Pagination
    paginator = Paginator(qs, PAGE_SIZE)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return {
        "orders": page_obj,
        "paginator": paginator,
        "counts": counts,
        "search_query": search_query,
        "status_filter": status_filter,
        "payment_filter": payment_filter,
        "fulfillment_filter": fulfillment_filter,
        "date_range": date_range_key,
        "sort_key": sort_key,
        "total_results": paginator.count,
    }


def get_order_details(order_id: int, user: Any) -> dict[str, Any]:
    """Retrieve detailed order info, fulfillment progress, timeline, and valid staff actions."""
    if not has_staff_capability(user, ORDERS_VIEW):
        raise ValidationError(_("You do not have permission to view orders."))

    try:
        order = (
            Order.objects.select_related("user", "shipping_address", "payment")
            .prefetch_related(
                "items__variant__product",
                "shipments__items__order_item",
                "events__actor",
                "shipments__events__actor",
            )
            .get(pk=order_id)
        )
    except Order.DoesNotExist as err:
        raise ValidationError(_("Order not found.")) from err

    can_manage = has_staff_capability(user, ORDERS_MANAGE)

    # Line items with option information
    items = list(order.items.select_related("variant__product").all())

    # Shipments
    shipments = list(order.shipments.exclude(status=Shipment.Status.CANCELLED).all())
    active_shipment = shipments[0] if shipments else None

    # Fulfillment items and quantities
    unfulfilled_items = get_unfulfilled_order_items(order)
    fulfillment_summary = get_order_fulfillment_summary(order)

    # Merged unified timeline
    timeline_events: list[dict[str, Any]] = []

    for ev in order.events.all():
        actor_name = ev.actor.get_full_name() or ev.actor.email if ev.actor else _("System")
        timeline_events.append(
            {
                "id": f"order-{ev.pk}",
                "timestamp": ev.created_at,
                "category": "order",
                "event_type": ev.event_type,
                "display_type": ev.get_event_type_display(),
                "actor": actor_name,
                "note": ev.note or ev.get_event_type_display(),
                "metadata": ev.metadata or {},
            }
        )

    for shp in shipments:
        for ev in shp.events.all():
            actor_name = ev.actor.get_full_name() or ev.actor.email if ev.actor else _("System")
            timeline_events.append(
                {
                    "id": f"shipment-{ev.pk}",
                    "timestamp": ev.occurred_at,
                    "category": "shipment",
                    "event_type": ev.event_type,
                    "display_type": ev.get_event_type_display(),
                    "actor": actor_name,
                    "note": ev.note or ev.get_event_type_display(),
                    "metadata": ev.metadata or {},
                    "shipment_ref": shp.reference,
                }
            )

    timeline_events.sort(key=lambda x: x["timestamp"], reverse=True)

    # Next valid action determinations based on status and permissions
    can_process = can_manage and order.status == Order.Status.PAID
    can_pick = False
    can_pack = False
    can_dispatch = False

    if can_manage and order.status == Order.Status.PROCESSING and active_shipment:
        if active_shipment.status == Shipment.Status.PENDING:
            can_pick = True
        elif active_shipment.status in (Shipment.Status.PICKING, Shipment.Status.PROCESSING):
            can_pack = True
        elif active_shipment.status == Shipment.Status.PACKED:
            can_dispatch = True

    # Packing slip is printable as long as order is not cancelled
    can_print_slip = order.status != Order.Status.CANCELLED

    # Check payment verification status
    is_payment_verified = order.status in (
        Order.Status.PAID,
        Order.Status.PROCESSING,
        Order.Status.SHIPPED,
        Order.Status.DELIVERED,
    ) or (
        hasattr(order, "payment")
        and order.payment
        and order.payment.status == Payment.Status.SUCCEEDED
    )

    return {
        "order": order,
        "items": items,
        "shipments": shipments,
        "active_shipment": active_shipment,
        "unfulfilled_items": unfulfilled_items,
        "fulfillment_summary": fulfillment_summary,
        "timeline_events": timeline_events,
        "can_manage": can_manage,
        "can_process": can_process,
        "can_pick": can_pick,
        "can_pack": can_pack,
        "can_dispatch": can_dispatch,
        "can_print_slip": can_print_slip,
        "is_payment_verified": is_payment_verified,
    }


@transaction.atomic
def process_order_action(order_id: int, actor: Any) -> Shipment:
    """Transition an eligible PAID order into PROCESSING and create its fulfillment package.

    Enforces:
    - User has ORDERS_MANAGE capability.
    - Locks order row with select_for_update to prevent concurrency race conditions.
    - Idempotent: if already PROCESSING, returns existing shipment gracefully.
    - Rejects UNPAID, CANCELLED, or REFUNDED orders.
    - Records OrderEvent and AuditEvent.
    """
    if not has_staff_capability(actor, ORDERS_MANAGE):
        raise ValidationError(_("You do not have permission to process orders."))

    try:
        order = Order.objects.select_for_update().get(pk=order_id)
    except Order.DoesNotExist as err:
        raise ValidationError(_("Order not found.")) from err

    if order.status == Order.Status.PENDING_PAYMENT:
        raise ValidationError(
            _(
                "Order %(number)s is awaiting customer payment. "
                "Orders cannot be processed until payment is verified by the payment gateway."
            )
            % {"number": order.number}
        )

    if order.status in (Order.Status.CANCELLED, Order.Status.REFUNDED):
        raise ValidationError(
            _("Order %(number)s is in status '%(status)s' and cannot be processed for fulfillment.")
            % {"number": order.number, "status": order.get_status_display()}
        )

    # Idempotency check: if order is already processing and has an active shipment, return it
    if order.status == Order.Status.PROCESSING:
        existing_shipment = (
            order.shipments.exclude(status=Shipment.Status.CANCELLED)
            .order_by("-created_at")
            .first()
        )
        if existing_shipment:
            return existing_shipment

    # Create fulfillment package and transition order to PROCESSING
    shipment = create_fulfillment(order, staff_user=actor)

    # Log operational audit event
    record_audit_event(
        actor=actor,
        domain="orders",
        action="order.process",
        object_type="order",
        object_id=str(order.pk),
        object_repr=f"Order {order.number}",
        metadata={
            "order_number": order.number,
            "shipment_reference": shipment.reference,
            "status": Order.Status.PROCESSING,
        },
    )

    logger.info(
        "Staff %s marked order %s as PROCESSING (shipment %s)",
        actor,
        order.number,
        shipment.reference,
    )
    return shipment


@transaction.atomic
def start_picking_action(
    order_id: int,
    *,
    shipment_id: int | None = None,
    actor: Any,
    note: str = "",
) -> Shipment:
    """Advance shipment to PICKING state, attributing actor and timestamp."""
    if not has_staff_capability(actor, ORDERS_MANAGE):
        raise ValidationError(_("You do not have permission to manage fulfillment."))

    try:
        order = Order.objects.select_for_update().get(pk=order_id)
    except Order.DoesNotExist as err:
        raise ValidationError(_("Order not found.")) from err

    if order.status not in (Order.Status.PROCESSING, Order.Status.PAID):
        raise ValidationError(
            _("Cannot start picking for order in status '%(status)s'.")
            % {"status": order.get_status_display()}
        )

    shipments_qs = order.shipments.exclude(status=Shipment.Status.CANCELLED).select_for_update()
    if shipment_id:
        shipments_qs = shipments_qs.filter(pk=shipment_id)

    shipment = shipments_qs.order_by("-created_at").first()
    if not shipment:
        # If no shipment package exists, open one first
        shipment = create_fulfillment(order, staff_user=actor)

    if shipment.status == Shipment.Status.PICKING:
        # Idempotent return
        return shipment

    try:
        updated_shipment = start_picking(
            shipment,
            staff_user=actor,
            note=note or _("Warehouse picking started by staff."),
        )
    except InvalidTransition as exc:
        raise ValidationError(str(exc)) from exc

    record_audit_event(
        actor=actor,
        domain="orders",
        action="shipment.picking_started",
        object_type="order",
        object_id=str(order.pk),
        object_repr=f"Order {order.number}",
        reason=note or "",
        metadata={
            "order_number": order.number,
            "shipment_reference": updated_shipment.reference,
            "status": Shipment.Status.PICKING,
        },
    )
    return updated_shipment


@transaction.atomic
def mark_packed_action(
    order_id: int,
    *,
    shipment_id: int | None = None,
    actor: Any,
    package_weight_kg: Decimal | None = None,
    package_dimensions: str = "",
    internal_notes: str = "",
    note: str = "",
) -> Shipment:
    """Record package packing completion with dimensions and quality checklist notes."""
    if not has_staff_capability(actor, ORDERS_MANAGE):
        raise ValidationError(_("You do not have permission to record packing completion."))

    try:
        order = Order.objects.select_for_update().get(pk=order_id)
    except Order.DoesNotExist as err:
        raise ValidationError(_("Order not found.")) from err

    shipments_qs = order.shipments.exclude(status=Shipment.Status.CANCELLED).select_for_update()
    if shipment_id:
        shipments_qs = shipments_qs.filter(pk=shipment_id)

    shipment = shipments_qs.order_by("-created_at").first()
    if not shipment:
        raise ValidationError(_("No active shipment found to pack."))

    if shipment.status == Shipment.Status.PACKED:
        # Idempotent
        return shipment

    try:
        updated_shipment = mark_packed(
            shipment,
            staff_user=actor,
            package_weight_kg=package_weight_kg,
            package_dimensions=package_dimensions,
            internal_notes=internal_notes,
            note=note or _("Package picked, verified, and sealed for shipment."),
        )
    except InvalidTransition as exc:
        raise ValidationError(str(exc)) from exc

    record_audit_event(
        actor=actor,
        domain="orders",
        action="shipment.packed",
        object_type="order",
        object_id=str(order.pk),
        object_repr=f"Order {order.number}",
        reason=note or "",
        metadata={
            "order_number": order.number,
            "shipment_reference": updated_shipment.reference,
            "status": Shipment.Status.PACKED,
            "weight_kg": str(package_weight_kg) if package_weight_kg else None,
        },
    )
    return updated_shipment


@transaction.atomic
def dispatch_order_action(
    order_id: int,
    *,
    shipment_id: int | None = None,
    actor: Any,
    carrier: str = "",
    carrier_service: str = "",
    tracking_number: str = "",
    tracking_url: str = "",
    internal_notes: str = "",
    note: str = "",
) -> Shipment:
    """Hand the packed order to the courier, advancing shipment and order to SHIPPED."""
    if not has_staff_capability(actor, ORDERS_MANAGE):
        raise ValidationError(_("You do not have permission to dispatch orders."))

    try:
        order = Order.objects.select_for_update().get(pk=order_id)
    except Order.DoesNotExist as err:
        raise ValidationError(_("Order not found.")) from err

    shipments_qs = order.shipments.exclude(status=Shipment.Status.CANCELLED).select_for_update()
    if shipment_id:
        shipments_qs = shipments_qs.filter(pk=shipment_id)

    shipment = shipments_qs.order_by("-created_at").first()
    if not shipment:
        raise ValidationError(_("No active shipment found to dispatch."))

    if shipment.status == Shipment.Status.SHIPPED:
        # Idempotent
        return shipment

    try:
        updated_shipment = dispatch_shipment(
            shipment,
            staff_user=actor,
            carrier=carrier,
            carrier_service=carrier_service,
            tracking_number=tracking_number,
            tracking_url=tracking_url,
            internal_notes=internal_notes,
            note=note or _("Shipment handed to courier."),
        )
    except InvalidTransition as exc:
        raise ValidationError(str(exc)) from exc

    record_audit_event(
        actor=actor,
        domain="orders",
        action="shipment.dispatched",
        object_type="order",
        object_id=str(order.pk),
        object_repr=f"Order {order.number}",
        reason=note or "",
        metadata={
            "order_number": order.number,
            "shipment_reference": updated_shipment.reference,
            "carrier": carrier,
            "tracking_number": tracking_number,
            "status": Shipment.Status.SHIPPED,
        },
    )
    return updated_shipment
