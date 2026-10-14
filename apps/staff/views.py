"""Views for the FLASHWEAR Staff Portal (Phase 57)."""

from __future__ import annotations

import logging
import uuid

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
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
    INVENTORY_MANAGE,
    INVENTORY_VIEW,
    NOTIFICATIONS_VIEW,
    ORDERS_MANAGE,
    ORDERS_VIEW,
    PORTAL_ACCESS,
    PRODUCTS_VIEW,
    REPORTS_VIEW,
    ROLE_PRIMARY_GROUP,
    STAFF_MANAGE,
    SUPPORT_VIEW,
    TASKS_MANAGE,
    TASKS_VIEW,
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
    StaffTaskAssignForm,
    StaffTaskCommentForm,
    StaffTaskCreateForm,
    StaffTaskProgressForm,
    StaffTaskStatusForm,
    StaffTaskUpdateForm,
    StockAdjustmentForm,
    StockReceiveForm,
    StockReversalForm,
    StockThresholdForm,
)
from apps.staff.inventory_services import (
    can_manage_inventory,
    get_filtered_inventory,
    get_stock_ledger_history,
    get_variant_stock_details,
    is_inventory_manager,
    record_compensating_reversal,
    record_stock_adjustment,
    update_reorder_threshold,
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


# --------------------------------------------------------------------------------------
# Phase 62: Inventory & Stock Operations Views
# --------------------------------------------------------------------------------------


@staff_permission_required(INVENTORY_VIEW)
def inventory_view(request: HttpRequest) -> HttpResponse:
    """Searchable, paginated inventory workspace with stock statuses and multi-attribute filters."""
    context_data = get_filtered_inventory(request.GET)
    nav = get_staff_navigation(request.user, active_key="inventory")
    context_data.update(
        {
            "nav": nav,
            "active_key": "inventory",
            "can_manage": can_manage_inventory(request.user),
            "is_manager": is_inventory_manager(request.user),
        }
    )
    return render(request, "staff/inventory/workspace.html", context_data)


@staff_permission_required(INVENTORY_VIEW)
def inventory_variant_detail_view(request: HttpRequest, variant_id: int) -> HttpResponse:
    """Detailed inspection of a variant's stock, reservations, and ledger history."""
    try:
        context_data = get_variant_stock_details(variant_id, user=request.user)
    except ValidationError as exc:
        messages.error(request, str(exc))
        return redirect("staff:inventory")

    nav = get_staff_navigation(request.user, active_key="inventory")
    adjust_token = uuid.uuid4().hex
    receive_token = uuid.uuid4().hex
    stock = context_data["stock"]

    context_data.update(
        {
            "nav": nav,
            "active_key": "inventory",
            "adjust_form": StockAdjustmentForm(initial={"idempotency_token": adjust_token}),
            "receive_form": StockReceiveForm(initial={"idempotency_token": receive_token}),
            "threshold_form": StockThresholdForm(
                initial={"low_stock_threshold": stock.low_stock_threshold}
            ),
            "reversal_form": StockReversalForm() if context_data["is_manager"] else None,
        }
    )
    return render(request, "staff/inventory/detail.html", context_data)


@require_POST
@staff_permission_required(INVENTORY_MANAGE)
def inventory_adjust_action_view(request: HttpRequest, variant_id: int) -> HttpResponse:
    """Process manual stock adjustment (discrepancy, damage, loss)."""
    form = StockAdjustmentForm(request.POST)
    if not form.is_valid():
        first_err = next(iter(form.errors.values()))[0]
        messages.error(request, _("Invalid adjustment data: %(err)s") % {"err": first_err})
        return redirect("staff:inventory_variant_detail", variant_id=variant_id)

    cd = form.cleaned_data
    try:
        stock, _movement = record_stock_adjustment(
            variant_id,
            adjustment_type=cd["adjustment_type"],
            quantity=cd["quantity"],
            reason=cd["reason"],
            actor=request.user,
            reference=cd.get("reference", ""),
            note=cd.get("note", ""),
            idempotency_token=cd.get("idempotency_token", ""),
        )
        messages.success(
            request,
            _(
                "Stock adjustment applied successfully for %(sku)s. "
                "On-hand: %(on_hand)d, Available: %(avail)d."
            )
            % {
                "sku": stock.variant.sku,
                "on_hand": stock.on_hand,
                "avail": stock.available,
            },
        )
    except PermissionDenied as exc:
        return render_access_denied(request, required_capability=INVENTORY_MANAGE, message=str(exc))
    except ValidationError as exc:
        messages.error(request, str(exc))

    return redirect("staff:inventory_variant_detail", variant_id=variant_id)


@require_POST
@staff_permission_required(INVENTORY_MANAGE)
def inventory_receive_action_view(request: HttpRequest, variant_id: int) -> HttpResponse:
    """Receive stock shipment for a variant."""
    form = StockReceiveForm(request.POST)
    if not form.is_valid():
        first_err = next(iter(form.errors.values()))[0]
        messages.error(request, _("Invalid receipt data: %(err)s") % {"err": first_err})
        return redirect("staff:inventory_variant_detail", variant_id=variant_id)

    cd = form.cleaned_data
    try:
        stock, _movement = record_stock_adjustment(
            variant_id,
            adjustment_type="received",
            quantity=cd["quantity"],
            reason=cd.get("reason", "") or "Stock receipt",
            actor=request.user,
            reference=cd.get("supplier_reference", ""),
            note=cd.get("note", ""),
            idempotency_token=cd.get("idempotency_token", ""),
        )
        messages.success(
            request,
            _("Received %(qty)d units for %(sku)s. On-hand: %(on_hand)d, Available: %(avail)d.")
            % {
                "qty": cd["quantity"],
                "sku": stock.variant.sku,
                "on_hand": stock.on_hand,
                "avail": stock.available,
            },
        )
    except PermissionDenied as exc:
        return render_access_denied(request, required_capability=INVENTORY_MANAGE, message=str(exc))
    except ValidationError as exc:
        messages.error(request, str(exc))

    return redirect("staff:inventory_variant_detail", variant_id=variant_id)


@require_POST
@staff_permission_required(INVENTORY_MANAGE)
def inventory_threshold_action_view(request: HttpRequest, variant_id: int) -> HttpResponse:
    """Update variant reorder threshold."""
    form = StockThresholdForm(request.POST)
    if not form.is_valid():
        first_err = next(iter(form.errors.values()))[0]
        messages.error(request, _("Invalid threshold value: %(err)s") % {"err": first_err})
        return redirect("staff:inventory_variant_detail", variant_id=variant_id)

    try:
        stock = update_reorder_threshold(
            variant_id,
            threshold=form.cleaned_data.get("low_stock_threshold"),
            actor=request.user,
        )
        if stock.low_stock_threshold is not None:
            messages.success(
                request,
                _("Reorder threshold for %(sku)s updated to %(thresh)d.")
                % {"sku": stock.variant.sku, "thresh": stock.low_stock_threshold},
            )
        else:
            messages.success(
                request,
                _(
                    "Variant-specific threshold cleared for %(sku)s. "
                    "Inheriting product/global threshold."
                )
                % {"sku": stock.variant.sku},
            )
    except PermissionDenied as exc:
        return render_access_denied(request, required_capability=INVENTORY_MANAGE, message=str(exc))
    except ValidationError as exc:
        messages.error(request, str(exc))

    return redirect("staff:inventory_variant_detail", variant_id=variant_id)


@staff_permission_required(INVENTORY_VIEW)
def inventory_ledger_view(request: HttpRequest) -> HttpResponse:
    """Searchable, paginated audit ledger of all stock movements."""
    context_data = get_stock_ledger_history(request.GET)
    nav = get_staff_navigation(request.user, active_key="inventory")
    is_mgr = is_inventory_manager(request.user)
    context_data.update(
        {
            "nav": nav,
            "active_key": "inventory",
            "can_manage": can_manage_inventory(request.user),
            "is_manager": is_mgr,
            "reversal_form": StockReversalForm() if is_mgr else None,
        }
    )
    return render(request, "staff/inventory/ledger.html", context_data)


@require_POST
@staff_permission_required(INVENTORY_MANAGE)
def inventory_reverse_action_view(request: HttpRequest, movement_id: int) -> HttpResponse:
    """Authorize and execute a compensating reversal for an erroneous movement."""
    if not is_inventory_manager(request.user):
        return render_access_denied(
            request,
            required_capability=INVENTORY_MANAGE,
            message=_(
                "Only inventory managers or administrators can authorize compensating reversals."
            ),
        )

    form = StockReversalForm(request.POST)
    if not form.is_valid():
        first_err = next(iter(form.errors.values()))[0]
        messages.error(request, _("Invalid reversal form: %(err)s") % {"err": first_err})
        return redirect("staff:inventory_ledger")

    try:
        stock, reversal_movement = record_compensating_reversal(
            movement_id,
            reason=form.cleaned_data["reason"],
            actor=request.user,
        )
        messages.success(
            request,
            _(
                "Movement #%(orig_id)d reversed successfully with "
                "compensating movement #%(rev_id)d. "
                "New on-hand for %(sku)s is %(on_hand)d."
            )
            % {
                "orig_id": movement_id,
                "rev_id": reversal_movement.pk,
                "sku": stock.variant.sku,
                "on_hand": stock.on_hand,
            },
        )
    except PermissionDenied as exc:
        return render_access_denied(request, required_capability=INVENTORY_MANAGE, message=str(exc))
    except ValidationError as exc:
        messages.error(request, str(exc))

    next_url = request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("staff:inventory_ledger")


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


# --------------------------------------------------------------------------------------
# Staff Task Management & Collaboration (Phase 63)
# --------------------------------------------------------------------------------------


@staff_permission_required(TASKS_VIEW)
def task_list_view(request: HttpRequest) -> HttpResponse:
    """List staff tasks with scope filters, priority filters, search, and metrics."""
    from apps.staff.models import StaffTaskPriority, StaffTaskStatus
    from apps.staff.task_services import (
        get_eligible_assignees,
        get_filtered_tasks,
        get_staff_task_metrics,
    )

    user = request.user
    scope = request.GET.get("scope", "all").strip().lower()
    status_filter = request.GET.get("status", "").strip().lower()
    priority_filter = request.GET.get("priority", "").strip().lower()
    assignee_id = request.GET.get("assignee", "").strip()
    due_filter = request.GET.get("due", "").strip().lower()
    query = request.GET.get("q", "").strip()

    assignee_int = None
    if assignee_id.isdigit():
        assignee_int = int(assignee_id)

    tasks_qs = get_filtered_tasks(
        user=user,
        status=status_filter or None,
        priority=priority_filter or None,
        assignee_id=assignee_int,
        scope=scope,
        search_query=query or None,
        due_filter=due_filter or None,
    )

    paginator = Paginator(tasks_qs, 15)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    metrics = get_staff_task_metrics(user)
    eligible_assignees = get_eligible_assignees()
    can_manage = user.is_superuser or has_staff_capability(user, TASKS_MANAGE)
    nav = get_staff_navigation(user, active_key="tasks")

    return render(
        request,
        "staff/tasks/list.html",
        {
            "nav": nav,
            "active_key": "tasks",
            "page_obj": page_obj,
            "tasks": page_obj.object_list,
            "metrics": metrics,
            "scope": scope,
            "status_filter": status_filter,
            "priority_filter": priority_filter,
            "assignee_filter": assignee_id,
            "due_filter": due_filter,
            "search_query": query,
            "can_manage": can_manage,
            "statuses": StaffTaskStatus.choices,
            "priorities": StaffTaskPriority.choices,
            "eligible_assignees": eligible_assignees,
        },
    )


@staff_permission_required(TASKS_MANAGE)
def task_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new staff task and assign it to an active employee."""
    from apps.staff.models import StaffTaskPriority, StaffTaskRelatedType
    from apps.staff.task_services import create_staff_task

    initial_data = {}
    rel_type = request.GET.get("related_type", "").strip()
    rel_id = request.GET.get("related_id", "").strip()
    if rel_type in dict(StaffTaskRelatedType.choices):
        initial_data["related_object_type"] = rel_type
    if rel_id.isdigit():
        initial_data["related_object_id"] = int(rel_id)

    if request.method == "POST":
        form = StaffTaskCreateForm(request.POST)
        if form.is_valid():
            try:
                task = create_staff_task(
                    creator=request.user,
                    title=form.cleaned_data["title"],
                    description=form.cleaned_data.get("description", ""),
                    priority=form.cleaned_data.get("priority", StaffTaskPriority.NORMAL),
                    assignee=form.cleaned_data.get("assignee"),
                    due_date=form.cleaned_data.get("due_date"),
                    related_object_type=form.cleaned_data.get("related_object_type", ""),
                    related_object_id=form.cleaned_data.get("related_object_id"),
                )
                messages.success(request, _("Task #%(id)d successfully created.") % {"id": task.pk})
                return redirect("staff:task_detail", pk=task.pk)
            except ValidationError as exc:
                for field, errors in exc.message_dict.items():
                    for err in errors:
                        form.add_error(field if field in form.fields else None, err)
            except PermissionDenied as exc:
                messages.error(request, str(exc))
    else:
        form = StaffTaskCreateForm(initial=initial_data)

    nav = get_staff_navigation(request.user, active_key="tasks")
    return render(
        request,
        "staff/tasks/create.html",
        {
            "nav": nav,
            "active_key": "tasks",
            "form": form,
        },
    )


@staff_permission_required(TASKS_VIEW)
def task_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    """View task workspace, history, linked business object, and post comments."""
    from apps.staff.models import StaffTask, StaffTaskStatus
    from apps.staff.task_services import (
        can_user_assign_task,
        can_user_comment,
        can_user_edit_task,
        can_user_update_progress,
        can_user_view_task,
        get_linked_object_staff_url,
    )

    task = get_object_or_404(
        StaffTask.objects.select_related("creator", "assignee", "completed_by"),
        pk=pk,
    )

    if not can_user_view_task(request.user, task):
        return render_access_denied(
            request,
            required_capability=TASKS_VIEW,
            message=_("You are not authorized to view Task #%(id)d.") % {"id": pk},
        )

    # Allowed status choices based on current lifecycle state
    allowed_status_keys = task.VALID_TRANSITIONS.get(task.status, set()) | {task.status}
    allowed_status_choices = [
        choice for choice in StaffTaskStatus.choices if choice[0] in allowed_status_keys
    ]

    status_form = StaffTaskStatusForm(
        initial={"status": task.status},
        allowed_choices=allowed_status_choices,
    )
    assign_form = StaffTaskAssignForm(initial={"assignee": task.assignee})
    progress_form = StaffTaskProgressForm(initial={"progress_percent": task.progress_percent})
    comment_form = StaffTaskCommentForm()
    update_form = StaffTaskUpdateForm(
        initial={
            "title": task.title,
            "description": task.description,
            "priority": task.priority,
            "due_date": task.due_date,
        }
    )

    can_edit = can_user_edit_task(request.user, task)
    can_assign = can_user_assign_task(request.user)
    can_progress = can_user_update_progress(request.user, task)
    can_comment = can_user_comment(request.user, task)
    linked_object_url = get_linked_object_staff_url(task, request.user)

    comments = task.comments.select_related("author").order_by("created_at")
    activities = task.activities.select_related("actor").order_by("-created_at")

    nav = get_staff_navigation(request.user, active_key="tasks")

    return render(
        request,
        "staff/tasks/detail.html",
        {
            "nav": nav,
            "active_key": "tasks",
            "task": task,
            "status_form": status_form,
            "assign_form": assign_form,
            "progress_form": progress_form,
            "comment_form": comment_form,
            "update_form": update_form,
            "can_edit": can_edit,
            "can_assign": can_assign,
            "can_progress": can_progress,
            "can_comment": can_comment,
            "linked_object_url": linked_object_url,
            "comments": comments,
            "activities": activities,
        },
    )


@require_POST
@staff_permission_required(TASKS_MANAGE)
def task_edit_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit core task fields (title, description, priority, due date)."""
    from apps.staff.models import StaffTask
    from apps.staff.task_services import update_task_fields

    task = get_object_or_404(StaffTask, pk=pk)
    form = StaffTaskUpdateForm(request.POST)
    if form.is_valid():
        try:
            update_task_fields(
                task=task,
                actor=request.user,
                title=form.cleaned_data["title"],
                description=form.cleaned_data.get("description", ""),
                priority=form.cleaned_data["priority"],
                due_date=form.cleaned_data.get("due_date"),
                due_date_cleared=(
                    form.cleaned_data.get("due_date") is None and "due_date" in request.POST
                ),
            )
            messages.success(request, _("Task #%(id)d details updated.") % {"id": pk})
        except PermissionDenied as exc:
            messages.error(request, str(exc))
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)

    return redirect("staff:task_detail", pk=pk)


@require_POST
@staff_permission_required(TASKS_MANAGE)
def task_assign_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Assign or reassign task to an active employee."""
    from apps.staff.models import StaffTask
    from apps.staff.task_services import reassign_staff_task

    task = get_object_or_404(StaffTask, pk=pk)
    form = StaffTaskAssignForm(request.POST)
    if form.is_valid():
        try:
            reassign_staff_task(
                task=task,
                new_assignee=form.cleaned_data["assignee"],
                actor=request.user,
            )
            messages.success(request, _("Task #%(id)d assignment updated.") % {"id": pk})
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, str(exc))
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)

    return redirect("staff:task_detail", pk=pk)


@require_POST
@staff_permission_required(TASKS_VIEW)
def task_status_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Update task lifecycle status."""
    from apps.staff.models import StaffTask
    from apps.staff.task_services import update_task_status

    task = get_object_or_404(StaffTask, pk=pk)
    new_status = request.POST.get("status", "").strip().lower()

    try:
        update_task_status(
            task=task,
            new_status=new_status,
            actor=request.user,
        )
        messages.success(
            request,
            _("Task #%(id)d status updated to %(status)s.")
            % {"id": pk, "status": task.get_status_display()},
        )
    except PermissionDenied as exc:
        messages.error(request, str(exc))

    return redirect("staff:task_detail", pk=pk)


@require_POST
@staff_permission_required(TASKS_VIEW)
def task_progress_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Update task progress percentage."""
    from apps.staff.models import StaffTask
    from apps.staff.task_services import update_task_progress

    task = get_object_or_404(StaffTask, pk=pk)
    form = StaffTaskProgressForm(request.POST)
    if form.is_valid():
        try:
            update_task_progress(
                task=task,
                progress_percent=form.cleaned_data["progress_percent"],
                actor=request.user,
            )
            messages.success(
                request,
                _("Task #%(id)d progress updated to %(pct)d%%.")
                % {"id": pk, "pct": task.progress_percent},
            )
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, str(exc))
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)

    return redirect("staff:task_detail", pk=pk)


@require_POST
@staff_permission_required(TASKS_VIEW)
def task_comment_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Post an operational note or comment on a task."""
    from apps.staff.models import StaffTask
    from apps.staff.task_services import add_task_comment

    task = get_object_or_404(StaffTask, pk=pk)
    form = StaffTaskCommentForm(request.POST)
    if form.is_valid():
        try:
            add_task_comment(
                task=task,
                author=request.user,
                comment_text=form.cleaned_data["comment"],
            )
            messages.success(request, _("Operational note added to Task #%(id)d.") % {"id": pk})
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, str(exc))
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)

    return redirect("staff:task_detail", pk=pk)


# --------------------------------------------------------------------------------------
# Staff In-App Notification Center (Phase 63)
# --------------------------------------------------------------------------------------


@staff_permission_required(NOTIFICATIONS_VIEW)
def notification_list_view(request: HttpRequest) -> HttpResponse:
    """Staff in-app notification center with read states, filters, and direct action links."""
    from apps.staff.notification_services import (
        get_staff_notifications,
        get_staff_unread_count,
    )

    filter_type = request.GET.get("filter", "all").strip().lower()
    unread_only = filter_type == "unread"

    notifications_qs = get_staff_notifications(request.user, unread_only=unread_only)

    paginator = Paginator(notifications_qs, 20)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    unread_count = get_staff_unread_count(request.user)
    nav = get_staff_navigation(request.user, active_key="notifications")

    return render(
        request,
        "staff/notifications/list.html",
        {
            "nav": nav,
            "active_key": "notifications",
            "page_obj": page_obj,
            "notifications": page_obj.object_list,
            "unread_count": unread_count,
            "filter_type": filter_type,
        },
    )


@require_POST
@staff_permission_required(NOTIFICATIONS_VIEW)
def notification_read_action_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark a staff notification as read (with IDOR protection) and navigate to URL."""
    from apps.staff.notification_services import mark_staff_notification_read

    notification = mark_staff_notification_read(request.user, pk)
    if not notification:
        messages.error(request, _("Notification not found or access denied."))
        return redirect("staff:notification_list")

    redirect_url = request.POST.get("next") or (
        notification.action_url if notification.action_url else reverse("staff:notification_list")
    )
    if not url_has_allowed_host_and_scheme(
        redirect_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        redirect_url = reverse("staff:notification_list")

    return redirect(redirect_url)


@require_POST
@staff_permission_required(NOTIFICATIONS_VIEW)
def notification_mark_all_read_action_view(request: HttpRequest) -> HttpResponse:
    """Mark all unread staff notifications as read for current user."""
    from apps.staff.notification_services import mark_all_staff_notifications_read

    count = mark_all_staff_notifications_read(request.user)
    messages.success(request, _("Marked %(count)d notifications as read.") % {"count": count})
    return redirect("staff:notification_list")
