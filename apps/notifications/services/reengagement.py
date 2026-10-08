"""Smart Re-engagement & Customer Retention services (Phase 39).

Provides non-annoying, data-backed re-engagement triggers:
1. Post-purchase review invitations (purchase-gated, delivered orders only, 2+ days post-delivery).
2. Saved bag / cart continuation (24h+ inactive, in-stock verified, 14-day user cooldown).
3. Upcoming FLASH Drop reminders (subscribed drops, server-authoritative).
4. FLASH DNA style discovery (curated styling recommendations, 14-day user cooldown).

House Rules:
* Zero fake urgency: no countdowns, no fake scarcity, no fake price drops.
* Natural, polite tone: never reveal internal visit counts or tracking activity.
* Strict anti-spam: deduplication, deterministic idempotency keys, frequency limits, cooldowns.
* Respect customer notification preferences and marketing opt-ins.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.analytics.services import record_event
from apps.notifications.models import (
    Channel,
    Notification,
    NotificationCategory,
    NotificationSubscription,
    NotificationType,
)
from apps.notifications.services import events, preferences

logger = logging.getLogger("flashwear.notifications.reengagement")

__all__ = [
    "check_and_trigger_drop_reminders",
    "check_and_trigger_order_review_reminders",
    "check_and_trigger_saved_bag_reminders",
    "check_and_trigger_style_recommendations",
    "get_customer_reengagement_summary",
]

# Anti-spam frequency cooldown constants
REVIEW_REMINDER_COOLDOWN_DAYS = 7
SAVED_BAG_COOLDOWN_DAYS = 14
STYLE_RECOMMENDATION_COOLDOWN_DAYS = 14
DROP_REMINDER_WINDOW_HOURS = 24


def check_and_trigger_order_review_reminders(
    *,
    order_id: int | None = None,
    min_days: int = 2,
    max_days: int = 30,
) -> int:
    """Scan delivered orders and invite customers to review items they purchased.

    Strict rules:
    - Order status must be 'delivered'.
    - Delivery was between min_days (default 2) and max_days (default 30) ago.
    - Customer must be eligible to review (has not reviewed this product yet).
    - At most ONE review reminder sent to a user within REVIEW_REMINDER_COOLDOWN_DAYS (7 days).
    - Idempotency key prevents duplicate prompts for the same order and product.
    - Respects user notification preferences for the RECOMMENDATIONS category.
    """
    from apps.engagement.services import reviews as review_services
    from apps.orders.models import Order

    now = timezone.now()
    min_cutoff = now - timedelta(days=min_days)
    max_cutoff = now - timedelta(days=max_days)

    orders_qs = (
        Order.objects.filter(
            status="delivered",
            updated_at__lte=min_cutoff,
            updated_at__gte=max_cutoff,
            user__isnull=False,
            user__is_active=True,
        )
        .select_related("user")
        .prefetch_related("items__variant__product__images")
    )

    if order_id is not None:
        orders_qs = orders_qs.filter(pk=order_id)

    sent_count = 0
    cooldown_cutoff = now - timedelta(days=REVIEW_REMINDER_COOLDOWN_DAYS)

    # Track users notified in this batch to prevent stacking reminders
    notified_users: set[int] = set()

    for order in orders_qs:
        user = order.user
        if not user or user.pk in notified_users:
            continue

        # Check user-level cooldown: did this user receive a review reminder in the last 7 days?
        has_recent_reminder = Notification.objects.filter(
            user=user,
            notification_type=NotificationType.ORDER_REVIEW_REMINDER,
            created_at__gte=cooldown_cutoff,
        ).exists()
        if has_recent_reminder:
            continue

        # Find first eligible unreviewed item in this order
        for item in order.items.all():
            variant = item.variant
            if not variant or not variant.product:
                continue
            product = variant.product

            # Eligibility check (purchase-gated, unreviewed)
            can_review, _reason = review_services.eligibility(user, product)
            if not can_review:
                continue

            idempotency_key = f"review_reminder:order:{order.pk}:product:{product.pk}"
            if Notification.objects.filter(user=user, idempotency_key=idempotency_key).exists():
                continue

            primary_img = product.primary_image
            img_url = primary_img.image.url if (primary_img and primary_img.image) else ""

            context = {
                "product_name": product.name,
                "order_number": order.number,
                "order_id": order.pk,
                "product_id": product.pk,
                "product_image_url": img_url,
            }

            notifs = events.emit(
                notification_type=NotificationType.ORDER_REVIEW_REMINDER,
                user=user,
                idempotency_key=idempotency_key,
                context=context,
                action_url=product.get_absolute_url(),
                related_object_type="order",
                related_object_id=order.pk,
            )

            if notifs:
                sent_count += len(notifs)
                notified_users.add(user.pk)
                record_event(
                    "reengagement_triggered",
                    user=user,
                    object_type="product",
                    object_id=product.pk,
                    metadata={
                        "trigger": "order_review_reminder",
                        "order_number": order.number,
                    },
                    idempotency_key=idempotency_key,
                )
                # One item reminder per order delivery follow-up
                break

    return sent_count


def check_and_trigger_saved_bag_reminders(
    *,
    min_hours: int = 24,
    max_hours: int = 72,
) -> int:
    """Scan open carts and send at most one polite continuation reminder.

    Strict rules:
    - Cart must belong to an active, authenticated user.
    - Cart was modified between min_hours and max_hours ago.
    - All items must be valid and genuinely available in stock right now.
    - At most ONE reminder per user every SAVED_BAG_COOLDOWN_DAYS (14 days).
    - Idempotent: safe on repeat runs.
    - Respects user notification preferences.
    """
    from apps.shop.models import Cart

    now = timezone.now()
    min_cutoff = now - timedelta(hours=min_hours)
    max_cutoff = now - timedelta(hours=max_hours)

    carts_qs = (
        Cart.objects.filter(
            user__isnull=False,
            user__is_active=True,
            updated_at__lte=min_cutoff,
            updated_at__gte=max_cutoff,
        )
        .select_related("user")
        .prefetch_related("items__variant__stock", "items__variant__product")
    )

    sent_count = 0
    cooldown_cutoff = now - timedelta(days=SAVED_BAG_COOLDOWN_DAYS)
    notified_users: set[int] = set()

    for cart in carts_qs:
        user = cart.user
        if not user or user.pk in notified_users:
            continue

        item_count = cart.get_item_count()
        if item_count <= 0:
            continue

        # Verify real stock availability: every item must be genuinely available
        all_in_stock = True
        for cart_item in cart.items.all():
            variant = cart_item.variant
            if not variant or not variant.product or not variant.product.is_published:
                all_in_stock = False
                break
            stock = getattr(variant, "stock", None)
            if stock is None or stock.available < cart_item.quantity:
                all_in_stock = False
                break

        if not all_in_stock:
            continue

        # Check user-level cooldown (14 days)
        has_recent = Notification.objects.filter(
            user=user,
            notification_type=NotificationType.SAVED_BAG_REMINDER,
            created_at__gte=cooldown_cutoff,
        ).exists()
        if has_recent:
            continue

        idempotency_key = f"saved_bag:cart:{cart.pk}:{cart.updated_at.strftime('%Y%m%d')}"
        if Notification.objects.filter(user=user, idempotency_key=idempotency_key).exists():
            continue

        context = {
            "item_count": item_count,
        }

        notifs = events.emit(
            notification_type=NotificationType.SAVED_BAG_REMINDER,
            user=user,
            idempotency_key=idempotency_key,
            context=context,
            action_url="/shop/cart/",
            related_object_type="cart",
            related_object_id=cart.pk,
        )

        if notifs:
            sent_count += len(notifs)
            notified_users.add(user.pk)
            record_event(
                "reengagement_triggered",
                user=user,
                object_type="cart",
                object_id=cart.pk,
                metadata={
                    "trigger": "saved_bag_reminder",
                    "item_count": item_count,
                },
                idempotency_key=idempotency_key,
            )

    return sent_count


def check_and_trigger_drop_reminders(
    *,
    drop_id: int | None = None,
    hours_ahead: int = DROP_REMINDER_WINDOW_HOURS,
) -> int:
    """Scan scheduled drops starting within hours_ahead and notify subscribed users.

    Strict rules:
    - Drop must be in SCHEDULED state with a valid starts_at in the window.
    - Users must have an active NotificationSubscription for DROP_UPCOMING on this drop.
    - Idempotency key ensures exactly one reminder per (drop, user).
    - Respects DROPS category preferences.
    """
    from apps.drops.models import DropStatus, FlashDrop

    now = timezone.now()
    window_end = now + timedelta(hours=hours_ahead)

    drops_qs = FlashDrop.objects.filter(
        status=DropStatus.SCHEDULED,
        starts_at__gt=now,
        starts_at__lte=window_end,
    )
    if drop_id is not None:
        drops_qs = drops_qs.filter(pk=drop_id)

    sent_count = 0
    for drop in drops_qs:
        subs = list(
            NotificationSubscription.objects.filter(
                notification_type=NotificationType.DROP_UPCOMING,
                related_object_type="drop",
                related_object_id=drop.pk,
                user__is_active=True,
            ).select_related("user")
        )

        for sub in subs:
            user = sub.user
            idempotency_key = f"drop:{drop.pk}:upcoming_remind:{user.pk}"
            if Notification.objects.filter(user=user, idempotency_key=idempotency_key).exists():
                continue

            context = {
                "drop_name": drop.name,
                "starts_at": drop.starts_at,
            }

            notifs = events.emit(
                notification_type=NotificationType.DROP_UPCOMING,
                user=user,
                idempotency_key=idempotency_key,
                context=context,
                action_url=f"/drops/{drop.slug}/",
                related_object_type="drop",
                related_object_id=drop.pk,
            )

            if notifs:
                sent_count += len(notifs)
                record_event(
                    "reengagement_triggered",
                    user=user,
                    object_type="drop",
                    object_id=drop.pk,
                    metadata={
                        "trigger": "drop_upcoming_reminder",
                        "drop_name": drop.name,
                    },
                    idempotency_key=idempotency_key,
                )

    return sent_count


def check_and_trigger_style_recommendations(
    *,
    user=None,
    limit: int = 5,
) -> int:
    """Recommend compatible new styles to customers who have defined their FLASH DNA.

    Strict rules:
    - User has a completed style profile.
    - Relevant recommendations exist from genuine style matching.
    - Cooldown: at most ONE style recommendation notification per user every 14 days.
    - Idempotency key per user per fortnight.
    """
    from apps.accounts.models import User
    from apps.recommendations.services import get_customer_recommendations
    from apps.styling.services.style_profile import get_style_profile_summary

    users_qs = [user] if user else User.objects.filter(is_active=True)
    sent_count = 0
    now = timezone.now()
    cooldown_cutoff = now - timedelta(days=STYLE_RECOMMENDATION_COOLDOWN_DAYS)

    for u in users_qs:
        if not u or not u.is_active:
            continue

        # Verify completed FLASH DNA style profile
        summary = get_style_profile_summary(u)
        if not summary.get("is_complete"):
            continue

        # Cooldown check
        has_recent = Notification.objects.filter(
            user=u,
            notification_type=NotificationType.STYLE_RECOMMENDATION,
            created_at__gte=cooldown_cutoff,
        ).exists()
        if has_recent:
            continue

        idempotency_key = f"style_reengagement:{u.pk}:{now.strftime('%Y_week%W')}"
        if Notification.objects.filter(user=u, idempotency_key=idempotency_key).exists():
            continue

        # Genuine recommendation check
        recs = get_customer_recommendations(u, limit=limit)
        if not recs:
            continue

        top_rec = recs[0]
        product = getattr(top_rec, "product", None) or (top_rec.get("product") if isinstance(top_rec, dict) else None)
        if not product:
            continue

        idempotency_key = f"style_reengagement:{u.pk}:{now.strftime('%Y_week%W')}"

        primary_img = getattr(product, "primary_image", None)
        img_url = primary_img.image.url if (primary_img and primary_img.image) else ""

        context = {
            "product_name": product.name,
            "product_image_url": img_url,
        }

        notifs = events.emit(
            notification_type=NotificationType.STYLE_RECOMMENDATION,
            user=u,
            idempotency_key=idempotency_key,
            context=context,
            action_url="/account/",
            related_object_type="product",
            related_object_id=product.pk,
        )

        if notifs:
            sent_count += len(notifs)
            record_event(
                "reengagement_triggered",
                user=u,
                object_type="product",
                object_id=product.pk,
                metadata={
                    "trigger": "style_recommendation",
                    "product_name": product.name,
                },
                idempotency_key=idempotency_key,
            )

    return sent_count


def get_customer_reengagement_summary(user) -> dict[str, Any]:
    """Compile genuine re-engagement cues for the customer home dashboard.

    Returns:
    - pending_reviews: delivered products awaiting customer review
    - price_dropped_items: saved wishlist items with verified active price drops
    - restocked_items: saved wishlist items that recently came back in stock
    - upcoming_drops: scheduled drops the customer has subscribed to
    """
    if not user or not user.is_authenticated:
        return {
            "pending_reviews": [],
            "price_dropped_items": [],
            "restocked_items": [],
            "upcoming_drops": [],
            "has_reengagement_content": False,
        }

    from apps.drops.models import DropStatus, FlashDrop
    from apps.engagement.models import Review
    from apps.orders.models import Order
    from apps.shop.models import WishlistItem

    # 1. Delivered orders with unreviewed items (limit 2)
    pending_reviews: list[dict[str, Any]] = []
    delivered_orders = (
        Order.objects.filter(user=user, status="delivered")
        .order_by("-updated_at")[:5]
        .prefetch_related("items__variant__product__images")
    )

    reviewed_product_ids = set(
        Review.objects.filter(author=user).values_list("product_id", flat=True)
    )

    for order in delivered_orders:
        for item in order.items.all():
            variant = item.variant
            if not variant or not variant.product:
                continue
            prod = variant.product
            if prod.pk not in reviewed_product_ids:
                pending_reviews.append(
                    {
                        "product": prod,
                        "order": order,
                        "action_url": prod.get_absolute_url(),
                    }
                )
                if len(pending_reviews) >= 2:
                    break
        if len(pending_reviews) >= 2:
            break

    # 2. Wishlist items with price drops or back in stock
    price_dropped_items = []
    restocked_items = []
    wishlist = getattr(user, "wishlist", None)
    if wishlist:
        items = (
            WishlistItem.objects.filter(wishlist=wishlist)
            .select_related("product", "variant")
            .prefetch_related("product__images")
        )
        for w_item in items:
            if w_item.has_price_drop:
                price_dropped_items.append(w_item)
            elif w_item.is_back_in_stock:
                restocked_items.append(w_item)

    # 3. Upcoming drops the user subscribed to
    upcoming_drop_ids = list(
        NotificationSubscription.objects.filter(
            user=user,
            related_object_type="drop",
            notification_type__in=[NotificationType.DROP_UPCOMING, NotificationType.DROP_LIVE],
        ).values_list("related_object_id", flat=True)
    )

    upcoming_drops = list(
        FlashDrop.objects.filter(
            pk__in=upcoming_drop_ids,
            status=DropStatus.SCHEDULED,
            starts_at__gt=timezone.now(),
        ).order_by("starts_at")[:2]
    )

    has_content = bool(pending_reviews or price_dropped_items or restocked_items or upcoming_drops)

    return {
        "pending_reviews": pending_reviews,
        "price_dropped_items": price_dropped_items[:3],
        "restocked_items": restocked_items[:3],
        "upcoming_drops": upcoming_drops,
        "has_reengagement_content": has_content,
    }
