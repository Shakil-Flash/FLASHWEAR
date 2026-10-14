"""Daily operations center and unified dashboard services for the staff portal (Phase 58).

Provides:
- Role-aware business status aggregations with authoritative definitions.
- "My Work" queues (items assigned to the current employee).
- Team queues and unassigned work items (orders, shipments, support tickets).
- Urgent operational alerts (SLA escalations, delivery exceptions, stockouts).
- Permission-filtered recent activity feed from AuditEvent.
- Context-sensitive quick actions.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count, F, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.backoffice.models import AuditEvent
from apps.staff.constants import (
    DELIVERIES_VIEW,
    INVENTORY_VIEW,
    ORDERS_VIEW,
    PRODUCTS_MANAGE,
    PRODUCTS_VIEW,
    STAFF_MANAGE,
    SUPPORT_VIEW,
)
from apps.staff.permissions import (
    get_user_capabilities,
    is_owner_or_superuser,
)

logger = logging.getLogger("flashwear.staff.dashboard")


def parse_date_range(range_key: str | None) -> tuple[timezone.datetime | None, timezone.datetime]:
    """Calculate date boundaries for dashboard filtering based on selected range."""
    now = timezone.now()
    if not range_key or range_key == "all":
        return None, now

    if range_key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now

    if range_key == "7d":
        start = now - timedelta(days=7)
        return start, now

    if range_key == "30d":
        start = now - timedelta(days=30)
        return start, now

    return None, now


def get_daily_operations_summary(
    user: Any,
    range_key: str | None = None,
) -> dict[str, Any]:
    """Aggregate authoritative operational status counts scoped to user permissions."""
    caps = get_user_capabilities(user)
    start_date, end_date = parse_date_range(range_key)
    summary: dict[str, Any] = {}

    # 1. Products & Catalog
    if PRODUCTS_VIEW in caps:
        from apps.catalog.models import Product

        prod_qs = Product.objects.all()
        if start_date:
            prod_qs = prod_qs.filter(created_at__gte=start_date, created_at__lte=end_date)

        prod_agg = prod_qs.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status=Product.Status.ACTIVE)),
            draft=Count("id", filter=Q(status=Product.Status.DRAFT)),
            archived=Count("id", filter=Q(status=Product.Status.ARCHIVED)),
            missing_description=Count(
                "id",
                filter=Q(short_description="") | Q(short_description__isnull=True),
            ),
        )
        summary["products"] = {
            "total": prod_agg["total"],
            "published": prod_agg["active"],
            "draft": prod_agg["draft"],
            "archived": prod_agg["archived"],
            "requires_attention": prod_agg["draft"] + prod_agg["missing_description"],
        }

    # 2. Inventory Health
    if INVENTORY_VIEW in caps:
        from apps.inventory.models import Stock

        stock_threshold = getattr(settings, "BACKOFFICE_LOW_STOCK_THRESHOLD", 5)
        stock_qs = Stock.objects.all()

        stock_agg = stock_qs.aggregate(
            total_variants=Count("id"),
            out_of_stock=Count("id", filter=Q(on_hand__lte=0)),
            low_stock=Count(
                "id",
                filter=Q(on_hand__gt=0, on_hand__lte=F("low_stock_threshold"))
                | (
                    Q(low_stock_threshold__isnull=True)
                    & Q(on_hand__gt=0, on_hand__lte=stock_threshold)
                ),
            ),
        )
        summary["inventory"] = {
            "total_tracked": stock_agg["total_variants"],
            "out_of_stock": stock_agg["out_of_stock"],
            "low_stock": stock_agg["low_stock"],
            "stock_risks": stock_agg["out_of_stock"] + stock_agg["low_stock"],
        }

    # 3. Orders & Processing Queues
    if ORDERS_VIEW in caps:
        from apps.orders.models import Order, ReturnRequest

        order_qs = Order.objects.all()
        if start_date:
            order_qs = order_qs.filter(created_at__gte=start_date, created_at__lte=end_date)

        order_agg = order_qs.aggregate(
            total=Count("id"),
            pending_payment=Count("id", filter=Q(status=Order.Status.PENDING_PAYMENT)),
            awaiting_processing=Count("id", filter=Q(status=Order.Status.PAID)),
            processing=Count("id", filter=Q(status=Order.Status.PROCESSING)),
            shipped=Count("id", filter=Q(status=Order.Status.SHIPPED)),
            delivered=Count("id", filter=Q(status=Order.Status.DELIVERED)),
        )

        # Returns summary
        return_qs = ReturnRequest.objects.all()
        if start_date:
            return_qs = return_qs.filter(created_at__gte=start_date, created_at__lte=end_date)

        return_agg = return_qs.aggregate(
            pending_review=Count(
                "id",
                filter=Q(
                    status__in=[
                        ReturnRequest.Status.REQUESTED,
                        ReturnRequest.Status.UNDER_REVIEW,
                    ]
                ),
            ),
            awaiting_receipt=Count(
                "id",
                filter=Q(
                    status__in=[
                        ReturnRequest.Status.APPROVED,
                        ReturnRequest.Status.AWAITING_SHIPMENT,
                        ReturnRequest.Status.RECEIVED,
                    ]
                ),
            ),
            in_inspection=Count(
                "id",
                filter=Q(
                    status__in=[
                        ReturnRequest.Status.INSPECTED,
                        ReturnRequest.Status.INSPECTION_PASSED,
                        ReturnRequest.Status.INSPECTION_FAILED,
                    ]
                ),
            ),
        )

        summary["orders"] = {
            "total": order_agg["total"],
            "pending_payment": order_agg["pending_payment"],
            "awaiting_processing": order_agg["awaiting_processing"],
            "processing": order_agg["processing"],
            "actionable_queue": order_agg["awaiting_processing"] + order_agg["processing"],
            "shipped": order_agg["shipped"],
            "delivered": order_agg["delivered"],
        }
        summary["returns"] = {
            "pending_review": return_agg["pending_review"],
            "awaiting_receipt": return_agg["awaiting_receipt"],
            "in_inspection": return_agg["in_inspection"],
            "total_pending": return_agg["pending_review"] + return_agg["awaiting_receipt"],
        }

    # 4. Deliveries & Shipping
    if DELIVERIES_VIEW in caps:
        from apps.orders.models import Shipment

        shipment_qs = Shipment.objects.all()
        if start_date:
            shipment_qs = shipment_qs.filter(created_at__gte=start_date, created_at__lte=end_date)

        shipment_agg = shipment_qs.aggregate(
            total=Count("id"),
            awaiting_dispatch=Count(
                "id",
                filter=Q(
                    status__in=[
                        Shipment.Status.PENDING,
                        Shipment.Status.PICKING,
                        Shipment.Status.PACKED,
                        Shipment.Status.PROCESSING,
                    ]
                ),
            ),
            in_transit=Count(
                "id",
                filter=Q(
                    status__in=[
                        Shipment.Status.SHIPPED,
                        Shipment.Status.IN_TRANSIT,
                        Shipment.Status.OUT_FOR_DELIVERY,
                    ]
                ),
            ),
            exceptions=Count(
                "id",
                filter=Q(
                    status__in=[
                        Shipment.Status.ATTEMPT_FAILED,
                        Shipment.Status.EXCEPTION,
                        Shipment.Status.RETURNED_TO_SENDER,
                    ]
                ),
            ),
            delivered=Count("id", filter=Q(status=Shipment.Status.DELIVERED)),
        )
        summary["deliveries"] = {
            "total": shipment_agg["total"],
            "awaiting_dispatch": shipment_agg["awaiting_dispatch"],
            "in_transit": shipment_agg["in_transit"],
            "exceptions": shipment_agg["exceptions"],
            "delivered": shipment_agg["delivered"],
        }

    # 5. Customer Support Tickets
    if SUPPORT_VIEW in caps:
        from apps.support.models import SupportTicket

        ticket_qs = SupportTicket.objects.all()
        if start_date:
            ticket_qs = ticket_qs.filter(created_at__gte=start_date, created_at__lte=end_date)

        ticket_agg = ticket_qs.aggregate(
            total=Count("id"),
            open=Count("id", filter=Q(status=SupportTicket.Status.OPEN)),
            in_progress=Count("id", filter=Q(status=SupportTicket.Status.IN_PROGRESS)),
            escalated=Count("id", filter=Q(status=SupportTicket.Status.ESCALATED)),
            waiting_customer=Count(
                "id", filter=Q(status=SupportTicket.Status.WAITING_FOR_CUSTOMER)
            ),
            urgent=Count(
                "id",
                filter=Q(
                    priority=SupportTicket.Priority.URGENT,
                    status__in=[
                        SupportTicket.Status.OPEN,
                        SupportTicket.Status.IN_PROGRESS,
                        SupportTicket.Status.ESCALATED,
                    ],
                ),
            ),
            unassigned=Count(
                "id",
                filter=Q(
                    assigned_to__isnull=True,
                    status__in=[
                        SupportTicket.Status.OPEN,
                        SupportTicket.Status.ESCALATED,
                    ],
                ),
            ),
        )
        summary["support"] = {
            "total": ticket_agg["total"],
            "open": ticket_agg["open"],
            "in_progress": ticket_agg["in_progress"],
            "escalated": ticket_agg["escalated"],
            "urgent": ticket_agg["urgent"],
            "unassigned": ticket_agg["unassigned"],
            "active_tickets": (
                ticket_agg["open"] + ticket_agg["in_progress"] + ticket_agg["escalated"]
            ),
        }

    # 6. Staff & System Accounts
    if STAFF_MANAGE in caps:
        from apps.staff.services import get_all_staff_users

        staff_qs = get_all_staff_users()
        active_staff = staff_qs.filter(is_active=True).count()
        inactive_staff = staff_qs.filter(is_active=False).count()
        summary["staff"] = {
            "total": active_staff + inactive_staff,
            "active_count": active_staff,
            "inactive_count": inactive_staff,
        }

    return summary


def get_my_work_and_team_queues(user: Any) -> dict[str, Any]:
    """Retrieve work assigned to the current employee and unassigned team queues."""
    caps = get_user_capabilities(user)

    my_work: list[dict[str, Any]] = []
    team_queues: list[dict[str, Any]] = []
    urgent_alerts: list[dict[str, Any]] = []

    # 1. Support Tickets
    if SUPPORT_VIEW in caps:
        from apps.support.models import SupportTicket

        # My Assigned Tickets
        assigned_tickets = (
            SupportTicket.objects.filter(
                assigned_to=user,
                status__in=[
                    SupportTicket.Status.OPEN,
                    SupportTicket.Status.IN_PROGRESS,
                    SupportTicket.Status.ESCALATED,
                    SupportTicket.Status.WAITING_INTERNAL,
                ],
            )
            .select_related("customer")
            .order_by("-priority", "created_at")[:8]
        )
        for t in assigned_tickets:
            cust_email = t.customer.email if t.customer else "Guest"
            is_urgent = (
                t.priority == SupportTicket.Priority.URGENT
                or t.status == SupportTicket.Status.ESCALATED
            )
            my_work.append(
                {
                    "type": "ticket",
                    "title": f"Ticket #{t.number}: {t.subject}",
                    "subtitle": f"Customer: {cust_email} • Priority: {t.priority.upper()}",
                    "status": t.get_status_display(),
                    "priority": t.priority,
                    "is_urgent": is_urgent,
                    "timestamp": t.created_at,
                    "url": reverse("staff:support"),
                }
            )

        # Unassigned Team Tickets
        unassigned_tickets = (
            SupportTicket.objects.filter(
                assigned_to__isnull=True,
                status__in=[SupportTicket.Status.OPEN, SupportTicket.Status.ESCALATED],
            )
            .select_related("customer")
            .order_by("-priority", "created_at")[:8]
        )
        for t in unassigned_tickets:
            team_queues.append(
                {
                    "type": "unassigned_ticket",
                    "title": f"Ticket #{t.number}: {t.subject}",
                    "subtitle": f"Unassigned • Priority: {t.priority.upper()}",
                    "status": t.get_status_display(),
                    "priority": t.priority,
                    "is_urgent": t.priority == SupportTicket.Priority.URGENT,
                    "timestamp": t.created_at,
                    "url": reverse("staff:support"),
                }
            )

        # Escalated or Urgent Alerts
        escalated_tickets = SupportTicket.objects.filter(
            status=SupportTicket.Status.ESCALATED,
        ).order_by("-updated_at")[:5]
        for t in escalated_tickets:
            urgent_alerts.append(
                {
                    "severity": "critical",
                    "title": f"Escalated Support Ticket #{t.number}",
                    "message": t.escalation_reason or "Escalated for immediate review.",
                    "timestamp": t.escalated_at or t.updated_at,
                    "url": reverse("staff:support"),
                }
            )

    # 2. Fulfillment & Deliveries
    if DELIVERIES_VIEW in caps:
        from apps.orders.models import Shipment

        # Shipments assigned to or handled by current user
        my_shipments = (
            Shipment.objects.filter(
                Q(picked_by=user) | Q(packed_by=user) | Q(dispatched_by=user),
                status__in=[
                    Shipment.Status.PENDING,
                    Shipment.Status.PICKING,
                    Shipment.Status.PACKED,
                    Shipment.Status.PROCESSING,
                ],
            )
            .select_related("order")
            .order_by("-updated_at")[:8]
        )
        for s in my_shipments:
            carrier_label = s.carrier or "Pending"
            my_work.append(
                {
                    "type": "shipment",
                    "title": f"Shipment #{s.reference} (Order #{s.order.number})",
                    "subtitle": f"Carrier: {carrier_label} • Status: {s.get_status_display()}",
                    "status": s.get_status_display(),
                    "priority": "normal",
                    "is_urgent": False,
                    "timestamp": s.updated_at,
                    "url": f"{reverse('staff:deliveries')}?q={s.reference}",
                }
            )

        # Unassigned Shipments Awaiting Picking
        unassigned_shipments = (
            Shipment.objects.filter(
                picked_by__isnull=True,
                status=Shipment.Status.PENDING,
            )
            .select_related("order")
            .order_by("created_at")[:8]
        )
        for s in unassigned_shipments:
            team_queues.append(
                {
                    "type": "unassigned_shipment",
                    "title": f"Shipment #{s.reference} (Order #{s.order.number})",
                    "subtitle": "Awaiting Warehouse Picking",
                    "status": s.get_status_display(),
                    "priority": "normal",
                    "is_urgent": False,
                    "timestamp": s.created_at,
                    "url": reverse("staff:deliveries"),
                }
            )

        # Delivery Exceptions
        exceptions = (
            Shipment.objects.filter(
                status__in=[
                    Shipment.Status.ATTEMPT_FAILED,
                    Shipment.Status.EXCEPTION,
                    Shipment.Status.RETURNED_TO_SENDER,
                ]
            )
            .select_related("order")
            .order_by("-updated_at")[:5]
        )
        for s in exceptions:
            order_num = s.order.number if s.order else "Unknown"
            urgent_alerts.append(
                {
                    "severity": "warning",
                    "title": f"Delivery Exception: Shipment #{s.reference}",
                    "message": f"Carrier alert: {s.get_status_display()} for Order #{order_num}.",
                    "timestamp": s.updated_at,
                    "url": f"{reverse('staff:deliveries')}?status=exceptions",
                }
            )

    # 3. Inventory Stockout Alerts
    if INVENTORY_VIEW in caps:
        from apps.inventory.models import Stock

        out_of_stock = (
            Stock.objects.filter(on_hand__lte=0)
            .select_related("variant__product")
            .order_by("updated_at")[:5]
        )
        for stock in out_of_stock:
            sku = stock.variant.sku if stock.variant else "Unknown"
            product_name = (
                stock.variant.product.name if stock.variant and stock.variant.product else sku
            )
            urgent_alerts.append(
                {
                    "severity": "critical",
                    "title": f"Stockout Risk: {sku}",
                    "message": f"Zero units on hand for '{product_name}'. Reorder required.",
                    "timestamp": stock.updated_at,
                    "url": f"{reverse('staff:inventory')}?status=out_of_stock",
                }
            )

    return {
        "my_work": my_work[:10],
        "team_queues": team_queues[:10],
        "urgent_alerts": urgent_alerts[:8],
    }


def get_recent_activity_feed(user: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Retrieve recent audit activity filtered strictly to the user's operational scope."""
    caps = get_user_capabilities(user)
    allowed_domains: list[str] = []

    # Map capabilities to audit domains
    if is_owner_or_superuser(user) or STAFF_MANAGE in caps:
        # Owners and managers can view all staff-relevant activity
        allowed_domains = [
            "catalog",
            "product",
            "order",
            "shipping",
            "delivery",
            "inventory",
            "support",
            "staff",
        ]
    else:
        if PRODUCTS_VIEW in caps:
            allowed_domains.extend(["catalog", "product"])
        if ORDERS_VIEW in caps:
            allowed_domains.extend(["order", "returns"])
        if DELIVERIES_VIEW in caps:
            allowed_domains.extend(["shipping", "delivery"])
        if INVENTORY_VIEW in caps:
            allowed_domains.append("inventory")
        if SUPPORT_VIEW in caps:
            allowed_domains.append("support")

    if not allowed_domains:
        return []

    events_qs = (
        AuditEvent.objects.filter(domain__in=allowed_domains)
        .select_related("actor")
        .order_by("-created_at")[:limit]
    )

    feed: list[dict[str, Any]] = []
    for ev in events_qs:
        # Determine human-friendly presentation
        is_warning = any(
            w in ev.action.lower() or w in (ev.reason or "").lower()
            for w in ["deactivate", "exception", "failed", "cancel", "revoke", "out_of_stock"]
        )
        feed.append(
            {
                "id": ev.id,
                "actor_email": ev.actor.email if ev.actor else ev.actor_label or "System",
                "action": ev.action,
                "domain": ev.domain.capitalize(),
                "object_repr": ev.object_repr or f"{ev.object_type} #{ev.object_id}",
                "reason": ev.reason or "",
                "created_at": ev.created_at,
                "is_warning": is_warning,
            }
        )

    return feed


def get_quick_actions(user: Any) -> list[dict[str, Any]]:
    """Generate permitted operational shortcuts with exact destinations and permissions."""
    caps = get_user_capabilities(user)
    actions: list[dict[str, Any]] = []

    if PRODUCTS_MANAGE in caps or PRODUCTS_VIEW in caps:
        actions.append(
            {
                "key": "products_drafts",
                "label": _("Review Draft Products"),
                "description": _("Inspect draft items awaiting publishing"),
                "url": f"{reverse('staff:products')}?status=draft",
                "icon": "document-search",
                "badge": None,
            }
        )

    if ORDERS_VIEW in caps:
        actions.append(
            {
                "key": "orders_queue",
                "label": _("Order Processing Queue"),
                "description": _("Review paid orders ready for warehouse fulfillment"),
                "url": f"{reverse('staff:orders')}?status=processing",
                "icon": "shopping-bag",
                "badge": None,
            }
        )
        actions.append(
            {
                "key": "returns_pending",
                "label": _("Inspect Pending Returns"),
                "description": _("Review customer return and exchange requests"),
                "url": f"{reverse('staff:orders')}?type=returns",
                "icon": "arrow-path",
                "badge": None,
            }
        )

    if INVENTORY_VIEW in caps:
        actions.append(
            {
                "key": "inventory_low_stock",
                "label": _("Low-Stock Items"),
                "description": _("View variants near or below reorder threshold"),
                "url": f"{reverse('staff:inventory')}?status=low_stock",
                "icon": "exclamation-triangle",
                "badge": None,
            }
        )

    if DELIVERIES_VIEW in caps:
        actions.append(
            {
                "key": "deliveries_exceptions",
                "label": _("Delivery Exceptions"),
                "description": _("Inspect failed delivery attempts and transit issues"),
                "url": f"{reverse('staff:deliveries')}?status=exceptions",
                "icon": "truck",
                "badge": None,
            }
        )

    if SUPPORT_VIEW in caps:
        actions.append(
            {
                "key": "support_urgent",
                "label": _("Urgent Support Desk"),
                "description": _("Open urgent and escalated customer support tickets"),
                "url": f"{reverse('staff:support')}?status=urgent",
                "icon": "chat-bubble-left-right",
                "badge": None,
            }
        )

    if STAFF_MANAGE in caps:
        actions.append(
            {
                "key": "staff_invite",
                "label": _("Create Staff Account"),
                "description": _("Add a new staff member and assign operational roles"),
                "url": reverse("staff:account_create"),
                "icon": "user-plus",
                "badge": None,
            }
        )
        actions.append(
            {
                "key": "staff_audit",
                "label": _("Staff Audit Trail"),
                "description": _("Inspect immutable audit events and access logs"),
                "url": reverse("staff:audit_logs"),
                "icon": "shield-check",
                "badge": None,
            }
        )

    return actions
