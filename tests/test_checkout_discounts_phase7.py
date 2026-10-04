"""Checkout discounts end to end: panels, validation, placement and stale guards over HTTP.

This is the layer where engagement meets Phase 5/6: codes and points are applied through the
checkout endpoints, frozen by validation, re-checked at placement, and every disagreement
lands as a customer-readable message rather than a wrong total. Money assertions go through
the frozen snapshot, so a rendering change cannot mask an arithmetic one.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.engagement.models import PointsReservation, PromotionUsage
from apps.orders.models import Order
from apps.shop.models import CheckoutSession
from tests.phase6_helpers import add_to_bag, message_texts, sign_in
from tests.phase7_helpers import (
    checkout_totals,
    grant_points,
    make_promotion,
)

pytestmark = pytest.mark.django_db


def _apply_promotion(client, code):
    return client.post(reverse("shop:checkout-promotion"), {"code": code})


def _apply_points(client, points):
    return client.post(reverse("shop:checkout-loyalty"), {"points": str(points)})


def _ready_checkout(client, user, product, *, code=None, points=0):
    """Signed in, bag filled, address chosen, discounts applied -- one step before validate."""
    from tests.phase6_helpers import make_address, seed_stock

    sign_in(client)
    variant = product.variants.first()
    seed_stock(variant, 10)
    add_to_bag(client, variant)
    address = user.addresses.order_by("pk").first()
    if address is None:
        address = make_address(user, is_default_shipping=True)
    client.post(reverse("shop:checkout-address"), {"address_id": address.pk})
    if code:
        _apply_promotion(client, code)
    if points:
        _apply_points(client, points)
    return CheckoutSession.objects.get(user=user)


# =============================================================================
# The open checkout (live estimates)
# =============================================================================


class TestOpenCheckout:
    def test_the_checkout_requires_sign_in(self, client, product):
        add_to_bag(client, product.variants.first())

        response = client.get(reverse("shop:checkout"))

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_the_summary_shows_the_estimated_discounts(self, client, user, product):
        make_promotion("TEN", percent=10)
        grant_points(user, 500)
        _ready_checkout(client, user, product, code="TEN", points=300)

        response = client.get(reverse("shop:checkout"))
        context = response.context

        assert context["promo_discount"] == Decimal("4.90")
        assert context["loyalty_discount"] == Decimal("3.00")
        assert context["total"] == Decimal("46.10")  # 49.00 - 4.90 - 3.00 + 5.00
        content = response.content.decode()
        assert "TEN" in content
        assert "46.10" in content

    def test_the_panel_exposes_the_loyalty_facts(self, client, user, product):
        grant_points(user, 500)
        _ready_checkout(client, user, product)

        response = client.get(reverse("shop:checkout"))

        assert response.context["loyalty_balance"] == 500
        assert response.context["redemption_message"].startswith("100 points")
        assert response.context["redeem_increment"] == 100

    def test_a_code_that_went_bad_blocks_the_validate_step(self, client, user, product):
        promotion = make_promotion("TEN", percent=10)
        _ready_checkout(client, user, product, code="TEN")
        promotion.ends_at = timezone.now() - timedelta(minutes=1)
        promotion.save(update_fields=["ends_at"])

        response = client.get(reverse("shop:checkout"))

        assert "That code has expired." in response.context["errors"]
        assert response.context["promo_discount"] == Decimal("0.00")


# =============================================================================
# Validation (the freeze)
# =============================================================================


class TestValidation:
    def test_validation_freezes_both_discounts_into_the_snapshot(self, client, user, product):
        make_promotion("TEN", percent=10)
        grant_points(user, 500)
        checkout = _ready_checkout(client, user, product, code="TEN", points=300)

        response = client.post(reverse("shop:checkout-validate"), follow=True)

        checkout.refresh_from_db()
        totals = checkout_totals(checkout)
        assert checkout.is_validated
        assert totals["promotion"] == Decimal("4.90")
        assert totals["loyalty"] == Decimal("3.00")
        assert totals["points"] == 300
        assert totals["total"] == totals["subtotal"] - Decimal("7.90") + totals["shipping"]
        assert "checks out" in message_texts(response)

    def test_validation_without_discounts_keeps_zeroed_keys(self, client, user, product):
        checkout = _ready_checkout(client, user, product)

        client.post(reverse("shop:checkout-validate"), follow=True)

        checkout.refresh_from_db()
        totals = checkout_totals(checkout)
        assert totals["promotion"] == Decimal("0.00")
        assert totals["loyalty"] == Decimal("0.00")
        assert totals["points"] == 0

    def test_the_validated_page_renders_the_discount_rows(self, client, user, product):
        make_promotion("TEN", percent=10)
        grant_points(user, 500)
        _ready_checkout(client, user, product, code="TEN", points=300)
        client.post(reverse("shop:checkout-validate"))

        response = client.get(reverse("shop:checkout"))
        content = response.content.decode()

        assert response.context["checkout"].is_validated
        assert "Promotion (TEN)" in content
        assert "FLASH Points (300)" in content
        assert "4.90" in content  # the frozen discount row
        assert "46.10" in content  # 49.00 - 7.90 + 5.00


# =============================================================================
# Placement (the re-check)
# =============================================================================


class TestPlacement:
    def test_a_discounted_checkout_places_a_discounted_order(self, client, user, product):
        make_promotion("TEN", percent=10)
        grant_points(user, 500)
        checkout = _ready_checkout(client, user, product, code="TEN", points=300)
        client.post(reverse("shop:checkout-validate"))

        response = client.post(reverse("shop:checkout-place"), {"checkout_id": checkout.pk})

        assert response.status_code == 302
        order = Order.objects.get()
        assert order.promotion_code == "TEN"
        assert order.loyalty_points == 300
        assert order.discount_amount == Decimal("7.90")
        assert order.total == Decimal("46.10")
        usage = PromotionUsage.objects.get()
        assert usage.order == order
        hold = PointsReservation.objects.get(order=order)
        assert hold.status == PointsReservation.Status.ACTIVE
        checkout.refresh_from_db()
        assert checkout.status == CheckoutSession.Status.CONVERTED

    def test_an_expired_code_between_validate_and_place_is_refused(self, client, user, product):
        promotion = make_promotion("TEN", percent=10)
        checkout = _ready_checkout(client, user, product, code="TEN")
        client.post(reverse("shop:checkout-validate"))
        promotion.ends_at = timezone.now() - timedelta(minutes=1)
        promotion.save(update_fields=["ends_at"])

        response = client.post(
            reverse("shop:checkout-place"),
            {"checkout_id": checkout.pk},
            follow=True,
        )

        assert Order.objects.count() == 0
        assert "review your order again" in message_texts(response)
        assert PromotionUsage.objects.count() == 0

    def test_points_released_between_validate_and_place_are_refused(self, client, user, product):
        grant_points(user, 500)
        checkout = _ready_checkout(client, user, product, points=300)
        client.post(reverse("shop:checkout-validate"))
        PointsReservation.objects.filter(checkout=checkout).update(
            status=PointsReservation.Status.RELEASED
        )

        response = client.post(
            reverse("shop:checkout-place"),
            {"checkout_id": checkout.pk},
            follow=True,
        )

        assert Order.objects.count() == 0
        assert "FLASH Points" in message_texts(response)

    def test_a_replayed_place_returns_the_same_order(self, client, user, product):
        checkout = _ready_checkout(client, user, product)
        client.post(reverse("shop:checkout-validate"))

        first = client.post(reverse("shop:checkout-place"), {"checkout_id": checkout.pk})
        second = client.post(reverse("shop:checkout-place"), {"checkout_id": checkout.pk})

        assert first["Location"] == second["Location"]
        assert Order.objects.count() == 1
