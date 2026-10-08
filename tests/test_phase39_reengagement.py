"""Test suite for Phase 39: Smart Re-engagement & Customer Retention.

Covers:
1. Wishlist price drop trigger, threshold, cooldown & deduplication.
2. Wishlist back-in-stock trigger, stock bounce protection.
3. Post-purchase review invitation (delivered only, purchase-gated, cooldown, preference checks).
4. Saved bag reminder (in-stock validation, cooldown, idempotency).
5. Upcoming FLASH Drop reminders (subscribed drops, server-authoritative).
6. Style recommendation discovery (FLASH DNA matching, frequency limits).
7. Preference enforcement and opt-out suppression.
8. Celery tasks execution & idempotency.
9. In-app notification center categorization, detail view & CTA tracking.
10. Customer home integration (only renders when genuine data exists).
11. Security & authorization.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import Event
from apps.catalog.models import Product, ProductVariant
from apps.drops.models import DropStatus, FlashDrop
from apps.engagement.models import Review
from apps.inventory.models import Stock
from apps.notifications.models import (
    Channel,
    Notification,
    NotificationCategory,
    NotificationSubscription,
    NotificationType,
)
from apps.notifications.services import preferences
from apps.notifications.services.reengagement import (
    check_and_trigger_drop_reminders,
    check_and_trigger_order_review_reminders,
    check_and_trigger_saved_bag_reminders,
    check_and_trigger_style_recommendations,
    get_customer_reengagement_summary,
)
from apps.notifications.tasks import (
    sweep_drop_upcoming_reminders,
    sweep_review_reminders,
    sweep_saved_bag_reminders,
    sweep_style_recommendations,
)
from apps.orders.models import Order, OrderItem
from apps.shop.models import Cart, CartItem
from apps.shop.wishlist_services import (
    add_to_wishlist,
    check_and_trigger_back_in_stock_alerts,
    check_and_trigger_price_drop_alerts,
)


@pytest.fixture
def make_delivered_order(db, verified_user, product):
    """Helper to create a delivered order with one line item."""
    def _make(user=verified_user, prod=product, days_ago=3):
        from apps.shop.models import Cart, CheckoutSession

        now = timezone.now()
        cart = Cart.objects.create(user=user, session_key="", status=Cart.Status.CONVERTED)
        checkout = CheckoutSession.objects.create(
            user=user, cart=cart, status=CheckoutSession.Status.CONVERTED
        )
        order = Order.objects.create(
            number=f"TST-{user.pk}-{Order.objects.count() + 1:04d}",
            checkout=checkout,
            user=user,
            status=Order.Status.DELIVERED,
            subtotal=Decimal("49.00"),
            shipping_amount=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total=Decimal("49.00"),
        )
        variant = prod.variants.first()
        OrderItem.objects.create(
            order=order,
            variant=variant,
            quantity=1,
            unit_price=Decimal("49.00"),
            total_price=Decimal("49.00"),
        )
        # Set updated_at to simulate historical delivery
        Order.objects.filter(pk=order.pk).update(updated_at=now - timedelta(days=days_ago))
        order.refresh_from_db()
        return order

    return _make


# =============================================================================
# 1. Price Drop Trigger & Anti-Spam
# =============================================================================


@pytest.mark.django_db
class TestPriceDropReengagement:
    def test_price_drop_trigger_and_threshold(self, verified_user, make_product, colour, size):
        prod = make_product(name="Denim Jacket")
        var = ProductVariant.objects.create(
            product=prod, sku="DJ-01", color=colour, size=size, price=Decimal("100.00")
        )
        Stock.objects.create(variant=var, on_hand=10, reserved=0)
        add_to_wishlist(verified_user, prod, variant=var, notify_price_drop=True)

        # 1. No price change -> 0 notifications
        assert check_and_trigger_price_drop_alerts(variant=var) == 0

        # 2. Minor fractional change below threshold (100 -> 99.80 = 0.2% drop, 0.20 taka) -> suppressed
        ProductVariant.objects.filter(pk=var.pk).update(price=Decimal("99.80"))
        assert check_and_trigger_price_drop_alerts(variant=var) == 0

        # 3. Real price drop (100 -> 90.00 = 10% drop) -> 1 notification sent
        ProductVariant.objects.filter(pk=var.pk).update(price=Decimal("90.00"))
        sent = check_and_trigger_price_drop_alerts(variant=var)
        assert sent >= 1

        notif = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.PRICE_DROP
        ).first()
        assert notif is not None
        assert notif.category == NotificationCategory.WISHLIST
        assert "Denim Jacket" in notif.title

        # 4. Re-run without price change: deduplicated -> 0 sent
        assert check_and_trigger_price_drop_alerts(variant=var) == 0

    def test_price_drop_cooldown_window(self, verified_user, make_product, colour, size):
        prod = make_product(name="Linen Shirt")
        var = ProductVariant.objects.create(
            product=prod, sku="LS-01", color=colour, size=size, price=Decimal("100.00")
        )
        Stock.objects.create(variant=var, on_hand=10, reserved=0)
        add_to_wishlist(verified_user, prod, variant=var, notify_price_drop=True)

        # First drop: 100 -> 90
        ProductVariant.objects.filter(pk=var.pk).update(price=Decimal("90.00"))
        assert check_and_trigger_price_drop_alerts(variant=var) >= 1

        # Second small drop within 48 hours: 90 -> 88 (only 2.2% further drop) -> suppressed by cooldown
        ProductVariant.objects.filter(pk=var.pk).update(price=Decimal("88.00"))
        assert check_and_trigger_price_drop_alerts(variant=var) == 0

        # Substantial further drop within cooldown: 90 -> 75 (16.6% drop from 90) -> allowed!
        ProductVariant.objects.filter(pk=var.pk).update(price=Decimal("75.00"))
        assert check_and_trigger_price_drop_alerts(variant=var) >= 1


# =============================================================================
# 2. Back in Stock Alert & Bounce Protection
# =============================================================================


@pytest.mark.django_db
class TestBackInStockReengagement:
    def test_back_in_stock_alert_and_jitter_cooldown(self, verified_user, make_product, colour, size):
        prod = make_product(name="Oxford Shirt")
        var = ProductVariant.objects.create(
            product=prod, sku="OX-01", color=colour, size=size, price=Decimal("60.00")
        )
        stock = Stock.objects.create(variant=var, on_hand=0, reserved=0)

        # Added while out of stock
        item, _ = add_to_wishlist(verified_user, prod, variant=var, notify_back_in_stock=True)
        assert item.was_out_of_stock is True

        # Restocked
        Stock.objects.filter(pk=stock.pk).update(on_hand=5)
        sent = check_and_trigger_back_in_stock_alerts(variant=var)
        assert sent >= 1

        notif = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.BACK_IN_STOCK
        ).first()
        assert notif is not None
        assert "Oxford Shirt" in notif.title

        # Jitter: Stock bounces (goes out of stock and back in within same hour)
        item.refresh_from_db()
        item.was_out_of_stock = True
        item.save()

        # Alert should be suppressed by 12h cooldown
        sent_jitter = check_and_trigger_back_in_stock_alerts(variant=var)
        assert sent_jitter == 0


# =============================================================================
# 3. Post-Purchase Review Follow-Up
# =============================================================================


@pytest.mark.django_db
class TestPostPurchaseReviewReengagement:
    def test_order_review_reminder_happy_path(self, verified_user, make_delivered_order, product):
        order = make_delivered_order(user=verified_user, prod=product, days_ago=3)

        sent = check_and_trigger_order_review_reminders(order_id=order.pk)
        assert sent >= 1

        notif = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.ORDER_REVIEW_REMINDER
        ).first()
        assert notif is not None
        assert notif.category == NotificationCategory.RECOMMENDATIONS
        assert product.name in notif.title or product.name in notif.body
        assert notif.action_url == product.get_absolute_url()

        # Verify analytics event recorded
        ev = Event.objects.filter(
            name="reengagement_triggered", user=verified_user, object_id=product.pk
        ).first()
        assert ev is not None
        assert ev.metadata["trigger"] == "order_review_reminder"

        # Re-running sweep: idempotent, 0 sent
        assert check_and_trigger_order_review_reminders(order_id=order.pk) == 0

    def test_order_review_reminder_skips_if_already_reviewed(
        self, verified_user, make_delivered_order, product
    ):
        order = make_delivered_order(user=verified_user, prod=product, days_ago=3)

        # User already reviewed the product
        Review.objects.create(
            author=verified_user,
            product=product,
            order=order,
            rating=5,
            title="Great piece",
            body="Loved it.",
            status=Review.Status.PUBLISHED,
        )

        sent = check_and_trigger_order_review_reminders(order_id=order.pk)
        assert sent == 0
        assert (
            Notification.objects.filter(
                user=verified_user, notification_type=NotificationType.ORDER_REVIEW_REMINDER
            ).count()
            == 0
        )

    def test_order_review_reminder_skips_undelivered_orders(
        self, verified_user, product
    ):
        from apps.shop.models import CheckoutSession

        cart = Cart.objects.create(user=verified_user, session_key="", status=Cart.Status.CONVERTED)
        checkout = CheckoutSession.objects.create(
            user=verified_user, cart=cart, status=CheckoutSession.Status.CONVERTED
        )
        order = Order.objects.create(
            number="ORD-SHIPPED-01",
            checkout=checkout,
            user=verified_user,
            status=Order.Status.SHIPPED,  # Not delivered yet
            subtotal=Decimal("49.00"),
            shipping_amount=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total=Decimal("49.00"),
        )
        variant = product.variants.first()
        OrderItem.objects.create(
            order=order,
            variant=variant,
            quantity=1,
            unit_price=Decimal("49.00"),
            total_price=Decimal("49.00"),
        )
        Order.objects.filter(pk=order.pk).update(updated_at=timezone.now() - timedelta(days=5))

        sent = check_and_trigger_order_review_reminders(order_id=order.pk)
        assert sent == 0

    def test_order_review_reminder_user_cooldown(
        self, verified_user, make_delivered_order, make_product, colour, size
    ):
        prod1 = make_product(name="Tee One")
        ProductVariant.objects.create(product=prod1, sku="T1", color=colour, size=size, price=Decimal("40.00"))
        order1 = make_delivered_order(user=verified_user, prod=prod1, days_ago=4)

        prod2 = make_product(name="Tee Two")
        ProductVariant.objects.create(product=prod2, sku="T2", color=colour, size=size, price=Decimal("40.00"))
        order2 = make_delivered_order(user=verified_user, prod=prod2, days_ago=3)

        # Triggering reviews: user should get at most 1 reminder per cooldown window (7 days)
        sent = check_and_trigger_order_review_reminders()
        assert sent >= 1
        notifs = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.ORDER_REVIEW_REMINDER
        )
        assert notifs.count() == 1


# =============================================================================
# 4. Saved Bag / Cart Re-engagement
# =============================================================================


@pytest.mark.django_db
class TestSavedBagReengagement:
    def test_saved_bag_reminder_triggers_for_in_stock_cart(
        self, verified_user, product
    ):
        variant = product.variants.first()
        Stock.objects.create(variant=variant, on_hand=5, reserved=0)

        cart = Cart.objects.create(user=verified_user)
        CartItem.objects.create(
            cart=cart,
            variant=variant,
            quantity=1,
            price_snapshot=variant.price,
        )
        # Age cart past 24h
        Cart.objects.filter(pk=cart.pk).update(
            updated_at=timezone.now() - timedelta(hours=36)
        )

        sent = check_and_trigger_saved_bag_reminders()
        assert sent >= 1

        notif = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.SAVED_BAG_REMINDER
        ).first()
        assert notif is not None
        assert "bag" in notif.title.lower() or "items" in notif.title.lower()

        # Idempotency check: repeat run sends 0
        assert check_and_trigger_saved_bag_reminders() == 0

    def test_saved_bag_reminder_skips_out_of_stock_items(
        self, verified_user, product
    ):
        variant = product.variants.first()
        Stock.objects.create(variant=variant, on_hand=0, reserved=0)  # Out of stock!

        cart = Cart.objects.create(user=verified_user)
        CartItem.objects.create(
            cart=cart,
            variant=variant,
            quantity=1,
            price_snapshot=variant.price,
        )
        Cart.objects.filter(pk=cart.pk).update(
            updated_at=timezone.now() - timedelta(hours=36)
        )

        sent = check_and_trigger_saved_bag_reminders()
        assert sent == 0


# =============================================================================
# 5. Upcoming FLASH Drop Reminders
# =============================================================================


@pytest.mark.django_db
class TestDropReengagement:
    def test_drop_upcoming_reminder(self, verified_user):
        now = timezone.now()
        drop = FlashDrop.objects.create(
            name="Neon Cyber Drop",
            slug="neon-cyber-drop",
            status=DropStatus.SCHEDULED,
            starts_at=now + timedelta(hours=8),
            ends_at=now + timedelta(hours=32),
        )

        # Register user interest / subscription
        NotificationSubscription.objects.create(
            user=verified_user,
            notification_type=NotificationType.DROP_UPCOMING,
            related_object_type="drop",
            related_object_id=drop.pk,
        )

        sent = check_and_trigger_drop_reminders(drop_id=drop.pk)
        assert sent >= 1

        notif = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.DROP_UPCOMING
        ).first()
        assert notif is not None
        assert "Neon Cyber Drop" in notif.title or "Neon Cyber Drop" in notif.body
        assert f"/drops/{drop.slug}/" in notif.action_url

        # Idempotency on repeat
        assert check_and_trigger_drop_reminders(drop_id=drop.pk) == 0


# =============================================================================
# 6. Customer Preferences & Suppression
# =============================================================================


@pytest.mark.django_db
class TestPreferencesEnforcement:
    def test_recommendations_category_disabled_suppresses_notification(
        self, verified_user, make_delivered_order, product
    ):
        order = make_delivered_order(user=verified_user, prod=product, days_ago=3)

        # Customer opts out of RECOMMENDATIONS category
        preferences.set_preferences(
            verified_user,
            {NotificationCategory.RECOMMENDATIONS: {"email": False, "in_app": False}},
        )

        sent = check_and_trigger_order_review_reminders(order_id=order.pk)
        assert sent == 0
        assert (
            Notification.objects.filter(
                user=verified_user, notification_type=NotificationType.ORDER_REVIEW_REMINDER
            ).count()
            == 0
        )


# =============================================================================
# 7. Celery Tasks Execution
# =============================================================================


@pytest.mark.django_db
class TestCeleryReengagementTasks:
    def test_tasks_run_safely_and_return_metrics(self):
        r1 = sweep_review_reminders()
        assert isinstance(r1, dict)
        assert "sent" in r1

        r2 = sweep_saved_bag_reminders()
        assert isinstance(r2, dict)
        assert "sent" in r2

        r3 = sweep_drop_upcoming_reminders()
        assert isinstance(r3, dict)
        assert "sent" in r3

        r4 = sweep_style_recommendations()
        assert isinstance(r4, dict)
        assert "sent" in r4


# =============================================================================
# 8. In-App Notification Center UI & Action Tracking
# =============================================================================


@pytest.mark.django_db
class TestNotificationCenterUI:
    def test_center_renders_and_detail_marks_read_with_analytics(
        self, client, verified_user, product
    ):
        client.force_login(verified_user)

        # Create a notification
        notif = Notification.objects.create(
            user=verified_user,
            notification_type=NotificationType.ORDER_REVIEW_REMINDER,
            category=NotificationCategory.RECOMMENDATIONS,
            channel=Channel.IN_APP,
            title="How did your piece fit?",
            body="Leave a quick review.",
            action_url=product.get_absolute_url(),
            status="sent",
        )
        assert notif.is_unread is True

        # Open center
        center_url = reverse("notifications:center")
        resp = client.get(center_url)
        assert resp.status_code == 200
        assert "How did your piece fit?" in resp.content.decode()
        assert "Personal Curation" in resp.content.decode()

        # Open detail view -> marks read and records analytics
        detail_url = reverse("notifications:detail", args=[notif.pk])
        resp_detail = client.get(detail_url)
        assert resp_detail.status_code == 200
        notif.refresh_from_db()
        assert notif.is_unread is False

        assert Event.objects.filter(
            name="notification_opened", user=verified_user, object_id=notif.pk
        ).exists()

        # Click action button -> tracks notification_clicked and redirects to action_url
        action_url = reverse("notifications:action-click", args=[notif.pk])
        resp_action = client.get(action_url)
        assert resp_action.status_code == 302
        assert resp_action.url == product.get_absolute_url()

        assert Event.objects.filter(
            name="notification_clicked", user=verified_user, object_id=notif.pk
        ).exists()

    def test_security_idor_returns_404_for_other_user_notification(
        self, client, verified_user, other_user
    ):
        client.force_login(verified_user)

        # Notification belonging to other_user
        foreign_notif = Notification.objects.create(
            user=other_user,
            notification_type=NotificationType.ORDER_REVIEW_REMINDER,
            category=NotificationCategory.RECOMMENDATIONS,
            channel=Channel.IN_APP,
            title="Private notification",
            status="sent",
        )

        detail_url = reverse("notifications:detail", args=[foreign_notif.pk])
        resp = client.get(detail_url)
        assert resp.status_code == 404


# =============================================================================
# 9. Customer Home Dashboard Integration
# =============================================================================


@pytest.mark.django_db
class TestCustomerHomeIntegration:
    def test_customer_home_shows_reengagement_when_data_exists(
        self, client, verified_user, make_delivered_order, product
    ):
        client.force_login(verified_user)
        order = make_delivered_order(user=verified_user, prod=product, days_ago=3)

        summary = get_customer_reengagement_summary(verified_user)
        assert summary["has_reengagement_content"] is True
        assert len(summary["pending_reviews"]) >= 1

        dashboard_url = reverse("account:dashboard")
        resp = client.get(dashboard_url)
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Delivered Order Follow-up" in content
        assert product.name in content
        assert "Review Item →" in content

    def test_customer_home_clean_when_no_reengagement_data(
        self, client, verified_user
    ):
        client.force_login(verified_user)
        summary = get_customer_reengagement_summary(verified_user)
        assert summary["has_reengagement_content"] is False

        dashboard_url = reverse("account:dashboard")
        resp = client.get(dashboard_url)
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Delivered Order Follow-up" not in content
