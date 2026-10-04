"""FLASH Loop views (Phase 13).

Two audiences, two boundaries:

* the **public shelf** (``/loop/``) only ever sees what
  :mod:`apps.loop.selectors.public_resale_listings` yields: approved,
  active, non-rejected listings. Drafts, moderation notes, seller
  contact details and ownership evidence are not in the queryset, so
  they cannot leak through a template bug either.
* the **account area** (``/account/loop/``) scopes every fetch by
  ``request.user`` at the top of the view. Another customer's row
  does not 403 -- it 404s, so the URL space of someone else's items
  is indistinguishable from an item that does not exist.

All state changes go through :mod:`apps.loop.services.loop_items`;
no view writes ``status`` itself.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.core.utils import storefront_open
from apps.loop import selectors
from apps.loop.forms import LoopItemCreateForm, LoopPhotoForm
from apps.loop.models import LoopItem, LoopItemImage, RecycleRequest, ResaleListing, TradeInRequest
from apps.loop.services import loop_items as loop_services
from apps.loop.services.circularity import circularity_info
from apps.loop.services.errors import LoopError

__all__ = [
    "loop_create",
    "loop_dashboard",
    "loop_item_cancel",
    "loop_item_detail",
    "loop_item_photo_add",
    "loop_item_submit",
    "resale_detail",
    "resale_list",
]

# A shelf page shows as many cards as a catalogue grid: same density,
# same paginator mechanics.
LOOP_LISTINGS_PER_PAGE = 12


def _parse_decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None


# =============================================================================
# Public resale discovery
# =============================================================================


@storefront_open
@never_cache
@require_GET
def resale_list(request):
    """``/loop/`` (and ``/loop/resale/``) -- the public second-hand shelf."""
    sort = request.GET.get("sort") or selectors.DEFAULT_PUBLIC_SORT
    if sort not in selectors.PUBLIC_SORT_CHOICES:
        sort = selectors.DEFAULT_PUBLIC_SORT

    queryset = selectors.public_resale_listings(
        sort=sort,
        category=(request.GET.get("category") or "").strip().lower(),
        size=(request.GET.get("size") or "").strip(),
        color=(request.GET.get("color") or "").strip().lower(),
        condition=(request.GET.get("condition") or "").strip().lower(),
        material=(request.GET.get("material") or "").strip().lower(),
        min_price=_parse_decimal(request.GET.get("min_price")),
        max_price=_parse_decimal(request.GET.get("max_price")),
        q=(request.GET.get("q") or "").strip(),
    )

    paginator = Paginator(queryset, LOOP_LISTINGS_PER_PAGE)
    page = paginator.get_page(request.GET.get("page"))

    context = {
        "page_obj": page,
        "paginator": paginator,
        "listings": page.object_list,
        "sort": sort,
        "sort_choices": [
            ("newest", "Newest first"),
            ("price_asc", "Price: low to high"),
            ("price_desc", "Price: high to low"),
        ],
        "condition_choices": LoopItem.Condition.choices,
        "canonical_url": request.build_absolute_uri("/loop/resale/"),
        "seo_title": "Pre-loved FLASHWEAR | FLASH Loop",
        "seo_description": (
            "Shop verified pre-loved FLASHWEAR garments, or send yours back into circulation."
        ),
    }
    return render(request, "loop/resale_list.html", context)


@storefront_open
@never_cache
@require_GET
def resale_detail(request, slug: str):
    """``/loop/resale/<slug>/`` -- one listing's public page."""
    listing = selectors.public_listing(slug)
    if listing is None:
        raise Http404("No listing matches the given URL.")

    item = listing.loop_item
    product = item.product
    context = {
        "listing": listing,
        "loop_item": item,
        "product": product,
        "variant": item.variant,
        "images": list(item.images.all()),
        "circularity": circularity_info(item),
        "seo_title": f"{item.display_title} — pre-loved | FLASH Loop",
        "seo_description": (
            f"{item.display_title} in {item.get_condition_display().lower()} "
            f"condition. {listing.asking_price} {settings.CATALOG_CURRENCY_CODE}."
        ),
        "canonical_url": request.build_absolute_uri(listing.get_absolute_url()),
        "og_url": request.build_absolute_uri(listing.get_absolute_url()),
        "currency_code": settings.CATALOG_CURRENCY_CODE,
    }
    return render(request, "loop/resale_detail.html", context)


# =============================================================================
# Account area: dashboard and the creation flow
# =============================================================================


@login_required
@never_cache
@require_GET
def loop_dashboard(request):
    """``/account/loop/`` -- one customer's FLASH Loop activity."""
    dashboard = selectors.user_dashboard(request.user)
    context = {
        **dashboard,
        "loop_status_choices": LoopItem.Status.choices,
        "resale_status_choices": ResaleListing.Status.choices,
    }
    return render(request, "account/loop_dashboard.html", context)


@login_required
@never_cache
def loop_create(request):
    """``/account/loop/new/`` -- pick an owned garment, pick a path.

    The item choices are built server-side from the customer's own
    purchases; a tampered ``owned_item`` value fails choice validation
    before ownership logic runs, and the service re-verifies it anyway.
    """
    if request.method == "POST":
        form = LoopItemCreateForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                item = loop_services.create_loop_item(
                    request.user,
                    form.cleaned_data["loop_type"],
                    condition=form.cleaned_data["condition"],
                    condition_notes=form.cleaned_data.get("condition_notes", ""),
                    title=form.cleaned_data.get("title", ""),
                    description=form.cleaned_data.get("description", ""),
                    asking_price=form.cleaned_data.get("asking_price"),
                    **form.evidence_kwargs(),
                )
            except LoopError as exc:
                messages.error(request, exc.message)
            else:
                messages.success(
                    request,
                    "Draft created. Add photos if you like, then submit it for review.",
                )
                return redirect("account:loop-item-detail", pk=item.pk)
    else:
        form = LoopItemCreateForm(user=request.user)

    if not owned_items_exist(form):
        messages.info(
            request,
            "You need a delivered FLASHWEAR purchase to start a FLASH Loop.",
        )

    return render(
        request,
        "account/loop_create.html",
        {"form": form},
    )


def owned_items_exist(form: LoopItemCreateForm) -> bool:
    """Whether the customer has at least one eligible garment."""
    return bool(form.fields["owned_item"].choices and len(form.fields["owned_item"].choices) > 1)


def _own_item(request, pk: int) -> LoopItem:
    """The customer's own loop item, or 404 (never someone else's)."""
    return get_object_or_404(selectors.user_loop_items(request.user), pk=pk)


def _child(model, item: LoopItem):
    """The item's child row (listing / trade-in / recycle), or ``None``.

    The reverse OneToOne descriptor raises ``DoesNotExist`` while the item
    is still a draft; a page that renders "nothing yet" must not 500.
    """
    return model.objects.filter(loop_item=item).first()


@login_required
@never_cache
@require_GET
def loop_item_detail(request, pk: int):
    """``/account/loop/<pk>/`` -- review, photos and submit for the owner."""
    item = _own_item(request, pk)
    photo_form = LoopPhotoForm()
    can_submit = item.status == LoopItem.Status.DRAFT
    can_cancel = item.is_active
    # The child rows only exist after submission, so look them up instead of
    # touching the reverse OneToOne accessor (which raises on a draft).
    trade_in = _child(TradeInRequest, item) if item.type == LoopItem.Type.TRADE_IN else None
    recycle = _child(RecycleRequest, item) if item.type == LoopItem.Type.RECYCLE else None
    listing = _child(ResaleListing, item) if item.type == LoopItem.Type.RESALE else None
    context = {
        "loop_item": item,
        "photo_form": photo_form,
        "images": list(item.images.all()),
        "can_submit": can_submit,
        "can_cancel": can_cancel,
        "max_images": settings.LOOP_MAX_IMAGES,
        "trade_in": trade_in,
        "recycle": recycle,
        "listing": listing,
        "circularity": circularity_info(item),
    }
    return render(request, "account/loop_item_detail.html", context)


@login_required
@require_POST
def loop_item_photo_add(request, pk: int):
    """Attach one photo to the customer's own draft loop item."""
    item = _own_item(request, pk)
    if item.images.count() >= settings.LOOP_MAX_IMAGES:
        messages.error(request, f"You can upload at most {settings.LOOP_MAX_IMAGES} photos.")
        return redirect("account:loop-item-detail", pk=pk)

    form = LoopPhotoForm(request.POST, request.FILES)
    if form.is_valid():
        photo: LoopItemImage = form.save(commit=False)
        photo.loop_item = item
        photo.position = item.images.count()
        photo.full_clean()
        photo.save()
        messages.success(request, "Photo added.")
    else:
        for error in form.non_field_errors():
            messages.error(request, error)
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
    return redirect("account:loop-item-detail", pk=pk)


@login_required
@require_POST
def loop_item_submit(request, pk: int):
    """DRAFT -> SUBMITTED. Idempotent at the service layer."""
    item = _own_item(request, pk)
    try:
        loop_services.submit_loop_item(item, actor=request.user)
    except LoopError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Submitted for review. We'll be in touch.")
    return redirect("account:loop-item-detail", pk=pk)


@login_required
@require_POST
def loop_item_cancel(request, pk: int):
    """Cancel the customer's own active loop item. Staff may use admin."""
    item = _own_item(request, pk)
    try:
        loop_services.cancel_loop_item(item, actor=request.user)
    except LoopError as exc:
        messages.error(request, exc.message)
    else:
        messages.info(request, "The loop request was cancelled.")
    return redirect("account:loop")
