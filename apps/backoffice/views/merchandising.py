"""Merchandising screens: products, inventory, drops, promotions, quests (Phase 16).

Mutations here are thin: catalogue state changes go through ``apps.catalog.services``,
stock through :func:`apps.inventory.services.adjust_stock`. Drops, promotions and quests
are **read-only** in the back office -- their windows and counters belong to the apps that
own them (and to ``/admin/``), so this layer reports what those apps decided rather than
re-deciding it.
"""

from __future__ import annotations

import csv

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.backoffice import selectors
from apps.backoffice.forms import CatalogBulkForm, InventoryAdjustForm
from apps.backoffice.permissions import (
    CATALOG_MANAGE,
    CATALOG_VIEW,
    INVENTORY_MANAGE,
    INVENTORY_VIEW,
    MARKETING_MANAGE,
    MARKETING_VIEW,
    backoffice_access,
    has,
)
from apps.backoffice.services import operations
from apps.backoffice.views.base import render_bo
from apps.catalog.models import Category, Product
from apps.inventory import intelligence as inventory_intel

__all__ = [
    "adjust_stock",
    "catalog_health_report",
    "catalog_remediate",
    "drops",
    "export_catalog_health_csv",
    "export_inventory_health_csv",
    "inventory",
    "inventory_health",
    "inventory_reconcile_action",
    "inventory_reconciliation",
    "inventory_restock",
    "merch_featured_collections",
    "merch_featured_products",
    "merch_product_add",
    "merch_product_remove",
    "merch_product_reorder",
    "merch_scheduled",
    "products",
    "products_bulk",
    "promotions",
    "quests",
]


#: Scope links offered on the promotions screen. The keys are the ones ``promotions()``
#: understands, so a link can never ask for a filter that does not exist.
PROMOTION_SCOPES = (
    ("", "All"),
    ("active", "Running"),
    ("scheduled", "Scheduled"),
    ("expired", "Expired"),
    ("limit", "Near limit"),
)


@backoffice_access(CATALOG_VIEW)
def products(request):
    """``/operations/products/`` -- catalogue health, filters and the bulk picker."""
    health = selectors.catalog_health()
    queryset = selectors.products(
        q=request.GET.get("q", ""),
        status=request.GET.get("status", ""),
        issue=request.GET.get("issue", ""),
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/products.html",
        active="products",
        page=selectors.paginate(request, queryset),
        health=health,
        bulk_form=CatalogBulkForm(),
        can_manage=has(request.user, CATALOG_MANAGE),
        filters={
            "q": request.GET.get("q", ""),
            "status": request.GET.get("status", ""),
            "issue": request.GET.get("issue", ""),
            "sort": request.GET.get("sort", ""),
        },
    )


@backoffice_access(CATALOG_VIEW)
def catalog_health_report(request):
    """``/operations/products/health/`` -- full catalog quality & intelligence report."""
    from django.core.paginator import Paginator
    from django.db.models import Q
    from apps.catalog.models import Category
    from apps.catalog.quality import audit_catalog

    severity = request.GET.get("severity", "").strip() or None
    issue_code = request.GET.get("issue", "").strip() or None
    category_slug = request.GET.get("category", "").strip() or None
    status = request.GET.get("status", "").strip() or None
    q = request.GET.get("q", "").strip()

    prod_qs = None
    if q:
        prod_qs = Product.objects.filter(Q(name__icontains=q) | Q(slug__icontains=q))

    report = audit_catalog(
        queryset=prod_qs,
        severity=severity,
        issue_code=issue_code,
        category_slug=category_slug,
        status=status,
    )

    categories = Category.objects.filter(is_active=True).order_by("name")
    paginator = Paginator(report.all_issues, 30)
    page_number = request.GET.get("page", 1)
    page = paginator.get_page(page_number)

    return render_bo(
        request,
        "backoffice/catalog_health.html",
        active="products",
        report=report,
        page=page,
        categories=categories,
        can_manage=has(request.user, CATALOG_MANAGE),
        filters={
            "severity": severity or "",
            "issue": issue_code or "",
            "category": category_slug or "",
            "status": status or "",
            "q": q,
        },
    )


@backoffice_access(CATALOG_VIEW)
def export_catalog_health_csv(request):
    """Export catalog quality audit issues as a CSV download."""
    import csv
    from django.http import HttpResponse
    from django.db.models import Q
    from django.utils import timezone
    from apps.catalog.quality import audit_catalog

    severity = request.GET.get("severity", "").strip() or None
    issue_code = request.GET.get("issue", "").strip() or None
    category_slug = request.GET.get("category", "").strip() or None
    status = request.GET.get("status", "").strip() or None
    q = request.GET.get("q", "").strip()

    prod_qs = None
    if q:
        prod_qs = Product.objects.filter(Q(name__icontains=q) | Q(slug__icontains=q))

    report = audit_catalog(
        queryset=prod_qs,
        severity=severity,
        issue_code=issue_code,
        category_slug=category_slug,
        status=status,
    )

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    now_str = timezone.now().strftime("%Y%m%d-%H%M")
    response["Content-Disposition"] = f'attachment; filename="flashwear-catalog-audit-{now_str}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "Product ID",
        "Product Name",
        "Severity",
        "Category Area",
        "Issue Code",
        "Description",
        "Variant SKU",
        "Admin URL",
    ])
    for issue in report.all_issues:
        writer.writerow([
            issue.product_id,
            issue.product_name,
            issue.severity,
            issue.category,
            issue.code,
            issue.message,
            issue.variant_sku or "",
            request.build_absolute_uri(issue.admin_url),
        ])
    return response


@require_POST
@backoffice_access(CATALOG_MANAGE)
def catalog_remediate(request):
    """Run safe targeted remediation action from Back Office."""
    from apps.backoffice.services.audit import record as record_audit
    from apps.catalog.quality import (
        audit_catalog,
        ensure_variant_stock,
        normalize_primary_images,
        populate_fallback_alt_text,
    )

    action = request.POST.get("action", "").strip()
    reason = request.POST.get("reason", "").strip() or "Backoffice catalog remediation"

    if action == "ensure_stock":
        count = ensure_variant_stock()
        messages.success(request, f"Created {count} missing stock records with zero quantity.")
        record_audit(
            actor=request.user,
            domain="catalog",
            action="catalog.remediate.ensure_stock",
            object_type="catalog.stock",
            object_id="all",
            object_repr=f"{count} stock rows created",
            reason=reason,
        )
    elif action == "fix_primary_images":
        count = normalize_primary_images()
        messages.success(request, f"Normalized primary images across {count} products.")
        record_audit(
            actor=request.user,
            domain="catalog",
            action="catalog.remediate.normalize_primary_images",
            object_type="catalog.product_image",
            object_id="all",
            object_repr=f"{count} products normalized",
            reason=reason,
        )
    elif action == "populate_alt_text":
        count = populate_fallback_alt_text()
        messages.success(request, f"Populated fallback alt text for {count} product images.")
        record_audit(
            actor=request.user,
            domain="catalog",
            action="catalog.remediate.populate_alt_text",
            object_type="catalog.product_image",
            object_id="all",
            object_repr=f"{count} alt texts populated",
            reason=reason,
        )
    elif action == "unpublish_blocking":
        report = audit_catalog(status=Product.Status.ACTIVE)
        blocking_pks = [
            pk for pk, audit in report.product_audits.items()
            if audit.blocking_count > 0 and audit.product.status == Product.Status.ACTIVE
        ]
        if blocking_pks:
            res = operations.catalog_action(
                products=Product.objects.filter(pk__in=blocking_pks),
                action="unpublish",
                actor=request.user,
                reason=reason or "Unpublish products with blocking catalog quality issues",
            )
            messages.warning(
                request,
                f"Unpublished {len(res.succeeded)} active product(s) with blocking quality errors.",
            )
        else:
            messages.info(request, "No active products with blocking quality errors found.")
    else:
        messages.error(request, f"Unknown remediation action: {action}")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("backoffice:catalog_health")
    return redirect(next_url)



@require_POST
@backoffice_access(CATALOG_MANAGE)
def products_bulk(request):
    """Publish / unpublish / archive the selection -- one audit row for the whole run."""
    form = CatalogBulkForm(request.POST)
    ids = request.POST.getlist("selected")
    if not form.is_valid() or not ids:
        messages.error(request, "Select at least one product and choose an action.")
        return redirect("backoffice:products")

    result = operations.catalog_action(
        products=Product.objects.filter(pk__in=ids),
        action=form.cleaned_data["action"],
        actor=request.user,
        reason=form.cleaned_data["reason"],
    )
    _report_bulk(request, result, "product")
    return redirect("backoffice:products")


def _report_bulk(request, result, noun: str) -> None:
    """Turn a partial success into an honest message: what moved, what did not, why."""
    if result.succeeded:
        moved = ", ".join(str(pk) for pk in result.succeeded)
        messages.success(request, f"{len(result.succeeded)} {noun}(s) updated: {moved}.")
    for obj, reason in result.failed:
        messages.error(request, f"{obj}: {reason}")


@backoffice_access(INVENTORY_VIEW)
def inventory(request):
    """``/operations/inventory/`` -- every SKU's counters and the adjustment form."""
    from django.conf import settings

    queryset = selectors.stock_rows(
        q=request.GET.get("q", ""),
        state=request.GET.get("state", ""),
        sort=request.GET.get("sort", ""),
    )
    page = selectors.paginate(request, queryset)
    threshold = settings.BACKOFFICE_LOW_STOCK_THRESHOLD
    # The state word is resolved here: a template must not work out business state from
    # two counters, and ``stock_state`` is the one place that knows the rule.
    rows = [
        {"stock": stock, "state": selectors.stock_state(stock, threshold, stock.qty_available)}
        for stock in page
    ]
    return render_bo(
        request,
        "backoffice/inventory.html",
        active="inventory",
        page=page,
        rows=rows,
        recent_movements=selectors.recent_movements(limit=25),
        adjust_form=InventoryAdjustForm(initial={"variant": request.GET.get("variant", "")}),
        filters={
            "q": request.GET.get("q", ""),
            "state": request.GET.get("state", ""),
            "sort": request.GET.get("sort", ""),
        },
        low_stock_count=selectors.low_stock_rows().count(),
        threshold=threshold,
    )


@require_POST
@backoffice_access(INVENTORY_MANAGE)
def adjust_stock(request):
    """Apply one signed stock change and record who asked for it."""
    form = InventoryAdjustForm(request.POST)
    if not form.is_valid():
        for field, errors in form.errors.items():
            messages.error(request, f"{field}: {' '.join(errors)}")
        return redirect("backoffice:inventory")

    data = form.cleaned_data
    try:
        operations.adjust_inventory(
            variant=data["variant"],
            delta=data["delta"],
            kind=data["kind"],
            note=data["note"],
            reason=data["reason"],
            actor=request.user,
        )
    except operations.DOMAIN_ERRORS as exc:
        messages.error(request, operations.error_message(exc))
    else:
        messages.success(
            request,
            f"{data['variant'].sku}: {data['delta']:+d} applied ({data['kind']}).",
        )
    return redirect("backoffice:inventory")


# ======================================================================================
# Inventory Intelligence, Health & Restock Views (Phase 44)
# ======================================================================================


@backoffice_access(INVENTORY_VIEW)
def inventory_health(request):
    """``/operations/inventory/health/`` -- real-time inventory health & distribution dashboard."""
    summary = inventory_intel.get_inventory_health_summary()
    categories = Category.objects.filter(is_active=True).order_by("name")
    return render_bo(
        request,
        "backoffice/inventory_health.html",
        active="inventory",
        summary=summary,
        categories=categories,
        can_manage=has(request.user, INVENTORY_MANAGE),
    )


@backoffice_access(INVENTORY_VIEW)
def export_inventory_health_csv(request):
    """``/operations/inventory/health/export/`` -- safe CSV export of inventory stock levels."""
    from apps.inventory.models import Stock

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    now_str = timezone.now().strftime("%Y%m%d_%H%M%S")
    response["Content-Disposition"] = f'attachment; filename="flashwear_inventory_health_{now_str}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "Variant ID",
        "SKU",
        "Product Name",
        "Category",
        "Color / Size",
        "On Hand",
        "Reserved",
        "Available",
        "Configured Threshold",
        "Stock State",
        "Updated At",
    ])

    stocks = (
        Stock.objects.select_related(
            "variant",
            "variant__product",
            "variant__product__category",
            "variant__color",
            "variant__size",
        )
        .order_by("variant__sku")
    )

    for stock in stocks:
        v = stock.variant
        cat_name = v.product.category.name if v.product.category else "Uncategorized"
        color_size = v.option_label or "Default"
        thresh = stock.effective_threshold
        state = selectors.stock_state(stock, thresh, stock.available)
        writer.writerow([
            v.pk,
            v.sku,
            v.product.name,
            cat_name,
            color_size,
            stock.on_hand,
            stock.reserved,
            stock.available,
            thresh,
            state.upper(),
            stock.updated_at.strftime("%Y-%m-%d %H:%M:%S") if stock.updated_at else "",
        ])

    return response


@backoffice_access(INVENTORY_VIEW)
def inventory_restock(request):
    """``/operations/inventory/restock/`` -- data-grounded restock prioritization & receipt recording."""
    if request.method == "POST":
        if not has(request.user, INVENTORY_MANAGE):
            messages.error(request, _("You do not have permission to record restock receipts."))
            return redirect("backoffice:inventory_restock")

        try:
            variant_id = int(request.POST.get("variant_id") or request.POST.get("variant") or 0)
            quantity = int(request.POST.get("quantity", 0))
            reference = request.POST.get("reference", "").strip()
            reason = request.POST.get("reason", "").strip()
            note = request.POST.get("note", "").strip()

            movement = inventory_intel.record_restock_receipt(
                variant_id=variant_id,
                quantity=quantity,
                reference=reference,
                reason=reason,
                note=note,
                actor=request.user,
            )
            messages.success(
                request,
                f"Successfully recorded restock receipt: +{quantity} units for {movement.variant.sku} (Ref: {reference}).",
            )
        except Exception as exc:
            messages.error(request, f"Restock failed: {exc}")

        return redirect("backoffice:inventory_restock")

    category = request.GET.get("category", "")
    priority = request.GET.get("priority", "")
    q = request.GET.get("q", "")

    restock_items = inventory_intel.get_prioritized_restock_list(
        category_slug=category,
        priority_filter=priority,
        q=q,
    )
    categories = Category.objects.filter(is_active=True).order_by("name")

    critical_count = sum(1 for item in restock_items if item.is_critical)
    urgent_count = sum(1 for item in restock_items if item.is_urgent)
    warning_count = sum(1 for item in restock_items if item.is_warning)

    return render_bo(
        request,
        "backoffice/inventory_restock.html",
        active="inventory",
        items=restock_items,
        categories=categories,
        filters={"category": category, "priority": priority, "q": q},
        critical_count=critical_count,
        urgent_count=urgent_count,
        warning_count=warning_count,
        can_manage=has(request.user, INVENTORY_MANAGE),
    )


@backoffice_access(INVENTORY_VIEW)
def inventory_reconciliation(request):
    """``/operations/inventory/reconciliation/`` -- read-only reconciliation report & safe correction tools."""
    category = request.GET.get("category", "")
    severity = request.GET.get("severity", "")
    q = request.GET.get("q", "")

    report = inventory_intel.audit_inventory_reconciliation(
        category_slug=category,
        severity_filter=severity,
        q=q,
    )
    categories = Category.objects.filter(is_active=True).order_by("name")

    return render_bo(
        request,
        "backoffice/inventory_reconciliation.html",
        active="inventory",
        report=report,
        categories=categories,
        filters={"category": category, "severity": severity, "q": q},
        can_manage=has(request.user, INVENTORY_MANAGE),
    )


@require_POST
@backoffice_access(INVENTORY_MANAGE)
def inventory_reconcile_action(request):
    """Handle explicit, permission-controlled, auditable reconciliation actions."""
    action = request.POST.get("action", "").strip()
    reason = request.POST.get("reason", "").strip() or "Backoffice reconciliation"

    if action in ("reconcile_reserved", "reconcile_variant"):
        variant_id = request.POST.get("variant_id")
        if variant_id:
            try:
                res = inventory_intel.reconcile_variant_reserved(
                    variant_id=int(variant_id),
                    actor=request.user,
                    reason=reason,
                )
                messages.success(
                    request,
                    f"Reconciled reserved count for {res['sku']}: set to {res['new_reserved']} (delta {res['delta']:+d}).",
                )
            except Exception as exc:
                messages.error(request, f"Reconciliation failed: {exc}")
        else:
            messages.error(request, "No variant specified for reservation reconciliation.")

    elif action == "sweep_expired":
        count = inventory_intel.reconcile_expired_holds(actor=request.user, reason=reason)
        messages.success(request, f"Swept {count} expired checkout hold(s) back to available pool.")

    elif action == "release_cancelled_orders":
        count = inventory_intel.reconcile_cancelled_order_holds(actor=request.user, reason=reason)
        messages.success(request, f"Released {count} active reservation(s) on cancelled orders.")

    else:
        messages.error(request, f"Unknown reconciliation action: '{action}'")

    next_url = (
        request.POST.get("next")
        or request.META.get("HTTP_REFERER")
        or reverse("backoffice:inventory_reconciliation")
    )
    return redirect(next_url)


@backoffice_access(MARKETING_VIEW)
def drops(request):
    """``/operations/drops/`` -- what the drops app says is live, scheduled or over."""
    queryset = selectors.drops(
        status=request.GET.get("status", ""),
        q=request.GET.get("q", ""),
    )
    return render_bo(
        request,
        "backoffice/drops.html",
        active="drops",
        page=selectors.paginate(request, queryset),
        filters={"q": request.GET.get("q", ""), "status": request.GET.get("status", "")},
        status_choices=_drop_choices(),
    )


def _drop_choices():
    from apps.drops.models import DropStatus

    return DropStatus.choices


@backoffice_access(MARKETING_VIEW)
def promotions(request):
    """``/operations/promotions/`` -- usage against the domain's own limits."""
    queryset = selectors.promotions(
        scope=request.GET.get("scope", ""),
        q=request.GET.get("q", ""),
    )
    page = selectors.paginate(request, queryset)
    # ``promotion_state`` is a Python-side rule; resolving it per row here keeps the
    # template free of business logic without costing a query per row.
    states = {promotion.pk: selectors.promotion_state(promotion) for promotion in page}
    return render_bo(
        request,
        "backoffice/promotions.html",
        active="promotions",
        page=page,
        states=states,
        scopes=PROMOTION_SCOPES,
        filters={"q": request.GET.get("q", ""), "scope": request.GET.get("scope", "")},
    )


@backoffice_access(MARKETING_VIEW)
def quests(request):
    """``/operations/quests/`` -- published definitions and their participation."""
    from apps.quests.models import Quest

    queryset = selectors.quests(
        q=request.GET.get("q", ""),
        publish_state=request.GET.get("publish_state", ""),
        sort=request.GET.get("sort", ""),
    )
    return render_bo(
        request,
        "backoffice/quests.html",
        active="quests",
        page=selectors.paginate(request, queryset),
        publish_states=Quest.PublishState.choices,
        filters={
            "q": request.GET.get("q", ""),
            "publish_state": request.GET.get("publish_state", ""),
            "sort": request.GET.get("sort", ""),
        },
    )


@backoffice_access(CATALOG_VIEW)
def merch_featured_products(request):
    """``/operations/merchandising/featured-products/`` -- feature products in sections."""
    from apps.backoffice.forms_content import SectionProductForm
    from apps.core.models import HomepageSection, SectionProduct

    product_sections = HomepageSection.objects.filter(
        section_type__in=[
            HomepageSection.SectionType.FEATURED_PRODUCTS,
            HomepageSection.SectionType.NEW_ARRIVALS,
            HomepageSection.SectionType.SHOP_BY_MOOD,
            HomepageSection.SectionType.COMPLETE_LOOK,
        ]
    ).order_by("display_order", "id")

    selected_section_id = request.GET.get("section")
    selected_section = None
    if selected_section_id:
        selected_section = product_sections.filter(pk=selected_section_id).first()
    if not selected_section and product_sections.exists():
        selected_section = product_sections.first()

    pinned_products = []
    if selected_section:
        pinned_products = (
            SectionProduct.objects.filter(section=selected_section)
            .select_related("product", "product__brand")
            .prefetch_related("product__images")
            .order_by("display_order", "id")
        )

    can_manage = has(request.user, CATALOG_MANAGE) or has(request.user, MARKETING_MANAGE)
    form = SectionProductForm()

    return render_bo(
        request,
        "backoffice/merchandising/featured_products.html",
        active="merch_featured_products",
        sections=product_sections,
        selected_section=selected_section,
        pinned_products=pinned_products,
        form=form,
        can_manage=can_manage,
    )


@require_POST
def merch_product_add(request, section_pk):
    """Add a product to a featured section."""
    from django.http import Http404
    from django.shortcuts import get_object_or_404

    from apps.backoffice.forms_content import SectionProductForm
    from apps.backoffice.models import AuditEvent
    from apps.backoffice.services.audit import record as record_audit
    from apps.core.models import HomepageSection, SectionProduct

    if not (has(request.user, CATALOG_MANAGE) or has(request.user, MARKETING_MANAGE)):
        raise Http404

    section = get_object_or_404(HomepageSection, pk=section_pk)
    form = SectionProductForm(request.POST)
    if form.is_valid():
        product = form.cleaned_data["product"]
        sp, created = SectionProduct.objects.get_or_create(
            section=section,
            product=product,
            defaults={
                "display_order": form.cleaned_data["display_order"],
                "starts_at": form.cleaned_data["starts_at"],
                "ends_at": form.cleaned_data["ends_at"],
                "is_active": form.cleaned_data["is_active"],
            },
        )
        if not created:
            sp.display_order = form.cleaned_data["display_order"]
            sp.starts_at = form.cleaned_data["starts_at"]
            sp.ends_at = form.cleaned_data["ends_at"]
            sp.is_active = form.cleaned_data["is_active"]
            sp.save()

        record_audit(
            actor=request.user,
            domain=AuditEvent.Domain.CATALOG,
            action="merch.product.add",
            object_type="SectionProduct",
            object_id=str(sp.pk),
            object_repr=f"{section.title} -> {product.name}",
            reason=f"Added product '{product.name}' to section '{section.title}'",
        )
        messages.success(request, f"Product '{product.name}' added to '{section.title}'.")
    else:
        for err in form.errors.values():
            messages.error(request, " ".join(err))

    return redirect(f"/operations/merchandising/featured-products/?section={section.pk}")


@require_POST
def merch_product_reorder(request, section_pk):
    """Update display order for a section product without heavy JS."""
    from django.http import Http404
    from django.shortcuts import get_object_or_404

    from apps.backoffice.models import AuditEvent
    from apps.backoffice.services.audit import record as record_audit
    from apps.core.models import HomepageSection, SectionProduct

    if not (has(request.user, CATALOG_MANAGE) or has(request.user, MARKETING_MANAGE)):
        raise Http404

    section = get_object_or_404(HomepageSection, pk=section_pk)
    product_pk = request.POST.get("product_id")
    new_order = request.POST.get("display_order")

    try:
        new_order = int(new_order)
    except (TypeError, ValueError):
        messages.error(request, "Invalid display order number.")
        return redirect(f"/operations/merchandising/featured-products/?section={section.pk}")

    sp = get_object_or_404(SectionProduct, section=section, product_id=product_pk)
    sp.display_order = max(0, new_order)
    sp.save()

    record_audit(
        actor=request.user,
        domain=AuditEvent.Domain.CATALOG,
        action="merch.product.reorder",
        object_type="SectionProduct",
        object_id=str(sp.pk),
        object_repr=f"{section.title} -> {sp.product.name}",
        reason=f"Updated order to {sp.display_order}",
    )
    messages.success(request, f"Updated display order for '{sp.product.name}'.")
    return redirect(f"/operations/merchandising/featured-products/?section={section.pk}")


@require_POST
def merch_product_remove(request, section_pk, product_pk):
    """Remove a featured product from a section."""
    from django.http import Http404
    from django.shortcuts import get_object_or_404

    from apps.backoffice.models import AuditEvent
    from apps.backoffice.services.audit import record as record_audit
    from apps.core.models import HomepageSection, SectionProduct

    if not (has(request.user, CATALOG_MANAGE) or has(request.user, MARKETING_MANAGE)):
        raise Http404

    section = get_object_or_404(HomepageSection, pk=section_pk)
    sp = get_object_or_404(SectionProduct, section=section, product_id=product_pk)
    prod_title = sp.product.name
    sp.delete()

    record_audit(
        actor=request.user,
        domain=AuditEvent.Domain.CATALOG,
        action="merch.product.remove",
        object_type="SectionProduct",
        object_id=str(product_pk),
        object_repr=f"{section.title} -> {prod_title}",
        reason=f"Removed product '{prod_title}' from section '{section.title}'",
    )
    messages.success(request, f"Product '{prod_title}' removed from '{section.title}'.")
    return redirect(f"/operations/merchandising/featured-products/?section={section.pk}")


@backoffice_access(CATALOG_VIEW)
def merch_featured_collections(request):
    """``/operations/merchandising/featured-collections/`` -- featured collections overview."""

    from apps.core.models import HomepageSection

    sections = (
        HomepageSection.objects.filter(
            section_type__in=[
                HomepageSection.SectionType.FEATURED_COLLECTION,
                HomepageSection.SectionType.DROPS,
            ]
        )
        .select_related("linked_collection", "linked_category", "linked_drop")
        .order_by("display_order", "id")
    )

    return render_bo(
        request,
        "backoffice/merchandising/featured_collections.html",
        active="merch_featured_collections",
        sections=sections,
    )


@backoffice_access(CATALOG_VIEW)
def merch_scheduled(request):
    """``/operations/merchandising/scheduled/`` -- Unified timeline of all scheduled content."""
    from django.utils import timezone

    from apps.core.models import Campaign, EditorialStory, HomepageSection, SectionProduct

    now = timezone.now()

    campaigns = Campaign.objects.all().order_by("starts_at")
    scheduled_sections = (
        HomepageSection.objects.filter(starts_at__isnull=False)
        .select_related("campaign")
        .order_by("starts_at")
    )
    scheduled_products = (
        SectionProduct.objects.filter(starts_at__isnull=False)
        .select_related("section", "product")
        .order_by("starts_at")
    )
    scheduled_stories = EditorialStory.objects.filter(starts_at__isnull=False).order_by("starts_at")

    return render_bo(
        request,
        "backoffice/merchandising/scheduled.html",
        active="merch_scheduled",
        now=now,
        campaigns=campaigns,
        sections=scheduled_sections,
        products=scheduled_products,
        stories=scheduled_stories,
    )
