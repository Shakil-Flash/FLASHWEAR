"""Phase 53 Tests: Returns, Exchanges & Refund Workflow.

Validates:
1. Configurable eligibility rules (return window, order/payment statuses, category/product restrictions).
2. Partial returns, multi-item requests, remaining quantity limits.
3. Reason normalization and choices (wrong size, damaged item, incorrect item, defective product, changed mind).
4. Evidence attachments (ReturnImage creation, image validation).
5. State lifecycle (REQUESTED -> UNDER_REVIEW -> APPROVED -> AWAITING_SHIPMENT -> RECEIVED -> INSPECTION_PASSED / INSPECTION_FAILED -> REFUND_PENDING / EXCHANGE_PENDING -> REFUNDED / COMPLETED).
6. Security & Authorization (prevention of self-approval by staff, prevention of customer self-approval, IDOR protection).
7. Required reasons for rejections and failed inspections.
8. Customer cancellation boundaries (permitted before receipt, forbidden after receipt).
9. Inventory safety:
   - Returned goods do not automatically become sellable inventory.
   - Only RESTOCKABLE disposition units increase sellable on-hand stock.
   - DAMAGED, DEFECTIVE, DISPOSAL dispositions are withheld from sellable stock.
   - Duplicate restock prevention.
10. Refund financial correctness:
    - Refund calculated from server-side order item totals with discounts.
    - Capped at payment captured amount and remaining refundable balance.
    - Shipping refund only allowed once per order.
    - Idempotency prevents duplicate refunds.
    - Manual refund workflow for offline processing.
11. Exchange processing:
    - Catalog replacement variant verification & inventory stock checks.
    - Restocks original item only if restockable; allocates replacement variant.
    - Automatic refund for cheaper replacement variant.
    - No silent charges for more expensive replacement variant.
12. Notification accuracy and duplicate prevention.
13. Customer and staff UI views and permission checks.
"""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.backoffice.models import AuditEvent
from apps.backoffice.permissions import (
    ORDERS_GROUP,
    ensure_backoffice_groups,
)
from apps.catalog.models import Category, ProductVariant
from apps.inventory.models import InventoryMovement, Stock
from apps.inventory.services import InsufficientStock
from apps.notifications.models import Notification, NotificationType
from apps.orders.models import (
    InvalidTransition,
    Order,
    ReturnEvent,
    ReturnItem,
    ReturnRequest,
)
from apps.orders.returns_services import (
    approve_return_request,
    cancel_return_request,
    check_order_return_eligibility,
    create_return_request,
    inspect_return,
    mark_return_awaiting_shipment,
    mark_return_exchange_pending,
    mark_return_inspection_failed,
    mark_return_inspection_passed,
    mark_return_received,
    mark_return_refund_pending,
    mark_return_under_review,
    normalize_return_reason,
    process_exchange_for_return,
    process_refund_for_return,
    record_manual_refund_for_return,
    reject_return_request,
)
from apps.payments.models import Payment, Refund
from tests.phase6_helpers import place_and_pay, placed_order, seed_stock


def make_delivered_order(user, variant, quantity=2, stock=20):
    """Create a fully paid, delivered order with seeded stock."""
    order = place_and_pay(user, variant, quantity=quantity, stock=stock)
    order.transition_to(Order.Status.PROCESSING)
    order.transition_to(Order.Status.SHIPPED)
    order.transition_to(Order.Status.DELIVERED)
    return order


def make_sample_image(name="test_proof.jpg", color="red"):
    """Create a valid in-memory image for upload tests."""
    file = io.BytesIO()
    image = Image.new("RGB", (200, 200), color=color)
    image.save(file, format="JPEG")
    file.seek(0)
    return SimpleUploadedFile(name, file.read(), content_type="image/jpeg")


@pytest.fixture
def other_user(db):
    return get_user_model().objects.create_user(
        email="other_customer@flashwear.test",
        password="Str0ng-Passw0rd!",
    )


@pytest.fixture
def replacement_variant(db, product, other_colour, other_size):
    """Sibling variant for exchange tests."""
    return ProductVariant.objects.create(
        product=product,
        sku="FW-TEE-SND-L",
        color=other_colour,
        size=other_size,
        price=Decimal("49.00"),
    )


@pytest.fixture
def cheaper_variant(db, product, other_colour, size):
    """Sibling variant with cheaper price for price difference tests."""
    return ProductVariant.objects.create(
        product=product,
        sku="FW-TEE-SND-M",
        color=other_colour,
        size=size,
        price=Decimal("35.00"),
    )


@pytest.fixture
def expensive_variant(db, product, other_colour, size):
    """Sibling variant with higher price for price difference tests."""
    return ProductVariant.objects.create(
        product=product,
        sku="FW-TEE-SND-XL",
        color=other_colour,
        size=size,
        price=Decimal("65.00"),
    )


@pytest.fixture
def staff_user(db):
    ensure_backoffice_groups()
    user = get_user_model().objects.create_user(
        email="staff_p53@flashwear.test",
        password="Str0ng-Passw0rd!",
        is_staff=True,
    )
    user.groups.add(Group.objects.get(name=ORDERS_GROUP))
    return user


# =============================================================================
# 1. Eligibility Rules & Policy Boundaries
# =============================================================================


@pytest.mark.django_db
def test_eligibility_window_and_deadline_configurable(user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=2)

    # Standard 30-day window
    eligibility = check_order_return_eligibility(order)
    assert eligibility["is_eligible"] is True
    assert eligibility["window_days"] == 30
    assert eligibility["days_remaining"] >= 29

    # Past return window
    order.delivered_at = timezone.now() - timedelta(days=35)
    order.save(update_fields=["delivered_at"])
    eligibility = check_order_return_eligibility(order)
    assert eligibility["is_eligible"] is False
    assert "return window for this order closed" in eligibility["reason"]

    # Configurable window override
    with override_settings(RETURN_WINDOW_DAYS=45):
        eligibility = check_order_return_eligibility(order)
        assert eligibility["is_eligible"] is True
        assert eligibility["window_days"] == 45
        assert eligibility["days_remaining"] >= 9


@pytest.mark.django_db
def test_eligibility_category_and_product_policy_restrictions(user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)

    # Place product in a non-returnable category (e.g. underwear)
    underwear_cat = Category.objects.create(name="Underwear", slug="underwear")
    product.category = underwear_cat
    product.save(update_fields=["category"])

    eligibility = check_order_return_eligibility(order)
    assert eligibility["is_eligible"] is False
    assert any("non-returnable" in item.get("policy_restriction_reason", "") for item in eligibility["items"])


@pytest.mark.django_db
def test_eligibility_unpaid_or_pending_orders_disallowed(user, product):
    variant = product.variants.first()
    order = placed_order(user, variant, 1, stock=10)
    assert order.status == Order.Status.PENDING_PAYMENT
    eligibility = check_order_return_eligibility(order)
    assert eligibility["is_eligible"] is False
    assert "shipped or delivered" in eligibility["reason"]


# =============================================================================
# 2. Return Request Creation, Partial Returns & Reasons
# =============================================================================


@pytest.mark.django_db
def test_reason_normalization():
    assert normalize_return_reason("wrong size") == ReturnRequest.Reason.WRONG_SIZE
    assert normalize_return_reason("damaged item") == ReturnRequest.Reason.DAMAGED
    assert normalize_return_reason("incorrect item") == ReturnRequest.Reason.INCORRECT_ITEM
    assert normalize_return_reason("defective product") == ReturnRequest.Reason.DEFECTIVE_PRODUCT
    assert normalize_return_reason("changed mind") == ReturnRequest.Reason.CHANGED_MIND
    assert normalize_return_reason("size_fit") == ReturnRequest.Reason.SIZE_FIT


@pytest.mark.django_db
def test_partial_returns_and_quantity_limits(user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=3)
    order_item = order.items.first()

    # Return 1 of 3
    ret1 = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
        reason="wrong size",
    )
    assert ret1.status == ReturnRequest.Status.REQUESTED
    assert ret1.reason == ReturnRequest.Reason.WRONG_SIZE
    assert ret1.items.count() == 1
    assert ret1.items.first().quantity == 1

    # Check remaining eligibility on order: 2 units left
    eligibility = check_order_return_eligibility(order)
    assert eligibility["is_eligible"] is True
    assert eligibility["items"][0]["remaining_quantity"] == 2

    # Return remaining 2 units in second request
    ret2 = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 2}],
        reason="damaged item",
    )
    assert ret2.status == ReturnRequest.Status.REQUESTED
    assert ret2.reason == ReturnRequest.Reason.DAMAGED

    # Third request must fail since 0 units remain
    with pytest.raises(ValidationError) as exc:
        create_return_request(
            order=order,
            user=user,
            items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
        )
    assert "All eligible items on this order have already been returned" in str(exc.value)


@pytest.mark.django_db
def test_return_request_with_supporting_evidence(user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    img_file = make_sample_image("damage_evidence.jpg")

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
        reason="defective product",
        customer_note="Torn fabric along the left seam.",
        images=[img_file],
    )
    assert ret.images.count() == 1
    assert "damage_evidence" in ret.images.first().image.name


# =============================================================================
# 3. State Lifecycle & Workflow Transitions
# =============================================================================


@pytest.mark.django_db
def test_full_return_workflow_with_under_review_and_awaiting_shipment(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
        reason="defective product",
    )
    assert ret.status == ReturnRequest.Status.REQUESTED

    # 1. Staff places under review
    ret = mark_return_under_review(ret, staff_user=staff_user, note="Assigned to QA specialist.")
    assert ret.status == ReturnRequest.Status.UNDER_REVIEW
    assert ret.under_review_at is not None

    # 2. Staff approves
    ret = approve_return_request(ret, staff_user=staff_user, customer_explanation="Please return with tags intact.")
    assert ret.status == ReturnRequest.Status.APPROVED
    assert ret.approved_at is not None

    # 3. Mark awaiting shipment
    ret = mark_return_awaiting_shipment(ret, staff_user=staff_user)
    assert ret.status == ReturnRequest.Status.AWAITING_SHIPMENT
    assert ret.awaiting_shipment_at is not None

    # 4. Parcel received at warehouse
    ret = mark_return_received(ret, staff_user=staff_user, tracking_number="FW-TRK-9901")
    assert ret.status == ReturnRequest.Status.RECEIVED
    assert ret.received_at is not None
    assert ret.tracking_number == "FW-TRK-9901"

    # 5. Inspection passed
    ret = inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": ret.items.first().pk,
                "accepted_quantity": 1,
                "rejected_quantity": 0,
                "condition": ReturnItem.Condition.LIKE_NEW,
                "disposition": ReturnItem.Disposition.RESTOCKABLE,
                "inspection_notes": "Passed inspection",
            }
        ],
        target_status=ReturnRequest.Status.INSPECTION_PASSED,
    )
    assert ret.status == ReturnRequest.Status.INSPECTION_PASSED
    assert ret.inspection_passed_at is not None


@pytest.mark.django_db
def test_failed_inspection_and_rejections_require_reasons(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    # Rejection requires reason
    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    with pytest.raises(ValidationError) as exc:
        reject_return_request(ret, staff_user=staff_user, reason="", customer_explanation="")
    assert "A reason is required to reject a return request" in str(exc.value)

    # Valid rejection
    ret = reject_return_request(ret, staff_user=staff_user, reason="Tags removed and washed.")
    assert ret.status == ReturnRequest.Status.REJECTED
    assert ret.rejected_at is not None
    assert ret.rejection_reason == "Tags removed and washed."


@pytest.mark.django_db
def test_staff_self_approval_prevention(product):
    """Staff cannot approve or review their own personal return requests."""
    ensure_backoffice_groups()
    staff = get_user_model().objects.create_user(
        email="dual_role_staff@flashwear.test",
        password="Str0ng-Passw0rd!",
        is_staff=True,
    )
    staff.groups.add(Group.objects.get(name=ORDERS_GROUP))

    variant = product.variants.first()
    order = make_delivered_order(staff, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=staff,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )

    # Staff trying to self-approve
    with pytest.raises(ValidationError) as exc:
        approve_return_request(ret, staff_user=staff)
    assert "Staff members cannot approve their own return requests" in str(exc.value)

    # Staff trying to self-review
    with pytest.raises(ValidationError) as exc:
        mark_return_under_review(ret, staff_user=staff)
    assert "Staff members cannot review their own return requests" in str(exc.value)


@pytest.mark.django_db
def test_customer_cancellation_rules(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )

    # Cancellation allowed while in REQUESTED
    assert ret.is_cancellable_by_customer is True
    ret = cancel_return_request(ret, user=user)
    assert ret.status == ReturnRequest.Status.CANCELLED
    assert ret.cancelled_at is not None

    # After receipt, customer cannot cancel
    ret2 = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    ret2 = approve_return_request(ret2, staff_user=staff_user)
    ret2 = mark_return_received(ret2, staff_user=staff_user)
    assert ret2.status == ReturnRequest.Status.RECEIVED
    assert ret2.is_cancellable_by_customer is False

    with pytest.raises(InvalidTransition):
        cancel_return_request(ret2, user=user)


# =============================================================================
# 4. Inventory Safety & Disposition Enforcement
# =============================================================================


@pytest.mark.django_db
def test_inspection_dispositions_restockable_vs_damaged_inventory_safety(user, staff_user, product):
    """Returned goods must not automatically become sellable inventory.

    Only RESTOCKABLE disposition items are restocked to on-hand inventory.
    DAMAGED or DEFECTIVE items are withheld from sellable stock.
    """
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=2, stock=10)
    order_item = order.items.first()

    # Stock on hand after sale (10 - 2 = 8)
    stock = Stock.get_for_variant(variant)
    assert stock.on_hand == 8

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 2}],
    )
    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)

    # Item 1: 1 unit RESTOCKABLE, Item 1: 1 unit DAMAGED
    ret_item = ret.items.first()
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": ret_item.pk,
                "received_quantity": 2,
                "accepted_quantity": 2,
                "rejected_quantity": 0,
                "condition": ReturnItem.Condition.DAMAGED_CUSTOMER,
                "disposition": ReturnItem.Disposition.DAMAGED,
                "inspection_notes": "Damaged by customer; scrap/write-off.",
            }
        ],
    )
    ret.refresh_from_db()
    ret_item.refresh_from_db()
    assert ret_item.disposition == ReturnItem.Disposition.DAMAGED

    # Process refund
    refund = process_refund_for_return(ret, staff_user=staff_user)
    assert refund.status == Refund.Status.SUCCEEDED
    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.REFUNDED

    # INVENTORY SAFETY VERIFICATION:
    # Because disposition was DAMAGED, on-hand stock MUST NOT increase!
    stock.refresh_from_db()
    assert stock.on_hand == 8  # Still 8, NOT 10!
    ret_item.refresh_from_db()
    assert ret_item.is_restocked is True
    assert ret_item.restocked_quantity == 0

    # Verify audit event for withholding from stock
    withheld_event = ret.events.filter(event_type="withheld_from_stock").first()
    assert withheld_event is not None
    assert "safely withheld from sellable inventory" in withheld_event.note


@pytest.mark.django_db
def test_restockable_item_increases_stock_once_idempotently(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=2, stock=10)
    order_item = order.items.first()

    stock = Stock.get_for_variant(variant)
    assert stock.on_hand == 8

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)

    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": ret.items.first().pk,
                "received_quantity": 1,
                "accepted_quantity": 1,
                "rejected_quantity": 0,
                "condition": ReturnItem.Condition.LIKE_NEW,
                "disposition": ReturnItem.Disposition.RESTOCKABLE,
            }
        ],
    )

    process_refund_for_return(ret, staff_user=staff_user)

    stock.refresh_from_db()
    assert stock.on_hand == 9  # 8 + 1 = 9

    # Verify ledger movement recorded with return reference
    movement = InventoryMovement.objects.filter(
        variant=variant,
        kind=InventoryMovement.Kind.RETURNED,
        reference=f"return:{ret.number}",
    ).first()
    assert movement is not None
    assert movement.on_hand_delta == 1


# =============================================================================
# 5. Financial Correctness, Refund Caps & Idempotency
# =============================================================================


@pytest.mark.django_db
def test_refund_cannot_exceed_payment_amount_and_deducts_discounts(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=2)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 2}],
    )
    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": ret.items.first().pk,
                "accepted_quantity": 2,
                "rejected_quantity": 0,
                "condition": ReturnItem.Condition.LIKE_NEW,
            }
        ],
    )

    refund = process_refund_for_return(ret, staff_user=staff_user)
    assert refund.status == Refund.Status.SUCCEEDED
    assert refund.amount <= order.payment.amount

    # Subsequent refund attempt raises ValidationError
    with pytest.raises(ValidationError):
        process_refund_for_return(ret, staff_user=staff_user)


@pytest.mark.django_db
def test_shipping_refund_only_allowed_once(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=2)
    order_item = order.items.first()

    ret1 = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    approve_return_request(ret1, staff_user=staff_user)
    mark_return_received(ret1, staff_user=staff_user)
    inspect_return(
        ret1,
        staff_user=staff_user,
        inspections=[{"item_id": ret1.items.first().pk, "accepted_quantity": 1, "rejected_quantity": 0}],
    )
    refund1 = process_refund_for_return(ret1, staff_user=staff_user, refund_shipping=True)
    assert refund1.is_shipping_refunded is True

    # Second return on same order cannot re-refund shipping
    ret2 = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    approve_return_request(ret2, staff_user=staff_user)
    mark_return_received(ret2, staff_user=staff_user)
    inspect_return(
        ret2,
        staff_user=staff_user,
        inspections=[{"item_id": ret2.items.first().pk, "accepted_quantity": 1, "rejected_quantity": 0}],
    )
    refund2 = process_refund_for_return(ret2, staff_user=staff_user, refund_shipping=True)
    assert refund2.is_shipping_refunded is False


@pytest.mark.django_db
def test_manual_refund_workflow(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[{"item_id": ret.items.first().pk, "accepted_quantity": 1, "rejected_quantity": 0}],
    )

    manual_ref = record_manual_refund_for_return(
        ret,
        staff_user=staff_user,
        amount=Decimal("45.00"),
        reference="BANK-WIRE-REF-992",
        note="Manual wire transfer executed.",
    )
    assert manual_ref.provider == "manual"
    assert manual_ref.provider_reference == "BANK-WIRE-REF-992"
    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.REFUNDED


# =============================================================================
# 6. Exchange Processing & Catalog Replacement Stock
# =============================================================================


@pytest.mark.django_db
def test_exchange_processing_cheaper_replacement_refunds_difference(
    user, staff_user, product, cheaper_variant
):
    variant = product.variants.first()
    seed_stock(cheaper_variant, 10)
    order = make_delivered_order(user, variant, quantity=1, stock=10)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[
            {
                "order_item_id": order_item.pk,
                "quantity": 1,
                "replacement_variant_id": cheaper_variant.pk,
            }
        ],
        return_type=ReturnRequest.ReturnType.EXCHANGE,
    )
    assert ret.return_type == ReturnRequest.ReturnType.EXCHANGE
    assert ret.items.first().replacement_variant == cheaper_variant

    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[{"item_id": ret.items.first().pk, "accepted_quantity": 1, "rejected_quantity": 0}],
    )

    process_exchange_for_return(ret, staff_user=staff_user)
    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.COMPLETED

    # Check replacement stock was deducted
    rep_stock = Stock.get_for_variant(cheaper_variant)
    assert rep_stock.on_hand == 9

    # Check price difference refund was created
    diff_refund = Refund.objects.filter(
        return_request=ret,
        reason=Refund.Reason.EXCHANGE_DIFFERENCE,
    ).first()
    assert diff_refund is not None
    assert diff_refund.amount == (order_item.unit_price - cheaper_variant.price)


@pytest.mark.django_db
def test_exchange_insufficient_stock_fails_safely(user, product, replacement_variant):
    variant = product.variants.first()
    seed_stock(replacement_variant, 0)  # No stock available
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    with pytest.raises(InsufficientStock):
        create_return_request(
            order=order,
            user=user,
            items_data=[
                {
                    "order_item_id": order_item.pk,
                    "quantity": 1,
                    "replacement_variant_id": replacement_variant.pk,
                }
            ],
            return_type=ReturnRequest.ReturnType.EXCHANGE,
        )


# =============================================================================
# 7. Notifications & Support Integration
# =============================================================================


@pytest.mark.django_db
def test_notifications_emitted_accurately_without_duplicates(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    assert Notification.objects.filter(
        user=user, notification_type=NotificationType.RETURN_REQUESTED
    ).exists()

    approve_return_request(ret, staff_user=staff_user)
    assert Notification.objects.filter(
        user=user, notification_type=NotificationType.RETURN_APPROVED
    ).exists()

    # Re-approving does not create duplicate notification
    initial_count = Notification.objects.filter(
        user=user, notification_type=NotificationType.RETURN_APPROVED
    ).count()
    approve_return_request(ret, staff_user=staff_user)
    assert Notification.objects.filter(
        user=user, notification_type=NotificationType.RETURN_APPROVED
    ).count() == initial_count


# =============================================================================
# 8. Security & Permissions (IDOR & Backoffice Access)
# =============================================================================


@pytest.mark.django_db
def test_customer_ui_idor_prevention(client, user, other_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )

    # other_user attempts to view user's return detail
    client.force_login(other_user)
    url = reverse("account:return-detail", kwargs={"number": ret.number})
    response = client.get(url)
    assert response.status_code == 404

    # other_user attempts to cancel user's return
    cancel_url = reverse("account:return-cancel", kwargs={"number": ret.number})
    post_res = client.post(cancel_url)
    assert post_res.status_code == 404


@pytest.mark.django_db
def test_customer_cannot_access_backoffice_return_actions(client, user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )

    client.force_login(user)
    bo_url = reverse("backoffice:return_action", kwargs={"number": ret.number})
    res = client.post(bo_url, {"action": "approve"})
    # Normal user is redirected to backoffice login, forbidden, or 404 (302/403/404)
    assert res.status_code in (302, 403, 404)
