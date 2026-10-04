"""Phase 5: cart, wishlist and checkout.

The boundary these tests guard: checkout ends at *validated*. No payment
instrument, no order number, no stock reservation -- those belong to Phase 6, and
the last test in this file is here to prove the cart survives validation intact
for exactly that handoff.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Address
from apps.catalog.models import Product, ProductVariant
from apps.shop.checkout import set_shipping_address, set_shipping_method, validate_checkout
from apps.shop.models import Cart, CartItem, CheckoutSession, Wishlist, WishlistItem
from apps.shop.services import get_cart_totals, update_cart_item_quantity
from apps.shop.shipping import get_shipping_method, get_shipping_methods

HX = {"HX-Request": "true"}

pytestmark = pytest.mark.django_db


# =============================================================================
# Helpers
# =============================================================================


def add_to_bag(client, variant, quantity=1, *, headers=None):
    """POST a variant into the bag the way the product page form does."""
    return client.post(
        reverse("shop:cart-add"),
        {"variant_id": variant.pk, "quantity": quantity},
        headers=headers,
    )


def sign_in(client, email="shopper@flashwear.test"):
    """Real login POST, so the ``user_logged_in`` merge signal actually runs."""
    return client.post(
        reverse("accounts:login"),
        {"username": email, "password": "Str0ng-Passw0rd!"},
    )


def message_texts(response) -> str:
    return " | ".join(str(message) for message in response.context.get("messages", []))


def make_address(user, **overrides):
    params = {
        "user": user,
        "full_name": "Mallory Attacker",
        "phone": "+44 7700 900111",
        "line1": "9 Hostile Road",
        "line2": "",
        "city": "London",
        "region": "Greater London",
        "postal_code": "N1 1AA",
        "country": "GB",
        "address_type": "both",
    }
    params.update(overrides)
    return Address.objects.create(**params)


# =============================================================================
# Cart
# =============================================================================


class TestCart:
    def test_anonymous_add_creates_a_guest_cart(self, client, product):
        response = add_to_bag(client, product.variants.first())
        assert response.status_code == 302

        cart = Cart.objects.get()
        assert cart.user is None
        assert cart.session_key
        assert cart.status == Cart.Status.ACTIVE

        item = cart.items.get()
        assert item.quantity == 1
        assert item.price_snapshot == Decimal("49.00")

    def test_adding_the_same_variant_twice_stacks_one_row(self, client, product):
        variant = product.variants.first()
        add_to_bag(client, variant, 1)
        add_to_bag(client, variant, 2)

        cart = Cart.objects.get()
        assert cart.items.count() == 1
        assert cart.items.get().quantity == 3

    def test_cart_page_renders_the_line_and_the_availability_caveat(self, client, product):
        add_to_bag(client, product.variants.first())
        html = client.get(reverse("shop:cart")).content.decode()
        assert "FW-TEE-BLK-M" in html
        assert "Availability will be confirmed before order placement." in html
        assert "$49.00" in html

    def test_navbar_bag_badge_shows_the_quantity(self, client, product):
        add_to_bag(client, product.variants.first(), 2)
        html = client.get(reverse("shop:cart")).content.decode()
        assert 'aria-label="Bag, 2 items"' in html

    def test_htmx_add_returns_a_json_payload(self, client, product):
        response = add_to_bag(client, product.variants.first(), headers=HX)
        assert response.status_code == 200
        payload = response.json()
        assert payload["item_count"] == 1
        assert payload["total_quantity"] == 1
        assert payload["subtotal"] == "49.00"
        assert payload["currency"] == "USD"

    def test_quantity_update_changes_the_row(self, client, product):
        add_to_bag(client, product.variants.first())
        item = Cart.objects.get().items.get()

        response = client.post(reverse("shop:cart-update", args=[item.pk]), {"quantity": "5"})
        assert response.status_code == 302
        item.refresh_from_db()
        assert item.quantity == 5

    @pytest.mark.parametrize("bad", ["abc", "0", "-3", "2.5", ""])
    def test_nonsense_quantities_never_crash_or_apply(self, client, product, bad):
        add_to_bag(client, product.variants.first())
        item = Cart.objects.get().items.get()

        response = client.post(reverse("shop:cart-update", args=[item.pk]), {"quantity": bad})
        assert response.status_code == 302
        item.refresh_from_db()
        assert item.quantity == 1

    def test_htmx_rejects_a_bad_quantity_with_400(self, client, product):
        add_to_bag(client, product.variants.first())
        item = Cart.objects.get().items.get()

        response = client.post(
            reverse("shop:cart-update", args=[item.pk]), {"quantity": "abc"}, headers=HX
        )
        assert response.status_code == 400

    def test_quantity_cap_is_enforced(self, client, product):
        variant = product.variants.first()
        add_to_bag(client, variant, 99)

        response = add_to_bag(client, variant, 1, headers=HX)
        assert response.status_code == 400
        assert Cart.objects.get().items.get().quantity == 99

    def test_remove_and_clear(self, client, product):
        variant = product.variants.first()
        add_to_bag(client, variant)
        item = Cart.objects.get().items.get()

        client.post(reverse("shop:cart-remove", args=[item.pk]))
        assert not Cart.objects.get().items.exists()

        add_to_bag(client, variant)
        client.post(reverse("shop:cart-clear"))
        assert not Cart.objects.get().items.exists()

    def test_an_inactive_variant_cannot_be_added(self, client, product):
        variant = product.variants.first()
        ProductVariant.objects.filter(pk=variant.pk).update(is_active=False)

        response = add_to_bag(client, variant, headers=HX)
        assert response.status_code == 404
        assert CartItem.objects.count() == 0

    def test_an_unpublished_product_cannot_be_added(self, client, make_product, make_variant):
        draft = make_product(name="Coming Soon", slug="coming-soon", status=Product.Status.DRAFT)
        variant = make_variant(draft, sku="FW-DRAFT-ONE")

        response = add_to_bag(client, variant, headers=HX)
        assert response.status_code == 400
        assert CartItem.objects.count() == 0

    def test_get_never_writes_a_cart_row(self, client):
        response = client.get(reverse("shop:cart-count"))
        assert response.json() == {"count": 0}
        assert Cart.objects.count() == 0

    def test_mutations_require_post(self, client, product):
        assert client.get(reverse("shop:cart-add")).status_code == 405

    def test_mutations_require_csrf(self, product):
        client = Client(enforce_csrf_checks=True)
        response = add_to_bag(client, product.variants.first())
        assert response.status_code == 403

    def test_one_customer_cannot_touch_another_customers_line(
        self, client, user, other_user, product
    ):
        client.force_login(user)
        add_to_bag(client, product.variants.first(), 2)
        theirs = Cart.objects.get(user=user).items.get()

        client.force_login(other_user)
        response = client.post(reverse("shop:cart-update", args=[theirs.pk]), {"quantity": "9"})
        assert response.status_code == 302
        theirs.refresh_from_db()
        assert theirs.quantity == 2

    def test_totals_are_decimal_and_add_up(self, client, product):
        add_to_bag(client, product.variants.first(), 3)
        totals = get_cart_totals(Cart.objects.get())
        assert totals["subtotal"] == Decimal("147.00")
        assert totals["item_count"] == 1
        assert totals["total_quantity"] == 3

    def test_totals_endpoint_reports_decimal_strings(self, client, product):
        add_to_bag(client, product.variants.first(), 2)
        payload = client.get(reverse("shop:cart-totals")).json()
        assert payload["subtotal"] == "98.00"
        assert payload["item_count"] == 1
        assert payload["total_quantity"] == 2
        assert payload["currency"] == "USD"

    def test_add_without_a_variant_is_refused(self, client):
        response = client.post(reverse("shop:cart-add"), {}, headers=HX)
        assert response.status_code == 400

        response = client.post(reverse("shop:cart-add"), {})
        assert response.status_code == 302
        assert Cart.objects.count() == 0
        assert CartItem.objects.count() == 0

    def test_removing_over_htmx_returns_the_new_count(self, client, product):
        add_to_bag(client, product.variants.first())
        item = Cart.objects.get().items.get()

        response = client.post(reverse("shop:cart-remove", args=[item.pk]), headers=HX)
        assert response.status_code == 200
        assert response.json() == {
            "item_count": 0,
            "total_quantity": 0,
            "subtotal": "0.00",
            "currency": "USD",
        }

    def test_clearing_over_htmx_reports_an_empty_bag(self, client, product):
        add_to_bag(client, product.variants.first())
        response = client.post(reverse("shop:cart-clear"), headers=HX)
        assert response.status_code == 200
        assert response.json()["item_count"] == 0
        assert response.json()["total_quantity"] == 0


# =============================================================================
# Pricing: authoritative prices, snapshots only for detecting change
# =============================================================================


class TestPricing:
    def test_a_posted_price_cannot_override_the_catalogue(self, client, product):
        client.post(
            reverse("shop:cart-add"),
            {"variant_id": product.variants.first().pk, "quantity": 1, "price": "1.00"},
        )
        item = Cart.objects.get().items.get()
        assert item.price_snapshot == Decimal("49.00")

    def test_a_price_change_is_surfaced_and_totals_follow_it(self, client, product):
        variant = product.variants.first()
        add_to_bag(client, variant, 2)
        ProductVariant.objects.filter(pk=variant.pk).update(price=Decimal("59.00"))

        # Display recomputes from the live price; the snapshot is kept only for the diff.
        totals = get_cart_totals(Cart.objects.get())
        assert totals["subtotal"] == Decimal("118.00")

        html = client.get(reverse("shop:cart")).content.decode()
        assert "Prices have changed" in html
        assert "Price when added" in html
        assert "$118.00" in html

        item = Cart.objects.get().items.get()
        assert item.price_snapshot == Decimal("49.00")

    def test_checkout_review_shows_the_current_price(self, client, user, address, product):
        client.force_login(user)
        variant = product.variants.first()
        add_to_bag(client, variant)
        ProductVariant.objects.filter(pk=variant.pk).update(price=Decimal("55.00"))

        html = client.get(reverse("shop:checkout")).content.decode()
        assert "Prices changed since you added these items." in html
        assert "$55.00" in html


# =============================================================================
# Wishlist
# =============================================================================


class TestWishlist:
    def test_the_wishlist_is_signed_in_only(self, client):
        response = client.get(reverse("shop:wishlist"))
        assert response.status_code == 302
        assert "accounts/login" in response["Location"]
        assert "wishlist" in response["Location"]

    def test_add_render_and_remove(self, client, user, product):
        client.force_login(user)
        response = client.post(reverse("shop:wishlist-add"), {"product_id": product.pk})
        assert response.status_code == 302
        assert WishlistItem.objects.filter(wishlist__user=user, product=product).count() == 1

        html = client.get(reverse("shop:wishlist")).content.decode()
        assert product.name in html

        item = WishlistItem.objects.get()
        client.post(reverse("shop:wishlist-remove", args=[item.pk]))
        assert not WishlistItem.objects.exists()

    def test_saving_twice_keeps_one_row(self, client, user, product):
        client.force_login(user)
        for _ in range(2):
            client.post(reverse("shop:wishlist-add"), {"product_id": product.pk})
        assert WishlistItem.objects.count() == 1

    def test_a_variant_saving_is_variant_specific(self, client, user, product):
        client.force_login(user)
        variant = product.variants.first()
        payload = {"product_id": product.pk, "variant_id": variant.pk}
        client.post(reverse("shop:wishlist-add"), payload)
        client.post(reverse("shop:wishlist-add"), payload)
        client.post(reverse("shop:wishlist-add"), {"product_id": product.pk})

        # One variant row + one bare product row; duplicates collapse.
        assert WishlistItem.objects.count() == 2

    def test_you_cannot_remove_someone_elses_saved_item(self, client, user, other_user, product):
        client.force_login(user)
        client.post(reverse("shop:wishlist-add"), {"product_id": product.pk})
        theirs = WishlistItem.objects.get()

        client.force_login(other_user)
        response = client.post(reverse("shop:wishlist-remove", args=[theirs.pk]))
        assert response.status_code == 404
        assert WishlistItem.objects.filter(pk=theirs.pk).exists()

    def test_anonymous_count_is_zero_and_writes_nothing(self, client):
        response = client.get(reverse("shop:wishlist-count"))
        assert response.json() == {"count": 0}
        assert Wishlist.objects.count() == 0

    def test_a_draft_product_cannot_be_saved(self, client, user, draft_product):
        client.force_login(user)
        response = client.post(
            reverse("shop:wishlist-add"), {"product_id": draft_product.pk}, headers=HX
        )
        assert response.status_code == 404
        assert WishlistItem.objects.count() == 0

    def test_wishlist_add_without_a_product_id_is_refused(self, client, user):
        client.force_login(user)
        response = client.post(reverse("shop:wishlist-add"), {}, headers=HX)
        assert response.status_code == 400

        response = client.post(reverse("shop:wishlist-add"), {})
        assert response.status_code == 302
        assert WishlistItem.objects.count() == 0

    def test_wishlist_htmx_add_and_remove_return_counts(self, client, user, product):
        client.force_login(user)
        variant = product.variants.first()

        response = client.post(
            reverse("shop:wishlist-add"),
            {"product_id": product.pk, "variant_id": variant.pk},
            headers=HX,
        )
        assert response.json() == {"count": 1}

        item = WishlistItem.objects.get()
        response = client.post(reverse("shop:wishlist-remove", args=[item.pk]), headers=HX)
        assert response.json() == {"count": 0}

    def test_authenticated_wishlist_count(self, client, user, product):
        client.force_login(user)
        client.post(reverse("shop:wishlist-add"), {"product_id": product.pk})
        assert client.get(reverse("shop:wishlist-count")).json() == {"count": 1}


# =============================================================================
# Cart merge on login
# =============================================================================


class TestCartMerge:
    def test_the_guest_bag_follows_you_through_the_front_door(self, client, user, product):
        variant = product.variants.first()
        add_to_bag(client, variant)  # anonymous, before signing in
        guest_cart = Cart.objects.get(user=None)

        response = sign_in(client)
        assert response.status_code == 302

        user_cart = Cart.objects.get(user=user, status=Cart.Status.ACTIVE)
        assert user_cart.items.get().variant == variant

        guest_cart.refresh_from_db()
        assert guest_cart.status == Cart.Status.CONVERTED
        assert guest_cart.converted_at is not None

        # What the shopper now sees is the merged bag.
        html = client.get(reverse("shop:cart")).content.decode()
        assert "FW-TEE-BLK-M" in html

    def test_matching_lines_add_their_quantities(self, client, user, product):
        variant = product.variants.first()
        user_cart = Cart.objects.create(user=user, status=Cart.Status.ACTIVE)
        CartItem.objects.create(
            cart=user_cart, variant=variant, quantity=1, price_snapshot=variant.price
        )

        add_to_bag(client, variant, 2)
        sign_in(client)

        user_cart.refresh_from_db()
        assert user_cart.items.count() == 1
        assert user_cart.items.get().quantity == 3

    def test_a_merge_over_the_quantity_cap_is_skipped_not_fatal(self, client, user, product):
        variant = product.variants.first()
        user_cart = Cart.objects.create(user=user, status=Cart.Status.ACTIVE)
        CartItem.objects.create(
            cart=user_cart, variant=variant, quantity=99, price_snapshot=variant.price
        )

        add_to_bag(client, variant, 1)
        guest_cart = Cart.objects.get(user=None)

        response = sign_in(client)
        assert response.status_code == 302  # login itself never fails

        user_cart.refresh_from_db()
        assert user_cart.items.get().quantity == 99  # untouched, rollback held

        guest_cart.refresh_from_db()
        assert guest_cart.status == Cart.Status.ACTIVE

    def test_signing_in_without_a_guest_bag_changes_nothing(self, client, user):
        response = sign_in(client)
        assert response.status_code == 302
        assert Cart.objects.filter(status=Cart.Status.CONVERTED).count() == 0

    def test_a_signed_in_visitor_never_gains_a_second_active_cart(self, client, user, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first())
        add_to_bag(client, product.variants.first())
        assert Cart.objects.filter(user=user, status=Cart.Status.ACTIVE).count() == 1


# =============================================================================
# Shipping
# =============================================================================


class TestShipping:
    def test_rates_come_from_settings_keyed_on_the_subtotal(self):
        standard, express = get_shipping_methods(Decimal("49.00"))
        assert standard.code == "standard"
        assert standard.amount == Decimal("5.00")
        assert standard.estimate
        assert express.code == "express"
        assert express.amount == Decimal("15.00")

        free = get_shipping_methods(Decimal("100.00"))[0]
        assert free.amount == Decimal("0.00")
        assert free.is_free

    def test_an_unknown_code_resolves_to_nothing(self):
        assert get_shipping_method("drone", Decimal("49.00")) is None
        assert get_shipping_method("standard", Decimal("49.00")).code == "standard"


# =============================================================================
# Checkout
# =============================================================================


class TestCheckout:
    def test_checkout_is_signed_in_only(self, client):
        response = client.get(reverse("shop:checkout"))
        assert response.status_code == 302
        assert "accounts/login" in response["Location"]
        assert "checkout" in response["Location"]

    def test_an_empty_bag_bounces_back_to_the_bag(self, client, user):
        client.force_login(user)
        response = client.get(reverse("shop:checkout"), follow=True)
        assert response.redirect_chain[-1][0] == reverse("shop:cart")
        assert "Your bag is empty." in message_texts(response)

    def test_the_page_lists_only_your_own_addresses(
        self, client, user, other_user, address, product
    ):
        make_address(user=other_user)
        client.force_login(user)
        add_to_bag(client, product.variants.first())

        html = client.get(reverse("shop:checkout")).content.decode()
        assert "Ada Lovelace" in html
        assert "SW1A 1AA" in html
        assert "Mallory Attacker" not in html
        assert "N1 1AA" not in html

    def test_selecting_an_address_records_it(self, client, user, address, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first())

        response = client.post(reverse("shop:checkout-address"), {"address_id": address.pk})
        assert response.status_code == 302
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.shipping_address_id == address.pk

    def test_someone_elses_address_is_a_404_and_never_assigned(
        self, client, user, other_user, product
    ):
        foreign = make_address(user=other_user)

        client.force_login(user)
        add_to_bag(client, product.variants.first())

        response = client.post(reverse("shop:checkout-address"), {"address_id": foreign.pk})
        assert response.status_code == 404
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.shipping_address_id is None

    def test_a_posted_price_never_reaches_the_shipping_record(self, client, user, address, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first())
        client.post(reverse("shop:checkout-address"), {"address_id": address.pk})

        client.post(reverse("shop:checkout-shipping"), {"method": "express", "amount": "0.00"})
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.shipping_method_code == "express"

    def test_an_unknown_shipping_code_is_refused(self, client, user, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first())

        response = client.post(reverse("shop:checkout-shipping"), {"method": "drone"}, follow=True)
        assert "Unknown shipping method." in message_texts(response)
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.shipping_method_code == ""

    def test_validating_freezes_lines_address_and_totals(self, client, user, address, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first(), 2)
        client.post(reverse("shop:checkout-address"), {"address_id": address.pk})

        response = client.post(reverse("shop:checkout-validate"), follow=True)
        assert "Everything checks out" in message_texts(response)

        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.status == CheckoutSession.Status.VALIDATED
        assert checkout.is_validated
        assert checkout.validated_at is not None
        assert checkout.subtotal == Decimal("98.00")
        assert checkout.shipping_amount == Decimal("5.00")
        assert checkout.total == Decimal("103.00")

        snapshot = checkout.snapshot
        assert snapshot["cart_id"] == checkout.cart_id
        assert snapshot["currency"] == "USD"
        line = snapshot["lines"][0]
        assert line["sku"] == "FW-TEE-BLK-M"
        assert line["quantity"] == 2
        assert line["unit_price"] == "49.00"
        assert line["line_total"] == "98.00"
        assert snapshot["shipping"]["code"] == "standard"
        assert snapshot["shipping"]["amount"] == "5.00"
        assert snapshot["address"]["full_name"] == "Ada Lovelace"
        assert snapshot["address"]["postal_code"] == "SW1A 1AA"
        assert snapshot["total"] == "103.00"

    def test_express_totals_are_computed_server_side(self, client, user, address, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first())
        client.post(reverse("shop:checkout-address"), {"address_id": address.pk})
        client.post(reverse("shop:checkout-shipping"), {"method": "express", "amount": "0.00"})

        client.post(reverse("shop:checkout-validate"))
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.shipping_amount == Decimal("15.00")
        assert checkout.total == Decimal("64.00")

    def test_validation_uses_the_default_address_when_none_was_chosen(self, user, address, product):
        checkout = CheckoutSession.objects.create(user=user, cart=Cart.objects.create(user=user))
        CartItem.objects.create(
            cart=checkout.cart,
            variant=product.variants.first(),
            quantity=1,
            price_snapshot=Decimal("49.00"),
        )

        validate_checkout(checkout)
        checkout.refresh_from_db()
        assert checkout.status == CheckoutSession.Status.VALIDATED
        assert checkout.shipping_address_id == address.pk

    def test_validating_without_any_address_is_refused(self, client, user, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first())

        response = client.post(reverse("shop:checkout-validate"), follow=True)
        assert "Choose where we should deliver." in message_texts(response)
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.status == CheckoutSession.Status.OPEN

    def test_validating_with_an_unsellable_line_is_refused(self, client, user, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first())
        Product.objects.filter(pk=product.pk).update(status=Product.Status.DRAFT)

        response = client.post(reverse("shop:checkout-validate"), follow=True)
        assert "no longer available" in message_texts(response)
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.status == CheckoutSession.Status.OPEN
        assert checkout.snapshot == {}

    def test_editing_the_bag_after_validation_reopens_the_session(
        self, client, user, address, product
    ):
        client.force_login(user)
        add_to_bag(client, product.variants.first())
        client.post(reverse("shop:checkout-address"), {"address_id": address.pk})
        client.post(reverse("shop:checkout-validate"))

        cart = Cart.objects.get(user=user)
        update_cart_item_quantity(cart, cart.items.get().pk, 3)

        client.get(reverse("shop:checkout"))
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.status == CheckoutSession.Status.OPEN
        assert checkout.snapshot == {}
        assert checkout.validated_at is None

    def test_a_validated_session_renders_the_handoff_state(self, client, user, address, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first())
        client.post(reverse("shop:checkout-address"), {"address_id": address.pk})
        client.post(reverse("shop:checkout-validate"))

        html = client.get(reverse("shop:checkout")).content.decode()
        assert "Your details check out" in html
        # Phase 6 opened the handoff: the form carries the session id and offers the button.
        assert 'name="checkout_id"' in html
        assert "Place order" in html
        assert "Nothing has been charged and no order has been placed yet." in html

    def test_validation_refreshes_price_snapshots_before_freezing(
        self, client, user, address, product
    ):
        client.force_login(user)
        variant = product.variants.first()
        add_to_bag(client, variant)
        ProductVariant.objects.filter(pk=variant.pk).update(price=Decimal("55.00"))
        client.post(reverse("shop:checkout-address"), {"address_id": address.pk})

        client.post(reverse("shop:checkout-validate"))
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.subtotal == Decimal("55.00")
        assert checkout.total == Decimal("60.00")

        item = Cart.objects.get(user=user).items.get()
        assert item.price_snapshot == Decimal("55.00")

    def test_the_checkout_session_is_created_once_and_reused(self, client, user, product):
        client.force_login(user)
        add_to_bag(client, product.variants.first())
        client.get(reverse("shop:checkout"))
        client.get(reverse("shop:checkout"))
        assert CheckoutSession.objects.filter(user=user).count() == 1

    def test_validation_stops_at_the_phase_boundary(self, client, user, address, product):
        """Phase 6 owns orders: validation must leave the cart ACTIVE and untouched."""
        client.force_login(user)
        add_to_bag(client, product.variants.first())
        client.post(reverse("shop:checkout-address"), {"address_id": address.pk})
        client.post(reverse("shop:checkout-validate"))

        cart = Cart.objects.get(user=user)
        assert cart.status == Cart.Status.ACTIVE
        assert cart.items.count() == 1
        # One open-or-validated session, still bound to this cart.
        checkout = CheckoutSession.objects.get(user=user)
        assert checkout.cart_id == cart.pk

    def test_service_layer_refuses_a_foreign_address(self, user, other_user):
        foreign = make_address(user=other_user)
        cart = Cart.objects.create(user=user)
        checkout = CheckoutSession.objects.create(user=user, cart=cart)

        with pytest.raises(ValidationError, match="not yours"):
            set_shipping_address(checkout, foreign)

        checkout.refresh_from_db()
        assert checkout.shipping_address_id is None

    def test_service_layer_refuses_an_unknown_method(self, user, product):
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart,
            variant=product.variants.first(),
            quantity=1,
            price_snapshot=Decimal("49.00"),
        )
        checkout = CheckoutSession.objects.create(user=user, cart=cart)

        with pytest.raises(ValidationError, match="Unknown shipping method"):
            set_shipping_method(checkout, "drone")
