"""Back Office views for Procurement, Suppliers, and Purchase Order operations (Phase 45)."""

from __future__ import annotations

import csv
import datetime
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
    CASHFLOW_MANAGE,
    CASHFLOW_VIEW,
    EXPENSES_APPROVE,
    EXPENSES_MANAGE,
    EXPENSES_VIEW,
    INVOICES_APPROVE,
    INVOICES_MANAGE,
    INVOICES_VIEW,
    PAYMENTS_AP_RECORD,
    PROFITABILITY_VIEW,
    PURCHASING_APPROVE,
    PURCHASING_MANAGE,
    PURCHASING_VIEW,
    WORKING_CAPITAL_VIEW,
    backoffice_access,
    has,
)
from apps.catalog.models import ProductVariant
from apps.procurement.models import (
    CashAccount,
    CashAccountAdjustment,
    CashAccountType,
    CashForecastScenario,
    CreditNoteStatus,
    ExpenseBudget,
    ExpenseCategory,
    ExpenseStatus,
    InvoiceMatchingStatus,
    InvoiceMatchRecord,
    OperatingExpense,
    PaymentMethod,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderStatus,
    ScenarioAdjustment,
    Supplier,
    SupplierCreditNote,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierPaymentStatus,
    SupplierVariant,
)
from apps.procurement.selectors import (
    generate_cash_flow_forecast,
    get_accounts_payable_dashboard_metrics,
    get_cash_accounts_queryset,
    get_cash_flow_alerts,
    get_cash_flow_dashboard_metrics,
    get_expense_budget_vs_actual,
    get_operating_expenses_queryset,
    get_procurement_dashboard_metrics,
    get_product_profitability_metrics,
    get_profitability_metrics,
    get_purchase_orders_queryset,
    get_supplier_ap_summary,
    get_supplier_credit_notes_queryset,
    get_supplier_invoices_queryset,
    get_supplier_payments_queryset,
    get_supplier_performance,
    get_suppliers_queryset,
    get_working_capital_metrics,
)
from apps.procurement.services import (
    CreditNoteApplicationError,
    DiscrepancyApprovalRequired,
    DuplicateInvoiceError,
    DuplicateReceiptError,
    InsufficientInvoiceBalanceError,
    InvalidExpenseOperationError,
    InvalidInvoiceOperationError,
    InvalidStatusTransition,
    OrderImmutableError,
    OverReceiptError,
    PaymentReversalError,
    ProcurementError,
    SeparationOfDutiesError,
    add_scenario_adjustment,
    adjust_cash_account_balance,
    apply_credit_note_to_invoice,
    approve_operating_expense,
    approve_purchase_order,
    approve_supplier_invoice,
    cancel_purchase_order,
    create_cash_account,
    create_forecast_scenario,
    create_operating_expense,
    create_or_update_budget,
    create_purchase_order,
    create_supplier,
    create_supplier_credit_note,
    create_supplier_invoice,
    delete_scenario_adjustment,
    dispute_supplier_invoice,
    pay_operating_expense,
    perform_three_way_match,
    receive_purchase_order_delivery,
    record_supplier_payment,
    reject_operating_expense,
    reverse_supplier_payment,
    send_purchase_order,
    submit_for_approval,
    submit_operating_expense,
    submit_supplier_invoice,
    update_cash_account,
    update_operating_expense,
    update_purchase_order,
    update_supplier,
    update_supplier_invoice,
    void_operating_expense,
    void_supplier_invoice,
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


# =============================================================================
# Phase 46: Accounts Payable & Supplier Invoice Back Office Views
# =============================================================================


@backoffice_access(INVOICES_VIEW)
def accounts_payable_dashboard(request: HttpRequest) -> HttpResponse:
    """Accounts Payable Intelligence Dashboard displaying liabilities, aging, and KPIs."""
    supplier_id_raw = request.GET.get("supplier")
    supplier_id = int(supplier_id_raw) if supplier_id_raw and supplier_id_raw.isdigit() else None

    metrics = get_accounts_payable_dashboard_metrics(supplier_id=supplier_id)
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")

    context = {
        "title": _("Accounts Payable & Invoice Intelligence"),
        "active_tab": "accounts_payable",
        "metrics": metrics,
        "suppliers": suppliers,
        "selected_supplier_id": supplier_id,
    }
    return render(request, "backoffice/procurement/ap_dashboard.html", context)


@backoffice_access(INVOICES_VIEW)
def supplier_invoices_list(request: HttpRequest) -> HttpResponse:
    """Searchable, filterable directory of supplier invoices."""
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip() or None
    supplier_id_raw = request.GET.get("supplier", "").strip()
    supplier_id = int(supplier_id_raw) if supplier_id_raw.isdigit() else None
    overdue_only = request.GET.get("overdue") == "1"

    invoices_qs = get_supplier_invoices_queryset(
        q=q,
        supplier_id=supplier_id,
        status=status,
        overdue_only=overdue_only,
    )

    paginator = Paginator(invoices_qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    suppliers = Supplier.objects.filter(is_active=True).order_by("name")

    context = {
        "title": _("Supplier Invoices & Bills"),
        "active_tab": "accounts_payable",
        "invoices_page": page_obj,
        "q": q,
        "selected_status": status,
        "selected_supplier_id": supplier_id,
        "overdue_only": overdue_only,
        "suppliers": suppliers,
        "status_choices": SupplierInvoiceStatus.choices,
    }
    return render(request, "backoffice/procurement/invoice_list.html", context)


@backoffice_access(INVOICES_MANAGE)
def supplier_invoice_create(request: HttpRequest) -> HttpResponse:
    """Record a new supplier bill with line items and duplicate safeguards."""
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")
    selected_supplier_id = request.GET.get("supplier") or request.POST.get("supplier")
    selected_supplier = None
    if selected_supplier_id and str(selected_supplier_id).isdigit():
        selected_supplier = Supplier.objects.filter(pk=int(selected_supplier_id)).first()

    po_options = []
    if selected_supplier:
        po_options = selected_supplier.purchase_orders.exclude(
            status__in=[PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.CANCELLED]
        ).order_by("-created_at")

    selected_po_id = request.GET.get("purchase_order") or request.POST.get("purchase_order")
    selected_po = None
    if selected_po_id and str(selected_po_id).isdigit():
        selected_po = PurchaseOrder.objects.filter(pk=int(selected_po_id)).prefetch_related("items__variant__product").first()

    if request.method == "POST":
        supplier_id = request.POST.get("supplier")
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        invoice_number = request.POST.get("invoice_number", "").strip()
        invoice_date_raw = request.POST.get("invoice_date")
        due_date_raw = request.POST.get("due_date")
        currency = request.POST.get("currency", supplier.currency)
        supplier_ref = request.POST.get("supplier_reference", "").strip()
        internal_notes = request.POST.get("internal_notes", "").strip()
        tax_amount = Decimal(request.POST.get("tax_amount", "0.00") or "0.00")
        shipping_amount = Decimal(request.POST.get("shipping_amount", "0.00") or "0.00")
        discount_amount = Decimal(request.POST.get("discount_amount", "0.00") or "0.00")
        is_dup_override = request.POST.get("is_duplicate_override") == "on"
        dup_override_reason = request.POST.get("duplicate_override_reason", "").strip()
        attachment = request.FILES.get("attachment")

        po_id = request.POST.get("purchase_order")
        po = PurchaseOrder.objects.filter(pk=po_id).first() if po_id and str(po_id).isdigit() else None

        # Parse dynamic line items
        items_data = []
        item_indices = request.POST.getlist("item_index")
        for idx in item_indices:
            desc = request.POST.get(f"line_desc_{idx}", "").strip()
            qty = int(request.POST.get(f"line_qty_{idx}", 1) or 1)
            cost = Decimal(request.POST.get(f"line_cost_{idx}", "0.00") or "0.00")
            po_item_id = request.POST.get(f"line_po_item_{idx}")
            po_item = PurchaseOrderItem.objects.filter(pk=po_item_id).first() if po_item_id and str(po_item_id).isdigit() else None
            variant_id = request.POST.get(f"line_variant_{idx}")
            variant = ProductVariant.objects.filter(pk=variant_id).first() if variant_id and str(variant_id).isdigit() else None

            if desc or po_item:
                items_data.append({
                    "description": desc,
                    "quantity": qty,
                    "unit_cost": cost,
                    "po_item": po_item,
                    "variant": variant or (po_item.variant if po_item else None),
                    "notes": request.POST.get(f"line_notes_{idx}", "").strip(),
                })

        try:
            invoice = create_supplier_invoice(
                supplier=supplier,
                invoice_number=invoice_number,
                invoice_date=invoice_date_raw or timezone.localdate(),
                due_date=due_date_raw or (timezone.localdate() + timezone.timedelta(days=30)),
                currency=currency,
                purchase_order=po,
                supplier_reference=supplier_ref,
                tax_amount=tax_amount,
                shipping_amount=shipping_amount,
                discount_amount=discount_amount,
                items_data=items_data,
                attachment=attachment,
                internal_notes=internal_notes,
                is_duplicate_override=is_dup_override,
                duplicate_override_reason=dup_override_reason,
                user=request.user,
            )
            messages.success(request, _("Supplier invoice %(num)s recorded successfully as draft.") % {"num": invoice.invoice_number})
            return redirect("backoffice:supplier_invoice_detail", pk=invoice.pk)
        except DuplicateInvoiceError as ex:
            messages.error(request, str(ex))
        except (ProcurementError, ValidationError) as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    context = {
        "title": _("Record Supplier Invoice"),
        "active_tab": "accounts_payable",
        "suppliers": suppliers,
        "selected_supplier": selected_supplier,
        "po_options": po_options,
        "selected_po": selected_po,
        "today": timezone.localdate(),
        "default_due_date": timezone.localdate() + timezone.timedelta(days=30),
    }
    return render(request, "backoffice/procurement/invoice_form.html", context)


@backoffice_access(INVOICES_VIEW)
def supplier_invoice_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Detailed invoice dossier with line items, 3-way matching scorecard, and audit history."""
    invoice = get_object_or_404(
        SupplierInvoice.objects.select_related("supplier", "purchase_order", "match_record", "created_by", "approved_by", "voided_by")
        .prefetch_related("lines", "lines__po_item", "lines__variant", "payment_allocations__payment", "credit_applications__credit_note"),
        pk=pk,
    )

    can_manage = has(request.user, INVOICES_MANAGE)
    can_approve = has(request.user, INVOICES_APPROVE)
    can_pay = has(request.user, PAYMENTS_AP_RECORD)

    # Check separation of duties: preparer cannot approve
    is_preparer = bool(invoice.created_by_id and invoice.created_by_id == request.user.pk)
    can_approve_this_invoice = can_approve and (not is_preparer or request.user.is_superuser)

    match_record = getattr(invoice, "match_record", None)

    context = {
        "title": f"Invoice {invoice.invoice_number} ({invoice.supplier.name})",
        "active_tab": "accounts_payable",
        "invoice": invoice,
        "lines": invoice.lines.all(),
        "match_record": match_record,
        "allocations": invoice.payment_allocations.all(),
        "credits": invoice.credit_applications.all(),
        "can_manage": can_manage,
        "can_approve": can_approve,
        "can_approve_this_invoice": can_approve_this_invoice,
        "can_pay": can_pay,
        "is_preparer": is_preparer,
    }
    return render(request, "backoffice/procurement/invoice_detail.html", context)


@backoffice_access(INVOICES_MANAGE)
def supplier_invoice_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit an existing draft supplier invoice."""
    invoice = get_object_or_404(
        SupplierInvoice.objects.select_related("supplier", "purchase_order").prefetch_related("lines"),
        pk=pk,
    )

    if not invoice.can_edit:
        messages.error(request, _("Only draft invoices can be edited."))
        return redirect("backoffice:supplier_invoice_detail", pk=invoice.pk)

    suppliers = Supplier.objects.filter(is_active=True).order_by("name")
    po_options = invoice.supplier.purchase_orders.exclude(
        status__in=[PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.CANCELLED]
    ).order_by("-created_at")

    if request.method == "POST":
        invoice_number = request.POST.get("invoice_number", "").strip()
        invoice_date_raw = request.POST.get("invoice_date")
        due_date_raw = request.POST.get("due_date")
        currency = request.POST.get("currency", invoice.currency)
        supplier_ref = request.POST.get("supplier_reference", "").strip()
        internal_notes = request.POST.get("internal_notes", "").strip()
        tax_amount = Decimal(request.POST.get("tax_amount", "0.00") or "0.00")
        shipping_amount = Decimal(request.POST.get("shipping_amount", "0.00") or "0.00")
        discount_amount = Decimal(request.POST.get("discount_amount", "0.00") or "0.00")
        attachment = request.FILES.get("attachment")

        po_id = request.POST.get("purchase_order")
        po = PurchaseOrder.objects.filter(pk=po_id).first() if po_id and str(po_id).isdigit() else None

        items_data = []
        item_indices = request.POST.getlist("item_index")
        for idx in item_indices:
            desc = request.POST.get(f"line_desc_{idx}", "").strip()
            qty = int(request.POST.get(f"line_qty_{idx}", 1) or 1)
            cost = Decimal(request.POST.get(f"line_cost_{idx}", "0.00") or "0.00")
            po_item_id = request.POST.get(f"line_po_item_{idx}")
            po_item = PurchaseOrderItem.objects.filter(pk=po_item_id).first() if po_item_id and str(po_item_id).isdigit() else None
            variant_id = request.POST.get(f"line_variant_{idx}")
            variant = ProductVariant.objects.filter(pk=variant_id).first() if variant_id and str(variant_id).isdigit() else None

            if desc or po_item:
                items_data.append({
                    "description": desc,
                    "quantity": qty,
                    "unit_cost": cost,
                    "po_item": po_item,
                    "variant": variant or (po_item.variant if po_item else None),
                    "notes": request.POST.get(f"line_notes_{idx}", "").strip(),
                })

        try:
            update_supplier_invoice(
                invoice=invoice,
                invoice_number=invoice_number,
                invoice_date=invoice_date_raw,
                due_date=due_date_raw,
                currency=currency,
                purchase_order=po,
                supplier_reference=supplier_ref,
                tax_amount=tax_amount,
                shipping_amount=shipping_amount,
                discount_amount=discount_amount,
                items_data=items_data,
                attachment=attachment,
                internal_notes=internal_notes,
                user=request.user,
            )
            messages.success(request, _("Invoice %(num)s updated successfully.") % {"num": invoice.invoice_number})
            return redirect("backoffice:supplier_invoice_detail", pk=invoice.pk)
        except (ProcurementError, ValidationError) as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    context = {
        "title": f"Edit Invoice {invoice.invoice_number}",
        "active_tab": "accounts_payable",
        "invoice": invoice,
        "lines": invoice.lines.all(),
        "suppliers": suppliers,
        "selected_supplier": invoice.supplier,
        "po_options": po_options,
        "selected_po": invoice.purchase_order,
    }
    return render(request, "backoffice/procurement/invoice_form.html", context)


@backoffice_access(INVOICES_VIEW)
def supplier_invoice_action(request: HttpRequest, pk: int) -> HttpResponse:
    """Handle lifecycle state machine operations (submit, approve, dispute, void, match)."""
    if request.method != "POST":
        return HttpResponseRedirect(reverse("backoffice:supplier_invoice_detail", kwargs={"pk": pk}))

    invoice = get_object_or_404(SupplierInvoice, pk=pk)
    action = request.POST.get("action", "").strip()

    try:
        if action == "submit":
            if not has(request.user, INVOICES_MANAGE):
                raise Http404()
            submit_supplier_invoice(invoice, user=request.user)
            messages.success(request, _("Invoice %(num)s submitted for approval.") % {"num": invoice.invoice_number})

        elif action == "approve":
            if not has(request.user, INVOICES_APPROVE):
                raise Http404()
            override_discrepancies = request.POST.get("override_discrepancies") == "on"
            override_reason = request.POST.get("override_reason", "").strip()
            approve_supplier_invoice(
                invoice,
                user=request.user,
                override_discrepancies=override_discrepancies,
                override_reason=override_reason,
            )
            messages.success(request, _("Invoice %(num)s approved successfully. Accounts payable liability recognized.") % {"num": invoice.invoice_number})

        elif action == "dispute":
            if not (has(request.user, INVOICES_MANAGE) or has(request.user, INVOICES_APPROVE)):
                raise Http404()
            reason = request.POST.get("dispute_reason", "").strip()
            dispute_supplier_invoice(invoice, user=request.user, reason=reason)
            messages.warning(request, _("Invoice %(num)s marked as disputed.") % {"num": invoice.invoice_number})

        elif action == "void":
            if not has(request.user, INVOICES_APPROVE):
                raise Http404()
            reason = request.POST.get("void_reason", "").strip()
            void_supplier_invoice(invoice, user=request.user, reason=reason)
            messages.info(request, _("Invoice %(num)s voided successfully.") % {"num": invoice.invoice_number})

        elif action == "match":
            if not (has(request.user, INVOICES_VIEW)):
                raise Http404()
            match_rec = perform_three_way_match(invoice)
            messages.info(request, _("Three-way match evaluated: %(stat)s.") % {"stat": match_rec.get_status_display()})

        else:
            messages.error(request, _("Unknown invoice action."))

    except SeparationOfDutiesError as ex:
        messages.error(request, str(ex))
    except DiscrepancyApprovalRequired as ex:
        messages.error(request, str(ex))
    except (ProcurementError, ValidationError) as ex:
        msg = ex.message if hasattr(ex, "message") else str(ex)
        messages.error(request, msg)

    return redirect("backoffice:supplier_invoice_detail", pk=invoice.pk)


@backoffice_access(INVOICES_VIEW)
def supplier_payments_list(request: HttpRequest) -> HttpResponse:
    """Directory of accounts payable supplier disbursements and settlements."""
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip() or None
    supplier_id_raw = request.GET.get("supplier", "").strip()
    supplier_id = int(supplier_id_raw) if supplier_id_raw.isdigit() else None

    payments_qs = get_supplier_payments_queryset(
        q=q,
        supplier_id=supplier_id,
        status=status,
    )

    paginator = Paginator(payments_qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")

    context = {
        "title": _("Supplier Disbursements (Accounts Payable)"),
        "active_tab": "accounts_payable",
        "payments_page": page_obj,
        "q": q,
        "selected_status": status,
        "selected_supplier_id": supplier_id,
        "suppliers": suppliers,
        "can_record_payment": has(request.user, PAYMENTS_AP_RECORD),
    }
    return render(request, "backoffice/procurement/payment_list.html", context)


@backoffice_access(PAYMENTS_AP_RECORD)
def supplier_payment_create(request: HttpRequest) -> HttpResponse:
    """Record an internal supplier disbursement and allocate it across approved invoices."""
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")
    selected_supplier_id = request.GET.get("supplier") or request.POST.get("supplier")
    selected_supplier = None
    open_invoices = []

    if selected_supplier_id and str(selected_supplier_id).isdigit():
        selected_supplier = Supplier.objects.filter(pk=int(selected_supplier_id)).first()
        if selected_supplier:
            open_invoices = [
                inv for inv in selected_supplier.invoices.filter(
                    status__in=[SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID]
                ).order_by("due_date")
                if inv.outstanding_balance > Decimal("0.00")
            ]

    if request.method == "POST":
        supplier_id = request.POST.get("supplier")
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        amount_raw = request.POST.get("amount", "0.00")
        amount = Decimal(str(amount_raw or "0.00"))
        payment_method = request.POST.get("payment_method", PaymentMethod.BANK_TRANSFER)
        payment_date_raw = request.POST.get("payment_date")
        bank_ref = request.POST.get("bank_transaction_reference", "").strip()
        notes = request.POST.get("notes", "").strip()

        allocations_data = []
        for inv in open_invoices:
            alloc_val = request.POST.get(f"alloc_invoice_{inv.pk}", "").strip()
            if alloc_val:
                try:
                    alloc_amt = Decimal(alloc_val)
                    if alloc_amt > Decimal("0.00"):
                        allocations_data.append({"invoice_id": inv.pk, "amount": alloc_amt})
                except (ValueError, TypeError):
                    pass

        try:
            payment = record_supplier_payment(
                supplier=supplier,
                amount=amount,
                payment_method=payment_method,
                payment_date=payment_date_raw or timezone.localdate(),
                bank_transaction_reference=bank_ref,
                allocations_data=allocations_data,
                notes=notes,
                user=request.user,
            )
            messages.success(request, _("Disbursement %(ref)s recorded successfully.") % {"ref": payment.payment_reference})
            return redirect("backoffice:supplier_payments")
        except (ProcurementError, ValidationError) as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    context = {
        "title": _("Record Supplier Payment"),
        "active_tab": "accounts_payable",
        "suppliers": suppliers,
        "selected_supplier": selected_supplier,
        "open_invoices": open_invoices,
        "payment_methods": PaymentMethod.choices,
        "today": timezone.localdate(),
    }
    return render(request, "backoffice/procurement/payment_form.html", context)


@backoffice_access(PAYMENTS_AP_RECORD)
def supplier_payment_reverse(request: HttpRequest, pk: int) -> HttpResponse:
    """Reverse a completed supplier payment with auditable restoration of invoice balances."""
    if request.method != "POST":
        return HttpResponseRedirect(reverse("backoffice:supplier_payments"))

    payment = get_object_or_404(SupplierPayment, pk=pk)
    reason = request.POST.get("reversal_reason", "").strip()

    try:
        reverse_supplier_payment(payment, user=request.user, reason=reason)
        messages.warning(request, _("Payment %(ref)s reversed successfully.") % {"ref": payment.payment_reference})
    except (ProcurementError, ValidationError) as ex:
        msg = ex.message if hasattr(ex, "message") else str(ex)
        messages.error(request, msg)

    return redirect("backoffice:supplier_payments")


@backoffice_access(INVOICES_VIEW)
def supplier_credit_notes_list(request: HttpRequest) -> HttpResponse:
    """List of supplier credit notes."""
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip() or None
    supplier_id_raw = request.GET.get("supplier", "").strip()
    supplier_id = int(supplier_id_raw) if supplier_id_raw.isdigit() else None

    credit_notes_qs = get_supplier_credit_notes_queryset(
        q=q,
        supplier_id=supplier_id,
        status=status,
    )

    paginator = Paginator(credit_notes_qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")

    context = {
        "title": _("Supplier Credit Notes"),
        "active_tab": "accounts_payable",
        "credit_notes_page": page_obj,
        "q": q,
        "selected_status": status,
        "selected_supplier_id": supplier_id,
        "suppliers": suppliers,
        "can_manage": has(request.user, INVOICES_MANAGE),
    }
    return render(request, "backoffice/procurement/credit_note_list.html", context)


@backoffice_access(INVOICES_MANAGE)
def supplier_credit_note_create(request: HttpRequest) -> HttpResponse:
    """Record an approved credit note from a supplier."""
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")
    selected_supplier_id = request.GET.get("supplier") or request.POST.get("supplier")
    selected_supplier = None
    if selected_supplier_id and str(selected_supplier_id).isdigit():
        selected_supplier = Supplier.objects.filter(pk=int(selected_supplier_id)).first()

    if request.method == "POST":
        supplier_id = request.POST.get("supplier")
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        cn_number = request.POST.get("credit_note_number", "").strip()
        amount = Decimal(request.POST.get("amount", "0.00") or "0.00")
        reason = request.POST.get("reason", "").strip()
        credit_date_raw = request.POST.get("credit_date")
        notes = request.POST.get("notes", "").strip()
        invoice_id = request.POST.get("invoice")
        invoice = SupplierInvoice.objects.filter(pk=invoice_id).first() if invoice_id and str(invoice_id).isdigit() else None

        try:
            cn = create_supplier_credit_note(
                supplier=supplier,
                credit_note_number=cn_number,
                amount=amount,
                reason=reason,
                invoice=invoice,
                credit_date=credit_date_raw or timezone.localdate(),
                currency=supplier.currency,
                notes=notes,
                user=request.user,
            )
            messages.success(request, _("Credit note %(num)s recorded successfully.") % {"num": cn.credit_note_number})
            return redirect("backoffice:supplier_credit_notes")
        except (ProcurementError, ValidationError) as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    context = {
        "title": _("Record Supplier Credit Note"),
        "active_tab": "accounts_payable",
        "suppliers": suppliers,
        "selected_supplier": selected_supplier,
        "today": timezone.localdate(),
    }
    return render(request, "backoffice/procurement/credit_note_form.html", context)


@backoffice_access(INVOICES_MANAGE)
def supplier_credit_note_apply(request: HttpRequest, pk: int) -> HttpResponse:
    """Apply an unapplied credit note amount against an approved invoice."""
    if request.method != "POST":
        return HttpResponseRedirect(reverse("backoffice:supplier_credit_notes"))

    credit_note = get_object_or_404(SupplierCreditNote, pk=pk)
    invoice_id = request.POST.get("invoice_id")
    invoice = get_object_or_404(SupplierInvoice, pk=invoice_id)
    amount_raw = request.POST.get("amount", "0.00")

    try:
        amount = Decimal(amount_raw)
        apply_credit_note_to_invoice(
            credit_note=credit_note,
            invoice=invoice,
            amount=amount,
            user=request.user,
        )
        messages.success(request, _("Credit %(num)s applied to invoice %(inv)s successfully.") % {
            "num": credit_note.credit_note_number,
            "inv": invoice.invoice_number,
        })
    except (ProcurementError, ValidationError) as ex:
        msg = ex.message if hasattr(ex, "message") else str(ex)
        messages.error(request, msg)

    return redirect("backoffice:supplier_credit_notes")


# =============================================================================
# Phase 48: Cash Flow Forecasting & Treasury Views
# =============================================================================


@backoffice_access(CASHFLOW_VIEW)
def cash_flow_dashboard(request: HttpRequest) -> HttpResponse:
    """Cash Flow Forecasting & Working Capital executive dashboard."""
    currency = request.GET.get("currency", "USD").upper()
    horizon_raw = request.GET.get("horizon", "30")
    try:
        horizon_days = int(horizon_raw)
        if horizon_days not in (30, 60, 90):
            horizon_days = 30
    except ValueError:
        horizon_days = 30

    scenario_id_raw = request.GET.get("scenario_id")
    scenario_id = int(scenario_id_raw) if scenario_id_raw and scenario_id_raw.isdigit() else None
    scenario_mode = request.GET.get("scenario_mode", "expected")
    if scenario_mode not in ("conservative", "expected", "optimistic"):
        scenario_mode = "expected"

    data = get_cash_flow_dashboard_metrics(
        currency=currency,
        horizon_days=horizon_days,
        scenario_id=scenario_id,
        scenario_mode=scenario_mode,
    )

    can_manage = has(request.user, CASHFLOW_MANAGE)

    context = {
        "title": _("Cash Flow & Working Capital Intelligence"),
        "active_tab": "cash_flow",
        "can_manage": can_manage,
        **data,
    }
    return render(request, "backoffice/procurement/cash_flow_dashboard.html", context)


@backoffice_access(CASHFLOW_VIEW)
def cash_accounts_list(request: HttpRequest) -> HttpResponse:
    """Directory of cash accounts, bank balances, and safety thresholds."""
    currency = request.GET.get("currency", "USD").upper()
    accounts = get_cash_accounts_queryset(currency=currency, active_only=False)
    currencies = list(CashAccount.objects.values_list("currency", flat=True).distinct()) or ["USD"]
    can_manage = has(request.user, CASHFLOW_MANAGE)

    total_balance = sum((acc.current_balance for acc in accounts if acc.is_active), Decimal("0.00"))

    context = {
        "title": _("Cash & Bank Accounts"),
        "active_tab": "cash_flow",
        "accounts": accounts,
        "currency": currency,
        "currencies": currencies,
        "total_balance": total_balance,
        "can_manage": can_manage,
    }
    return render(request, "backoffice/procurement/cash_accounts_list.html", context)


@backoffice_access(CASHFLOW_MANAGE)
def cash_account_create(request: HttpRequest) -> HttpResponse:
    """Create a new cash/treasury account."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        account_type = request.POST.get("account_type", CashAccountType.CHECKING)
        institution_name = request.POST.get("institution_name", "").strip()
        account_number_last4 = request.POST.get("account_number_last4", "").strip()
        currency = request.POST.get("currency", "USD").upper().strip()
        opening_balance = request.POST.get("opening_balance", "0.00")
        minimum_threshold = request.POST.get("minimum_threshold", "5000.00")
        description = request.POST.get("description", "").strip()

        try:
            account = create_cash_account(
                name=name,
                account_type=account_type,
                currency=currency,
                opening_balance=Decimal(opening_balance),
                minimum_threshold=Decimal(minimum_threshold),
                institution_name=institution_name,
                account_number_last4=account_number_last4,
                description=description,
                user=request.user,
            )
            messages.success(request, _("Cash account '%(name)s' created successfully.") % {"name": account.name})
            return redirect("backoffice:cash_accounts")
        except (ValidationError, Exception) as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    context = {
        "title": _("New Cash Account"),
        "active_tab": "cash_flow",
        "account_types": CashAccountType.choices,
        "today": timezone.localdate(),
    }
    return render(request, "backoffice/procurement/cash_account_form.html", context)


@backoffice_access(CASHFLOW_MANAGE)
def cash_account_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit cash account parameters, safety threshold, and active status."""
    account = get_object_or_404(CashAccount, pk=pk)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        account_type = request.POST.get("account_type")
        institution_name = request.POST.get("institution_name", "").strip()
        account_number_last4 = request.POST.get("account_number_last4", "").strip()
        minimum_threshold = request.POST.get("minimum_threshold", "5000.00")
        description = request.POST.get("description", "").strip()
        is_active = request.POST.get("is_active") == "on"

        try:
            update_cash_account(
                account,
                name=name,
                account_type=account_type,
                minimum_threshold=Decimal(minimum_threshold),
                institution_name=institution_name,
                account_number_last4=account_number_last4,
                description=description,
                is_active=is_active,
                user=request.user,
            )
            messages.success(request, _("Account '%(name)s' updated successfully.") % {"name": account.name})
            return redirect("backoffice:cash_accounts")
        except (ValidationError, Exception) as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    context = {
        "title": _("Edit Cash Account"),
        "active_tab": "cash_flow",
        "account": account,
        "account_types": CashAccountType.choices,
    }
    return render(request, "backoffice/procurement/cash_account_form.html", context)


@backoffice_access(CASHFLOW_MANAGE)
def cash_account_adjust(request: HttpRequest, pk: int) -> HttpResponse:
    """Record an audited balance adjustment or bank reconciliation."""
    if request.method != "POST":
        return redirect("backoffice:cash_accounts")

    account = get_object_or_404(CashAccount, pk=pk)
    amount_raw = request.POST.get("amount", "0.00")
    adjustment_type = request.POST.get("adjustment_type", "reconciliation")
    reason = request.POST.get("reason", "").strip()
    reference = request.POST.get("reference", "").strip()

    try:
        amount = Decimal(amount_raw)
        adj = adjust_cash_account_balance(
            account,
            amount=amount,
            adjustment_type=adjustment_type,
            reason=reason,
            reference=reference,
            user=request.user,
        )
        messages.success(
            request,
            _("Adjusted %(acc)s by %(curr)s %(amt)s. New balance: %(new)s") % {
                "acc": account.name,
                "curr": account.currency,
                "amt": amount,
                "new": account.current_balance,
            },
        )
    except (ValidationError, Exception) as ex:
        msg = ex.message if hasattr(ex, "message") else str(ex)
        messages.error(request, msg)

    return redirect("backoffice:cash_accounts")


@backoffice_access(CASHFLOW_VIEW)
def cash_forecast_scenarios_list(request: HttpRequest) -> HttpResponse:
    """Manage What-If liquidity simulation scenarios."""
    scenarios = CashForecastScenario.objects.all().order_by("-created_at")
    can_manage = has(request.user, CASHFLOW_MANAGE)

    context = {
        "title": _("What-If Planning Scenarios"),
        "active_tab": "cash_flow",
        "scenarios": scenarios,
        "can_manage": can_manage,
    }
    return render(request, "backoffice/procurement/scenarios_list.html", context)


@backoffice_access(CASHFLOW_MANAGE)
def cash_forecast_scenario_create(request: HttpRequest) -> HttpResponse:
    """Create a new What-If simulation scenario."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        horizon_days = int(request.POST.get("horizon_days", 30))

        try:
            scenario = create_forecast_scenario(
                name=name,
                description=description,
                horizon_days=horizon_days,
                user=request.user,
            )
            messages.success(request, _("Scenario '%(name)s' created.") % {"name": scenario.name})
            return redirect("backoffice:scenario_detail", pk=scenario.pk)
        except (ValidationError, Exception) as ex:
            msg = ex.message if hasattr(ex, "message") else str(ex)
            messages.error(request, msg)

    return redirect("backoffice:forecast_scenarios")


@backoffice_access(CASHFLOW_VIEW)
def cash_forecast_scenario_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Detail view of a What-If scenario with adjustment ledger and simulation preview."""
    scenario = get_object_or_404(CashForecastScenario, pk=pk)
    adjustments = scenario.adjustments.all().order_by("adjusted_date")
    can_manage = has(request.user, CASHFLOW_MANAGE)

    # Run side-by-side forecast comparison: Baseline vs Scenario
    baseline_forecast = generate_cash_flow_forecast(
        currency="USD",
        horizon_days=scenario.horizon_days,
        scenario=None,
        scenario_mode="expected",
    )
    simulated_forecast = generate_cash_flow_forecast(
        currency="USD",
        horizon_days=scenario.horizon_days,
        scenario=scenario,
        scenario_mode="expected",
    )

    open_invoices = SupplierInvoice.objects.filter(
        status__in=[SupplierInvoiceStatus.APPROVED, SupplierInvoiceStatus.PARTIALLY_PAID]
    ).order_by("due_date")[:20]

    context = {
        "title": f"Scenario: {scenario.name}",
        "active_tab": "cash_flow",
        "scenario": scenario,
        "adjustments": adjustments,
        "can_manage": can_manage,
        "baseline_forecast": baseline_forecast,
        "simulated_forecast": simulated_forecast,
        "open_invoices": open_invoices,
        "adjustment_types": ScenarioAdjustment.AdjustmentType.choices,
        "today": timezone.localdate(),
    }
    return render(request, "backoffice/procurement/scenario_detail.html", context)


@backoffice_access(CASHFLOW_MANAGE)
def scenario_adjustment_add(request: HttpRequest, pk: int) -> HttpResponse:
    """Add a simulated adjustment to a What-If scenario."""
    if request.method != "POST":
        return redirect("backoffice:scenario_detail", pk=pk)

    scenario = get_object_or_404(CashForecastScenario, pk=pk)
    adjustment_type = request.POST.get("adjustment_type")
    adjusted_date_raw = request.POST.get("adjusted_date")
    amount_raw = request.POST.get("amount", "0.01")
    notes = request.POST.get("notes", "").strip()
    invoice_id = request.POST.get("target_supplier_invoice")

    target_invoice = None
    if invoice_id and invoice_id.isdigit():
        target_invoice = SupplierInvoice.objects.filter(pk=int(invoice_id)).first()

    try:
        adjusted_date = datetime.date.fromisoformat(adjusted_date_raw)
        amount = Decimal(amount_raw)
        original_date = target_invoice.due_date if target_invoice else None

        add_scenario_adjustment(
            scenario=scenario,
            adjustment_type=adjustment_type,
            adjusted_date=adjusted_date,
            amount=amount,
            original_date=original_date,
            target_supplier_invoice=target_invoice,
            notes=notes,
            user=request.user,
        )
        messages.success(request, _("Adjustment added to %(scen)s.") % {"scen": scenario.name})
    except (ValueError, ValidationError, Exception) as ex:
        msg = ex.message if hasattr(ex, "message") else str(ex)
        messages.error(request, msg)

    return redirect("backoffice:scenario_detail", pk=pk)


@backoffice_access(CASHFLOW_MANAGE)
def scenario_adjustment_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove a simulated adjustment from a What-If scenario."""
    adjustment = get_object_or_404(ScenarioAdjustment, pk=pk)
    scenario_pk = adjustment.scenario.pk
    delete_scenario_adjustment(adjustment, user=request.user)
    messages.success(request, _("Simulated adjustment deleted."))
    return redirect("backoffice:scenario_detail", pk=scenario_pk)


def _sanitize_csv_cell(value: Any) -> Any:
    """Defend against CSV / formula injection (CWE-1236)."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


@backoffice_access(CASHFLOW_VIEW)
def cash_flow_forecast_export_csv(request: HttpRequest) -> HttpResponse:
    """Safe CSV export of daily cash flow forecast timeline."""
    currency = request.GET.get("currency", "USD").upper()
    horizon_raw = request.GET.get("horizon", "30")
    try:
        horizon_days = int(horizon_raw)
        if horizon_days not in (30, 60, 90):
            horizon_days = 30
    except ValueError:
        horizon_days = 30

    scenario_id_raw = request.GET.get("scenario_id")
    scenario_id = int(scenario_id_raw) if scenario_id_raw and scenario_id_raw.isdigit() else None
    scenario_mode = request.GET.get("scenario_mode", "expected")

    scenario = None
    if scenario_id:
        scenario = CashForecastScenario.objects.filter(pk=scenario_id, is_active=True).first()

    today = timezone.localdate()
    forecast = generate_cash_flow_forecast(
        as_of_date=today,
        currency=currency,
        horizon_days=horizon_days,
        scenario=scenario,
        scenario_mode=scenario_mode,
    )

    response = HttpResponse(content_type="text/csv")
    filename = f"cash_flow_forecast_{currency}_{horizon_days}d_{today.isoformat()}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        "Day",
        "Date",
        "Opening Balance",
        "Confirmed Inflows",
        "Projected Inflows",
        "Custom Inflows",
        "Total Inflows",
        "Committed AP Outflows",
        "Committed OpEx Outflows",
        "PO Commitments Outflows",
        "Recurring Outflows",
        "Custom Outflows",
        "Refund Reserve Outflows",
        "Total Outflows",
        "Net Movement",
        "Closing Balance",
        "Below Threshold",
        "Deficit",
    ])

    for row in forecast["timeline"]:
        writer.writerow([
            _sanitize_csv_cell(row["day_index"]),
            _sanitize_csv_cell(row["date"].isoformat()),
            _sanitize_csv_cell(str(row["opening_balance"])),
            _sanitize_csv_cell(str(row["confirmed_inflows"])),
            _sanitize_csv_cell(str(row["projected_inflows"])),
            _sanitize_csv_cell(str(row["custom_inflows"])),
            _sanitize_csv_cell(str(row["total_inflows"])),
            _sanitize_csv_cell(str(row["ap_outflows"])),
            _sanitize_csv_cell(str(row["opex_outflows"])),
            _sanitize_csv_cell(str(row["po_outflows"])),
            _sanitize_csv_cell(str(row["recurring_outflows"])),
            _sanitize_csv_cell(str(row["custom_outflows"])),
            _sanitize_csv_cell(str(row["refund_reserves"])),
            _sanitize_csv_cell(str(row["total_outflows"])),
            _sanitize_csv_cell(str(row["net_movement"])),
            _sanitize_csv_cell(str(row["closing_balance"])),
            _sanitize_csv_cell("YES" if row["is_below_threshold"] else "NO"),
            _sanitize_csv_cell("YES" if row["is_deficit"] else "NO"),
        ])

    return response


