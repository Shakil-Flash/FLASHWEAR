"""Phase 45: Procurement & Purchase Order selectors and reporting intelligence.

Read-only, optimized querysets for:
- Staff Purchasing Dashboard KPIs.
- Supplier directories with search, filter, and performance summaries.
- Purchase Order lists with overdue calculations and status filters.
- Supplier performance tracking based on actual verified delivery timestamps.
"""

import datetime
from decimal import Decimal
from typing import Any

from django.db.models import Avg, Count, F, Q, Sum
from django.utils import timezone

from apps.catalog.models import Product, ProductVariant
from apps.orders.models import Order, OrderItem, ReturnItem, ReturnRequest
from apps.payments.models import Payment, Refund
from apps.procurement.models import (
    CreditNoteStatus,
    ExpenseBudget,
    ExpenseCategory,
    ExpenseStatus,
    InvoiceMatchingStatus,
    OperatingExpense,
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
    SupplierVariant,
)

__all__ = [
    "get_accounts_payable_dashboard_metrics",
    "get_expense_budget_vs_actual",
    "get_operating_expenses_queryset",
    "get_procurement_dashboard_metrics",
    "get_product_profitability_metrics",
    "get_profitability_metrics",
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


# =============================================================================
# Phase 47: Profitability Intelligence & Operating Expense Selectors
# =============================================================================


def _resolve_date_range(
    preset: str = "this_month",
    start_date: Any = None,
    end_date: Any = None,
) -> tuple[datetime.date, datetime.date, str]:
    """Resolve report boundary dates from human presets or custom dates."""
    today = timezone.localdate()
    if preset == "last_month":
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - datetime.timedelta(days=1)
        start = last_month_end.replace(day=1)
        end = last_month_end
        label = "Last Month"
    elif preset == "this_quarter":
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        start = today.replace(month=quarter_month, day=1)
        end = today
        label = "This Quarter"
    elif preset == "ytd":
        start = today.replace(month=1, day=1)
        end = today
        label = "Year to Date"
    elif preset == "all_time":
        start = datetime.date(2020, 1, 1)
        end = today
        label = "All Time"
    elif preset == "custom" and start_date and end_date:
        if isinstance(start_date, str):
            start = datetime.date.fromisoformat(start_date)
        else:
            start = start_date
        if isinstance(end_date, str):
            end = datetime.date.fromisoformat(end_date)
        else:
            end = end_date
        label = f"{start} to {end}"
    else:
        start = today.replace(day=1)
        end = today
        label = "This Month"
    return start, end, label


def _resolve_variant_cost(variant: ProductVariant) -> Decimal:
    """Resolve authoritative unit cost for a variant using established hierarchy.

    1. Historical variant cost price (if explicitly set > 0).
    2. Average agreed purchase order unit cost for approved/received orders.
    3. Supplier variant catalog purchase cost.
    4. Zero fallback (flagged as estimated/incomplete).
    """
    if variant.cost_price is not None and variant.cost_price > Decimal("0.00"):
        return variant.cost_price

    po_avg = (
        PurchaseOrderItem.objects.filter(
            variant=variant,
            purchase_order__status__in=[
                PurchaseOrderStatus.APPROVED,
                PurchaseOrderStatus.ORDERED,
                PurchaseOrderStatus.PARTIALLY_RECEIVED,
                PurchaseOrderStatus.FULLY_RECEIVED,
            ],
        ).aggregate(Avg("unit_cost"))["unit_cost__avg"]
    )
    if po_avg is not None and po_avg > Decimal("0.00"):
        return Decimal(str(po_avg)).quantize(Decimal("0.01"))

    sup_cost = (
        SupplierVariant.objects.filter(variant=variant, is_active=True)
        .values_list("purchase_cost", flat=True)
        .first()
    )
    if sup_cost is not None and sup_cost > Decimal("0.00"):
        return Decimal(str(sup_cost)).quantize(Decimal("0.01"))

    return Decimal("0.00")


def get_profitability_metrics(
    start_date: Any = None,
    end_date: Any = None,
    period_preset: str = "this_month",
) -> dict[str, Any]:
    """Calculate executive profitability, revenue, COGS, and operating expense metrics."""
    start, end, period_label = _resolve_date_range(period_preset, start_date, end_date)

    # 1. Customer Orders & Gross Merchandise Sales (GMS)
    # Paid/fulfilled orders within reporting period
    paid_statuses = [
        Order.Status.PAID,
        Order.Status.PROCESSING,
        Order.Status.SHIPPED,
        Order.Status.DELIVERED,
    ]
    paid_orders = Order.objects.filter(
        status__in=paid_statuses,
        created_at__date__gte=start,
        created_at__date__lte=end,
    )

    gross_merchandise_sales = Decimal("0.00")
    total_discounts = Decimal("0.00")
    total_shipping_revenue = Decimal("0.00")
    total_taxes_collected = Decimal("0.00")

    order_ids = []
    for ord in paid_orders:
        order_ids.append(ord.pk)
        gross_merchandise_sales += ord.subtotal
        total_discounts += ord.discount_amount
        total_shipping_revenue += ord.shipping_amount
        total_taxes_collected += ord.tax_amount

    paid_orders_count = len(order_ids)

    # 2. Customer Refunds
    refunds_qs = Refund.objects.filter(
        status=Refund.Status.SUCCEEDED,
        created_at__date__gte=start,
        created_at__date__lte=end,
    )
    total_refunds = refunds_qs.aggregate(Sum("amount"))["amount__sum"] or Decimal("0.00")

    # 3. Net Sales
    net_sales = max(Decimal("0.00"), gross_merchandise_sales - total_discounts - total_refunds)

    # 4. Cost of Goods Sold (COGS)
    order_items = (
        OrderItem.objects.filter(order_id__in=order_ids)
        .select_related("variant", "variant__product")
    )

    total_cogs = Decimal("0.00")
    total_units_sold = 0
    total_units_returned = 0

    # Map returns in period
    returns_map: dict[int, int] = {}
    return_items = ReturnItem.objects.filter(
        request__status=ReturnRequest.Status.ACCEPTED,
        request__order_id__in=order_ids,
    ).values("variant_id").annotate(qty=Sum("quantity"))
    for r in return_items:
        returns_map[r["variant_id"]] = r["qty"] or 0

    for item in order_items:
        sold_qty = item.quantity
        returned_qty = returns_map.get(item.variant_id, 0)
        net_qty = max(0, sold_qty - returned_qty)

        total_units_sold += sold_qty
        total_units_returned += returned_qty

        unit_cost = _resolve_variant_cost(item.variant)
        line_cogs = Decimal(str(net_qty)) * unit_cost
        total_cogs += line_cogs

    total_cogs = total_cogs.quantize(Decimal("0.01"))
    net_units_sold = max(0, total_units_sold - total_units_returned)

    # 5. Gross Profit & Gross Margin
    gross_profit = net_sales - total_cogs
    gross_margin_percent = (
        (gross_profit / net_sales * Decimal("100.00")).quantize(Decimal("0.01"))
        if net_sales > Decimal("0.00")
        else Decimal("0.00")
    )

    # 6. Variable Selling Costs
    # Payment processing fee: standard 2.9% + $0.30 per successful customer transaction
    gateway_fees = (
        (net_sales * Decimal("0.029") + (Decimal(str(paid_orders_count)) * Decimal("0.30"))).quantize(Decimal("0.01"))
        if paid_orders_count > 0
        else Decimal("0.00")
    )
    packaging_costs = (
        OperatingExpense.objects.filter(
            category__code="PACKAGING",
            status__in=[ExpenseStatus.APPROVED, ExpenseStatus.PAID],
            expense_date__gte=start,
            expense_date__lte=end,
        ).aggregate(Sum("total_amount"))["total_amount__sum"]
        or Decimal("0.00")
    )
    delivery_costs = (
        OperatingExpense.objects.filter(
            category__code="DELIVERY",
            status__in=[ExpenseStatus.APPROVED, ExpenseStatus.PAID],
            expense_date__gte=start,
            expense_date__lte=end,
        ).aggregate(Sum("total_amount"))["total_amount__sum"]
        or Decimal("0.00")
    )
    variable_selling_costs = gateway_fees + packaging_costs + delivery_costs
    contribution_profit = gross_profit - variable_selling_costs
    contribution_margin_percent = (
        (contribution_profit / net_sales * Decimal("100.00")).quantize(Decimal("0.01"))
        if net_sales > Decimal("0.00")
        else Decimal("0.00")
    )

    # 7. Operating Expenses (OpEx)
    # Exclude any expense tied to an inventory PO supplier invoice to prevent double counting
    opex_qs = OperatingExpense.objects.filter(
        status__in=[ExpenseStatus.APPROVED, ExpenseStatus.PAID],
        expense_date__gte=start,
        expense_date__lte=end,
    ).exclude(supplier_invoice__isnull=False)

    total_opex = opex_qs.aggregate(Sum("total_amount"))["total_amount__sum"] or Decimal("0.00")

    # Categories breakdown
    categories_breakdown = []
    cat_aggregates = (
        opex_qs.values("category__id", "category__name", "category__code")
        .annotate(total=Sum("total_amount"), count=Count("id"))
        .order_by("-total")
    )
    for c in cat_aggregates:
        cat_total = c["total"] or Decimal("0.00")
        pct = (
            (cat_total / total_opex * Decimal("100.00")).quantize(Decimal("0.01"))
            if total_opex > Decimal("0.00")
            else Decimal("0.00")
        )
        categories_breakdown.append({
            "category_id": c["category__id"],
            "name": c["category__name"],
            "code": c["category__code"],
            "total": cat_total,
            "count": c["count"],
            "percentage": pct,
        })

    # 8. Estimated Operating Profit (EBIT)
    operating_profit = gross_profit - total_opex
    operating_margin_percent = (
        (operating_profit / net_sales * Decimal("100.00")).quantize(Decimal("0.01"))
        if net_sales > Decimal("0.00")
        else Decimal("0.00")
    )

    # 9. Cash Flow Overview
    cash_received = (
        Payment.objects.filter(
            status=Payment.Status.SUCCEEDED,
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).aggregate(Sum("amount"))["amount__sum"]
        or Decimal("0.00")
    )
    supplier_disbursements = (
        SupplierPayment.objects.filter(
            status=SupplierPaymentStatus.COMPLETED,
            payment_date__gte=start,
            payment_date__lte=end,
        ).aggregate(Sum("amount"))["amount__sum"]
        or Decimal("0.00")
    )
    opex_paid = (
        OperatingExpense.objects.filter(
            status=ExpenseStatus.PAID,
            payment_date__gte=start,
            payment_date__lte=end,
        ).aggregate(Sum("total_amount"))["total_amount__sum"]
        or Decimal("0.00")
    )
    net_operating_cash_flow = cash_received - supplier_disbursements - opex_paid

    # 10. Monthly Trends (last 6 months)
    monthly_trends = []
    today = timezone.localdate()
    for m in range(5, -1, -1):
        # Calculate start and end of that month
        # Target month offset
        year_offset = (today.month - 1 - m) // 12
        target_month = (today.month - 1 - m) % 12 + 1
        target_year = today.year + year_offset
        m_start = datetime.date(target_year, target_month, 1)
        if target_month == 12:
            m_end = datetime.date(target_year, 12, 31)
        else:
            m_end = datetime.date(target_year, target_month + 1, 1) - datetime.timedelta(days=1)

        m_sales = (
            Order.objects.filter(
                status__in=paid_statuses,
                created_at__date__gte=m_start,
                created_at__date__lte=m_end,
            ).aggregate(Sum("subtotal"))["subtotal__sum"]
            or Decimal("0.00")
        )
        m_disc = (
            Order.objects.filter(
                status__in=paid_statuses,
                created_at__date__gte=m_start,
                created_at__date__lte=m_end,
            ).aggregate(Sum("discount_amount"))["discount_amount__sum"]
            or Decimal("0.00")
        )
        m_ref = (
            Refund.objects.filter(
                status=Refund.Status.SUCCEEDED,
                created_at__date__gte=m_start,
                created_at__date__lte=m_end,
            ).aggregate(Sum("amount"))["amount__sum"]
            or Decimal("0.00")
        )
        m_net_sales = max(Decimal("0.00"), m_sales - m_disc - m_ref)

        m_opex = (
            OperatingExpense.objects.filter(
                status__in=[ExpenseStatus.APPROVED, ExpenseStatus.PAID],
                expense_date__gte=m_start,
                expense_date__lte=m_end,
            ).exclude(supplier_invoice__isnull=False).aggregate(Sum("total_amount"))["total_amount__sum"]
            or Decimal("0.00")
        )

        monthly_trends.append({
            "period": f"{target_year}-{target_month:02d}",
            "label": m_start.strftime("%b %Y"),
            "net_sales": m_net_sales,
            "opex": m_opex,
            "operating_profit": m_net_sales - m_opex,
        })

    # 11. Alerts & Operational Warnings
    unapproved_expenses_count = OperatingExpense.objects.filter(status=ExpenseStatus.SUBMITTED).count()
    unapproved_expenses_total = (
        OperatingExpense.objects.filter(status=ExpenseStatus.SUBMITTED).aggregate(Sum("total_amount"))["total_amount__sum"]
        or Decimal("0.00")
    )

    budget_vs_actual = get_expense_budget_vs_actual(year=start.year, month=start.month)
    budget_warnings = [b for b in budget_vs_actual["budgets"] if b["status"] in ("WARNING", "OVER_BUDGET")]

    return {
        "period_preset": period_preset,
        "period_label": period_label,
        "start_date": start,
        "end_date": end,
        # Sales metrics
        "gross_merchandise_sales": gross_merchandise_sales,
        "total_discounts": total_discounts,
        "total_refunds": total_refunds,
        "net_sales": net_sales,
        "shipping_revenue": total_shipping_revenue,
        "taxes_collected": total_taxes_collected,
        "paid_orders_count": paid_orders_count,
        # Units
        "units_sold": total_units_sold,
        "units_returned": total_units_returned,
        "net_units_sold": net_units_sold,
        # Cost & margins
        "total_cogs": total_cogs,
        "gross_profit": gross_profit,
        "gross_margin_percent": gross_margin_percent,
        "variable_selling_costs": variable_selling_costs,
        "contribution_profit": contribution_profit,
        "contribution_margin_percent": contribution_margin_percent,
        # Operating expenses
        "total_opex": total_opex,
        "categories_breakdown": categories_breakdown,
        "operating_profit": operating_profit,
        "operating_margin_percent": operating_margin_percent,
        # Cash flow
        "cash_received": cash_received,
        "supplier_disbursements": supplier_disbursements,
        "opex_paid": opex_paid,
        "net_operating_cash_flow": net_operating_cash_flow,
        # Trends & alerts
        "monthly_trends": monthly_trends,
        "unapproved_expenses_count": unapproved_expenses_count,
        "unapproved_expenses_total": unapproved_expenses_total,
        "budget_warnings": budget_warnings,
        "budget_overview": budget_vs_actual,
    }


def get_product_profitability_metrics(
    start_date: Any = None,
    end_date: Any = None,
    q: str = "",
    product_id: int | None = None,
) -> list[dict[str, Any]]:
    """Calculate line-level profitability for products and variants."""
    start, end, _ = _resolve_date_range("this_month" if not start_date else "custom", start_date, end_date)

    paid_statuses = [
        Order.Status.PAID,
        Order.Status.PROCESSING,
        Order.Status.SHIPPED,
        Order.Status.DELIVERED,
    ]

    items_qs = (
        OrderItem.objects.filter(
            order__status__in=paid_statuses,
            order__created_at__date__gte=start,
            order__created_at__date__lte=end,
        )
        .select_related("variant", "variant__product")
    )

    if q:
        items_qs = items_qs.filter(
            Q(product_name__icontains=q)
            | Q(sku__icontains=q)
            | Q(variant__product__name__icontains=q)
        )

    if product_id:
        items_qs = items_qs.filter(variant__product_id=product_id)

    # Group by variant
    variant_stats: dict[int, dict[str, Any]] = {}

    # Map returns in period
    order_ids = list(items_qs.values_list("order_id", flat=True).distinct())
    returns_map: dict[int, int] = {}
    return_items = ReturnItem.objects.filter(
        request__status=ReturnRequest.Status.ACCEPTED,
        request__order_id__in=order_ids,
    ).values("variant_id").annotate(qty=Sum("quantity"))
    for r in return_items:
        returns_map[r["variant_id"]] = r["qty"] or 0

    for item in items_qs:
        v_id = item.variant_id
        if v_id not in variant_stats:
            variant_stats[v_id] = {
                "variant": item.variant,
                "sku": item.sku,
                "product_name": item.product_name,
                "option_label": item.option_label,
                "units_sold": 0,
                "gross_revenue": Decimal("0.00"),
            }
        variant_stats[v_id]["units_sold"] += item.quantity
        variant_stats[v_id]["gross_revenue"] += item.line_total

    results = []
    for v_id, stat in variant_stats.items():
        v = stat["variant"]
        units_sold = stat["units_sold"]
        units_returned = returns_map.get(v_id, 0)
        net_units = max(0, units_sold - units_returned)
        gross_rev = stat["gross_revenue"]

        # Calculate unit cost
        unit_cost = _resolve_variant_cost(v)
        cogs = (Decimal(str(net_units)) * unit_cost).quantize(Decimal("0.01"))

        # Net sales (proportional to units returned)
        if units_sold > 0:
            net_sales = (gross_rev * (Decimal(str(net_units)) / Decimal(str(units_sold)))).quantize(Decimal("0.01"))
        else:
            net_sales = Decimal("0.00")

        gross_profit = net_sales - cogs
        margin_pct = (
            (gross_profit / net_sales * Decimal("100.00")).quantize(Decimal("0.01"))
            if net_sales > Decimal("0.00")
            else Decimal("0.00")
        )

        results.append({
            "variant_id": v_id,
            "sku": stat["sku"],
            "product_name": stat["product_name"],
            "option_label": stat["option_label"],
            "units_sold": units_sold,
            "units_returned": units_returned,
            "net_units": net_units,
            "gross_revenue": gross_rev,
            "net_sales": net_sales,
            "unit_cost": unit_cost,
            "cogs": cogs,
            "gross_profit": gross_profit,
            "margin_percent": margin_pct,
        })

    # Sort highest gross profit first
    results.sort(key=lambda x: x["gross_profit"], reverse=True)
    return results


def get_expense_budget_vs_actual(year: int | None = None, month: int | None = None) -> dict[str, Any]:
    """Calculate monthly budget consumption, remaining variance, and warning alerts."""
    today = timezone.localdate()
    y = year or today.year
    m = month or today.month

    categories = ExpenseCategory.objects.filter(is_active=True).order_by("name")
    budgets_qs = ExpenseBudget.objects.filter(year=y, month=m).select_related("category")
    budgets_by_cat = {b.category_id: b for b in budgets_qs}

    # Actual spend in that month
    month_start = datetime.date(y, m, 1)
    if m == 12:
        month_end = datetime.date(y, 12, 31)
    else:
        month_end = datetime.date(y, m + 1, 1) - datetime.timedelta(days=1)

    actuals_qs = (
        OperatingExpense.objects.filter(
            status__in=[ExpenseStatus.APPROVED, ExpenseStatus.PAID],
            expense_date__gte=month_start,
            expense_date__lte=month_end,
        )
        .values("category_id")
        .annotate(total=Sum("total_amount"))
    )
    actuals_by_cat = {a["category_id"]: a["total"] or Decimal("0.00") for a in actuals_qs}

    budget_items = []
    total_budget = Decimal("0.00")
    total_actual = Decimal("0.00")

    for cat in categories:
        b = budgets_by_cat.get(cat.pk)
        b_amount = b.budget_amount if b else Decimal("0.00")
        threshold = b.warning_threshold_percent if b else 80
        actual = actuals_by_cat.get(cat.pk, Decimal("0.00"))

        total_budget += b_amount
        total_actual += actual

        remaining = b_amount - actual
        if b_amount > Decimal("0.00"):
            consumed_pct = (actual / b_amount * Decimal("100.00")).quantize(Decimal("0.1"))
        else:
            consumed_pct = Decimal("100.0") if actual > Decimal("0.00") else Decimal("0.0")

        if b_amount > Decimal("0.00"):
            if consumed_pct > Decimal("100.0"):
                status = "OVER_BUDGET"
            elif consumed_pct >= Decimal(str(threshold)):
                status = "WARNING"
            else:
                status = "OK"
        else:
            status = "UNBUDGETED" if actual > Decimal("0.00") else "OK"

        budget_items.append({
            "category": cat,
            "budget": b,
            "budget_amount": b_amount,
            "actual_spend": actual,
            "remaining_budget": remaining,
            "consumed_percent": consumed_pct,
            "warning_threshold": threshold,
            "status": status,
        })

    total_remaining = total_budget - total_actual
    overall_consumed_pct = (
        (total_actual / total_budget * Decimal("100.00")).quantize(Decimal("0.1"))
        if total_budget > Decimal("0.00")
        else Decimal("0.0")
    )

    return {
        "year": y,
        "month": m,
        "period": f"{y:04d}-{m:02d}",
        "budgets": budget_items,
        "total_budget": total_budget,
        "total_actual": total_actual,
        "total_remaining": total_remaining,
        "overall_consumed_percent": overall_consumed_pct,
    }


def get_operating_expenses_queryset(
    q: str = "",
    category_id: int | None = None,
    status: str | None = None,
    start_date: Any = None,
    end_date: Any = None,
):
    """Return filterable queryset for operating expenses directory."""
    qs = OperatingExpense.objects.select_related("category", "supplier", "created_by", "approved_by")

    if q:
        qs = qs.filter(
            Q(expense_number__icontains=q)
            | Q(title__icontains=q)
            | Q(payee_name__icontains=q)
            | Q(description__icontains=q)
            | Q(supplier__name__icontains=q)
            | Q(payment_reference__icontains=q)
        )

    if category_id:
        qs = qs.filter(category_id=category_id)

    if status:
        qs = qs.filter(status=status)

    if start_date:
        qs = qs.filter(expense_date__gte=start_date)

    if end_date:
        qs = qs.filter(expense_date__lte=end_date)

    return qs

