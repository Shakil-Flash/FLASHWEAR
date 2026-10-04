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
        adjust_form=InventoryAdjustForm(
            initial={"variant": request.GET.get("variant", "")}
        ),
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
