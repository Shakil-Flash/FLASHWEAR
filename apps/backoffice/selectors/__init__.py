"""Read-side selectors for the Back Office (Phase 16).

Where a query happens decides what it may contain:

* list selectors return an ordered, eager-loaded queryset and nothing else -- the view
  pages them, the template renders rows, and no screen ever loads a whole table;
* filter helpers parse *and allowlist* user input, so a query parameter can only ever
  select from a known set of states or sort by a known set of columns;
* the dashboard selectors aggregate with ``values(...).annotate(...)`` so a metric costs
  one query whether the shop has ten orders or ten thousand.
"""

from __future__ import annotations

from apps.backoffice.selectors.audit import audit_events  # noqa: F401
from apps.backoffice.selectors.catalog import (  # noqa: F401
    catalog_health,
    drops,
    low_stock_rows,
    promotions,
    quests,
    stock_rows,
)
from apps.backoffice.selectors.common import (  # noqa: F401
    DateRange,
    RANGES,
    paginate,
    resolve_range,
    sort_queryset,
)
from apps.backoffice.selectors.dashboard import dashboard_summary  # noqa: F401
from apps.backoffice.selectors.queues import (  # noqa: F401
    loop_items,
    resale_listings,
    review_queue,
    support_tickets,
)
from apps.backoffice.selectors.sales import (  # noqa: F401
    customer_search,
    order_detail,
    order_events,
    orders,
    payment_detail,
    payment_events,
    payments,
    points_ledger,
    shipments,
)

__all__ = [
    "DateRange",
    "RANGES",
    "audit_events",
    "catalog_health",
    "customer_search",
    "dashboard_summary",
    "drops",
    "loop_items",
    "low_stock_rows",
    "order_detail",
    "order_events",
    "orders",
    "paginate",
    "payment_detail",
    "payment_events",
    "payments",
    "points_ledger",
    "promotions",
    "quests",
    "resale_listings",
    "resolve_range",
    "review_queue",
    "shipments",
    "sort_queryset",
    "stock_rows",
    "support_tickets",
]
