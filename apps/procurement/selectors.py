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
    CreditNoteStatus,
    InvoiceMatchingStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderStatus,
    Supplier,
    SupplierCreditNote,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
    SupplierPayment,
    SupplierPaymentStatus,
)

__all__ = [
    "get_accounts_payable_dashboard_metrics",
    "get_procurement_dashboard_metrics",
    "get_purchase_orders_queryset",
    "get_supplier_ap_summary",
    "get_supplier_credit_notes_queryset",
    "get_supplier_invoices_queryset",
    "get_supplier_payments_queryset",
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


# =============================================================================
# Phase 46: Accounts Payable & Supplier Invoices Intelligence Selectors
# =============================================================================


def get_accounts_payable_dashboard_metrics(
    as_of_date: Any = None,
    supplier_id: int | None = None,
) -> dict[str, Any]:
    """Calculate grounded financial aggregates for the Accounts Payable Intelligence Dashboard."""
    today = as_of_date or timezone.localdate()
    soon_cutoff = today + timezone.timedelta(days=14)

    base_invoices = SupplierInvoice.objects.exclude(status=SupplierInvoiceStatus.VOIDED)
    if supplier_id:
        base_invoices = base_invoices.filter(supplier_id=supplier_id)

    # Approved liabilities (approved and partially paid invoices)
    approved_invoices = base_invoices.filter(
        status__in=[SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID]
    ).select_related("supplier")

    total_approved_liability = Decimal("0.00")
    total_due_soon = Decimal("0.00")
    total_overdue = Decimal("0.00")
    overdue_invoices_count = 0

    # Aging report buckets
    aging_buckets = {
        "current": {"label": "Current / Not Due", "count": 0, "amount": Decimal("0.00")},
        "days_1_30": {"label": "1–30 Days Overdue", "count": 0, "amount": Decimal("0.00")},
        "days_31_60": {"label": "31–60 Days Overdue", "count": 0, "amount": Decimal("0.00")},
        "days_61_90": {"label": "61–90 Days Overdue", "count": 0, "amount": Decimal("0.00")},
        "over_90": {"label": "> 90 Days Overdue", "count": 0, "amount": Decimal("0.00")},
    }

    upcoming_deadlines: list[SupplierInvoice] = []

    for inv in approved_invoices:
        balance = inv.outstanding_balance
        if balance <= Decimal("0.00"):
            continue

        total_approved_liability += balance

        if inv.due_date < today:
            total_overdue += balance
            overdue_invoices_count += 1
            days = (today - inv.due_date).days
            if 1 <= days <= 30:
                aging_buckets["days_1_30"]["count"] += 1
                aging_buckets["days_1_30"]["amount"] += balance
            elif 31 <= days <= 60:
                aging_buckets["days_31_60"]["count"] += 1
                aging_buckets["days_31_60"]["amount"] += balance
            elif 61 <= days <= 90:
                aging_buckets["days_61_90"]["count"] += 1
                aging_buckets["days_61_90"]["amount"] += balance
            else:
                aging_buckets["over_90"]["count"] += 1
                aging_buckets["over_90"]["amount"] += balance
        else:
            aging_buckets["current"]["count"] += 1
            aging_buckets["current"]["amount"] += balance

            if inv.due_date <= soon_cutoff:
                total_due_soon += balance

        upcoming_deadlines.append(inv)

    upcoming_deadlines.sort(key=lambda x: x.due_date)

    # Operational status counts
    pending_approval_count = base_invoices.filter(status=SupplierInvoiceStatus.SUBMITTED).count()
    disputed_count = base_invoices.filter(status=SupplierInvoiceStatus.DISPUTED).count()
    partially_paid_count = base_invoices.filter(status=SupplierInvoiceStatus.PARTIALLY_PAID).count()
    unmatched_count = (
        base_invoices.filter(match_record__isnull=False)
        .exclude(match_record__status=InvoiceMatchingStatus.MATCHED)
        .count()
    )

    # Recent supplier disbursements
    recent_payments = (
        SupplierPayment.objects.filter(status=SupplierPaymentStatus.COMPLETED)
        .select_related("supplier", "recorded_by")
        .order_by("-payment_date", "-created_at")[:8]
    )

    # Supplier level outstanding balances
    suppliers_qs = Supplier.objects.filter(is_active=True).prefetch_related("invoices")
    supplier_balances: list[dict[str, Any]] = []
    for s in suppliers_qs:
        s_out = Decimal("0.00")
        s_over = Decimal("0.00")
        for inv in s.invoices.all():
            if inv.status in (SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID):
                bal = inv.outstanding_balance
                if bal > Decimal("0.00"):
                    s_out += bal
                    if inv.due_date < today:
                        s_over += bal
        if s_out > Decimal("0.00"):
            supplier_balances.append({
                "supplier": s,
                "outstanding_liability": s_out,
                "overdue_liability": s_over,
                "currency": s.currency,
            })
    supplier_balances.sort(key=lambda x: x["outstanding_liability"], reverse=True)

    return {
        "as_of_date": today,
        "total_approved_liability": total_approved_liability,
        "total_outstanding_liability": total_approved_liability,
        "total_due_soon": total_due_soon,
        "amount_due_soon": total_due_soon,
        "total_overdue": total_overdue,
        "overdue_amount": total_overdue,
        "overdue_invoices_count": overdue_invoices_count,
        "pending_approval_count": pending_approval_count,
        "awaiting_approval_count": pending_approval_count,
        "disputed_count": disputed_count,
        "partially_paid_count": partially_paid_count,
        "unmatched_count": unmatched_count,
        "aging_buckets": aging_buckets,
        "aging_schedule": aging_buckets,
        "recent_payments": recent_payments,
        "supplier_balances": supplier_balances[:10],
        "upcoming_deadlines": upcoming_deadlines[:10],
    }


def get_supplier_invoices_queryset(
    q: str = "",
    supplier_id: int | None = None,
    status: str | None = None,
    overdue_only: bool = False,
    po_id: int | None = None,
    date_from: Any = None,
    date_to: Any = None,
) -> Any:
    """Filter supplier invoices with eager loading for back office lists."""
    qs = (
        SupplierInvoice.objects.select_related("supplier", "purchase_order", "match_record", "created_by", "approved_by")
        .prefetch_related("lines")
        .order_by("-invoice_date", "-pk")
    )

    if q:
        query = q.strip()
        qs = qs.filter(
            Q(invoice_number__icontains=query)
            | Q(normalized_invoice_number__icontains=query)
            | Q(supplier_reference__icontains=query)
            | Q(supplier__name__icontains=query)
            | Q(purchase_order__reference__icontains=query)
        )

    if supplier_id:
        qs = qs.filter(supplier_id=supplier_id)

    if status:
        qs = qs.filter(status=status)

    if po_id:
        qs = qs.filter(purchase_order_id=po_id)

    if date_from:
        qs = qs.filter(invoice_date__gte=date_from)

    if date_to:
        qs = qs.filter(invoice_date__lte=date_to)

    if overdue_only:
        today = timezone.localdate()
        qs = qs.filter(
            due_date__lt=today,
            status__in=[SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID],
        ).filter(total_amount__gt=F("amount_paid") + F("amount_credited"))

    return qs


def get_supplier_payments_queryset(
    q: str = "",
    supplier_id: int | None = None,
    status: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
) -> Any:
    """Filter supplier payments for back office lists."""
    qs = (
        SupplierPayment.objects.select_related("supplier", "recorded_by", "reversed_by")
        .prefetch_related("allocations__invoice")
        .order_by("-payment_date", "-created_at")
    )

    if q:
        query = q.strip()
        qs = qs.filter(
            Q(payment_reference__icontains=query)
            | Q(bank_transaction_reference__icontains=query)
            | Q(supplier__name__icontains=query)
        )

    if supplier_id:
        qs = qs.filter(supplier_id=supplier_id)

    if status:
        qs = qs.filter(status=status)

    if date_from:
        qs = qs.filter(payment_date__gte=date_from)

    if date_to:
        qs = qs.filter(payment_date__lte=date_to)

    return qs


def get_supplier_credit_notes_queryset(
    q: str = "",
    supplier_id: int | None = None,
    status: str | None = None,
) -> Any:
    """Filter supplier credit notes."""
    qs = (
        SupplierCreditNote.objects.select_related("supplier", "invoice", "created_by")
        .prefetch_related("applications__invoice")
        .order_by("-credit_date", "-pk")
    )

    if q:
        query = q.strip()
        qs = qs.filter(
            Q(credit_note_number__icontains=query)
            | Q(reason__icontains=query)
            | Q(supplier__name__icontains=query)
        )

    if supplier_id:
        qs = qs.filter(supplier_id=supplier_id)

    if status:
        qs = qs.filter(status=status)

    return qs


def get_supplier_ap_summary(supplier: Supplier, as_of_date: Any = None) -> dict[str, Any]:
    """Calculate accounts payable summary for a specific supplier profile."""
    today = as_of_date or timezone.localdate()
    invoices = supplier.invoices.exclude(status=SupplierInvoiceStatus.VOIDED)

    total_invoiced = Decimal("0.00")
    total_paid = Decimal("0.00")
    total_credited = Decimal("0.00")
    outstanding_liability = Decimal("0.00")
    overdue_liability = Decimal("0.00")

    for inv in invoices:
        total_invoiced += inv.total_amount
        total_paid += inv.amount_paid
        total_credited += inv.amount_credited

        if inv.status in (SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID):
            bal = inv.outstanding_balance
            if bal > Decimal("0.00"):
                outstanding_liability += bal
                if inv.due_date < today:
                    overdue_liability += bal

    unapplied_credits = sum((cn.unapplied_amount for cn in supplier.credit_notes.filter(status=CreditNoteStatus.APPROVED)), Decimal("0.00"))

    return {
        "supplier": supplier,
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "total_credited": total_credited,
        "outstanding_liability": outstanding_liability,
        "total_outstanding": outstanding_liability,
        "overdue_liability": overdue_liability,
        "overdue_amount": overdue_liability,
        "unapplied_credits": unapplied_credits,
        "net_liability": max(Decimal("0.00"), outstanding_liability - unapplied_credits),
    }

