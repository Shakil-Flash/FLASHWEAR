"""Merchandising screens: products, inventory, drops, promotions, quests (Phase 16).

Mutations here are thin: catalogue state changes go through ``apps.catalog.services``,
stock through :func:`apps.inventory.services.adjust_stock`. Drops, promotions and quests
are **read-only** in the back office -- their windows and counters belong to the apps that
own them (and to ``/admin/``), so this layer reports what those apps decided rather than
re-deciding it.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect
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
from apps.catalog.models import Product

__all__ = [
    "adjust_stock",
    "drops",
    "inventory",
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
