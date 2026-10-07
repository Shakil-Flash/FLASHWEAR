"""Shop signals.

Cart merge on login is the only signal in this app. Django cycles the session key
*before* ``user_logged_in`` fires, so the guest cart cannot be found by its old
session key at that point. ``get_or_create_cart`` therefore records the guest cart's
id in the session data, which ``cycle_key`` carries over to the new session, and the
receiver reads it back after login.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.shop.services import CartMergeError, merge_carts

logger = logging.getLogger("flashwear.shop")

GUEST_CART_SESSION_KEY = "guest_cart_id"


@receiver(user_logged_in)
def merge_guest_cart(sender, request, user, **kwargs):
    """Merge any guest cart into the user's cart, then mark the guest cart converted."""
    guest_cart_id = request.session.pop(GUEST_CART_SESSION_KEY, None)
    if not guest_cart_id:
        return

    from apps.shop.models import Cart

    guest_cart = Cart.objects.filter(
        pk=guest_cart_id, user__isnull=True, status=Cart.Status.ACTIVE
    ).first()
    if guest_cart is None:
        return

    # Resolve the user's cart from ``user``, never from ``request.user``: Django's
    # test client (and any other synthetic login) fires this signal with a request
    # that has no ``user`` attribute yet.
    user_cart, _ = Cart.objects.get_or_create(
        user=user,
        status=Cart.Status.ACTIVE,
        defaults={"currency": settings.CATALOG_CURRENCY_CODE},
    )
    try:
        merge_carts(guest_cart, user_cart)
    except CartMergeError:
        # Never fail a login over a cart. The guest cart stays active (it is still
        # reachable by its session key until that session expires); the customer can
        # reconcile quantities by hand.
        logger.warning("Cart merge skipped for user %s: quantity limit conflict.", user.pk)


@receiver(post_save, sender="inventory.Stock")
def on_stock_level_changed(sender, instance, **kwargs):
    """Trigger back-in-stock alerts when inventory is restored, or record out-of-stock status."""
    if kwargs.get("raw", False):
        return
    try:
        from apps.shop.models import WishlistItem
        from apps.shop.wishlist_services import check_and_trigger_back_in_stock_alerts

        if instance.available > 0:
            check_and_trigger_back_in_stock_alerts(variant=instance.variant)
        else:
            WishlistItem.objects.filter(
                variant=instance.variant,
                notify_back_in_stock=True,
                was_out_of_stock=False,
            ).update(was_out_of_stock=True)
    except Exception:
        logger.exception(
            "Failed processing stock update alert for stock pk=%s",
            getattr(instance, "pk", None),
        )


@receiver(post_save, sender="catalog.ProductVariant")
def on_variant_saved(sender, instance, **kwargs):
    """Trigger price-drop alerts when a variant price decreases."""
    if kwargs.get("raw", False):
        return
    try:
        from apps.shop.wishlist_services import check_and_trigger_price_drop_alerts

        check_and_trigger_price_drop_alerts(variant=instance)
    except Exception:
        logger.exception(
            "Failed processing price drop alert for variant pk=%s",
            getattr(instance, "pk", None),
        )
