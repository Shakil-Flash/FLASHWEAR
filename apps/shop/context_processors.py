"""Template context for the shop app.

The navbar badge needs the cart count on every render. Reading (rather than creating)
the cart keeps plain GETs side-effect free: an anonymous visitor who never adds anything
gets ``0`` and no row is written.
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest

from apps.shop.services import get_cart_for_request


def cart_summary(request: HttpRequest) -> dict:
    """Expose cart/wishlist counts and cart limits to every template.

    Defensive by design: this runs on error pages too, where a synthesised request
    may carry neither ``user`` nor ``session``. Those pages get zeroes rather than
    a second exception on top of the first.
    """
    defaults = {
        "cart_count": 0,
        "wishlist_count": 0,
        "cart_max_quantity": getattr(settings, "CART_MAX_QUANTITY_PER_ITEM", 99),
    }

    if not hasattr(request, "user") or not hasattr(request, "session"):
        return defaults

    cart = get_cart_for_request(request)
    cart_count = cart.get_item_count() if cart is not None else 0

    wishlist_count = 0
    if request.user.is_authenticated:
        wishlist = getattr(request.user, "wishlist", None)
        wishlist_count = wishlist.get_item_count() if wishlist is not None else 0

    return {
        "cart_count": cart_count,
        "wishlist_count": wishlist_count,
        "cart_max_quantity": defaults["cart_max_quantity"],
    }
