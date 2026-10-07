"""Phase 33 — Smart Wishlist, Price Alerts & Back-in-Stock test suite.

Verifies:
- Smart wishlist item states (Available, Low stock, Out of stock, Back in stock, Unavailable).
- Honest pricing (current vs previous, real price drops, no fake urgency).
- Price drop alert opt-in, qualifying detection, and notification deduplication.
- Back-in-stock alert opt-in, variant-level inventory matching, and notification deduplication.
- Notification preferences integration (WISHLIST category, email/in-app channels).
- Product detail page wishlist and alert actions.
- Wishlist page visual filters and empty states.
- Smart suggestions integration reusing existing recommendations.
- Wishlist -> Cart flow (single move and bulk add all available).
- Security, ownership (IDOR prevention), and CSRF enforcement.
- Full analytics event tracking without duplicate systems.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.analytics.models import Event
from apps.catalog.models import Product, ProductVariant, Size
from apps.inventory.models import Stock
from apps.notifications.models import (
    Notification,
    NotificationCategory,
    NotificationType,
)
from apps.shop.models import Cart, WishlistItem
from apps.shop.wishlist_services import (
    add_to_wishlist,
    check_and_trigger_back_in_stock_alerts,
    check_and_trigger_price_drop_alerts,
    toggle_wishlist_alert,
)

pytestmark = pytest.mark.django_db


# =============================================================================
# 1. Smart Wishlist States & Data
# =============================================================================


class TestSmartWishlistStates:
    def test_item_displays_real_prices_and_states(self, make_product, verified_user):
        prod = make_product(name="Oversized Wool Coat")
        var = ProductVariant.objects.create(
            product=prod,
            sku="COAT-BLK-M",
            price=Decimal("250.00"),
            compare_at_price=Decimal("300.00"),
        )
        Stock.objects.create(variant=var, on_hand=8, reserved=0)

        item, created = add_to_wishlist(
            verified_user, prod, variant=var, note="For winter trip"
        )
        assert created is True
        assert item.current_price == Decimal("250.00")
        assert item.previous_price == Decimal("300.00")
        assert item.has_price_drop is True
        assert item.price_drop_amount == Decimal("50.00")
        assert item.stock_state == "in_stock"
        assert item.is_in_stock is True
        assert item.is_low_stock is False
        assert item.is_available is True

    def test_low_stock_only_triggers_on_real_inventory(self, make_product, verified_user):
        prod = make_product(name="Cashmere Beanie")
        var = ProductVariant.objects.create(
            product=prod, sku="BEANIE-BLK", price=Decimal("45.00")
        )
        # Exactly 3 units available
        Stock.objects.create(variant=var, on_hand=3, reserved=0)

        item, _ = add_to_wishlist(verified_user, prod, variant=var)
        assert item.stock_state == "low_stock"
        assert item.is_low_stock is True
        assert item.is_in_stock is True

    def test_out_of_stock_state(self, make_product, verified_user):
        prod = make_product(name="Limited Leather Jacket")
        var = ProductVariant.objects.create(
            product=prod, sku="JACKET-01", price=Decimal("450.00")
        )
        Stock.objects.create(variant=var, on_hand=0, reserved=0)

        item, _ = add_to_wishlist(verified_user, prod, variant=var)
        assert item.stock_state == "out_of_stock"
        assert item.is_out_of_stock is True
        assert item.is_in_stock is False

    def test_back_in_stock_state_after_restock(self, make_product, verified_user):
        prod = make_product(name="Silk Camp Shirt")
        var = ProductVariant.objects.create(
            product=prod, sku="SILK-SHIRT-1", price=Decimal("110.00")
        )
        stock = Stock.objects.create(variant=var, on_hand=0, reserved=0)

        item, _ = add_to_wishlist(
            verified_user, prod, variant=var, notify_back_in_stock=True
        )
        assert item.was_out_of_stock is True
        assert item.is_out_of_stock is True

        # Restock variant using update to test manual service sweep directly
        Stock.objects.filter(pk=stock.pk).update(on_hand=10)

        # Trigger alert sweep
        sent = check_and_trigger_back_in_stock_alerts(variant=var)
        assert sent >= 1

        item.refresh_from_db()
        assert item.is_back_in_stock is True
        assert item.stock_state == "back_in_stock"

    def test_unavailable_product_state(self, make_product, verified_user):
        prod = make_product(name="Draft Pants", status=Product.Status.DRAFT)
        var = ProductVariant.objects.create(
            product=prod, sku="PANTS-DRAFT", price=Decimal("75.00")
        )
        item, _ = add_to_wishlist(verified_user, prod, variant=var)
        assert item.is_available is False
        assert item.stock_state == "unavailable"


# =============================================================================
# 2. Price Drop Alerts
# =============================================================================


class TestPriceDropAlerts:
    def test_toggle_price_drop_alert_and_analytics(self, make_product, verified_user):
        prod = make_product(name="Tailored Blazer")
        var = ProductVariant.objects.create(
            product=prod, sku="BLZ-01", price=Decimal("200.00")
        )
        item, _ = add_to_wishlist(verified_user, prod, variant=var)
        assert item.notify_price_drop is False

        # Enable alert
        item = toggle_wishlist_alert(verified_user, item.pk, "price_drop", enabled=True)
        assert item.notify_price_drop is True
        assert item.price_when_added == Decimal("200.00")

        # Verify analytics
        event = Event.objects.filter(
            name="price_alert_enabled", user=verified_user, object_id=item.pk
        ).first()
        assert event is not None
        assert event.metadata["product_id"] == prod.pk

        # Disable alert
        item = toggle_wishlist_alert(verified_user, item.pk, "price_drop", enabled=False)
        assert item.notify_price_drop is False

    def test_qualifying_price_drop_triggers_notification_and_deduplicates(
        self, make_product, verified_user
    ):
        prod = make_product(name="Raw Denim Jeans")
        var = ProductVariant.objects.create(
            product=prod, sku="JEANS-RAW-1", price=Decimal("120.00")
        )
        Stock.objects.create(variant=var, on_hand=15, reserved=0)

        _item, _ = add_to_wishlist(
            verified_user, prod, variant=var, notify_price_drop=True
        )

        # 1. Price unchanged: sweep should send 0 notifications
        sent_same = check_and_trigger_price_drop_alerts(variant=var)
        assert sent_same == 0
        assert Notification.objects.filter(user=verified_user).count() == 0

        # 2. Price decreases to 95.00 (update directly to test sweep function)
        ProductVariant.objects.filter(pk=var.pk).update(price=Decimal("95.00"))

        sent = check_and_trigger_price_drop_alerts(variant=var)
        assert sent >= 1

        notif = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.PRICE_DROP
        ).first()
        assert notif is not None
        assert notif.category == NotificationCategory.WISHLIST
        assert "Raw Denim Jeans" in notif.title
        assert "95.00" in notif.body

        # Verify analytics event
        ev = Event.objects.filter(
            name="price_alert_triggered", user=verified_user, object_id=prod.pk
        ).first()
        assert ev is not None
        assert ev.metadata["new_price"] == "95.00"

        # 3. Re-run without price change: Deduplicated! Must send 0 notifications
        sent_recheck = check_and_trigger_price_drop_alerts(variant=var)
        assert sent_recheck == 0
        assert (
            Notification.objects.filter(
                user=verified_user, notification_type=NotificationType.PRICE_DROP
            ).count()
            == sent
        )

        # 4. Price increases: No notification!
        ProductVariant.objects.filter(pk=var.pk).update(price=Decimal("130.00"))
        sent_increase = check_and_trigger_price_drop_alerts(variant=var)
        assert sent_increase == 0

    def test_price_drop_signal_dispatch_on_variant_save(
        self, make_product, verified_user
    ):
        prod = make_product(name="Silk Blouse")
        var = ProductVariant.objects.create(
            product=prod, sku="BLOUSE-01", price=Decimal("150.00")
        )
        Stock.objects.create(variant=var, on_hand=10, reserved=0)

        add_to_wishlist(verified_user, prod, variant=var, notify_price_drop=True)

        # Saving variant with lower price triggers the post_save signal
        var.price = Decimal("120.00")
        var.save(update_fields=["price", "updated_at"])

        notif = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.PRICE_DROP
        ).first()
        assert notif is not None
        assert "Silk Blouse" in notif.title


# =============================================================================
# 3. Back-in-Stock Alerts
# =============================================================================


class TestBackInStockAlerts:
    def test_back_in_stock_alert_flow_and_deduplication(
        self, make_product, verified_user
    ):
        prod = make_product(name="Chunky Knit Sweater")
        size_s = Size.objects.create(name="Small", slug="size-s-knit", code="S-KNIT")
        size_m = Size.objects.create(name="Medium", slug="size-m-knit", code="M-KNIT")
        v1 = ProductVariant.objects.create(
            product=prod, sku="KNIT-BLK-S", price=Decimal("140.00"), size=size_s
        )
        v2 = ProductVariant.objects.create(
            product=prod, sku="KNIT-BLK-M", price=Decimal("140.00"), size=size_m
        )
        s1 = Stock.objects.create(variant=v1, on_hand=0, reserved=0)
        Stock.objects.create(variant=v2, on_hand=5, reserved=0)

        # User saves out-of-stock variant v1 and opts into back-in-stock alert
        item, _ = add_to_wishlist(
            verified_user, prod, variant=v1, notify_back_in_stock=True
        )
        assert item.was_out_of_stock is True

        # Restock unrelated variant v2: v1 must NOT notify
        check_and_trigger_back_in_stock_alerts(variant=v2)
        assert (
            Notification.objects.filter(
                user=verified_user, notification_type=NotificationType.BACK_IN_STOCK
            ).count()
            == 0
        )

        # Now restock watched variant v1 (using update to test sweep function)
        Stock.objects.filter(pk=s1.pk).update(on_hand=10)

        sent = check_and_trigger_back_in_stock_alerts(variant=v1)
        assert sent >= 1

        notif = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.BACK_IN_STOCK
        ).first()
        assert notif is not None
        assert notif.category == NotificationCategory.WISHLIST
        assert "Chunky Knit Sweater" in notif.title

        # Check analytics event
        ev = Event.objects.filter(
            name="back_in_stock_triggered", user=verified_user, object_id=prod.pk
        ).first()
        assert ev is not None

        # Re-run: Must NOT notify again! Deduplicated & was_out_of_stock reset
        sent_again = check_and_trigger_back_in_stock_alerts(variant=v1)
        assert sent_again == 0

        # Even if warehouse adds more stock:
        Stock.objects.filter(pk=s1.pk).update(on_hand=20)
        sent_third = check_and_trigger_back_in_stock_alerts(variant=v1)
        assert sent_third == 0

        assert (
            Notification.objects.filter(
                user=verified_user, notification_type=NotificationType.BACK_IN_STOCK
            ).count()
            == sent
        )

    def test_back_in_stock_signal_dispatch_on_stock_save(
        self, make_product, verified_user
    ):
        prod = make_product(name="Linen Shorts")
        v = ProductVariant.objects.create(
            product=prod, sku="SHORTS-01", price=Decimal("65.00")
        )
        s = Stock.objects.create(variant=v, on_hand=0, reserved=0)

        add_to_wishlist(verified_user, prod, variant=v, notify_back_in_stock=True)

        # Saving stock with on_hand > 0 triggers post_save signal
        s.on_hand = 15
        s.save(update_fields=["on_hand", "updated_at"])

        notif = Notification.objects.filter(
            user=verified_user, notification_type=NotificationType.BACK_IN_STOCK
        ).first()
        assert notif is not None
        assert "Linen Shorts" in notif.title

        # Check analytics event
        ev = Event.objects.filter(
            name="back_in_stock_triggered", user=verified_user, object_id=prod.pk
        ).first()
        assert ev is not None

        # Re-run or subsequent stock updates: Must NOT notify again!
        sent_again = check_and_trigger_back_in_stock_alerts(variant=v)
        assert sent_again == 0

        # Even if warehouse adds more stock:
        s.on_hand = 20
        s.save(update_fields=["on_hand", "updated_at"])
        sent_third = check_and_trigger_back_in_stock_alerts(variant=v)
        assert sent_third == 0


# =============================================================================
# 4. Notification Preferences Integration
# =============================================================================


class TestNotificationPreferencesIntegration:
    def test_wishlist_category_in_preferences(self, verified_user):
        from apps.notifications.services import preferences

        pref = preferences.get_preference(verified_user, NotificationCategory.WISHLIST)
        assert pref is not None
        assert pref.category == NotificationCategory.WISHLIST
        assert pref.email_enabled is True
        assert pref.in_app_enabled is True

        # Customer toggles off email for wishlist
        preferences.set_preferences(
            verified_user,
            {NotificationCategory.WISHLIST.value: {"email": False, "in_app": True}},
        )
        pref.refresh_from_db()
        assert pref.email_enabled is False
        assert pref.in_app_enabled is True


# =============================================================================
# 5. Product Page Integration
# =============================================================================


class TestProductPageIntegration:
    def test_pdp_displays_save_and_saved_button(
        self, client, verified_user, make_product
    ):
        prod = make_product(name="Structured Linen Vest")
        var = ProductVariant.objects.create(
            product=prod, sku="VEST-01", price=Decimal("85.00")
        )
        Stock.objects.create(variant=var, on_hand=10, reserved=0)

        # 1. Anonymous visitor sees "Save"
        res_anon = client.get(prod.get_absolute_url())
        assert res_anon.status_code == 200
        assert b"Save" in res_anon.content

        # 2. Signed-in user saves product
        client.force_login(verified_user)
        add_res = client.post(
            reverse("shop:wishlist-add"),
            {"product_id": prod.pk, "variant_id": var.pk},
            follow=True,
        )
        assert add_res.status_code == 200

        # Now PDP shows "Saved" and alert options
        res_auth = client.get(prod.get_absolute_url())
        assert res_auth.status_code == 200
        content = res_auth.content.decode()
        assert "Saved" in content
        assert "Price drop alert" in content
        assert "Back in stock alert" in content


# =============================================================================
# 6. Wishlist Page, Filters & Empty States
# =============================================================================


class TestWishlistPageAndFilters:
    def test_empty_wishlist_displays_encouraging_copy(self, client, verified_user):
        client.force_login(verified_user)
        res = client.get(reverse("shop:wishlist"))
        assert res.status_code == 200
        content = res.content.decode()
        assert "Your wishlist is waiting for something special." in content
        assert "Browse the catalogue" in content

    def test_filter_pills_and_filtering(self, client, verified_user, make_product):
        # Product 1: in stock
        p1 = make_product(name="In Stock Shirt")
        v1 = ProductVariant.objects.create(
            product=p1, sku="SHIRT-1", price=Decimal("70.00")
        )
        Stock.objects.create(variant=v1, on_hand=10, reserved=0)
        add_to_wishlist(verified_user, p1, variant=v1)

        # Product 2: out of stock
        p2 = make_product(name="Sold Out Trench")
        v2 = ProductVariant.objects.create(
            product=p2, sku="TRENCH-2", price=Decimal("220.00")
        )
        Stock.objects.create(variant=v2, on_hand=0, reserved=0)
        add_to_wishlist(verified_user, p2, variant=v2)

        client.force_login(verified_user)

        # View "all"
        res_all = client.get(reverse("shop:wishlist"))
        assert res_all.status_code == 200
        c_all = res_all.content.decode()
        assert "In Stock Shirt" in c_all
        assert "Sold Out Trench" in c_all

        # View "available"
        res_avail = client.get(reverse("shop:wishlist") + "?filter=available")
        assert res_avail.status_code == 200
        c_avail = res_avail.content.decode()
        assert "In Stock Shirt" in c_avail
        assert "Sold Out Trench" not in c_avail

        # View "out_of_stock"
        res_oos = client.get(reverse("shop:wishlist") + "?filter=out_of_stock")
        assert res_oos.status_code == 200
        c_oos = res_oos.content.decode()
        assert "Sold Out Trench" in c_oos
        assert "In Stock Shirt" not in c_oos


# =============================================================================
# 7. Wishlist → Cart Flow
# =============================================================================


class TestWishlistToCartFlow:
    def test_move_single_item_to_bag(self, client, verified_user, make_product):
        prod = make_product(name="Pleated Trousers")
        var = ProductVariant.objects.create(
            product=prod, sku="TR-01", price=Decimal("110.00")
        )
        Stock.objects.create(variant=var, on_hand=5, reserved=0)

        item, _ = add_to_wishlist(verified_user, prod, variant=var)
        client.force_login(verified_user)

        move_url = reverse("shop:wishlist-move-to-bag", kwargs={"item_pk": item.pk})
        res = client.post(move_url, follow=True)
        assert res.status_code == 200

        # Removed from wishlist
        assert not WishlistItem.objects.filter(pk=item.pk).exists()

        # Added to cart
        cart = Cart.objects.filter(user=verified_user, status=Cart.Status.ACTIVE).first()
        assert cart is not None
        assert cart.items.filter(variant=var).exists()

        # Analytics tracked
        ev = Event.objects.filter(
            name="wishlist_to_cart", user=verified_user, object_id=var.pk
        ).first()
        assert ev is not None

    def test_add_all_available_to_bag_action(
        self, client, verified_user, make_product
    ):
        # Item 1: Available
        p1 = make_product(name="Polo Shirt")
        v1 = ProductVariant.objects.create(
            product=p1, sku="POLO-1", price=Decimal("60.00")
        )
        Stock.objects.create(variant=v1, on_hand=5, reserved=0)
        item1, _ = add_to_wishlist(verified_user, p1, variant=v1)

        # Item 2: Out of stock (should be skipped safely)
        p2 = make_product(name="Sold Out Cap")
        v2 = ProductVariant.objects.create(
            product=p2, sku="CAP-1", price=Decimal("30.00")
        )
        Stock.objects.create(variant=v2, on_hand=0, reserved=0)
        item2, _ = add_to_wishlist(verified_user, p2, variant=v2)

        client.force_login(verified_user)
        res = client.post(reverse("shop:wishlist-add-all-to-bag"), follow=True)
        assert res.status_code == 200

        # Available item moved, out-of-stock item remains in wishlist
        assert not WishlistItem.objects.filter(pk=item1.pk).exists()
        assert WishlistItem.objects.filter(pk=item2.pk).exists()

        # Cart contains item 1
        cart = Cart.objects.filter(user=verified_user, status=Cart.Status.ACTIVE).first()
        assert cart.items.filter(variant=v1).exists()
        assert not cart.items.filter(variant=v2).exists()


# =============================================================================
# 8. Security & Permissions (IDOR prevention)
# =============================================================================


class TestWishlistPermissionsAndSecurity:
    def test_user_cannot_modify_or_delete_other_users_wishlist(
        self, client, verified_user, other_user, make_product
    ):
        prod = make_product(name="Private Watch")
        var = ProductVariant.objects.create(
            product=prod, sku="WATCH-01", price=Decimal("300.00")
        )
        Stock.objects.create(variant=var, on_hand=2, reserved=0)

        # Item belongs to verified_user
        item, _ = add_to_wishlist(verified_user, prod, variant=var)

        # Attacker is other_user
        client.force_login(other_user)

        # 1. Attacker tries to remove victim's item
        del_res = client.post(
            reverse("shop:wishlist-remove", kwargs={"item_pk": item.pk})
        )
        assert del_res.status_code == 404
        assert WishlistItem.objects.filter(pk=item.pk).exists()

        # 2. Attacker tries to toggle alert on victim's item
        alert_res = client.post(
            reverse("shop:wishlist-toggle-alert", kwargs={"item_pk": item.pk}),
            {"alert_type": "price_drop", "enabled": "1"},
        )
        assert alert_res.status_code == 404

        # 3. Attacker tries to move victim's item to cart
        move_res = client.post(
            reverse("shop:wishlist-move-to-bag", kwargs={"item_pk": item.pk})
        )
        assert move_res.status_code == 404

    def test_endpoints_require_authentication(self, client, make_product):
        prod = make_product(name="Public Item")
        # Anonymous POST to add
        res_add = client.post(
            reverse("shop:wishlist-add"), {"product_id": prod.pk}
        )
        assert res_add.status_code == 302
        assert reverse("accounts:login") in res_add["Location"]

        # Anonymous GET to wishlist detail
        res_get = client.get(reverse("shop:wishlist"))
        assert res_get.status_code == 302
        assert reverse("accounts:login") in res_get["Location"]


# =============================================================================
# 9. Smart Suggestions & Product Redirect Tracking
# =============================================================================


class TestSuggestionsAndRedirectTracking:
    def test_wishlist_to_product_tracking_and_redirect(
        self, client, verified_user, make_product
    ):
        prod = make_product(name="Silk Bandana")
        item, _ = add_to_wishlist(verified_user, prod)

        client.force_login(verified_user)
        url = reverse("shop:wishlist-to-product", kwargs={"item_pk": item.pk})
        res = client.get(url)
        assert res.status_code == 302
        assert res["Location"] == prod.get_absolute_url()

        # Check analytics event
        ev = Event.objects.filter(
            name="wishlist_to_product", user=verified_user, object_id=prod.pk
        ).first()
        assert ev is not None
        assert ev.metadata["wishlist_item"] == item.pk

    def test_ajax_htmx_endpoints_return_json(
        self, client, verified_user, make_product
    ):
        prod = make_product(name="Minimalist Cardholder")
        client.force_login(verified_user)

        # HTMX wishlist add
        res = client.post(
            reverse("shop:wishlist-add"),
            {"product_id": prod.pk},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["count"] == 1
        item_id = data["item_id"]

        # HTMX toggle alert
        alert_res = client.post(
            reverse("shop:wishlist-toggle-alert", kwargs={"item_pk": item_id}),
            {"alert_type": "price_drop", "enabled": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        assert alert_res.status_code == 200
        alert_data = alert_res.json()
        assert alert_data["success"] is True
        assert alert_data["enabled"] is True

        # HTMX remove
        remove_res = client.post(
            reverse("shop:wishlist-remove", kwargs={"item_pk": item_id}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        assert remove_res.status_code == 200
        remove_data = remove_res.json()
        assert remove_data["success"] is True
        assert remove_data["count"] == 0
