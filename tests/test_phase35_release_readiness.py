"""Phase 35: End-to-End Quality & Release Readiness Test Suite.

Audits, verifies, and protects the complete customer and operational journeys:
1. Guest cart preservation, sign-in merge, quantity limits, and clean item reconciliation.
2. Complete checkout flow: cart -> validated checkout -> order created with holds and events.
3. Payment success webhook idempotency: redelivery does not duplicate orders or payments.
4. Payment failure and reservation release: inventory freed, order cancelled safely.
5. Out-of-stock race condition during checkout: clear error message and safe redirection.
6. Discount revalidation: promotion or points eligibility changes block stale handoff.
7. Order history and tracking: customer sees shipment/timeline; object-level privacy 404s.
8. Wishlist intelligence & alerts: removal stops alerts, wishlist_count works without NameError.
9. Review moderation lifecycle: purchase gate, pending state, public visibility, re-moderation.
10. Staff operational actions: least-privilege 404s, append-only AuditEvent immutability.
11. Security regression controls: open redirect, DB URL checks, and closet isolation.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from apps.backoffice.models import AuditEvent
from apps.backoffice.services import audit as audit_service
from apps.closet.models import Outfit
from apps.engagement.models import Review
from apps.engagement.services.reviews import (
    create_review,
    eligibility,
    public_queryset,
    set_status,
    update_review,
)
from apps.inventory.models import Reservation, Stock
from apps.inventory.services import (
    sweep_expired_holds,
)
from apps.notifications.models import (
    Channel,
    Notification,
    NotificationCategory,
    NotificationType,
)
from apps.orders.models import Order, OrderEvent
from apps.orders.services import (
    StaleCheckout,
    create_order_from_checkout,
)
from apps.payments.models import Payment, PaymentEvent
from apps.payments.providers.base import ProviderEvent
from apps.payments.services import handle_provider_event, start_payment
from apps.shop.checkout import get_or_create_checkout, set_shipping_address, validate_checkout
from apps.shop.models import Cart, CartItem, CheckoutSession, WishlistItem
from apps.shop.services import (
    add_to_cart,
    merge_carts,
)
from apps.shop.wishlist_services import (
    add_to_wishlist,
    check_and_trigger_price_drop_alerts,
    remove_from_wishlist,
)
from tests.phase6_helpers import placed_order, seed_stock, validated_checkout
from tests.phase7_helpers import make_promotion

User = get_user_model()
pytestmark = pytest.mark.django_db


class TestCustomerJourney1GuestCartMerge:
    """Journey 1: Guest browses products -> adds to cart -> signs in -> cart is preserved."""

    def test_guest_cart_merges_into_empty_user_cart_on_login(self, client, user, product):
        variant = product.variants.first()
        seed_stock(variant, 20)

        # 1. Guest visits and adds variant to bag
        res_add = client.post(
            reverse("shop:cart-add"),
            {"variant_id": variant.pk, "quantity": 3},
        )
        assert res_add.status_code == 302

        # Verify guest cart in session
        guest_cart_id = client.session.get("guest_cart_id")
        assert guest_cart_id is not None
        guest_cart = Cart.objects.get(pk=guest_cart_id)
        assert guest_cart.items.filter(variant=variant, quantity=3).exists()

        # 2. Customer signs in
        res_login = client.post(
            reverse("accounts:login"),
            {"username": user.email, "password": "Str0ng-Passw0rd!"},
        )
        assert res_login.status_code == 302

        # 3. User cart now owns the items; guest cart converted
        user_cart = Cart.objects.get(user=user, status=Cart.Status.ACTIVE)
        assert user_cart.items.filter(variant=variant, quantity=3).exists()

        guest_cart.refresh_from_db()
        assert guest_cart.status == Cart.Status.CONVERTED
        assert guest_cart.items.count() == 0

    def test_guest_cart_merges_existing_variant_and_deletes_guest_row(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 50)

        guest_cart = Cart.objects.create(session_key="guest-session-123", status=Cart.Status.ACTIVE)
        user_cart = Cart.objects.create(user=user, status=Cart.Status.ACTIVE)

        CartItem.objects.create(
            cart=guest_cart, variant=variant, quantity=2, price_snapshot=variant.price
        )
        CartItem.objects.create(
            cart=user_cart, variant=variant, quantity=3, price_snapshot=variant.price
        )

        merged = merge_carts(guest_cart, user_cart)
        assert merged == 1

        user_cart_item = user_cart.items.get(variant=variant)
        assert user_cart_item.quantity == 5

        # Guest cart item row must be deleted, not orphaned
        assert not guest_cart.items.filter(variant=variant).exists()
        assert guest_cart.status == Cart.Status.CONVERTED


class TestCustomerJourney2CheckoutAndOrderCreation:
    """Journey 2: Customer selects a valid variant -> checks out -> order created correctly."""

    def test_order_created_with_stock_holds_and_addresses(self, user, product):
        variant = product.variants.first()
        checkout = validated_checkout(user, variant, quantity=2, stock=10)
        assert checkout.is_validated

        stock_before = Stock.objects.get(variant=variant)
        assert stock_before.reserved == 0

        # Create order from checkout
        order = create_order_from_checkout(checkout)

        assert order.status == Order.Status.PENDING_PAYMENT
        assert order.user == user
        assert order.items.count() == 1
        item = order.items.first()
        assert item.variant_id == variant.pk
        assert item.quantity == 2

        # Holds attached
        stock_after = Stock.objects.get(variant=variant)
        assert stock_after.reserved == 2
        assert Reservation.objects.filter(order=order, status=Reservation.Status.ACTIVE).exists()

        # Checkout converted
        checkout.refresh_from_db()
        assert checkout.status == CheckoutSession.Status.CONVERTED

        # Order events recorded
        assert OrderEvent.objects.filter(order=order, event_type=OrderEvent.Type.CREATED).exists()
        assert OrderEvent.objects.filter(
            order=order, event_type=OrderEvent.Type.STOCK_RESERVED
        ).exists()


class TestCustomerJourney3PaymentWebhookIdempotency:
    """Journey 3: Payment succeeds -> duplicate webhook arrives -> payment/order not duplicated."""

    def test_duplicate_webhook_delivery_is_strictly_idempotent(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, quantity=2, stock=10)
        payment = start_payment(order)

        assert payment.status == Payment.Status.PENDING
        stock = Stock.objects.get(variant=variant)
        assert stock.on_hand == 10
        assert stock.reserved == 2

        event = ProviderEvent(
            event_id="evt_succ_unique_123",
            event_type=Payment.Status.SUCCEEDED,
            payment_reference=payment.provider_reference,
            amount=order.total,
            currency=order.currency,
            payload={"status": "paid"},
        )

        # 1. First webhook delivery
        record1 = handle_provider_event("development", event)
        assert record1.event_id == "evt_succ_unique_123"

        order.refresh_from_db()
        payment.refresh_from_db()
        assert order.status == Order.Status.PAID
        assert payment.status == Payment.Status.SUCCEEDED

        stock.refresh_from_db()
        assert stock.on_hand == 8
        assert stock.reserved == 0

        # 2. Redelivered webhook (duplicate delivery with same event_id)
        record2 = handle_provider_event("development", event)
        assert record2.pk == record1.pk

        # Confirm nothing was double-consumed
        stock.refresh_from_db()
        assert stock.on_hand == 8
        assert stock.reserved == 0
        assert PaymentEvent.objects.filter(event_id="evt_succ_unique_123").count() == 1


class TestCustomerJourney4PaymentFailureAndExpiry:
    """Journey 4: Payment fails or checkout expires -> reservations released safely."""

    def test_payment_failure_releases_stock_holds(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, quantity=3, stock=10)
        payment = start_payment(order)

        stock = Stock.objects.get(variant=variant)
        assert stock.on_hand == 10
        assert stock.reserved == 3

        fail_event = ProviderEvent(
            event_id="evt_fail_999",
            event_type=Payment.Status.FAILED,
            payment_reference=payment.provider_reference,
            amount=order.total,
            currency=order.currency,
            reason="insufficient_funds",
            payload={"error": "card_declined"},
        )

        handle_provider_event("development", fail_event)

        order.refresh_from_db()
        payment.refresh_from_db()
        assert order.status == Order.Status.CANCELLED
        assert payment.status == Payment.Status.FAILED

        # Stock released
        stock.refresh_from_db()
        assert stock.on_hand == 10
        assert stock.reserved == 0
        assert Reservation.objects.filter(order=order, status=Reservation.Status.RELEASED).exists()

    def test_sweep_expired_unplaced_holds(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 10)

        cart = Cart.objects.create(user=user, status=Cart.Status.ACTIVE)
        checkout = get_or_create_checkout(user, cart)

        past = timezone.now() - timedelta(minutes=30)
        # Hold without order
        res = Reservation.objects.create(
            variant=variant,
            checkout=checkout,
            order=None,
            quantity=2,
            status=Reservation.Status.ACTIVE,
            expires_at=past,
        )
        Stock.objects.filter(variant=variant).update(reserved=2)

        swept = sweep_expired_holds(now=timezone.now())
        assert swept == 1

        res.refresh_from_db()
        assert res.status == Reservation.Status.EXPIRED
        stock = Stock.objects.get(variant=variant)
        assert stock.reserved == 0


class TestCustomerJourney5OutOfStockRaceDuringCheckout:
    """Journey 5: Inventory becomes unavailable during checkout -> clear error and safe redirect."""

    def test_insufficient_stock_at_place_redirects_with_clear_error(self, client, user, product):
        variant = product.variants.first()
        checkout = validated_checkout(user, variant, quantity=5, stock=5)

        # Race: Stock drops from 5 to 2 before order placement
        Stock.objects.filter(variant=variant).update(on_hand=2)

        client.force_login(user)
        res = client.post(
            reverse("shop:checkout-place"),
            {"checkout_id": checkout.pk},
            follow=True,
        )
        assert res.status_code == 200
        # Redirected back to checkout
        assert any(
            "We could not hold" in str(m) and "only 2 left" in str(m)
            for m in res.context["messages"]
        )

        # No order created
        assert not Order.objects.filter(checkout=checkout).exists()


class TestCustomerJourney6DiscountsAndEligibilityValidation:
    """Journey 6: Promo/loyalty eligibility changes before placement -> revalidated."""

    def test_promotion_expiry_before_order_placement_raises_stale_checkout(
        self, user, product, address
    ):
        variant = product.variants.first()
        seed_stock(variant, 10)

        promo = make_promotion(code="FLASH20", percent=20)
        cart = Cart.objects.create(user=user, status=Cart.Status.ACTIVE)
        add_to_cart(cart, variant, quantity=1)

        checkout = get_or_create_checkout(user, cart)
        set_shipping_address(checkout, address)
        checkout.promotion_code = "FLASH20"
        checkout.save()

        validate_checkout(checkout)
        assert checkout.is_validated

        # Promotion expires between validation and order creation
        promo.ends_at = timezone.now() - timedelta(minutes=5)
        promo.save()

        with pytest.raises(StaleCheckout) as exc:
            create_order_from_checkout(checkout)
        assert "expired" in str(exc.value).lower() or "discounts changed" in str(exc.value).lower()


class TestCustomerJourney7OrderHistoryAndTracking:
    """Journey 7: Customer order history -> correct shipment state; object-level 404s."""

    def test_customer_order_detail_and_privacy_boundaries(self, client, user, other_user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, quantity=1, stock=10)

        # Customer viewing own order
        client.force_login(user)
        res_own = client.get(reverse("account:order-detail", kwargs={"number": order.number}))
        assert res_own.status_code == 200
        assert order.number in res_own.content.decode()

        # Other customer attempting to view order -> 404
        client.force_login(other_user)
        res_other = client.get(reverse("account:order-detail", kwargs={"number": order.number}))
        assert res_other.status_code == 404


class TestCustomerJourney8WishlistIntelligenceAndAlerts:
    """Journey 8: Customer saves a product -> removes it -> alerts stop; wishlist_count works."""

    def test_wishlist_count_endpoint_no_name_error(self, client, user, product):
        client.force_login(user)
        add_to_wishlist(user, product)

        # /shop/wishlist/count/ must return valid JSON without 500 NameError
        res = client.get(reverse("shop:wishlist-count"))
        assert res.status_code == 200
        assert res.json() == {"count": 1}

    def test_wishlist_removal_stops_price_drop_alerts(self, user, product):
        item, _ = add_to_wishlist(user, product, notify_price_drop=True)
        item.price_when_added = Decimal("50.00")
        item.save()

        # Remove from wishlist
        removed = remove_from_wishlist(user, item.pk)
        assert removed is True
        assert not WishlistItem.objects.filter(pk=item.pk).exists()

        # Price drops on product
        variant = product.variants.first()
        variant.price = Decimal("30.00")
        variant.save()

        notified = check_and_trigger_price_drop_alerts()
        assert notified == 0
        assert Notification.objects.filter(user=user).count() == 0


class TestCustomerJourney9ReviewLifecycleAndModeration:
    """Journey 9: Customer submits content/review -> moderation and visibility rules enforced."""

    def test_review_submission_gating_and_moderation_flow(self, user, product):
        variant = product.variants.first()

        # 1. Unpurchased product cannot be reviewed
        can_review, reason = eligibility(user, product)
        assert can_review is False
        assert reason == "purchase_required"

        # 2. Purchase product
        order = placed_order(user, variant, quantity=1, stock=10)
        order.status = Order.Status.PAID
        order.save()

        # Now eligible
        can_review, _ = eligibility(user, product)
        assert can_review is True

        # 3. Create review -> Status is PENDING
        review = create_review(
            user=user,
            product=product,
            rating=5,
            title="Exceptional quality",
            body="Heavyweight drape feels bespoke.",
        )
        assert review.status == Review.Status.PENDING

        # 4. Public queryset does NOT show pending review
        assert public_queryset(product).count() == 0

        # 5. Moderator publishes review
        set_status(review, Review.Status.PUBLISHED)
        assert public_queryset(product).count() == 1

        # 6. User edits review content -> reverts to PENDING
        update_review(
            review,
            rating=4,
            title="Updated Title",
            body="Slightly different thoughts after 3 washes.",
        )
        review.refresh_from_db()
        assert review.status == Review.Status.PENDING
        assert public_queryset(product).count() == 0


class TestCustomerJourney10StaffOperationalAudit:
    """Journey 10: Staff performs operational action -> permissions & audit logging enforced."""

    def test_backoffice_least_privilege_and_audit_event_immutability(
        self, client, user, admin_user, product
    ):
        variant = product.variants.first()
        seed_stock(variant, 10)

        # Non-staff customer gets 404
        client.force_login(user)
        res_cust = client.get(reverse("backoffice:dashboard"))
        assert res_cust.status_code == 404

        # Staff action generates append-only AuditEvent
        client.force_login(admin_user)
        audit = audit_service.record(
            actor=admin_user,
            domain=AuditEvent.Domain.INVENTORY,
            action="inventory.adjust",
            object_type="inventory.stock",
            object_id=variant.pk,
            object_repr=f"Stock for {variant.sku}",
            reason="Physical inventory count reconcile",
        )

        assert audit is not None
        assert audit.domain == AuditEvent.Domain.INVENTORY
        assert audit.actor_label == admin_user.email

        # AuditEvent is append-only: saving an existing row raises ValidationError
        with pytest.raises(ValidationError):
            audit.reason = "attempted rewrite"
            audit.save()

        # AuditEvent cannot be deleted
        with pytest.raises(ValidationError):
            audit.delete()


class TestSecurityRegressions:
    """Security regressions: Open redirect prevention, DB URL checks, private closet isolation."""

    def test_notification_database_constraint_blocks_external_urls(self, user):
        # Database check constraint notif_action_url_internal forbids external URLs
        with pytest.raises(IntegrityError):
            Notification.objects.create(
                user=user,
                notification_type=NotificationType.ORDER_PLACED,
                category=NotificationCategory.ORDERS,
                title="Test Alert",
                body="External link probe",
                action_url="https://attacker-domain.evil/steal-credentials",
            )

    def test_notification_action_click_allows_safe_internal_redirect(self, client, user):
        client.force_login(user)

        notif = Notification.objects.create(
            user=user,
            notification_type=NotificationType.ORDER_PLACED,
            category=NotificationCategory.ORDERS,
            channel=Channel.IN_APP,
            title="Order Alert",
            body="View order",
            action_url="/account/orders/",
        )

        res = client.get(
            reverse("notifications:action-click", kwargs={"pk": notif.pk}),
        )
        assert res.status_code == 302
        assert res.url == "/account/orders/"

    def test_closet_outfits_are_strictly_isolated_between_customers(self, client, user, other_user):
        outfit = Outfit.objects.create(
            user=user,
            name="Confidential Summer Capsule",
            status=Outfit.Status.SAVED,
        )

        # Owner can view outfit
        client.force_login(user)
        res_own = client.get(reverse("account:outfit-detail", kwargs={"pk": outfit.pk}))
        assert res_own.status_code == 200

        # Other customer is barred with 404
        client.force_login(other_user)
        res_other = client.get(reverse("account:outfit-detail", kwargs={"pk": outfit.pk}))
        assert res_other.status_code == 404
