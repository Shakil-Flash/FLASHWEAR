"""Phase 23 Tests: Returns, Exchanges & Refunds.

Validates:
1. Server-side eligibility checks (status, payment, window, limits, duplicate protection).
2. Return state machine (REQUESTED -> APPROVED -> RECEIVED -> INSPECTED -> REFUNDED).
3. Inspection workflow (received, accepted, rejected quantities, condition, note).
4. Exchanges (replacement variant validation, inventory deduction, restock, price difference).
5. Refunds (full, partial, item-level, shipping refund, idempotency, over-refund prevention).
6. Authoritative inventory restocking (auditability, duplicate restock prevention).
7. Loyalty points reversal.
8. Customer UX (return request form, return list, detail, cancellation, IDOR protection).
9. Staff Back Office (list, detail, actions, permissions, audit trail).
10. Notifications integration.
11. REST API endpoints and object-level permissions.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.backoffice.models import AuditEvent
from apps.backoffice.permissions import (
    ORDERS_GROUP,
    ensure_backoffice_groups,
)
from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryMovement, Stock
from apps.notifications.models import Notification, NotificationType
from apps.orders.models import (
    InvalidTransition,
    Order,
    ReturnItem,
    ReturnRequest,
)
from apps.orders.returns_services import (
    approve_return_request,
    cancel_return_request,
    check_order_return_eligibility,
    create_return_request,
    inspect_return,
    mark_return_received,
    process_exchange_for_return,
    process_refund_for_return,
    reject_return_request,
)
from apps.payments.models import Refund
from tests.phase6_helpers import place_and_pay, seed_stock


def make_delivered_order(user, variant, quantity=2, stock=20):
    """Create a fully paid, delivered order with seeded stock."""
    order = place_and_pay(user, variant, quantity=quantity, stock=stock)
    order.transition_to(Order.Status.PROCESSING)
    order.transition_to(Order.Status.SHIPPED)
    order.transition_to(Order.Status.DELIVERED)
    return order


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
        price=Decimal("39.00"),
    )


@pytest.fixture
def staff_user(db):
    ensure_backoffice_groups()
    user = get_user_model().objects.create_user(
        email="staff@flashwear.test",
        password="Str0ng-Passw0rd!",
        is_staff=True,
    )
    user.groups.add(Group.objects.get(name=ORDERS_GROUP))
    return user


# =============================================================================
# 1. Eligibility Checks
# =============================================================================


@pytest.mark.django_db
def test_order_return_eligibility_delivered_order(user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=2)

    eligibility = check_order_return_eligibility(order)
    assert eligibility["is_eligible"] is True
    assert eligibility["days_remaining"] >= 29
    assert len(eligibility["items"]) == 1
    assert eligibility["items"][0]["remaining_quantity"] == 2


@pytest.mark.django_db
def test_order_return_eligibility_unpaid_or_pending(user, product):
    from tests.phase6_helpers import placed_order

    variant = product.variants.first()
    order = placed_order(user, variant, quantity=1)

    eligibility = check_order_return_eligibility(order)
    assert eligibility["is_eligible"] is False
    assert "shipped or delivered" in eligibility["reason"]


@pytest.mark.django_db
def test_order_return_eligibility_window_expired(user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)

    # Move delivered_at back by 35 days
    past = timezone.now() - timedelta(days=35)
    Order.objects.filter(pk=order.pk).update(delivered_at=past, shipped_at=past)
    order.refresh_from_db()

    eligibility = check_order_return_eligibility(order)
    assert eligibility["is_eligible"] is False
    assert "return window" in eligibility["reason"]


@pytest.mark.django_db
def test_partial_quantity_and_duplicate_request_prevention(user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=3)
    order_item = order.items.first()

    # Request 1 of 3
    ret1 = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
        reason=ReturnRequest.Reason.SIZE_FIT,
    )
    assert ret1.status == ReturnRequest.Status.REQUESTED

    # Check eligibility: only 2 remaining
    eligibility = check_order_return_eligibility(order)
    assert eligibility["is_eligible"] is True
    assert eligibility["items"][0]["remaining_quantity"] == 2

    # Request remaining 2
    ret2 = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 2}],
        reason=ReturnRequest.Reason.SIZE_FIT,
    )
    assert ret2.status == ReturnRequest.Status.REQUESTED

    # Check eligibility: 0 remaining -> whole order ineligible
    eligibility2 = check_order_return_eligibility(order)
    assert eligibility2["is_eligible"] is False
    assert "All eligible items on this order have already been returned" in eligibility2["reason"]

    # Trying to create another return should raise ValidationError
    with pytest.raises(ValidationError, match="All eligible items"):
        create_return_request(
            order=order,
            user=user,
            items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
        )


@pytest.mark.django_db
def test_cancelled_return_frees_quantity(user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    # Now 0 remaining
    assert check_order_return_eligibility(order)["is_eligible"] is False

    # Customer cancels request
    ret = cancel_return_request(ret, user=user)
    assert ret.status == ReturnRequest.Status.CANCELLED

    # Check eligibility: item is available again
    assert check_order_return_eligibility(order)["is_eligible"] is True


# =============================================================================
# 2. State Machine Transitions & Audit Trail
# =============================================================================


@pytest.mark.django_db
def test_return_lifecycle_state_transitions(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    assert ret.status == ReturnRequest.Status.REQUESTED

    # Approve
    ret = approve_return_request(ret, staff_user=staff_user, note="Approved for inspection")
    assert ret.status == ReturnRequest.Status.APPROVED
    assert ret.approved_at is not None

    # Receive
    ret = mark_return_received(ret, staff_user=staff_user, tracking_number="TRACK-12345")
    assert ret.status == ReturnRequest.Status.RECEIVED
    assert ret.received_at is not None
    assert ret.tracking_number == "TRACK-12345"

    # Inspect
    ret = inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": ret.items.first().pk,
                "accepted_quantity": 1,
                "rejected_quantity": 0,
                "condition": ReturnItem.Condition.LIKE_NEW,
                "inspection_notes": "Perfect condition",
            }
        ],
    )
    assert ret.status == ReturnRequest.Status.INSPECTED
    assert ret.inspected_at is not None

    # Refund
    refund = process_refund_for_return(ret, staff_user=staff_user)
    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.REFUNDED
    assert ret.refunded_at is not None
    assert refund.status == Refund.Status.SUCCEEDED

    # Verify audit events
    events = list(ret.events.all())
    assert len(events) >= 5
    event_types = [e.event_type for e in events]
    assert "created" in event_types
    assert "status_changed" in event_types


@pytest.mark.django_db
def test_invalid_state_transitions(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )

    # Cannot jump directly to REFUNDED from REQUESTED
    with pytest.raises(InvalidTransition):
        ret.transition_to(ReturnRequest.Status.REFUNDED)

    # Reject
    ret = reject_return_request(ret, staff_user=staff_user, reason="Not eligible")
    assert ret.status == ReturnRequest.Status.REJECTED

    # Cannot approve after reject
    with pytest.raises(InvalidTransition):
        approve_return_request(ret, staff_user=staff_user)


# =============================================================================
# 3. Inspection Workflow & Over-Refund Prevention
# =============================================================================


@pytest.mark.django_db
def test_inspection_partial_accepted_and_rejected(user, staff_user, product):
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

    item = ret.items.first()
    # Inspect: accept 1, reject 1
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": item.pk,
                "accepted_quantity": 1,
                "rejected_quantity": 1,
                "condition": ReturnItem.Condition.DAMAGED_CUSTOMER,
                "inspection_notes": "1 item stained by customer",
            }
        ],
    )
    item.refresh_from_db()
    assert item.accepted_quantity == 1
    assert item.rejected_quantity == 1
    # Refund amount must be 1 * unit_price (49.00), NOT 2 * 49.00
    assert item.refund_amount == Decimal("49.00")

    # Execute refund
    refund = process_refund_for_return(ret, staff_user=staff_user)
    assert refund.amount == Decimal("49.00")


@pytest.mark.django_db
def test_inspection_quantity_mismatch_validation(user, staff_user, product):
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

    item = ret.items.first()
    # Mismatch: 1 accepted + 0 rejected != 2 received
    with pytest.raises(ValidationError, match="must equal received"):
        inspect_return(
            ret,
            staff_user=staff_user,
            inspections=[
                {
                    "item_id": item.pk,
                    "received_quantity": 2,
                    "accepted_quantity": 1,
                    "rejected_quantity": 0,
                }
            ],
        )


# =============================================================================
# 4. Inventory Restocking & Idempotency
# =============================================================================


@pytest.mark.django_db
def test_inventory_restocking_and_duplicate_prevention(user, staff_user, product):
    variant = product.variants.first()
    seed_stock(variant, on_hand=10)
    order = make_delivered_order(user, variant, quantity=2, stock=None)
    order_item = order.items.first()

    # Initial stock on hand was 10, 2 were fulfilled -> 8 on hand
    stock = Stock.get_for_variant(variant)
    initial_on_hand = stock.on_hand

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

    # Process refund -> should restock +2
    process_refund_for_return(ret, staff_user=staff_user)

    stock.refresh_from_db()
    assert stock.on_hand == initial_on_hand + 2

    # Verify inventory movement recorded with RETURNED kind
    movement = InventoryMovement.objects.filter(
        variant=variant, kind=InventoryMovement.Kind.RETURNED
    ).first()
    assert movement is not None
    assert movement.on_hand_delta == 2

    item = ret.items.first()
    assert item.is_restocked is True

    # Re-running process_refund_for_return must fail (idempotency guard)
    with pytest.raises(ValidationError, match="Return must be INSPECTED before issuing a refund"):
        process_refund_for_return(ret, staff_user=staff_user)

    # Stock must not have increased again
    stock.refresh_from_db()
    assert stock.on_hand == initial_on_hand + 2


# =============================================================================
# 5. Exchanges
# =============================================================================


@pytest.mark.django_db
def test_exchange_processing_and_stock_swap(user, staff_user, product, replacement_variant):
    orig_variant = product.variants.first()
    seed_stock(orig_variant, on_hand=5)
    seed_stock(replacement_variant, on_hand=5)

    order = make_delivered_order(user, orig_variant, quantity=1, stock=None)
    order_item = order.items.first()

    ret = create_return_request(
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
    assert ret.return_type == ReturnRequest.ReturnType.EXCHANGE
    assert ret.items.first().replacement_variant == replacement_variant

    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[
            {
                "item_id": ret.items.first().pk,
                "accepted_quantity": 1,
                "rejected_quantity": 0,
                "condition": ReturnItem.Condition.LIKE_NEW,
            }
        ],
    )

    # Process exchange
    orig_stock_before = Stock.get_for_variant(orig_variant).on_hand
    repl_stock_before = Stock.get_for_variant(replacement_variant).on_hand

    ret = process_exchange_for_return(ret, staff_user=staff_user)

    assert ret.status == ReturnRequest.Status.COMPLETED
    assert ret.completed_at is not None

    # Original returned variant restocked (+1)
    assert Stock.get_for_variant(orig_variant).on_hand == orig_stock_before + 1
    # Replacement variant fulfilled (-1)
    assert Stock.get_for_variant(replacement_variant).on_hand == repl_stock_before - 1


@pytest.mark.django_db
def test_exchange_price_difference_refund(user, staff_user, product, cheaper_variant):
    orig_variant = product.variants.first()  # 49.00
    seed_stock(cheaper_variant, on_hand=5)  # 39.00

    order = make_delivered_order(user, orig_variant, quantity=1)
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
    item = ret.items.first()
    # Price difference is 39 - 49 = -10.00
    assert item.price_difference == Decimal("-10.00")

    approve_return_request(ret, staff_user=staff_user)
    mark_return_received(ret, staff_user=staff_user)
    inspect_return(
        ret,
        staff_user=staff_user,
        inspections=[{"item_id": item.pk, "accepted_quantity": 1, "rejected_quantity": 0}],
    )

    process_exchange_for_return(ret, staff_user=staff_user)

    # A refund of 10.00 should be issued for the difference
    refund = ret.refunds.first()
    assert refund is not None
    assert refund.amount == Decimal("10.00")
    assert refund.status == Refund.Status.SUCCEEDED


# =============================================================================
# 6. Loyalty Points Reversal
# =============================================================================


@pytest.mark.django_db
def test_loyalty_points_reversal_on_refund(user, staff_user, product):
    from apps.engagement.services.loyalty import balance_for, earn_points_for_order

    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    # Credit loyalty points for the order (e.g. 50 points)
    earn_points_for_order(order)
    initial_balance = balance_for(user)
    assert initial_balance > 0

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
                "accepted_quantity": 1,
                "rejected_quantity": 0,
            }
        ],
    )

    process_refund_for_return(ret, staff_user=staff_user)

    # Balance after full order refund should be 0 (reversed)
    new_balance = balance_for(user)
    assert new_balance == 0


# =============================================================================
# 7. Customer UX & IDOR Protection
# =============================================================================


@pytest.mark.django_db
def test_customer_ux_return_flow_and_idor(client, user, other_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    # Sign in as user
    client.force_login(user)

    # GET return request page
    resp = client.get(reverse("account:order-return-request", kwargs={"number": order.number}))
    assert resp.status_code == 200

    # POST return request
    resp = client.post(
        reverse("account:order-return-request", kwargs={"number": order.number}),
        {
            "return_type": ReturnRequest.ReturnType.REFUND,
            "reason": ReturnRequest.Reason.SIZE_FIT,
            "customer_note": "Too tight in the shoulders",
            f"item_{order_item.pk}_selected": "1",
            f"item_{order_item.pk}_quantity": "1",
        },
    )
    assert resp.status_code == 302
    ret = ReturnRequest.objects.get(order=order)
    assert ret.customer_note == "Too tight in the shoulders"

    # User can view return list
    resp = client.get(reverse("account:returns"))
    assert resp.status_code == 200
    assert ret.number in resp.content.decode()

    # User can view return detail
    resp = client.get(reverse("account:return-detail", kwargs={"number": ret.number}))
    assert resp.status_code == 200
    assert ret.number in resp.content.decode()

    # IDOR Check: other_user cannot view or cancel this return
    client.force_login(other_user)
    resp = client.get(reverse("account:return-detail", kwargs={"number": ret.number}))
    assert resp.status_code == 404

    resp = client.post(reverse("account:return-cancel", kwargs={"number": ret.number}))
    assert resp.status_code == 404

    # Sign back in and cancel
    client.force_login(user)
    resp = client.post(reverse("account:return-cancel", kwargs={"number": ret.number}))
    assert resp.status_code == 302
    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.CANCELLED


# =============================================================================
# 8. Staff Back Office Actions & Permissions
# =============================================================================


@pytest.mark.django_db
def test_backoffice_returns_permissions_and_actions(client, staff_user, user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()
    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )

    # Plain customer cannot access backoffice (404)
    client.force_login(user)
    resp = client.get(reverse("backoffice:returns"))
    assert resp.status_code == 404

    # Staff user with ORDERS_VIEW can access
    client.force_login(staff_user)
    resp = client.get(reverse("backoffice:returns"))
    assert resp.status_code == 200
    assert ret.number in resp.content.decode()

    # Staff views detail
    resp = client.get(reverse("backoffice:return_detail", kwargs={"number": ret.number}))
    assert resp.status_code == 200

    # Staff approves return via action
    resp = client.post(
        reverse("backoffice:return_action", kwargs={"number": ret.number}),
        {"action": "approve", "note": "Approved by backoffice"},
    )
    assert resp.status_code == 302
    ret.refresh_from_db()
    assert ret.status == ReturnRequest.Status.APPROVED

    # Audit event was recorded
    audit = AuditEvent.objects.filter(object_id=str(ret.pk), action="return.approve").first()
    assert audit is not None
    assert audit.actor == staff_user


# =============================================================================
# 9. Notifications Integration
# =============================================================================


@pytest.mark.django_db
def test_notifications_emitted_for_return_lifecycle(user, staff_user, product):
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=1)
    order_item = order.items.first()

    # Clear prior notifications
    Notification.objects.filter(user=user).delete()

    ret = create_return_request(
        order=order,
        user=user,
        items_data=[{"order_item_id": order_item.pk, "quantity": 1}],
    )
    # Check notification for RETURN_REQUESTED
    n = Notification.objects.filter(
        user=user, notification_type=NotificationType.RETURN_REQUESTED
    ).first()
    assert n is not None

    approve_return_request(ret, staff_user=staff_user)
    n = Notification.objects.filter(
        user=user, notification_type=NotificationType.RETURN_APPROVED
    ).first()
    assert n is not None


# =============================================================================
# 10. REST API Endpoints
# =============================================================================


@pytest.mark.django_db
def test_returns_rest_api(user, other_user, product):
    api_client = APIClient()
    variant = product.variants.first()
    order = make_delivered_order(user, variant, quantity=2)
    order_item = order.items.first()

    # Authenticate as user
    api_client.force_authenticate(user=user)

    # 1. Eligibility endpoint
    url = reverse("v1:order-return-eligibility", kwargs={"number": order.number})
    resp = api_client.get(url)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["is_eligible"] is True
    assert resp.data["items"][0]["remaining_quantity"] == 2

    # 2. Create return endpoint
    url = reverse("v1:order-return-create", kwargs={"number": order.number})
    payload = {
        "return_type": "refund",
        "reason": "size_fit",
        "customer_note": "A bit too large",
        "items": [{"order_item_id": order_item.pk, "quantity": 1}],
    }
    resp = api_client.post(url, payload, format="json")
    assert resp.status_code == status.HTTP_201_CREATED
    ret_number = resp.data["number"]
    assert ret_number.startswith("RET-")

    # 3. List returns endpoint
    url = reverse("v1:return-list")
    resp = api_client.get(url)
    assert resp.status_code == status.HTTP_200_OK
    assert any(item["number"] == ret_number for item in resp.data["results"])

    # 4. Detail return endpoint
    url = reverse("v1:return-detail", kwargs={"number": ret_number})
    resp = api_client.get(url)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["order_number"] == order.number

    # 5. Cancel return endpoint
    url = reverse("v1:return-cancel", kwargs={"number": ret_number})
    resp = api_client.post(url, {"note": "Cancelled via API"}, format="json")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["status"] == "cancelled"

    # IDOR Check: other_user gets 404 for this return
    api_client.force_authenticate(user=other_user)
    url = reverse("v1:return-detail", kwargs={"number": ret_number})
    resp = api_client.get(url)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
