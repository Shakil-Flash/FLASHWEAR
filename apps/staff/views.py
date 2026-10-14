"""Views for the FLASHWEAR Staff Portal (Phase 57)."""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import F, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.backoffice.models import AuditEvent
from apps.staff.constants import (
    DELIVERIES_MANAGE,
    DELIVERIES_VIEW,
    INVENTORY_VIEW,
    ORDERS_MANAGE,
    ORDERS_VIEW,
    PORTAL_ACCESS,
    PRODUCTS_VIEW,
    REPORTS_VIEW,
    ROLE_PRIMARY_GROUP,
    STAFF_MANAGE,
    SUPPORT_VIEW,
    StaffRole,
)
from apps.staff.dashboard_services import (
    get_daily_operations_summary,
    get_my_work_and_team_queues,
    get_quick_actions,
    get_recent_activity_feed,
)
from apps.staff.delivery_services import (
    assign_shipment_action,
    generate_delivery_otp_action,
    get_filtered_shipments,
    get_shipment_details,
    is_delivery_manager,
    record_delivery_attempt_action,
    record_proof_of_delivery_action,
    update_delivery_status_action,
)
from apps.staff.forms import (
    DeliveryAttemptForm,
    OrderDispatchForm,
    OrderPackForm,
    ProofOfDeliveryForm,
    ShipmentAssignmentForm,
    ShipmentStatusUpdateForm,
    StaffAccountCreateForm,
    StaffAccountRoleForm,
    StaffLoginForm,
)
from apps.staff.navigation import get_staff_navigation
from apps.staff.order_services import (
    dispatch_order_action,
    get_filtered_orders,
    get_order_details,
    mark_packed_action,
    process_order_action,
    start_picking_action,
)
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
    """Staff Portal main daily operations dashboard."""
    user = request.user
    user_roles = get_user_roles(user)
    role_labels = [dict(StaffRole.choices).get(r, r) for r in user_roles]
    range_key = request.GET.get("range", "today").strip().lower()
    if range_key not in ["today", "7d", "30d", "all"]:
        range_key = "today"

    summary = get_daily_operations_summary(user, range_key=range_key)
    queues = get_my_work_and_team_queues(user)
    recent_activity = get_recent_activity_feed(user, limit=10)
    quick_actions = get_quick_actions(user)
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
            "my_work": queues["my_work"],
            "team_queues": queues["team_queues"],
            "urgent_alerts": queues["urgent_alerts"],
            "recent_activity": recent_activity,
            "quick_actions": quick_actions,
            "active_range": range_key,
            "current_time": timezone.now(),
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
    """Products & Catalog module landing for staff with status filter support."""
    from apps.catalog.models import Product

    status_filter = request.GET.get("status", "").strip().lower()
    products_qs = Product.objects.select_related("category").prefetch_related("variants")

    if status_filter == "draft":
        products_qs = products_qs.filter(status=Product.Status.DRAFT)
    elif status_filter == "active":
        products_qs = products_qs.filter(status=Product.Status.ACTIVE)

    products = products_qs.order_by("-updated_at")[:25]
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
            "filter_status": status_filter,
            "can_manage": has_staff_capability(request.user, "staff.products.manage"),
        },
    )


@staff_permission_required(INVENTORY_VIEW)
def inventory_view(request: HttpRequest) -> HttpResponse:
    """Inventory module landing for staff with low/out of stock filter support."""
    from apps.inventory.models import Stock

    status_filter = request.GET.get("status", "").strip().lower()
    threshold = getattr(settings, "BACKOFFICE_LOW_STOCK_THRESHOLD", 5)
    stocks_qs = Stock.objects.select_related("variant__product")

    if status_filter == "out_of_stock":
        stocks_qs = stocks_qs.filter(on_hand__lte=0)
    elif status_filter == "low_stock":
        stocks_qs = stocks_qs.filter(
            Q(on_hand__gt=0, on_hand__lte=F("low_stock_threshold"))
            | (Q(low_stock_threshold__isnull=True) & Q(on_hand__gt=0, on_hand__lte=threshold))
        )

    stocks = stocks_qs.order_by("on_hand")[:25]
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
            "filter_status": status_filter,
            "can_manage": has_staff_capability(request.user, "staff.inventory.manage"),
        },
    )


@staff_permission_required(ORDERS_VIEW)
def orders_view(request: HttpRequest) -> HttpResponse:
    """Orders workspace for staff with searchable queues, pagination, and returns filter support."""
    from apps.orders.models import ReturnRequest

    view_type = request.GET.get("type", "").strip().lower()
    status_filter = request.GET.get("status", "").strip().lower()

    if view_type == "returns":
        returns = ReturnRequest.objects.select_related("order").order_by("-created_at")[:25]
        nav = get_staff_navigation(request.user, active_key="orders")
        return render(
            request,
            "staff/module_placeholder.html",
            {
                "nav": nav,
                "active_key": "orders",
                "title": "Customer Return Requests",
                "module": "returns",
                "items": returns,
                "filter_status": status_filter,
                "can_manage": has_staff_capability(request.user, ORDERS_MANAGE),
            },
        )

    order_data = get_filtered_orders(request.user, request.GET)
    nav = get_staff_navigation(request.user, active_key="orders")
    can_manage = has_staff_capability(request.user, ORDERS_MANAGE)

    return render(
        request,
        "staff/orders/list.html",
        {
            "nav": nav,
            "active_key": "orders",
            "can_manage": can_manage,
            **order_data,
        },
    )


@staff_permission_required(ORDERS_VIEW)
def order_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Order inspection workspace displaying lines, address, timeline, and actions."""
    try:
        details = get_order_details(order_id=pk, user=request.user)
    except ValidationError as exc:
        messages.error(request, exc.messages[0] if hasattr(exc, "messages") else str(exc))
        return redirect("staff:orders")

    nav = get_staff_navigation(request.user, active_key="orders")
    pack_form = OrderPackForm()
    dispatch_form = OrderDispatchForm()

    return render(
        request,
        "staff/orders/detail.html",
        {
            "nav": nav,
            "active_key": "orders",
            "pack_form": pack_form,
            "dispatch_form": dispatch_form,
            **details,
        },
    )


@staff_permission_required(ORDERS_VIEW)
def order_packing_slip_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Print-friendly packing slip using verified order data and customer address."""
    try:
        details = get_order_details(order_id=pk, user=request.user)
    except ValidationError as exc:
        messages.error(request, exc.messages[0] if hasattr(exc, "messages") else str(exc))
        return redirect("staff:orders")

    return render(
        request,
        "staff/orders/packing_slip.html",
        details,
    )


@require_POST
@staff_permission_required(ORDERS_MANAGE)
def order_process_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark an eligible PAID order as PROCESSING and open fulfillment package."""
    try:
        shipment = process_order_action(order_id=pk, actor=request.user)
        messages.success(
            request,
            _("Order %(number)s marked as Processing. Fulfillment package %(ref)s opened.")
            % {"number": shipment.order.number, "ref": shipment.reference},
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0] if hasattr(exc, "messages") else str(exc))

    return redirect("staff:order_detail", pk=pk)


@require_POST
@staff_permission_required(ORDERS_MANAGE)
def order_start_picking_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Advance shipment to PICKING state, attributing actor and timestamp."""
    shipment_id = request.POST.get("shipment_id")
    shipment_id_int = int(shipment_id) if shipment_id and shipment_id.isdigit() else None
    note = request.POST.get("note", "").strip()

    try:
        shipment = start_picking_action(
            order_id=pk,
            shipment_id=shipment_id_int,
            actor=request.user,
            note=note,
        )
        messages.success(
            request,
            _("Warehouse picking started for shipment %(ref)s.") % {"ref": shipment.reference},
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0] if hasattr(exc, "messages") else str(exc))

    return redirect("staff:order_detail", pk=pk)


@require_POST
@staff_permission_required(ORDERS_MANAGE)
def order_mark_packed_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Record package packing completion with dimensions and staff checklist."""
    shipment_id = request.POST.get("shipment_id")
    shipment_id_int = int(shipment_id) if shipment_id and shipment_id.isdigit() else None

    form = OrderPackForm(request.POST)
    if form.is_valid():
        try:
            shipment = mark_packed_action(
                order_id=pk,
                shipment_id=shipment_id_int,
                actor=request.user,
                package_weight_kg=form.cleaned_data.get("package_weight_kg"),
                package_dimensions=form.cleaned_data.get("package_dimensions") or "",
                internal_notes=form.cleaned_data.get("internal_notes") or "",
            )
            messages.success(
                request,
                _("Package %(ref)s verified, quality checked, and marked as Packed.")
                % {"ref": shipment.reference},
            )
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if hasattr(exc, "messages") else str(exc))
    else:
        messages.error(request, _("Invalid packing details provided."))

    return redirect("staff:order_detail", pk=pk)


@require_POST
@staff_permission_required(ORDERS_MANAGE)
def order_dispatch_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Hand the packed order to courier, advancing shipment and order to SHIPPED."""
    shipment_id = request.POST.get("shipment_id")
    shipment_id_int = int(shipment_id) if shipment_id and shipment_id.isdigit() else None

    form = OrderDispatchForm(request.POST)
    if form.is_valid():
        try:
            shipment = dispatch_order_action(
                order_id=pk,
                shipment_id=shipment_id_int,
                actor=request.user,
                carrier=form.cleaned_data.get("carrier") or "",
                carrier_service=form.cleaned_data.get("carrier_service") or "",
                tracking_number=form.cleaned_data.get("tracking_number") or "",
                tracking_url=form.cleaned_data.get("tracking_url") or "",
                internal_notes=form.cleaned_data.get("internal_notes") or "",
            )
            messages.success(
                request,
                _("Shipment %(ref)s dispatched to carrier %(carrier)s.")
                % {"ref": shipment.reference, "carrier": shipment.carrier or "courier"},
            )
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if hasattr(exc, "messages") else str(exc))
    else:
        messages.error(request, _("Invalid courier dispatch details provided."))

    return redirect("staff:order_detail", pk=pk)


@staff_permission_required(DELIVERIES_VIEW)
def deliveries_view(request: HttpRequest) -> HttpResponse:
    """Deliveries & Shipments operations workspace."""
    workspace_data = get_filtered_shipments(request.user, request.GET)
    nav = get_staff_navigation(request.user, active_key="deliveries")

    return render(
        request,
        "staff/deliveries/list.html",
        {
            "nav": nav,
            "active_key": "deliveries",
            "title": _("Delivery & Shipment Operations"),
            "shipments": workspace_data["shipments"],
            "page_obj": workspace_data["page_obj"],
            "counts": workspace_data["counts"],
            "pure_worker": workspace_data["pure_worker"],
            "is_manager": workspace_data["is_manager"],
            "eligible_staff": workspace_data["eligible_staff"],
            "current_filters": workspace_data["current_filters"],
            "can_manage": has_staff_capability(request.user, DELIVERIES_MANAGE),
        },
    )


@staff_permission_required(DELIVERIES_VIEW)
def delivery_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Delivery details inspector with address snapshot, items, timeline, and actions."""
    try:
        details = get_shipment_details(pk, request.user)
    except ValidationError:
        messages.error(request, _("Shipment #%(pk)s was not found.") % {"pk": pk})
        return redirect("staff:deliveries")
    except PermissionDenied as exc:
        return render_access_denied(request, required_capability=DELIVERIES_VIEW, message=str(exc))

    shipment = details["shipment"]
    nav = get_staff_navigation(request.user, active_key="deliveries")

    context = {
        "nav": nav,
        "active_key": "deliveries",
        "title": _("Shipment %(ref)s") % {"ref": shipment.reference or f"#{shipment.pk}"},
        **details,
        "assignment_form": ShipmentAssignmentForm(),
        "status_form": ShipmentStatusUpdateForm(shipment=shipment),
        "attempt_form": DeliveryAttemptForm(),
        "pod_form": ProofOfDeliveryForm(),
    }
    return render(request, "staff/deliveries/detail.html", context)


@require_POST
@staff_permission_required(DELIVERIES_MANAGE)
def delivery_assign_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Assign or reassign an active shipment to an eligible delivery worker."""
    if not is_delivery_manager(request.user):
        return render_access_denied(
            request,
            required_capability=DELIVERIES_MANAGE,
            message=_("Only delivery operations managers can assign shipments."),
        )

    staff_id_str = request.POST.get("assigned_to", "").strip()
    note = request.POST.get("assignment_note", "").strip()

    if not staff_id_str.isdigit():
        messages.error(request, _("Please select a valid delivery staff worker."))
        return redirect("staff:delivery_detail", pk=pk)

    try:
        shipment = assign_shipment_action(
            pk,
            staff_user_id=int(staff_id_str),
            manager=request.user,
            assignment_note=note,
        )
        assigned_name = shipment.assigned_to.get_full_name() or shipment.assigned_to.email
        messages.success(
            request,
            _("Shipment %(ref)s assigned to %(worker)s successfully.")
            % {
                "ref": shipment.reference or f"#{shipment.pk}",
                "worker": assigned_name,
            },
        )
    except PermissionDenied as exc:
        return render_access_denied(
            request, required_capability=DELIVERIES_MANAGE, message=str(exc)
        )
    except ValidationError as exc:
        messages.error(request, str(exc))

    return redirect("staff:delivery_detail", pk=pk)


@require_POST
@staff_permission_required(DELIVERIES_MANAGE)
def delivery_status_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Advance shipment delivery status."""
    target_status = request.POST.get("target_status", "").strip()
    location = request.POST.get("location", "").strip()
    note = request.POST.get("note", "").strip()

    try:
        shipment = update_delivery_status_action(
            pk,
            target_status=target_status,
            location=location,
            note=note,
            actor=request.user,
        )
        messages.success(
            request,
            _("Shipment %(ref)s status updated to '%(status)s'.")
            % {
                "ref": shipment.reference or f"#{shipment.pk}",
                "status": shipment.get_status_display(),
            },
        )
    except PermissionDenied as exc:
        return render_access_denied(
            request, required_capability=DELIVERIES_MANAGE, message=str(exc)
        )
    except ValidationError as exc:
        messages.error(request, str(exc))

    return redirect("staff:delivery_detail", pk=pk)


@require_POST
@staff_permission_required(DELIVERIES_MANAGE)
def delivery_attempt_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Record an unsuccessful delivery attempt or operational exception."""
    form = DeliveryAttemptForm(request.POST)
    if not form.is_valid():
        for err_list in form.errors.values():
            for err in err_list:
                messages.error(request, err)
        return redirect("staff:delivery_detail", pk=pk)

    try:
        shipment = record_delivery_attempt_action(
            pk,
            failure_reason=form.cleaned_data["failure_reason"],
            location=form.cleaned_data.get("location", ""),
            note=form.cleaned_data.get("note", ""),
            next_attempt_date=form.cleaned_data.get("next_attempt_date"),
            requires_manager_review=form.cleaned_data.get("requires_manager_review", False),
            actor=request.user,
        )
        messages.warning(
            request,
            _("Delivery attempt #%(count)d recorded for Shipment %(ref)s.")
            % {
                "count": shipment.delivery_attempts_count,
                "ref": shipment.reference or f"#{shipment.pk}",
            },
        )
    except PermissionDenied as exc:
        return render_access_denied(
            request, required_capability=DELIVERIES_MANAGE, message=str(exc)
        )
    except ValidationError as exc:
        messages.error(request, str(exc))

    return redirect("staff:delivery_detail", pk=pk)


@require_POST
@staff_permission_required(DELIVERIES_MANAGE)
def delivery_pod_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Verify proof of delivery and record parcel completion."""
    form = ProofOfDeliveryForm(request.POST)
    if not form.is_valid():
        for err_list in form.errors.values():
            for err in err_list:
                messages.error(request, err)
        return redirect("staff:delivery_detail", pk=pk)

    try:
        shipment = record_proof_of_delivery_action(
            pk,
            pod_type=form.cleaned_data["pod_type"],
            otp_code=form.cleaned_data.get("otp_code", ""),
            recipient_name=form.cleaned_data.get("recipient_name", ""),
            notes=form.cleaned_data.get("notes", ""),
            actor=request.user,
        )
        messages.success(
            request,
            _("Shipment %(ref)s confirmed DELIVERED. Customer and order status synchronized.")
            % {"ref": shipment.reference or f"#{shipment.pk}"},
        )
    except PermissionDenied as exc:
        return render_access_denied(
            request, required_capability=DELIVERIES_MANAGE, message=str(exc)
        )
    except ValidationError as exc:
        messages.error(request, str(exc))

    return redirect("staff:delivery_detail", pk=pk)


@require_POST
@staff_permission_required(DELIVERIES_MANAGE)
def delivery_otp_generate_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Generate or refresh customer OTP verification code."""
    try:
        shipment, raw_otp = generate_delivery_otp_action(pk, actor=request.user)
        messages.info(
            request,
            _("Generated delivery confirmation code for %(ref)s: %(otp)s (Valid for 48 hours).")
            % {"ref": shipment.reference or f"#{shipment.pk}", "otp": raw_otp},
        )
    except PermissionDenied as exc:
        return render_access_denied(
            request, required_capability=DELIVERIES_MANAGE, message=str(exc)
        )
    except ValidationError as exc:
        messages.error(request, str(exc))

    return redirect("staff:delivery_detail", pk=pk)


@staff_permission_required(SUPPORT_VIEW)
def support_view(request: HttpRequest) -> HttpResponse:
    """Support Tickets module landing with urgent/unassigned filter support."""
    from apps.support.models import SupportTicket

    status_filter = request.GET.get("status", "").strip().lower()
    tickets_qs = SupportTicket.objects.select_related("customer")

    if status_filter == "urgent":
        tickets_qs = tickets_qs.filter(
            Q(priority=SupportTicket.Priority.URGENT) | Q(status=SupportTicket.Status.ESCALATED)
        )
    elif status_filter == "open":
        tickets_qs = tickets_qs.filter(
            status__in=[
                SupportTicket.Status.OPEN,
                SupportTicket.Status.IN_PROGRESS,
                SupportTicket.Status.ESCALATED,
            ]
        )
    elif status_filter == "unassigned":
        tickets_qs = tickets_qs.filter(
            assigned_to__isnull=True,
            status__in=[SupportTicket.Status.OPEN, SupportTicket.Status.ESCALATED],
        )

    tickets = tickets_qs.order_by("-updated_at")[:25]
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
            "filter_status": status_filter,
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
