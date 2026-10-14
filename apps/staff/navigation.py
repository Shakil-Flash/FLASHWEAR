"""Permission-aware navigation definitions and breadcrumbs for FLASHWEAR Staff Portal."""

from __future__ import annotations

from typing import Any

from django.urls import reverse

from apps.staff.constants import (
    DELIVERIES_VIEW,
    INVENTORY_VIEW,
    NOTIFICATIONS_VIEW,
    ORDERS_VIEW,
    PORTAL_ACCESS,
    PRODUCTS_VIEW,
    REPORTS_VIEW,
    STAFF_MANAGE,
    SUPPORT_VIEW,
    TASKS_VIEW,
)
from apps.staff.permissions import get_user_capabilities

# Section -> Items: (key, label, url_name, required_capability, icon_svg)
STAFF_NAVIGATION_SCHEMA: tuple[tuple[str, tuple[dict[str, Any], ...]], ...] = (
    (
        "Workspace",
        (
            {
                "key": "dashboard",
                "label": "Dashboard",
                "url_name": "staff:dashboard",
                "capability": PORTAL_ACCESS,
                "icon": "home",
            },
            {
                "key": "tasks",
                "label": "Staff Tasks",
                "url_name": "staff:task_list",
                "capability": TASKS_VIEW,
                "icon": "clipboard-list",
            },
            {
                "key": "notifications",
                "label": "Notifications",
                "url_name": "staff:notification_list",
                "capability": NOTIFICATIONS_VIEW,
                "icon": "bell",
            },
        ),
    ),
    (
        "Catalog & Stock",
        (
            {
                "key": "products",
                "label": "Products",
                "url_name": "staff:products",
                "capability": PRODUCTS_VIEW,
                "icon": "cube",
            },
            {
                "key": "inventory",
                "label": "Inventory",
                "url_name": "staff:inventory",
                "capability": INVENTORY_VIEW,
                "icon": "archive",
            },
        ),
    ),
    (
        "Fulfillment & Shipping",
        (
            {
                "key": "orders",
                "label": "Orders",
                "url_name": "staff:orders",
                "capability": ORDERS_VIEW,
                "icon": "shopping-bag",
            },
            {
                "key": "deliveries",
                "label": "Deliveries",
                "url_name": "staff:deliveries",
                "capability": DELIVERIES_VIEW,
                "icon": "truck",
            },
        ),
    ),
    (
        "Care & Analytics",
        (
            {
                "key": "support",
                "label": "Support Tickets",
                "url_name": "staff:support",
                "capability": SUPPORT_VIEW,
                "icon": "chat-bubble",
            },
            {
                "key": "reports",
                "label": "Reports",
                "url_name": "staff:reports",
                "capability": REPORTS_VIEW,
                "icon": "chart-bar",
            },
        ),
    ),
    (
        "Administration",
        (
            {
                "key": "accounts",
                "label": "Staff Accounts",
                "url_name": "staff:account_list",
                "capability": STAFF_MANAGE,
                "icon": "users",
            },
            {
                "key": "audit",
                "label": "Staff Audit Trail",
                "url_name": "staff:audit_logs",
                "capability": STAFF_MANAGE,
                "icon": "shield-check",
            },
        ),
    ),
)


def get_staff_navigation(user: Any, active_key: str = "") -> list[dict]:
    """Return filtered navigation tree containing only items authorized for `user`."""
    if user is None or not getattr(user, "is_authenticated", False):
        return []

    caps = get_user_capabilities(user)
    tree: list[dict] = []

    for section_label, items in STAFF_NAVIGATION_SCHEMA:
        visible_items = []
        for item in items:
            if item["capability"] in caps:
                try:
                    url = reverse(item["url_name"])
                except Exception:
                    url = "#"
                visible_items.append(
                    {
                        "key": item["key"],
                        "label": item["label"],
                        "url": url,
                        "url_name": item["url_name"],
                        "icon": item["icon"],
                        "is_active": (active_key == item["key"]),
                    }
                )
        if visible_items:
            tree.append(
                {
                    "label": section_label,
                    "items": visible_items,
                }
            )

    return tree
