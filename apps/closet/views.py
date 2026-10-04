"""Closet and outfit pages, mounted under ``/account/`` (Phase 8).

The URL patterns live in :mod:`apps.accounts.account_urls` (the account area declares
exactly one ``app_name`` and wraps every route in ``login_required``), the views live here
with the rest of the closet code -- the same split orders and engagement use.

Two habits run through every view:

* **Ownership is a queryset filter, never a template condition.** Items and outfits resolve
  through ``request.user``'s own rows, so someone else's id 404s before any rule runs.
* **POST + redirect (PRG).** Every mutation is a form button, the outcome lands as a
  message, and the builder re-renders -- no page depends on JavaScript, and reorder is
  up/down buttons rather than drag-and-drop.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.closet import forms as closet_forms
from apps.closet.models import ClosetItem, Outfit, OutfitItem
from apps.closet.services import closet as closet_services
from apps.closet.services import outfits as outfit_services
from apps.closet.services.errors import ClosetError
from apps.orders.models import OrderItem

__all__ = [
    "closet_page",
    "item_add_purchase",
    "item_archive",
    "item_create",
    "item_delete",
    "item_edit",
    "item_restore",
    "outfit_archive",
    "outfit_create",
    "outfit_delete",
    "outfit_detail",
    "outfit_duplicate",
    "outfit_item_add",
    "outfit_item_move",
    "outfit_item_remove",
    "outfit_restore",
    "outfit_save",
    "outfit_update",
    "outfits_page",
    "purchases_page",
]

CLOSET_PAGE_SIZE = 24


# =============================================================================
# Helpers
# =============================================================================


def _owned_item(user, pk: int) -> ClosetItem:
    """One of this customer's pieces. Someone else's id is a 404, never a 403 probe."""
    return get_object_or_404(ClosetItem, pk=pk, user=user)


def _owned_outfit(user, pk: int) -> Outfit:
    return get_object_or_404(Outfit, pk=pk, user=user)


def _safe_next(request, fallback: str) -> str:
    """Accept a post-submit redirect only when it points back into this site."""
    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return fallback


def _filter_form(user, data=None):
    """Bind the filter form with this wardrobe's own brand/colour values as choices."""
    user_items = ClosetItem.objects.filter(user=user)
    colors = sorted(
        value for value in user_items.values_list("color", flat=True).distinct() if value
    )
    brands = sorted(
        value for value in user_items.values_list("brand", flat=True).distinct() if value
    )
    categories = [(key, label) for key, label in ClosetItem.Category.choices]
    return closet_forms.ClosetFilterForm(
        data,
        category_choices=categories,
        color_choices=[(value, value) for value in colors],
        brand_choices=[(value, value) for value in brands],
    )


def _filter_kwargs(form) -> dict:
    """Valid filter form -> the service's kwargs (invalid forms fall back to defaults)."""
    if not form.is_bound or not form.is_valid():
        return {}
    return {key: value for key, value in form.cleaned_data.items() if value}


# =============================================================================
# Closet
# =============================================================================


def closet_page(request):
    """``/account/closet/`` -- the wardrobe: filters, search, sort, item cards."""
    form = _filter_form(request.user, request.GET or None)
    queryset = closet_services.filter_closet(request.user, **_filter_kwargs(form))
    page = Paginator(queryset, CLOSET_PAGE_SIZE).get_page(request.GET.get("page"))

    category_counts = closet_services.count_by_category(request.user)
    totals = ClosetItem.objects.filter(user=request.user).aggregate(
        active=Count("id", filter={"status": ClosetItem.Status.ACTIVE}),
        archived=Count("id", filter={"status": ClosetItem.Status.ARCHIVED}),
    )
    active_category = request.GET.get("category", "")
    if active_category not in ClosetItem.Category.values:
        active_category = ""
    # Rail links preserve every other active filter (and drop the page), so browsing by
    # category never silently resets a search the customer already applied.
    base_params = request.GET.copy()
    base_params.pop("page", None)
    category_links = []
    for key, label in (("", _("All")), *ClosetItem.Category.choices):
        params = base_params.copy()
        if key:
            params["category"] = key
        else:
            params.pop("category", None)
        category_links.append((key, label, category_counts.get(key, 0), f"?{params.urlencode()}"))

    context = {
        "items": page,
        "form": form,
        "category_links": category_links,
        "active_category": active_category,
        "category_choices": ClosetItem.Category.choices,
        "totals": totals,
        "has_filters": bool(_filter_kwargs(form)),
    }
    return render(request, "account/closet.html", context)


def item_create(request):
    """``GET/POST /account/closet/new/`` -- add manually owned clothing."""
    if request.method == "POST":
        form = closet_forms.ClosetItemForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                closet_services.create_manual_item(request.user, **form.cleaned_data)
            except ClosetError as exc:
                messages.error(request, exc.message)
            else:
                messages.success(request, _("Added to your closet."))
                return redirect("account:closet")
        else:
            messages.error(request, _("Please check the highlighted fields."))
    else:
        form = closet_forms.ClosetItemForm()
    return render(
        request,
        "account/closet_item_form.html",
        {"form": form, "item": None},
    )


def item_edit(request, pk: int):
    """``GET/POST /account/closet/<pk>/edit/`` -- metadata and photo of one piece.

    ``source``/``variant``/``order_item``/``unit`` are not on the form, so a purchased
    piece's provenance cannot be edited away -- only its wardrobe metadata can change.
    """
    item = _owned_item(request.user, pk)
    if request.method == "POST":
        form = closet_forms.ClosetItemForm(request.POST, request.FILES, instance=item)
        if form.is_valid():
            try:
                form.save()
            except ClosetError as exc:
                messages.error(request, exc.message)
            else:
                messages.success(request, _("Your changes are saved."))
                return redirect("account:closet")
        else:
            messages.error(request, _("Please check the highlighted fields."))
    else:
        form = closet_forms.ClosetItemForm(instance=item)
    return render(
        request,
        "account/closet_item_form.html",
        {"form": form, "item": item},
    )


@require_POST
def item_archive(request, pk: int):
    """``POST .../archive/`` -- out of the default closet; outfit history keeps rendering it."""
    item = _owned_item(request.user, pk)
    closet_services.set_status(item, ClosetItem.Status.ARCHIVED)
    messages.success(request, _("%(name)s moved to your archive.") % {"name": item.name})
    return redirect("account:closet")


@require_POST
def item_restore(request, pk: int):
    """``POST .../restore/`` -- back into the active wardrobe (and available to outfits)."""
    item = _owned_item(request.user, pk)
    closet_services.set_status(item, ClosetItem.Status.ACTIVE)
    messages.success(request, _("%(name)s is back in your closet.") % {"name": item.name})
    return redirect("account:closet")


@require_POST
def item_delete(request, pk: int):
    """``POST .../delete/`` -- hard-remove, refused while an outfit still wears the piece."""
    item = _owned_item(request.user, pk)
    try:
        closet_services.delete_item(item)
    except ClosetError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, _("%(name)s was removed from your closet.") % {"name": item.name})
    return redirect("account:closet")


def purchases_page(request):
    """``/account/closet/purchases/`` -- delivered lines with units still to add."""
    candidates = closet_services.purchase_candidates(request.user)
    rows = [
        (order, line, left, guess, closet_forms.PurchaseAddForm(initial={"category": guess}))
        for order, line, left, guess in candidates
    ]
    return render(
        request,
        "account/closet_purchases.html",
        {"rows": rows},
    )


@require_POST
def item_add_purchase(request, pk: int):
    """``POST /account/closet/purchases/<pk>/add/`` -- move delivered units into the closet.

    The line resolves through the caller's own orders (someone else's id 404s); eligibility
    (delivered), provenance and the source stamp are the service's job. ``units`` is clamped
    to what remains, so replaying the POST converges instead of duplicating.
    """
    order_item = get_object_or_404(OrderItem, pk=pk, order__user=request.user)
    form = closet_forms.PurchaseAddForm(request.POST)
    fallback = reverse("account:closet-purchases")
    if form.is_valid():
        try:
            created = closet_services.add_purchased_units(
                request.user,
                order_item,
                units=form.cleaned_data["units"],
                category=form.cleaned_data["category"],
            )
        except ClosetError as exc:
            messages.error(request, exc.message)
        else:
            if created:
                messages.success(
                    request,
                    _("%(count)d piece%(plural)s added to your closet.")
                    % {"count": len(created), "plural": "" if len(created) == 1 else "s"},
                )
            else:
                messages.info(
                    request,
                    _("That item is already in your closet."),
                )
    else:
        messages.error(request, _("Please choose a category and a quantity."))
    return redirect(_safe_next(request, fallback))


# =============================================================================
# Outfits
# =============================================================================


def outfits_page(request):
    """``/account/outfits/`` -- saved, draft and archived outfits with completeness."""
    status = request.GET.get("status", "")
    queryset = outfit_services.get_user_outfits(request.user, status=status)
    outfits = queryset[:60]
    counts = {
        row["status"]: row["n"]
        for row in Outfit.objects.filter(user=request.user).values("status").annotate(n=Count("id"))
    }
    enriched = [
        (outfit, outfit_services.calculate_outfit_completeness(outfit)) for outfit in outfits
    ]
    status_tabs = [
        (key, label, counts.get(key, 0))
        for key, label in [
            ("", _("All")),
            (Outfit.Status.SAVED, _("Saved")),
            (Outfit.Status.DRAFT, _("Drafts")),
            (Outfit.Status.ARCHIVED, _("Archived")),
        ]
    ]
    return render(
        request,
        "account/outfits.html",
        {
            "outfits": enriched,
            "active_status": status if status in outfit_services.VALID_STATUSES else "",
            "status_tabs": status_tabs,
        },
    )


def outfit_create(request):
    """``GET/POST /account/outfits/new/`` -- name an outfit, then compose it on the builder."""
    if request.method == "POST":
        form = closet_forms.OutfitForm(request.POST)
        if form.is_valid():
            try:
                outfit = outfit_services.create_outfit(request.user, **form.cleaned_data)
            except ClosetError as exc:
                messages.error(request, exc.message)
            else:
                messages.success(request, _("Outfit created. Add some pieces."))
                return redirect("account:outfit-detail", pk=outfit.pk)
        else:
            messages.error(request, _("Please give the outfit a name."))
    else:
        form = closet_forms.OutfitForm()
    return render(request, "account/outfit_form.html", {"form": form})


def outfit_detail(request, pk: int):
    """``/account/outfits/<pk>/`` -- the builder: metadata, picker, slots, completeness."""
    outfit = get_object_or_404(outfit_services.get_user_outfits(request.user), pk=pk)
    links = list(outfit.items.all())
    in_outfit = {link.closet_item_id for link in links}
    pick_category = request.GET.get("pick", "")
    available = closet_services.get_available_closet_items(request.user).exclude(pk__in=in_outfit)
    if pick_category and pick_category in ClosetItem.Category.values:
        available = available.filter(category=pick_category)

    context = {
        "outfit": outfit,
        "links": links,
        "completeness": outfit_services.calculate_outfit_completeness(outfit),
        "metadata_form": closet_forms.OutfitForm(instance=outfit),
        "add_form": closet_forms.OutfitAddItemForm(),
        "available": available,
        "pick_category": pick_category,
        "category_choices": ClosetItem.Category.choices,
        "role_for_category": outfit_services.ROLE_FOR_CATEGORY,
        "status_choices": [
            (Outfit.Status.DRAFT, _("Draft")),
            (Outfit.Status.SAVED, _("Saved")),
            (Outfit.Status.ARCHIVED, _("Archived")),
        ],
    }
    return render(request, "account/outfit_builder.html", context)


@require_POST
def outfit_update(request, pk: int):
    """``POST .../update/`` -- metadata edit from the builder's own form."""
    outfit = _owned_outfit(request.user, pk)
    form = closet_forms.OutfitForm(request.POST, instance=outfit)
    if form.is_valid():
        try:
            outfit_services.update_outfit(outfit, **form.cleaned_data)
        except ClosetError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, _("Outfit details saved."))
    else:
        messages.error(request, _("Please check the outfit details."))
    return redirect("account:outfit-detail", pk=pk)


@require_POST
def outfit_save(request, pk: int):
    """``POST .../save/`` -- draft -> saved (or a saved outfit refreshed)."""
    outfit = _owned_outfit(request.user, pk)
    outfit_services.set_outfit_status(outfit, Outfit.Status.SAVED)
    messages.success(request, _("%(name)s is saved.") % {"name": outfit.name})
    return redirect("account:outfit-detail", pk=pk)


@require_POST
def outfit_archive(request, pk: int):
    outfit = _owned_outfit(request.user, pk)
    outfit_services.set_outfit_status(outfit, Outfit.Status.ARCHIVED)
    messages.success(request, _("%(name)s archived.") % {"name": outfit.name})
    return redirect("account:outfits")


@require_POST
def outfit_restore(request, pk: int):
    outfit = _owned_outfit(request.user, pk)
    outfit_services.set_outfit_status(outfit, Outfit.Status.SAVED)
    messages.success(request, _("%(name)s restored.") % {"name": outfit.name})
    return redirect("account:outfits")


@require_POST
def outfit_duplicate(request, pk: int):
    """``POST .../duplicate/`` -- a fresh draft copy; the original is untouched."""
    outfit = _owned_outfit(request.user, pk)
    clone = outfit_services.duplicate_outfit(outfit)
    messages.success(request, _("Duplicated as “%(name)s”.") % {"name": clone.name})
    return redirect("account:outfit-detail", pk=clone.pk)


@require_POST
def outfit_delete(request, pk: int):
    """``POST .../delete/`` -- the outfit goes; every garment stays in the closet."""
    outfit = _owned_outfit(request.user, pk)
    outfit_services.delete_outfit(outfit)
    messages.success(request, _("Outfit deleted. Your pieces are untouched."))
    return redirect("account:outfits")


@require_POST
def outfit_item_add(request, pk: int):
    """``POST .../items/add/`` -- place one wardrobe piece (resolved through the owner)."""
    outfit = _owned_outfit(request.user, pk)
    form = closet_forms.OutfitAddItemForm(request.POST)
    if form.is_valid():
        closet_item = get_object_or_404(
            ClosetItem, pk=form.cleaned_data["closet_item"], user=request.user
        )
        try:
            outfit_services.add_item(outfit, closet_item, note=form.cleaned_data["note"])
        except ClosetError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, _("%(name)s added.") % {"name": closet_item.name})
    else:
        messages.error(request, _("That item could not be added."))
    return redirect("account:outfit-detail", pk=pk)


@require_POST
def outfit_item_remove(request, pk: int, item_id: int):
    """``POST .../items/<id>/remove/`` -- taken out of this outfit only."""
    outfit = _owned_outfit(request.user, pk)
    link = get_object_or_404(OutfitItem, pk=item_id, outfit=outfit)
    name = link.closet_item.name
    outfit_services.remove_item(outfit, link)
    messages.success(request, _("%(name)s removed from the outfit.") % {"name": name})
    return redirect("account:outfit-detail", pk=pk)


@require_POST
def outfit_item_move(request, pk: int, item_id: int):
    """``POST .../items/<id>/move/`` -- keyboard-friendly up/down reordering."""
    outfit = _owned_outfit(request.user, pk)
    link = get_object_or_404(OutfitItem, pk=item_id, outfit=outfit)
    direction = request.POST.get("direction", "")
    if direction not in ("up", "down"):
        messages.error(request, _("That move is not valid."))
        return redirect("account:outfit-detail", pk=pk)
    delta = -1 if direction == "up" else 1
    try:
        outfit_services.move_item_to(outfit, link, link.position + delta)
    except ClosetError as exc:
        messages.error(request, exc.message)
    return redirect("account:outfit-detail", pk=pk)
