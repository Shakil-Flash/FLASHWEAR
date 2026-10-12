"""The analytics funnel screen and reporting export (Phase 20 / Phase 36).

Aggregates only: the selector returns counts, rates and top lists -- never a visitor id,
session key, IP or any other per-person row -- so the capability ``analytics.view`` is all
a staff member needs and nothing here can be used to read an individual customer's path.
"""

from __future__ import annotations

import csv

from django.http import HttpResponse

from apps.backoffice.permissions import ANALYTICS_VIEW, backoffice_access
from apps.backoffice.selectors.analytics import (
    analytics_dashboard_data,
    funnel_summary,
    merchandising_insights,
)
from apps.backoffice.views.base import range_from_request, render_bo

__all__ = ["analytics", "export_analytics_csv"]


@backoffice_access(ANALYTICS_VIEW)
def analytics(request):
    """``/operations/analytics/`` -- visitors, funnel stages, top products and searches."""
    date_range = range_from_request(request)
    data = analytics_dashboard_data(date_range=date_range)
    return render_bo(
        request,
        "backoffice/analytics.html",
        active="analytics",
        summary=data["funnel"],
        data=data,
        current_range=date_range.key,
        date_range=date_range,
    )


@backoffice_access(ANALYTICS_VIEW)
def export_analytics_csv(request):
    """Export filtered analytics report as CSV. Gated by analytics.view."""
    date_range = range_from_request(request)
    export_type = request.GET.get("type", "summary").lower()

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = f"flashwear-analytics-{export_type}-{date_range.key}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)

    if export_type == "products":
        merch = merchandising_insights(date_range=date_range)
        writer.writerow(["Rank", "Product ID", "Product Name", "Units Sold", "Revenue (GBP)"])
        for row in merch["best_sellers"]:
            writer.writerow(
                [
                    row["rank"],
                    row["product_id"],
                    row["name"],
                    row["units_sold"],
                    str(row["revenue"]),
                ]
            )
    elif export_type == "funnel":
        funnel = funnel_summary(date_range=date_range)
        writer.writerow(["Stage", "Count", "Step Conversion Rate (%)"])
        writer.writerow(["Visitors", funnel["visitors"], "100.0"])
        writer.writerow(
            [
                "Product Views",
                funnel["product_views"],
                str(funnel["step_conversion"]["view_to_cart"]),
            ]
        )
        writer.writerow(
            ["Cart Adds", funnel["cart_adds"], str(funnel["step_conversion"]["cart_to_checkout"])]
        )
        writer.writerow(
            [
                "Checkouts Started",
                funnel["checkout_started"],
                str(funnel["step_conversion"]["checkout_to_order"]),
            ]
        )
        writer.writerow(
            [
                "Orders Placed",
                funnel["orders_placed"],
                str(funnel["step_conversion"]["order_to_paid"]),
            ]
        )
        writer.writerow(
            [
                "Orders Completed",
                funnel["orders_completed"],
                str(funnel["conversion_rate"]),
            ]
        )
    else:  # summary
        data = analytics_dashboard_data(date_range=date_range)
        f = data["funnel"]
        cs = data["customers_and_support"]
        m = data["merchandising"]
        writer.writerow(["Metric", "Value"])
        writer.writerow(["Report", "FLASHWEAR Business Analytics Summary"])
        writer.writerow(["Date Range", date_range.label])
        writer.writerow(["Gross Revenue (GBP)", str(f["gross_revenue"])])
        writer.writerow(["Net Revenue (GBP)", str(f["net_revenue"])])
        writer.writerow(["Completed Orders", f["orders_completed"]])
        writer.writerow(["Placed Orders", f["orders_placed"]])
        writer.writerow(["Average Order Value (GBP)", f"{f['aov']:.2f}" if f["aov"] else "0.00"])
        writer.writerow(["Distinct Visitors", f["visitors"]])
        writer.writerow(["Conversion Rate (%)", f"{f['conversion_rate']}%"])
        writer.writerow(["Refunds Count", cs["refunds_count"]])
        writer.writerow(["Refunds Total (GBP)", str(cs["refunds_total"])])
        writer.writerow(["Cancellations Count", cs["cancellations_count"]])
        writer.writerow(["Cancellations Total (GBP)", str(cs["cancellations_total"])])
        writer.writerow(["New Buyers", cs["new_buyers"]])
        writer.writerow(["Returning Buyers", cs["returning_buyers"]])
        writer.writerow(["Support Tickets Opened", cs["tickets_opened"]])
        writer.writerow(["Support Tickets Resolved", cs["tickets_resolved"]])
        writer.writerow(["Avg Resolution Hours", cs["avg_resolution_hours"] or "N/A"])
        writer.writerow(["Products with Zero Sales", m["zero_sales_count"]])

    return response
