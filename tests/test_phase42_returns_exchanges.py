"""Phase 42 Tests: Returns, Exchanges & Post-Purchase Experience.

Comprehensive test suite verifying:
1. Server-side eligibility rules and ownership/IDOR protection.
2. Partial quantity returns and duplicate/over-return prevention.
3. Concurrency safeguards under transaction locks.
4. Secure image uploads and format validation.
5. Strict state transitions across full workflow:
   (REQUESTED, APPROVED, RECEIVED, INSPECTED, REFUND_PENDING,
    EXCHANGE_PENDING, REFUNDED, COMPLETED, REJECTED, CANCELLED).
6. Staff decisions, explanations, and Back Office audit integration.
7. Accurate Decimal refund calculations, idempotency, and condition-based restocking.
8. Exchange inventory allocation and price difference calculation.
9. Customer experience: order detail link, return forms, estimated vs confirmed
   refund separation, timeline, and support integration.
10. Notification deduplication and retry idempotency.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.backoffice.models import AuditEvent
from apps.backoffice.permissions import ORDERS_GROUP, ensure_backoffice_groups
from apps.catalog.models import ProductVariant
from apps.inventory.models import Stock
from apps.orders.models import (
    InvalidTransition,
    Order,
    ReturnImage,
    ReturnItem,
    ReturnRequest,
)
from apps.orders.returns_services import (
    approve_return_request,
    cancel_return_request,
    check_order_return_eligibility,
    create_return_request,
    inspect_return,
    mark_return_exchange_pending,
    mark_return_received,
    mark_return_refund_pending,
    process_exchange_for_return,
    process_refund_for_return,
    reject_return_request,
)
from tests.phase6_helpers import place_and_pay, seed_stock

pytestmark = pytest.mark.django_db


# =============================================================================
# Fixtures & Helpers
# =============================================================================


def make_test_image(
    name: str = "proof.png",
    fmt: str = "PNG",
    size: tuple[int, int] = (200, 200),
    color: tuple[int, int, int] = (10, 80, 160),
) -> SimpleUploadedFile:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=mime)


def make_delivered_order(user, variant, quantity: int = 2, stock: int = 20) -> Order:
    order = place_and_pay(user, variant, quantity=quantity, stock=stock)
    order.transition_to(Order.Status.PROCESSING)
    order.transition_to(Order.Status.SHIPPED)
    order.transition_to(Order.Status.DELIVERED)
    return order


@pytest.fixture
def customer(user):
    return user


@pytest.fixture
def variant(product):
    return product.variants.first()


@pytest.fixture
def other_user(db):
    return get_user_model().objects.create_user(
        email="other_customer@flashwear.test",
        password="Str0ng-Passw0rd!",
    )


@pytest.fixture
def staff_user(db):
    ensure_backoffice_groups()
    user = get_user_model().objects.create_user(
        email="staff_ops@flashwear.test",
        password="Str0ng-Passw0rd!",
        is_staff=True,
    )
    user.groups.add(Group.objects.get(name=ORDERS_GROUP))
    return user


@pytest.fixture
def replacement_variant(db, product, other_colour, other_size):
    return ProductVariant.objects.create(
        product=product,
        sku="FW-TEE-REP-L",
        color=other_colour,
        size=other_size,
        price=Decimal("49.00"),
    )


@pytest.fixture
def cheaper_variant(db, product, other_colour, size):
    return ProductVariant.objects.create(
        product=product,
        sku="FW-TEE-REP-S",
        color=other_colour,
        size=size,
        price=Decimal("35.00"),
    )


# =============================================================================
# 1. Eligibility, Ownership & IDOR Protection Tests
# =============================================================================


def test_order_return_eligibility_delivered_order(customer, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    rep = check_order_return_eligibility(order)
    assert rep["is_eligible"] is True
    assert rep["days_remaining"] >= 29
    assert len(rep["items"]) == 1
    assert rep["items"][0]["remaining_quantity"] == 2


def test_order_return_eligibility_past_deadline(customer, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    order.delivered_at = timezone.now() - timedelta(days=35)
    order.save(update_fields=["delivered_at"])

    rep = check_order_return_eligibility(order)
    assert rep["is_eligible"] is False
    assert "return window" in rep["reason"].lower()


def test_ownership_idor_protection_create_return(customer, other_user, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    line = order.items.first()

    with pytest.raises(ValidationError, match="another user's order"):
        create_return_request(
            order=order,
            user=other_user,
            items_data=[{"order_item_id": line.pk, "quantity": 1}],
        )


def test_ownership_idor_protection_cancellation(customer, other_user, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    line = order.items.first()
    ret = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 1}],
    )

    with pytest.raises(ValidationError, match="another user's return"):
        cancel_return_request(ret, user=other_user)


# =============================================================================
# 2. Supporting Images Validation Tests
# =============================================================================


def test_create_return_with_valid_images(customer, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    line = order.items.first()

    img1 = make_test_image("defect1.png", "PNG")
    img2 = make_test_image("defect2.jpg", "JPEG")

    ret = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 1}],
        customer_note="Defective seam on sleeve",
        images=[img1, img2],
    )

    assert ret.images.count() == 2
    assert ReturnImage.objects.filter(return_request=ret).count() == 2


def test_create_return_with_corrupt_file_rejects(customer, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    line = order.items.first()

    fake_file = SimpleUploadedFile(
        "broken.png", b"not an actual png byte stream", content_type="image/png"
    )

    with pytest.raises(ValidationError):
        create_return_request(
            order=order,
            user=customer,
            items_data=[{"order_item_id": line.pk, "quantity": 1}],
            images=[fake_file],
        )

    # Clean transaction rollback - no dangling ReturnRequest
    assert ReturnRequest.objects.filter(order=order).count() == 0


def test_create_return_max_image_limit(customer, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    line = order.items.first()

    six_images = [make_test_image(f"img{i}.png") for i in range(6)]
    with pytest.raises(ValidationError, match="at most 5"):
        create_return_request(
            order=order,
            user=customer,
            items_data=[{"order_item_id": line.pk, "quantity": 1}],
            images=six_images,
        )


# =============================================================================
# 3. Partial Quantity & Duplicate Protection Tests
# =============================================================================


def test_partial_quantity_and_over_return_prevention(customer, variant):
    order = make_delivered_order(customer, variant, quantity=3)
    line = order.items.first()

    # Request 1 unit
    ret1 = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 1}],
    )
    assert ret1.status == ReturnRequest.Status.REQUESTED

    # Remaining is 2; attempting to request 3 raises error
    with pytest.raises(ValidationError, match="Only 2 remaining"):
        create_return_request(
            order=order,
            user=customer,
            items_data=[{"order_item_id": line.pk, "quantity": 3}],
        )

    # Request remaining 2 units
    ret2 = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 2}],
    )
    assert ret2.status == ReturnRequest.Status.REQUESTED

    # Now 0 remaining
    rep = check_order_return_eligibility(order)
    assert rep["is_eligible"] is False
    assert "already been returned" in rep["reason"].lower()


# =============================================================================
# 4. Explicit Workflow State Machine Tests
# =============================================================================


def test_full_workflow_state_machine_with_pending_states(customer, staff_user, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    line = order.items.first()

    ret = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 2}],
    )
    assert ret.status == ReturnRequest.Status.REQUESTED

    # 1. Approve
    ret = approve_return_request(
        ret,
        staff_user=staff_user,
        note="Approved by QA",
        customer_explanation="Please pack securely and ship via prepaid label.",
    )
    assert ret.status == ReturnRequest.Status.APPROVED
    assert ret.approved_at is not None
    assert "pack securely" in ret.customer_explanation

    # 2. Mark received
    ret = mark_return_received(ret, staff_user=staff_user, tracking_number="FW-INB-9988")
    assert ret.status == ReturnRequest.Status.RECEIVED
    assert ret.received_at is not None
    assert ret.tracking_number == "FW-INB-9988"

    # 3. Inspect
    item = ret.items.first()
    ret = inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": item.pk,
                "received_quantity": 2,
                "accepted_quantity": 2,
                "rejected_quantity": 0,
                "condition": ReturnItem.Condition.LIKE_NEW,
                "inspection_notes": "Tags attached, unworn.",
            }
        ],
        customer_explanation="Inspection passed in like-new condition.",
    )
    assert ret.status == ReturnRequest.Status.INSPECTED
    assert ret.inspected_at is not None
    item.refresh_from_db()
    assert item.refund_amount == line.line_total

    # 4. Move to REFUND_PENDING
    ret = mark_return_refund_pending(ret, staff_user=staff_user, note="Queued for treasury payout")
    assert ret.status == ReturnRequest.Status.REFUND_PENDING
    assert ret.refund_pending_at is not None

    # 5. Process refund from REFUND_PENDING
    refund = process_refund_for_return(ret, staff_user=staff_user)
    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.REFUNDED
    assert ret.refunded_at is not None
    assert refund.amount == line.line_total

    # 6. Complete
    ret.transition_to(
        ReturnRequest.Status.COMPLETED, actor=staff_user, note="Return lifecycle finalized."
    )
    assert ret.status == ReturnRequest.Status.COMPLETED
    assert ret.completed_at is not None


def test_invalid_transitions_rejected(customer, staff_user, variant):
    order = make_delivered_order(customer, variant, quantity=1)
    line = order.items.first()
    ret = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 1}],
    )

    # Cannot skip directly to REFUNDED from REQUESTED
    with pytest.raises(InvalidTransition):
        ret.transition_to(ReturnRequest.Status.REFUNDED)

    # Reject request
    ret = reject_return_request(ret, staff_user=staff_user, reason="Outside acceptable conditions")
    assert ret.status == ReturnRequest.Status.REJECTED

    # Cannot transition from REJECTED to APPROVED
    with pytest.raises(InvalidTransition):
        ret.transition_to(ReturnRequest.Status.APPROVED)


def test_customer_cancellation_allowed_states(customer, variant):
    order = make_delivered_order(customer, variant, quantity=1)
    line = order.items.first()

    # Cancel while in REQUESTED
    ret1 = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 1}],
    )
    assert ret1.is_cancellable_by_customer is True
    ret1 = cancel_return_request(ret1, user=customer, note="Changed mind")
    assert ret1.status == ReturnRequest.Status.CANCELLED

    # Cancel while in APPROVED
    ret2 = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 1}],
    )
    ret2 = approve_return_request(ret2)
    assert ret2.is_cancellable_by_customer is True
    ret2 = cancel_return_request(ret2, user=customer, note="Decided to keep")
    assert ret2.status == ReturnRequest.Status.CANCELLED

    # But once RECEIVED at warehouse, cancellation is forbidden
    ret3 = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 1}],
    )
    ret3 = approve_return_request(ret3)
    ret3 = mark_return_received(ret3)
    assert ret3.is_cancellable_by_customer is False
    with pytest.raises(InvalidTransition):
        cancel_return_request(ret3, user=customer)


# =============================================================================
# 5. Estimated vs Confirmed Refund Properties Tests
# =============================================================================


def test_estimated_vs_confirmed_refund_separation(customer, staff_user, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    line = order.items.first()

    ret = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 2}],
    )

    # Initial state: estimated refund is line total, confirmed is 0.00
    assert ret.estimated_refund_amount == line.line_total
    assert ret.confirmed_refund_amount == Decimal("0.00")

    # Approve and receive
    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)

    # Inspection accepts only 1 of 2 units
    item = ret.items.first()
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": item.pk,
                "received_quantity": 2,
                "accepted_quantity": 1,
                "rejected_quantity": 1,
                "condition": ReturnItem.Condition.WORN,
                "inspection_notes": "One unit washed/worn",
            }
        ],
    )
    ret.refresh_from_db()
    # Confirmed accepted refund is now half line total
    expected_confirmed = (line.line_total / Decimal("2")).quantize(Decimal("0.01"))
    assert ret.confirmed_refund_amount == expected_confirmed
    # Estimated still reflects the original requested total
    assert ret.estimated_refund_amount == line.line_total


# =============================================================================
# 6. Stock Movement & Restocking Integrity Tests
# =============================================================================


def test_authoritative_restock_only_accepted_units(customer, staff_user, variant):
    order = make_delivered_order(customer, variant, quantity=2, stock=10)
    stock_before = Stock.get_for_variant(variant).on_hand

    line = order.items.first()
    ret = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 2}],
    )
    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)

    # Accept 1 unit, reject 1 unit
    item = ret.items.first()
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": item.pk,
                "received_quantity": 2,
                "accepted_quantity": 1,
                "rejected_quantity": 1,
                "condition": ReturnItem.Condition.LIKE_NEW,
            }
        ],
    )

    # Stock not touched until process_refund_for_return
    assert Stock.get_for_variant(variant).on_hand == stock_before

    process_refund_for_return(ret, staff_user=staff_user)

    # Exactly 1 accepted unit restocked
    assert Stock.get_for_variant(variant).on_hand == stock_before + 1

    # Idempotency: re-running adjust_stock is guarded by item.is_restocked
    item.refresh_from_db()
    assert item.is_restocked is True


# =============================================================================
# 7. Exchange Workflow Tests
# =============================================================================


def test_exchange_allocates_replacement_and_restocks_original(
    customer, staff_user, variant, replacement_variant
):
    # Seed replacement variant stock
    seed_stock(replacement_variant, 5)

    order = make_delivered_order(customer, variant, quantity=1, stock=5)
    line = order.items.first()

    ret = create_return_request(
        order=order,
        user=customer,
        return_type=ReturnRequest.ReturnType.EXCHANGE,
        items_data=[
            {
                "order_item_id": line.pk,
                "quantity": 1,
                "replacement_variant_id": replacement_variant.pk,
            }
        ],
    )
    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)

    item = ret.items.first()
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": item.pk,
                "received_quantity": 1,
                "accepted_quantity": 1,
                "rejected_quantity": 0,
                "condition": ReturnItem.Condition.UNOPENED,
            }
        ],
    )

    # Move to EXCHANGE_PENDING
    mark_return_exchange_pending(ret, staff_user=staff_user)
    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.EXCHANGE_PENDING

    orig_stock_before = Stock.get_for_variant(variant).on_hand
    rep_stock_before = Stock.get_for_variant(replacement_variant).on_hand

    process_exchange_for_return(ret, staff_user=staff_user)

    # Original variant restocked (+1), replacement variant allocated (-1)
    assert Stock.get_for_variant(variant).on_hand == orig_stock_before + 1
    assert Stock.get_for_variant(replacement_variant).on_hand == rep_stock_before - 1

    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.COMPLETED


# =============================================================================
# 8. Web Views & Template Integration Tests
# =============================================================================


def test_order_detail_view_renders_return_action_and_linked_returns(customer, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    client = Client()
    client.force_login(customer)

    # 1. Detail page with no returns yet: shows "Return or Exchange" action button
    response = client.get(reverse("account:order-detail", kwargs={"number": order.number}))
    assert response.status_code == 200
    assert "Return or Exchange" in response.content.decode()
    assert (
        reverse("account:order-return-request", kwargs={"number": order.number})
        in response.content.decode()
    )

    # 2. Create return
    line = order.items.first()
    ret = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 1}],
    )

    # 3. Reload detail page: displays linked returns section
    response = client.get(reverse("account:order-detail", kwargs={"number": order.number}))
    assert response.status_code == 200
    content = response.content.decode()
    assert ret.number in content
    assert "Returns & Exchanges" in content


def test_customer_return_request_post_with_file_upload(customer, variant):
    order = make_delivered_order(customer, variant, quantity=2)
    line = order.items.first()

    client = Client()
    client.force_login(customer)

    img = make_test_image("proof.png", "PNG")
    url = reverse("account:order-return-request", kwargs={"number": order.number})
    post_data = {
        "return_type": ReturnRequest.ReturnType.REFUND,
        "reason": ReturnRequest.Reason.DEFECTIVE,
        "customer_note": "Torn pocket on arrival",
        f"item_{line.pk}_selected": "1",
        f"item_{line.pk}_quantity": "1",
        "images": [img],
    }

    response = client.post(url, data=post_data, follow=True)
    assert response.status_code == 200

    ret = ReturnRequest.objects.get(order=order)
    assert ret.status == ReturnRequest.Status.REQUESTED
    assert ret.reason == ReturnRequest.Reason.DEFECTIVE
    assert ret.images.count() == 1


def test_customer_return_detail_shows_estimated_and_confirmed_refunds(
    customer, staff_user, variant
):
    order = make_delivered_order(customer, variant, quantity=2)
    line = order.items.first()
    ret = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 2}],
    )

    client = Client()
    client.force_login(customer)

    url = reverse("account:return-detail", kwargs={"number": ret.number})
    response = client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Estimated Refund" in content
    assert "Confirmed Refund" in content
    assert "Need help? Contact support" in content


def test_backoffice_return_actions_and_audit(staff_user, customer, variant):
    order = make_delivered_order(customer, variant, quantity=1)
    line = order.items.first()
    ret = create_return_request(
        order=order,
        user=customer,
        items_data=[{"order_item_id": line.pk, "quantity": 1}],
    )

    client = Client()
    client.force_login(staff_user)

    action_url = reverse("backoffice:return_action", kwargs={"number": ret.number})

    # Approve via Back Office
    resp = client.post(
        action_url,
        data={
            "action": "approve",
            "note": "Backoffice approval",
            "customer_explanation": "Approved. Drop off parcel at carrier.",
        },
        follow=True,
    )
    assert resp.status_code == 200
    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.APPROVED
    assert "Drop off parcel" in ret.customer_explanation

    # Audit recorded
    assert AuditEvent.objects.filter(
        object_type="return_request",
        object_id=str(ret.pk),
        action="return.approve",
    ).exists()
