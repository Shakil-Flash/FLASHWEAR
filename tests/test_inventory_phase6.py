"""Inventory Phase 6: counters, holds, the ledger, the sweeper, the admin flow.

Concurrency: the lock contract (stock rows locked last, ascending variant id)
is not observable on SQLite, which serialises writes instead of honouring
``select_for_update``. The racing test below therefore runs only on PostgreSQL
and reports itself as skipped in this environment (no database server on 5432);
the single-threaded tests cover the conditional ``UPDATE ... WHERE status``
idempotency -- the replay half of the story -- on every backend, and the lock
order itself is documented in ``apps/inventory/services.py``.
"""

from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import InventoryMovement, Reservation, Stock
from apps.inventory.services import (
    InsufficientStock,
    adjust_stock,
    consume_holds,
    release_holds,
    reserve_stock,
    sweep_expired_holds,
)
from tests.phase6_helpers import open_checkout, seed_stock

pytestmark = pytest.mark.django_db


# =============================================================================
# Counters
# =============================================================================


class TestStockCounters:
    def test_a_stock_row_is_born_on_first_use(self, product):
        variant = product.variants.first()

        stock = Stock.get_for_variant(variant)

        assert (stock.on_hand, stock.reserved, stock.available) == (0, 0, 0)

    def test_available_is_on_hand_minus_reserved(self, product):
        stock = seed_stock(product.variants.first(), 7, reserved=2)

        assert stock.available == 5

    def test_the_database_refuses_reserved_above_on_hand(self, product):
        with pytest.raises(IntegrityError), transaction.atomic():
            Stock.objects.create(variant=product.variants.first(), on_hand=1, reserved=2)


# =============================================================================
# Reserving
# =============================================================================


class TestReserving:
    def test_reserving_holds_units_and_records_the_ledger(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 5)
        checkout = open_checkout(user, variant=variant)

        holds = reserve_stock(
            checkout, [{"variant_id": variant.pk, "quantity": 2}], reference="test:hold"
        )

        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved, stock.available) == (5, 2, 3)
        hold = holds[0]
        assert hold.status == Reservation.Status.ACTIVE
        assert hold.quantity == 2
        assert hold.order_id is None
        assert hold.checkout == checkout
        movement = InventoryMovement.objects.get(kind=InventoryMovement.Kind.RESERVED)
        assert (movement.on_hand_delta, movement.reserved_delta) == (0, 2)
        assert movement.reference == "test:hold"

    def test_holds_expire_on_the_configured_window(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 5)
        checkout = open_checkout(user, variant=variant)

        with override_settings(INVENTORY_RESERVATION_MINUTES=5):
            hold = reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 1}])[0]

        assert timedelta(minutes=4) <= hold.expires_at - timezone.now() <= timedelta(minutes=6)

    def test_reserving_beyond_availability_changes_nothing(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 3)
        checkout = open_checkout(user, variant=variant)

        with pytest.raises(InsufficientStock) as exc:
            reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 4}])

        assert exc.value.sku == variant.sku
        assert (exc.value.requested, exc.value.available) == (4, 3)
        stock = Stock.objects.get(variant=variant)
        assert stock.reserved == 0
        assert not Reservation.objects.filter(status=Reservation.Status.ACTIVE).exists()
        assert InventoryMovement.objects.count() == 0

    def test_lines_are_merged_per_variant(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 10)
        checkout = open_checkout(user, variant=variant)

        holds = reserve_stock(
            checkout,
            [
                {"variant_id": variant.pk, "quantity": 1},
                {"variant_id": variant.pk, "quantity": 2},
            ],
        )

        assert len(holds) == 1
        assert holds[0].quantity == 3

    def test_a_quantity_below_one_is_refused(self, user, product):
        variant = product.variants.first()
        checkout = open_checkout(user, variant=variant)

        with pytest.raises(ValueError):
            reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 0}])

    def test_re_reserving_replaces_the_previous_hold(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 10)
        checkout = open_checkout(user, variant=variant)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 2}])

        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 4}])

        stock = Stock.objects.get(variant=variant)
        assert stock.reserved == 4
        active = Reservation.objects.filter(status=Reservation.Status.ACTIVE)
        assert list(active.values_list("quantity", flat=True)) == [4]
        assert Reservation.objects.filter(status=Reservation.Status.RELEASED).count() == 1

    def test_a_failed_revalidation_restores_the_old_holds(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 10)
        checkout = open_checkout(user, variant=variant)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 8}])

        with pytest.raises(InsufficientStock):
            reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 11}])

        hold = Reservation.objects.get(status=Reservation.Status.ACTIVE)
        assert hold.quantity == 8
        assert Stock.objects.get(variant=variant).reserved == 8
        # The release-then-fail sequence rolled back: no orphan ledger rows.
        assert InventoryMovement.objects.filter(kind=InventoryMovement.Kind.RELEASED).count() == 0


# =============================================================================
# Settling (release / consume)
# =============================================================================


class TestSettlingHolds:
    def test_release_returns_units_and_is_idempotent(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 5)
        checkout = open_checkout(user, variant=variant)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 2}])

        assert release_holds(checkout.reservations.all()) == 1
        assert release_holds(checkout.reservations.all()) == 0

        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved, stock.available) == (5, 0, 5)
        assert InventoryMovement.objects.filter(kind=InventoryMovement.Kind.RELEASED).count() == 1

    def test_consume_sells_units_and_is_idempotent(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 5)
        checkout = open_checkout(user, variant=variant)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 2}])

        assert consume_holds(checkout.reservations.all()) == 1
        assert consume_holds(checkout.reservations.all()) == 0

        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved) == (3, 0)
        sold = InventoryMovement.objects.get(kind=InventoryMovement.Kind.SOLD)
        assert (sold.on_hand_delta, sold.reserved_delta) == (-2, -2)
        assert checkout.reservations.get().status == Reservation.Status.CONSUMED


# =============================================================================
# Sweeper
# =============================================================================


class TestSweeper:
    def test_expired_unowned_holds_return_to_the_pool(self, user, other_user, product):
        variant = product.variants.first()
        seed_stock(variant, 10)
        abandoned = open_checkout(user, variant=variant)
        reserve_stock(
            abandoned,
            [{"variant_id": variant.pk, "quantity": 4}],
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        current = open_checkout(other_user, variant=variant)
        reserve_stock(current, [{"variant_id": variant.pk, "quantity": 1}])

        released = sweep_expired_holds()

        assert released == 1
        expired = Reservation.objects.get(pk=abandoned.reservations.get().pk)
        assert expired.status == Reservation.Status.EXPIRED
        stock = Stock.objects.get(variant=variant)
        assert stock.reserved == 1
        assert InventoryMovement.objects.filter(kind=InventoryMovement.Kind.EXPIRED).count() == 1
        # A second pass has nothing left to do.
        assert sweep_expired_holds() == 0

    def test_holds_that_are_not_due_yet_are_left_alone(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 10)
        checkout = open_checkout(user, variant=variant)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 3}])

        assert sweep_expired_holds() == 0
        assert checkout.reservations.get().status == Reservation.Status.ACTIVE
        assert Stock.objects.get(variant=variant).reserved == 3


# =============================================================================
# Manual adjustments
# =============================================================================


class TestAdjustments:
    def test_a_positive_delta_receives_stock_with_a_ledger_row(self, user, product):
        variant = product.variants.first()

        stock = adjust_stock(variant, 6, user=user, note="delivery 4711")

        assert stock.on_hand == 6
        movement = InventoryMovement.objects.get()
        assert movement.kind == InventoryMovement.Kind.ADJUSTMENT
        assert movement.on_hand_delta == 6
        assert movement.note == "delivery 4711"
        assert movement.user == user

    def test_a_negative_delta_cannot_dip_below_held_units(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 5, reserved=3)

        with pytest.raises(InsufficientStock):
            adjust_stock(variant, -3)

        assert Stock.objects.get(variant=variant).on_hand == 5

    def test_writing_off_everything_is_allowed_when_nothing_is_held(self, user, product):
        variant = product.variants.first()
        seed_stock(variant, 4)

        stock = adjust_stock(variant, -4, user=user)

        assert (stock.on_hand, stock.available) == (0, 0)

    def test_a_zero_delta_is_refused(self, product):
        with pytest.raises(ValueError):
            adjust_stock(product.variants.first(), 0)

    def test_ledger_sums_agree_with_the_counters(self, user, product):
        variant = product.variants.first()
        adjust_stock(variant, 7, user=user, note="first delivery")
        checkout = open_checkout(user, variant=variant)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 3}])
        release_holds(checkout.reservations.all())

        stock = Stock.objects.get(variant=variant)
        ledger = list(InventoryMovement.objects.filter(variant=variant).order_by("pk"))
        assert sum(row.on_hand_delta for row in ledger) == stock.on_hand == 7
        assert sum(row.reserved_delta for row in ledger) == stock.reserved == 0
        assert [row.kind for row in ledger] == [
            InventoryMovement.Kind.ADJUSTMENT,
            InventoryMovement.Kind.RESERVED,
            InventoryMovement.Kind.RELEASED,
        ]


# =============================================================================
# Admin workflow
# =============================================================================


class TestAdminAdjustWorkflow:
    def test_the_adjust_confirmation_template_renders(self, admin_client, product):
        stock = seed_stock(product.variants.first(), 5)

        response = admin_client.post(
            reverse("admin:inventory_stock_changelist"),
            {"action": "adjust_stock", ACTION_CHECKBOX_NAME: [str(stock.pk)]},
        )

        assert response.status_code == 200
        assert "admin/inventory/stock/adjust.html" in [t.name for t in response.templates]

    def test_applying_an_adjustment_moves_the_counter_and_the_ledger(
        self, admin_client, admin_user, product
    ):
        stock = seed_stock(product.variants.first(), 5)

        response = admin_client.post(
            reverse("admin:inventory_stock_changelist"),
            {
                "action": "adjust_stock",
                ACTION_CHECKBOX_NAME: [str(stock.pk)],
                "delta": "4",
                "kind": "received",
                "note": "delivery 9",
                "apply": "1",
            },
        )

        assert response.status_code == 302
        stock.refresh_from_db()
        assert stock.on_hand == 9
        movement = InventoryMovement.objects.get()
        assert movement.kind == InventoryMovement.Kind.RECEIVED
        assert movement.on_hand_delta == 4
        assert movement.reference == "admin"
        assert movement.user == admin_user

    def test_a_bad_adjustment_is_reported_not_applied(self, admin_client, product):
        stock = seed_stock(product.variants.first(), 5)

        response = admin_client.post(
            reverse("admin:inventory_stock_changelist"),
            {
                "action": "adjust_stock",
                ACTION_CHECKBOX_NAME: [str(stock.pk)],
                "delta": "-50",
                "kind": "adjustment",
                "note": "typo",
                "apply": "1",
            },
            follow=True,
        )

        assert response.status_code == 200
        stock.refresh_from_db()
        assert stock.on_hand == 5
        assert InventoryMovement.objects.count() == 0
        assert "only" in response.content.decode() or "available" in response.content.decode()


# =============================================================================
# Configuration
# =============================================================================


class TestSweeperScheduling:
    def test_the_sweeper_is_registered_in_the_beat_schedule(self):
        entry = settings.CELERY_BEAT_SCHEDULE["inventory-sweep-expired-reservations"]

        assert entry["task"] == "inventory.sweep_expired_reservations"
        assert entry["schedule"] == settings.INVENTORY_SWEEP_INTERVAL_SECONDS

    def test_the_celery_task_delegates_to_the_sweeper(self, user, product):
        from apps.inventory.tasks import sweep_expired_reservations

        variant = product.variants.first()
        seed_stock(variant, 5)
        checkout = open_checkout(user, variant=variant)
        reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 2}])
        Reservation.objects.update(expires_at=timezone.now() - timedelta(minutes=5))

        assert sweep_expired_reservations() == 1
        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved) == (5, 0)


# =============================================================================
# Concurrency (PostgreSQL only)
# =============================================================================


class TestConcurrencyContract:
    @pytest.mark.django_db(transaction=True)
    def test_only_one_racing_reserve_wins_the_last_unit(self, db, user, other_user, product):
        if connection.vendor != "postgresql":
            pytest.skip("select_for_update semantics are only observable on PostgreSQL")

        import threading

        from django.db import connections

        variant = product.variants.first()
        seed_stock(variant, 1)
        first = open_checkout(user, variant=variant)
        second = open_checkout(other_user, variant=variant)

        results: list[str] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2, timeout=15)

        def attempt(checkout):
            try:
                barrier.wait()
                reserve_stock(checkout, [{"variant_id": variant.pk, "quantity": 1}])
            except InsufficientStock:
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
        assert Stock.objects.get(variant=variant).reserved == 1
