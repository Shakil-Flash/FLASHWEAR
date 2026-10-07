"""Back Office views (Phase 16).

Three conventions every screen here follows:

* the view declares the capability it needs with ``@backoffice_access(...)`` -- the URL
  space itself answers 404 to anyone without it;
* reads go through :mod:`apps.backoffice.selectors`, writes through
  :mod:`apps.backoffice.services.operations`, and a view never touches an ORM write;
* every list is filtered by allowlisted values and paged, so no screen can be asked to
  load a whole table.
"""

from __future__ import annotations

from apps.backoffice.views.analytics import analytics
from apps.backoffice.views.community import (
    loop,
    loop_action,
    points,
    points_adjust,
    reviews,
    reviews_bulk,
    support,
    support_assign,
    support_status,
)
from apps.backoffice.views.content import (
    content_campaign_delete,
    content_campaign_edit,
    content_campaigns,
    content_editorial,
    content_editorial_delete,
    content_editorial_edit,
    content_homepage,
    content_homepage_delete,
    content_homepage_edit,
    content_homepage_toggle,
    content_preview,
)
from apps.backoffice.views.dashboard import alerts, dashboard, health
from apps.backoffice.views.governance import audit, staff, staff_update
from apps.backoffice.views.merchandising import (
    adjust_stock,
    drops,
    inventory,
    merch_featured_collections,
    merch_featured_products,
    merch_product_add,
    merch_product_remove,
    merch_product_reorder,
    merch_scheduled,
    products,
    products_bulk,
    promotions,
    quests,
)
from apps.backoffice.views.notifications import (
    notification_detail,
    notification_retry,
    notifications,
)
from apps.backoffice.views.sales import (
    customers,
    order_action,
    order_detail,
    order_note,
    orders,
    payment_detail,
    payments,
    return_action,
    return_detail,
    returns,
    shipments,
)

__all__ = [
    "adjust_stock",
    "alerts",
    "analytics",
    "audit",
    "content_campaign_delete",
    "content_campaign_edit",
    "content_campaigns",
    "content_editorial",
    "content_editorial_delete",
    "content_editorial_edit",
    "content_homepage",
    "content_homepage_delete",
    "content_homepage_edit",
    "content_homepage_toggle",
    "content_preview",
    "customers",
    "dashboard",
    "drops",
    "health",
    "inventory",
    "loop",
    "loop_action",
    "merch_featured_collections",
    "merch_featured_products",
    "merch_product_add",
    "merch_product_remove",
    "merch_product_reorder",
    "merch_scheduled",
    "notification_detail",
    "notification_retry",
    "notifications",
    "order_action",
    "order_detail",
    "order_note",
    "orders",
    "payment_detail",
    "payments",
    "points",
    "points_adjust",
    "products",
    "products_bulk",
    "promotions",
    "quests",
    "return_action",
    "return_detail",
    "returns",
    "reviews",
    "reviews_bulk",
    "shipments",
    "staff",
    "staff_update",
    "support",
    "support_assign",
    "support_status",
]
