"""Staff portal domain services and account management logic."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.translation import gettext as _

from apps.backoffice.services import audit as audit_service
from apps.staff.constants import (
    ALL_STAFF_GROUPS,
    DELIVERIES_VIEW,
    INVENTORY_VIEW,
    ORDERS_VIEW,
    PRODUCTS_VIEW,
    ROLE_PRIMARY_GROUP,
    STAFF_MANAGE,
    SUPPORT_VIEW,
    StaffRole,
)
from apps.staff.permissions import (
    get_user_capabilities,
    get_user_roles,
    is_owner_or_superuser,
)

logger = logging.getLogger("flashwear.staff")
User = get_user_model()


def get_all_staff_users():
    """Return queryset of all users holding at least one staff group or superuser flag."""
    return (
        User.objects.filter(
            Q(groups__name__in=ALL_STAFF_GROUPS)
            | Q(groups__name__in=["Administrator", "Back Office Operator", "Support Agent"])
            | Q(is_superuser=True)
            | Q(is_staff=True)
        )
        .distinct()
        .prefetch_related("groups")
        .order_by("email")
    )


def count_active_owners() -> int:
    """Return count of active users with Owner role or superuser status."""
    owner_groups = ["Owner", "Administrator"]
    return (
        User.objects.filter(is_active=True)
        .filter(Q(is_superuser=True) | Q(groups__name__in=owner_groups))
        .distinct()
        .count()
    )


@transaction.atomic
def create_staff_account(
    *,
    email: str,
    first_name: str = "",
    last_name: str = "",
    roles: list[str],
    password: str | None = None,
    send_invite: bool = False,
    actor: Any,
) -> tuple[Any, str]:
    """Create a new staff user with designated roles.

    Returns (user, temp_password).
    """
    email = User.normalize_email_value(email)
    if not email:
        raise ValidationError(_("Email address is required."))

    if User.objects.filter(email=email).exists():
        raise ValidationError(_("A user with this email address already exists."))

    # Privilege validation
    if StaffRole.OWNER in roles and not is_owner_or_superuser(actor):
        raise ValidationError(_("Only an Owner can assign the Owner role."))

    # Generate or set password
    temp_password = ""
    if not password:
        temp_password = get_random_string(length=16)
        user_password = temp_password
    else:
        user_password = password

    # Ordinary staff DO NOT need is_staff=True or is_superuser=True
    is_super = StaffRole.OWNER in roles and getattr(actor, "is_superuser", False)
    user = User(
        email=email,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        is_active=True,
        is_staff=is_super,  # Restrict is_staff to superusers to block /admin/ direct access
        is_superuser=is_super,
    )
    user.set_password(user_password)
    user.save()

    # Assign group memberships
    groups_to_add: set[str] = set()
    for role in roles:
        primary_grp = ROLE_PRIMARY_GROUP.get(role)
        if primary_grp:
            groups_to_add.add(primary_grp)

    if groups_to_add:
        group_objs = [Group.objects.get_or_create(name=name)[0] for name in groups_to_add]
        user.groups.add(*group_objs)

    # Record audit events
    audit_service.record(
        actor=actor,
        domain="staff",
        action="staff.account.create",
        object_type="accounts.user",
        object_id=user.pk,
        object_repr=user.email,
        reason=f"Created staff account for {user.get_full_name() or user.email}",
        metadata={"email": user.email, "roles": roles, "is_staff": user.is_staff},
    )

    audit_service.record(
        actor=actor,
        domain="staff",
        action="staff.role.assign",
        object_type="accounts.user",
        object_id=user.pk,
        object_repr=user.email,
        reason="Initial role assignment upon staff account creation.",
        metadata={"assigned_roles": roles, "groups": list(groups_to_add)},
    )

    logger.info("Staff account created: %s by %s", user.email, getattr(actor, "email", "system"))
    return user, temp_password


@transaction.atomic
def update_staff_roles(
    *,
    user: Any,
    roles: list[str],
    actor: Any,
) -> list[str]:
    """Update assigned roles for a staff user.

    Enforces invariants against self-promotion and removal of last owner.
    """
    if actor and actor.pk == user.pk and not getattr(actor, "is_superuser", False):
        raise ValidationError(_("You cannot modify your own roles or permissions."))

    # Check Owner role assignment permissions
    if StaffRole.OWNER in roles and not is_owner_or_superuser(actor):
        raise ValidationError(_("Only an Owner can assign the Owner role."))

    # Check protection of last active owner
    current_roles = get_user_roles(user)
    if StaffRole.OWNER in current_roles and StaffRole.OWNER not in roles:
        if count_active_owners() <= 1:
            raise ValidationError(_("Cannot revoke the Owner role from the last active Owner."))

    # Resolve target groups
    target_groups: set[str] = set()
    for role in roles:
        grp = ROLE_PRIMARY_GROUP.get(role)
        if grp:
            target_groups.add(grp)

    # All known staff role groups that can be managed here
    managed_groups = set(ALL_STAFF_GROUPS)
    current_group_names = set(user.groups.values_list("name", flat=True))

    to_add = [g for g in target_groups if g not in current_group_names]
    to_remove = [g for g in current_group_names if g in managed_groups and g not in target_groups]

    if to_add:
        group_objs = [Group.objects.get_or_create(name=name)[0] for name in to_add]
        user.groups.add(*group_objs)

    if to_remove:
        remove_objs = Group.objects.filter(name__in=to_remove)
        user.groups.remove(*remove_objs)

    # Invalidate cached group names
    if hasattr(user, "_staff_group_names"):
        delattr(user, "_staff_group_names")

    # Audit records
    if to_add:
        audit_service.record(
            actor=actor,
            domain="staff",
            action="staff.role.assign",
            object_type="accounts.user",
            object_id=user.pk,
            object_repr=user.email,
            reason="Assigned staff roles.",
            metadata={"added_groups": to_add, "new_roles": roles},
        )
    if to_remove:
        audit_service.record(
            actor=actor,
            domain="staff",
            action="staff.role.revoke",
            object_type="accounts.user",
            object_id=user.pk,
            object_repr=user.email,
            reason="Revoked staff roles.",
            metadata={"removed_groups": to_remove, "new_roles": roles},
        )

    logger.info("Updated staff roles for %s: %s", user.email, roles)
    return roles


@transaction.atomic
def toggle_staff_active(
    *,
    user: Any,
    is_active: bool,
    actor: Any,
) -> bool:
    """Activate or deactivate a staff user.

    Enforces invariants against deactivating oneself or the last active owner.
    """
    if actor and actor.pk == user.pk:
        raise ValidationError(_("You cannot deactivate your own account."))

    if not is_active and is_owner_or_superuser(user):
        if count_active_owners() <= 1:
            raise ValidationError(_("Cannot deactivate the final Owner account."))

    user.is_active = is_active
    if not is_active:
        user.deactivated_at = timezone.now()
        # Invalidate active sessions
        Session.objects.filter(session_data__contains=f'"_auth_user_id": "{user.pk}"').delete()
    else:
        user.deactivated_at = None

    user.save(update_fields=["is_active", "deactivated_at", "updated_at"])

    action = "staff.account.activate" if is_active else "staff.account.deactivate"
    audit_service.record(
        actor=actor,
        domain="staff",
        action=action,
        object_type="accounts.user",
        object_id=user.pk,
        object_repr=user.email,
        reason=(
            f"Staff account {'activated' if is_active else 'deactivated'} "
            f"by {getattr(actor, 'email', 'admin')}."
        ),
        metadata={"is_active": is_active},
    )

    logger.info("Staff account active toggled to %s for %s", is_active, user.email)
    return is_active


def get_staff_dashboard_summary(user: Any) -> dict[str, Any]:
    """Gather live operational status cards for modules the staff user is authorized to view."""
    caps = get_user_capabilities(user)
    summary: dict[str, Any] = {}

    # Products & Catalog
    if PRODUCTS_VIEW in caps:
        from apps.catalog.models import Product

        total_products = Product.objects.count()
        published_products = Product.objects.filter(status=Product.Status.ACTIVE).count()
        summary["products"] = {
            "total": total_products,
            "published": published_products,
            "draft": max(0, total_products - published_products),
        }

    # Inventory
    if INVENTORY_VIEW in caps:
        from apps.inventory.models import Stock

        low_stock_threshold = getattr(settings, "BACKOFFICE_LOW_STOCK_THRESHOLD", 5)
        low_stock_count = Stock.objects.filter(on_hand__lte=low_stock_threshold).count()
        out_of_stock_count = Stock.objects.filter(on_hand__lte=0).count()
        summary["inventory"] = {
            "low_stock": low_stock_count,
            "out_of_stock": out_of_stock_count,
        }

    # Orders
    if ORDERS_VIEW in caps:
        from apps.orders.models import Order

        pending_orders = Order.objects.filter(status=Order.Status.PENDING_PAYMENT).count()
        processing_orders = Order.objects.filter(
            status__in=[Order.Status.PAID, Order.Status.PROCESSING]
        ).count()
        summary["orders"] = {
            "pending": pending_orders,
            "processing": processing_orders,
        }

    # Deliveries
    if DELIVERIES_VIEW in caps:
        from apps.orders.models import Shipment

        in_transit = Shipment.objects.filter(
            status__in=[Shipment.Status.IN_TRANSIT, Shipment.Status.OUT_FOR_DELIVERY]
        ).count()
        exceptions = Shipment.objects.filter(
            status__in=[Shipment.Status.ATTEMPT_FAILED, Shipment.Status.EXCEPTION]
        ).count()
        summary["deliveries"] = {
            "in_transit": in_transit,
            "exceptions": exceptions,
        }

    # Support
    if SUPPORT_VIEW in caps:
        from apps.support.models import SupportTicket

        open_tickets = SupportTicket.objects.filter(
            status__in=[SupportTicket.Status.OPEN, SupportTicket.Status.IN_PROGRESS]
        ).count()
        summary["support"] = {
            "open": open_tickets,
        }

    # Staff Accounts
    if STAFF_MANAGE in caps:
        active_staff = get_all_staff_users().filter(is_active=True).count()
        summary["staff"] = {
            "active_count": active_staff,
        }

    return summary


def get_eligible_delivery_staff():
    """Return active staff users eligible for delivery assignment."""
    group_name = ROLE_PRIMARY_GROUP[StaffRole.DELIVERY_STAFF]
    return (
        User.objects.filter(is_active=True)
        .filter(
            Q(groups__name=group_name)
            | Q(groups__name="Delivery Staff")
            | Q(is_superuser=True)
        )
        .distinct()
        .order_by("first_name", "last_name", "email")
    )
