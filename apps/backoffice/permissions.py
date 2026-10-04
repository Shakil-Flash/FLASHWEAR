"""Staff capabilities for the FLASHWEAR Back Office (Phase 16).

Access is a **capability**, and a capability is a group plus ``is_staff`` -- the same
contract :mod:`apps.support.permissions` established, generalised to the operations
domains. There is no parallel authentication system: Django's user, staff flag, groups
and superuser contract are all this module reads.

Three rules the whole back office leans on:

1. **``is_staff`` alone holds nothing.** An operator who may browse ``/admin/`` cannot
   open ``/operations/`` until they are in a back-office group, and a customer who is in
   a group cannot either (no staff flag, no access).
2. **A superuser implies every capability** -- Django's own contract, so the admin and
   the back office never disagree about who is in charge.
3. **Missing access answers 404, not 403.** A route that says "403, forbidden" has told
   a scanner exactly where the operations platform lives; a route that does not exist
   tells it nothing. A failure raised *by a domain service* (an illegal transition, an
   insufficient balance) still surfaces as its own message -- this is only about the URL
   space.

Least privilege lives in one table, :data:`CAPABILITY_GROUPS`: every capability lists the
groups that carry it, so "what may an Order Manager see?" is a lookup rather than the
accumulated ``if`` statements of twenty views.
"""

from __future__ import annotations

from functools import wraps

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.http import Http404

from apps.support.permissions import AGENT_GROUP as SUPPORT_AGENT_GROUP
from apps.support.permissions import MANAGER_GROUP as SUPPORT_MANAGER_GROUP

__all__ = [
    "ADMIN_GROUP",
    "AUDIT_VIEW",
    "BACKOFFICE_GROUPS",
    "CAPABILITIES",
    "CAPABILITY_GROUPS",
    "CATALOG_GROUP",
    "CATALOG_MANAGE",
    "CATALOG_VIEW",
    "CUSTOMERS_VIEW",
    "FINANCE_GROUP",
    "INVENTORY_GROUP",
    "INVENTORY_MANAGE",
    "INVENTORY_VIEW",
    "LOYALTY_MANAGE",
    "LOYALTY_VIEW",
    "MANAGER_GROUPS",
    "MARKETING_GROUP",
    "MARKETING_MANAGE",
    "MARKETING_VIEW",
    "MODERATION_MANAGE",
    "MODERATION_VIEW",
    "MODERATOR_GROUP",
    "NAVIGATION",
    "OPERATOR_GROUP",
    "OPS_VIEW",
    "ORDERS_GROUP",
    "ORDERS_MANAGE",
    "ORDERS_VIEW",
    "PAYMENTS_VIEW",
    "STAFF_MANAGE",
    "SUPPORT_MANAGE",
    "SUPPORT_VIEW",
    "backoffice_access",
    "capabilities_for",
    "ensure_backoffice_groups",
    "group_names_for",
    "has",
    "navigation_for",
    "staff_members",
]

# --------------------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------------------

OPS_VIEW = "ops.view"  # enter /operations/ at all
ORDERS_VIEW = "orders.view"
ORDERS_MANAGE = "orders.manage"
CUSTOMERS_VIEW = "customers.view"
INVENTORY_VIEW = "inventory.view"
INVENTORY_MANAGE = "inventory.manage"
PAYMENTS_VIEW = "payments.view"
CATALOG_VIEW = "catalog.view"
CATALOG_MANAGE = "catalog.manage"
MARKETING_VIEW = "marketing.view"
MARKETING_MANAGE = "marketing.manage"
MODERATION_VIEW = "moderation.view"
MODERATION_MANAGE = "moderation.manage"
LOYALTY_VIEW = "loyalty.view"
LOYALTY_MANAGE = "loyalty.manage"
SUPPORT_VIEW = "support.view"
SUPPORT_MANAGE = "support.manage"
AUDIT_VIEW = "audit.view"
STAFF_MANAGE = "staff.manage"

CAPABILITIES = (
    OPS_VIEW,
    ORDERS_VIEW,
    ORDERS_MANAGE,
    CUSTOMERS_VIEW,
    INVENTORY_VIEW,
    INVENTORY_MANAGE,
    PAYMENTS_VIEW,
    CATALOG_VIEW,
    CATALOG_MANAGE,
    MARKETING_VIEW,
    MARKETING_MANAGE,
    MODERATION_VIEW,
    MODERATION_MANAGE,
    LOYALTY_VIEW,
    LOYALTY_MANAGE,
    SUPPORT_VIEW,
    SUPPORT_MANAGE,
    AUDIT_VIEW,
    STAFF_MANAGE,
)

# --------------------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------------------

OPERATOR_GROUP = "Back Office Operator"
ORDERS_GROUP = "Order Manager"
INVENTORY_GROUP = "Inventory Manager"
FINANCE_GROUP = "Finance"
MARKETING_GROUP = "Marketing Manager"
MODERATOR_GROUP = "Content Moderator"
CATALOG_GROUP = "Catalog Manager"
ADMIN_GROUP = "Administrator"

#: Every group this app creates. Support's two are seeded by *their* migration and are
#: deliberately not re-created here -- one owner per row.
BACKOFFICE_GROUPS = (
    OPERATOR_GROUP,
    ORDERS_GROUP,
    INVENTORY_GROUP,
    FINANCE_GROUP,
    MARKETING_GROUP,
    MODERATOR_GROUP,
    CATALOG_GROUP,
    ADMIN_GROUP,
)

#: Group holders are managers rather than doers: they may read the audit log.
MANAGER_GROUPS = frozenset(
    {
        ADMIN_GROUP,
        ORDERS_GROUP,
        INVENTORY_GROUP,
        FINANCE_GROUP,
        MARKETING_GROUP,
        MODERATOR_GROUP,
        CATALOG_GROUP,
    }
)

_ALL_GROUPS = frozenset(BACKOFFICE_GROUPS) | {SUPPORT_AGENT_GROUP, SUPPORT_MANAGER_GROUP}

_SUPPORT_GROUPS = frozenset({SUPPORT_AGENT_GROUP, SUPPORT_MANAGER_GROUP})

#: capability -> the groups that carry it. The single source of truth for the permission
#: matrix in the documentation, the navigation, the dashboard tiles and every access check.
CAPABILITY_GROUPS: dict[str, frozenset[str]] = {
    OPS_VIEW: _ALL_GROUPS,
    ORDERS_VIEW: frozenset(
        {
            OPERATOR_GROUP,
            ORDERS_GROUP,
            FINANCE_GROUP,
            ADMIN_GROUP,
            SUPPORT_AGENT_GROUP,
            SUPPORT_MANAGER_GROUP,
        }
    ),
    ORDERS_MANAGE: frozenset({ORDERS_GROUP, ADMIN_GROUP}),
    CUSTOMERS_VIEW: frozenset(
        {
            OPERATOR_GROUP,
            ORDERS_GROUP,
            FINANCE_GROUP,
            ADMIN_GROUP,
            SUPPORT_AGENT_GROUP,
            SUPPORT_MANAGER_GROUP,
        }
    ),
    INVENTORY_VIEW: frozenset({OPERATOR_GROUP, INVENTORY_GROUP, CATALOG_GROUP, ADMIN_GROUP}),
    INVENTORY_MANAGE: frozenset({INVENTORY_GROUP, ADMIN_GROUP}),
    PAYMENTS_VIEW: frozenset({FINANCE_GROUP, ORDERS_GROUP, ADMIN_GROUP}),
    CATALOG_VIEW: frozenset({OPERATOR_GROUP, CATALOG_GROUP, MARKETING_GROUP, ADMIN_GROUP}),
    CATALOG_MANAGE: frozenset({CATALOG_GROUP, ADMIN_GROUP}),
    MARKETING_VIEW: frozenset({MARKETING_GROUP, ADMIN_GROUP}),
    MARKETING_MANAGE: frozenset({MARKETING_GROUP, ADMIN_GROUP}),
    MODERATION_VIEW: frozenset({MODERATOR_GROUP, ADMIN_GROUP}),
    MODERATION_MANAGE: frozenset({MODERATOR_GROUP, ADMIN_GROUP}),
    LOYALTY_VIEW: frozenset({FINANCE_GROUP, ADMIN_GROUP}),
    LOYALTY_MANAGE: frozenset({FINANCE_GROUP, ADMIN_GROUP}),
    SUPPORT_VIEW: _SUPPORT_GROUPS | {ADMIN_GROUP},
    SUPPORT_MANAGE: _SUPPORT_GROUPS | {ADMIN_GROUP},
    AUDIT_VIEW: MANAGER_GROUPS,
    STAFF_MANAGE: frozenset({ADMIN_GROUP}),
}


def group_names_for(user) -> frozenset[str]:
    """The group names this user belongs to (empty for anonymous/missing users)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return frozenset()
    return frozenset(user.groups.values_list("name", flat=True))


def has(user, capability: str) -> bool:
    """Does ``user`` hold ``capability``? Unknown capabilities raise -- a typo in a
    permission check must never degrade to "no access" quietly in a test that passes."""
    if capability not in CAPABILITY_GROUPS:
        raise KeyError(f"Unknown back office capability: {capability!r}")
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not user.is_active:
        return False
    if user.is_superuser:
        return True
    if not user.is_staff:
        return False
    return bool(CAPABILITY_GROUPS[capability] & group_names_for(user))


def capabilities_for(user) -> frozenset[str]:
    """Every capability this user holds -- what the navigation and dashboard may show."""
    if user is None or not getattr(user, "is_authenticated", False):
        return frozenset()
    if not user.is_active:
        return frozenset()
    if user.is_superuser:
        return frozenset(CAPABILITIES)
    if not user.is_staff:
        return frozenset()
    names = group_names_for(user)
    return frozenset(cap for cap, groups in CAPABILITY_GROUPS.items() if groups & names)


def backoffice_access(capability: str | None = None):
    """Gate a view behind ``OPS_VIEW`` (and ``capability``, when given).

    404 rather than 403 on the way in: the URL space of an internal tool is not public
    knowledge. Applied *inside* ``login_required`` so an anonymous caller is still sent
    to the login page like every other private surface.
    """

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not has(user, OPS_VIEW):
                raise Http404
            if capability is not None and not has(user, capability):
                raise Http404
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def ensure_backoffice_groups() -> list[str]:
    """Create every back-office group (idempotent). Returns the names that were missing.

    Called by migration ``0001`` and by the tests, so a fresh database behaves the same
    as a production one. Like support's groups these carry no Django model permissions:
    there is exactly one answer to "may this user open this screen?"
    """
    created: list[str] = []
    for name in BACKOFFICE_GROUPS:
        _, was_created = Group.objects.get_or_create(name=name)
        if was_created:
            created.append(name)
    return created


def staff_members() -> list:
    """Active staff accounts, ordered for display (the staff & roles screen)."""
    return list(
        get_user_model()
        .objects.filter(is_active=True, is_staff=True)
        .prefetch_related("groups")
        .order_by(get_user_model().USERNAME_FIELD)
    )


# --------------------------------------------------------------------------------------
# Navigation: one definition, consumed by the sidebar and the dashboard tiles, so a
# section a user may not open is never offered to them. ``(key, label, url, capability)``.
# --------------------------------------------------------------------------------------

NAVIGATION = (
    (
        "Overview",
        (
            ("dashboard", "Dashboard", "backoffice:dashboard", OPS_VIEW),
            ("alerts", "Alerts", "backoffice:alerts", OPS_VIEW),
        ),
    ),
    (
        "Sales",
        (
            ("orders", "Orders", "backoffice:orders", ORDERS_VIEW),
            ("payments", "Payments", "backoffice:payments", PAYMENTS_VIEW),
            ("shipments", "Shipments", "backoffice:shipments", ORDERS_VIEW),
            ("customers", "Customers", "backoffice:customers", CUSTOMERS_VIEW),
        ),
    ),
    (
        "Merchandising",
        (
            ("products", "Products", "backoffice:products", CATALOG_VIEW),
            ("inventory", "Inventory", "backoffice:inventory", INVENTORY_VIEW),
            ("drops", "Drops", "backoffice:drops", MARKETING_VIEW),
            ("promotions", "Promotions", "backoffice:promotions", MARKETING_VIEW),
            ("quests", "Quests", "backoffice:quests", MARKETING_VIEW),
        ),
    ),
    (
        "Community",
        (
            ("reviews", "Reviews", "backoffice:reviews", MODERATION_VIEW),
            ("loop", "FLASH Loop", "backoffice:loop", MODERATION_VIEW),
            ("support", "Support", "backoffice:support", SUPPORT_VIEW),
            ("points", "FLASH Points", "backoffice:points", LOYALTY_VIEW),
        ),
    ),
    (
        "Governance",
        (
            ("audit", "Audit log", "backoffice:audit", AUDIT_VIEW),
            ("staff", "Staff & roles", "backoffice:staff", STAFF_MANAGE),
        ),
    ),
)


def navigation_for(user) -> list[dict]:
    """The sidebar this user is entitled to: ``[{"label", "items": [{key, ...}]}]``."""
    caps = capabilities_for(user)
    groups: list[dict] = []
    for label, items in NAVIGATION:
        visible = [
            {"key": key, "label": text, "url_name": url}
            for key, text, url, capability in items
            if capability in caps
        ]
        if visible:
            groups.append({"label": label, "items": visible})
    return groups
