"""FLASH Points Phase 7: earning, redemption holds, the handoff and the sweepers.

Earning is driven by the real payment webhook (``place_and_pay``), redemption through the
real two-phase lifecycle (reserve -> attach -> consume/release), and the stale guards at
handoff are tested by moving the world *between* validation and placement -- exactly the
window those guards exist for. The racing test is structural: ``select_for_update`` semantics
are only observable on PostgreSQL, so it skips (and is reported as unverified) elsewhere.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.engagement.models import PointsReservation, PointsTransaction
from apps.engagement.services import loyalty
from apps.engagement.services.errors import LoyaltyError
from apps.orders.models import Order
from apps.orders.services import StaleCheckout, cancel_order, create_order_from_checkout
from tests.phase6_helpers import (
    message_texts,
    open_checkout,
    place_and_pay,
    placed_order,
)
from tests.phase7_helpers import (
    checkout_totals,
    discounted_checkout,
    grant_points,
    make_promotion,
    pay_order,
)

pytestmark = pytest.mark.django_db


# =============================================================================
# Earning (the payment webhook is the only writer)
# =============================================================================


class TestEarning:
    def test_a_paid_order_awards_points_on_the_subtotal(self, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)

        entry = PointsTransaction.objects.get()
        assert entry.amount == 490  # 49.00 x LOYALTY_EARN_RATE (10)
        assert entry.transaction_type == PointsTransaction.TransactionType.PURCHASE_EARN
        assert entry.reference == f"order:{order.number}"
        assert entry.order == order
        assert entry.expires_at > timezone.now() + timedelta(days=360)
        assert loyalty.balance_for(user) == 490

    def test_awarding_is_idempotent(self, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)

        loyalty.earn_points_for_order(order)
        loyalty.earn_points_for_order(order)

        assert PointsTransaction.objects.count() == 1
        assert loyalty.balance_for(user) == 490

    def test_an_unpaid_order_awards_nothing(self, user, product):
        placed_order(user, product.variants.first(), stock=10)

        assert (
            PointsTransaction.objects.filter(
                transaction_type=PointsTransaction.TransactionType.PURCHASE_EARN
            ).exists()
            is False
        )
        assert loyalty.balance_for(user) == 0

    def test_points_are_earned_on_the_discounted_subtotal(self, user, product):
        make_promotion("TENOFF", percent=10)
        checkout = discounted_checkout(user, product.variants.first(), code="TENOFF")
        order = create_order_from_checkout(checkout)

        pay_order(order)

        entry = PointsTransaction.objects.get(
            transaction_type=PointsTransaction.TransactionType.PURCHASE_EARN
        )
        assert entry.amount == 441  # (49.00 - 4.90) x 10, truncated to whole points

    def test_a_fully_covered_order_earns_nothing(self, user, product):
        make_promotion("FREEBIE", fixed=49.00)
        checkout = discounted_checkout(user, product.variants.first(), code="FREEBIE")
        order = create_order_from_checkout(checkout)

        pay_order(order)

        assert not PointsTransaction.objects.filter(
            transaction_type=PointsTransaction.TransactionType.PURCHASE_EARN
        ).exists()
        assert loyalty.balance_for(user) == 0

    def test_a_reversal_offsets_the_earn_exactly_once(self, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)

        first = loyalty.reverse_earn_for_order(order)
        second = loyalty.reverse_earn_for_order(order)

        assert first is not None and first.amount == -490
        assert second.pk == first.pk
        assert loyalty.balance_for(user) == 0


# =============================================================================
# Redemption math and validation
# =============================================================================


class TestRedemptionRules:
    @pytest.mark.parametrize(
        ("points", "expected"),
        [
            (100, Decimal("1.00")),
            (150, Decimal("1.50")),
            (0, Decimal("0.00")),
            (333, Decimal("3.33")),
        ],
    )
    def test_points_convert_to_currency(self, points, expected):
        assert loyalty.redeem_discount(points) == expected

    @pytest.mark.parametrize("points", [0, -100])
    def test_zero_or_negative_points_are_refused(self, points):
        with pytest.raises(LoyaltyError) as exc:
            loyalty.validate_redemption(_userless(), points, eligible_subtotal=Decimal("100.00"))

        assert exc.value.code == "invalid"

    def test_points_must_land_on_the_increment_grid(self):
        with pytest.raises(LoyaltyError) as exc:
            loyalty.validate_redemption(_userless(), 150, eligible_subtotal=Decimal("100.00"))

        assert exc.value.code == "increment"
        assert "multiples of 100" in exc.value.message

    def test_more_points_than_the_balance_is_refused(self, user):
        grant_points(user, 200)

        with pytest.raises(LoyaltyError) as exc:
            loyalty.validate_redemption(user, 300, eligible_subtotal=Decimal("100.00"))

        assert exc.value.code == "insufficient"

    def test_the_percent_cap_refuses_even_an_affordable_balance(self, user):
        grant_points(user, 10_000)

        with pytest.raises(LoyaltyError) as exc:
            loyalty.validate_redemption(user, 5_100, eligible_subtotal=Decimal("100.00"))

        assert exc.value.code == "over_limit"

    def test_max_redeemable_floors_to_the_increment(self, user):
        grant_points(user, 1_050)

        maximum = loyalty.max_redeemable_points(user, eligible_subtotal=Decimal("100.00"))

        assert maximum == 1_000  # balance 1050 floors to 1000; the 50-cap allows 5000

    def test_a_minimum_order_amount_returns_zero_below_the_floor(self, user, settings):
        settings.LOYALTY_MIN_ORDER_AMOUNT = Decimal("50.00")
        grant_points(user, 5_000)

        maximum = loyalty.max_redeemable_points(user, eligible_subtotal=Decimal("40.00"))

        assert maximum == 0


# =============================================================================
# Reservations (the hold)
# =============================================================================


class TestReservations:
    def test_a_hold_pins_points_and_the_balance_drops(self, user, product):
        grant_points(user, 500)
        checkout = open_checkout(user, variant=product.variants.first())

        loyalty.reserve_for_checkout(checkout, 300)

        assert loyalty.balance_for(user) == 200
        assert loyalty.held_for(user) == 300
        hold = PointsReservation.objects.get()
        assert hold.checkout == checkout
        assert hold.status == PointsReservation.Status.ACTIVE

    def test_reapplying_replaces_the_hold(self, user, product):
        grant_points(user, 500)
        checkout = open_checkout(user, variant=product.variants.first())
        loyalty.reserve_for_checkout(checkout, 300)

        loyalty.reserve_for_checkout(checkout, 100)

        assert loyalty.held_for(user) == 100
        assert loyalty.balance_for(user) == 400
        assert PointsReservation.objects.filter(status=PointsReservation.Status.ACTIVE).count() == 1

    def test_an_impossible_reapply_fails_and_keeps_the_old_hold(self, user, product):
        grant_points(user, 500)
        checkout = open_checkout(user, variant=product.variants.first())
        loyalty.reserve_for_checkout(checkout, 100)

        with pytest.raises(LoyaltyError) as exc:
            loyalty.reserve_for_checkout(checkout, 9_999)

        assert exc.value.code == "insufficient"
        assert loyalty.held_for(user) == 100

    def test_releasing_returns_the_points(self, user, product):
        grant_points(user, 500)
        checkout = open_checkout(user, variant=product.variants.first())
        loyalty.reserve_for_checkout(checkout, 300)

        loyalty.reserve_for_checkout(checkout, 0)

        assert loyalty.balance_for(user) == 500
        assert loyalty.held_for(user) == 0
        assert PointsReservation.objects.get().status == PointsReservation.Status.RELEASED

    def test_a_checkout_sees_its_own_hold_as_free_money(self, user, product):
        grant_points(user, 500)
        checkout = open_checkout(user, variant=product.variants.first())
        loyalty.reserve_for_checkout(checkout, 500)

        assert loyalty.balance_for(user) == 0
        assert loyalty.balance_for(user, excluding_checkout=checkout) == 500


# =============================================================================
# The full lifecycle: validate -> place -> pay / cancel
# =============================================================================


class TestLifecycle:
    def test_validation_freezes_the_points_in_the_snapshot(self, user, product):
        grant_points(user, 500)
        checkout = discounted_checkout(user, product.variants.first(), points=300)

        totals = checkout_totals(checkout)

        assert totals["points"] == 300
        assert totals["loyalty"] == Decimal("3.00")
        assert totals["total"] == totals["subtotal"] - totals["loyalty"] + totals["shipping"]
        assert checkout.loyalty_points == 300
        assert loyalty.held_for(user) == 300

    def test_the_order_carries_the_points_and_discount(self, user, product):
        grant_points(user, 500)
        checkout = discounted_checkout(user, product.variants.first(), points=300)

        order = create_order_from_checkout(checkout)

        assert order.loyalty_points == 300
        assert order.discount_amount == Decimal("3.00")
        hold = PointsReservation.objects.get(order=order)
        assert hold.status == PointsReservation.Status.ACTIVE

    def test_payment_consumes_the_hold_into_a_negative_ledger_row(self, user, product):
        grant_points(user, 500)
        checkout = discounted_checkout(user, product.variants.first(), points=300)
        order = create_order_from_checkout(checkout)

        pay_order(order)

        hold = PointsReservation.objects.get(order=order)
        assert hold.status == PointsReservation.Status.CONSUMED
        redemption = PointsTransaction.objects.get(
            transaction_type=PointsTransaction.TransactionType.REDEMPTION
        )
        assert redemption.amount == -300
        assert redemption.reference == f"order:{order.number}"
        # 500 granted - 300 redeemed + 460 earned on the 46.00 payable after discount.
        assert loyalty.balance_for(user) == 660

    def test_cancelling_an_unpaid_order_hands_the_points_back(self, user, product):
        grant_points(user, 500)
        checkout = discounted_checkout(user, product.variants.first(), points=300)
        order = create_order_from_checkout(checkout)

        cancel_order(order)

        hold = PointsReservation.objects.get(order=order)
        assert hold.status == PointsReservation.Status.RELEASED
        assert loyalty.balance_for(user) == 500
        assert not PointsTransaction.objects.filter(
            transaction_type=PointsTransaction.TransactionType.REDEMPTION
        ).exists()


# =============================================================================
# Stale guards (the world moves between validation and placement)
# =============================================================================


class TestStaleGuards:
    def test_released_points_block_the_handoff(self, user, product):
        grant_points(user, 500)
        checkout = discounted_checkout(user, product.variants.first(), points=300)
        PointsReservation.objects.filter(checkout=checkout).update(
            status=PointsReservation.Status.RELEASED
        )

        with pytest.raises(StaleCheckout) as exc:
            create_order_from_checkout(checkout)

        assert "FLASH Points" in str(exc.value)
        assert not PointsReservation.objects.filter(order__isnull=False).exists()
        assert not Order.objects.filter(checkout=checkout).exists()  # atomic rollback

    def test_an_expired_promotion_blocks_the_handoff(self, user, product):
        promotion = make_promotion("FLASH5", percent=5)
        checkout = discounted_checkout(user, product.variants.first(), code="FLASH5")
        promotion.ends_at = timezone.now() - timedelta(minutes=1)
        promotion.save(update_fields=["ends_at"])

        with pytest.raises(StaleCheckout) as exc:
            create_order_from_checkout(checkout)

        assert "expired" in str(exc.value).lower()


# =============================================================================
# Sweepers and the dashboard
# =============================================================================


class TestSweepers:
    def test_an_abandoned_hold_expires_and_its_points_return(self, user, product):
        grant_points(user, 500)
        checkout = open_checkout(user, variant=product.variants.first())
        hold = loyalty.reserve_for_checkout(checkout, 300)
        PointsReservation.objects.filter(pk=hold.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        released = loyalty.sweep_expired_reservations()

        assert released == 1
        hold.refresh_from_db()
        assert hold.status == PointsReservation.Status.EXPIRED
        assert loyalty.balance_for(user) == 500

    def test_a_hold_attached_to_an_order_is_never_swept(self, user, product):
        grant_points(user, 500)
        checkout = discounted_checkout(user, product.variants.first(), points=300)
        order = create_order_from_checkout(checkout)
        PointsReservation.objects.filter(order=order).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        released = loyalty.sweep_expired_reservations()

        assert released == 0
        hold = PointsReservation.objects.get(order=order)
        assert hold.status == PointsReservation.Status.ACTIVE

    def test_due_earnings_expire_with_an_offsetting_row(self, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        earn = PointsTransaction.objects.get()
        PointsTransaction.objects.filter(pk=earn.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )

        expired = loyalty.expire_due_points()

        assert expired == 1
        expiration = PointsTransaction.objects.get(
            transaction_type=PointsTransaction.TransactionType.EXPIRATION
        )
        assert expiration.amount == -earn.amount
        assert expiration.reference == f"expiry:{earn.pk}"
        assert loyalty.balance_for(user) == 0

    def test_expiry_is_idempotent(self, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        PointsTransaction.objects.update(expires_at=timezone.now() - timedelta(days=1))

        first = loyalty.expire_due_points()
        second = loyalty.expire_due_points()

        assert (first, second) == (1, 0)
        assert (
            PointsTransaction.objects.filter(
                transaction_type=PointsTransaction.TransactionType.EXPIRATION
            ).count()
            == 1
        )

    def test_expiry_defers_while_the_customer_holds_a_checkout(self, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        PointsTransaction.objects.update(expires_at=timezone.now() - timedelta(days=1))
        loyalty.reserve_for_checkout(open_checkout(user, variant=product.variants.first()), 100)

        expired = loyalty.expire_due_points()

        assert expired == 0
        assert not PointsTransaction.objects.filter(
            transaction_type=PointsTransaction.TransactionType.EXPIRATION
        ).exists()
        assert loyalty.balance_for(user) == 390  # ledger intact (490), minus the 100 held

    def test_the_celery_task_reports_both_counts(self, user, product):
        from apps.engagement.tasks import sweep_loyalty

        place_and_pay(user, product.variants.first(), stock=10)
        PointsTransaction.objects.update(expires_at=timezone.now() - timedelta(days=1))

        counts = sweep_loyalty()

        assert counts == {"released": 0, "expired": 1}


class TestDashboard:
    def test_the_dashboard_requires_sign_in(self, client):
        response = client.get(reverse("account:loyalty"))

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_it_shows_the_balance_and_the_ledger(self, client, user, product):
        grant_points(user, 500)
        place_and_pay(user, product.variants.first(), stock=10)
        client.force_login(user)

        response = client.get(reverse("account:loyalty"))

        assert response.status_code == 200
        content = response.content.decode()
        assert "990" in content  # 500 granted + 490 earned
        assert "Expiring soon" in content
        assert response.context["balance"] == 990
        assert len(response.context["transactions"]) == 2

    def test_an_empty_ledger_says_so(self, client, user):
        client.force_login(user)

        response = client.get(reverse("account:loyalty"))

        assert "No point movements yet" in response.content.decode()
        assert response.context["balance"] == 0

    def test_the_history_paginates(self, client, user):
        for index in range(20):
            loyalty.adjust_balance(user, 100, note=f"Grant {index}")
        client.force_login(user)

        page_two = client.get(reverse("account:loyalty"), {"page": 2})

        assert page_two.context["transactions"].number == 2
        assert len(page_two.context["transactions"]) == 5  # 15 per page, 20 rows


class TestAdminAdjustment:
    def test_a_zero_adjustment_is_refused(self, user):
        with pytest.raises(LoyaltyError) as exc:
            loyalty.adjust_balance(user, 0)

        assert exc.value.code == "invalid"

    def test_an_adjustment_is_one_ledger_row(self, user):
        entry = loyalty.adjust_balance(user, -50, note="Goodwill credit clawback")

        assert entry.amount == -50
        assert entry.transaction_type == PointsTransaction.TransactionType.ADMIN_ADJUSTMENT
        assert loyalty.balance_for(user) == -50


# =============================================================================
# Concurrency (PostgreSQL only)
# =============================================================================


class TestConcurrencyContract:
    @pytest.mark.django_db(transaction=True)
    def test_only_one_racing_checkout_can_hold_the_same_points(self, db, user, product):
        if connection.vendor != "postgresql":
            pytest.skip("select_for_update semantics are only observable on PostgreSQL")

        import threading

        from django.db import connections

        grant_points(user, 500)
        variant = product.variants.first()
        first = open_checkout(user, variant=variant)
        second = open_checkout(user, variant=variant)

        results: list[str] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2, timeout=15)

        def attempt(checkout):
            try:
                barrier.wait()
                loyalty.reserve_for_checkout(checkout, 500)
            except LoyaltyError:
                results.append("refused")
            except Exception as exc:  # pragma: no cover - re-raised below
                errors.append(exc)
            else:
                results.append("held")
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=attempt, args=(checkout,)) for checkout in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        assert sorted(results) == ["held", "refused"]
        assert loyalty.held_for(user) == 500


def _userless():
    """A detached user stand-in for pure-math validation (no row is ever read)."""
    from django.contrib.auth import get_user_model

    return get_user_model()(pk=999_999, email="nobody@flashwear.test")


# =============================================================================
# Admin: the read-only ledger, the adjust action and hold release
# =============================================================================


class TestPointsAdmin:
    def _changelist(self, client, admin_user):
        from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME

        client.force_login(admin_user)
        return reverse("admin:engagement_pointstransaction_changelist"), ACTION_CHECKBOX_NAME

    def test_the_ledger_has_no_add_change_or_delete(self, client, admin_user, user):
        grant_points(user, 100)
        entry = PointsTransaction.objects.get()
        changelist, _ = self._changelist(client, admin_user)

        add_page = client.get(f"{changelist}add/")
        change_page = client.get(f"{changelist}{entry.pk}/change/")
        delete_page = client.post(f"{changelist}{entry.pk}/delete/")

        assert add_page.status_code == 403
        # Change stays viewable as evidence but nothing is editable: every field is
        # readonly and the save button never renders.
        assert change_page.status_code == 200
        assert 'name="_save"' not in change_page.content.decode()
        assert delete_page.status_code == 403

    def test_the_adjust_action_walks_through_the_intermediate_form(self, client, admin_user, user):
        grant_points(user, 100)
        entry = PointsTransaction.objects.get()
        changelist, checkbox = self._changelist(client, admin_user)
        selected = {checkbox: [str(entry.pk)], "action": "adjust_selected_users"}

        intermediate = client.post(changelist, selected)

        assert intermediate.status_code == 200
        assert "Adjust FLASH Points" in intermediate.content.decode()

        applied = client.post(
            changelist, {**selected, "apply": "1", "amount": "250", "note": "Goodwill"}
        )

        assert applied.status_code == 302
        assert loyalty.balance_for(user) == 350
        assert PointsTransaction.objects.count() == 2
        assert PointsTransaction.objects.filter(
            transaction_type=PointsTransaction.TransactionType.ADMIN_ADJUSTMENT,
            amount=250,
            note="Goodwill",
        ).exists()

    def test_a_zero_adjustment_is_refused_by_the_service(self, client, admin_user, user):
        grant_points(user, 100)
        entry = PointsTransaction.objects.get()
        changelist, checkbox = self._changelist(client, admin_user)
        selected = {checkbox: [str(entry.pk)], "action": "adjust_selected_users"}

        response = client.post(
            changelist,
            {**selected, "apply": "1", "amount": "0", "note": "nothing"},
            follow=True,
        )

        assert PointsTransaction.objects.count() == 1
        assert loyalty.balance_for(user) == 100
        assert "non-zero" in message_texts(response)

    def test_the_release_action_returns_held_points(self, client, admin_user, user, product):
        from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME

        grant_points(user, 500)
        checkout = open_checkout(user, variant=product.variants.first())
        hold = loyalty.reserve_for_checkout(checkout, 300)
        client.force_login(admin_user)

        response = client.post(
            reverse("admin:engagement_pointsreservation_changelist"),
            {
                ACTION_CHECKBOX_NAME: [str(hold.pk)],
                "action": "release_selected",
            },
        )

        assert response.status_code == 302
        hold.refresh_from_db()
        assert hold.status == PointsReservation.Status.RELEASED
        assert loyalty.balance_for(user) == 500
