"""The analytics, BI, merchandising and funnel selectors (Phase 20 / Phase 36).

Every figure is an aggregate over the append-only event log and authoritative domain tables
-- no estimates, no third-party pixels, no per-customer rows. Two conventions:

* **Visitors, not sessions.** A visitor is the middleware's opaque id, falling back to the
  session key and finally the user id, so an anonymous shopper who signs in midway still
  counts once (and nothing here can be used to read an individual back).
* **Stages are independent counts.** A window can legitimately contain more orders than
  checkouts (a returning buyer whose first checkout in the window predates it), so the
  conversion rate is against visitors rather than a monotonic assertion -- and it is 0.0
  with no visitors instead of a divide error.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import CharField, Count, Q, Sum, Value
from django.db.models.functions import Cast, Coalesce, TruncDate

from apps.analytics.models import Event
from apps.backoffice.selectors.common import DateRange
from apps.backoffice.selectors.dashboard import REVENUE_STATUSES
from apps.catalog.models import Product, ProductVariant
from apps.creator.models import CreatorPost, CreatorPostStatus
from apps.orders.models import Order, OrderItem
from apps.payments.models import Refund
from apps.shop.models import CheckoutSession
from apps.support.models import SupportTicket

__all__ = [
    "analytics_dashboard_data",
    "customer_and_support_summary",
    "funnel_summary",
    "merchandising_insights",
    "sales_trends",
]

#: Identity collapsed to one string per visitor: cookie, then session, then account.
_VISITOR = Coalesce(
    "visitor_id",
    "session_key",
    Cast("user_id", CharField()),
    Value(""),
)


def _window(date_range: DateRange, field: str) -> dict:
    """``{field}__gte, {field}__lt}`` kwargs for a half-open window (empty for all time)."""
    if date_range.is_all_time:
        return {}
    return {f"{field}__gte": date_range.start, f"{field}__lt": date_range.end}


def _top_products(events) -> list[dict]:
    """Most-viewed products in the window (one aggregate + one id lookup)."""
    rows = list(
        events.filter(name=Event.Name.PRODUCT_VIEW, object_id__isnull=False)
        .values("object_id")
        .annotate(views=Count("id"))
        .order_by("-views", "object_id")[:10]
    )
    if not rows:
        return []
    names = dict(
        Product.objects.filter(pk__in=[row["object_id"] for row in rows]).values_list("pk", "name")
    )
    # A product deleted since the view happened drops out rather than showing a ghost row.
    return [
        {"name": names[row["object_id"]], "views": row["views"], "product_id": row["object_id"]}
        for row in rows
        if row["object_id"] in names
    ]


def funnel_summary(*, date_range: DateRange) -> dict:
    """Aggregate funnel metrics and step conversion rates for the window."""
    events = Event.objects.filter(**_window(date_range, "created_at"))

    visitors = events.aggregate(n=Count(_VISITOR, distinct=True))["n"] or 0
    product_views = events.filter(name=Event.Name.PRODUCT_VIEW).count()
    searches = events.filter(name=Event.Name.PRODUCT_SEARCH).count()
    cart_adds = events.filter(name__in=[Event.Name.CART_ADD, Event.Name.PRODUCT_TO_CART]).count()
    checkout_started = events.filter(name=Event.Name.CHECKOUT_STARTED).count()

    placed_orders = Order.objects.filter(**_window(date_range, "created_at"))
    orders_placed_count = placed_orders.count()

    completed = Order.objects.filter(**_window(date_range, "paid_at"), status__in=REVENUE_STATUSES)
    completed_count = completed.count()
    gross_revenue = completed.aggregate(total=Sum("total"))["total"] or Decimal("0.00")

    # Completed refunds in window
    refunds = Refund.objects.filter(
        status=Refund.Status.SUCCEEDED,
        **_window(date_range, "processed_at" if date_range.start else "created_at"),
    ).aggregate(total=Coalesce(Sum("amount"), Decimal("0.00")), count=Count("pk"))
    refunds_amount = refunds["total"]
    refunds_count = refunds["count"]
    net_revenue = max(Decimal("0.00"), gross_revenue - refunds_amount)

    # An abandoned checkout is a session that got as far as the checkout page and never
    # converted: still open or validated, in the window. Converted/expired ones are not.
    abandoned = CheckoutSession.objects.filter(
        **_window(date_range, "created_at"),
        status__in=(CheckoutSession.Status.OPEN, CheckoutSession.Status.VALIDATED),
    ).count()

    top_searches = list(
        events.filter(name=Event.Name.PRODUCT_SEARCH)
        .filter(metadata__query__isnull=False)
        .exclude(metadata__query="")
        .values("metadata__query")
        .annotate(hits=Count("id"))
        .order_by("-hits", "metadata__query")[:10]
    )

    conversion_rate = round((completed_count / visitors * 100) if visitors else 0.0, 1)

    step_conversion = {
        "view_to_cart": round((cart_adds / product_views * 100) if product_views else 0.0, 1),
        "cart_to_checkout": round((checkout_started / cart_adds * 100) if cart_adds else 0.0, 1),
        "checkout_to_order": round(
            (orders_placed_count / checkout_started * 100) if checkout_started else 0.0, 1
        ),
        "order_to_paid": round(
            (completed_count / orders_placed_count * 100) if orders_placed_count else 0.0, 1
        ),
    }

    return {
        "range": date_range,
        "visitors": visitors,
        "product_views": product_views,
        "searches": searches,
        "cart_adds": cart_adds,
        "checkout_started": checkout_started,
        "orders_placed": orders_placed_count,
        "orders_completed": completed_count,
        "conversion_rate": conversion_rate,
        "step_conversion": step_conversion,
        "revenue": gross_revenue,
        "gross_revenue": gross_revenue,
        "net_revenue": net_revenue,
        "refunds_amount": refunds_amount,
        "refunds_count": refunds_count,
        "aov": (gross_revenue / completed_count) if completed_count else None,
        "abandoned_checkouts": abandoned,
        "top_products": _top_products(events),
        "top_searches": top_searches,
    }


def merchandising_insights(*, date_range: DateRange) -> dict:
    """Actionable product, search, collection and engagement metrics for staff."""
    events = Event.objects.filter(**_window(date_range, "created_at"))

    # Completed order items in window
    completed_items = OrderItem.objects.filter(
        order__status__in=REVENUE_STATUSES,
        **_window(date_range, "order__paid_at"),
    )

    # 1. Best selling products (grouped by variant's product)
    best_seller_rows = list(
        completed_items.values("variant__product_id")
        .annotate(
            units_sold=Sum("quantity"),
            revenue=Sum("line_total"),
        )
        .order_by("-units_sold", "-revenue")[:10]
    )
    product_pks = [
        row["variant__product_id"] for row in best_seller_rows if row["variant__product_id"]
    ]
    product_info = {
        p.pk: {"name": p.name, "slug": p.slug} for p in Product.objects.filter(pk__in=product_pks)
    }
    best_sellers = []
    for rank, row in enumerate(best_seller_rows, start=1):
        pid = row["variant__product_id"]
        meta = product_info.get(pid, {"name": f"Product #{pid}", "slug": ""})
        best_sellers.append(
            {
                "rank": rank,
                "product_id": pid,
                "name": meta["name"],
                "slug": meta["slug"],
                "units_sold": row["units_sold"] or 0,
                "revenue": row["revenue"] or Decimal("0.00"),
            }
        )

    # 2. Products with zero sales in window
    sold_product_ids = completed_items.values_list("variant__product_id", flat=True).distinct()
    zero_sales_qs = Product.objects.filter(status=Product.Status.ACTIVE).exclude(
        pk__in=sold_product_ids
    )
    zero_sales_count = zero_sales_qs.count()
    zero_sales_sample = list(zero_sales_qs.values("id", "name", "slug").order_by("name")[:10])

    # 3. High views but low purchases (high traffic, <= 1 sales)
    top_views = list(
        events.filter(name=Event.Name.PRODUCT_VIEW, object_id__isnull=False)
        .values("object_id")
        .annotate(views=Count("id"))
        .order_by("-views")[:20]
    )
    if top_views:
        view_pids = [row["object_id"] for row in top_views]
        sales_map = dict(
            completed_items.filter(variant__product_id__in=view_pids)
            .values("variant__product_id")
            .annotate(units=Sum("quantity"))
            .values_list("variant__product_id", "units")
        )
        view_names = dict(Product.objects.filter(pk__in=view_pids).values_list("pk", "name"))
        high_view_low_purchase = [
            {
                "product_id": row["object_id"],
                "name": view_names.get(row["object_id"], f"Product #{row['object_id']}"),
                "views": row["views"],
                "units_sold": sales_map.get(row["object_id"], 0),
            }
            for row in top_views
            if row["object_id"] in view_names and sales_map.get(row["object_id"], 0) <= 1
        ][:10]
    else:
        high_view_low_purchase = []

    # 4. Search insights: popular searches and weak/zero-result searches
    top_searches = list(
        events.filter(name=Event.Name.PRODUCT_SEARCH)
        .filter(metadata__query__isnull=False)
        .exclude(metadata__query="")
        .values("metadata__query")
        .annotate(hits=Count("id"))
        .order_by("-hits", "metadata__query")[:10]
    )
    weak_searches = list(
        events.filter(name=Event.Name.PRODUCT_SEARCH)
        .filter(Q(metadata__result_count=0) | Q(metadata__results_count=0))
        .filter(metadata__query__isnull=False)
        .exclude(metadata__query="")
        .values("metadata__query")
        .annotate(hits=Count("id"))
        .order_by("-hits", "metadata__query")[:10]
    )

    # 5. Wishlist & Cart activity
    wishlist_rows = list(
        events.filter(name=Event.Name.WISHLIST_ADD, object_id__isnull=False)
        .values("object_id")
        .annotate(adds=Count("id"))
        .order_by("-adds")[:10]
    )
    w_pks = [r["object_id"] for r in wishlist_rows]
    w_names = dict(Product.objects.filter(pk__in=w_pks).values_list("pk", "name"))
    top_wishlisted = [
        {"name": w_names[r["object_id"]], "adds": r["adds"], "product_id": r["object_id"]}
        for r in wishlist_rows
        if r["object_id"] in w_names
    ]

    cart_add_rows = list(
        events.filter(name=Event.Name.CART_ADD, object_id__isnull=False)
        .values("object_id", "object_type")
        .annotate(adds=Count("id"))
        .order_by("-adds")[:10]
    )
    variant_ids = [r["object_id"] for r in cart_add_rows if r["object_type"] == "variant"]
    variant_product_map = dict(
        ProductVariant.objects.filter(pk__in=variant_ids).values_list("pk", "product__name")
    )
    top_cart_adds = []
    for r in cart_add_rows:
        if r["object_type"] == "variant":
            name = variant_product_map.get(r["object_id"])
        else:
            name = w_names.get(r["object_id"])
        if name:
            top_cart_adds.append({"name": name, "adds": r["adds"]})

    # 6. Recommendation engagement
    rec_views = events.filter(name=Event.Name.RECOMMENDATION_VIEW).count()
    rec_clicks = events.filter(
        name__in=[Event.Name.RECOMMENDATION_CLICK, Event.Name.RECOMMENDATION_CLICKED]
    ).count()
    rec_ctr = round((rec_clicks / rec_views * 100) if rec_views else 0.0, 1)

    # 7. Drop and Creator performance
    drop_views = events.filter(name=Event.Name.DROP_VIEW).count()
    drop_purchases = events.filter(name=Event.Name.DROP_PURCHASE).count()
    creator_views = events.filter(name=Event.Name.CREATOR_CONTENT_VIEW).count()
    published_creator_posts = CreatorPost.objects.filter(
        status=CreatorPostStatus.PUBLISHED,
        **_window(date_range, "published_at"),
    ).count()

    return {
        "best_sellers": best_sellers,
        "zero_sales_count": zero_sales_count,
        "zero_sales_sample": zero_sales_sample,
        "high_view_low_purchase": high_view_low_purchase,
        "top_searches": top_searches,
        "weak_searches": weak_searches,
        "top_wishlisted": top_wishlisted,
        "top_cart_adds": top_cart_adds,
        "recommendations": {
            "views": rec_views,
            "clicks": rec_clicks,
            "ctr": rec_ctr,
        },
        "drops_and_creators": {
            "drop_views": drop_views,
            "drop_purchases": drop_purchases,
            "creator_views": creator_views,
            "published_creator_posts": published_creator_posts,
        },
    }


def sales_trends(*, date_range: DateRange) -> dict:
    """Daily revenue and completed order trend series for charting."""
    completed = Order.objects.filter(
        status__in=REVENUE_STATUSES,
        **_window(date_range, "paid_at"),
    )
    daily_rows = list(
        completed.annotate(day=TruncDate("paid_at"))
        .values("day")
        .annotate(
            revenue=Coalesce(Sum("total"), Decimal("0.00")),
            orders=Count("pk"),
        )
        .order_by("day")
    )

    series = [
        {
            "date": row["day"].isoformat() if row["day"] else "",
            "label": row["day"].strftime("%b %d") if row["day"] else "",
            "revenue": row["revenue"],
            "orders": row["orders"],
        }
        for row in daily_rows
        if row["day"] is not None
    ]

    max_revenue = max([s["revenue"] for s in series], default=Decimal("0.00"))
    max_orders = max([s["orders"] for s in series], default=0)

    # Attach percentage heights for simple, reliable CSS/SVG bar chart rendering
    for item in series:
        rev = float(item["revenue"])
        max_r = float(max_revenue) if max_revenue > 0 else 1.0
        item["height_pct"] = round((rev / max_r) * 100) if max_r > 0 else 0

    return {
        "series": series,
        "total_days": len(series),
        "max_revenue": max_revenue,
        "max_orders": max_orders,
    }


def customer_and_support_summary(*, date_range: DateRange) -> dict:
    """Customer segments, promotions usage, returns, refunds, and support resolution metrics."""
    completed_orders = Order.objects.filter(
        status__in=REVENUE_STATUSES,
        **_window(date_range, "paid_at"),
    )

    # New vs returning buyers
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

    # Promotions & loyalty
    promo_orders = completed_orders.filter(promotion_code__gt="")
    promo_orders_count = promo_orders.count()
    promo_discounts = completed_orders.aggregate(
        d=Coalesce(Sum("discount_amount"), Decimal("0.00"))
    )["d"]
    top_promos = list(
        promo_orders.values("promotion_code")
        .annotate(usage=Count("pk"), discounts=Sum("discount_amount"))
        .order_by("-usage")[:5]
    )

    # Cancellations
    cancellations = Order.objects.filter(
        status=Order.Status.CANCELLED,
        **_window(date_range, "cancelled_at" if date_range.start else "created_at"),
    ).aggregate(total=Coalesce(Sum("total"), Decimal("0.00")), count=Count("pk"))

    # Refunds
    refunds = Refund.objects.filter(
        status=Refund.Status.SUCCEEDED,
        **_window(date_range, "processed_at" if date_range.start else "created_at"),
    ).aggregate(total=Coalesce(Sum("amount"), Decimal("0.00")), count=Count("pk"))

    # Support tickets & resolution
    tickets_opened = SupportTicket.objects.filter(**_window(date_range, "created_at")).count()
    resolved_pairs = list(
        SupportTicket.objects.filter(
            status__in=[SupportTicket.Status.RESOLVED, SupportTicket.Status.CLOSED],
            resolved_at__isnull=False,
            **_window(date_range, "resolved_at"),
        ).values_list("created_at", "resolved_at")[:200]
    )
    durations = [
        (res - cr).total_seconds() / 3600.0
        for cr, res in resolved_pairs
        if res and cr and res >= cr
    ]
    avg_resolution_hours = round(sum(durations) / len(durations), 1) if durations else None

    return {
        "new_buyers": new_buyers,
        "returning_buyers": returning_buyers,
        "promo_orders_count": promo_orders_count,
        "promo_discounts": promo_discounts,
        "top_promos": top_promos,
        "cancellations_count": cancellations["count"],
        "cancellations_total": cancellations["total"],
        "refunds_count": refunds["count"],
        "refunds_total": refunds["total"],
        "tickets_opened": tickets_opened,
        "tickets_resolved": len(durations),
        "avg_resolution_hours": avg_resolution_hours,
    }


def analytics_dashboard_data(*, date_range: DateRange) -> dict:
    """Master selector for the entire /operations/analytics/ screen."""
    funnel = funnel_summary(date_range=date_range)
    merch = merchandising_insights(date_range=date_range)
    trends = sales_trends(date_range=date_range)
    cust_supp = customer_and_support_summary(date_range=date_range)

    return {
        "range": date_range,
        "funnel": funnel,
        "merchandising": merch,
        "trends": trends,
        "customers_and_support": cust_supp,
    }
