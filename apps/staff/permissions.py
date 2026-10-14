"""Role-based access control and permission helpers for the FLASHWEAR Staff Portal."""

from __future__ import annotations

from functools import wraps
from typing import Any

from django.contrib.auth.models import Group
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.staff.constants import (
    ALL_CAPABILITIES,
    ALL_STAFF_GROUPS,
    GROUP_TO_ROLE,
    PORTAL_ACCESS,
    ROLE_CAPABILITIES,
    STAFF_MANAGE,
    StaffRole,
)


def ensure_staff_groups() -> list[str]:
    """Idempotently create all standard staff groups. Returns names of newly created groups."""
    created: list[str] = []
    for name in ALL_STAFF_GROUPS:
        _, was_created = Group.objects.get_or_create(name=name)
        if was_created:
            created.append(name)
    return created


def get_user_group_names(user: Any) -> frozenset[str]:
    """Retrieve group names for the user, cached on the instance for the request lifetime."""
    if user is None or not getattr(user, "is_authenticated", False):
        return frozenset()
    cached = getattr(user, "_staff_group_names", None)
    if cached is None:
        cached = frozenset(user.groups.values_list("name", flat=True))
        try:
            user._staff_group_names = cached
        except AttributeError:
            pass
    return cached


def get_user_roles(user: Any) -> list[str]:
    """Return a list of canonical StaffRole string identifiers held by the user."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return []

    roles: set[str] = set()
    if user.is_superuser:
        roles.add(StaffRole.OWNER)

    group_names = get_user_group_names(user)
    for grp in group_names:
        role = GROUP_TO_ROLE.get(grp)
        if role:
            roles.add(role)

    # Order by StaffRole definition order
    order = [c.value for c in StaffRole]
    return [r for r in order if r in roles]


def get_user_capabilities(user: Any) -> frozenset[str]:
    """Return the set of all effective granular capabilities held by the user."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return frozenset()

    if user.is_superuser:
        return frozenset(ALL_CAPABILITIES)

    roles = get_user_roles(user)
    caps: set[str] = set()
    for role in roles:
        caps.update(ROLE_CAPABILITIES.get(role, frozenset()))

    return frozenset(caps)


def has_staff_capability(user: Any, capability: str) -> bool:
    """Check if the user holds a specific staff capability."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False

    if user.is_superuser:
        return True

    return capability in get_user_capabilities(user)


def has_portal_access(user: Any) -> bool:
    """Check if the user is authorized to enter the staff portal at all."""
    return has_staff_capability(user, PORTAL_ACCESS)


def is_owner_or_superuser(user: Any) -> bool:
    """Check if the user is an Owner or superuser."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return user.is_superuser or (StaffRole.OWNER in get_user_roles(user))


def can_manage_staff(user: Any) -> bool:
    """Check if the user has authorization to manage staff accounts and roles."""
    return has_staff_capability(user, STAFF_MANAGE)


def render_access_denied(
    request: HttpRequest,
    required_capability: str | None = None,
    message: str | None = None,
) -> HttpResponse:
    """Render a clean, secure access-denied page with HTTP 403 Forbidden."""
    user_roles = get_user_roles(request.user) if request.user.is_authenticated else []
    return render(
        request,
        "staff/access_denied.html",
        {
            "required_capability": required_capability,
            "message": message,
            "user_roles": user_roles,
        },
        status=403,
    )


def staff_login_required(view_func):
    """Decorator ensuring user is authenticated and has staff portal access."""

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not request.user.is_authenticated:
            login_url = reverse("staff:login")
            return redirect(f"{login_url}?next={request.get_full_path()}")

        if not request.user.is_active:
            login_url = reverse("staff:login")
            return redirect(f"{login_url}?error=deactivated")

        if not has_portal_access(request.user):
            return render_access_denied(
                request,
                required_capability=PORTAL_ACCESS,
                message=(
                    "Your account does not have staff portal access. "
                    "If you are an employee, contact your Operations Manager."
                ),
            )

        return view_func(request, *args, **kwargs)

    return wrapper


def staff_permission_required(capability: str):
    """Decorator enforcing a specific granular capability."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request: HttpRequest, *args, **kwargs):
            if not request.user.is_authenticated:
                login_url = reverse("staff:login")
                return redirect(f"{login_url}?next={request.get_full_path()}")

            if not request.user.is_active:
                login_url = reverse("staff:login")
                return redirect(f"{login_url}?error=deactivated")

            if not has_portal_access(request.user):
                return render_access_denied(
                    request,
                    required_capability=PORTAL_ACCESS,
                    message="Your account does not have staff portal access.",
                )

            if not has_staff_capability(request.user, capability):
                return render_access_denied(
                    request,
                    required_capability=capability,
                    message=f"Access to this module requires the '{capability}' permission.",
                )

            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
