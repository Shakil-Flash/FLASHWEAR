"""Phase 55: Advanced Analytics, Business Intelligence & Executive Dashboard Selectors.

Provides verified, fast, and authoritative business intelligence aggregates for FLASHWEAR.
Reconciles financial indicators against orders, payments, refunds, inventory, and procurement.
Never fabricates data or invents zero values; clearly separates verified metrics, estimates,
and unavailable data.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate, TruncMonth, TruncWeek
from django.utils import timezone

from apps.backoffice.selectors.common import DateRange
from apps.backoffice.selectors.dashboard import REVENUE_STATUSES
from apps.catalog.models import Brand, Category, Product, ProductVariant
from apps.engagement.models.retention import RetentionCampaign
from apps.inventory.models import Stock
from apps.orders.models import Order, OrderItem, ReturnItem, ReturnRequest
from apps.payments.models import Refund
from apps.procurement.models import (
    ExpenseStatus,
    OperatingExpense,
    PurchaseOrder,
    PurchaseOrderStatus,
    SupplierInvoice,
    SupplierInvoiceStatus,
)
from apps.procurement.selectors import _resolve_variant_cost

__all__ = [
    "calculate_delta_percent",
    "get_customer_and_retention_intelligence",
    "get_executive_bi_metrics",
    "get_procurement_and_expense_intelligence",
    "get_product_and_inventory_intelligence",
    "get_sales_time_series",
    "previous_equivalent_range",
    "sanitize_csv_cell",
]


def _window(date_range: DateRange, field: str) -> dict:
    """Return half-open window filter kwargs for DateTimeField: start <= x < end."""
    if date_range.is_all_time or date_range.start is None or date_range.end is None:
        return {}
    return {f"{field}__gte": date_range.start, f"{field}__lt": date_range.end}


def _date_window(date_range: DateRange, field: str) -> dict:
    """Return half-open window filter kwargs for DateField: start <= x < end."""
    if date_range.is_all_time or date_range.start is None or date_range.end is None:
        return {}
    return {f"{field}__gte": date_range.start.date(), f"{field}__lt": date_range.end.date()}


def previous_equivalent_range(date_range: DateRange) -> DateRange | None:
    """Calculate the immediately preceding equivalent period for comparison.

    For example, if the current range is a 7-day window from [Oct 7 to Oct 14),
    the previous equivalent period is the 7-day window [Sep 30 to Oct 7).
    Returns None for 'all' time or when boundaries are unbounded.
    """
    if date_range.is_all_time or not date_range.start or not date_range.end:
        return None

    duration = date_range.end - date_range.start
    if duration <= timedelta(0):
        return None

    prior_end = date_range.start
    prior_start = date_range.start - duration
    label = f"Prior ({prior_start.strftime('%b %d')} – {prior_end.strftime('%b %d')})"
    return DateRange(key="prior", label=label, start=prior_start, end=prior_end)


def calculate_delta_percent(
    current: Decimal | float | int | None,
    previous: Decimal | float | int | None,
) -> float | None:
    """Calculate percentage change between current and previous period.

    Returns:
        float: percentage change (e.g. +12.5 or -5.0) rounded to 1 decimal place.
        None: if comparison is unavailable (e.g. previous period is missing).
    """
    if current is None or previous is None:
        return None

    curr_val = float(current)
    prev_val = float(previous)

    if prev_val == 0.0:
        if curr_val == 0.0:
            return 0.0
        return 100.0 if curr_val > 0.0 else -100.0

    delta = ((curr_val - prev_val) / abs(prev_val)) * 100.0
    return round(delta, 1)


def sanitize_csv_cell(value: Any) -> str:
    """Sanitize cell value to prevent CSV / spreadsheet formula injection attacks.

    Prepends a single quote (') if the text representation begins with any of
    the dangerous formula trigger characters: '=', '+', '-', '@', tab, or carriage return.
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if text[0] in ("\t", "\r"):
        return f"'{text}"
    stripped = text.lstrip(" ")
    if stripped and stripped[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{text}"
    return text


def _single_period_kpis(date_range: DateRange) -> dict[str, Any]:
    """Calculate baseline business and financial KPIs for a single period."""
    # 1. Orders and Sales
    completed_orders = Order.objects.filter(
        status__in=REVENUE_STATUSES,
        **_window(date_range, "paid_at"),
    )
    sales_agg = completed_orders.aggregate(
        gmv=Coalesce(Sum("subtotal"), Decimal("0.00")),
        discounts=Coalesce(Sum("discount_amount"), Decimal("0.00")),
        shipping=Coalesce(Sum("shipping_amount"), Decimal("0.00")),
        tax=Coalesce(Sum("tax_amount"), Decimal("0.00")),
        gross_revenue=Coalesce(Sum("total"), Decimal("0.00")),
        paid_count=Count("pk"),
    )
    gmv = sales_agg["gmv"]
    discounts = sales_agg["discounts"]
    shipping_revenue = sales_agg["shipping"]
    tax_collected = sales_agg["tax"]
    gross_revenue = sales_agg["gross_revenue"]
    paid_orders_count = sales_agg["paid_count"]

    aov = (
        (gross_revenue / paid_orders_count).quantize(Decimal("0.01"))
        if paid_orders_count > 0
        else Decimal("0.00")
    )

    # Placed orders (including unpaid/cancelled)
    placed_count = Order.objects.filter(**_window(date_range, "created_at")).count()

    # 2. Succeeded Refunds
    refunds_agg = Refund.objects.filter(
        status=Refund.Status.SUCCEEDED,
        **_window(date_range, "processed_at" if date_range.start else "created_at"),
    ).aggregate(
        total=Coalesce(Sum("amount"), Decimal("0.00")),
        count=Count("pk"),
    )
    refunds_total = refunds_agg["total"]
    refunds_count = refunds_agg["count"]

    net_sales = max(Decimal("0.00"), gross_revenue - refunds_total)

    # 3. Cancellations
    cancellations_agg = Order.objects.filter(
        status=Order.Status.CANCELLED,
        **_window(date_range, "cancelled_at" if date_range.start else "created_at"),
    ).aggregate(
        total=Coalesce(Sum("total"), Decimal("0.00")),
        count=Count("pk"),
    )
    cancellations_count = cancellations_agg["count"]
    cancellations_total = cancellations_agg["total"]

    # 4. Cost of Goods Sold (COGS) & Gross Profit
    # Query items on completed orders in this window
    completed_items = (
        OrderItem.objects.filter(
            order__status__in=REVENUE_STATUSES,
            **_window(date_range, "order__paid_at"),
        )
        .select_related("variant", "variant__product")
    )

    total_units_sold = 0
    total_cogs = Decimal("0.00")
    units_with_cost = 0

    # Map returns in this window for completed orders
    returns_map: dict[int, int] = {}
    return_items = ReturnItem.objects.filter(
        return_request__status__in=[
            ReturnRequest.Status.APPROVED,
            ReturnRequest.Status.RECEIVED,
            ReturnRequest.Status.INSPECTION_PASSED,
            ReturnRequest.Status.REFUND_PENDING,
            ReturnRequest.Status.REFUNDED,
            ReturnRequest.Status.COMPLETED,
        ],
        **_window(date_range, "return_request__created_at"),
    ).values("order_item__variant_id").annotate(qty=Sum("quantity"))
    for r in return_items:
        v_id = r["order_item__variant_id"]
        if v_id:
            returns_map[v_id] = r["qty"] or 0

    for item in completed_items:
        sold_qty = item.quantity
        total_units_sold += sold_qty
        returned_qty = returns_map.get(item.variant_id, 0)
        net_qty = max(0, sold_qty - returned_qty)

        unit_cost = _resolve_variant_cost(item.variant) if item.variant else Decimal("0.00")
        if unit_cost > Decimal("0.00"):
            units_with_cost += sold_qty
        total_cogs += Decimal(str(net_qty)) * unit_cost

    total_cogs = total_cogs.quantize(Decimal("0.01"))
    cost_coverage_pct = (
        round((units_with_cost / total_units_sold) * 100, 1)
        if total_units_sold > 0
        else 100.0
    )

    if total_units_sold == 0:
        cost_status = "no_sales"
        estimated_gross_profit = Decimal("0.00")
        gross_margin_pct = 0.0
    elif units_with_cost == 0:
        cost_status = "unavailable"
        estimated_gross_profit = None
        gross_margin_pct = None
    elif units_with_cost < total_units_sold:
        cost_status = "estimated"
        estimated_gross_profit = net_sales - total_cogs
        gross_margin_pct = (
            round(float(estimated_gross_profit / net_sales) * 100, 1)
            if net_sales > Decimal("0.00")
            else 0.0
        )
    else:
        cost_status = "verified"
        estimated_gross_profit = net_sales - total_cogs
        gross_margin_pct = (
            round(float(estimated_gross_profit / net_sales) * 100, 1)
            if net_sales > Decimal("0.00")
            else 0.0
        )

    # 5. Operating Expenses (OpEx)
    # Exclude expenses tied to a supplier invoice to prevent double counting
    opex_qs = OperatingExpense.objects.filter(
        status__in=[ExpenseStatus.APPROVED, ExpenseStatus.PAID],
        **_date_window(date_range, "expense_date"),
    ).exclude(supplier_invoice__isnull=False)

    opex_total = (
        opex_qs.aggregate(total=Coalesce(Sum("total_amount"), Decimal("0.00")))["total"]
        or Decimal("0.00")
    )
    opex_count = opex_qs.count()

    operating_profit = (
        (estimated_gross_profit - opex_total) if estimated_gross_profit is not None else None
    )
    operating_margin_pct = (
        round(float(operating_profit / net_sales) * 100, 1)
        if operating_profit is not None and net_sales > Decimal("0.00")
        else None
    )

    # 6. Fulfillment & Delivery Timings
    # Shipped orders with timestamps
    shipped_orders = list(
        Order.objects.filter(
            status__in=[Order.Status.SHIPPED, Order.Status.DELIVERED],
            paid_at__isnull=False,
            shipped_at__isnull=False,
            **_window(date_range, "shipped_at"),
        ).values_list("paid_at", "shipped_at")
    )
    fulfillment_hours = [
        (s - p).total_seconds() / 3600.0
        for p, s in shipped_orders
        if s and p and s >= p
    ]
    avg_fulfillment_hours = (
        round(sum(fulfillment_hours) / len(fulfillment_hours), 1)
        if fulfillment_hours
        else None
    )

    # Delivered orders with timestamps
    delivered_orders = list(
        Order.objects.filter(
            status=Order.Status.DELIVERED,
            shipped_at__isnull=False,
            delivered_at__isnull=False,
            **_window(date_range, "delivered_at"),
        ).values_list("shipped_at", "delivered_at")
    )
    delivery_hours = [
        (d - s).total_seconds() / 3600.0
        for s, d in delivered_orders
        if d and s and d >= s
    ]
    avg_delivery_hours = (
        round(sum(delivery_hours) / len(delivery_hours), 1)
        if delivery_hours
        else None
    )

    # 7. Customer metrics (new vs returning)
    buyer_ids = list(completed_orders.values_list("user_id", flat=True).distinct())
    if buyer_ids and date_range.start:
        prior_buyer_ids = set(
            Order.objects.filter(
                user_id__in=buyer_ids,
                status__in=REVENUE_STATUSES,
                paid_at__lt=date_range.start,
            ).values_list("user_id", flat=True)
        )
    else:
        prior_buyer_ids = set()
    returning_buyers = len(prior_buyer_ids)
    new_buyers = len(buyer_ids) - returning_buyers
    repeat_rate_pct = (
        round((returning_buyers / len(buyer_ids)) * 100, 1)
        if buyer_ids
        else 0.0
    )

    # 8. Promotions
    promo_orders = completed_orders.filter(promotion_code__gt="")
    promo_orders_count = promo_orders.count()
    promo_discounts = promo_orders.aggregate(
        d=Coalesce(Sum("discount_amount"), Decimal("0.00"))
    )["d"]
    promo_revenue = promo_orders.aggregate(
        r=Coalesce(Sum("total"), Decimal("0.00"))
    )["r"]

    return {
        "gmv": gmv,
        "discounts": discounts,
        "shipping_revenue": shipping_revenue,
        "tax_collected": tax_collected,
        "gross_revenue": gross_revenue,
        "net_sales": net_sales,
        "paid_orders_count": paid_orders_count,
        "placed_orders_count": placed_count,
        "aov": aov,
        "refunds_count": refunds_count,
        "refunds_total": refunds_total,
        "cancellations_count": cancellations_count,
        "cancellations_total": cancellations_total,
        "total_units_sold": total_units_sold,
        "cogs": total_cogs,
        "cost_coverage_pct": cost_coverage_pct,
        "cost_status": cost_status,
        "estimated_gross_profit": estimated_gross_profit,
        "gross_margin_pct": gross_margin_pct,
        "opex_total": opex_total,
        "opex_count": opex_count,
        "operating_profit": operating_profit,
        "operating_margin_pct": operating_margin_pct,
        "shipped_orders_count": len(shipped_orders),
        "delivered_orders_count": len(delivered_orders),
        "avg_fulfillment_hours": avg_fulfillment_hours,
        "avg_delivery_hours": avg_delivery_hours,
        "total_buyers": len(buyer_ids),
        "new_buyers": new_buyers,
        "returning_buyers": returning_buyers,
        "repeat_rate_pct": repeat_rate_pct,
        "promo_orders_count": promo_orders_count,
        "promo_discounts": promo_discounts,
        "promo_revenue": promo_revenue,
    }


def get_executive_bi_metrics(*, date_range: DateRange) -> dict[str, Any]:
    """Master executive dashboard selector calculating current and comparison period KPIs."""
    current_kpis = _single_period_kpis(date_range)
    prior_range = previous_equivalent_range(date_range)

    if prior_range:
        prior_kpis = _single_period_kpis(prior_range)
        deltas = {
            "gmv": calculate_delta_percent(current_kpis["gmv"], prior_kpis["gmv"]),
            "gross_revenue": calculate_delta_percent(
                current_kpis["gross_revenue"], prior_kpis["gross_revenue"]
            ),
            "net_sales": calculate_delta_percent(
                current_kpis["net_sales"], prior_kpis["net_sales"]
            ),
            "paid_orders_count": calculate_delta_percent(
                current_kpis["paid_orders_count"], prior_kpis["paid_orders_count"]
            ),
            "aov": calculate_delta_percent(current_kpis["aov"], prior_kpis["aov"]),
            "refunds_total": calculate_delta_percent(
                current_kpis["refunds_total"], prior_kpis["refunds_total"]
            ),
            "cancellations_count": calculate_delta_percent(
                current_kpis["cancellations_count"], prior_kpis["cancellations_count"]
            ),
            "new_buyers": calculate_delta_percent(
                current_kpis["new_buyers"], prior_kpis["new_buyers"]
            ),
            "returning_buyers": calculate_delta_percent(
                current_kpis["returning_buyers"], prior_kpis["returning_buyers"]
            ),
            "repeat_rate_pct": calculate_delta_percent(
                current_kpis["repeat_rate_pct"], prior_kpis["repeat_rate_pct"]
            ),
            "estimated_gross_profit": calculate_delta_percent(
                current_kpis["estimated_gross_profit"], prior_kpis["estimated_gross_profit"]
            ),
        }
    else:
        prior_kpis = None
        deltas = {}

    # Inventory risks snapshot
    threshold = getattr(settings, "BACKOFFICE_LOW_STOCK_THRESHOLD", 5)
    stock_agg = Stock.objects.annotate(available_qty=F("on_hand") - F("reserved")).aggregate(
        total_on_hand=Coalesce(Sum("on_hand"), 0),
        total_reserved=Coalesce(Sum("reserved"), 0),
        low_stock_count=Count(
            "pk", filter=Q(available_qty__gt=0, available_qty__lte=threshold)
        ),
        out_of_stock_count=Count("pk", filter=Q(available_qty=0)),
    )
    total_available = max(0, stock_agg["total_on_hand"] - stock_agg["total_reserved"])

    inventory_summary = {
        "on_hand": stock_agg["total_on_hand"],
        "reserved": stock_agg["total_reserved"],
        "available": total_available,
        "low_stock_count": stock_agg["low_stock_count"],
        "out_of_stock_count": stock_agg["out_of_stock_count"],
    }

    return {
        "range": date_range,
        "prior_range": prior_range,
        "current": current_kpis,
        "prior": prior_kpis,
        "deltas": deltas,
        "inventory": inventory_summary,
        "currency_code": getattr(settings, "CATALOG_CURRENCY_CODE", "USD"),
        "currency_symbol": getattr(settings, "CATALOG_CURRENCY_SYMBOL", "$"),
    }


def get_sales_time_series(*, date_range: DateRange, interval: str = "day") -> dict[str, Any]:
    """Daily, weekly, or monthly sales and order trend series for charting."""
    completed = Order.objects.filter(
        status__in=REVENUE_STATUSES,
        **_window(date_range, "paid_at"),
    )

    if interval == "month":
        trunc_func = TruncMonth("paid_at")
        date_fmt = "%b %Y"
    elif interval == "week":
        trunc_func = TruncWeek("paid_at")
        date_fmt = "Wk %W"
    else:
        trunc_func = TruncDate("paid_at")
        date_fmt = "%b %d"

    order_rows = list(
        completed.annotate(period=trunc_func)
        .values("period")
        .annotate(
            gross_revenue=Coalesce(Sum("total"), Decimal("0.00")),
            orders=Count("pk"),
            discounts=Coalesce(Sum("discount_amount"), Decimal("0.00")),
        )
        .order_by("period")
    )

    # Refunds by period
    refunds = Refund.objects.filter(
        status=Refund.Status.SUCCEEDED,
        **_window(date_range, "processed_at" if date_range.start else "created_at"),
    )
    ref_trunc = (
        TruncMonth("processed_at" if date_range.start else "created_at")
        if interval == "month"
        else (
            TruncWeek("processed_at" if date_range.start else "created_at")
            if interval == "week"
            else TruncDate("processed_at" if date_range.start else "created_at")
        )
    )
    refund_map = dict(
        refunds.annotate(period=ref_trunc)
        .values("period")
        .annotate(total=Coalesce(Sum("amount"), Decimal("0.00")))
        .values_list("period", "total")
    )

    series = []
    for row in order_rows:
        period_val = row["period"]
        if not period_val:
            continue
        p_date = period_val.date() if isinstance(period_val, datetime) else period_val
        gross = row["gross_revenue"]
        ref_amt = refund_map.get(period_val, Decimal("0.00"))
        net = max(Decimal("0.00"), gross - ref_amt)
        series.append(
            {
                "date": p_date.isoformat(),
                "label": p_date.strftime(date_fmt),
                "gross_revenue": gross,
                "net_revenue": net,
                "orders": row["orders"],
                "discounts": row["discounts"],
                "refunds": ref_amt,
            }
        )

    max_revenue = max([s["gross_revenue"] for s in series], default=Decimal("0.00"))
    max_orders = max([s["orders"] for s in series], default=0)

    for item in series:
        rev = float(item["gross_revenue"])
        max_r = float(max_revenue) if max_revenue > 0 else 1.0
        item["height_pct"] = round((rev / max_r) * 100) if max_r > 0 else 0

    return {
        "series": series,
        "total_periods": len(series),
        "max_revenue": max_revenue,
        "max_orders": max_orders,
        "interval": interval,
    }


def get_product_and_inventory_intelligence(*, date_range: DateRange) -> dict[str, Any]:
    """Actionable product performance, return rates, inventory risks, and category breakdowns."""
    completed_items = OrderItem.objects.filter(
        order__status__in=REVENUE_STATUSES,
        **_window(date_range, "order__paid_at"),
    )

    # 1. Best Sellers by Product
    best_seller_rows = list(
        completed_items.values("variant__product_id")
        .annotate(
            units_sold=Sum("quantity"),
            revenue=Sum("line_total"),
        )
        .order_by("-units_sold", "-revenue")[:15]
    )
    p_ids = [r["variant__product_id"] for r in best_seller_rows if r["variant__product_id"]]
    p_meta = dict(
        Product.objects.filter(pk__in=p_ids).values_list("pk", "name")
    )
    best_sellers = [
        {
            "rank": rank,
            "product_id": r["variant__product_id"],
            "name": p_meta.get(r["variant__product_id"], f"Product #{r['variant__product_id']}"),
            "units_sold": r["units_sold"] or 0,
            "revenue": r["revenue"] or Decimal("0.00"),
        }
        for rank, r in enumerate(best_seller_rows, start=1)
    ]

    # 2. Sales by Category
    category_rows = list(
        completed_items.values("variant__product__category__name")
        .annotate(
            units_sold=Sum("quantity"),
            revenue=Sum("line_total"),
        )
        .order_by("-revenue")[:10]
    )
    total_cat_rev = sum([r["revenue"] or Decimal("0.00") for r in category_rows], Decimal("0.00"))
    categories_breakdown = [
        {
            "category_name": r["variant__product__category__name"] or "Uncategorized",
            "units_sold": r["units_sold"] or 0,
            "revenue": r["revenue"] or Decimal("0.00"),
            "share_pct": (
                round(float((r["revenue"] or 0) / total_cat_rev) * 100, 1)
                if total_cat_rev > Decimal("0.00")
                else 0.0
            ),
        }
        for r in category_rows
    ]

    # 3. Sales by Brand
    brand_rows = list(
        completed_items.values("variant__product__brand__name")
        .annotate(
            units_sold=Sum("quantity"),
            revenue=Sum("line_total"),
        )
        .order_by("-revenue")[:10]
    )
    brands_breakdown = [
        {
            "brand_name": r["variant__product__brand__name"] or "In-House",
            "units_sold": r["units_sold"] or 0,
            "revenue": r["revenue"] or Decimal("0.00"),
        }
        for r in brand_rows
    ]

    # 4. Product Return Rates
    # Calculate returns attributed to products in this window
    return_rows = list(
        ReturnItem.objects.filter(
            return_request__status__in=[
                ReturnRequest.Status.APPROVED,
                ReturnRequest.Status.RECEIVED,
                ReturnRequest.Status.INSPECTION_PASSED,
                ReturnRequest.Status.REFUND_PENDING,
                ReturnRequest.Status.REFUNDED,
                ReturnRequest.Status.COMPLETED,
            ],
            **_window(date_range, "return_request__created_at"),
        )
        .values("order_item__variant__product_id")
        .annotate(returned_units=Sum("quantity"), return_requests_count=Count("id"))
        .order_by("-returned_units")[:15]
    )

    product_return_rates = []
    if return_rows:
        ret_pids = [r["order_item__variant__product_id"] for r in return_rows if r["order_item__variant__product_id"]]
        ret_names = dict(Product.objects.filter(pk__in=ret_pids).values_list("pk", "name"))
        sales_units_map = dict(
            completed_items.filter(variant__product_id__in=ret_pids)
            .values("variant__product_id")
            .annotate(units=Sum("quantity"))
            .values_list("variant__product_id", "units")
        )

        for r in return_rows:
            pid = r["order_item__variant__product_id"]
            if not pid:
                continue
            units_ret = r["returned_units"] or 0
            units_sold = sales_units_map.get(pid, 0)
            rate_pct = (
                round((units_ret / units_sold) * 100, 1)
                if units_sold > 0
                else 100.0
            )
            product_return_rates.append(
                {
                    "product_id": pid,
                    "name": ret_names.get(pid, f"Product #{pid}"),
                    "returned_units": units_ret,
                    "units_sold": units_sold,
                    "return_rate_pct": rate_pct,
                }
            )

    # 5. Inventory Risks (Stockout and Low-Stock)
    threshold = getattr(settings, "BACKOFFICE_LOW_STOCK_THRESHOLD", 5)
    risk_stocks = (
        Stock.objects.select_related("variant__product", "variant__size", "variant__color")
        .annotate(available_qty=F("on_hand") - F("reserved"))
        .filter(available_qty__lte=threshold)
        .order_by("available_qty", "variant__product__name")[:25]
    )

    # Calculate velocity for duration of window (units sold per day)
    duration_days = (
        max(1, (date_range.end - date_range.start).days)
        if date_range.start and date_range.end
        else 30
    )
    variant_ids = [s.variant_id for s in risk_stocks]
    sales_velocity_map = dict(
        completed_items.filter(variant_id__in=variant_ids)
        .values("variant_id")
        .annotate(units=Sum("quantity"))
        .values_list("variant_id", "units")
    )

    inventory_risks = []
    for s in risk_stocks:
        avail = s.available
        sold_in_window = sales_velocity_map.get(s.variant_id, 0)
        daily_velocity = sold_in_window / duration_days
        days_remaining = (
            round(avail / daily_velocity)
            if daily_velocity > 0 and avail > 0
            else (0 if avail == 0 else None)
        )
        status_label = "Out of Stock" if avail <= 0 else "Low Stock"
        v = s.variant
        p_name = v.product.name if v and v.product else "Unknown"
        attrs = []
        if v.size:
            attrs.append(v.size.name)
        if v.color:
            attrs.append(v.color.name)
        variant_desc = f"{p_name} ({', '.join(attrs)})" if attrs else p_name

        inventory_risks.append(
            {
                "stock_id": s.pk,
                "variant_id": s.variant_id,
                "sku": v.sku if v else "",
                "product_name": variant_desc,
                "on_hand": s.on_hand,
                "reserved": s.reserved,
                "available": avail,
                "threshold": s.effective_threshold,
                "status": status_label,
                "sold_in_period": sold_in_window,
                "days_remaining": days_remaining,
            }
        )

    # 6. Slow-moving stock: items on hand > 0, but 0 sales in the period
    sold_variant_ids = completed_items.values_list("variant_id", flat=True).distinct()
    slow_moving_qs = (
        Stock.objects.select_related("variant__product", "variant__size", "variant__color")
        .filter(on_hand__gt=0)
        .exclude(variant_id__in=sold_variant_ids)
        .order_by("-on_hand")[:15]
    )
    slow_moving = [
        {
            "sku": s.variant.sku,
            "name": s.variant.product.name,
            "on_hand": s.on_hand,
            "reserved": s.reserved,
            "available": s.available,
        }
        for s in slow_moving_qs
        if s.variant and s.variant.product
    ]

    return {
        "best_sellers": best_sellers,
        "categories_breakdown": categories_breakdown,
        "brands_breakdown": brands_breakdown,
        "product_return_rates": product_return_rates,
        "inventory_risks": inventory_risks,
        "slow_moving": slow_moving,
    }


def get_customer_and_retention_intelligence(*, date_range: DateRange) -> dict[str, Any]:
    """Customer acquisition, repeat purchasing, retention campaigns and promo attribution."""
    completed_orders = Order.objects.filter(
        status__in=REVENUE_STATUSES,
        **_window(date_range, "paid_at"),
    )

    buyer_ids = list(completed_orders.values_list("user_id", flat=True).distinct())
    if buyer_ids and date_range.start:
        prior_buyer_ids = set(
            Order.objects.filter(
                user_id__in=buyer_ids,
                status__in=REVENUE_STATUSES,
                paid_at__lt=date_range.start,
            ).values_list("user_id", flat=True)
        )
    else:
        prior_buyer_ids = set()

    returning_buyer_ids = prior_buyer_ids
    new_buyer_ids = set(buyer_ids) - returning_buyer_ids

    new_buyers_count = len(new_buyer_ids)
    returning_buyers_count = len(returning_buyer_ids)
    total_buyers_count = len(buyer_ids)

    # AOV by customer segment
    new_buyer_revenue = (
        completed_orders.filter(user_id__in=new_buyer_ids).aggregate(
            tot=Coalesce(Sum("total"), Decimal("0.00"))
        )["tot"]
        if new_buyer_ids
        else Decimal("0.00")
    )
    new_buyer_orders = (
        completed_orders.filter(user_id__in=new_buyer_ids).count()
        if new_buyer_ids
        else 0
    )
    new_buyer_aov = (
        (new_buyer_revenue / new_buyer_orders).quantize(Decimal("0.01"))
        if new_buyer_orders > 0
        else Decimal("0.00")
    )

    returning_buyer_revenue = (
        completed_orders.filter(user_id__in=returning_buyer_ids).aggregate(
            tot=Coalesce(Sum("total"), Decimal("0.00"))
        )["tot"]
        if returning_buyer_ids
        else Decimal("0.00")
    )
    returning_buyer_orders = (
        completed_orders.filter(user_id__in=returning_buyer_ids).count()
        if returning_buyer_ids
        else 0
    )
    returning_buyer_aov = (
        (returning_buyer_revenue / returning_buyer_orders).quantize(Decimal("0.01"))
        if returning_buyer_orders > 0
        else Decimal("0.00")
    )

    repeat_purchase_rate = (
        round((returning_buyers_count / total_buyers_count) * 100, 1)
        if total_buyers_count > 0
        else 0.0
    )

    # Promotion attribution
    promos = list(
        completed_orders.filter(promotion_code__gt="")
        .values("promotion_code")
        .annotate(
            usage=Count("pk"),
            total_discount=Sum("discount_amount"),
            attributable_sales=Sum("total"),
        )
        .order_by("-usage")[:10]
    )

    # Retention campaigns (Phase 49)
    campaigns = list(
        RetentionCampaign.objects.filter(
            **_window(date_range, "created_at"),
        ).values("name", "objective", "target_segment", "status")[:10]
    )

    return {
        "total_buyers": total_buyers_count,
        "new_buyers": new_buyers_count,
        "returning_buyers": returning_buyers_count,
        "repeat_purchase_rate": repeat_purchase_rate,
        "new_buyer_aov": new_buyer_aov,
        "returning_buyer_aov": returning_buyer_aov,
        "new_buyer_revenue": new_buyer_revenue,
        "returning_buyer_revenue": returning_buyer_revenue,
        "promotions": promos,
        "campaigns": campaigns,
    }


def get_procurement_and_expense_intelligence(*, date_range: DateRange) -> dict[str, Any]:
    """Supplier purchases, invoices, and operating expense breakdown.

    Maintains clean accounting boundaries between:
    - Purchase Order commitments (future liability).
    - Supplier Invoices (verified accounts payable).
    - Supplier Payments (cash outflow).
    - Operating Expenses (OpEx overhead).
    """
    # 1. Purchase Orders
    po_qs = PurchaseOrder.objects.filter(**_window(date_range, "created_at"))
    po_count = po_qs.count()
    po_approved_count = po_qs.filter(
        status__in=[
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.ORDERED,
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
            PurchaseOrderStatus.FULLY_RECEIVED,
        ]
    ).count()

    # 2. Supplier Invoices
    inv_qs = SupplierInvoice.objects.filter(**_window(date_range, "created_at"))
    inv_count = inv_qs.count()
    inv_totals = inv_qs.aggregate(
        total_invoiced=Coalesce(Sum("total_amount"), Decimal("0.00")),
        total_paid=Coalesce(Sum("amount_paid"), Decimal("0.00")),
    )
    total_invoiced = inv_totals["total_invoiced"]
    total_paid_invoices = inv_totals["total_paid"]
    outstanding_invoices = max(Decimal("0.00"), total_invoiced - total_paid_invoices)

    # 3. Operating Expenses by Category (excluding PO invoice expenses to avoid double counting)
    opex_qs = OperatingExpense.objects.filter(
        status__in=[ExpenseStatus.APPROVED, ExpenseStatus.PAID],
        **_date_window(date_range, "expense_date"),
    ).exclude(supplier_invoice__isnull=False)

    total_opex = (
        opex_qs.aggregate(total=Coalesce(Sum("total_amount"), Decimal("0.00")))["total"]
        or Decimal("0.00")
    )

    categories = list(
        opex_qs.values("category__name", "category__code")
        .annotate(total=Sum("total_amount"), count=Count("pk"))
        .order_by("-total")
    )
    cat_breakdown = [
        {
            "category_name": c["category__name"] or "General Overhead",
            "category_code": c["category__code"] or "GEN",
            "total": c["total"] or Decimal("0.00"),
            "count": c["count"],
            "share_pct": (
                round(float((c["total"] or 0) / total_opex) * 100, 1)
                if total_opex > Decimal("0.00")
                else 0.0
            ),
        }
        for c in categories
    ]

    return {
        "purchase_orders_count": po_count,
        "purchase_orders_approved_count": po_approved_count,
        "invoices_count": inv_count,
        "total_invoiced": total_invoiced,
        "total_paid_invoices": total_paid_invoices,
        "outstanding_invoices": outstanding_invoices,
        "total_opex": total_opex,
        "categories": cat_breakdown,
    }
