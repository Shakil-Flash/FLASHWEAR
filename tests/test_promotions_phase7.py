"""Promotions Phase 7: the engine's rules, consumption and every validation surface.

The engine is the only thing that decides whether a code applies, so it is tested head-on
(generic vs specific messages, rounding, caps), then again through the two surfaces that
persist its verdict: the checkout apply/remove endpoints and the handoff's consumption. The
anti-enumeration rule gets its own test: an unknown code and a malformed one must be
indistinguishable to the person typing them.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.engagement.models import Promotion, PromotionUsage
from apps.engagement.services import discounts, promotions
from apps.engagement.services.errors import PromotionError
from apps.orders.services import create_order_from_checkout
from tests.phase6_helpers import message_texts, open_checkout, placed_order
from tests.phase7_helpers import discounted_checkout, grant_points, make_promotion

pytestmark = pytest.mark.django_db


def _finding(code, user, subtotal="49.00"):
    with pytest.raises(PromotionError) as exc:
        promotions.find_for_checkout(code, user=user, subtotal=Decimal(subtotal))
    return exc.value


# =============================================================================
# Codes and eligibility
# =============================================================================


class TestCodeHandling:
    def test_codes_are_trimmed_and_upper_cased(self, user):
        promotion = make_promotion("SAVE10", percent=10)

        found = promotions.find_for_checkout("  save10 ", user=user, subtotal=Decimal("49.00"))

        assert found == promotion

    def test_a_malformed_code_gets_the_generic_message(self, user):
        error = _finding("a b c", user)

        assert error.message == promotions.GENERIC_INVALID_MESSAGE
        assert error.code == "not_found"

    def test_an_unknown_code_gets_the_same_generic_message(self, user):
        error = _finding("NOPE99", user)

        assert error.message == promotions.GENERIC_INVALID_MESSAGE

    def test_unknown_and_malformed_are_indistinguishable(self, user):
        unknown = _finding("GHOST99", user)
        malformed = _finding("!", user)
        empty = _finding("", user)

        assert unknown.message == malformed.message == empty.message

    def test_an_inactive_promotion_names_its_state(self, user):
        make_promotion("OFF10", percent=10, is_active=False)

        assert _finding("OFF10", user).code == "inactive"

    def test_a_future_window_has_not_started(self, user):
        now = timezone.now()
        make_promotion(
            "LATER10",
            percent=10,
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=2),
        )

        error = _finding("LATER10", user)
        assert error.code == "not_started"
        assert "not active yet" in error.message

    def test_an_expired_window_says_expired(self, user):
        now = timezone.now()
        make_promotion(
            "OLD10",
            percent=10,
            starts_at=now - timedelta(days=10),
            ends_at=now - timedelta(days=1),
        )

        assert _finding("OLD10", user).code == "expired"

    def test_a_minimum_order_shows_the_amount(self, user):
        make_promotion("BIGONLY", percent=10, min_order_amount=Decimal("100.00"))

        error = _finding("BIGONLY", user, subtotal="49.00")
        assert error.code == "minimum_not_met"
        assert "100.00" in error.message

    def test_a_spent_usage_limit_is_refused(self, user):
        make_promotion("LAST1", percent=10, usage_limit=1, used_count=1)

        assert _finding("LAST1", user).code == "limit_reached"

    def test_a_per_user_limit_counts_that_customers_usages(self, user, product):
        promotion = make_promotion("ONCE", percent=10, per_user_limit=1)
        order = placed_order(user, product.variants.first(), stock=10)
        promotions.consume_usage(promotion, user=user, order=order, discount=Decimal("4.90"))

        assert _finding("ONCE", user).code == "user_limit_reached"


# =============================================================================
# Discount math
# =============================================================================


class TestDiscountMath:
    def test_percentages_round_half_up(self):
        promotion = make_promotion("TEN", percent=10)

        assert promotions.calculate_discount(promotion, Decimal("49.95")) == Decimal("5.00")

    def test_a_fixed_discount_is_taken_as_is(self):
        promotion = make_promotion("FIVER", fixed=5)

        assert promotions.calculate_discount(promotion, Decimal("49.00")) == Decimal("5.00")

    def test_a_discount_never_exceeds_the_subtotal(self):
        promotion = make_promotion("TWENTY", fixed=20)

        assert promotions.calculate_discount(promotion, Decimal("15.00")) == Decimal("15.00")


# =============================================================================
# The stacking engine
# =============================================================================


class TestComputeDiscounts:
    def test_a_promotion_alone_reduces_the_subtotal(self, user):
        make_promotion("TEN", percent=10)

        breakdown = discounts.compute_discounts(
            user=user, subtotal=Decimal("49.00"), promotion_code="TEN"
        )

        assert breakdown.promotion_discount == Decimal("4.90")
        assert breakdown.loyalty_discount == Decimal("0.00")
        assert breakdown.total_after_discount == Decimal("44.10")

    def test_points_stack_on_what_the_promotion_left(self, user):
        make_promotion("TEN", percent=10)
        grant_points(user, 500)

        breakdown = discounts.compute_discounts(
            user=user,
            subtotal=Decimal("49.00"),
            promotion_code="TEN",
            loyalty_points=200,
        )

        assert breakdown.promotion_discount == Decimal("4.90")
        assert breakdown.loyalty_discount == Decimal("2.00")
        assert breakdown.total_discount == Decimal("6.90")

    def test_a_promotion_can_forbid_points_redemption(self, user):
        make_promotion("HARDNO", percent=10, allow_loyalty_redemption=False)
        grant_points(user, 500)

        with pytest.raises(Exception) as exc:
            discounts.compute_discounts(
                user=user,
                subtotal=Decimal("49.00"),
                promotion_code="HARDNO",
                loyalty_points=100,
            )

        assert getattr(exc.value, "code", None) == "not_combinable"

    def test_an_empty_code_is_just_the_subtotal(self, user):
        breakdown = discounts.compute_discounts(user=user, subtotal=Decimal("49.00"))

        assert breakdown.promotion_code == ""
        assert breakdown.total_discount == Decimal("0.00")

    def test_the_percent_cap_applies_even_with_an_huge_balance(self, user):
        grant_points(user, 100_000)

        with pytest.raises(Exception) as exc:
            discounts.compute_discounts(user=user, subtotal=Decimal("49.00"), loyalty_points=3_000)

        # 50% of 49.00 is 24.50, so 30.00 of points is refused at the engine, not in a template.
        assert getattr(exc.value, "code", None) == "over_limit"

    def test_points_need_the_configured_minimum_after_discounts(self, user, settings):
        settings.LOYALTY_MIN_ORDER_AMOUNT = Decimal("40.00")
        make_promotion("HALF", percent=90)
        grant_points(user, 500)

        with pytest.raises(Exception) as exc:
            discounts.compute_discounts(
                user=user,
                subtotal=Decimal("49.00"),
                promotion_code="HALF",
                loyalty_points=100,
            )

        assert getattr(exc.value, "code", None) == "minimum_not_met"

    def test_validation_writes_nothing(self, user):
        promotion = make_promotion("TEN", percent=10)

        discounts.compute_discounts(user=user, subtotal=Decimal("49.00"), promotion_code="TEN")

        promotion.refresh_from_db()
        assert promotion.used_count == 0
        assert not PromotionUsage.objects.exists()


# =============================================================================
# Consumption (the handoff's side)
# =============================================================================


class TestConsumption:
    def test_consuming_increments_and_records(self, user, product):
        promotion = make_promotion("TEN", percent=10)
        order = placed_order(user, product.variants.first(), stock=10)

        usage = promotions.consume_usage(
            promotion, user=user, order=order, discount=Decimal("4.90")
        )

        promotion.refresh_from_db()
        assert promotion.used_count == 1
        assert usage.discount_amount == Decimal("4.90")
        assert usage.order == order

    def test_replaying_the_same_order_does_not_double_count(self, user, product):
        promotion = make_promotion("TEN", percent=10)
        order = placed_order(user, product.variants.first(), stock=10)

        first = promotions.consume_usage(
            promotion, user=user, order=order, discount=Decimal("4.90")
        )
        second = promotions.consume_usage(
            promotion, user=user, order=order, discount=Decimal("4.90")
        )

        promotion.refresh_from_db()
        assert second.pk == first.pk
        assert promotion.used_count == 1

    def test_the_last_use_cannot_be_taken_twice(self, user, other_user, product):
        make_promotion("LAST1", percent=10, usage_limit=1)
        first_order = placed_order(user, product.variants.first(), stock=10)
        second_order = placed_order(other_user, product.variants.first(), stock=None)
        promotion = Promotion.objects.get(code="LAST1")

        promotions.consume_usage(promotion, user=user, order=first_order, discount=Decimal("4.90"))

        with pytest.raises(PromotionError) as exc:
            promotions.consume_usage(
                promotion,
                user=other_user,
                order=second_order,
                discount=Decimal("4.90"),
            )

        assert exc.value.code == "limit_reached"
        promotion.refresh_from_db()
        assert promotion.used_count == 1

    def test_a_per_user_limit_stops_a_second_order(self, user, product):
        promotion = make_promotion("ONCE", percent=10, per_user_limit=1)
        first = placed_order(user, product.variants.first(), stock=10)
        second = placed_order(user, product.variants.first(), stock=None)

        promotions.consume_usage(promotion, user=user, order=first, discount=Decimal("4.90"))

        with pytest.raises(PromotionError) as exc:
            promotions.consume_usage(promotion, user=user, order=second, discount=Decimal("4.90"))

        assert exc.value.code == "user_limit_reached"
        promotion.refresh_from_db()
        assert promotion.used_count == 1

    def test_the_database_backstop_rejects_an_oversold_counter(self):
        promotion = make_promotion("GUARDED", percent=10, usage_limit=1, used_count=1)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                promotion.used_count = 2
                promotion.save(update_fields=["used_count"])


# =============================================================================
# The checkout apply/remove endpoints
# =============================================================================


class TestCheckoutPromotionEndpoint:
    def test_sign_in_is_required(self, client):
        response = client.post(reverse("shop:checkout-promotion"), {"code": "TEN"})

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_only_post_is_allowed(self, client, user, product):
        client.force_login(user)
        open_checkout(user, variant=product.variants.first())

        response = client.get(reverse("shop:checkout-promotion"))

        assert response.status_code == 405

    def test_the_normalised_code_lands_on_the_checkout(self, client, user, product):
        make_promotion("TEN", percent=10)
        client.force_login(user)
        checkout = open_checkout(user, variant=product.variants.first())

        client.post(reverse("shop:checkout-promotion"), {"code": " ten "})

        checkout.refresh_from_db()
        assert checkout.promotion_code == "TEN"

    def test_an_invalid_code_never_becomes_state(self, client, user, product):
        client.force_login(user)
        checkout = open_checkout(user, variant=product.variants.first())

        response = client.post(reverse("shop:checkout-promotion"), {"code": "GHOST99"}, follow=True)

        checkout.refresh_from_db()
        assert checkout.promotion_code == ""
        assert promotions.GENERIC_INVALID_MESSAGE in message_texts(response)

    def test_an_empty_code_removes_the_promotion(self, client, user, product):
        make_promotion("TEN", percent=10)
        client.force_login(user)
        checkout = open_checkout(user, variant=product.variants.first())
        client.post(reverse("shop:checkout-promotion"), {"code": "TEN"})

        response = client.post(reverse("shop:checkout-promotion"), {"code": ""}, follow=True)

        checkout.refresh_from_db()
        assert checkout.promotion_code == ""
        assert "removed" in message_texts(response).lower()

    def test_applying_twice_replaces_the_code(self, client, user, product):
        make_promotion("TEN", percent=10)
        make_promotion("FIVER", fixed=5)
        client.force_login(user)
        checkout = open_checkout(user, variant=product.variants.first())
        client.post(reverse("shop:checkout-promotion"), {"code": "TEN"})

        client.post(reverse("shop:checkout-promotion"), {"code": "FIVER"})

        checkout.refresh_from_db()
        assert checkout.promotion_code == "FIVER"


class TestCheckoutLoyaltyEndpoint:
    def test_sign_in_is_required(self, client, product):
        response = client.post(reverse("shop:checkout-loyalty"), {"points": "100"})

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_points_are_pinned_with_a_hold(self, client, user, product):
        from apps.engagement.services import loyalty as loyalty_services

        grant_points(user, 500)
        client.force_login(user)
        checkout = open_checkout(user, variant=product.variants.first())

        response = client.post(reverse("shop:checkout-loyalty"), {"points": "300"}, follow=True)

        checkout.refresh_from_db()
        assert checkout.loyalty_points == 300
        assert loyalty_services.held_for(user) == 300
        assert "300" in message_texts(response)

    def test_more_points_than_the_balance_is_refused(self, client, user, product):
        grant_points(user, 100)
        client.force_login(user)
        checkout = open_checkout(user, variant=product.variants.first())

        response = client.post(reverse("shop:checkout-loyalty"), {"points": "500"}, follow=True)

        checkout.refresh_from_db()
        assert checkout.loyalty_points == 0
        assert "that many FLASH Points" in message_texts(response)

    def test_removal_releases_the_hold(self, client, user, product):
        from apps.engagement.services import loyalty as loyalty_services

        grant_points(user, 500)
        client.force_login(user)
        checkout = open_checkout(user, variant=product.variants.first())
        client.post(reverse("shop:checkout-loyalty"), {"points": "300"})

        response = client.post(reverse("shop:checkout-loyalty"), {"remove": "1"}, follow=True)

        checkout.refresh_from_db()
        assert checkout.loyalty_points == 0
        assert loyalty_services.held_for(user) == 0
        assert "removed" in message_texts(response).lower()

    def test_an_off_grid_amount_is_refused(self, client, user, product):
        grant_points(user, 500)
        client.force_login(user)
        checkout = open_checkout(user, variant=product.variants.first())

        client.post(reverse("shop:checkout-loyalty"), {"points": "150"}, follow=True)

        checkout.refresh_from_db()
        assert checkout.loyalty_points == 0


# =============================================================================
# The preview API
# =============================================================================


PROMOTION_API = "/api/v1/checkout/promotion/"


class TestPromotionValidateAPI:
    def test_sign_in_is_required(self, api_client):
        response = api_client.post(PROMOTION_API, {"code": "TEN", "subtotal": "49.00"})

        assert response.status_code == 403

    def test_a_valid_code_previews_the_discount(self, api_client, user):
        make_promotion("TEN", percent=10)
        api_client.force_authenticate(user=user)

        response = api_client.post(PROMOTION_API, {"code": "TEN", "subtotal": "49.00"})

        assert response.status_code == 200
        assert response.data == {
            "valid": True,
            "code": "TEN",
            "discount": "4.90",
            "display": "10% off",
        }

    def test_an_unknown_code_is_a_400_with_the_generic_detail(self, api_client, user):
        api_client.force_authenticate(user=user)

        response = api_client.post(PROMOTION_API, {"code": "GHOST99", "subtotal": "49.00"})

        assert response.status_code == 400
        assert response.data == {
            "valid": False,
            "detail": promotions.GENERIC_INVALID_MESSAGE,
            "code": "not_found",
        }

    def test_an_expired_code_reports_its_state(self, api_client, user):
        now = timezone.now()
        make_promotion(
            "OLD10",
            percent=10,
            starts_at=now - timedelta(days=10),
            ends_at=now - timedelta(days=1),
        )
        api_client.force_authenticate(user=user)

        response = api_client.post(PROMOTION_API, {"code": "OLD10", "subtotal": "49.00"})

        assert response.status_code == 400
        assert response.data["code"] == "expired"

    def test_a_negative_subtotal_is_a_validation_error(self, api_client, user):
        api_client.force_authenticate(user=user)

        response = api_client.post(PROMOTION_API, {"code": "TEN", "subtotal": "-1.00"})

        assert response.status_code == 400

    def test_the_preview_persists_nothing(self, api_client, user):
        promotion = make_promotion("TEN", percent=10)
        api_client.force_authenticate(user=user)

        api_client.post(PROMOTION_API, {"code": "TEN", "subtotal": "49.00"})

        promotion.refresh_from_db()
        assert promotion.used_count == 0
        assert not PromotionUsage.objects.exists()


# =============================================================================
# End to end: a discounted order through the real handoff
# =============================================================================


class TestPromotionThroughTheHandoff:
    def test_the_order_records_the_code_discount_and_usage(self, user, product):
        make_promotion("TEN", percent=10)
        checkout = discounted_checkout(user, product.variants.first(), code="TEN")

        order = create_order_from_checkout(checkout)

        assert order.promotion_code == "TEN"
        assert order.discount_amount == Decimal("4.90")
        assert order.total == Decimal("44.10") + Decimal("5.00")  # + standard shipping
        usage = PromotionUsage.objects.get()
        assert usage.order == order
        Promotion.objects.get(code="TEN").refresh_from_db()
        assert Promotion.objects.get(code="TEN").used_count == 1

    def test_replaying_the_handoff_does_not_double_charge_the_code(self, user, product):
        make_promotion("TEN", percent=10)
        checkout = discounted_checkout(user, product.variants.first(), code="TEN")

        first = create_order_from_checkout(checkout)
        second = create_order_from_checkout(checkout)

        assert second.pk == first.pk
        assert PromotionUsage.objects.count() == 1
        assert Promotion.objects.get(code="TEN").used_count == 1


# =============================================================================
# Admin form: the friendly validation layer
# =============================================================================


class TestPromotionAdminForm:
    def _data(self, **overrides):
        from apps.engagement.admin import PromotionAdminForm

        data = {
            "code": "TEN",
            "name": "Ten percent off",
            "description": "",
            "discount_type": Promotion.DiscountType.PERCENTAGE,
            "discount_value": "10",
            "starts_at": "2026-01-01 00:00:00",
            "ends_at": "2026-02-01 00:00:00",
            "is_active": "on",
            "min_order_amount": "0.00",
            "allow_loyalty_redemption": "on",
            # readonly in the admin, so its "required" never bites there
            "used_count": "0",
        }
        data.update(overrides)
        return PromotionAdminForm(data=data)

    def test_a_complete_campaign_is_valid(self):
        form = self._data()

        assert form.is_valid()
        assert form.cleaned_data["discount_value"] == Decimal("10")

    def test_an_inverted_window_is_refused(self):
        form = self._data(starts_at="2026-02-01 00:00:00", ends_at="2026-01-01 00:00:00")

        assert not form.is_valid()
        assert "The end of the window must be after its start." in form.non_field_errors()

    def test_a_percentage_above_one_hundred_is_refused(self):
        form = self._data(discount_value="150")

        assert not form.is_valid()
        assert "A percentage discount cannot exceed 100%." in form.non_field_errors()

    def test_a_non_positive_discount_is_refused(self):
        form = self._data(discount_value="0")

        assert not form.is_valid()
        assert "The discount must be greater than zero." in form.non_field_errors()

    def test_a_duplicate_code_is_refused_even_with_different_casing(self):
        make_promotion("TEN", percent=10)
        form = self._data(code="ten")

        assert not form.is_valid()
        assert "That code already exists." in form.errors["code"]

    def test_the_code_is_stored_normalised(self):
        form = self._data(code="  flash-wed  ")

        assert form.is_valid()
        assert form.cleaned_data["code"] == "FLASH-WED"
