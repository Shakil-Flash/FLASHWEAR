"""Cart and wishlist views.

All read/write operations go through the service layer so business logic
stays out of the HTTP layer.

Every mutation is POST-only (no GET mutations) and requires the CSRF token.
HTMX requests receive JSON; classic form posts are redirected.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.analytics.services import record_event
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
    """Display the cart page with all items, totals, validation problems and recommendations."""
    from apps.catalog.merchandising import get_continue_shopping_items, get_recently_viewed
    from apps.catalog.selectors import homepage_new_arrivals
    from apps.shop.checkout import discount_breakdown
    from apps.shop.models import CheckoutSession

    cart = get_or_create_cart(request)
    items = list(cart.get_items())
    totals = get_cart_totals(cart, items=items)
    subtotal = totals["subtotal"]

    free_threshold = Decimal("100.00")
    free_remaining = max(Decimal("0.00"), free_threshold - subtotal)
    free_qualified = subtotal >= free_threshold
    free_pct = (
        min(100, int((subtotal / free_threshold) * 100))
        if free_threshold > Decimal("0.00")
        else 100
    )

    promo_code = ""
    promo_discount = Decimal("0.00")
    loyalty_points = 0
    loyalty_discount = Decimal("0.00")

    if request.user.is_authenticated and bool(items):
        checkout = (
            CheckoutSession.objects.filter(
                user=request.user,
                status__in=[CheckoutSession.Status.OPEN, CheckoutSession.Status.VALIDATED],
            )
            .order_by("-created_at")
            .first()
        )
        if checkout and (checkout.promotion_code or checkout.loyalty_points):
            try:
                breakdown = discount_breakdown(
                    checkout,
                    promotion_code=checkout.promotion_code,
                    loyalty_points=checkout.loyalty_points,
                )
                promo_code = breakdown.promotion_code
                promo_discount = breakdown.promotion_discount
                loyalty_points = breakdown.loyalty_points
                loyalty_discount = breakdown.loyalty_discount
            except Exception:
                pass

    shipping_estimate = Decimal("0.00") if free_qualified else Decimal("5.00")
    estimated_total = max(
        Decimal("0.00"),
        (subtotal - promo_discount - loyalty_discount + shipping_estimate).quantize(
            Decimal("0.01")
        ),
    )

    continue_items = get_continue_shopping_items(request, limit=4)
    if not continue_items:
        continue_items = get_recently_viewed(request, limit=4)
    if not continue_items:
        continue_items = homepage_new_arrivals(limit=4)

    record_event(
        "cart_view",
        request=request,
        object_type="cart",
        object_id=cart.pk,
        metadata={
            "item_count": len(items),
            "total_quantity": sum(i.quantity for i in items),
        },
    )

    context = {
        "cart": cart,
        "items": items,
        "totals": totals,
        "subtotal": subtotal,
        "promo_code": promo_code,
        "promo_discount": promo_discount,
        "loyalty_points": loyalty_points,
        "loyalty_discount": loyalty_discount,
        "shipping_estimate": shipping_estimate,
        "estimated_total": estimated_total,
        "free_threshold": free_threshold,
        "free_remaining": free_remaining,
        "free_qualified": free_qualified,
        "free_pct": free_pct,
        "errors": validate_cart_items(cart, items=items),
        "price_changes": get_price_changes(cart, items=items),
        "continue_items": continue_items,
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

    record_event(
        "cart_add",
        request=request,
        object_type="variant",
        object_id=variant.pk,
        metadata={"quantity": quantity, "product": variant.product_id},
    )
    record_event(
        "product_to_cart",
        request=request,
        object_type="variant",
        object_id=variant.pk,
        metadata={"quantity": quantity, "product": variant.product_id, "sku": variant.sku},
    )

    if (
        _is_htmx(request)
        or request.headers.get("accept") == "application/json"
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
    ):
        payload = _cart_payload(cart)
        payload["success"] = True
        payload["message"] = str(_("Added to bag."))
        img_url = ""
        if variant.product.primary_image:
            img_url = variant.product.primary_image.image.url
        payload["added_variant"] = {
            "id": variant.pk,
            "sku": variant.sku,
            "product_name": variant.product.name,
            "option_label": variant.option_label or "",
            "price": str(variant.price),
            "image_url": img_url,
        }
        return JsonResponse(payload)

    next_url = request.POST.get("next")
    messages.success(request, _("Added to your bag."))
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        sep = "&" if "?" in next_url else "?"
        return redirect(f"{next_url}{sep}added={variant.pk}")

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

    record_event(
        "cart_remove",
        request=request,
        object_type="cart_item",
        object_id=item_pk,
    )

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
    """Display the user's wishlist with real availability status and options."""
    wishlist = _get_or_create_wishlist(request.user)
    items = list(
        wishlist.items.select_related(
            "product__brand", "variant__color", "variant__size"
        ).prefetch_related("product__variants__stock", "product__images")
    )
    enriched_items = []
    for item in items:
        prod = item.product
        var = item.variant
        is_avail = prod.is_published
        stock_msg = "In stock"
        is_in_stock = False

        if not is_avail:
            stock_msg = "Unavailable"
        elif var:
            stock = getattr(var, "stock", None)
            if stock:
                if stock.available <= 0:
                    stock_msg = "Sold out"
                elif stock.available <= 5:
                    stock_msg = f"Low stock: {stock.available} left"
                    is_in_stock = True
                else:
                    stock_msg = "In stock"
                    is_in_stock = True
            else:
                is_in_stock = True
        else:
            is_in_stock = prod.is_in_stock
            if not is_in_stock:
                stock_msg = "Sold out"
            else:
                stock_msg = "In stock"

        is_low_stock = bool(
            var
            and getattr(var, "stock", None)
            and 0 < var.stock.available <= 5
        )
        is_on_sale = bool(var.is_discounted if var else prod.is_on_sale)
        is_recently_added = bool(
            item.created_at
            and item.created_at >= timezone.now() - timedelta(days=14)
        )

        enriched_items.append(
            {
                "item": item,
                "is_available": is_avail,
                "is_in_stock": is_in_stock,
                "is_low_stock": is_low_stock,
                "is_sold_out": not is_in_stock,
                "is_on_sale": is_on_sale,
                "is_recently_added": is_recently_added,
                "stock_msg": stock_msg,
                "purchasable_variants": prod.purchasable_variants if not var else [],
            }
        )

    return render(
        request,
        "shop/wishlist.html",
        {"wishlist": wishlist, "enriched_items": enriched_items},
    )


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

    record_event(
        "wishlist_add",
        request=request,
        object_type="product",
        object_id=product.pk,
        metadata={"variant": variant.pk if variant else None},
    )

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
    record_event(
        "wishlist_remove",
        request=request,
        object_type="product",
        object_id=item.product_id,
    )
    item.delete()

    if _is_htmx(request):
        return JsonResponse({"count": wishlist.get_item_count()})

    messages.success(request, _("Removed from wishlist"))
    return redirect("shop:wishlist")


@login_required
@require_POST
def wishlist_move_to_bag(request, item_pk: int):
    """Move a wishlist item into the customer's cart, tracking wishlist_to_cart."""
    wishlist = get_object_or_404(Wishlist, user=request.user)
    item = get_object_or_404(
        WishlistItem.objects.select_related("product", "variant"),
        pk=item_pk,
        wishlist=wishlist,
    )

    if not item.product.is_published:
        messages.error(request, _("This product is currently unavailable."))
        return redirect("shop:wishlist")

    variant = item.variant
    if variant is None:
        variant_id = request.POST.get("variant_id")
        if variant_id:
            variant = get_object_or_404(
                ProductVariant,
                pk=variant_id,
                product=item.product,
                is_active=True,
            )

    if variant is None:
        messages.error(request, _("Please choose a size or colour before adding to bag."))
        return redirect("shop:wishlist")

    if not variant.is_active:
        messages.error(request, _("Selected option is no longer available."))
        return redirect("shop:wishlist")

    cart = get_or_create_cart(request)
    try:
        add_to_cart(cart, variant, quantity=1)
    except ValidationError as err:
        return _handle_error(request, err)

    record_event(
        "wishlist_to_cart",
        request=request,
        object_type="variant",
        object_id=variant.pk,
        metadata={"product": item.product_id, "wishlist_item": item.pk},
    )

    if request.POST.get("keep_in_wishlist") != "1":
        item.delete()

    messages.success(request, _("Moved %(product)s to your bag.") % {"product": item.product.name})
    return redirect("shop:cart")


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


def cart_mini(request):
    """Return JSON for the lightweight mini-cart slide-over drawer."""
    from decimal import Decimal

    cart = get_cart_for_request(request)
    if cart is None or cart.is_empty():
        return JsonResponse({
            "is_empty": True,
            "items": [],
            "totals": {
                "item_count": 0,
                "total_quantity": 0,
                "subtotal": "0.00",
                "currency": "USD",
            },
            "free_shipping_threshold": "100.00",
            "amount_for_free_shipping": "100.00",
        })

    items = list(cart.get_items())
    totals = get_cart_totals(cart, items=items)
    subtotal = totals["subtotal"]
    free_threshold = Decimal("100.00")
    needed_for_free = max(Decimal("0.00"), free_threshold - subtotal)

    items_data = []
    for item in items:
        img_url = ""
        if item.variant and item.variant.product.primary_image:
            img_url = item.variant.product.primary_image.image.url
        items_data.append({
            "id": item.pk,
            "product_name": item.variant.product.name,
            "product_url": item.variant.product.get_absolute_url(),
            "option_label": item.variant.option_label or "",
            "sku": item.variant.sku,
            "quantity": item.quantity,
            "price": str(item.price_snapshot),
            "line_total": str(item.line_total),
            "image_url": img_url,
            "is_eligible": item.variant_is_eligible,
        })

    return JsonResponse({
        "is_empty": False,
        "items": items_data,
        "totals": {
            "item_count": totals["item_count"],
            "total_quantity": totals["total_quantity"],
            "subtotal": str(subtotal),
            "currency": totals["currency"],
        },
        "free_shipping_threshold": str(free_threshold),
        "amount_for_free_shipping": str(needed_for_free),
    })
