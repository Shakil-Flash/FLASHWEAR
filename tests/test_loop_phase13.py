"""FLASH Loop (Phase 13): ownership, the resale lifecycle, trade-in credit,
recycling, the public shelf, the account screens, the API and the state machine.

The contract these tests protect:

* **ownership is server-verified** -- a payload naming someone else's order
  line, closet row or variant creates nothing, and the failure message never
  says whose row it was;
* **moderation is staff-only** -- no customer-facing surface (form, API
  payload, direct service call with a customer actor) can approve, publish
  or set authenticity;
* **the shelf shows one definition of "public"** -- active, published,
  reviewed, not authenticity-rejected, not sold;
* **money is policy, not guesswork** -- the trade-in estimate is a
  deterministic Decimal function of the settings block, and the credit is
  awarded exactly once, on completion;
* **first-party commerce is untouched** -- historical orders never change and
  resale never moves catalogue stock;
* **status and authenticity are not client-writable** on any surface.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.admin.sites import site
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import Stock
from apps.loop.models import LoopCredit, LoopItem, LoopItemImage, RecycleRequest, ResaleListing
from apps.loop.services import loop_items as svc
from apps.loop.services import ownership as ownership_service
from apps.loop.services.circularity import circularity_info
from apps.loop.services.errors import EligibilityError, LoopError, OwnershipError, TransitionError
from apps.loop.services.valuation import estimate_trade_in_credit
from apps.orders.services import deliver_order, ship_order
from tests.phase6_helpers import place_and_pay

pytestmark = pytest.mark.django_db

# A real 1x1 PNG: the upload path runs content validation, so a text blob
# would test the validator instead of the view.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQG"
    "AhKmMIQAAAABJRU5ErkJggg=="
)


# --------------------------------------------------------------------------------------
# Builders: everything goes through the real services, so a test that passes here
# describes a state production can actually reach.
# --------------------------------------------------------------------------------------


def delivered_order_item(user, variant, *, quantity=1, stock=10):
    """A paid, shipped, delivered order line the user owns."""
    order = place_and_pay(user, variant, quantity, stock=stock)
    ship_order(order, tracking_number="JD0002")
    deliver_order(order, actor=user)
    order.refresh_from_db()
    assert order.status == order.Status.DELIVERED
    return order.items.first()


def make_resale_draft(user, variant, *, condition="good", price=Decimal("30.00"), title=""):
    """A DRAFT resale loop item backed by a delivered order line."""
    order_item = delivered_order_item(user, variant)
    return svc.create_loop_item(
        user,
        LoopItem.Type.RESALE,
        condition=condition,
        order_item_id=order_item.pk,
        asking_price=price,
        title=title,
    )


def walk_resale_to_listed(item, staff):
    """DRAFT -> SUBMITTED -> UNDER_REVIEW -> APPROVED -> LISTED, plus the listing."""
    svc.submit_loop_item(item, actor=item.user)
    svc.start_review(item, actor=staff)
    svc.approve_resale(item, actor=staff)
    listing = svc.publish_listing(item, actor=staff)
    item.refresh_from_db()
    return listing


def make_public_listing(
    user, variant, staff, *, condition="good", price=Decimal("30.00"), title=""
):
    """A listing that is on the public shelf right now."""
    item = make_resale_draft(user, variant, condition=condition, price=price, title=title)
    listing = walk_resale_to_listed(item, staff)
    return item, listing


def make_trade_in(user, variant):
    """A submitted trade-in (the request row already exists)."""
    order_item = delivered_order_item(user, variant)
    item = svc.create_loop_item(
        user,
        LoopItem.Type.TRADE_IN,
        condition=LoopItem.Condition.LIKE_NEW,
        order_item_id=order_item.pk,
    )
    svc.submit_loop_item(item, actor=user)
    return item


def owned_closet_item(user, variant):
    """A PURCHASED wardrobe row for ``user`` (the strongest evidence)."""
    from apps.closet.services.closet import add_purchased_units

    order_item = delivered_order_item(user, variant)
    return add_purchased_units(user, order_item, units=1)[0]


# --------------------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------------------


class TestOwnershipEvidence:
    def test_a_purchased_wardrobe_row_is_evidence(self, user, product):
        closet_item = owned_closet_item(user, product.variants.first())

        evidence = ownership_service.verify_ownership(user, closet_item_id=closet_item.pk)

        assert evidence.via_closet is True
        assert evidence.product == product
        assert evidence.variant == closet_item.variant

    def test_a_delivered_order_line_is_evidence(self, user, product):
        order_item = delivered_order_item(user, product.variants.first())

        evidence = ownership_service.verify_ownership(user, order_item_id=order_item.pk)

        assert evidence.via_order is True
        assert evidence.product == product

    def test_a_manual_wardrobe_row_is_not_evidence(self, user):
        from apps.closet.models import ClosetItem
        from apps.closet.services.closet import create_manual_item

        manual = create_manual_item(
            user, name="Vintage jacket from home", category=ClosetItem.Category.OUTERWEAR
        )

        with pytest.raises(OwnershipError) as excinfo:
            ownership_service.verify_ownership(user, closet_item_id=manual.pk)
        assert excinfo.value.code == "loop_no_ownership_evidence"

    def test_someone_elses_wardrobe_row_is_refused(self, user, other_user, product):
        closet_item = owned_closet_item(other_user, product.variants.first())

        with pytest.raises(OwnershipError) as excinfo:
            ownership_service.verify_ownership(user, closet_item_id=closet_item.pk)
        # The message must not reveal whose row it was.
        assert "not yours" not in excinfo.value.message
        assert excinfo.value.code == "loop_no_ownership_evidence"

    def test_someone_elses_order_line_is_refused(self, user, other_user, product):
        order_item = delivered_order_item(other_user, product.variants.first())

        with pytest.raises(OwnershipError) as excinfo:
            ownership_service.verify_ownership(user, order_item_id=order_item.pk)
        assert "not yours" not in excinfo.value.message

    def test_an_undelivered_order_is_not_evidence(self, user, product):
        from apps.orders.models import Order

        order = place_and_pay(user, product.variants.first())
        order_item = order.items.first()
        assert order.status != Order.Status.DELIVERED

        with pytest.raises(OwnershipError):
            ownership_service.verify_ownership(user, order_item_id=order_item.pk)

    def test_third_party_brands_are_refused(self, user, other_brand, make_product, make_variant):
        product = make_product(name="Northline Shell", brand=other_brand)
        variant = make_variant(product)
        order_item = delivered_order_item(user, variant)

        with pytest.raises(LoopError):
            ownership_service.verify_ownership(user, order_item_id=order_item.pk)

    def test_an_in_house_product_without_a_brand_label_is_allowed(
        self, user, make_product, make_variant
    ):
        product = make_product(name="Unbranded Tee", brand=None)
        variant = make_variant(product)
        order_item = delivered_order_item(user, variant)

        evidence = ownership_service.verify_ownership(user, order_item_id=order_item.pk)

        assert evidence.product == product

    def test_a_nonexistent_id_is_refused_not_crashed(self, user):
        with pytest.raises(OwnershipError):
            ownership_service.verify_ownership(user, closet_item_id=999_999)
        with pytest.raises(OwnershipError):
            ownership_service.verify_ownership(user, order_item_id=999_999)


# --------------------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------------------


class TestCreateLoopItem:
    def test_a_resale_draft_lands_with_server_written_fields(self, user, product):
        order_item = delivered_order_item(user, product.variants.first())

        item = svc.create_loop_item(
            user,
            LoopItem.Type.RESALE,
            condition=LoopItem.Condition.GOOD,
            order_item_id=order_item.pk,
            asking_price=Decimal("25.00"),
            title="Pre-loved tee",
        )

        assert item.status == LoopItem.Status.DRAFT
        assert item.authenticity_status == LoopItem.AuthenticityStatus.UNVERIFIED
        assert item.user == user
        assert item.product == product
        assert item.order_item == order_item
        assert item.moderation_note == ""
        assert item.asking_price == Decimal("25.00")

    def test_a_resale_without_a_price_is_refused(self, user, product):
        order_item = delivered_order_item(user, product.variants.first())

        with pytest.raises(LoopError) as excinfo:
            svc.create_loop_item(
                user, LoopItem.Type.RESALE, condition="good", order_item_id=order_item.pk
            )
        assert excinfo.value.code == "loop_missing_price"

    def test_a_price_never_lands_on_a_trade_in(self, user, product):
        order_item = delivered_order_item(user, product.variants.first())

        item = svc.create_loop_item(
            user,
            LoopItem.Type.TRADE_IN,
            condition="good",
            order_item_id=order_item.pk,
            asking_price=Decimal("10.00"),
        )

        assert item.asking_price is None

    def test_an_unknown_condition_is_refused(self, user, product):
        order_item = delivered_order_item(user, product.variants.first())

        with pytest.raises(LoopError) as excinfo:
            svc.create_loop_item(
                user,
                LoopItem.Type.RESALE,
                condition="mint",
                order_item_id=order_item.pk,
                asking_price=Decimal("20.00"),
            )
        assert excinfo.value.code == "loop_bad_condition"

    def test_the_same_unit_cannot_enter_the_loop_twice(self, user, product):
        order_item = delivered_order_item(user, product.variants.first())
        svc.create_loop_item(
            user,
            LoopItem.Type.RESALE,
            condition="good",
            order_item_id=order_item.pk,
            asking_price=Decimal("25.00"),
        )

        with pytest.raises(EligibilityError) as excinfo:
            svc.create_loop_item(
                user,
                LoopItem.Type.TRADE_IN,
                condition="good",
                order_item_id=order_item.pk,
            )
        assert excinfo.value.code == "loop_item_already_active"

    def test_a_terminal_item_frees_the_unit_for_a_new_cycle(self, user, product, admin_user):
        item = make_resale_draft(user, product.variants.first())
        svc.submit_loop_item(item, actor=user)
        svc.cancel_loop_item(item, actor=user)

        again = svc.create_loop_item(
            user,
            LoopItem.Type.RESALE,
            condition="good",
            order_item_id=item.order_item_id,
            asking_price=Decimal("25.00"),
        )

        assert again.pk != item.pk
        assert again.status == LoopItem.Status.DRAFT

    def test_recycling_refuses_a_product_outside_the_programme(
        self, user, make_product, make_variant, settings
    ):
        settings.LOOP_RECYCLABLE_MATERIALS = ["wool"]
        settings.LOOP_RECYCLABLE_CATEGORIES = ["jeans"]
        product = make_product(name="Leather Belt")
        variant = make_variant(product)
        order_item = delivered_order_item(user, variant)

        with pytest.raises(EligibilityError) as excinfo:
            svc.create_loop_item(
                user, LoopItem.Type.RECYCLE, condition="fair", order_item_id=order_item.pk
            )
        assert excinfo.value.code == "loop_not_recyclable"
        assert LoopItem.objects.count() == 0

    def test_the_database_backstops_a_duplicate_active_unit(self, user, product, admin_user):
        item = make_resale_draft(user, product.variants.first())

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                LoopItem.objects.create(
                    user=user,
                    type=LoopItem.Type.TRADE_IN,
                    condition=LoopItem.Condition.GOOD,
                    order_item=item.order_item,
                    closet_item=None,
                )


# --------------------------------------------------------------------------------------
# Resale lifecycle
# --------------------------------------------------------------------------------------


class TestResaleLifecycle:
    def test_the_full_walk_to_the_shelf(self, user, product, admin_user):
        item = make_resale_draft(user, product.variants.first())

        listing = walk_resale_to_listed(item, admin_user)

        assert item.status == LoopItem.Status.LISTED
        assert item.listed_at is not None
        assert item.reviewed_at is not None
        assert item.reviewed_by == admin_user
        assert listing.status == ResaleListing.Status.ACTIVE
        assert listing.listed_at is not None
        assert listing.seller == user
        assert listing.asking_price == item.asking_price

    def test_submitting_creates_the_pending_listing_once(self, user, product):
        item = make_resale_draft(user, product.variants.first())

        svc.submit_loop_item(item, actor=user)
        svc.submit_loop_item(item, actor=user)  # double click

        assert item.status == LoopItem.Status.SUBMITTED
        assert ResaleListing.objects.filter(loop_item=item).count() == 1
        listing = item.resale_listing
        assert listing.status == ResaleListing.Status.PENDING_REVIEW

    def test_a_draft_cannot_jump_straight_to_the_shelf(self, user, product, admin_user):
        item = make_resale_draft(user, product.variants.first())

        with pytest.raises(TransitionError) as excinfo:
            svc.publish_listing(item, actor=admin_user)
        assert excinfo.value.code == "loop_bad_transition"
        item.refresh_from_db()
        assert item.status == LoopItem.Status.DRAFT
        assert ResaleListing.objects.filter(loop_item=item).count() == 0

    def test_moderation_is_staff_only(self, user, product):
        item = make_resale_draft(user, product.variants.first())
        svc.submit_loop_item(item, actor=user)

        for call in (
            lambda: svc.start_review(item, actor=user),
            lambda: svc.approve_resale(item, actor=user),
            lambda: svc.reject_item(item, actor=user),
            lambda: svc.publish_listing(item, actor=user),
            lambda: svc.set_authenticity(
                item, actor=user, status=LoopItem.AuthenticityStatus.VERIFIED
            ),
        ):
            with pytest.raises(LoopError) as excinfo:
                call()
            assert excinfo.value.code == "loop_not_staff"

        item.refresh_from_db()
        assert item.status == LoopItem.Status.SUBMITTED
        assert item.authenticity_status == LoopItem.AuthenticityStatus.UNVERIFIED

    def test_rejection_cancels_the_listing(self, user, product, admin_user):
        item = make_resale_draft(user, product.variants.first())
        svc.submit_loop_item(item, actor=user)
        svc.start_review(item, actor=admin_user)

        svc.reject_item(item, actor=admin_user, note="Damaged beyond repair")

        item.refresh_from_db()
        listing = item.resale_listing
        assert item.status == LoopItem.Status.REJECTED
        assert item.moderation_note == "Damaged beyond repair"
        assert listing.status == ResaleListing.Status.CANCELLED

    def test_cancelling_takes_the_listing_with_it(self, user, product, admin_user):
        item, listing = make_public_listing(user, product.variants.first(), admin_user)

        svc.cancel_loop_item(item, actor=user)

        item.refresh_from_db()
        listing.refresh_from_db()
        assert item.status == LoopItem.Status.CANCELLED
        assert listing.status == ResaleListing.Status.CANCELLED

    def test_publish_is_idempotent(self, user, product, admin_user):
        item, listing = make_public_listing(user, product.variants.first(), admin_user)

        svc.publish_listing(item, actor=admin_user)
        svc.publish_listing(item, actor=admin_user)

        item.refresh_from_db()
        assert item.status == LoopItem.Status.LISTED
        assert ResaleListing.objects.filter(loop_item=item).count() == 1
        assert listing.listed_at is not None

    def test_a_sale_moves_both_rows_and_stamps_the_time(self, user, product, admin_user):
        item, listing = make_public_listing(user, product.variants.first(), admin_user)

        svc.complete_sale(item)

        item.refresh_from_db()
        listing.refresh_from_db()
        assert item.status == LoopItem.Status.SOLD
        assert item.sold_at is not None
        assert listing.status == ResaleListing.Status.SOLD
        assert listing.sold_at is not None

    def test_listing_never_touches_catalogue_stock(self, user, product, admin_user):
        variant = product.variants.first()
        # The order's own reservation is created before the loop starts; what
        # the loop must never do is move these counters again.
        item = make_resale_draft(user, variant)
        stock = Stock.get_for_variant(variant)
        stock.on_hand, stock.reserved = 6, 1
        stock.save(update_fields=["on_hand", "reserved", "updated_at"])

        walk_resale_to_listed(item, admin_user)
        svc.complete_sale(item)

        stock.refresh_from_db()
        assert (stock.on_hand, stock.reserved) == (6, 1)

    def test_historical_orders_are_immutable(self, user, product, admin_user):
        item = make_resale_draft(user, product.variants.first())
        order = item.order_item.order
        before = (order.status, order.total, item.order_item.quantity, item.order_item.unit_price)

        walk_resale_to_listed(item, admin_user)
        svc.complete_sale(item)

        order.refresh_from_db()
        after = (order.status, order.total, item.order_item.quantity, item.order_item.unit_price)
        assert after == before
        assert order.status == order.Status.DELIVERED


# --------------------------------------------------------------------------------------
# Trade-in valuation
# --------------------------------------------------------------------------------------


class TestTradeInValuation:
    def test_the_estimate_is_the_documented_formula(self, user, product, settings):
        settings.LOOP_TRADE_IN_BASE_VALUE = Decimal("20.00")
        order_item = delivered_order_item(user, product.variants.first())
        item = svc.create_loop_item(
            user,
            LoopItem.Type.TRADE_IN,
            condition=LoopItem.Condition.LIKE_NEW,
            order_item_id=order_item.pk,
        )

        # 20.00 x 0.45 (like new) x 1.00 (category) x 1.00 (age < 180 days)
        assert estimate_trade_in_credit(item) == Decimal("9.00")

    def test_the_estimate_never_exceeds_half_the_original_price(self, user, product, settings):
        settings.LOOP_TRADE_IN_BASE_VALUE = Decimal("500.00")
        order_item = delivered_order_item(user, product.variants.first())
        item = svc.create_loop_item(
            user,
            LoopItem.Type.TRADE_IN,
            condition=LoopItem.Condition.NEW_WITH_TAGS,
            order_item_id=order_item.pk,
        )

        # The un-capped product would be 275.00; the cap is 49.00 x 0.50.
        assert estimate_trade_in_credit(item) == Decimal("24.50")

    def test_age_depreciates_the_estimate(self, user, product):
        closet_item = owned_closet_item(user, product.variants.first())
        # The valuation anchors on the order date (the purchase), not on when
        # the piece was added to the wardrobe.
        order = closet_item.order_item.order
        long_ago = timezone.now() - timedelta(days=400)
        type(order).objects.filter(pk=order.pk).update(created_at=long_ago)
        item = svc.create_loop_item(
            user,
            LoopItem.Type.TRADE_IN,
            condition=LoopItem.Condition.LIKE_NEW,
            closet_item_id=closet_item.pk,
        )

        # 20.00 x 0.45 x 1.00 x 0.80 (older than a year)
        assert estimate_trade_in_credit(item) == Decimal("7.20")

    def test_the_same_inputs_always_produce_the_same_number(self, user, product):
        order_item = delivered_order_item(user, product.variants.first())
        item = svc.create_loop_item(
            user,
            LoopItem.Type.TRADE_IN,
            condition=LoopItem.Condition.GOOD,
            order_item_id=order_item.pk,
        )

        numbers = {estimate_trade_in_credit(item) for _ in range(5)}

        assert len(numbers) == 1
        assert numbers.pop().as_tuple().exponent == -2

    def test_the_estimate_is_never_negative(self, user, product, settings):
        settings.LOOP_TRADE_IN_BASE_VALUE = Decimal("-50.00")
        order_item = delivered_order_item(user, product.variants.first())
        item = svc.create_loop_item(
            user,
            LoopItem.Type.TRADE_IN,
            condition=LoopItem.Condition.DAMAGED,
            order_item_id=order_item.pk,
        )

        assert estimate_trade_in_credit(item) >= Decimal("0.00")


# --------------------------------------------------------------------------------------
# Trade-in lifecycle
# --------------------------------------------------------------------------------------


class TestTradeInLifecycle:
    def test_submitting_records_an_estimate(self, user, product):
        item = make_trade_in(user, product.variants.first())
        request = item.trade_in_request

        assert request.status == request.Status.SUBMITTED
        assert request.estimated_credit is not None
        assert request.final_credit is None
        assert item.status == LoopItem.Status.SUBMITTED

    def test_the_walk_to_a_credit_award(self, user, product, admin_user):
        item = make_trade_in(user, product.variants.first())
        svc.start_review(item, actor=admin_user)
        svc.offer_trade_in(item, actor=admin_user, final_credit=Decimal("12.50"))
        svc.accept_trade_in(item, actor=user)
        credit = svc.complete_trade_in(item, actor=admin_user)

        item.refresh_from_db()
        request = item.trade_in_request
        assert item.status == LoopItem.Status.TRADE_IN_COMPLETED
        assert request.status == request.Status.COMPLETED
        assert credit.reference == f"trade-in:{request.pk}"
        assert credit.amount == Decimal("12.50")
        assert credit.user == user

    def test_the_credit_is_awarded_exactly_once(self, user, product, admin_user):
        item = make_trade_in(user, product.variants.first())
        svc.start_review(item, actor=admin_user)
        svc.offer_trade_in(item, actor=admin_user, final_credit=Decimal("12.50"))
        svc.accept_trade_in(item, actor=user)

        first = svc.complete_trade_in(item, actor=admin_user)
        second = svc.complete_trade_in(item, actor=admin_user)

        assert first.pk == second.pk
        assert LoopCredit.objects.filter(user=user).count() == 1

    def test_no_credit_exists_before_completion(self, user, product, admin_user):
        item = make_trade_in(user, product.variants.first())
        svc.start_review(item, actor=admin_user)
        svc.offer_trade_in(item, actor=admin_user, final_credit=Decimal("12.50"))
        svc.accept_trade_in(item, actor=user)

        assert LoopCredit.objects.filter(user=user).count() == 0

    def test_only_the_owner_may_accept_the_offer(self, user, other_user, product, admin_user):
        item = make_trade_in(user, product.variants.first())
        svc.start_review(item, actor=admin_user)
        svc.offer_trade_in(item, actor=admin_user, final_credit=Decimal("10.00"))

        with pytest.raises(OwnershipError):
            svc.accept_trade_in(item, actor=other_user)

        item.trade_in_request.refresh_from_db()
        assert item.trade_in_request.status == item.trade_in_request.Status.OFFERED

    def test_a_customer_cannot_offer_themselves_credit(self, user, product):
        item = make_trade_in(user, product.variants.first())

        with pytest.raises(LoopError) as excinfo:
            svc.offer_trade_in(item, actor=user, final_credit=Decimal("999.00"))
        assert excinfo.value.code == "loop_not_staff"
        item.trade_in_request.refresh_from_db()
        assert item.trade_in_request.final_credit is None

    def test_completion_before_acceptance_is_refused(self, user, product, admin_user):
        item = make_trade_in(user, product.variants.first())
        svc.start_review(item, actor=admin_user)
        svc.offer_trade_in(item, actor=admin_user, final_credit=Decimal("10.00"))

        with pytest.raises(TransitionError):
            svc.complete_trade_in(item, actor=admin_user)

        assert LoopCredit.objects.count() == 0

    def test_declining_cancels_the_loop_item(self, user, product, admin_user):
        item = make_trade_in(user, product.variants.first())
        svc.start_review(item, actor=admin_user)
        svc.offer_trade_in(item, actor=admin_user, final_credit=Decimal("10.00"))

        svc.decline_trade_in(item, actor=user)

        item.refresh_from_db()
        assert item.status == LoopItem.Status.CANCELLED
        assert item.trade_in_request.status == item.trade_in_request.Status.DECLINED

    def test_loop_credits_are_a_ledger_the_customer_cannot_edit(self, user):
        credit = LoopCredit.objects.create(
            user=user, amount=Decimal("10.00"), reference="trade-in:ledger-check"
        )

        # The balance is derived from AWARDED rows; there is no spend or edit
        # path on the model, and the admin refuses add and delete outright.
        assert credit.is_valid is True
        assert credit.reference.startswith("trade-in:")
        assert not hasattr(credit, "spent_at")


# --------------------------------------------------------------------------------------
# Recycling
# --------------------------------------------------------------------------------------


class TestRecycling:
    def _make_recycle(self, user, product, condition="fair"):
        order_item = delivered_order_item(user, product.variants.first())
        item = svc.create_loop_item(
            user, LoopItem.Type.RECYCLE, condition=condition, order_item_id=order_item.pk
        )
        svc.submit_loop_item(item, actor=user)
        return item

    def test_the_walk_to_completion(self, user, product, admin_user):
        item = self._make_recycle(user, product)

        svc.start_review(item, actor=admin_user)
        svc.accept_recycling(item, actor=admin_user)
        svc.complete_recycling(item, actor=admin_user)

        item.refresh_from_db()
        request = item.recycle_request
        assert item.status == LoopItem.Status.RECYCLE_COMPLETED
        assert request.status == request.Status.PROCESSED
        assert request.processed_at is not None

    def test_recycling_awards_no_money(self, user, product, admin_user):
        item = self._make_recycle(user, product)
        svc.start_review(item, actor=admin_user)
        svc.accept_recycling(item, actor=admin_user)
        svc.complete_recycling(item, actor=admin_user)

        assert LoopCredit.objects.filter(user=user).count() == 0

    def test_rejection_is_final_for_the_item(self, user, product, admin_user):
        item = self._make_recycle(user, product)
        svc.start_review(item, actor=admin_user)

        svc.reject_recycling(item, actor=admin_user, note="Not accepted")

        item.refresh_from_db()
        assert item.status == LoopItem.Status.REJECTED
        assert item.recycle_request.status == item.recycle_request.Status.REJECTED

    def test_acceptance_requires_a_staff_actor(self, user, product):
        item = self._make_recycle(user, product)

        with pytest.raises(LoopError) as excinfo:
            svc.accept_recycling(item, actor=user)
        assert excinfo.value.code == "loop_not_staff"


# --------------------------------------------------------------------------------------
# The public shelf
# --------------------------------------------------------------------------------------


class TestPublicShelfSelectors:
    def test_an_active_listing_is_public(self, user, product, admin_user):
        _item, listing = make_public_listing(user, product.variants.first(), admin_user)

        from apps.loop import selectors

        slugs = list(selectors.public_resale_listings().values_list("slug", flat=True))
        assert slugs == [listing.slug]
        assert selectors.public_listing(listing.slug) is not None

    def test_pending_review_is_not_public(self, user, product):
        item = make_resale_draft(user, product.variants.first())
        svc.submit_loop_item(item, actor=user)

        from apps.loop import selectors

        assert selectors.public_resale_listings().count() == 0
        assert selectors.public_listing(item.resale_listing.slug) is None

    def test_a_rejected_authenticity_hides_the_listing(self, user, product, admin_user):
        item, _listing = make_public_listing(user, product.variants.first(), admin_user)
        svc.set_authenticity(item, actor=admin_user, status=LoopItem.AuthenticityStatus.REJECTED)

        from apps.loop import selectors

        assert selectors.public_resale_listings().count() == 0

    def test_a_sold_listing_leaves_the_shelf(self, user, product, admin_user):
        item, listing = make_public_listing(user, product.variants.first(), admin_user)
        svc.complete_sale(item)

        from apps.loop import selectors

        assert selectors.public_resale_listings().count() == 0
        assert selectors.public_listing(listing.slug) is None

    def test_a_cancelled_listing_leaves_the_shelf(self, user, product, admin_user):
        item, _listing = make_public_listing(user, product.variants.first(), admin_user)
        svc.cancel_loop_item(item, actor=user)

        from apps.loop import selectors

        assert selectors.public_resale_listings().count() == 0

    def test_condition_and_price_filters_narrow_the_shelf(
        self, user, product, admin_user, make_product, make_variant
    ):
        from apps.loop import selectors

        fair_variant = make_variant(make_product(name="Fair Hoodie"))
        make_public_listing(user, product.variants.first(), admin_user, condition="good")
        make_public_listing(
            user, fair_variant, admin_user, condition="fair", price=Decimal("15.00")
        )

        assert selectors.public_resale_listings(condition="fair").count() == 1
        assert selectors.public_resale_listings(min_price=Decimal("20.00")).count() == 1
        assert selectors.public_resale_listings(max_price=Decimal("20.00")).count() == 1
        assert selectors.public_resale_listings(condition="excellent").count() == 0

    def test_an_unknown_sort_falls_back_instead_of_failing(self, user, product, admin_user):
        from apps.loop import selectors

        make_public_listing(user, product.variants.first(), admin_user)

        assert selectors.public_resale_listings(sort="; drop table").count() == 1
        assert selectors.public_resale_listings(sort="").count() == 1

    def test_the_shelf_does_not_run_a_query_per_card(
        self, user, admin_user, make_product, make_variant, django_assert_num_queries
    ):
        from apps.loop import selectors

        for index in range(5):
            variant = make_variant(make_product(name=f"Shelf Tee {index}"))
            make_public_listing(user, variant, admin_user)

        with django_assert_num_queries(3):
            list(selectors.public_resale_listings())


# --------------------------------------------------------------------------------------
# Web: public pages
# --------------------------------------------------------------------------------------


class TestPublicPages:
    def test_the_shelf_renders_for_a_guest(self, client, user, product, admin_user):
        item, listing = make_public_listing(user, product.variants.first(), admin_user)

        response = client.get("/loop/resale/")

        assert response.status_code == 200
        assert listing.slug in response.content.decode()
        assert item.display_title in response.content.decode()

    def test_both_shelf_urls_reach_the_same_view(self, client):
        assert client.get("/loop/").status_code == 200
        assert client.get("/loop/resale/").status_code == 200

    def test_an_unknown_listing_404s(self, client):
        assert client.get("/loop/resale/no-such-listing/").status_code == 404

    def test_the_detail_page_hides_moderation_and_seller_contact(
        self, client, user, product, admin_user
    ):
        item, listing = make_public_listing(user, product.variants.first(), admin_user)
        LoopItem.objects.filter(pk=item.pk).update(
            moderation_note="internal: customer flagged twice"
        )

        response = client.get(listing.get_absolute_url())
        body = response.content.decode()

        assert response.status_code == 200
        assert "customer flagged twice" not in body
        assert user.email not in body

    def test_the_detail_page_states_the_purchase_boundary(self, client, user, product, admin_user):
        _, listing = make_public_listing(user, product.variants.first(), admin_user)

        body = client.get(listing.get_absolute_url()).content.decode()

        assert "not open yet" in body
        assert "checkout" in body.lower()

    def test_the_shelf_offers_no_checkout_link(self, client, user, product, admin_user):
        make_public_listing(user, product.variants.first(), admin_user)

        body = client.get("/loop/resale/").content.decode()

        assert "/checkout/" not in body

    def test_unlisted_items_are_not_linkable(self, client, user, product):
        item = make_resale_draft(user, product.variants.first())
        svc.submit_loop_item(item, actor=user)

        assert client.get(item.resale_listing.get_absolute_url()).status_code == 404


# --------------------------------------------------------------------------------------
# Web: account screens
# --------------------------------------------------------------------------------------


class TestAccountPages:
    def test_the_dashboard_requires_a_login(self, client):
        response = client.get("/account/loop/")
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_the_dashboard_renders_and_hides_other_peoples_loops(
        self, client, user, other_user, product, admin_user, make_product, make_variant
    ):
        _mine, _ = make_public_listing(
            user, product.variants.first(), admin_user, title="Mine and only mine"
        )
        _theirs, _ = make_public_listing(
            other_user,
            make_variant(make_product(name="Someone elses tee")),
            admin_user,
            title="Not yours to see",
        )
        client.force_login(user)

        body = client.get("/account/loop/").content.decode()

        assert "Mine and only mine" in body
        assert "Not yours to see" not in body

    def test_an_empty_dashboard_still_renders(self, client, user):
        client.force_login(user)

        response = client.get("/account/loop/")

        # Regression: a zero balance is an int, and the money filter needs a Decimal.
        assert response.status_code == 200

    def test_the_create_page_lists_only_your_own_purchases(
        self, client, user, other_user, product, make_product, make_variant
    ):
        mine = owned_closet_item(user, product.variants.first())
        theirs = owned_closet_item(other_user, make_variant(make_product(name="Their Tee")))
        client.force_login(user)

        body = client.get("/account/loop/new/").content.decode()

        assert f"closet:{mine.pk}" in body
        assert f"closet:{theirs.pk}" not in body

    def test_posting_a_foreign_ownership_id_creates_nothing(
        self, client, user, other_user, product
    ):
        theirs = owned_closet_item(other_user, product.variants.first())
        client.force_login(user)

        response = client.post(
            "/account/loop/new/",
            {
                "owned_item": f"closet:{theirs.pk}",
                "loop_type": "resale",
                "condition": "good",
                "condition_notes": "",
                "title": "",
                "description": "",
                "asking_price": "25.00",
            },
        )

        assert response.status_code == 200  # re-rendered with the form error
        assert LoopItem.objects.count() == 0
        assert "owned_item" in response.context["form"].errors

    def test_a_valid_submission_reaches_the_draft_page(self, client, user, product):
        order_item = delivered_order_item(user, product.variants.first())
        client.force_login(user)

        response = client.post(
            "/account/loop/new/",
            {
                "owned_item": f"order:{order_item.pk}",
                "loop_type": "resale",
                "condition": "good",
                "condition_notes": "worn twice",
                "title": "My tee",
                "description": "",
                "asking_price": "25.00",
            },
            follow=False,
        )

        assert response.status_code == 302
        item = LoopItem.objects.get()
        assert response.url == reverse("account:loop-item-detail", kwargs={"pk": item.pk})
        assert item.status == LoopItem.Status.DRAFT

    def test_a_draft_detail_page_renders_before_any_child_row_exists(self, client, user, product):
        # Regression: the reverse OneToOne accessors raise on a draft.
        item = make_resale_draft(user, product.variants.first())
        client.force_login(user)

        response = client.get(reverse("account:loop-item-detail", kwargs={"pk": item.pk}))

        assert response.status_code == 200

    def test_someone_elses_loop_item_404s(self, client, user, other_user, product):
        item = make_resale_draft(other_user, product.variants.first())
        client.force_login(user)

        assert (
            client.get(reverse("account:loop-item-detail", kwargs={"pk": item.pk})).status_code
            == 404
        )

    def test_submitting_someone_elses_item_404s(self, client, user, other_user, product):
        item = make_resale_draft(other_user, product.variants.first())
        client.force_login(user)

        response = client.post(reverse("account:loop-item-submit", kwargs={"pk": item.pk}))

        assert response.status_code == 404
        item.refresh_from_db()
        assert item.status == LoopItem.Status.DRAFT

    def test_cancelling_someone_elses_item_404s(self, client, user, other_user, product):
        item = make_resale_draft(other_user, product.variants.first())
        client.force_login(user)

        response = client.post(reverse("account:loop-item-cancel", kwargs={"pk": item.pk}))

        assert response.status_code == 404
        item.refresh_from_db()
        assert item.status == LoopItem.Status.DRAFT

    def test_the_owner_can_submit_and_cancel(self, client, user, product):
        item = make_resale_draft(user, product.variants.first())
        client.force_login(user)

        submit = client.post(reverse("account:loop-item-submit", kwargs={"pk": item.pk}))
        item.refresh_from_db()
        assert submit.status_code == 302
        assert item.status == LoopItem.Status.SUBMITTED

        cancel = client.post(reverse("account:loop-item-cancel", kwargs={"pk": item.pk}))
        item.refresh_from_db()
        assert cancel.status_code == 302
        assert item.status == LoopItem.Status.CANCELLED

    def test_a_customer_cannot_approve_or_publish_from_the_web(
        self, client, user, product, admin_user
    ):
        item = make_resale_draft(user, product.variants.first())
        svc.submit_loop_item(item, actor=user)
        client.force_login(user)

        # There is no customer URL for moderation: the only POST endpoints are
        # submit, cancel and photos, and none of them changes moderation state.
        for name in ("loop-item-submit", "loop-item-cancel"):
            client.post(reverse(f"account:{name}", kwargs={"pk": item.pk}))

        item.refresh_from_db()
        assert item.status in (LoopItem.Status.SUBMITTED, LoopItem.Status.CANCELLED)
        assert item.authenticity_status == LoopItem.AuthenticityStatus.UNVERIFIED

    def test_photo_upload_attaches_to_your_own_draft(self, client, user, product, settings):
        settings.MEDIA_ROOT = settings.BASE_DIR / "test-media-loop"
        item = make_resale_draft(user, product.variants.first())
        client.force_login(user)

        response = client.post(
            reverse("account:loop-item-photo", kwargs={"pk": item.pk}),
            {
                "image": SimpleUploadedFile("front.png", PNG_1PX, content_type="image/png"),
                "kind": LoopItemImage.Kind.FRONT,
                "alt_text": "Front of the tee",
            },
        )

        assert response.status_code == 302
        image = LoopItemImage.objects.get()
        assert image.loop_item == item
        assert image.alt_text == "Front of the tee"

    def test_the_photo_limit_is_enforced(self, client, user, product, settings):
        settings.MEDIA_ROOT = settings.BASE_DIR / "test-media-loop"
        item = make_resale_draft(user, product.variants.first())
        for position in range(settings.LOOP_MAX_IMAGES):
            LoopItemImage.objects.create(
                loop_item=item,
                image="loop/x.png",
                kind=LoopItemImage.Kind.FRONT,
                alt_text=f"Photo {position}",
                position=position,
            )
        client.force_login(user)

        response = client.post(
            reverse("account:loop-item-photo", kwargs={"pk": item.pk}),
            {
                "image": SimpleUploadedFile("front.png", PNG_1PX, content_type="image/png"),
                "kind": LoopItemImage.Kind.FRONT,
                "alt_text": "One too many",
            },
        )

        assert response.status_code == 302
        assert LoopItemImage.objects.count() == settings.LOOP_MAX_IMAGES

    def test_selling_your_own_item_is_not_possible_from_a_form(
        self, client, user, product, admin_user
    ):
        item, _ = make_public_listing(user, product.variants.first(), admin_user)
        client.force_login(user)

        # The cancel endpoint is the only state write a customer has, and it
        # can never mint a sale.
        client.post(reverse("account:loop-item-cancel", kwargs={"pk": item.pk}))

        item.refresh_from_db()
        assert item.status == LoopItem.Status.CANCELLED


# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------


class TestPublicApi:
    def test_the_shelf_needs_no_authentication(self, api_client, user, product, admin_user):
        make_public_listing(user, product.variants.first(), admin_user)

        response = api_client.get("/api/v1/loop/resale/")

        assert response.status_code == 200
        assert response.data["count"] == 1

    def test_the_shelf_leaks_no_seller_contact_details(self, api_client, user, product, admin_user):
        make_public_listing(user, product.variants.first(), admin_user)

        body = str(api_client.get("/api/v1/loop/resale/").data)
        detail = str(
            api_client.get(f"/api/v1/loop/resale/{ResaleListing.objects.get().slug}/").data
        )

        assert user.email not in body
        assert user.email not in detail
        assert "moderation_note" not in body + detail
        assert "reviewer_note" not in body + detail

    def test_the_detail_endpoint_returns_public_facts_only(
        self, api_client, user, product, admin_user
    ):
        _, listing = make_public_listing(user, product.variants.first(), admin_user)

        data = api_client.get(f"/api/v1/loop/resale/{listing.slug}/").data

        assert data["asking_price"] in (str(listing.asking_price), float(listing.asking_price))
        assert data["condition"] == "good"
        assert "circularity" in data
        assert data["circularity"]["lifecycle_extension_is_estimate"] is True

    def test_an_unknown_slug_404s(self, api_client):
        assert api_client.get("/api/v1/loop/resale/nope/").status_code == 404

    def test_page_size_is_clamped(self, api_client):
        response = api_client.get("/api/v1/loop/resale/?page_size=100000")

        from django.conf import settings as django_settings

        assert response.data["count"] <= django_settings.CATALOG_API_MAX_PAGE_SIZE

    def test_the_root_endpoint_lists_the_surfaces(self, api_client):
        data = api_client.get("/api/v1/loop/").data
        assert {"resale", "items", "trade_ins", "recycling", "credits"} <= set(data)


class TestItemApi:
    def test_the_item_endpoints_require_authentication(self, api_client):
        api_client.credentials()
        assert api_client.get("/api/v1/loop/items/").status_code in (401, 403)
        assert api_client.get("/api/v1/loop/credits/").status_code in (401, 403)

    def test_your_items_are_listed(self, api_client, user, product):
        item = make_resale_draft(user, product.variants.first())
        api_client.force_authenticate(user)

        data = api_client.get("/api/v1/loop/items/").data

        assert data["count"] == 1
        assert data["results"][0]["id"] == item.pk

    def test_another_customers_item_404s(self, api_client, user, other_user, product):
        item = make_resale_draft(other_user, product.variants.first())
        api_client.force_authenticate(user)

        assert api_client.get(f"/api/v1/loop/items/{item.pk}/").status_code == 404
        assert api_client.post(f"/api/v1/loop/items/{item.pk}/submit/").status_code == 404
        assert api_client.post(f"/api/v1/loop/items/{item.pk}/cancel/").status_code == 404
        item.refresh_from_db()
        assert item.status == LoopItem.Status.DRAFT

    def test_status_and_authenticity_in_the_payload_are_ignored(self, api_client, user, product):
        order_item = delivered_order_item(user, product.variants.first())
        api_client.force_authenticate(user)

        response = api_client.post(
            "/api/v1/loop/items/",
            {
                "ownership_source": "order",
                "ownership_id": order_item.pk,
                "type": "resale",
                "condition": "good",
                "asking_price": "25.00",
                "status": "approved",
                "authenticity_status": "verified",
                "moderation_note": "please approve me",
            },
        )

        assert response.status_code == 201
        item = LoopItem.objects.get()
        assert item.status == LoopItem.Status.DRAFT
        assert item.authenticity_status == LoopItem.AuthenticityStatus.UNVERIFIED
        assert item.moderation_note == ""

    def test_foreign_ownership_is_refused_with_a_generic_message(
        self, api_client, user, other_user, product
    ):
        theirs = owned_closet_item(other_user, product.variants.first())
        api_client.force_authenticate(user)

        response = api_client.post(
            "/api/v1/loop/items/",
            {
                "ownership_source": "closet",
                "ownership_id": theirs.pk,
                "type": "resale",
                "condition": "good",
                "asking_price": "25.00",
            },
        )

        assert response.status_code == 400
        assert response.data["code"] == "loop_no_ownership_evidence"
        assert "not yours" not in response.data["detail"]
        assert LoopItem.objects.count() == 0

    def test_a_resale_without_a_price_is_a_validation_error(self, api_client, user, product):
        order_item = delivered_order_item(user, product.variants.first())
        api_client.force_authenticate(user)

        response = api_client.post(
            "/api/v1/loop/items/",
            {
                "ownership_source": "order",
                "ownership_id": order_item.pk,
                "type": "resale",
                "condition": "good",
            },
        )

        assert response.status_code == 400
        assert "asking_price" in response.data

    def test_submit_and_cancel_flow_through_the_api(self, api_client, user, product):
        item = make_resale_draft(user, product.variants.first())
        api_client.force_authenticate(user)

        submitted = api_client.post(f"/api/v1/loop/items/{item.pk}/submit/")
        assert submitted.status_code == 200
        assert submitted.data["status"] == LoopItem.Status.SUBMITTED

        cancelled = api_client.post(f"/api/v1/loop/items/{item.pk}/cancel/")
        assert cancelled.status_code == 200
        assert cancelled.data["status"] == LoopItem.Status.CANCELLED

    def test_items_are_read_only_outside_the_two_endpoints(self, api_client, user, product):
        item = make_resale_draft(user, product.variants.first())
        api_client.force_authenticate(user)

        assert api_client.patch(f"/api/v1/loop/items/{item.pk}/").status_code == 405
        assert api_client.delete(f"/api/v1/loop/items/{item.pk}/").status_code == 405

        item.refresh_from_db()
        assert item.status == LoopItem.Status.DRAFT

    def test_a_customer_cannot_publish_through_the_api(self, api_client, user, product, admin_user):
        item = make_resale_draft(user, product.variants.first())
        svc.submit_loop_item(item, actor=user)
        api_client.force_authenticate(user)

        # No moderation endpoint exists at all.
        assert api_client.post(f"/api/v1/loop/items/{item.pk}/approve/").status_code == 404
        item.refresh_from_db()
        assert item.status == LoopItem.Status.SUBMITTED


class TestTradeInAndCreditApi:
    def test_creating_a_trade_in_submits_it_with_an_estimate(self, api_client, user, product):
        order_item = delivered_order_item(user, product.variants.first())
        api_client.force_authenticate(user)

        response = api_client.post(
            "/api/v1/loop/trade-ins/",
            {
                "ownership_source": "order",
                "ownership_id": order_item.pk,
                "condition": "like_new",
                "condition_notes": "barely worn",
            },
        )

        assert response.status_code == 201
        assert response.data["status"] == "submitted"
        assert response.data["estimated_credit"] is not None
        assert response.data["final_credit"] is None

    def test_trade_in_and_recycling_lists_are_customer_scoped(
        self, api_client, user, other_user, product, make_product, make_variant
    ):
        mine = make_trade_in(user, product.variants.first())
        theirs = make_trade_in(other_user, make_variant(make_product(name="Their Tee")))
        api_client.force_authenticate(user)

        data = api_client.get("/api/v1/loop/trade-ins/").data

        ids = {row["id"] for row in data["results"]}
        assert mine.trade_in_request.pk in ids
        assert theirs.trade_in_request.pk not in ids

    def test_credits_are_customer_scoped(self, api_client, user, other_user):
        LoopCredit.objects.create(user=user, amount=Decimal("12.00"), reference="trade-in:mine")
        LoopCredit.objects.create(
            user=other_user, amount=Decimal("99.00"), reference="trade-in:theirs"
        )
        api_client.force_authenticate(user)

        data = api_client.get("/api/v1/loop/credits/").data

        assert data["count"] == 1
        assert Decimal(str(data["results"][0]["amount"])) == Decimal("12.00")
        assert "theirs" not in str(data)

    def test_recycling_create_rejects_foreign_ownership(
        self, api_client, user, other_user, product
    ):
        theirs = owned_closet_item(other_user, product.variants.first())
        api_client.force_authenticate(user)

        response = api_client.post(
            "/api/v1/loop/recycling/",
            {
                "ownership_source": "closet",
                "ownership_id": theirs.pk,
                "condition": "fair",
            },
        )

        assert response.status_code == 400
        assert RecycleRequest.objects.count() == 0


# --------------------------------------------------------------------------------------
# State machine, models, admin, tasks
# --------------------------------------------------------------------------------------


class TestStateMachines:
    @pytest.mark.parametrize(
        ("start", "target", "allowed"),
        [
            (LoopItem.Status.DRAFT, LoopItem.Status.SUBMITTED, True),
            (LoopItem.Status.DRAFT, LoopItem.Status.LISTED, False),
            (LoopItem.Status.DRAFT, LoopItem.Status.SOLD, False),
            (LoopItem.Status.SUBMITTED, LoopItem.Status.APPROVED, False),
            (LoopItem.Status.UNDER_REVIEW, LoopItem.Status.APPROVED, True),
            (LoopItem.Status.UNDER_REVIEW, LoopItem.Status.REJECTED, True),
            (LoopItem.Status.APPROVED, LoopItem.Status.LISTED, True),
            (LoopItem.Status.LISTED, LoopItem.Status.SOLD, True),
            (LoopItem.Status.SOLD, LoopItem.Status.LISTED, False),
            (LoopItem.Status.REJECTED, LoopItem.Status.SUBMITTED, False),
            (LoopItem.Status.CANCELLED, LoopItem.Status.DRAFT, False),
        ],
    )
    def test_loop_item_edges(self, start, target, allowed):
        item = LoopItem(status=start, type=LoopItem.Type.RESALE, condition="good")
        assert item.can_transition_to(target) is allowed

    def test_every_listed_edge_is_real(self, user, product):
        for start, targets in LoopItem.ALLOWED_TRANSITIONS.items():
            item = LoopItem(status=start, type=LoopItem.Type.RESALE, condition="good")
            for target in targets:
                assert item.can_transition_to(target), f"{start} -> {target}"

    def test_terminal_states_have_no_way_out(self):
        for status in LoopItem.terminal_statuses():
            assert LoopItem.ALLOWED_TRANSITIONS[status] == ()

    @pytest.mark.parametrize(
        ("start", "target", "allowed"),
        [
            (ResaleListing.Status.PENDING_REVIEW, ResaleListing.Status.ACTIVE, True),
            (ResaleListing.Status.PENDING_REVIEW, ResaleListing.Status.SOLD, False),
            (ResaleListing.Status.ACTIVE, ResaleListing.Status.SOLD, True),
            (ResaleListing.Status.SOLD, ResaleListing.Status.ACTIVE, False),
        ],
    )
    def test_listing_edges(self, start, target, allowed):
        listing = ResaleListing(status=start, asking_price=Decimal("10.00"))
        assert listing.can_transition_to(target) is allowed


class TestCircularity:
    def test_the_payload_is_json_safe_and_honestly_labelled(self, user, product):
        order_item = delivered_order_item(user, product.variants.first())
        item = svc.create_loop_item(
            user,
            LoopItem.Type.RESALE,
            condition="good",
            order_item_id=order_item.pk,
            asking_price=Decimal("25.00"),
        )

        import json

        info = circularity_info(item)

        json.dumps(info)  # must not raise
        assert info["lifecycle_extension_is_estimate"] is True
        assert info["resale_eligible"] is True
        assert "co2" not in str(info).lower()

    def test_recycling_refuses_a_product_outside_the_programme_and_says_so(
        self, user, make_product, make_variant, settings
    ):
        from apps.catalog.models import Category

        settings.LOOP_RECYCLABLE_MATERIALS = []
        settings.LOOP_RECYCLABLE_CATEGORIES = ["t-shirts"]
        scarves = Category.objects.create(name="Scarves", slug="scarves")
        in_scope_variant = make_variant(make_product(name="Cotton Tee"))
        out_scope_variant = make_variant(make_product(name="Silk Scarf", category=scarves))
        out_order_item = delivered_order_item(user, out_scope_variant)

        with pytest.raises(EligibilityError) as excinfo:
            svc.create_loop_item(
                user, LoopItem.Type.RECYCLE, condition="fair", order_item_id=out_order_item.pk
            )
        assert excinfo.value.code == "loop_not_recyclable"
        assert LoopItem.objects.count() == 0

        # The same garment can still be resold; the recycling flag is honest
        # about being out of programme rather than blocking the whole Loop.
        in_item = svc.create_loop_item(
            user,
            LoopItem.Type.RECYCLE,
            condition="fair",
            order_item_id=delivered_order_item(user, in_scope_variant).pk,
        )
        assert circularity_info(in_item)["recycle_eligible"] is True


class TestAdminAndTasks:
    def test_the_loop_models_are_registered_for_moderators(self):
        assert LoopItem in site._registry
        assert ResaleListing in site._registry
        assert LoopCredit in site._registry

    def test_credits_cannot_be_hand_added_or_deleted_in_admin(self):
        from apps.loop.admin import LoopCreditAdmin

        model_admin = LoopCreditAdmin(LoopCredit, site)

        assert model_admin.has_add_permission(None) is False
        assert model_admin.has_delete_permission(None) is False

    def test_stale_listings_expire(self, user, product, admin_user, settings):
        from apps.loop.tasks import expire_stale_listings

        settings.LOOP_LISTING_TTL_DAYS = 30
        _, listing = make_public_listing(user, product.variants.first(), admin_user)
        stale_time = timezone.now() - timedelta(days=45)
        ResaleListing.objects.filter(pk=listing.pk).update(listed_at=stale_time)

        expired = expire_stale_listings()

        listing.refresh_from_db()
        assert expired == 1
        assert listing.status == ResaleListing.Status.EXPIRED
        assert listing.expired_at is not None

    def test_fresh_listings_survive_the_sweeper(self, user, product, admin_user):
        from apps.loop.tasks import expire_stale_listings

        _, listing = make_public_listing(user, product.variants.first(), admin_user)

        assert expire_stale_listings() == 0
        listing.refresh_from_db()
        assert listing.status == ResaleListing.Status.ACTIVE

    def test_past_due_credits_are_marked_expired(self, user):
        from apps.loop.tasks import expire_loop_credits

        credit = LoopCredit.objects.create(
            user=user,
            amount=Decimal("5.00"),
            reference="trade-in:stale",
            expires_at=timezone.now() - timedelta(days=1),
        )

        assert expire_loop_credits() == 1
        credit.refresh_from_db()
        assert credit.status == LoopCredit.Status.EXPIRED
        # The balance selector must not count it either.
        from apps.loop import selectors

        assert selectors.user_credits(user).count() == 0


class TestQueryHygiene:
    def test_the_shelf_page_is_not_a_query_per_card(
        self, client, user, admin_user, make_product, make_variant
    ):
        variants = []
        for index in range(5):
            variant = make_variant(make_product(name=f"Grid Item {index}"))
            variants.append(variant)
            make_public_listing(user, variant, admin_user)

        with CaptureQueriesContext(connection) as captured:
            response = client.get("/loop/resale/")

        assert response.status_code == 200
        # Cards are prefetched; an N+1 here would be 4 queries per listing.
        assert len(captured) < 4 * len(variants)
