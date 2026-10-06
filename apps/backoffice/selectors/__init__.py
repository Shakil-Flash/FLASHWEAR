"""Read-side selectors for the Back Office (Phase 16).

Where a query happens decides what it may contain:

* list selectors return an ordered, eager-loaded queryset and nothing else -- the view
  pages them, the template renders rows, and no screen ever loads a whole table;
* filter helpers parse *and allowlist* user input, so a query parameter can only ever
  select from a known set of states or sort by a known set of columns;
* the dashboard selectors aggregate with ``values(...).annotate(...)`` so a metric costs
  one query whether the shop has ten orders or ten thousand.

Everything a view is allowed to reach is re-exported here, so a view never reaches into a
module it was not meant to know about -- and a typo in a selector name fails at import
time rather than on a screen nobody has opened yet.
"""

from __future__ import annotations

from apps.backoffice.selectors.analytics import funnel_summary
from apps.backoffice.selectors.audit import AUDIT_ACTIONS, audit_events
from apps.backoffice.selectors.catalog import (
    PRODUCT_SORTS,
    catalog_health,
    drops,
    low_stock_rows,
    products,
    promotion_state,
    promotions,
    quests,
    recent_movements,
    stock_rows,
    stock_state,
)
from apps.backoffice.selectors.common import (
    RANGES,
    DateRange,
    paginate,
    resolve_range,
    sort_queryset,
)
from apps.backoffice.selectors.dashboard import REVENUE_STATUSES, dashboard_summary
from apps.backoffice.selectors.notifications import (
    CHANNEL_CHOICES,
    NOTIFICATION_SORTS,
    STATUS_CHOICES,
    TYPE_CHOICES,
    notification_detail,
    notification_metrics,
    notifications,
)
from apps.backoffice.selectors.queues import (
    REVIEW_SORTS,
    SUPPORT_SORTS,
    loop_items,
    open_support_statuses,
    pending_loop_count,
    resale_listings,
    review_queue,
    support_tickets,
)
from apps.backoffice.selectors.sales import (
    CUSTOMER_SORTS,
    ORDER_SORTS,
    customer_search,
    order_detail,
    order_events,
    order_timeline,
    orders,
    payment_detail,
    payment_events,
    payments,
    points_ledger,
    provider_label,
    return_detail,
    returns,
    shipments,
)

__all__ = [
    "AUDIT_ACTIONS",
    "CHANNEL_CHOICES",
    "CUSTOMER_SORTS",
    "NOTIFICATION_SORTS",
    "ORDER_SORTS",
    "PRODUCT_SORTS",
    "RANGES",
    "REVENUE_STATUSES",
    "REVIEW_SORTS",
    "STATUS_CHOICES",
    "SUPPORT_SORTS",
    "TYPE_CHOICES",
    "DateRange",
    "audit_events",
    "catalog_health",
    "customer_search",
    "dashboard_summary",
    "drops",
    "funnel_summary",
    "loop_items",
    "low_stock_rows",
    "notification_detail",
    "notification_metrics",
    "notifications",
    "open_support_statuses",
    "order_detail",
    "order_events",
    "order_timeline",
    "orders",
    "paginate",
    "payment_detail",
    "payment_events",
    "payments",
    "pending_loop_count",
    "points_ledger",
    "products",
    "promotion_state",
    "promotions",
    "provider_label",
    "quests",
    "recent_movements",
    "resale_listings",
    "resolve_range",
    "return_detail",
    "returns",
    "review_queue",
    "shipments",
    "sort_queryset",
    "stock_rows",
    "stock_state",
    "support_tickets",
]
