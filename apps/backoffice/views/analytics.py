"""The analytics funnel screen and reporting export (Phase 20 / Phase 36 / Phase 55).

Aggregates only: the selector returns counts, rates, and top lists -- never a visitor id,
session key, IP, or any other per-person row -- so the capability ``analytics.view`` is all
a staff member needs and nothing here can be used to read an individual customer's path.

Phase 55 adds:
- Executive BI metrics with period-over-period comparison.
- Product & inventory intelligence (return rates, stockout risks, velocity).
- Customer retention and promo attribution.
- Procurement and OpEx breakdowns for authorized roles.
- Formula-injection-sanitized CSV exports across multiple report types.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.http import Http404, HttpResponse

from apps.backoffice.permissions import (
    ANALYTICS_VIEW,
    EXPENSES_VIEW,
    PROFITABILITY_VIEW,
    PURCHASING_VIEW,
    backoffice_access,
    has,
)
from apps.backoffice.selectors.analytics import (
    analytics_dashboard_data,
    funnel_summary,
    get_customer_and_retention_intelligence,
    get_executive_bi_metrics,
    get_procurement_and_expense_intelligence,
    get_product_and_inventory_intelligence,
    get_sales_time_series,
    merchandising_insights,
    sanitize_csv_cell,
)
from apps.backoffice.views.base import range_from_request, render_bo

__all__ = ["analytics", "export_analytics_csv"]


@backoffice_access(ANALYTICS_VIEW)
def analytics(request):
    """``/operations/analytics/`` -- executive dashboard, sales, products, inventory risks, funnel."""
    date_range = range_from_request(request)
    data = analytics_dashboard_data(date_range=date_range)

    # Permission check for sensitive executive expense / procurement insights
    can_view_finance = has(request.user, PROFITABILITY_VIEW) or has(request.user, EXPENSES_VIEW)
    procurement_data = (
        get_procurement_and_expense_intelligence(date_range=date_range)
        if can_view_finance
        else None
    )

    # Customer retention details
    customer_retention = get_customer_and_retention_intelligence(date_range=date_range)

    # Sales time series for interactive charting
    time_series = get_sales_time_series(date_range=date_range)

    currency_code = getattr(settings, "CATALOG_CURRENCY_CODE", "USD")
    currency_symbol = getattr(settings, "CATALOG_CURRENCY_SYMBOL", "$")

    return render_bo(
        request,
        "backoffice/analytics.html",
        active="analytics",
        summary=data["funnel"],
        data=data,
        executive=data.get("executive", {}),
        product_bi=data.get("product_bi", {}),
        procurement_bi=procurement_data,
        customer_retention=customer_retention,
        time_series=time_series,
        can_view_finance=can_view_finance,
        currency_code=currency_code,
        currency_symbol=currency_symbol,
        current_range=date_range.key,
        date_range=date_range,
        from_date=request.GET.get("from", ""),
        to_date=request.GET.get("to", ""),
    )


def _safe_write_row(writer, row: list[Any]) -> None:
    """Write row to CSV with spreadsheet formula injection protection."""
    writer.writerow([sanitize_csv_cell(cell) for cell in row])


@backoffice_access(ANALYTICS_VIEW)
def export_analytics_csv(request):
    """Export filtered analytics report as CSV with formula injection protection.

    Gated by ``analytics.view``. Financial expense reports additionally require
    financial or purchasing access.
    """
    date_range = range_from_request(request)
    export_type = request.GET.get("type", "summary").lower()
    currency = getattr(settings, "CATALOG_CURRENCY_CODE", "USD")

    # Access control: financial / expense reports require executive financial authority
    if export_type in ("expenses", "procurement"):
        if not (
            has(request.user, PROFITABILITY_VIEW)
            or request.user.is_superuser
        ):
            raise Http404()

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = f"flashwear-analytics-{export_type}-{date_range.key}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)

    if export_type == "products":
        prod_data = get_product_and_inventory_intelligence(date_range=date_range)
        _safe_write_row(
            writer,
            ["Rank", "Product ID", "Product Name", "Units Sold", "Revenue (GBP)"],
        )
        for row in prod_data["best_sellers"]:
            _safe_write_row(
                writer,
                [
                    row["rank"],
                    row["product_id"],
                    row["name"],
                    row["units_sold"],
                    f"{row['revenue']:.2f}",
                ],
            )

        # Add section for product return rates if data exists
        if prod_data["product_return_rates"]:
            writer.writerow([])
            _safe_write_row(writer, ["--- Product Return Rates ---", "", "", "", ""])
            _safe_write_row(
                writer,
                ["Product ID", "Product Name", "Returned Units", "Units Sold", "Return Rate (%)"],
            )
            for ret in prod_data["product_return_rates"]:
                _safe_write_row(
                    writer,
                    [
                        ret["product_id"],
                        ret["name"],
                        ret["returned_units"],
                        ret["units_sold"],
                        f"{ret['return_rate_pct']:.1f}%",
                    ],
                )

    elif export_type in ("inventory", "inventory_risks"):
        prod_data = get_product_and_inventory_intelligence(date_range=date_range)
        _safe_write_row(
            writer,
            [
                "SKU",
                "Product & Variant",
                "On Hand",
                "Reserved",
                "Available",
                "Threshold",
                "Status",
                "Units Sold (Window)",
                "Estimated Days Remaining",
            ],
        )
        for risk in prod_data["inventory_risks"]:
            days_str = (
                str(risk["days_remaining"])
                if risk["days_remaining"] is not None
                else "N/A"
            )
            _safe_write_row(
                writer,
                [
                    risk["sku"],
                    risk["product_name"],
                    risk["on_hand"],
                    risk["reserved"],
                    risk["available"],
                    risk["threshold"],
                    risk["status"],
                    risk["sold_in_period"],
                    days_str,
                ],
            )

    elif export_type == "sales":
        ts = get_sales_time_series(date_range=date_range)
        _safe_write_row(
            writer,
            [
                "Date",
                "Completed Orders",
                f"Gross Revenue ({currency})",
                f"Discounts ({currency})",
                f"Refunds ({currency})",
                f"Net Revenue ({currency})",
            ],
        )
        for pt in ts["series"]:
            _safe_write_row(
                writer,
                [
                    pt["date"],
                    pt["orders"],
                    f"{pt['gross_revenue']:.2f}",
                    f"{pt['discounts']:.2f}",
                    f"{pt['refunds']:.2f}",
                    f"{pt['net_revenue']:.2f}",
                ],
            )

    elif export_type in ("customers", "retention"):
        cust = get_customer_and_retention_intelligence(date_range=date_range)
        _safe_write_row(writer, ["Customer Metric", "Value"])
        _safe_write_row(writer, ["Report", "Customer Retention & Acquisition"])
        _safe_write_row(writer, ["Date Range", date_range.label])
        _safe_write_row(writer, ["Total Purchasing Customers", cust["total_buyers"]])
        _safe_write_row(writer, ["New Customers", cust["new_buyers"]])
        _safe_write_row(writer, ["Returning Customers", cust["returning_buyers"]])
        _safe_write_row(writer, ["Repeat Purchase Rate (%)", f"{cust['repeat_purchase_rate']:.1f}%"])
        _safe_write_row(writer, [f"New Customer AOV ({currency})", f"{cust['new_buyer_aov']:.2f}"])
        _safe_write_row(writer, [f"Returning Customer AOV ({currency})", f"{cust['returning_buyer_aov']:.2f}"])
        _safe_write_row(writer, [f"New Customer Revenue ({currency})", f"{cust['new_buyer_revenue']:.2f}"])
        _safe_write_row(writer, [f"Returning Customer Revenue ({currency})", f"{cust['returning_buyer_revenue']:.2f}"])

        if cust["promotions"]:
            writer.writerow([])
            _safe_write_row(writer, ["--- Promotion Code Performance ---", "", ""])
            _safe_write_row(writer, ["Promo Code", "Times Used", f"Total Discounts ({currency})", f"Attributable Revenue ({currency})"])
            for p in cust["promotions"]:
                _safe_write_row(
                    writer,
                    [
                        p["promotion_code"],
                        p["usage"],
                        f"{p['total_discount']:.2f}",
                        f"{p['attributable_sales']:.2f}",
                    ],
                )

    elif export_type in ("expenses", "procurement"):
        proc = get_procurement_and_expense_intelligence(date_range=date_range)
        _safe_write_row(writer, ["Expense & Supplier Metric", "Value"])
        _safe_write_row(writer, ["Report", "Operating Expenses & Supplier Summary"])
        _safe_write_row(writer, ["Date Range", date_range.label])
        _safe_write_row(writer, ["Purchase Orders Created", proc["purchase_orders_count"]])
        _safe_write_row(writer, ["Purchase Orders Approved", proc["purchase_orders_approved_count"]])
        _safe_write_row(writer, ["Supplier Invoices Count", proc["invoices_count"]])
        _safe_write_row(writer, [f"Total Supplier Invoiced ({currency})", f"{proc['total_invoiced']:.2f}"])
        _safe_write_row(writer, [f"Total Supplier Invoices Paid ({currency})", f"{proc['total_paid_invoices']:.2f}"])
        _safe_write_row(writer, [f"Outstanding Supplier Invoices ({currency})", f"{proc['outstanding_invoices']:.2f}"])
        _safe_write_row(writer, [f"Total Operating Expenses ({currency})", f"{proc['total_opex']:.2f}"])

        if proc["categories"]:
            writer.writerow([])
            _safe_write_row(writer, ["--- Operating Expenses by Category ---", "", "", ""])
            _safe_write_row(writer, ["Category Name", "Code", f"Total Amount ({currency})", "Expense Records", "Share (%)"])
            for cat in proc["categories"]:
                _safe_write_row(
                    writer,
                    [
                        cat["category_name"],
                        cat["category_code"],
                        f"{cat['total']:.2f}",
                        cat["count"],
                        f"{cat['share_pct']:.1f}%",
                    ],
                )

    elif export_type == "funnel":
        funnel = funnel_summary(date_range=date_range)
        _safe_write_row(writer, ["Stage", "Count", "Step Conversion Rate (%)"])
        _safe_write_row(writer, ["Visitors", funnel["visitors"], "100.0"])
        _safe_write_row(
            writer,
            [
                "Product Views",
                funnel["product_views"],
                str(funnel["step_conversion"]["view_to_cart"]),
            ],
        )
        _safe_write_row(
            writer,
            ["Cart Adds", funnel["cart_adds"], str(funnel["step_conversion"]["cart_to_checkout"])],
        )
        _safe_write_row(
            writer,
            [
                "Checkouts Started",
                funnel["checkout_started"],
                str(funnel["step_conversion"]["checkout_to_order"]),
            ],
        )
        _safe_write_row(
            writer,
            [
                "Orders Placed",
                funnel["orders_placed"],
                str(funnel["step_conversion"]["order_to_paid"]),
            ],
        )
        _safe_write_row(
            writer,
            [
                "Orders Completed",
                funnel["orders_completed"],
                str(funnel["conversion_rate"]),
            ],
        )

    else:  # summary
        data = analytics_dashboard_data(date_range=date_range)
        f = data["funnel"]
        cs = data["customers_and_support"]
        m = data["merchandising"]
        exec_kpis = data.get("executive", {})
        curr = exec_kpis.get("current", {})
        deltas = exec_kpis.get("deltas", {})

        _safe_write_row(writer, ["Metric", "Value"])
        _safe_write_row(writer, ["Report", "FLASHWEAR Business Analytics Summary"])
        _safe_write_row(writer, ["Date Range", date_range.label])
        _safe_write_row(writer, ["Gross Revenue (GBP)", f"{curr.get('gross_revenue', f['gross_revenue']):.2f}"])
        _safe_write_row(writer, ["Net Revenue (GBP)", f"{curr.get('net_sales', f['net_revenue']):.2f}"])
        _safe_write_row(writer, ["Completed Orders", curr.get("paid_orders_count", f["orders_completed"])])
        _safe_write_row(writer, ["Placed Orders", f["orders_placed"]])
        _safe_write_row(writer, ["Average Order Value (GBP)", f"{curr.get('aov', f['aov']):.2f}" if curr.get("aov") else "0.00"])
        _safe_write_row(writer, ["Distinct Visitors", f["visitors"]])
        _safe_write_row(writer, ["Conversion Rate (%)", f"{f['conversion_rate']}%"])
        _safe_write_row(writer, ["Refunds Count", cs["refunds_count"]])
        _safe_write_row(writer, ["Refunds Total (GBP)", str(cs["refunds_total"])])
        _safe_write_row(writer, ["Cancellations Count", cs["cancellations_count"]])
        _safe_write_row(writer, ["Cancellations Total (GBP)", str(cs["cancellations_total"])])
        _safe_write_row(writer, ["New Buyers", cs["new_buyers"]])
        _safe_write_row(writer, ["Returning Buyers", cs["returning_buyers"]])
        _safe_write_row(writer, ["Support Tickets Opened", cs["tickets_opened"]])
        _safe_write_row(writer, ["Support Tickets Resolved", cs["tickets_resolved"]])
        _safe_write_row(writer, ["Avg Resolution Hours", cs["avg_resolution_hours"] or "N/A"])
        _safe_write_row(writer, ["Products with Zero Sales", m["zero_sales_count"]])

        # Cost and Profit metrics
        cost_status = curr.get("cost_status", "unavailable")
        gp = curr.get("estimated_gross_profit")
        gp_str = f"{gp:.2f}" if gp is not None else "Unavailable (Incomplete Cost Data)"
        gm_pct = curr.get("gross_margin_pct")
        gm_str = f"{gm_pct:.1f}%" if gm_pct is not None else "Unavailable"
        _safe_write_row(writer, [f"Estimated Gross Profit ({currency})", gp_str])
        _safe_write_row(writer, ["Estimated Gross Margin (%)", gm_str])

        # Operating Expenses & Profit
        _safe_write_row(writer, [f"Operating Expenses ({currency})", f"{curr.get('opex_total', Decimal('0.00')):.2f}"])
        op = curr.get("operating_profit")
        op_str = f"{op:.2f}" if op is not None else "Unavailable"
        _safe_write_row(writer, [f"Estimated Operating Profit ({currency})", op_str])

        # Fulfillment & Delivery
        ful_hrs = curr.get("avg_fulfillment_hours")
        _safe_write_row(writer, ["Avg Fulfillment Time (Hours)", f"{ful_hrs:.1f}h" if ful_hrs is not None else "N/A"])
        del_hrs = curr.get("avg_delivery_hours")
        _safe_write_row(writer, ["Avg Delivery Transit (Hours)", f"{del_hrs:.1f}h" if del_hrs is not None else "N/A"])

        # Repeat rate
        _safe_write_row(writer, ["Repeat Purchase Rate (%)", f"{curr.get('repeat_rate_pct', 0.0):.1f}%"])

        # Inventory
        inv = exec_kpis.get("inventory", {})
        _safe_write_row(writer, ["Inventory On Hand", inv.get("on_hand", 0)])
        _safe_write_row(writer, ["Inventory Reserved", inv.get("reserved", 0)])
        _safe_write_row(writer, ["Inventory Available", inv.get("available", 0)])
        _safe_write_row(writer, ["Low-Stock Items", inv.get("low_stock_count", 0)])
        _safe_write_row(writer, ["Out-of-Stock Items", inv.get("out_of_stock_count", 0)])

    return response
