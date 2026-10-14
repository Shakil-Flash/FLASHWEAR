"""Constants and role-to-capability matrices for FLASHWEAR Staff Portal (Phase 57)."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

# --------------------------------------------------------------------------------------
# Canonical Staff Roles
# --------------------------------------------------------------------------------------


class StaffRole(models.TextChoices):
    OWNER = "owner", _("Owner / Superuser")
    OPERATIONS_MANAGER = "operations_manager", _("Operations Manager")
    PRODUCT_MANAGER = "product_manager", _("Product Manager")
    INVENTORY_STAFF = "inventory_staff", _("Inventory Staff")
    ORDER_STAFF = "order_staff", _("Order Processing Staff")
    DELIVERY_STAFF = "delivery_staff", _("Delivery Staff")
    SUPPORT_STAFF = "support_staff", _("Support Staff")
    READ_ONLY = "read_only", _("Read-Only Staff")


# Primary Django Group names for each role
ROLE_PRIMARY_GROUP: dict[str, str] = {
    StaffRole.OWNER: "Owner",
    StaffRole.OPERATIONS_MANAGER: "Operations Manager",
    StaffRole.PRODUCT_MANAGER: "Product Manager",
    StaffRole.INVENTORY_STAFF: "Inventory Staff",
    StaffRole.ORDER_STAFF: "Order Processing Staff",
    StaffRole.DELIVERY_STAFF: "Delivery Staff",
    StaffRole.SUPPORT_STAFF: "Support Staff",
    StaffRole.READ_ONLY: "Read-Only Staff",
}

# Group aliases matching existing backoffice / support groups to guarantee backwards compatibility
ROLE_GROUPS: dict[str, frozenset[str]] = {
    StaffRole.OWNER: frozenset({"Owner", "Administrator"}),
    StaffRole.OPERATIONS_MANAGER: frozenset({"Operations Manager", "Back Office Operator"}),
    StaffRole.PRODUCT_MANAGER: frozenset({"Product Manager", "Catalog Manager"}),
    StaffRole.INVENTORY_STAFF: frozenset({"Inventory Staff", "Inventory Manager"}),
    StaffRole.ORDER_STAFF: frozenset({"Order Processing Staff", "Order Manager"}),
    StaffRole.DELIVERY_STAFF: frozenset({"Delivery Staff"}),
    StaffRole.SUPPORT_STAFF: frozenset({"Support Staff", "Support Agent", "Support Manager"}),
    StaffRole.READ_ONLY: frozenset({"Read-Only Staff"}),
}

# Inverted mapping: Group Name -> Canonical StaffRole
GROUP_TO_ROLE: dict[str, str] = {}
for role, groups in ROLE_GROUPS.items():
    for grp in groups:
        GROUP_TO_ROLE[grp] = role

ALL_STAFF_GROUPS: tuple[str, ...] = tuple(ROLE_PRIMARY_GROUP.values())

# --------------------------------------------------------------------------------------
# Granular Staff Capabilities
# --------------------------------------------------------------------------------------

PORTAL_ACCESS = "staff.portal_access"
PRODUCTS_VIEW = "staff.products.view"
PRODUCTS_MANAGE = "staff.products.manage"
ORDERS_VIEW = "staff.orders.view"
ORDERS_MANAGE = "staff.orders.manage"
INVENTORY_VIEW = "staff.inventory.view"
INVENTORY_MANAGE = "staff.inventory.manage"
DELIVERIES_VIEW = "staff.deliveries.view"
DELIVERIES_MANAGE = "staff.deliveries.manage"
SUPPORT_VIEW = "staff.support.view"
SUPPORT_MANAGE = "staff.support.manage"
REPORTS_VIEW = "staff.reports.view"
STAFF_MANAGE = "staff.accounts.manage"
TASKS_VIEW = "staff.tasks.view"
TASKS_MANAGE = "staff.tasks.manage"
NOTIFICATIONS_VIEW = "staff.notifications.view"

ALL_CAPABILITIES: tuple[str, ...] = (
    PORTAL_ACCESS,
    PRODUCTS_VIEW,
    PRODUCTS_MANAGE,
    ORDERS_VIEW,
    ORDERS_MANAGE,
    INVENTORY_VIEW,
    INVENTORY_MANAGE,
    DELIVERIES_VIEW,
    DELIVERIES_MANAGE,
    SUPPORT_VIEW,
    SUPPORT_MANAGE,
    REPORTS_VIEW,
    STAFF_MANAGE,
    TASKS_VIEW,
    TASKS_MANAGE,
    NOTIFICATIONS_VIEW,
)

# --------------------------------------------------------------------------------------
# Role to Capabilities Matrix
# --------------------------------------------------------------------------------------

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    StaffRole.OWNER: frozenset(ALL_CAPABILITIES),
    StaffRole.OPERATIONS_MANAGER: frozenset(
        {
            PORTAL_ACCESS,
            PRODUCTS_VIEW,
            PRODUCTS_MANAGE,
            ORDERS_VIEW,
            ORDERS_MANAGE,
            INVENTORY_VIEW,
            INVENTORY_MANAGE,
            DELIVERIES_VIEW,
            DELIVERIES_MANAGE,
            SUPPORT_VIEW,
            SUPPORT_MANAGE,
            REPORTS_VIEW,
            STAFF_MANAGE,
            TASKS_VIEW,
            TASKS_MANAGE,
            NOTIFICATIONS_VIEW,
        }
    ),
    StaffRole.PRODUCT_MANAGER: frozenset(
        {
            PORTAL_ACCESS,
            PRODUCTS_VIEW,
            PRODUCTS_MANAGE,
            INVENTORY_VIEW,
            REPORTS_VIEW,
            TASKS_VIEW,
            TASKS_MANAGE,
            NOTIFICATIONS_VIEW,
        }
    ),
    StaffRole.INVENTORY_STAFF: frozenset(
        {
            PORTAL_ACCESS,
            INVENTORY_VIEW,
            INVENTORY_MANAGE,
            PRODUCTS_VIEW,
            DELIVERIES_VIEW,
            TASKS_VIEW,
            TASKS_MANAGE,
            NOTIFICATIONS_VIEW,
        }
    ),
    StaffRole.ORDER_STAFF: frozenset(
        {
            PORTAL_ACCESS,
            ORDERS_VIEW,
            ORDERS_MANAGE,
            INVENTORY_VIEW,
            DELIVERIES_VIEW,
            TASKS_VIEW,
            TASKS_MANAGE,
            NOTIFICATIONS_VIEW,
        }
    ),
    StaffRole.DELIVERY_STAFF: frozenset(
        {
            PORTAL_ACCESS,
            DELIVERIES_VIEW,
            DELIVERIES_MANAGE,
            ORDERS_VIEW,
            TASKS_VIEW,
            NOTIFICATIONS_VIEW,
        }
    ),
    StaffRole.SUPPORT_STAFF: frozenset(
        {
            PORTAL_ACCESS,
            SUPPORT_VIEW,
            SUPPORT_MANAGE,
            ORDERS_VIEW,
            DELIVERIES_VIEW,
            TASKS_VIEW,
            TASKS_MANAGE,
            NOTIFICATIONS_VIEW,
        }
    ),
    StaffRole.READ_ONLY: frozenset(
        {
            PORTAL_ACCESS,
            PRODUCTS_VIEW,
            ORDERS_VIEW,
            INVENTORY_VIEW,
            DELIVERIES_VIEW,
            SUPPORT_VIEW,
            REPORTS_VIEW,
            TASKS_VIEW,
            NOTIFICATIONS_VIEW,
        }
    ),
}

# Inverted mapping: Capability -> Roles that carry it
CAPABILITY_ROLES: dict[str, set[str]] = {cap: set() for cap in ALL_CAPABILITIES}
for role_name, caps in ROLE_CAPABILITIES.items():
    for cap in caps:
        CAPABILITY_ROLES[cap].add(role_name)
