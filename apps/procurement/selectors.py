"""Phase 45: Procurement & Purchase Order selectors and reporting intelligence.

Read-only, optimized querysets for:
- Staff Purchasing Dashboard KPIs.
- Supplier directories with search, filter, and performance summaries.
- Purchase Order lists with overdue calculations and status filters.
- Supplier performance tracking based on actual verified delivery timestamps.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from apps.procurement.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderStatus,
    Supplier,
)

__all__ = [
    "get_procurement_dashboard_metrics",
    "get_purchase_orders_queryset",
    "get_supplier_performance",
    "get_suppliers_queryset",
]


def get_procurement_dashboard_metrics() -> dict[str, Any]:
    """Calculate real database statistics for the staff Purchasing Intelligence Dashboard."""
    today = timezone.localdate()

    # Active suppliers
    active_suppliers_count = Supplier.objects.filter(is_active=True).count()
    total_suppliers_count = Supplier.objects.count()

    # Purchase orders by status groups
    open_orders = PurchaseOrder.objects.exclude(
        status__in=[PurchaseOrderStatus.FULLY_RECEIVED, PurchaseOrderStatus.CANCELLED]
    ).select_related("supplier")

    draft_count = open_orders.filter(status=PurchaseOrderStatus.DRAFT).count()
    pending_approval_count = open_orders.filter(
        status=PurchaseOrderStatus.PENDING_APPROVAL
    ).count()
    approved_count = open_orders.filter(status=PurchaseOrderStatus.APPROVED).count()
    ordered_count = open_orders.filter(status=PurchaseOrderStatus.ORDERED).count()
    partially_received_count = open_orders.filter(
        status=PurchaseOrderStatus.PARTIALLY_RECEIVED
    ).count()

    # Overdue orders: expected delivery date is in the past and order not completed
    overdue_orders_qs = open_orders.filter(
        expected_delivery_date__isnull=False,
        expected_delivery_date__lt=today,
        status__in=[
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.ORDERED,
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
        ],
    )
    overdue_count = overdue_orders_qs.count()

    # Calculate outstanding units and committed spend on open orders
    open_orders_with_items = open_orders.prefetch_related("items")
    total_outstanding_units = 0
    total_committed_spend = Decimal("0.00")

    for order in open_orders_with_items:
        total_outstanding_units += order.total_outstanding_units
        total_committed_spend += order.total_cost

    # Recent deliveries (last 10 receipts)
    recent_receipts = (
        PurchaseOrderReceipt.objects.select_related(
            "purchase_order", "purchase_order__supplier", "received_by"
        )
        .prefetch_related("items", "items__line_item", "items__line_item__variant")
        .order_by("-received_at")[:10]
    )

    # Urgent orders needing attention (overdue or due in the next 3 days)
    urgent_date_limit = today + timezone.timedelta(days=3)
    urgent_orders = (
        open_orders.filter(
            expected_delivery_date__isnull=False,
            expected_delivery_date__lte=urgent_date_limit,
            status__in=[
                PurchaseOrderStatus.APPROVED,
                PurchaseOrderStatus.ORDERED,
                PurchaseOrderStatus.PARTIALLY_RECEIVED,
            ],
        )
        .order_by("expected_delivery_date")[:10]
    )

    return {
        "active_suppliers_count": active_suppliers_count,
        "total_suppliers_count": total_suppliers_count,
        "draft_count": draft_count,
        "pending_approval_count": pending_approval_count,
        "approved_count": approved_count,
        "ordered_count": ordered_count,
        "partially_received_count": partially_received_count,
        "overdue_count": overdue_count,
        "total_open_orders": open_orders.count(),
        "total_outstanding_units": total_outstanding_units,
        "total_committed_spend": total_committed_spend,
        "recent_receipts": recent_receipts,
        "urgent_orders": urgent_orders,
    }


def get_suppliers_queryset(
    *,
    q: str = "",
    is_active: bool | None = None,
) -> Any:
    """Retrieve filtered, annotated suppliers list."""
    qs = Supplier.objects.annotate(
        orders_total=Count("purchase_orders", distinct=True),
        open_orders_total=Count(
            "purchase_orders",
            filter=~Q(
                purchase_orders__status__in=[
                    PurchaseOrderStatus.FULLY_RECEIVED,
                    PurchaseOrderStatus.CANCELLED,
                ]
            ),
            distinct=True,
        ),
    ).order_by("name")

    if is_active is not None:
        qs = qs.filter(is_active=is_active)

    search = q.strip()
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(code__icontains=search)
            | Q(contact_name__icontains=search)
            | Q(email__icontains=search)
        )
    return qs


def get_purchase_orders_queryset(
    *,
    q: str = "",
    status: str = "",
    supplier_id: int | None = None,
    is_overdue: bool | None = None,
) -> Any:
    """Retrieve filtered purchase orders with prefetched relationships."""
    today = timezone.localdate()
    qs = (
        PurchaseOrder.objects.select_related("supplier", "created_by", "approved_by")
        .prefetch_related("items", "items__variant", "items__variant__product")
        .order_by("-created_at", "-pk")
    )

    if status and status in PurchaseOrderStatus.values:
        qs = qs.filter(status=status)

    if supplier_id:
        qs = qs.filter(supplier_id=supplier_id)

    if is_overdue is True:
        qs = qs.filter(
            expected_delivery_date__isnull=False,
            expected_delivery_date__lt=today,
            status__in=[
                PurchaseOrderStatus.APPROVED,
                PurchaseOrderStatus.ORDERED,
                PurchaseOrderStatus.PARTIALLY_RECEIVED,
            ],
        )
    elif is_overdue is False:
        qs = qs.exclude(
            expected_delivery_date__isnull=False,
            expected_delivery_date__lt=today,
            status__in=[
                PurchaseOrderStatus.APPROVED,
                PurchaseOrderStatus.ORDERED,
                PurchaseOrderStatus.PARTIALLY_RECEIVED,
            ],
        )

    search = q.strip()
    if search:
        qs = qs.filter(
            Q(reference__icontains=search)
            | Q(supplier__name__icontains=search)
            | Q(supplier__code__icontains=search)
            | Q(notes__icontains=search)
        )
    return qs


def get_supplier_performance(supplier: Supplier) -> dict[str, Any]:
    """Calculate grounded historical supplier performance indicators."""
    orders = (
        supplier.purchase_orders.prefetch_related("receipts", "items")
        .all()
    )

    total_orders = orders.count()
    completed_orders = [
        o for o in orders if o.status == PurchaseOrderStatus.FULLY_RECEIVED
    ]

    total_spend = sum((o.total_cost for o in orders if o.status != PurchaseOrderStatus.CANCELLED), Decimal("0.00"))
    total_ordered_units = sum((o.total_ordered_units for o in orders if o.status != PurchaseOrderStatus.CANCELLED), 0)
    total_received_units = sum((o.total_received_units for o in orders), 0)

    # On-time delivery evaluation:
    # A completed order is on-time if its latest receipt date <= expected_delivery_date
    eligible_orders_for_lead_time = [
        o for o in completed_orders if o.expected_delivery_date and o.receipts.exists()
    ]

    on_time_count = 0
    lead_time_days_list: list[int] = []

    for o in eligible_orders_for_lead_time:
        latest_receipt = o.receipts.order_by("-received_at").first()
        if latest_receipt:
            receipt_date = latest_receipt.received_at.date()
            if receipt_date <= o.expected_delivery_date:
                on_time_count += 1
            if o.sent_at:
                days_taken = (receipt_date - o.sent_at.date()).days
                if days_taken >= 0:
                    lead_time_days_list.append(days_taken)

    on_time_rate = (
        round((on_time_count / len(eligible_orders_for_lead_time)) * 100, 1)
        if eligible_orders_for_lead_time
        else None
    )

    avg_lead_time = (
        round(sum(lead_time_days_list) / len(lead_time_days_list), 1)
        if lead_time_days_list
        else (supplier.lead_time_days or None)
    )

    return {
        "total_orders": total_orders,
        "completed_orders_count": len(completed_orders),
        "total_spend": total_spend,
        "total_ordered_units": total_ordered_units,
        "total_received_units": total_received_units,
        "fulfillment_rate": (
            round((total_received_units / total_ordered_units) * 100, 1)
            if total_ordered_units > 0
            else None
        ),
        "on_time_rate": on_time_rate,
        "average_lead_time_days": avg_lead_time,
        "evaluated_deliveries_count": len(eligible_orders_for_lead_time),
    }
