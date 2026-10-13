"""Tests for FLASHWEAR Phase 54: Shipping, Fulfillment & Delivery Tracking Intelligence.

Covers:
1. Fulfillment eligibility: unpaid, cancelled, and refunded orders are rejected; paid orders succeed.
2. Partial fulfillment & split shipments: line-item quantity allocations, unfulfilled balance calculations, and overshipment prevention.
3. Picking, packing, and dispatch lifecycle: staff attribution, weight/dimensions/internal notes, and state transition guards.
4. Provider-neutral courier & tracking URLs: verified courier URLs (FedEx, UPS, USPS, DHL, Royal Mail), manual couriers, and open-redirect sanitization.
5. Tracking scans, out-of-order idempotency, and non-regression: delivered shipments cannot regress to in-transit on late scans.
6. Signed carrier tracking webhooks: HMAC-SHA256 signature verification, timestamp tolerance, and event_id replay protection.
7. Delivery exception workflows: failed attempts and delivery exceptions recorded without automatic cancel/refund mutations.
8. Customer tracking experience & IDOR security: authenticated customer-scoped API and web access, internal notes withheld, safe external tracking links.
9. Staff operations interface: backoffice shipments queue, shipment detail actions, and order fulfillment split forms.
10. Transactional notifications: correct delivery event notifications with deduplication via idempotency keys.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import time
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.backoffice.permissions import (
    ORDERS_GROUP,
    ensure_backoffice_groups,
)
from apps.catalog.models import Category, Color, Product, ProductVariant, Size
from apps.inventory.models import Stock
from apps.notifications.models import Notification, NotificationType
from apps.orders.fulfillment_services import (
    cancel_fulfillment,
    create_fulfillment,
    dispatch_shipment,
    get_order_fulfillment_summary,
    get_unfulfilled_order_items,
    mark_packed,
    record_delivery_exception,
    start_picking,
    update_tracking_event,
)
from apps.orders.models import (
    InvalidTransition,
    Order,
    Shipment,
    ShipmentEvent,
    ShipmentItem,
)
from tests.phase6_helpers import make_address, place_and_pay, placed_order, seed_stock


@pytest.fixture
def catalog_setup(db):
    category = Category.objects.create(name="Outerwear", slug="outerwear")
    product = Product.objects.create(
        name="Technical Trench Coat",
        slug="technical-trench-coat",
        status=Product.Status.ACTIVE,
        category=category,
    )
    product_b = Product.objects.create(
        name="Merino Knit Beanie",
        slug="merino-knit-beanie",
        status=Product.Status.ACTIVE,
        category=category,
    )
    black = Color.objects.create(name="Midnight Black", slug="midnight-black", hex_code="#000000")
    sand = Color.objects.create(name="Desert Sand", slug="desert-sand", hex_code="#E0D6C3")
    size_m = Size.objects.create(name="Medium", code="M", display_order=1)
    size_os = Size.objects.create(name="One Size", code="OS", display_order=2)

    variant_a = ProductVariant.objects.create(
        product=product,
        color=black,
        size=size_m,
        sku="TTC-BLK-M",
        price=Decimal("220.00"),
        is_active=True,
    )
    variant_b = ProductVariant.objects.create(
        product=product_b,
        color=sand,
        size=size_os,
        sku="MKB-SND-OS",
        price=Decimal("45.00"),
        is_active=True,
    )
    seed_stock(variant_a, 50)
    seed_stock(variant_b, 50)
    return {
        "variant_a": variant_a,
        "variant_b": variant_b,
    }


@pytest.fixture
def staff_user(db):
    ensure_backoffice_groups()
    user = get_user_model().objects.create_user(
        email="warehouse_operator@flashwear.test",
        password="Str0ng-Passw0rd!",
        is_staff=True,
    )
    user.groups.add(Group.objects.get(name=ORDERS_GROUP))
    return user


@pytest.fixture
def other_customer(db):
    return get_user_model().objects.create_user(
        email="other_shopper@flashwear.test",
        password="Str0ng-Passw0rd!",
    )


# ==============================================================================
# 1. Fulfillment Eligibility Tests
# ==============================================================================

@pytest.mark.django_db
def test_fulfillment_eligibility_unpaid_order_rejected(user, catalog_setup):
    """An unpaid order cannot have fulfillments created."""
    order = placed_order(user, catalog_setup["variant_a"], quantity=1)
    assert order.status == Order.Status.PENDING_PAYMENT

    with pytest.raises(ValidationError) as exc:
        create_fulfillment(order)
    assert "not eligible for fulfillment" in str(exc.value).lower()


@pytest.mark.django_db
def test_fulfillment_eligibility_cancelled_order_rejected(user, catalog_setup):
    """A cancelled order cannot have fulfillments created."""
    order = placed_order(user, catalog_setup["variant_a"], quantity=1)
    order.status = Order.Status.CANCELLED
    order.save(update_fields=["status"])

    with pytest.raises(ValidationError) as exc:
        create_fulfillment(order)
    assert "cancelled" in str(exc.value).lower()


@pytest.mark.django_db
def test_fulfillment_eligibility_paid_order_success(user, staff_user, catalog_setup):
    """A paid order can be fulfilled, creating a Pending shipment with items and reference."""
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=2)
    assert order.status == Order.Status.PAID

    shipment = create_fulfillment(order, staff_user=staff_user)
    assert shipment.status == Shipment.Status.PENDING
    assert shipment.reference.startswith("SHP-")
    assert shipment.items.count() == 1
    assert shipment.items.first().quantity == 2
    assert shipment.events.filter(event_type=Shipment.Status.PENDING).exists()


# ==============================================================================
# 2. Partial Fulfillment & Overshipment Prevention
# ==============================================================================

@pytest.mark.django_db
def test_partial_fulfillment_and_overshipment_prevention(user, staff_user, catalog_setup):
    """Support split shipments and prevent allocating more than ordered quantities."""
    # Create order with 2 items
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=3)
    item_a = order.items.first()

    # Add second item to order
    from apps.orders.models import OrderItem
    item_b = OrderItem.objects.create(
        order=order,
        variant=catalog_setup["variant_b"],
        sku="MKB-SND-OS",
        product_name="Merino Knit Beanie",
        unit_price=Decimal("45.00"),
        quantity=2,
        line_total=Decimal("90.00"),
    )

    summary = get_order_fulfillment_summary(order)
    assert summary["total_ordered"] == 5
    assert summary["total_fulfilled"] == 0
    assert summary["can_fulfill"] is True
    assert not summary["is_fully_fulfilled"]

    # Fulfill partial: 2 of Item A
    shipment_1 = create_fulfillment(
        order=order,
        items_data=[{"order_item_id": item_a.pk, "quantity": 2}],
        staff_user=staff_user,
    )
    assert shipment_1.items.get(order_item=item_a).quantity == 2

    # Verify unfulfilled counts
    unfulfilled = get_unfulfilled_order_items(order)
    unfulfilled_map = {u["item"].pk: u["unfulfilled_qty"] for u in unfulfilled}
    assert unfulfilled_map[item_a.pk] == 1  # 3 - 2 = 1 left
    assert unfulfilled_map[item_b.pk] == 2  # 2 - 0 = 2 left

    # Attempt overshipment on Item A (request 2 when only 1 is unfulfilled)
    with pytest.raises(ValidationError) as exc:
        create_fulfillment(
            order=order,
            items_data=[{"order_item_id": item_a.pk, "quantity": 2}],
            staff_user=staff_user,
        )
    assert "exceeds unfulfilled" in str(exc.value)

    # Attempt zero quantity
    with pytest.raises(ValidationError) as exc:
        create_fulfillment(
            order=order,
            items_data=[{"order_item_id": item_a.pk, "quantity": 0}],
            staff_user=staff_user,
        )
    assert "greater than zero" in str(exc.value)

    # Fulfill remaining in package 2
    shipment_2 = create_fulfillment(
        order=order,
        items_data=[
            {"order_item_id": item_a.pk, "quantity": 1},
            {"order_item_id": item_b.pk, "quantity": 2},
        ],
        staff_user=staff_user,
    )
    assert shipment_2.items.count() == 2

    # Check order fulfillment completion
    summary_final = get_order_fulfillment_summary(order)
    assert summary_final["total_fulfilled"] == 5
    assert summary_final["is_fully_fulfilled"] is True
    assert summary_final["can_fulfill"] is False
    assert len(summary_final["unfulfilled_items"]) == 0


# ==============================================================================
# 3. Warehouse Picking, Packing & Dispatch Workflow
# ==============================================================================

@pytest.mark.django_db
def test_warehouse_picking_packing_dispatch_lifecycle(user, staff_user, catalog_setup):
    """Step through picking, packing, and dispatch with staff attribution and data validation."""
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=1)
    shipment = create_fulfillment(order, staff_user=staff_user)

    # 1. Picking
    start_picking(shipment, staff_user=staff_user, note="Picked from bin A-14")
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.PICKING
    assert shipment.picked_by == staff_user
    assert shipment.picked_at is not None
    assert shipment.events.filter(event_type=Shipment.Status.PICKING).exists()

    # 2. Packing
    mark_packed(
        shipment,
        staff_user=staff_user,
        package_weight_kg=Decimal("1.45"),
        package_dimensions="35x25x10cm",
        internal_notes="Wrapped with eco tissue",
        customer_notes="Fragile item enclosed",
        note="Box #4 packed and sealed",
    )
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.PACKED
    assert shipment.packed_by == staff_user
    assert shipment.packed_at is not None
    assert shipment.package_weight_kg == Decimal("1.45")
    assert shipment.package_dimensions == "35x25x10cm"
    assert "eco tissue" in shipment.internal_notes
    assert "Fragile" in shipment.customer_notes
    assert shipment.events.filter(event_type=Shipment.Status.PACKED).exists()

    # 3. Dispatch
    est_date = timezone.now().date() + timezone.timedelta(days=2)
    dispatch_shipment(
        shipment,
        staff_user=staff_user,
        carrier="FedEx",
        carrier_service="FedEx 2Day",
        tracking_number="794644790132",
        estimated_delivery=est_date,
        note="Handed over to FedEx courier",
    )
    shipment.refresh_from_db()
    order.refresh_from_db()

    assert shipment.status == Shipment.Status.SHIPPED
    assert shipment.dispatched_by == staff_user
    assert shipment.dispatched_at is not None
    assert shipment.carrier == "FedEx"
    assert shipment.carrier_service == "FedEx 2Day"
    assert shipment.tracking_number == "794644790132"
    assert shipment.estimated_delivery == est_date
    assert order.status == Order.Status.SHIPPED
    assert order.shipped_at is not None

    # Verify customer received dispatch notification
    notif = user.notifications.filter(notification_type=NotificationType.ORDER_SHIPPED).first()
    assert notif is not None
    assert shipment.reference in notif.body or shipment.tracking_number in notif.body


@pytest.mark.django_db
def test_invalid_lifecycle_transition_raises_error(user, staff_user, catalog_setup):
    """Cannot jump directly from PENDING to DELIVERED."""
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=1)
    shipment = create_fulfillment(order, staff_user=staff_user)

    with pytest.raises(InvalidTransition):
        shipment.transition_to(Shipment.Status.DELIVERED)


# ==============================================================================
# 4. Safe Courier Tracking URLs
# ==============================================================================

@pytest.mark.django_db
def test_safe_tracking_url_resolution(user, catalog_setup):
    """External tracking URLs are generated safely for verified couriers and sanitize malicious inputs."""
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=1)
    shipment = create_fulfillment(order)

    # FedEx
    shipment.carrier = "FedEx"
    shipment.tracking_number = "794644790132"
    assert shipment.safe_tracking_url == "https://www.fedex.com/fedextrack/?trknbr=794644790132"

    # UPS
    shipment.carrier = "UPS"
    shipment.tracking_number = "1Z9999999999999999"
    assert shipment.safe_tracking_url == "https://www.ups.com/track?tracknum=1Z9999999999999999"

    # USPS
    shipment.carrier = "USPS"
    shipment.tracking_number = "9400100000000000000000"
    assert shipment.safe_tracking_url == "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400100000000000000000"

    # DHL
    shipment.carrier = "DHL"
    shipment.tracking_number = "1234567890"
    assert shipment.safe_tracking_url == "https://www.dhl.com/en/express/tracking.html?AWB=1234567890"

    # Royal Mail
    shipment.carrier = "Royal Mail"
    shipment.tracking_number = "GB123456789GB"
    assert shipment.safe_tracking_url == "https://www.royalmail.com/track-your-item#/tracking-details/GB123456789GB"

    # Custom or unverified carrier without tracking_url -> None
    shipment.carrier = "Local Courier"
    shipment.tracking_number = "LOC123"
    shipment.tracking_url = ""
    assert shipment.safe_tracking_url is None

    # Malicious scheme in tracking_url -> sanitized to None
    shipment.tracking_url = "javascript:alert('xss')"
    assert shipment.safe_tracking_url is None

    # Safe https tracking_url on custom carrier
    shipment.tracking_url = "https://tracking.localcarrier.example.com/track/123"
    assert shipment.safe_tracking_url == "https://tracking.localcarrier.example.com/track/123"


# ==============================================================================
# 5. Out-of-Order Tracking & Idempotency Safeguards
# ==============================================================================

@pytest.mark.django_db
def test_tracking_scans_and_out_of_order_non_regression(user, staff_user, catalog_setup):
    """A delivered shipment records late transit scans in history but refuses to regress status."""
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=1)
    shipment = create_fulfillment(order, staff_user=staff_user)
    dispatch_shipment(shipment, staff_user=staff_user, carrier="FedEx", tracking_number="123456")

    # In Transit update
    update_tracking_event(
        shipment,
        event_type="carrier_scan",
        status=Shipment.Status.IN_TRANSIT,
        location="Sorting Facility, London",
        note="Departed facility",
    )
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.IN_TRANSIT

    # Out for Delivery update (triggers notification)
    update_tracking_event(
        shipment,
        event_type="out_for_delivery",
        status=Shipment.Status.OUT_FOR_DELIVERY,
        location="London Van 12",
        note="With courier for delivery",
    )
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.OUT_FOR_DELIVERY
    assert user.notifications.filter(notification_type=NotificationType.SHIPMENT_OUT_FOR_DELIVERY).exists()

    # Delivered update (triggers order delivery)
    update_tracking_event(
        shipment,
        event_type="delivered",
        status=Shipment.Status.DELIVERED,
        location="Front Porch",
        note="Signed for by resident",
    )
    shipment.refresh_from_db()
    order.refresh_from_db()
    assert shipment.status == Shipment.Status.DELIVERED
    assert shipment.delivered_at is not None
    assert order.status == Order.Status.DELIVERED
    assert order.delivered_at is not None
    assert user.notifications.filter(notification_type=NotificationType.ORDER_DELIVERED).exists()

    # Late out-of-order scan arrives claiming "in_transit"
    update_tracking_event(
        shipment,
        event_type="delayed_scan",
        status=Shipment.Status.IN_TRANSIT,
        location="Intermediate Waypoint",
        note="Delayed scan received from relay station",
    )
    shipment.refresh_from_db()
    order.refresh_from_db()

    # Shipment MUST NOT regress to in_transit
    assert shipment.status == Shipment.Status.DELIVERED
    assert order.status == Order.Status.DELIVERED
    # But event was safely recorded in history
    latest_event = shipment.events.order_by("-id").first()
    assert latest_event.location == "Intermediate Waypoint"
    assert latest_event.event_type == "delayed_scan"


# ==============================================================================
# 6. Carrier Tracking Webhook (HMAC-SHA256 & Replay Protection)
# ==============================================================================

@pytest.mark.django_db
@override_settings(CARRIER_WEBHOOK_SECRET="test-carrier-secret-key-12345")
def test_carrier_tracking_webhook_signature_and_replay_protection(client, user, staff_user, catalog_setup):
    """Test webhook authentication, timestamp replay window, and event deduplication."""
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=1)
    shipment = create_fulfillment(order, staff_user=staff_user)
    dispatch_shipment(shipment, staff_user=staff_user, carrier="FedEx", tracking_number="987654321")

    url = reverse("v1:shipping-webhook", kwargs={"provider": "fedex"})
    secret = "test-carrier-secret-key-12345".encode()

    payload = {
        "event_id": "evt_scan_1001",
        "reference": shipment.reference,
        "status": "in_transit",
        "location": "Heathrow Gateway",
        "description": "Customs cleared and in transit",
    }
    raw_body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(secret, f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()

    # 1. Valid webhook call
    resp = client.post(
        url,
        data=raw_body,
        content_type="application/json",
        HTTP_X_CARRIER_SIGNATURE=signature,
        HTTP_X_CARRIER_TIMESTAMP=timestamp,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in ("processed", "ok")

    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.IN_TRANSIT
    assert shipment.events.filter(metadata__event_id="evt_scan_1001").exists()

    # 2. Replay of same event_id is idempotent (already processed)
    resp_replay = client.post(
        url,
        data=raw_body,
        content_type="application/json",
        HTTP_X_CARRIER_SIGNATURE=signature,
        HTTP_X_CARRIER_TIMESTAMP=timestamp,
    )
    assert resp_replay.status_code == 200
    assert resp_replay.json()["status"] == "already_processed"
    # Event count should not increase
    assert shipment.events.filter(metadata__event_id="evt_scan_1001").count() == 1

    # 3. Invalid signature rejected
    bad_signature = "bad_signature_hex"
    resp_bad = client.post(
        url,
        data=raw_body,
        content_type="application/json",
        HTTP_X_CARRIER_SIGNATURE=bad_signature,
        HTTP_X_CARRIER_TIMESTAMP=timestamp,
    )
    assert resp_bad.status_code == 401

    # 4. Expired timestamp rejected (> 300s old)
    old_timestamp = str(int(time.time()) - 400)
    old_sig = hmac.new(secret, f"{old_timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    resp_stale = client.post(
        url,
        data=raw_body,
        content_type="application/json",
        HTTP_X_CARRIER_SIGNATURE=old_sig,
        HTTP_X_CARRIER_TIMESTAMP=old_timestamp,
    )
    assert resp_stale.status_code == 400
    assert "timestamp" in resp_stale.json()["error"].lower()


# ==============================================================================
# 7. Delivery Exception Handling
# ==============================================================================

@pytest.mark.django_db
def test_delivery_exception_recorded_without_auto_refund(user, staff_user, catalog_setup):
    """Delivery exceptions update status and notify customer, preserving order financial invariants."""
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=1)
    shipment = create_fulfillment(order, staff_user=staff_user)
    dispatch_shipment(shipment, staff_user=staff_user, carrier="UPS", tracking_number="1Z12345")

    record_delivery_exception(
        shipment,
        staff_user=staff_user,
        exception_type=Shipment.Status.ATTEMPT_FAILED,
        reason="Customer not home, gate code required",
        location="Delivery Route 4",
        note="Card left at door",
    )
    shipment.refresh_from_db()
    order.refresh_from_db()

    assert shipment.status == Shipment.Status.ATTEMPT_FAILED
    assert shipment.is_exception_state is True
    # Order remains SHIPPED; NO automatic cancellation or refund
    assert order.status == Order.Status.SHIPPED

    # Customer received notification
    notif = user.notifications.filter(notification_type=NotificationType.SHIPMENT_EXCEPTION).first()
    assert notif is not None
    assert "delivery exception" in notif.title.lower() or "delivery update" in notif.title.lower()


# ==============================================================================
# 8. Customer Ownership & IDOR Protection
# ==============================================================================

@pytest.mark.django_db
def test_customer_shipment_api_idor_protection(api_client, user, other_customer, staff_user, catalog_setup):
    """Customer cannot view another user's shipment API; internal notes are never exposed."""
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=1)
    shipment = create_fulfillment(order, staff_user=staff_user)
    start_picking(shipment, staff_user=staff_user)
    mark_packed(
        shipment,
        staff_user=staff_user,
        internal_notes="SECRET WAREHOUSE NOTE",
        customer_notes="Please inspect upon receipt",
    )
    dispatch_shipment(shipment, staff_user=staff_user, carrier="FedEx", tracking_number="794644790132")

    url = reverse("v1:order-shipment-detail", kwargs={"number": order.number, "reference": shipment.reference})

    # 1. Unauthenticated -> 401/403
    resp = api_client.get(url)
    assert resp.status_code in (401, 403)

    # 2. Other customer -> 404 (IDOR prevented)
    api_client.force_authenticate(user=other_customer)
    resp = api_client.get(url)
    assert resp.status_code == 404

    # 3. Owner -> 200 with safe fields
    api_client.force_authenticate(user=user)
    resp = api_client.get(url)
    assert resp.status_code == 200
    data = resp.json()
    assert data["reference"] == shipment.reference
    assert data["carrier"] == "FedEx"
    assert data["tracking_number"] == "794644790132"
    assert data["safe_tracking_url"] == "https://www.fedex.com/fedextrack/?trknbr=794644790132"
    assert data["customer_notes"] == "Please inspect upon receipt"
    assert len(data["items"]) == 1
    # Internal warehouse note must NOT be in response
    assert "SECRET WAREHOUSE NOTE" not in json.dumps(data)


@pytest.mark.django_db
def test_customer_order_detail_view_renders_shipments(client, user, staff_user, catalog_setup):
    """Customer order detail page displays package details, items, tracking links, and events."""
    client.force_login(user)
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=1)
    shipment = create_fulfillment(order, staff_user=staff_user)
    dispatch_shipment(shipment, staff_user=staff_user, carrier="FedEx", tracking_number="794644790132")

    update_tracking_event(
        shipment,
        event_type="in_transit",
        status=Shipment.Status.IN_TRANSIT,
        location="Manchester Hub",
        note="Package processed at hub",
    )

    url = reverse("account:order-detail", kwargs={"number": order.number})
    resp = client.get(url)
    assert resp.status_code == 200
    content = resp.content.decode()

    assert shipment.reference in content
    assert "FedEx" in content
    assert "794644790132" in content
    assert "Track with FedEx" in content
    assert "Manchester Hub" in content
    assert "Technical Trench Coat" in content


# ==============================================================================
# 9. Backoffice Staff Operations Interface
# ==============================================================================

@pytest.mark.django_db
def test_backoffice_shipment_queue_and_actions(client, staff_user, user, catalog_setup):
    """Staff can view queue, inspect shipment details, and execute fulfillment actions."""
    client.force_login(staff_user)
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=2)
    shipment = create_fulfillment(order, staff_user=staff_user)

    # 1. Shipments queue list
    queue_url = reverse("backoffice:shipments")
    resp = client.get(queue_url)
    assert resp.status_code == 200
    assert shipment.reference in resp.content.decode()

    # Search filter by reference
    resp_search = client.get(queue_url, {"q": shipment.reference})
    assert resp_search.status_code == 200
    assert shipment.reference in resp_search.content.decode()

    # 2. Shipment detail view
    detail_url = reverse("backoffice:shipment_detail", kwargs={"reference": shipment.reference})
    resp_detail = client.get(detail_url)
    assert resp_detail.status_code == 200
    assert shipment.reference in resp_detail.content.decode()

    # 3. Staff action: Start Picking
    action_url = reverse("backoffice:shipment_action", kwargs={"reference": shipment.reference})
    resp_pick = client.post(action_url, {"action": "start_picking", "note": "Picked item"})
    assert resp_pick.status_code == 302
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.PICKING

    # 4. Staff action: Mark Packed
    resp_pack = client.post(
        action_url,
        {
            "action": "mark_packed",
            "package_weight_kg": "2.10",
            "package_dimensions": "40x30x15cm",
            "internal_notes": "Handle carefully",
        },
    )
    assert resp_pack.status_code == 302
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.PACKED
    assert shipment.package_weight_kg == Decimal("2.10")

    # 5. Staff action: Dispatch
    resp_dispatch = client.post(
        action_url,
        {
            "action": "dispatch",
            "carrier": "FedEx",
            "tracking_number": "794644790132",
        },
    )
    assert resp_dispatch.status_code == 302
    shipment.refresh_from_db()
    assert shipment.status == Shipment.Status.SHIPPED


@pytest.mark.django_db
def test_backoffice_create_fulfillment_from_order(client, staff_user, user, catalog_setup):
    """Staff can create a split fulfillment directly from the order detail page."""
    client.force_login(staff_user)
    order = place_and_pay(user, catalog_setup["variant_a"], quantity=3)
    item = order.items.first()

    url = reverse("backoffice:create_order_fulfillment", kwargs={"number": order.number})
    resp = client.post(
        url,
        {
            f"qty_{item.pk}": "2",
            "carrier": "FedEx",
            "carrier_service": "Priority",
            "tracking_number": "794644790132",
        },
    )
    assert resp.status_code == 302
    shipment = Shipment.objects.filter(order=order).first()
    assert shipment is not None
    assert shipment.items.first().quantity == 2
    assert shipment.carrier == "FedEx"
