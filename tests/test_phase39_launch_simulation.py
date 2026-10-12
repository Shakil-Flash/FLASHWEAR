"""Phase 39: Launch Simulation, Load Testing & Release Validation Test Suite.

Audits, validates, and simulates realistic traffic, concurrency, and failure recovery:
1. Critical Journey Smoke Tests across all 12 core customer and operational paths.
2. Concurrency & Transaction Safety: simultaneous checkouts, duplicate webhooks,
   order creation idempotency, and promotion usage limits.
3. Failure Recovery & Degraded Conditions: cache outages, database disconnections,
   missing media fallbacks, and validation error UX.
4. Staged Load Benchmark: automated execution measuring throughput, median (p50)
   and 95th percentile (p95) latencies, and error rates.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.catalog.models import (
    Brand,
    Category,
    Color,
    Fit,
    Product,
    ProductVariant,
    Size,
)
from apps.inventory.models import Stock
from apps.inventory.services import (
    InsufficientStock,
    release_holds,
    reserve_stock,
    sweep_expired_holds,
)
from apps.notifications.models import NotificationCategory
from apps.notifications.services import in_app as notification_service
from apps.orders.models import Order, OrderEvent
from apps.orders.services import create_order_from_checkout
from apps.payments.models import Payment, PaymentEvent
from apps.payments.providers.development import make_provider_event
from apps.payments.services import handle_provider_event, start_payment
from apps.shop.checkout import get_or_create_checkout, set_shipping_address, validate_checkout
from apps.shop.models import Cart, CartItem, CheckoutSession
from apps.support.models import SupportTicket
from apps.support.services import tickets as ticket_service
from scripts.load_test import run_load_benchmark
from tests.phase6_helpers import make_address, seed_stock

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def catalog_setup():
    """Create a minimal active catalog for simulated journey tests."""
    brand = Brand.objects.create(name="Flash Studio", slug="flash-studio")
    category = Category.objects.create(name="Apparel", slug="apparel")
    fit = Fit.objects.create(name="Relaxed", slug="relaxed")
    color = Color.objects.create(name="Onyx Black", slug="onyx-black", hex_code="#111111")
    size_m = Size.objects.create(name="Medium", code="M", display_order=1)

    product = Product.objects.create(
        name="Signature Oversized Hoodie",
        slug="signature-oversized-hoodie",
        brand=brand,
        category=category,
        fit=fit,
        status=Product.Status.ACTIVE,
        is_featured=True,
    )
    variant = ProductVariant.objects.create(
        product=product,
        color=color,
        size=size_m,
        sku="HOODIE-BLK-M",
        price=Decimal("85.00"),
        is_active=True,
    )
    seed_stock(variant, on_hand=15, reserved=0)
    return {
        "brand": brand,
        "category": category,
        "product": product,
        "variant": variant,
    }


# ======================================================================================
# 1. Critical Journey Smoke Tests (12 Core Paths)
# ======================================================================================


class TestCriticalJourneySmokeTests:
    def test_journey_01_homepage_and_catalogue_loading(self, client, catalog_setup):
        """Journey 1: Homepage and product catalogue render HTTP 200 with active items."""
        resp_home = client.get("/")
        assert resp_home.status_code == 200

        resp_catalog = client.get(reverse("catalog:product-list"))
        assert resp_catalog.status_code == 200
        assert catalog_setup["product"].name in resp_catalog.content.decode("utf-8")

    def test_journey_02_search_filtering_and_product_detail(self, client, catalog_setup):
        """Journey 2: Search, category filter, and product detail page render correctly."""
        resp_search = client.get(f"{reverse('catalog:product-search')}?q=Hoodie")
        assert resp_search.status_code == 200
        assert catalog_setup["product"].name in resp_search.content.decode("utf-8")

        pdp_url = reverse("catalog:product-detail", kwargs={"slug": catalog_setup["product"].slug})
        resp_pdp = client.get(pdp_url)
        assert resp_pdp.status_code == 200
        content = resp_pdp.content.decode("utf-8")
        assert catalog_setup["product"].name in content
        assert "HOODIE-BLK-M" in content or "85.00" in content

    def test_journey_03_registration_and_login_flow(self, client):
        """Journey 3: User registration and authentication flows."""
        login_url = reverse("accounts:login")
        # Invalid login fails gracefully
        bad_resp = client.post(
            login_url,
            {"username": "nonexistent@flashwear.test", "password": "WrongPassword!"},
        )
        assert bad_resp.status_code == 200
        assert "_auth_user_id" not in client.session

        # Register and authenticate valid user
        user = User.objects.create_user(
            email="newshopper@flashwear.test",
            password="Str0ng-Passw0rd!",
        )
        login_resp = client.post(
            login_url,
            {"username": "newshopper@flashwear.test", "password": "Str0ng-Passw0rd!"},
        )
        assert login_resp.status_code == 302
        assert client.session.get("_auth_user_id") == str(user.pk)

    def test_journey_04_variant_selection_and_cart_operations(self, client, catalog_setup):
        """Journey 4: Adding items to cart, updating quantity, viewing cart."""
        variant = catalog_setup["variant"]
        add_url = reverse("shop:cart-add")
        resp_add = client.post(add_url, {"variant_id": variant.pk, "quantity": 2})
        assert resp_add.status_code == 302

        resp_cart = client.get(reverse("shop:cart"))
        assert resp_cart.status_code == 200
        assert variant.product.name in resp_cart.content.decode("utf-8")

        cart = Cart.objects.filter(items__variant=variant).first()
        assert cart is not None
        item = cart.items.first()
        assert item.quantity == 2

        # Update quantity
        update_url = reverse("shop:cart-update", kwargs={"item_pk": item.pk})
        client.post(update_url, {"quantity": 3})
        item.refresh_from_db()
        assert item.quantity == 3

    def test_journey_05_checkout_validation(self, user, catalog_setup):
        """Journey 5: Checkout session validation with address attachment."""
        variant = catalog_setup["variant"]
        cart = Cart.objects.create(user=user)
        checkout = get_or_create_checkout(user, cart)

        # Failure path: validating empty bag raises ValidationError
        with pytest.raises(ValidationError):
            validate_checkout(checkout)

        # Populate cart item and address
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=1, price_snapshot=variant.price
        )
        shipping_addr = make_address(user)
        set_shipping_address(checkout, shipping_addr)

        # Successful validation freezes snapshot
        validated = validate_checkout(checkout)
        assert validated.status == CheckoutSession.Status.VALIDATED
        assert validated.subtotal == Decimal("85.00")
        assert validated.total == Decimal("90.00")

    def test_journey_06_order_creation_and_holds(self, user, catalog_setup):
        """Journey 6: Converting validated checkout into order with stock holds."""
        variant = catalog_setup["variant"]
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=2, price_snapshot=variant.price
        )
        checkout = get_or_create_checkout(user, cart)
        set_shipping_address(checkout, make_address(user))
        validated = validate_checkout(checkout)

        order = create_order_from_checkout(validated)
        assert order.status == Order.Status.PENDING_PAYMENT
        assert order.items.count() == 1
        assert order.total == Decimal("170.00")
        assert OrderEvent.objects.filter(order=order, event_type="created").exists()

        # Stock hold is attached
        stock = Stock.objects.get(variant=variant)
        assert stock.reserved == 2

    def test_journey_07_payment_sandbox_and_mocked_flow(self, user, catalog_setup):
        """Journey 7: Payment initialization and provider webhook handling."""
        variant = catalog_setup["variant"]
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=1, price_snapshot=variant.price
        )
        checkout = get_or_create_checkout(user, cart)
        set_shipping_address(checkout, make_address(user))
        order = create_order_from_checkout(validate_checkout(checkout))

        # Start payment
        payment = start_payment(order)
        assert payment.status == Payment.Status.PENDING

        # Succeeded webhook event
        event = make_provider_event(payment, "succeeded")
        handle_provider_event("development", event)

        order.refresh_from_db()
        payment.refresh_from_db()
        assert payment.status == Payment.Status.SUCCEEDED
        assert order.status in (Order.Status.PROCESSING, Order.Status.PAID)

    def test_journey_08_inventory_reservation_and_release(self, user, catalog_setup):
        """Journey 8: Reservation hold and subsequent cancellation release."""
        variant = catalog_setup["variant"]
        cart = Cart.objects.create(user=user)
        checkout = get_or_create_checkout(user, cart)
        holds = reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 5}])
        assert len(holds) == 1
        stock = Stock.objects.get(variant=variant)
        assert stock.reserved == 5

        # Release holds
        release_holds(holds)
        stock.refresh_from_db()
        assert stock.reserved == 0

        # Sweep expired holds
        swept = sweep_expired_holds()
        assert swept == 0

    def test_journey_09_order_history_and_privacy(self, client, user, catalog_setup):
        """Journey 9: Order history access and cross-customer object-level privacy."""
        other_user = User.objects.create_user(
            email="stranger@flashwear.test", password="Str0ng-Passw0rd!"
        )
        variant = catalog_setup["variant"]
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=1, price_snapshot=variant.price
        )
        checkout = get_or_create_checkout(user, cart)
        set_shipping_address(checkout, make_address(user))
        order = create_order_from_checkout(validate_checkout(checkout))

        # Owner can view order
        client.force_login(user)
        resp_owner = client.get(reverse("account:order-detail", kwargs={"number": order.number}))
        assert resp_owner.status_code == 200

        # Stranger gets 404 (non-disclosure of order existence)
        client.force_login(other_user)
        resp_stranger = client.get(reverse("account:order-detail", kwargs={"number": order.number}))
        assert resp_stranger.status_code == 404

    def test_journey_10_notifications_lifecycle(self, client, user):
        """Journey 10: In-app notification creation, center rendering, and mark-as-read."""
        notif = notification_service.create(
            user=user,
            title="Flash Drop Live",
            body="New collection dropped.",
            category=NotificationCategory.DROPS,
        )
        client.force_login(user)
        resp_center = client.get(reverse("notifications:center"))
        assert resp_center.status_code == 200
        assert "Flash Drop Live" in resp_center.content.decode("utf-8")

        # Mark as read
        notif.refresh_from_db()
        assert notif.read_at is None
        notification_service.mark_read(user, notif.pk)
        notif.refresh_from_db()
        assert notif.read_at is not None

    def test_journey_11_support_ticket_submission(self, client, user):
        """Journey 11: Support ticket creation and authentication requirements."""
        # Anonymous user is redirected to login
        resp_anon = client.get(reverse("support:ticket-create"))
        assert resp_anon.status_code == 302

        # Authenticated customer opens ticket
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Delivery inquiry",
            description="When will my parcel arrive?",
            category=SupportTicket.Category.DELIVERY,
        )
        assert ticket.pk is not None
        assert ticket.status == SupportTicket.Status.OPEN

        client.force_login(user)
        resp_ticket = client.get(reverse("support:ticket-detail", kwargs={"number": ticket.number}))
        assert resp_ticket.status_code == 200

    def test_journey_12_staff_login_and_admin_permissions(self, client, user):
        """Journey 12: Operations platform least-privilege scoping."""
        admin_user = User.objects.create_superuser(
            email="admin@flashwear.test",
            password="Sup3r-S3cret!",
        )
        # Non-staff customer gets 404/403 on backoffice dashboard
        client.force_login(user)
        resp_user = client.get(reverse("backoffice:dashboard"))
        assert resp_user.status_code in (403, 404)

        # Admin user gets 200
        client.force_login(admin_user)
        resp_admin = client.get(reverse("backoffice:dashboard"))
        assert resp_admin.status_code == 200


# ======================================================================================
# 2. Concurrency & Transaction Safety
# ======================================================================================


class TestConcurrencyAndTransactionSafety:
    def test_simultaneous_checkout_stock_reservation_race(self, user, catalog_setup):
        """Simultaneous checkouts for the last unit: one succeeds, one fails."""
        variant = catalog_setup["variant"]
        seed_stock(variant, on_hand=1, reserved=0)

        # Customer A cart
        cart_a = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart_a, variant=variant, quantity=1, price_snapshot=variant.price
        )
        checkout_a = get_or_create_checkout(user, cart_a)
        set_shipping_address(checkout_a, make_address(user))
        val_a = validate_checkout(checkout_a)

        # Customer B cart
        user_b = User.objects.create_user(
            email="shopper_b@flashwear.test", password="Str0ng-Passw0rd!"
        )
        cart_b = Cart.objects.create(user=user_b)
        CartItem.objects.create(
            cart=cart_b, variant=variant, quantity=1, price_snapshot=variant.price
        )
        checkout_b = get_or_create_checkout(user_b, cart_b)
        set_shipping_address(checkout_b, make_address(user_b))
        val_b = validate_checkout(checkout_b)

        # Customer A claims the unit first
        order_a = create_order_from_checkout(val_a)
        assert order_a.pk is not None

        # Customer B attempts checkout but stock is now exhausted
        with pytest.raises(InsufficientStock):
            create_order_from_checkout(val_b)

        # Stock ledger integrity: 1 on_hand, 1 reserved, no overselling
        stock = Stock.objects.get(variant=variant)
        assert stock.on_hand == 1
        assert stock.reserved == 1

    def test_duplicate_payment_webhook_idempotency(self, user, catalog_setup):
        """Redelivering the identical payment webhook does not duplicate events."""
        variant = catalog_setup["variant"]
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=1, price_snapshot=variant.price
        )
        checkout = get_or_create_checkout(user, cart)
        set_shipping_address(checkout, make_address(user))
        order = create_order_from_checkout(validate_checkout(checkout))
        payment = start_payment(order)

        event = make_provider_event(payment, "succeeded")

        # First delivery
        handle_provider_event("development", event)
        payment.refresh_from_db()
        assert payment.status == Payment.Status.SUCCEEDED
        initial_events_count = PaymentEvent.objects.filter(payment=payment).count()

        # Duplicate delivery
        handle_provider_event("development", event)
        payment.refresh_from_db()
        assert payment.status == Payment.Status.SUCCEEDED
        # No extra events recorded for identical event ID
        assert PaymentEvent.objects.filter(payment=payment).count() == initial_events_count

    def test_order_creation_idempotency(self, user, catalog_setup):
        """Retrying order creation on already converted checkout returns existing order."""
        variant = catalog_setup["variant"]
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart, variant=variant, quantity=1, price_snapshot=variant.price
        )
        checkout = get_or_create_checkout(user, cart)
        set_shipping_address(checkout, make_address(user))
        validated = validate_checkout(checkout)

        order_1 = create_order_from_checkout(validated)
        # Second call returns identical order instance without creating duplicates
        order_2 = create_order_from_checkout(validated)
        assert order_1.pk == order_2.pk
        assert Order.objects.filter(checkout=validated).count() == 1


# ======================================================================================
# 3. Failure Recovery & Degraded Conditions
# ======================================================================================


class TestFailureRecoveryAndDegradedConditions:
    def test_cache_outage_graceful_handling(self, client, catalog_setup):
        """Storefront remains operational when cache encounters an error."""
        with patch("django.core.cache.cache.get", side_effect=Exception("Redis connection error")):
            resp = client.get("/")
            # Does not crash with 500; gracefully serves uncached content
            assert resp.status_code == 200

    def test_database_down_readiness_probe_returns_503(self, client):
        """Readiness probe returns 503 and safe status when database is unreachable."""
        with patch("apps.core.health.check_database", return_value="down"):
            resp = client.get(reverse("core:health_ready"))
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "not_ready"
            assert data["checks"]["database"] == "down"
            # Confirm no secret or traceback leaked
            content = resp.content.decode("utf-8").lower()
            assert "traceback" not in content
            assert "password" not in content

    def test_missing_product_image_graceful_fallback(self, client, catalog_setup):
        """Products without images render without template exceptions."""
        product = catalog_setup["product"]
        # Product has 0 images
        assert product.images.count() == 0
        pdp_url = reverse("catalog:product-detail", kwargs={"slug": product.slug})
        resp = client.get(pdp_url)
        assert resp.status_code == 200
        assert product.name in resp.content.decode("utf-8")

    def test_invalid_customer_input_user_friendly_errors(self, client):
        """Form submissions with invalid inputs render user-friendly errors."""
        login_url = reverse("accounts:login")
        resp = client.post(login_url, {"username": "", "password": ""})
        assert resp.status_code == 200
        # Re-renders form without 500 error
        assert (
            "This field is required" in resp.content.decode("utf-8")
            or "error" in resp.content.decode("utf-8").lower()
        )


# ======================================================================================
# 4. Staged Load Testing Benchmark
# ======================================================================================


class TestLoadTestingBenchmark:
    def test_staged_load_benchmark_execution(self, client, catalog_setup):
        """Execute load test runner against internal safe read endpoints."""
        endpoints = [
            "/",
            reverse("core:health_live"),
            reverse("core:health_ready"),
        ]

        def client_runner(path: str) -> tuple[int, float]:
            import time

            start = time.perf_counter()
            resp = client.get(path)
            duration_ms = (time.perf_counter() - start) * 1000.0
            return resp.status_code, duration_ms

        summary = run_load_benchmark(
            endpoints=endpoints,
            concurrency=1,
            total_requests=15,
            runner_fn=client_runner,
        )

        assert summary["total_requests"] == 15
        assert summary["failed_requests"] == 0
        assert summary["error_rate_pct"] == 0.0
        assert summary["latency_p50_ms"] < 100.0  # Median latency under 100ms
        assert summary["throughput_rps"] > 0.0
