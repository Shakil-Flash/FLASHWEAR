"""Smart Wishlist, Price Alerts & Back-in-Stock business logic (Phase 33).

Reuses existing Wishlist, Catalog, Inventory, Notifications, Analytics.
Guarantees:
- Real data only (no fake urgency, no fake price history).
- Idempotent and deduplicated notifications.
- Variant-aware stock checks.
- Safe cart conversions.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from apps.analytics.services import record_event
from apps.catalog.models import Product, ProductVariant
from apps.notifications.models import NotificationType
from apps.notifications.services import events
from apps.shop.models import Wishlist, WishlistItem

logger = logging.getLogger("flashwear.shop.wishlist")

__all__ = [
    "add_all_available_to_bag",
    "add_to_wishlist",
    "check_and_trigger_back_in_stock_alerts",
    "check_and_trigger_price_drop_alerts",
    "get_or_create_wishlist",
    "remove_from_wishlist",
    "toggle_wishlist_alert",
]


def get_or_create_wishlist(user) -> Wishlist:
    """Get or create the user's wishlist, tolerating a concurrent create race."""
    try:
        wishlist, _ = Wishlist.objects.get_or_create(user=user)
    except IntegrityError:
        wishlist = Wishlist.objects.get(user=user)
    return wishlist


def add_to_wishlist(
    user,
    product: Product,
    variant: ProductVariant | None = None,
    *,
    note: str = "",
    notify_price_drop: bool = False,
    notify_back_in_stock: bool = False,
    request: Any = None,
) -> tuple[WishlistItem, bool]:
    """Add a product or specific variant to the user's wishlist with real snapshots."""
    wishlist = get_or_create_wishlist(user)

    # Determine baseline price at time of adding
    price_when_added = None
    if variant is not None:
        price_when_added = variant.price
    elif product is not None:
        low, _ = product.price_range
        price_when_added = low

    # Determine initial stock status for back-in-stock tracking
    was_out_of_stock = False
    if variant is not None:
        stock = getattr(variant, "stock", None)
        if stock is not None and stock.available <= 0:
            was_out_of_stock = True
    elif product is not None and not product.is_in_stock:
        was_out_of_stock = True

    try:
        item, created = WishlistItem.objects.get_or_create(
            wishlist=wishlist,
            product=product,
            variant=variant,
            defaults={
                "note": note,
                "price_when_added": price_when_added,
                "notify_price_drop": notify_price_drop,
                "notify_back_in_stock": notify_back_in_stock,
                "was_out_of_stock": was_out_of_stock,
            },
        )
    except IntegrityError:
        item = WishlistItem.objects.get(
            wishlist=wishlist,
            product=product,
            variant=variant,
        )
        created = False

    if not created:
        updates = []
        if note and item.note != note:
            item.note = note
            updates.append("note")
        if notify_price_drop and not item.notify_price_drop:
            item.notify_price_drop = True
            updates.append("notify_price_drop")
        if notify_back_in_stock and not item.notify_back_in_stock:
            item.notify_back_in_stock = True
            item.was_out_of_stock = was_out_of_stock
            updates.extend(["notify_back_in_stock", "was_out_of_stock"])
        if updates:
            item.save(update_fields=[*updates, "updated_at"])

    # Analytics
    record_event(
        "wishlist_add",
        request=request,
        user=user,
        object_type="product",
        object_id=product.pk,
        metadata={"variant": variant.pk if variant else None},
    )
    if notify_price_drop:
        record_event(
            "price_alert_enabled",
            request=request,
            user=user,
            object_type="wishlist_item",
            object_id=item.pk,
            metadata={"product_id": product.pk, "variant_id": variant.pk if variant else None},
        )
    if notify_back_in_stock:
        record_event(
            "back_in_stock_alert_enabled",
            request=request,
            user=user,
            object_type="wishlist_item",
            object_id=item.pk,
            metadata={"product_id": product.pk, "variant_id": variant.pk if variant else None},
        )

    return item, created


def remove_from_wishlist(user, item_pk: int, request: Any = None) -> bool:
    """Remove a saved item from user's wishlist, logging analytics."""
    wishlist = get_or_create_wishlist(user)
    try:
        item = WishlistItem.objects.get(pk=item_pk, wishlist=wishlist)
    except WishlistItem.DoesNotExist:
        return False

    record_event(
        "wishlist_remove",
        request=request,
        user=user,
        object_type="product",
        object_id=item.product_id,
        metadata={"variant": item.variant_id},
    )
    item.delete()
    return True


def toggle_wishlist_alert(
    user,
    item_pk: int,
    alert_type: str,
    enabled: bool | None = None,
    request: Any = None,
) -> WishlistItem:
    """Toggle price-drop or back-in-stock alerts for a specific wishlist item."""
    wishlist = get_or_create_wishlist(user)
    item = WishlistItem.objects.select_related("product", "variant").get(
        pk=item_pk, wishlist=wishlist
    )

    if alert_type == "price_drop":
        new_val = (not item.notify_price_drop) if enabled is None else bool(enabled)
        item.notify_price_drop = new_val
        if new_val:
            if item.price_when_added is None:
                item.price_when_added = item.current_price
            record_event(
                "price_alert_enabled",
                request=request,
                user=user,
                object_type="wishlist_item",
                object_id=item.pk,
                metadata={"product_id": item.product_id, "variant_id": item.variant_id},
            )
        item.save(update_fields=["notify_price_drop", "price_when_added", "updated_at"])

    elif alert_type == "back_in_stock":
        new_val = (not item.notify_back_in_stock) if enabled is None else bool(enabled)
        item.notify_back_in_stock = new_val
        if new_val:
            if item.is_out_of_stock:
                item.was_out_of_stock = True
            record_event(
                "back_in_stock_alert_enabled",
                request=request,
                user=user,
                object_type="wishlist_item",
                object_id=item.pk,
                metadata={"product_id": item.product_id, "variant_id": item.variant_id},
            )
        item.save(update_fields=["notify_back_in_stock", "was_out_of_stock", "updated_at"])

    else:
        raise ValueError(f"Unknown alert_type: {alert_type}")

    return item


PRICE_DROP_COOLDOWN_HOURS = 48
MIN_PRICE_DROP_PERCENT = Decimal("2.0")
MIN_PRICE_DROP_AMOUNT = Decimal("5.00")
STOCK_ALERT_COOLDOWN_HOURS = 12


def check_and_trigger_price_drop_alerts(
    *, product: Product | None = None, variant: ProductVariant | None = None
) -> int:
    """Scan and dispatch price drop alerts for qualifying real price decreases.

    Anti-spam rules:
    1. Real price decrease strictly lower than baseline.
    2. Meaningful threshold: at least 2% drop or at least ৳5.00 decrease.
    3. Cooldown: do not notify if notified within 48 hours unless a major further drop
       (>=10%) occurs.
    4. Deterministic idempotency key: price_drop:<item_pk>:<price>.
    """
    from datetime import timedelta

    qs = (
        WishlistItem.objects.filter(notify_price_drop=True)
        .select_related("wishlist__user", "product__brand", "variant__color", "variant__size")
        .prefetch_related("product__images")
    )
    if variant is not None:
        qs = qs.filter(Q(variant=variant) | Q(product=variant.product, variant__isnull=True))
    elif product is not None:
        qs = qs.filter(product=product)

    sent_count = 0
    now = timezone.now()
    cooldown_threshold = now - timedelta(hours=PRICE_DROP_COOLDOWN_HOURS)

    for item in qs:
        user = item.wishlist.user
        if not user or not user.is_active:
            continue

        curr_price = item.current_price
        baseline = item.last_notified_price or item.price_when_added
        if (
            baseline is None
            and item.variant
            and item.variant.compare_at_price
            and item.variant.compare_at_price > curr_price
        ):
            baseline = item.variant.compare_at_price

        if curr_price is None or baseline is None:
            continue

        # Only notify when current price is strictly less than baseline price
        if curr_price >= baseline:
            continue

        # Prevent duplicate notifications for the exact same price level
        if item.last_notified_price is not None and item.last_notified_price == curr_price:
            continue

        # Anti-spam: enforce minimum drop threshold (avoid spam on tiny fractional rounding changes)
        price_diff = baseline - curr_price
        drop_pct = (price_diff / baseline) * Decimal("100.0") if baseline > 0 else Decimal("0.0")
        if drop_pct < MIN_PRICE_DROP_PERCENT and price_diff < MIN_PRICE_DROP_AMOUNT:
            continue

        # Anti-spam cooldown: if recently notified, require a significant further decrease (>= 10%)
        if item.last_notified_price is not None:
            from apps.notifications.models import Channel, Notification

            recent_notif = Notification.objects.filter(
                user=user,
                notification_type=NotificationType.PRICE_DROP,
                related_object_id=item.product_id,
                channel=Channel.IN_APP,
                created_at__gte=cooldown_threshold,
            ).first()
            if recent_notif:
                further_diff = item.last_notified_price - curr_price
                further_pct = (further_diff / item.last_notified_price) * Decimal("100.0")
                if further_pct < Decimal("10.0"):
                    continue

        idempotency_key = f"price_drop:{item.pk}:{curr_price}"
        primary_img = item.product.primary_image
        img_url = primary_img.image.url if (primary_img and primary_img.image) else ""

        context = {
            "product_name": item.product.name,
            "variant_label": item.variant.option_label if item.variant else "",
            "old_price": f"৳{baseline:,.2f}",
            "new_price": f"৳{curr_price:,.2f}",
            "product_image_url": img_url,
        }

        notifs = events.emit(
            notification_type=NotificationType.PRICE_DROP,
            user=user,
            idempotency_key=idempotency_key,
            context=context,
            action_url=item.get_absolute_url(),
            related_object_type="product",
            related_object_id=item.product_id,
        )

        item.last_notified_price = curr_price
        item.save(update_fields=["last_notified_price", "updated_at"])

        record_event(
            "price_alert_triggered",
            user=user,
            object_type="product",
            object_id=item.product_id,
            metadata={
                "old_price": str(baseline),
                "new_price": str(curr_price),
                "wishlist_item": item.pk,
                "variant_id": item.variant_id,
            },
            idempotency_key=idempotency_key,
        )
        if notifs:
            sent_count += len(notifs)
        else:
            sent_count += 1

    return sent_count


def check_and_trigger_back_in_stock_alerts(
    *, variant: ProductVariant | None = None, product: Product | None = None
) -> int:
    """Scan and dispatch back-in-stock alerts when an item transitions from out-of-stock.

    Strict anti-spam conditions:
    1. Previous state was out of stock (was_out_of_stock is True).
    2. Real inventory is currently available (is_in_stock is True).
    3. Stock jitter protection: minimum cooldown of 12 hours since last back-in-stock alert.
    4. Resets was_out_of_stock to False to prevent repeated alerts on multiple updates.
    """
    from datetime import timedelta

    qs = (
        WishlistItem.objects.filter(notify_back_in_stock=True, was_out_of_stock=True)
        .select_related(
            "wishlist__user", "product", "variant__stock", "variant__color", "variant__size"
        )
        .prefetch_related("product__images")
    )
    if variant is not None:
        qs = qs.filter(Q(variant=variant) | Q(product=variant.product, variant__isnull=True))
    elif product is not None:
        qs = qs.filter(product=product)

    sent_count = 0
    now = timezone.now()
    stock_cooldown_threshold = now - timedelta(hours=STOCK_ALERT_COOLDOWN_HOURS)

    for item in qs:
        user = item.wishlist.user
        if not user or not user.is_active:
            continue

        # Must genuinely be in stock now
        if not item.is_in_stock:
            continue

        # Jitter protection: do not re-alert if alerted in the last 12 hours
        if item.last_notified_stock_at and item.last_notified_stock_at > stock_cooldown_threshold:
            item.was_out_of_stock = False
            item.save(update_fields=["was_out_of_stock", "updated_at"])
            continue

        idempotency_key = f"back_in_stock:{item.pk}:{now.strftime('%Y%m%d%H')}"
        primary_img = item.product.primary_image
        img_url = primary_img.image.url if (primary_img and primary_img.image) else ""

        context = {
            "product_name": item.product.name,
            "variant_label": item.variant.option_label if item.variant else "",
            "product_image_url": img_url,
        }

        notifs = events.emit(
            notification_type=NotificationType.BACK_IN_STOCK,
            user=user,
            idempotency_key=idempotency_key,
            context=context,
            action_url=item.get_absolute_url(),
            related_object_type="product",
            related_object_id=item.product_id,
        )

        item.was_out_of_stock = False
        item.last_notified_stock_at = now
        item.save(update_fields=["was_out_of_stock", "last_notified_stock_at", "updated_at"])

        record_event(
            "back_in_stock_triggered",
            user=user,
            object_type="product",
            object_id=item.product_id,
            metadata={
                "variant_id": item.variant_id,
                "wishlist_item": item.pk,
            },
            idempotency_key=idempotency_key,
        )
        if notifs:
            sent_count += len(notifs)
        else:
            sent_count += 1

    return sent_count


def add_all_available_to_bag(request: Any, user: Any) -> tuple[int, int]:
    """Move all currently available wishlist items into the user's cart.

    Returns (added_count, skipped_count).
    """
    from apps.shop.services import add_to_cart, get_or_create_cart

    cart = get_or_create_cart(request)
    items = list(
        WishlistItem.objects.filter(wishlist__user=user)
        .select_related("product", "variant__stock", "variant__color", "variant__size")
        .prefetch_related("product__variants__stock")
    )

    added = 0
    skipped = 0

    for item in items:
        if not item.is_available:
            skipped += 1
            continue

        var = item.variant
        if var is None:
            purchasable = item.product.purchasable_variants
            if len(purchasable) == 1:
                var = purchasable[0]
            else:
                # Requires customer choice (multiple colors/sizes)
                skipped += 1
                continue

        stock = getattr(var, "stock", None)
        if stock is not None and stock.available <= 0:
            skipped += 1
            continue

        try:
            add_to_cart(cart, var, quantity=1)
            record_event(
                "wishlist_to_cart",
                request=request,
                user=user,
                object_type="variant",
                object_id=var.pk,
                metadata={"product": item.product_id, "wishlist_item": item.pk},
            )
            item.delete()
            added += 1
        except Exception:
            skipped += 1

    return added, skipped
