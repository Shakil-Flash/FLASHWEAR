"""Tests for Phase 36: Order Tracking & Post-Purchase Experience 2.0.

Covers:
- Order history list: product previews, status badges, formatted totals, primary CTAs
- Order history empty state: human copy, collection explore link
- Security & IDOR protection: orders scoped to authenticated user, cross-user 404
- Authoritative order timeline: placed, paid, processing, shipped, delivered milestones
- Shipment tracking: carrier, tracking number, carrier scans, pending dispatch copy
- Payment states: paid, failed with retry CTA, safe provider exposure
- Support integration: pre-filled order reference on support ticket create
- Review integration: review eligibility on delivered items vs already-reviewed badges
- FLASH Loop integration: circular options on delivered garments with deep link
- Loop create form pre-population from order_item parameter
- Dashboard recent orders links and field name regression
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.catalog.models import Color, Product, ProductVariant, Size
from apps.engagement.models import Review
from apps.inventory.models import Stock
from apps.orders.models import Order, Shipment, ShipmentEvent
from apps.payments.models import Payment
from tests.phase6_helpers import placed_order


@pytest.fixture
def order_fixture(db, user, category):
    product = Product.objects.create(
        name="Structured Wool Blazer",
        slug="structured-wool-blazer",
        status=Product.Status.ACTIVE,
        category=category,
    )
    black = Color.objects.create(name="Onyx", slug="onyx", hex_code="#111111")
    size_m = Size.objects.create(name="Medium", code="M", display_order=1)

    variant = ProductVariant.objects.create(
        product=product,
        color=black,
        size=size_m,
        sku="SWB-ONX-M",
        price=Decimal("180.00"),
        is_active=True,
    )
    Stock.objects.create(variant=variant, on_hand=20, reserved=0)

    order = placed_order(user, variant, stock=None)
    order.subtotal = Decimal("180.00")
    order.total = Decimal("180.00")
    order.shipping_name = "Express Courier"
    order.save(update_fields=["subtotal", "total", "shipping_name"])

    return {
        "user": user,
        "product": product,
        "variant": variant,
        "order": order,
    }


class TestOrderHistory:
    def test_order_history_renders_preview_and_actions(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]

        resp = client.get(reverse("account:orders"))
        assert resp.status_code == 200
        content = resp.content.decode()

        assert order.number in content
        assert "Structured Wool Blazer" in content
        assert order.get_status_display() in content
        assert reverse("account:order-detail", args=[order.number]) in content

    def test_order_history_empty_state_human_copy(self, client, django_user_model):
        fresh_user = django_user_model.objects.create_user(
            email="newshopper@test.com", password="Password123!"
        )
        client.force_login(fresh_user)

        resp = client.get(reverse("account:orders"))
        assert resp.status_code == 200
        content = resp.content.decode()

        assert "Your first FLASHWEAR order is waiting to happen" in content
        assert reverse("catalog:product-list") in content

    def test_order_history_ownership_isolation(self, client, user, other_user, order_fixture):
        # order belongs to `user`
        client.force_login(other_user)

        resp = client.get(reverse("account:orders"))
        assert resp.status_code == 200
        content = resp.content.decode()

        assert order_fixture["order"].number not in content


class TestOrderDetailAndSecurity:
    def test_order_detail_idor_protection(self, client, other_user, order_fixture):
        client.force_login(other_user)
        order = order_fixture["order"]

        # Accessing another user's order number returns 404
        resp = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp.status_code == 404

    def test_order_detail_anonymous_redirects_to_login(self, client, order_fixture):
        order = order_fixture["order"]
        resp = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp.status_code == 302
        assert reverse("accounts:login") in resp["Location"]

    def test_order_detail_renders_authoritative_timeline(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]

        # Move order to paid then shipped
        order.transition_to(Order.Status.PAID)
        order.transition_to(Order.Status.PROCESSING)
        order.transition_to(Order.Status.SHIPPED)

        resp = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp.status_code == 200
        ctx = resp.context
        assert "timeline" in ctx

        timeline = ctx["timeline"]
        # Placed and Paid and Processing and Shipped completed
        assert timeline[0]["completed"] is True
        assert timeline[1]["completed"] is True
        assert timeline[2]["completed"] is True
        assert timeline[3]["completed"] is True
        assert timeline[4]["completed"] is False  # Not delivered yet

    def test_order_detail_cancelled_state(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]
        order.transition_to(Order.Status.CANCELLED)

        resp = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "This order was cancelled" in content

    def test_shipment_tracking_when_dispatched_and_pending(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]

        # Pending shipment without tracking number
        resp = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp.status_code == 200
        assert (
            "Tracking information will appear when your shipment is dispatched"
            in resp.content.decode()
        )

        # Add shipment with tracking and carrier scan
        shipment = Shipment.objects.create(
            order=order,
            status=Shipment.Status.SHIPPED,
            carrier="DHL Express",
            tracking_number="DHL-987654321",
        )
        ShipmentEvent.objects.create(
            shipment=shipment,
            event_type=Shipment.Status.SHIPPED,
            note="Sorted at London Hub",
        )

        resp2 = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp2.status_code == 200
        content2 = resp2.content.decode()
        assert "DHL Express" in content2
        assert "DHL-987654321" in content2
        assert "Sorted at London Hub" in content2

    def test_payment_states_and_retry(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]
        Payment.objects.create(
            order=order,
            provider="development",
            amount=order.total,
            currency="USD",
            status=Payment.Status.FAILED,
            failure_message="Card expired or declined by issuer.",
        )

        resp = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Card expired or declined by issuer." in content
        assert "Retry Payment" in content
        assert reverse("shop:checkout-payment", args=[order.number]) in content

    def test_support_ticket_prefill_link(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]

        resp = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp.status_code == 200
        content = resp.content.decode()
        expected_support_url = f"{reverse('support:ticket-create')}?order={order.number}"
        assert expected_support_url in content


class TestPostPurchaseIntegrations:
    def test_review_integration_on_delivered_order(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]
        product = order_fixture["product"]

        # Delivered order
        order.transition_to(Order.Status.PAID)
        order.transition_to(Order.Status.PROCESSING)
        order.transition_to(Order.Status.SHIPPED)
        order.transition_to(Order.Status.DELIVERED)

        resp = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "How was it? Leave a review" in content

        # Now write review for this product
        Review.objects.create(
            author=user,
            product=product,
            order=order,
            rating=5,
            title="Exceptional craft",
            body="Superb tailoring and heavy drape.",
            status=Review.Status.PUBLISHED,
        )

        resp2 = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp2.status_code == 200
        content2 = resp2.content.decode()
        assert "Reviewed (5/5)" in content2

    def test_loop_integration_on_delivered_order(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]
        item = order.items.first()

        order.transition_to(Order.Status.PAID)
        order.transition_to(Order.Status.PROCESSING)
        order.transition_to(Order.Status.SHIPPED)
        order.transition_to(Order.Status.DELIVERED)

        resp = client.get(reverse("account:order-detail", args=[order.number]))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Circulate on Loop" in content
        assert f"{reverse('account:loop-create')}?order_item={item.pk}" in content

    def test_loop_create_prepopulates_from_order_item_param(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]
        item = order.items.first()

        order.transition_to(Order.Status.PAID)
        order.transition_to(Order.Status.PROCESSING)
        order.transition_to(Order.Status.SHIPPED)
        order.transition_to(Order.Status.DELIVERED)

        resp = client.get(reverse("account:loop-create"), {"order_item": str(item.pk)})
        assert resp.status_code == 200
        form = resp.context["form"]
        assert form.initial.get("owned_item") == f"order:{item.pk}"

    def test_dashboard_links_to_valid_order_number(self, client, user, order_fixture):
        client.force_login(user)
        order = order_fixture["order"]

        resp = client.get(reverse("account:dashboard"))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert f"Order #{order.number}" in content
        assert reverse("account:order-detail", args=[order.number]) in content
