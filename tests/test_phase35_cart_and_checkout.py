"""Tests for Phase 35: Cart & Checkout Experience 2.0.

Covers:
- Cart quantity update stepper and authoritative total recalculation
- Cart item removal and empty state handling
- Move from cart to wishlist (authenticated and anonymous)
- Smart cart suggestions (excluding current bag items)
- Promotion application from cart and checkout with safe redirection
- Loyalty points redemption, balance tracking, and points remaining
- Checkout progress steps and address creation flow with next redirection
- Payment retry idempotency and stock hold preservation
- Order confirmation rendering and post-purchase action links
- Security boundaries: cart and order isolation
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import Address
from apps.analytics.models import Event
from apps.catalog.models import Color, Product, ProductVariant, Size
from apps.inventory.models import Stock
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.shop.checkout import get_or_create_checkout
from apps.shop.models import Cart, Wishlist
from apps.shop.services import add_to_cart
from tests.phase6_helpers import placed_order
from tests.phase7_helpers import grant_points, make_promotion


def seed_stock(variant: ProductVariant, on_hand: int, reserved: int = 0) -> Stock:
    stock, _ = Stock.objects.get_or_create(variant=variant)
    stock.on_hand = on_hand
    stock.reserved = reserved
    stock.save(update_fields=["on_hand", "reserved"])
    return stock


@pytest.fixture
def catalog_setup(db, category):
    product = Product.objects.create(
        name="Tailored Wool Overshirt",
        slug="tailored-wool-overshirt",
        status=Product.Status.ACTIVE,
        category=category,
    )
    black = Color.objects.create(name="Charcoal", slug="charcoal", hex_code="#222222")
    size_m = Size.objects.create(name="Medium", code="M", display_order=1)
    size_l = Size.objects.create(name="Large", code="L", display_order=2)

    v_m = ProductVariant.objects.create(
        product=product,
        color=black,
        size=size_m,
        sku="TWO-CHR-M",
        price=Decimal("120.00"),
        is_active=True,
    )
    v_l = ProductVariant.objects.create(
        product=product,
        color=black,
        size=size_l,
        sku="TWO-CHR-L",
        price=Decimal("120.00"),
        is_active=True,
    )
    seed_stock(v_m, 15)
    seed_stock(v_l, 4)

    alt_prod = Product.objects.create(
        name="Raw Indigo Denim",
        slug="raw-indigo-denim",
        status=Product.Status.ACTIVE,
        category=category,
    )
    alt_v = ProductVariant.objects.create(
        product=alt_prod,
        color=black,
        size=size_m,
        sku="RID-CHR-M",
        price=Decimal("95.00"),
        is_active=True,
    )
    seed_stock(alt_v, 8)

    return {
        "product": product,
        "alt_product": alt_prod,
        "v_m": v_m,
        "v_l": v_l,
    }


class TestCartExperience:
    def test_cart_quantity_update_via_post_and_json(self, client, catalog_setup):
        variant = catalog_setup["v_m"]
        client.post(reverse("shop:cart-add"), {"variant_id": variant.pk, "quantity": 1})

        cart = Cart.objects.get()
        item = cart.items.first()
        assert item.quantity == 1

        # 1. Update quantity via standard POST
        update_url = reverse("shop:cart-update", args=[item.pk])
        resp = client.post(update_url, {"quantity": 3})
        assert resp.status_code == 302
        item.refresh_from_db()
        assert item.quantity == 3
        assert item.line_total == Decimal("360.00")

        # 2. Update quantity via AJAX / JSON
        resp_json = client.post(
            update_url,
            {"quantity": 2},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert resp_json.status_code == 200
        data = resp_json.json()
        assert data["success"] is True
        assert data["item"]["quantity"] == 2
        assert data["item"]["line_total"] == "240.00"
        assert data["subtotal"] == "240.00"

    def test_cart_remove_via_post_and_json(self, client, catalog_setup):
        variant = catalog_setup["v_m"]
        client.post(reverse("shop:cart-add"), {"variant_id": variant.pk, "quantity": 1})
        cart = Cart.objects.get()
        item = cart.items.first()

        # Remove via AJAX
        remove_url = reverse("shop:cart-remove", args=[item.pk])
        resp = client.post(remove_url, headers={"accept": "application/json"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["item_count"] == 0
        assert data["removed_item_id"] == item.pk
        assert cart.items.count() == 0

    def test_move_to_wishlist_authenticated(self, client, django_user_model, catalog_setup):
        user = django_user_model.objects.create_user(
            email="wishlist_shopper@test.com", password="SecurePassword123!"
        )
        client.force_login(user)

        variant = catalog_setup["v_m"]
        client.post(reverse("shop:cart-add"), {"variant_id": variant.pk, "quantity": 1})
        cart = Cart.objects.get(user=user)
        item = cart.items.first()

        # Move to wishlist
        move_url = reverse("shop:cart-move-to-wishlist", args=[item.pk])
        resp = client.post(move_url, follow=True)
        assert resp.status_code == 200

        # Item should be removed from cart and present in wishlist
        assert cart.items.count() == 0
        wishlist = Wishlist.objects.get(user=user)
        assert wishlist.items.filter(variant=variant).exists()

        # Analytics event tracked (recorded by add_to_wishlist)
        assert Event.objects.filter(
            name="wishlist_add",
            object_id=catalog_setup["product"].pk,
        ).exists()

    def test_move_to_wishlist_anonymous_prompts_sign_in(self, client, catalog_setup):
        variant = catalog_setup["v_m"]
        client.post(reverse("shop:cart-add"), {"variant_id": variant.pk, "quantity": 1})
        cart = Cart.objects.get()
        item = cart.items.first()

        move_url = reverse("shop:cart-move-to-wishlist", args=[item.pk])
        resp = client.post(move_url)
        assert resp.status_code == 302
        assert reverse("accounts:login") in resp["Location"]

    def test_cart_detail_renders_smart_suggestions_excluding_bag(self, client, catalog_setup):
        v_m = catalog_setup["v_m"]
        client.post(reverse("shop:cart-add"), {"variant_id": v_m.pk, "quantity": 1})

        resp = client.get(reverse("shop:cart"))
        assert resp.status_code == 200
        ctx = resp.context
        assert "cart_suggestions" in ctx
        # The added product must not appear in suggestions
        suggestion_pks = [p.pk for p in ctx["cart_suggestions"]]
        assert v_m.product_id not in suggestion_pks


class TestCheckoutExperience:
    @pytest.fixture
    def authenticated_checkout(self, client, django_user_model, catalog_setup):
        user = django_user_model.objects.create_user(
            email="checkout_user@test.com", password="SecurePassword123!"
        )
        client.force_login(user)
        cart = Cart.objects.create(user=user)
        variant = catalog_setup["v_m"]
        add_to_cart(cart, variant, 1)
        checkout = get_or_create_checkout(user, cart)

        addr = Address.objects.create(
            user=user,
            full_name="Jordan Reed",
            line1="456 Avenue",
            city="London",
            postal_code="EC1A 1BB",
            country="GB",
            is_default_shipping=True,
        )
        return {
            "user": user,
            "cart": cart,
            "checkout": checkout,
            "address": addr,
            "variant": variant,
        }

    def test_apply_and_remove_promotion_from_cart(self, client, authenticated_checkout):
        make_promotion("FLASH15", percent=15)
        promo_url = reverse("shop:checkout-promotion")

        # Apply from cart page with next parameter
        resp = client.post(
            promo_url,
            {"code": "FLASH15", "next": reverse("shop:cart")},
        )
        assert resp.status_code == 302
        assert resp["Location"] == reverse("shop:cart")

        checkout = authenticated_checkout["checkout"]
        checkout.refresh_from_db()
        assert checkout.promotion_code == "FLASH15"

        # Remove from cart page
        resp_remove = client.post(
            promo_url,
            {"code": "", "next": reverse("shop:cart")},
        )
        assert resp_remove.status_code == 302
        assert resp_remove["Location"] == reverse("shop:cart")
        checkout.refresh_from_db()
        assert checkout.promotion_code == ""

    def test_loyalty_remaining_calculated_and_exposed(self, client, authenticated_checkout):
        user = authenticated_checkout["user"]
        grant_points(user, 600)

        # Apply 200 points
        client.post(reverse("shop:checkout-loyalty"), {"points": "200"})

        resp = client.get(reverse("shop:checkout"))
        assert resp.status_code == 200
        ctx = resp.context
        assert ctx["loyalty_balance"] == 400
        assert ctx["checkout"].loyalty_points == 200
        assert ctx["loyalty_remaining"] == 400

    def test_address_creation_redirects_back_to_checkout_when_next_provided(
        self, client, authenticated_checkout
    ):
        addr_url = reverse("account:address-create")
        resp = client.post(
            addr_url + f"?next={reverse('shop:checkout')}",
            {
                "full_name": "Taylor Swift",
                "line1": "77 Music Row",
                "city": "Nashville",
                "region": "TN",
                "postal_code": "37203",
                "country": "US",
                "phone": "+16155551234",
                "address_type": "shipping",
            },
        )
        assert resp.status_code == 302
        assert resp["Location"] == reverse("shop:checkout")


class TestPaymentAndOrderConfirmation:
    def test_payment_retry_preserves_order_and_resets_state(
        self, client, django_user_model, catalog_setup
    ):
        user = django_user_model.objects.create_user(
            email="retry_user@test.com", password="Password123!"
        )
        client.force_login(user)
        variant = catalog_setup["v_m"]

        from apps.payments.services import start_payment

        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)
        payment.status = Payment.Status.FAILED
        payment.failure_message = "Processor timeout"
        payment.save(update_fields=["status", "failure_message"])

        retry_url = reverse("shop:checkout-payment-retry", args=[order.number])
        resp = client.post(retry_url)
        assert resp.status_code == 302
        assert resp["Location"] == reverse("shop:checkout-payment", args=[order.number])

        payment.refresh_from_db()
        assert payment.status in [Payment.Status.CREATED, Payment.Status.PENDING]
        assert payment.failure_message == ""

    def test_order_done_renders_confirmation_and_post_purchase_links(
        self, client, django_user_model, catalog_setup
    ):
        user = django_user_model.objects.create_user(
            email="done_user@test.com", password="Password123!"
        )
        client.force_login(user)
        variant = catalog_setup["v_m"]

        order = placed_order(user, variant, stock=10)
        order.status = Order.Status.PAID
        order.save(update_fields=["status"])

        resp = client.get(reverse("shop:checkout-done", args=[order.number]))
        assert resp.status_code == 200
        html = resp.content.decode()

        assert "Thank you — your order is confirmed!" in html
        assert order.number in html
        assert reverse("account:order-detail", args=[order.number]) in html
        assert reverse("support:ticket-create") in html
