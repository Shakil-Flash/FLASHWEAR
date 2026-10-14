"""Views for the FLASHWEAR Staff Portal (Phase 57)."""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.backoffice.models import AuditEvent
from apps.staff.constants import (
    DELIVERIES_VIEW,
    INVENTORY_VIEW,
    ORDERS_VIEW,
    PORTAL_ACCESS,
    PRODUCTS_VIEW,
    REPORTS_VIEW,
    ROLE_PRIMARY_GROUP,
    STAFF_MANAGE,
    SUPPORT_VIEW,
    StaffRole,
)
from apps.staff.forms import (
    StaffAccountCreateForm,
    StaffAccountRoleForm,
    StaffLoginForm,
)
from apps.staff.navigation import get_staff_navigation
from apps.staff.permissions import (
    get_user_capabilities,
    get_user_roles,
    has_portal_access,
    has_staff_capability,
    render_access_denied,
    staff_login_required,
    staff_permission_required,
)
from apps.staff.services import (
    User,
    create_staff_account,
    get_all_staff_users,
    get_staff_dashboard_summary,
    toggle_staff_active,
    update_staff_roles,
)

logger = logging.getLogger("flashwear.staff")


def login_view(request: HttpRequest) -> HttpResponse:
    """Dedicated sign-in view for staff portal employees."""
    next_url = request.GET.get("next") or request.POST.get("next") or reverse("staff:dashboard")

    # If already authenticated and has portal access, redirect safely
    if request.user.is_authenticated and request.user.is_active:
        if has_portal_access(request.user):
            if url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                return redirect(next_url)
            return redirect("staff:dashboard")

    form = StaffLoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.cleaned_data["user"]
        if not has_portal_access(user):
            messages.error(
                request,
                _("Signed in as %(email)s, but this account has no staff portal role assigned.")
                % {"email": user.email},
            )
            return render_access_denied(
                request,
                required_capability=PORTAL_ACCESS,
                message=(
                    "Your account does not have staff portal access. "
                    "If you are an employee, contact your administrator."
                ),
            )

        auth_login(request, user)
        if form.cleaned_data.get("remember_me"):
            request.session.set_expiry(60 * 60 * 24 * 30)  # 30 days
        else:
            request.session.set_expiry(0)  # On browser close

        messages.success(
            request,
            _("Welcome back, %(name)s.") % {"name": user.get_full_name() or user.email},
        )
        if url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(next_url)
        return redirect("staff:dashboard")

    return render(
        request,
        "staff/login.html",
        {
            "form": form,
            "next": next_url,
        },
    )


def logout_view(request: HttpRequest) -> HttpResponse:
    """Safely log out staff employee."""
    auth_logout(request)
    messages.info(request, _("You have been signed out of the staff portal."))
    return redirect("staff:login")


def access_denied_view(request: HttpRequest) -> HttpResponse:
    """Public access denied landing page."""
    return render_access_denied(request)


@staff_login_required
def dashboard_view(request: HttpRequest) -> HttpResponse:
    """Staff Portal main landing page."""
    user = request.user
    user_roles = get_user_roles(user)
    role_labels = [dict(StaffRole.choices).get(r, r) for r in user_roles]
    summary = get_staff_dashboard_summary(user)
    nav = get_staff_navigation(user, active_key="dashboard")

    return render(
        request,
        "staff/dashboard.html",
        {
            "nav": nav,
            "active_key": "dashboard",
            "user_roles": user_roles,
            "role_labels": role_labels,
            "summary": summary,
        },
    )


@staff_permission_required(STAFF_MANAGE)
def account_list_view(request: HttpRequest) -> HttpResponse:
    """List staff accounts with role badges, active filters, and search."""
    q = request.GET.get("q", "").strip()
    role_filter = request.GET.get("role", "").strip()
    status_filter = request.GET.get("status", "").strip()

    staff_qs = get_all_staff_users()

    if q:
        staff_qs = staff_qs.filter(
            Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        )

    if status_filter == "active":
        staff_qs = staff_qs.filter(is_active=True)
    elif status_filter == "inactive":
        staff_qs = staff_qs.filter(is_active=False)

    if role_filter:
        grp = ROLE_PRIMARY_GROUP.get(role_filter)
        if grp:
            staff_qs = staff_qs.filter(groups__name=grp)

    paginator = Paginator(staff_qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Attach computed roles to user objects for display
    staff_members = []
    for member in page_obj:
        roles = get_user_roles(member)
        role_labels = [dict(StaffRole.choices).get(r, r) for r in roles]
        staff_members.append(
            {
                "user": member,
                "roles": roles,
                "role_labels": role_labels,
                "is_owner": StaffRole.OWNER in roles,
            }
        )

    nav = get_staff_navigation(request.user, active_key="accounts")
    return render(
        request,
        "staff/accounts/list.html",
        {
            "nav": nav,
            "active_key": "accounts",
            "staff_members": staff_members,
            "page_obj": page_obj,
            "q": q,
            "role_filter": role_filter,
            "status_filter": status_filter,
            "all_roles": StaffRole.choices,
        },
    )


@staff_permission_required(STAFF_MANAGE)
def account_create_view(request: HttpRequest) -> HttpResponse:
    """Create or invite a new staff member."""
    form = StaffAccountCreateForm(request.POST or None, actor=request.user)

    if request.method == "POST" and form.is_valid():
        try:
            created_user, temp_pw = create_staff_account(
                email=form.cleaned_data["email"],
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
                roles=form.cleaned_data["roles"],
                password=form.cleaned_data.get("password") or None,
                actor=request.user,
            )
            messages.success(
                request,
                _(
                    "Staff account for %(email)s created successfully. "
                    + ("Temporary password: %(pw)s" if temp_pw else "")
                )
                % {"email": created_user.email, "pw": temp_pw},
            )
            return redirect("staff:account_detail", pk=created_user.pk)
        except ValidationError as exc:
            form.add_error(None, exc)

    nav = get_staff_navigation(request.user, active_key="accounts")
    return render(
        request,
        "staff/accounts/create.html",
        {
            "nav": nav,
            "active_key": "accounts",
            "form": form,
        },
    )


@staff_permission_required(STAFF_MANAGE)
def account_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Inspect staff member account, effective capabilities, and audit history."""
    target_user = get_object_or_404(User, pk=pk)
    user_roles = get_user_roles(target_user)
    role_labels = [dict(StaffRole.choices).get(r, r) for r in user_roles]
    capabilities = get_user_capabilities(target_user)

    audit_events = AuditEvent.objects.filter(
        Q(object_id=str(pk), object_type="accounts.user") | Q(actor=target_user)
    ).order_by("-created_at")[:20]

    nav = get_staff_navigation(request.user, active_key="accounts")
    return render(
        request,
        "staff/accounts/detail.html",
        {
            "nav": nav,
            "active_key": "accounts",
            "member": target_user,
            "user_roles": user_roles,
            "role_labels": role_labels,
            "capabilities": capabilities,
            "audit_events": audit_events,
            "can_edit_roles": (request.user.pk != target_user.pk or request.user.is_superuser),
            "is_self": (request.user.pk == target_user.pk),
        },
    )


@staff_permission_required(STAFF_MANAGE)
def account_roles_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Assign or revoke roles for a staff member."""
    target_user = get_object_or_404(User, pk=pk)

    # Invariant: staff cannot edit their own roles to prevent privilege self-escalation
    if request.user.pk == target_user.pk and not request.user.is_superuser:
        messages.error(request, _("You cannot modify your own roles or permissions."))
        return redirect("staff:account_detail", pk=pk)

    current_roles = get_user_roles(target_user)
    form = StaffAccountRoleForm(
        request.POST or None,
        actor=request.user,
        target_user=target_user,
        initial={"roles": current_roles},
    )

    if request.method == "POST" and form.is_valid():
        try:
            update_staff_roles(
                user=target_user,
                roles=form.cleaned_data["roles"],
                actor=request.user,
            )
            messages.success(
                request,
                _("Roles updated successfully for %(email)s.") % {"email": target_user.email},
            )
            return redirect("staff:account_detail", pk=pk)
        except ValidationError as exc:
            form.add_error(None, exc)

    nav = get_staff_navigation(request.user, active_key="accounts")
    return render(
        request,
        "staff/accounts/roles.html",
        {
            "nav": nav,
            "active_key": "accounts",
            "member": target_user,
            "form": form,
            "current_roles": current_roles,
        },
    )


@require_POST
@staff_permission_required(STAFF_MANAGE)
def account_toggle_active_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Activate or deactivate staff user account."""
    target_user = get_object_or_404(User, pk=pk)
    new_state = not target_user.is_active

    try:
        toggle_staff_active(user=target_user, is_active=new_state, actor=request.user)
        state_str = _("activated") if new_state else _("deactivated")
        messages.success(
            request,
            _("Account for %(email)s was %(state)s.")
            % {"email": target_user.email, "state": state_str},
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0] if exc.messages else str(exc))

    return redirect("staff:account_detail", pk=pk)


@staff_permission_required(STAFF_MANAGE)
def audit_logs_view(request: HttpRequest) -> HttpResponse:
    """Staff audit trail viewing recent privileged actions."""
    events = AuditEvent.objects.filter(domain="staff").order_by("-created_at")
    paginator = Paginator(events, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    nav = get_staff_navigation(request.user, active_key="audit")
    return render(
        request,
        "staff/audit_logs.html",
        {
            "nav": nav,
            "active_key": "audit",
            "page_obj": page_obj,
        },
    )


# --------------------------------------------------------------------------------------
# Foundation Module Placeholders (Connecting to Existing Data)
# --------------------------------------------------------------------------------------


@staff_permission_required(PRODUCTS_VIEW)
def products_view(request: HttpRequest) -> HttpResponse:
    """Products & Catalog module landing for staff."""
    from apps.catalog.models import Product

    products = (
        Product.objects.select_related("category")
        .prefetch_related("variants")
        .order_by("-updated_at")[:25]
    )
    nav = get_staff_navigation(request.user, active_key="products")
    return render(
        request,
        "staff/module_placeholder.html",
        {
            "nav": nav,
            "active_key": "products",
            "title": "Products & Catalogue",
            "module": "products",
            "items": products,
            "can_manage": has_staff_capability(request.user, "staff.products.manage"),
        },
    )


@staff_permission_required(INVENTORY_VIEW)
def inventory_view(request: HttpRequest) -> HttpResponse:
    """Inventory module landing for staff."""
    from apps.inventory.models import Stock

    stocks = Stock.objects.select_related("variant__product").order_by("on_hand")[:25]
    nav = get_staff_navigation(request.user, active_key="inventory")
    return render(
        request,
        "staff/module_placeholder.html",
        {
            "nav": nav,
            "active_key": "inventory",
            "title": "Inventory & Stock Levels",
            "module": "inventory",
            "items": stocks,
            "can_manage": has_staff_capability(request.user, "staff.inventory.manage"),
        },
    )


@staff_permission_required(ORDERS_VIEW)
def orders_view(request: HttpRequest) -> HttpResponse:
    """Orders module landing for staff."""
    from apps.orders.models import Order

    orders = Order.objects.order_by("-created_at")[:25]
    nav = get_staff_navigation(request.user, active_key="orders")
    return render(
        request,
        "staff/module_placeholder.html",
        {
            "nav": nav,
            "active_key": "orders",
            "title": "Order Processing & Fulfillment",
            "module": "orders",
            "items": orders,
            "can_manage": has_staff_capability(request.user, "staff.orders.manage"),
        },
    )


@staff_permission_required(DELIVERIES_VIEW)
def deliveries_view(request: HttpRequest) -> HttpResponse:
    """Deliveries & Shipments module landing for staff."""
    from apps.orders.models import Shipment

    shipments = Shipment.objects.select_related("order").order_by("-updated_at")[:25]
    nav = get_staff_navigation(request.user, active_key="deliveries")
    return render(
        request,
        "staff/module_placeholder.html",
        {
            "nav": nav,
            "active_key": "deliveries",
            "title": "Delivery & Shipment Tracking",
            "module": "deliveries",
            "items": shipments,
            "can_manage": has_staff_capability(request.user, "staff.deliveries.manage"),
        },
    )


@staff_permission_required(SUPPORT_VIEW)
def support_view(request: HttpRequest) -> HttpResponse:
    """Support Tickets module landing for staff."""
    from apps.support.models import SupportTicket

    tickets = SupportTicket.objects.order_by("-updated_at")[:25]
    nav = get_staff_navigation(request.user, active_key="support")
    return render(
        request,
        "staff/module_placeholder.html",
        {
            "nav": nav,
            "active_key": "support",
            "title": "Customer Support Tickets",
            "module": "support",
            "items": tickets,
            "can_manage": has_staff_capability(request.user, "staff.support.manage"),
        },
    )


@staff_permission_required(REPORTS_VIEW)
def reports_view(request: HttpRequest) -> HttpResponse:
    """Operational Reports module landing for staff."""
    nav = get_staff_navigation(request.user, active_key="reports")
    summary = get_staff_dashboard_summary(request.user)
    return render(
        request,
        "staff/module_placeholder.html",
        {
            "nav": nav,
            "active_key": "reports",
            "title": "Operational Reports & Insights",
            "module": "reports",
            "summary": summary,
            "can_manage": False,
        },
    )
