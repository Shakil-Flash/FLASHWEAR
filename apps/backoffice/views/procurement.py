"""Back Office views for Procurement, Suppliers, and Purchase Order operations (Phase 45)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.backoffice.permissions import (
    PURCHASING_APPROVE,
    PURCHASING_MANAGE,
    PURCHASING_VIEW,
    backoffice_access,
    has,
)
from apps.catalog.models import ProductVariant
from apps.procurement.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderStatus,
    Supplier,
    SupplierVariant,
)
from apps.procurement.selectors import (
    get_procurement_dashboard_metrics,
    get_purchase_orders_queryset,
    get_supplier_performance,
    get_suppliers_queryset,
)
from apps.procurement.services import (
    DuplicateReceiptError,
    InvalidStatusTransition,
    OrderImmutableError,
    OverReceiptError,
    ProcurementError,
    approve_purchase_order,
    cancel_purchase_order,
    create_purchase_order,
    create_supplier,
    receive_purchase_order_delivery,
    send_purchase_order,
    submit_for_approval,
    update_purchase_order,
    update_supplier,
)


@backoffice_access(PURCHASING_VIEW)
def procurement_dashboard(request: HttpRequest) -> HttpResponse:
    """Purchasing intelligence dashboard displaying grounded operational metrics."""
    metrics = get_procurement_dashboard_metrics()
    context = {
        "title": _("Procurement & Purchasing"),
        "active_tab": "procurement",
        **metrics,
    }
    return render(request, "backoffice/procurement/dashboard.html", context)


# =============================================================================
# Supplier Management Views
# =============================================================================


@backoffice_access(PURCHASING_VIEW)
def suppliers_list(request: HttpRequest) -> HttpResponse:
    """Searchable, filterable list of suppliers."""
    q = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "all")

    is_active = None
    if status_filter == "active":
        is_active = True
    elif status_filter == "inactive":
        is_active = False

    suppliers_qs = get_suppliers_queryset(q=q, is_active=is_active)
    paginator = Paginator(suppliers_qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "title": _("Suppliers"),
        "active_tab": "procurement",
        "suppliers": page_obj,
        "page_obj": page_obj,
        "q": q,
        "status_filter": status_filter,
        "total_count": paginator.count,
    }
    return render(request, "backoffice/procurement/supplier_list.html", context)


@backoffice_access(PURCHASING_MANAGE)
def supplier_create(request: HttpRequest) -> HttpResponse:
    """Create a new supplier business profile."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip()
        contact_name = request.POST.get("contact_name", "").strip()
        email = request.POST.get("email", "").strip()
        phone = request.POST.get("phone", "").strip()
        address_line1 = request.POST.get("address_line1", "").strip()
        address_line2 = request.POST.get("address_line2", "").strip()
        city = request.POST.get("city", "").strip()
        postal_code = request.POST.get("postal_code", "").strip()
        country = request.POST.get("country", "").strip() or "United Kingdom"
        currency = request.POST.get("currency", "USD").strip()
        lead_time_days = request.POST.get("lead_time_days")
        moq = request.POST.get("minimum_order_quantity", "1")
        is_active = request.POST.get("is_active") == "on"
        notes = request.POST.get("notes", "").strip()

        try:
            supplier = create_supplier(
                name=name,
                code=code,
                contact_name=contact_name,
                email=email,
                phone=phone,
                address_line1=address_line1,
                address_line2=address_line2,
                city=city,
                postal_code=postal_code,
                country=country,
                currency=currency,
                lead_time_days=int(lead_time_days) if lead_time_days else None,
                minimum_order_quantity=int(moq) if moq else 1,
                is_active=is_active,
                notes=notes,
                actor=request.user,
            )
            messages.success(request, _("Supplier %(name)s created successfully.") % {"name": supplier.name})
            return redirect("backoffice:supplier_detail", pk=supplier.pk)
        except (ValidationError, ValueError) as err:
            msg = err.message if hasattr(err, "message") else str(err)
            messages.error(request, msg)

    context = {
        "title": _("New Supplier"),
        "active_tab": "procurement",
        "action_url": reverse("backoffice:supplier_create"),
        "supplier": None,
    }
    return render(request, "backoffice/procurement/supplier_form.html", context)


@backoffice_access(PURCHASING_VIEW)
def supplier_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Supplier detail with performance history and variant catalogue."""
    supplier = get_object_or_404(Supplier, pk=pk)

    # Handle adding a variant to the supplier's catalogue
    if request.method == "POST" and has(request.user, PURCHASING_MANAGE):
        action = request.POST.get("action")
        if action == "add_variant":
            sku_or_id = request.POST.get("variant_sku", "").strip()
            purchase_cost_raw = request.POST.get("purchase_cost", "0.00").strip()
            supplier_sku = request.POST.get("supplier_sku", "").strip()
            lead_time = request.POST.get("lead_time_days")
            moq = request.POST.get("minimum_order_quantity", "1")
            is_preferred = request.POST.get("is_preferred") == "on"

            variant = (
                ProductVariant.objects.filter(sku__iexact=sku_or_id).first()
                or ProductVariant.objects.filter(pk=sku_or_id if sku_or_id.isdigit() else 0).first()
            )
            if not variant:
                messages.error(request, _("Variant %(sku)s not found.") % {"sku": sku_or_id})
            else:
                try:
                    cost = Decimal(purchase_cost_raw)
                    sv, created = SupplierVariant.objects.update_or_create(
                        supplier=supplier,
                        variant=variant,
                        defaults={
                            "purchase_cost": cost,
                            "supplier_sku": supplier_sku,
                            "lead_time_days": int(lead_time) if lead_time else None,
                            "minimum_order_quantity": max(1, int(moq) if moq else 1),
                            "is_preferred": is_preferred,
                            "currency": supplier.currency,
                        },
                    )
                    messages.success(
                        request,
                        _("Variant %(sku)s linked to %(supplier)s.")
                        % {"sku": variant.sku, "supplier": supplier.name},
                    )
                except Exception as ex:
                    messages.error(request, str(ex))
            return redirect("backoffice:supplier_detail", pk=supplier.pk)

    performance = get_supplier_performance(supplier)
    supplier_variants = (
        supplier.supplier_variants.select_related("variant", "variant__product")
        .order_by("variant__sku")
    )
    orders = (
        supplier.purchase_orders.prefetch_related("items")
        .order_by("-created_at")[:15]
    )

    context = {
        "title": supplier.name,
        "active_tab": "procurement",
        "supplier": supplier,
        "performance": performance,
        "supplier_variants": supplier_variants,
        "orders": orders,
        "can_manage": has(request.user, PURCHASING_MANAGE),
    }
    return render(request, "backoffice/procurement/supplier_detail.html", context)


@backoffice_access(PURCHASING_MANAGE)
def supplier_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit supplier details."""
    supplier = get_object_or_404(Supplier, pk=pk)

    if request.method == "POST":
        name = request.POST.get("name")
        contact_name = request.POST.get("contact_name")
        email = request.POST.get("email")
        phone = request.POST.get("phone")
        address_line1 = request.POST.get("address_line1")
        address_line2 = request.POST.get("address_line2")
        city = request.POST.get("city")
        postal_code = request.POST.get("postal_code")
        country = request.POST.get("country")
        currency = request.POST.get("currency")
        lead_time_days = request.POST.get("lead_time_days")
        moq = request.POST.get("minimum_order_quantity")
        is_active = request.POST.get("is_active") == "on"
        notes = request.POST.get("notes")

        try:
            update_supplier(
                supplier,
                name=name,
                contact_name=contact_name,
                email=email,
                phone=phone,
                address_line1=address_line1,
                address_line2=address_line2,
                city=city,
                postal_code=postal_code,
                country=country,
                currency=currency,
                lead_time_days=int(lead_time_days) if lead_time_days else None,
                minimum_order_quantity=int(moq) if moq else 1,
                is_active=is_active,
                notes=notes,
                actor=request.user,
            )
            messages.success(request, _("Supplier %(name)s updated.") % {"name": supplier.name})
            return redirect("backoffice:supplier_detail", pk=supplier.pk)
        except (ValidationError, ValueError) as err:
            msg = err.message if hasattr(err, "message") else str(err)
            messages.error(request, msg)

    context = {
        "title": _("Edit %(name)s") % {"name": supplier.name},
        "active_tab": "procurement",
        "action_url": reverse("backoffice:supplier_edit", args=[supplier.pk]),
        "supplier": supplier,
    }
    return render(request, "backoffice/procurement/supplier_form.html", context)


# =============================================================================
# Purchase Order Views
# =============================================================================


@backoffice_access(PURCHASING_VIEW)
def purchase_orders_list(request: HttpRequest) -> HttpResponse:
    """Searchable, filterable list of purchase orders."""
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    supplier_id = request.GET.get("supplier")
    is_overdue = None
    if request.GET.get("overdue") == "1":
        is_overdue = True

    orders_qs = get_purchase_orders_queryset(
        q=q,
        status=status,
        supplier_id=int(supplier_id) if supplier_id and supplier_id.isdigit() else None,
        is_overdue=is_overdue,
    )

    paginator = Paginator(orders_qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    suppliers = Supplier.objects.filter(is_active=True).order_by("name")

    context = {
        "title": _("Purchase Orders"),
        "active_tab": "procurement",
        "orders": page_obj,
        "page_obj": page_obj,
        "q": q,
        "selected_status": status,
        "selected_supplier": int(supplier_id) if supplier_id and supplier_id.isdigit() else "",
        "is_overdue": is_overdue,
        "statuses": PurchaseOrderStatus.choices,
        "suppliers": suppliers,
        "total_count": paginator.count,
    }
    return render(request, "backoffice/procurement/order_list.html", context)


@backoffice_access(PURCHASING_MANAGE)
def purchase_order_create(request: HttpRequest) -> HttpResponse:
    """Create draft purchase order with line items."""
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")

    if request.method == "POST":
        supplier_id = request.POST.get("supplier_id")
        delivery_date = request.POST.get("expected_delivery_date")
        currency = request.POST.get("currency", "USD").strip()
        notes = request.POST.get("notes", "").strip()

        supplier = get_object_or_404(Supplier, pk=supplier_id)

        # Parse line items from submitted tabular form
        # variants[] or sku[], quantities[], unit_costs[]
        skus = request.POST.getlist("variant_sku[]")
        quantities = request.POST.getlist("quantity[]")
        unit_costs = request.POST.getlist("unit_cost[]")
        item_notes = request.POST.getlist("item_notes[]")

        items: list[dict[str, Any]] = []
        for i, sku in enumerate(skus):
            sku_clean = sku.strip()
            if not sku_clean:
                continue
            variant = (
                ProductVariant.objects.filter(sku__iexact=sku_clean).first()
                or ProductVariant.objects.filter(pk=sku_clean if sku_clean.isdigit() else 0).first()
            )
            if not variant:
                messages.error(request, _("Variant %(sku)s not found.") % {"sku": sku_clean})
                return render(
                    request,
                    "backoffice/procurement/order_form.html",
                    {"title": _("New Purchase Order"), "suppliers": suppliers},
                )

            qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 1
            cost = unit_costs[i] if i < len(unit_costs) and unit_costs[i] else None
            note = item_notes[i] if i < len(item_notes) else ""
            items.append(
                {
                    "variant": variant,
                    "quantity": qty,
                    "unit_cost": cost,
                    "notes": note,
                }
            )

        try:
            po = create_purchase_order(
                supplier=supplier,
                created_by=request.user,
                items=items,
                currency=currency,
                expected_delivery_date=delivery_date or None,
                notes=notes,
            )
            messages.success(
                request,
                _("Draft purchase order %(ref)s created.") % {"ref": po.reference},
            )
            return redirect("backoffice:purchase_order_detail", pk=po.pk)
        except Exception as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    context = {
        "title": _("New Purchase Order"),
        "active_tab": "procurement",
        "suppliers": suppliers,
        "po": None,
    }
    return render(request, "backoffice/procurement/order_form.html", context)


@backoffice_access(PURCHASING_VIEW)
def purchase_order_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Detail view for a purchase order, including line items and receipts history."""
    po = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier", "created_by", "approved_by", "cancelled_by")
        .prefetch_related(
            "items",
            "items__variant",
            "items__variant__product",
            "receipts",
            "receipts__received_by",
            "receipts__items",
            "receipts__items__line_item",
        ),
        pk=pk,
    )

    can_manage = has(request.user, PURCHASING_MANAGE)
    can_approve = has(request.user, PURCHASING_APPROVE)

    context = {
        "title": f"{po.reference} - {po.supplier.name}",
        "active_tab": "procurement",
        "po": po,
        "items": po.items.all(),
        "receipts": po.receipts.all(),
        "can_manage": can_manage,
        "can_approve": can_approve,
    }
    return render(request, "backoffice/procurement/order_detail.html", context)


@backoffice_access(PURCHASING_MANAGE)
def purchase_order_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit draft purchase order items and metadata."""
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if not po.can_edit_items:
        messages.error(
            request,
            _("Cannot edit order %(ref)s in %(st)s status.")
            % {"ref": po.reference, "st": po.get_status_display()},
        )
        return redirect("backoffice:purchase_order_detail", pk=po.pk)

    if request.method == "POST":
        delivery_date = request.POST.get("expected_delivery_date")
        notes = request.POST.get("notes")

        skus = request.POST.getlist("variant_sku[]")
        quantities = request.POST.getlist("quantity[]")
        unit_costs = request.POST.getlist("unit_cost[]")
        item_notes = request.POST.getlist("item_notes[]")

        items: list[dict[str, Any]] = []
        for i, sku in enumerate(skus):
            sku_clean = sku.strip()
            if not sku_clean:
                continue
            variant = (
                ProductVariant.objects.filter(sku__iexact=sku_clean).first()
                or ProductVariant.objects.filter(pk=sku_clean if sku_clean.isdigit() else 0).first()
            )
            if not variant:
                messages.error(request, _("Variant %(sku)s not found.") % {"sku": sku_clean})
                return redirect("backoffice:purchase_order_edit", pk=po.pk)

            qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 1
            cost = unit_costs[i] if i < len(unit_costs) and unit_costs[i] else None
            note = item_notes[i] if i < len(item_notes) else ""
            items.append(
                {
                    "variant": variant,
                    "quantity": qty,
                    "unit_cost": cost,
                    "notes": note,
                }
            )

        try:
            update_purchase_order(
                po,
                items=items if items else None,
                expected_delivery_date=delivery_date or None,
                notes=notes,
                user=request.user,
            )
            messages.success(request, _("Purchase order %(ref)s updated.") % {"ref": po.reference})
            return redirect("backoffice:purchase_order_detail", pk=po.pk)
        except Exception as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    context = {
        "title": _("Edit %(ref)s") % {"ref": po.reference},
        "active_tab": "procurement",
        "po": po,
        "items": po.items.all(),
        "suppliers": [po.supplier],
    }
    return render(request, "backoffice/procurement/order_form.html", context)


@backoffice_access(PURCHASING_MANAGE)
def purchase_order_action(request: HttpRequest, pk: int) -> HttpResponse:
    """Execute purchase order lifecycle status transitions."""
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if request.method != "POST":
        return redirect("backoffice:purchase_order_detail", pk=po.pk)

    action = request.POST.get("action")
    try:
        if action == "submit":
            submit_for_approval(po, user=request.user)
            messages.success(request, _("Order submitted for approval."))
        elif action == "approve":
            if not has(request.user, PURCHASING_APPROVE):
                messages.error(request, _("You do not have permission to approve purchase orders."))
                return redirect("backoffice:purchase_order_detail", pk=po.pk)
            approve_purchase_order(po, user=request.user)
            messages.success(request, _("Purchase order approved."))
        elif action == "send":
            send_purchase_order(po, user=request.user)
            messages.success(request, _("Purchase order marked sent to supplier."))
        elif action == "cancel":
            reason = request.POST.get("reason", "").strip()
            cancel_purchase_order(po, user=request.user, reason=reason)
            messages.success(request, _("Purchase order cancelled."))
        else:
            messages.error(request, _("Unknown action '%(act)s'.") % {"act": action})
    except ProcurementError as pe:
        messages.error(request, str(pe))
    except Exception as ex:
        msg = ex.message if hasattr(ex, "message") else str(ex)
        messages.error(request, msg)

    return redirect("backoffice:purchase_order_detail", pk=po.pk)


@backoffice_access(PURCHASING_MANAGE)
def purchase_order_receive(request: HttpRequest, pk: int) -> HttpResponse:
    """Receive physical delivery against purchase order with over-receipt protection."""
    po = get_object_or_404(
        PurchaseOrder.objects.prefetch_related("items", "items__variant", "items__variant__product"),
        pk=pk,
    )

    if not po.can_receive:
        messages.error(
            request,
            _("Cannot receive delivery for order %(ref)s in %(st)s status.")
            % {"ref": po.reference, "st": po.get_status_display()},
        )
        return redirect("backoffice:purchase_order_detail", pk=po.pk)

    if request.method == "POST":
        delivery_reference = request.POST.get("delivery_reference", "").strip()
        receiving_notes = request.POST.get("notes", "").strip()

        items: list[dict[str, Any]] = []
        for line_item in po.items.all():
            qty_acc_str = request.POST.get(f"accepted_{line_item.pk}", "0").strip()
            qty_dmg_str = request.POST.get(f"damaged_{line_item.pk}", "0").strip()
            item_note = request.POST.get(f"note_{line_item.pk}", "").strip()

            try:
                qty_acc = max(0, int(qty_acc_str))
                qty_dmg = max(0, int(qty_dmg_str))
            except ValueError:
                qty_acc = 0
                qty_dmg = 0

            if qty_acc > 0 or qty_dmg > 0:
                items.append(
                    {
                        "line_item_id": line_item.pk,
                        "quantity_received": qty_acc,
                        "quantity_damaged": qty_dmg,
                        "notes": item_note,
                    }
                )

        try:
            receipt = receive_purchase_order_delivery(
                po,
                delivery_reference=delivery_reference,
                items=items,
                user=request.user,
                notes=receiving_notes,
            )
            messages.success(
                request,
                _("Delivery %(ref)s received successfully and credited to warehouse inventory.")
                % {"ref": receipt.delivery_reference},
            )
            return redirect("backoffice:purchase_order_detail", pk=po.pk)
        except (ProcurementError, ValidationError) as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    context = {
        "title": _("Receive Delivery — %(ref)s") % {"ref": po.reference},
        "active_tab": "procurement",
        "po": po,
        "items": po.items.all(),
    }
    return render(request, "backoffice/procurement/order_receive.html", context)


@backoffice_access(PURCHASING_VIEW)
def purchase_order_print(request: HttpRequest, pk: int) -> HttpResponse:
    """Printable purchase order slip for vendor or internal records."""
    po = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier", "created_by", "approved_by")
        .prefetch_related("items", "items__variant", "items__variant__product"),
        pk=pk,
    )
    context = {
        "title": f"Print {po.reference}",
        "po": po,
        "items": po.items.all(),
        "today": timezone.localdate(),
    }
    return render(request, "backoffice/procurement/order_print.html", context)
