"""Cart and wishlist views.

All read/write operations go through the service layer so business logic
stays out of the HTTP layer.

Every mutation is POST-only (no GET mutations) and requires the CSRF token.
HTMX requests receive JSON; classic form posts are redirected.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.catalog.models import Product, ProductVariant
from apps.shop.models import Wishlist, WishlistItem
from apps.shop.services import (
    add_to_cart,
    clear_cart,
    get_cart_for_request,
    get_cart_totals,
    get_or_create_cart,
    get_price_changes,
    remove_from_cart,
    update_cart_item_quantity,
    validate_cart_items,
)

# =============================================================================
# Helpers
# =============================================================================


def _is_htmx(request) -> bool:
    """True when the request came from HTMX."""
    return request.headers.get("HX-Request") == "true"


def _parse_quantity(request) -> int | None:
    """Read the quantity POST field, returning None when it is not a positive int."""
    raw = request.POST.get("quantity", "1")
    try:
        quantity = int(raw)
    except (TypeError, ValueError):
        return None
    if quantity < 1:
        return None
    return quantity


def _cart_payload(cart) -> dict:
    """JSON body used by HTMX cart responses."""
    totals = get_cart_totals(cart)
    return {
        "item_count": totals["item_count"],
        "total_quantity": totals["total_quantity"],
        "subtotal": str(totals["subtotal"]),
        "currency": totals["currency"],
    }


def _handle_error(request, error: Exception):
    """Turn a service error into an HTMX JSON response or a flash + redirect."""
    if _is_htmx(request):
        return JsonResponse({"error": str(error)}, status=400)
    messages.error(request, str(error))
    return redirect("shop:cart")


# =============================================================================
# Cart views
# =============================================================================


def cart_detail(request):
    """Display the cart page with all items, totals and validation problems."""
    cart = get_or_create_cart(request)
    context = {
        "cart": cart,
        "totals": get_cart_totals(cart),
        "errors": validate_cart_items(cart),
        "price_changes": get_price_changes(cart),
    }
    return render(request, "shop/cart.html", context)


@require_POST
def cart_add(request):
    """Add a variant to the cart via POST."""
    variant_id = request.POST.get("variant_id")
    quantity = _parse_quantity(request)

    if not variant_id or quantity is None:
        return _handle_error(
            request,
            ValidationError(_("A valid variant and quantity are required."), code="invalid_input"),
        )

    variant = get_object_or_404(ProductVariant, pk=variant_id, is_active=True)
    cart = get_or_create_cart(request)

    try:
        add_to_cart(cart, variant, quantity=quantity)
    except ValidationError as err:
        return _handle_error(request, err)

    if _is_htmx(request):
        return JsonResponse(_cart_payload(cart))

    messages.success(request, _("Added to cart"))
    return redirect("shop:cart")


@require_POST
def cart_update(request, item_pk: int):
    """Update a cart item's quantity."""
    quantity = _parse_quantity(request)

    if quantity is None:
        return _handle_error(
            request, ValidationError(_("Quantity must be at least 1."), code="quantity_invalid")
        )

    cart = get_or_create_cart(request)
    try:
        update_cart_item_quantity(cart, item_pk, quantity)
    except ValidationError as err:
        return _handle_error(request, err)

    if _is_htmx(request):
        return JsonResponse(_cart_payload(cart))

    return redirect("shop:cart")


@require_POST
def cart_remove(request, item_pk: int):
    """Remove an item from the cart."""
    cart = get_or_create_cart(request)
    try:
        remove_from_cart(cart, item_pk)
    except ValidationError as err:
        return _handle_error(request, err)

    if _is_htmx(request):
        return JsonResponse(_cart_payload(cart))

    messages.success(request, _("Item removed from cart"))
    return redirect("shop:cart")


@require_POST
def cart_clear(request):
    """Clear all items from the cart."""
    cart = get_or_create_cart(request)
    clear_cart(cart)
    messages.success(request, _("Cart cleared"))

    if _is_htmx(request):
        return JsonResponse({"item_count": 0, "total_quantity": 0, "subtotal": "0.00"})

    return redirect("shop:cart")


# =============================================================================
# Wishlist views
# =============================================================================


def _get_or_create_wishlist(user):
    """Get or create the user's wishlist, tolerating a create race."""
    try:
        wishlist, _ = Wishlist.objects.get_or_create(user=user)
    except IntegrityError:
        wishlist = Wishlist.objects.get(user=user)
    return wishlist


@login_required
@never_cache
def wishlist_detail(request):
    """Display the user's wishlist."""
    wishlist = _get_or_create_wishlist(request.user)
    return render(request, "shop/wishlist.html", {"wishlist": wishlist})


@login_required
@require_POST
def wishlist_add(request):
    """Add a product (optionally a variant) to the wishlist."""
    product_id = request.POST.get("product_id")
    variant_id = request.POST.get("variant_id")

    if not product_id:
        if _is_htmx(request):
            return JsonResponse({"error": _("Product ID required")}, status=400)
        messages.error(request, _("Product ID required"))
        return redirect("shop:wishlist")

    product = get_object_or_404(Product, pk=product_id)
    # is_published is a property (not a column), so it cannot go in the ORM filter.
    if not product.is_published:
        if _is_htmx(request):
            return JsonResponse({"error": _("Product not found")}, status=404)
        messages.error(request, _("Product not found"))
        return redirect("shop:wishlist")

    variant = None
    if variant_id:
        variant = get_object_or_404(
            ProductVariant,
            pk=variant_id,
            is_active=True,
            product=product,
        )

    wishlist = _get_or_create_wishlist(request.user)
    try:
        WishlistItem.objects.get_or_create(
            wishlist=wishlist,
            product=product,
            variant=variant,
        )
    except IntegrityError:
        # Duplicate created concurrently; it is already on the wishlist.
        pass

    if _is_htmx(request):
        return JsonResponse({"count": wishlist.get_item_count()})

    messages.success(request, _("Added to wishlist"))
    return redirect("shop:wishlist")


@login_required
@require_POST
def wishlist_remove(request, item_pk: int):
    """Remove an item from the wishlist."""
    wishlist = get_object_or_404(Wishlist, user=request.user)
    item = get_object_or_404(WishlistItem, pk=item_pk, wishlist=wishlist)
    item.delete()

    if _is_htmx(request):
        return JsonResponse({"count": wishlist.get_item_count()})

    messages.success(request, _("Removed from wishlist"))
    return redirect("shop:wishlist")


# =============================================================================
# HTMX / AJAX endpoints for mini-cart and badge counts
# =============================================================================


def cart_count(request):
    """Return the cart quantity as JSON (for mini-cart badge).

    Read-only: an anonymous visitor who has never opened a cart gets 0 and no row is
    written, so a GET never mutates state.
    """
    cart = get_cart_for_request(request)
    count = cart.get_item_count() if cart is not None else 0
    return JsonResponse({"count": count})


def wishlist_count(request):
    """Return the wishlist item count as JSON."""
    if request.user.is_authenticated:
        wishlist = _get_or_create_wishlist(request.user)
        return JsonResponse({"count": wishlist.get_item_count()})
    return JsonResponse({"count": 0})


def cart_totals(request):
    """Return cart totals as JSON (for HTMX updates)."""
    cart = get_or_create_cart(request)
    totals = get_cart_totals(cart)
    return JsonResponse({**totals, "subtotal": str(totals["subtotal"])})
