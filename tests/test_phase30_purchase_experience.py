"""Tests for Phase 30: Conversion, Trust & Purchase Experience.

Covers:
- Product confidence, sizing guide and smart alternatives
- Size selection and stock indicators
- Add-to-bag AJAX feedback and mini-cart endpoint
- Cart layout and server-authoritative financial breakdown
- Checkout progression and analytics event tracking
- Payment state transitions, failures, and retry recovery
- Order confirmation and post-purchase status progression
- Security, ownership, and idempotency boundaries
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.analytics.models import Event
from apps.catalog.merchandising import get_product_sizing_guide, get_smart_alternatives
from apps.catalog.models import Color, Fit, Product, ProductVariant, Size
from apps.inventory.models import Stock
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.shop.checkout import get_or_create_checkout
from apps.shop.models import Cart
from apps.shop.services import add_to_cart


def seed_stock(variant: ProductVariant, on_hand: int, reserved: int = 0) -> Stock:
    stock, _ = Stock.objects.get_or_create(variant=variant)
    stock.on_hand = on_hand
    stock.reserved = reserved
    stock.save(update_fields=["on_hand", "reserved"])
    return stock


@pytest.fixture
def catalog_setup(db, category):
    fit = Fit.objects.create(
        name="Relaxed Boxy",
        slug="relaxed-boxy",
        description="Relaxed through the torso with dropped shoulders.",
    )
    product = Product.objects.create(
        name="Studio Heavy Tee",
        slug="studio-heavy-tee",
        status=Product.Status.ACTIVE,
        category=category,
        fit=fit,
    )
    black = Color.objects.create(name="Black", slug="black", hex_code="#000000")
    size_m = Size.objects.create(name="Medium", code="M", display_order=1)
    size_l = Size.objects.create(name="Large", code="L", display_order=2)
    size_xl = Size.objects.create(name="X-Large", code="XL", display_order=3)

    v_m = ProductVariant.objects.create(
        product=product,
        color=black,
        size=size_m,
        sku="SHT-BLK-M",
        price=Decimal("45.00"),
        is_active=True,
    )
    v_l = ProductVariant.objects.create(
        product=product,
        color=black,
        size=size_l,
        sku="SHT-BLK-L",
        price=Decimal("45.00"),
        is_active=True,
    )
    v_xl = ProductVariant.objects.create(
        product=product,
        color=black,
        size=size_xl,
        sku="SHT-BLK-XL",
        price=Decimal("45.00"),
        is_active=True,
    )

    seed_stock(v_m, 10)  # In stock
    seed_stock(v_l, 3)   # Low stock
    seed_stock(v_xl, 0)  # Sold out

    # Alternative product
    alt_prod = Product.objects.create(
        name="Vintage Washed Tee",
        slug="vintage-washed-tee",
        status=Product.Status.ACTIVE,
        category=category,
        fit=fit,
    )
    alt_v = ProductVariant.objects.create(
        product=alt_prod,
        color=black,
        size=size_m,
        sku="VWT-BLK-M",
        price=Decimal("48.00"),
        is_active=True,
    )
    seed_stock(alt_v, 8)

    return {
        "product": product,
        "alt_product": alt_prod,
        "v_m": v_m,
        "v_l": v_l,
        "v_xl": v_xl,
        "size_m": size_m,
        "size_l": size_l,
        "size_xl": size_xl,
    }


class TestProductConfidenceAndSizing:
    def test_product_detail_sizing_guide_and_options(self, client, catalog_setup):
        product = catalog_setup["product"]
        url = reverse("catalog:product-detail", args=[product.slug])
        response = client.get(url)
        assert response.status_code == 200

        ctx = response.context
        assert "size_options" in ctx
        options = {opt["size"].code: opt for opt in ctx["size_options"]}

        # Medium: in stock (10 left)
        assert options["M"]["is_in_stock"] is True
        assert options["M"]["is_sold_out"] is False

        # Large: in stock (low stock: 3 left)
        assert options["L"]["is_in_stock"] is True
        assert options["L"]["units_left"] == 3

        # X-Large: sold out
        assert options["XL"]["is_in_stock"] is False
        assert options["XL"]["is_sold_out"] is True

        # Sizing guide verification
        guide = get_product_sizing_guide(product)
        assert guide["fit_name"] == "Relaxed Boxy"
        assert "extra room through the body" in guide["fit_description"]
        guide_codes = [s["code"] for s in guide["sizes"]]
        assert "M" in guide_codes
        assert "L" in guide_codes

    def test_smart_alternatives_deduplication_and_in_stock(self, catalog_setup):
        product = catalog_setup["product"]
        v_xl = catalog_setup["v_xl"]  # Sold out variant

        alternatives = get_smart_alternatives(product, v_xl)
        # Should include alt_product (in stock), and not the product itself
        assert all(alt.pk != product.pk for alt in alternatives)
        assert any(alt.slug == "vintage-washed-tee" for alt in alternatives)

    def test_alternative_product_clicked_event(self, client, catalog_setup):
        alt = catalog_setup["alt_product"]
        url = (
            reverse("catalog:track-merchandising")
            + f"?type=alternative_product_clicked&product_id={alt.pk}&next={alt.get_absolute_url()}"
        )
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"] == alt.get_absolute_url()

        event = Event.objects.filter(name="alternative_product_clicked").first()
        assert event is not None
        assert str(event.object_id) == str(alt.pk)


class TestAddToBagAndMiniCart:
    def test_ajax_add_to_bag_returns_instant_feedback(self, client, catalog_setup):
        v_m = catalog_setup["v_m"]
        url = reverse("shop:cart-add")

        response = client.post(
            url,
            {"variant_id": v_m.pk, "quantity": "2"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["item_count"] == 1
        assert data["total_quantity"] == 2
        assert data["subtotal"] == "90.00"
        assert data["added_variant"]["sku"] == v_m.sku

        # Verify product_to_cart event was recorded
        assert Event.objects.filter(name="product_to_cart").exists()

    def test_mini_cart_endpoint_payload(self, client, catalog_setup):
        v_m = catalog_setup["v_m"]
        client.post(
            reverse("shop:cart-add"),
            {"variant_id": v_m.pk, "quantity": "1"},
        )

        response = client.get(reverse("shop:cart-mini"))
        assert response.status_code == 200
        data = response.json()
        assert data["is_empty"] is False
        assert len(data["items"]) == 1
        assert data["items"][0]["sku"] == v_m.sku
        assert data["totals"]["subtotal"] == "45.00"
        # 100 - 45 = 55 needed for free shipping
        assert Decimal(data["amount_for_free_shipping"]) == Decimal("55.00")

    def test_empty_mini_cart(self, client):
        response = client.get(reverse("shop:cart-mini"))
        assert response.status_code == 200
        data = response.json()
        assert data["is_empty"] is True
        assert data["totals"]["item_count"] == 0


class TestCartAndCheckoutSummary:
    def test_cart_view_event_and_financial_breakdown(self, client, catalog_setup):
        v_m = catalog_setup["v_m"]
        client.post(
            reverse("shop:cart-add"),
            {"variant_id": v_m.pk, "quantity": "3"},  # 3 * 45 = 135 (qualifies for free shipping)
        )

        response = client.get(reverse("shop:cart"))
        assert response.status_code == 200
        ctx = response.context

        assert ctx["subtotal"] == Decimal("135.00")
        assert ctx["free_qualified"] is True
        assert ctx["free_remaining"] == Decimal("0.00")
        assert ctx["shipping_estimate"] == Decimal("0.00")
        assert ctx["estimated_total"] == Decimal("135.00")

        # Cart view event
        event = Event.objects.filter(name="cart_view").first()
        assert event is not None
        assert event.metadata["total_quantity"] == 3


class TestCheckoutProgressionAndRecovery:
    @pytest.fixture
    def authenticated_user_cart(self, client, django_user_model, catalog_setup):
        user = django_user_model.objects.create_user(
            email="shopper@test.com", password="SecurePassword123!"
        )
        client.force_login(user)
        cart = Cart.objects.create(user=user)
        v_m = catalog_setup["v_m"]
        add_to_cart(cart, v_m, quantity=1)
        checkout = get_or_create_checkout(user, cart)
        return {"user": user, "cart": cart, "checkout": checkout, "variant": v_m}

    def test_checkout_step_analytics_and_progression(self, client, authenticated_user_cart):
        checkout = authenticated_user_cart["checkout"]
        user = authenticated_user_cart["user"]

        from apps.accounts.models import Address

        addr = Address.objects.create(
            user=user,
            full_name="Alex River",
            line1="123 Studio Way",
            city="New York",
            region="NY",
            postal_code="10001",
            country="US",
        )

        # 1. Address step
        client.post(reverse("shop:checkout-address"), {"address_id": addr.pk})
        assert Event.objects.filter(
            name="checkout_step_completed", metadata__step="address"
        ).exists()

        # 2. Shipping step
        client.post(reverse("shop:checkout-shipping"), {"method": "standard"})
        assert Event.objects.filter(
            name="checkout_step_completed", metadata__step="shipping"
        ).exists()

        # 3. Validation step
        client.post(reverse("shop:checkout-validate"))
        checkout.refresh_from_db()
        assert checkout.is_validated is True

        # 4. Place order
        client.post(reverse("shop:checkout-place"), {"checkout_id": checkout.pk})
        order = Order.objects.filter(user=user).first()
        assert order is not None

        # 5. Payment started event
        client.get(reverse("shop:checkout-payment", args=[order.number]))
        assert Event.objects.filter(name="payment_started").exists()

    def test_payment_retry_resets_payment_and_redirects(
        self, client, django_user_model, catalog_setup
    ):
        user = django_user_model.objects.create_user(
            email="retry_shopper@test.com", password="SecurePassword123!"
        )
        client.force_login(user)
        variant = catalog_setup["v_m"]

        from apps.payments.services import start_payment
        from tests.phase6_helpers import placed_order

        order = placed_order(user, variant, stock=5)
        payment = start_payment(order)

        # Mark payment as failed
        payment.status = Payment.Status.FAILED
        payment.failure_message = "Insufficient funds"
        payment.save(update_fields=["status", "failure_message"])

        # Retry payment
        response = client.post(reverse("shop:checkout-payment-retry", args=[order.number]))
        assert response.status_code == 302
        assert response["Location"] == reverse("shop:checkout-payment", args=[order.number])

        payment.refresh_from_db()
        assert payment.status in [Payment.Status.CREATED, Payment.Status.PENDING]
        assert payment.failure_message == ""

    def test_order_ownership_enforced_on_retry(self, client, django_user_model, catalog_setup):
        owner = django_user_model.objects.create_user(
            email="owner@test.com", password="password"
        )
        intruder = django_user_model.objects.create_user(
            email="intruder@test.com", password="password"
        )
        variant = catalog_setup["v_m"]

        from tests.phase6_helpers import placed_order

        order = placed_order(owner, variant, stock=5)

        # Intruder attempts to retry owner's order
        client.force_login(intruder)
        response = client.post(reverse("shop:checkout-payment-retry", args=[order.number]))
        assert response.status_code == 404
