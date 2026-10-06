"""The funnel numbers behind ``/operations/analytics/`` (Phase 20).

Every figure is an aggregate over the append-only event log and the orders table -- no
estimates, no third-party pixels, no per-customer rows. Two conventions:

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

from django.db.models import CharField, Count, Sum, Value
from django.db.models.functions import Cast, Coalesce

from apps.analytics.models import Event
from apps.backoffice.selectors.common import DateRange
from apps.backoffice.selectors.dashboard import REVENUE_STATUSES
from apps.catalog.models import Product
from apps.orders.models import Order
from apps.shop.models import CheckoutSession

__all__ = ["funnel_summary"]

#: Identity collapsed to one string per visitor: cookie, then session, then account.
_VISITOR = Coalesce(
    "visitor_id",
    "session_key",
    Cast("user_id", CharField()),
    Value(""),
)


def _window(date_range: DateRange, field: str) -> dict:
    """``{field}__gte, field}__lt}`` kwargs for a half-open window (empty for all time)."""
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
        {"name": names[row["object_id"]], "views": row["views"]}
        for row in rows
        if row["object_id"] in names
    ]


def funnel_summary(*, date_range: DateRange) -> dict:
    """Aggregate funnel metrics for the window (one query per metric)."""
    events = Event.objects.filter(**_window(date_range, "created_at"))

    visitors = events.aggregate(n=Count(_VISITOR, distinct=True))["n"] or 0
    product_views = events.filter(name=Event.Name.PRODUCT_VIEW).count()
    searches = events.filter(name=Event.Name.PRODUCT_SEARCH).count()
    cart_adds = events.filter(name=Event.Name.CART_ADD).count()
    checkout_started = events.filter(name=Event.Name.CHECKOUT_STARTED).count()

    completed = Order.objects.filter(
        **_window(date_range, "created_at"), status__in=REVENUE_STATUSES
    )
    completed_count = completed.count()
    revenue = completed.aggregate(total=Sum("total"))["total"] or Decimal("0.00")

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

    return {
        "range": date_range,
        "visitors": visitors,
        "product_views": product_views,
        "searches": searches,
        "cart_adds": cart_adds,
        "checkout_started": checkout_started,
        "orders_completed": completed_count,
        "conversion_rate": round((completed_count / visitors * 100) if visitors else 0.0, 1),
        "revenue": revenue,
        "aov": (revenue / completed_count) if completed_count else None,
        "abandoned_checkouts": abandoned,
        "top_products": _top_products(events),
        "top_searches": top_searches,
    }
