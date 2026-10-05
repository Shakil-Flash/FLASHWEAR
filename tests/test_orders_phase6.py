"""Orders Phase 6: the checkout handoff, both state machines, the customer screens.

The handoff tests drive :func:`apps.orders.services.create_order_from_checkout`
directly; the flow tests drive the same code through HTTP (bag -> validate ->
place -> pay) because that is the path customers take. Fulfilment, the account
area and the API are covered separately so a failure names the layer that broke.
"""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.urls import reverse
from django.utils import timezone

from apps.accounts.navigation import ACCOUNT_SECTIONS
from apps.inventory.models import Reservation, Stock
from apps.inventory.services import InsufficientStock, sweep_expired_holds
from apps.orders.models import (
    InvalidTransition,
    Order,
    OrderEvent,
    OrderItem,
    Shipment,
)
from apps.orders.services import (
    CheckoutNotReady,
    StaleCheckout,
    cancel_order,
    create_order_from_checkout,
    deliver_order,
    mark_processing,
    ship_order,
)
from apps.payments.models import Payment
from apps.payments.services import start_payment
from apps.shop.models import Cart, CheckoutSession
from tests.phase6_helpers import (
    add_to_bag,
    make_address,
    message_texts,
    open_checkout,
    place_and_pay,
    placed_order,
    seed_stock,
    sign_in,
    validated_checkout,
)

pytestmark = pytest.mark.django_db


# =============================================================================
# Handoff
# =============================================================================


class TestHandoff:
    def test_a_validated_checkout_becomes_a_frozen_order(self, user, product):
        variant = product.variants.first()
        checkout = validated_checkout(user, variant, 2, stock=8)

        order = create_order_from_checkout(checkout)

        assert re.fullmatch(r"FW-\d{8}-[A-Z0-9]{8}", order.number)
        assert order.user == user
        assert order.status == Order.Status.PENDING_PAYMENT
        assert order.currency == "USD"
        assert order.subtotal == Decimal(checkout.snapshot["subtotal"])
        assert order.shipping_amount == Decimal(checkout.snapshot["shipping"]["amount"])
        assert order.total == Decimal(checkout.snapshot["total"])
        assert order.total == order.subtotal + order.shipping_amount
        item = order.items.get()
        assert (item.sku, item.quantity, item.unit_price) == (variant.sku, 2, variant.price)
        address = order.shipping_address
        assert address.full_name == "Ada Lovelace"
        assert address.address_id is not None
        types = set(order.events.values_list("event_type", flat=True))
        assert {OrderEvent.Type.CREATED, OrderEvent.Type.STOCK_RESERVED} <= types

        checkout.refresh_from_db()
        cart = order.checkout.cart
        assert checkout.status == CheckoutSession.Status.CONVERTED
        assert cart.status == Cart.Status.CONVERTED
        assert cart.converted_at is not None

        hold = order.reservations.get()
        assert (hold.status, hold.quantity) == (Reservation.Status.ACTIVE, 2)
        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved) == (8, 2)

    def test_replaying_the_handoff_returns_the_same_order(self, user, product):
        checkout = validated_checkout(user, product.variants.first(), stock=10)

        first = create_order_from_checkout(checkout)
        second = create_order_from_checkout(checkout)

        assert first.pk == second.pk
        assert Order.objects.count() == 1
        assert OrderItem.objects.count() == 1

    def test_an_unvalidated_session_is_refused(self, user, product):
        checkout = open_checkout(user, variant=product.variants.first())

        with pytest.raises(CheckoutNotReady):
            create_order_from_checkout(checkout)

        assert Order.objects.count() == 0

    def test_editing_the_bag_after_validation_blocks_the_handoff(self, user, product):
        checkout = validated_checkout(user, product.variants.first(), stock=10)
        checkout.cart.items.update(quantity=3)

        with pytest.raises(StaleCheckout, match="review"):
            create_order_from_checkout(checkout)

        checkout.refresh_from_db()
        assert checkout.status == CheckoutSession.Status.VALIDATED
        assert Order.objects.count() == 0

    def test_a_price_rise_after_validation_blocks_the_handoff(self, user, product):
        variant = product.variants.first()
        checkout = validated_checkout(user, variant, stock=10)
        variant.price = variant.price + Decimal("5.00")
        variant.save()

        with pytest.raises(StaleCheckout) as exc:
            create_order_from_checkout(checkout)

        assert "changed to" in str(exc.value)
        assert Order.objects.count() == 0

    def test_deactivating_a_variant_blocks_the_handoff(self, user, product):
        variant = product.variants.first()
        checkout = validated_checkout(user, variant, stock=10)
        variant.is_active = False
        variant.save()

        with pytest.raises(StaleCheckout, match="no longer available"):
            create_order_from_checkout(checkout)

        assert Order.objects.count() == 0

    def test_insufficient_stock_leaves_the_checkout_validated(self, user, product):
        variant = product.variants.first()
        checkout = validated_checkout(user, variant, 2, stock=1)

        with pytest.raises(InsufficientStock):
            create_order_from_checkout(checkout)

        checkout.refresh_from_db()
        assert checkout.status == CheckoutSession.Status.VALIDATED
        assert Order.objects.count() == 0
        assert Stock.objects.get(variant=variant).reserved == 0
        assert not Reservation.objects.filter(status=Reservation.Status.ACTIVE).exists()

    def test_order_numbers_are_random_not_walkable(self, user, product):
        variant = product.variants.first()
        first = placed_order(user, variant, stock=20)
        second = placed_order(user, variant, stock=None)

        assert first.number != second.number
        for number in (first.number, second.number):
            assert re.fullmatch(r"FW-\d{8}-[A-Z0-9]{8}", number)


# =============================================================================
# Order + shipment state machines
# =============================================================================


class TestFulfilment:
    def test_a_paid_order_walks_to_delivered(self, user, product):
        order = place_and_pay(user, product.variants.first())

        shipment = mark_processing(order, actor=order.user, carrier="DHL")
        order.refresh_from_db()
        assert shipment.status == Shipment.Status.PROCESSING
        assert order.status == Order.Status.PROCESSING

        ship_order(order, tracking_number="JD0002", carrier="DHL")
        order.refresh_from_db()
        shipment.refresh_from_db()
        assert shipment.status == Shipment.Status.SHIPPED
        assert shipment.tracking_number == "JD0002"
        assert shipment.shipped_at is not None
        assert order.status == Order.Status.SHIPPED
        assert order.shipped_at is not None

        deliver_order(order, actor=order.user)
        order.refresh_from_db()
        shipment.refresh_from_db()
        assert shipment.status == Shipment.Status.DELIVERED
        assert shipment.delivered_at is not None
        assert order.status == Order.Status.DELIVERED
        assert order.delivered_at is not None
        assert order.events.filter(event_type=OrderEvent.Type.SHIPMENT_CREATED).count() == 1
        # One event per move: processing, shipped, delivered (creation logs none).
        assert shipment.events.count() == 3

    def test_shipping_a_paid_order_starts_processing_for_you(self, user, product):
        order = place_and_pay(user, product.variants.first())

        shipment = ship_order(order, tracking_number="JD1")

        order.refresh_from_db()
        assert shipment.status == Shipment.Status.SHIPPED
        assert order.status == Order.Status.SHIPPED
        assert order.shipments.count() == 1

    def test_illegal_order_jumps_are_refused(self, user, product):
        order = placed_order(user, product.variants.first())

        with pytest.raises(InvalidTransition):
            order.transition_to(Order.Status.DELIVERED)
        with pytest.raises(InvalidTransition):
            mark_processing(order)
        with pytest.raises(InvalidTransition):
            ship_order(order)
        with pytest.raises(InvalidTransition):
            deliver_order(order)
        with pytest.raises(InvalidTransition):
            order.transition_to(Order.Status.REFUNDED)

    def test_illegal_shipment_jumps_are_refused(self, user, product):
        order = place_and_pay(user, product.variants.first())
        shipment = mark_processing(order)

        with pytest.raises(InvalidTransition):
            shipment.transition_to(Shipment.Status.DELIVERED)

        shipment.transition_to(Shipment.Status.SHIPPED)
        with pytest.raises(InvalidTransition):
            shipment.transition_to(Shipment.Status.PROCESSING)

        shipment.transition_to(Shipment.Status.DELIVERED)
        with pytest.raises(InvalidTransition):
            shipment.transition_to(Shipment.Status.SHIPPED)
        with pytest.raises(InvalidTransition):
            shipment.transition_to(Shipment.Status.CANCELLED)

    def test_paid_orders_cannot_be_cancelled_by_the_service(self, user, product):
        order = place_and_pay(user, product.variants.first())

        with pytest.raises(InvalidTransition):
            cancel_order(order)

        order.refresh_from_db()
        assert order.status == Order.Status.PAID

    def test_cancelling_an_unpaid_order_releases_its_stock(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)

        cancel_order(order, actor=user, event_type=OrderEvent.Type.CANCELLED_BY_CUSTOMER)

        order.refresh_from_db()
        assert order.status == Order.Status.CANCELLED
        assert order.cancelled_at is not None
        assert order.reservations.get().status == Reservation.Status.RELEASED
        assert Stock.objects.get(variant=variant).reserved == 0
        types = set(order.events.values_list("event_type", flat=True))
        assert {OrderEvent.Type.CANCELLED_BY_CUSTOMER, OrderEvent.Type.STOCK_RELEASED} <= types

        with pytest.raises(InvalidTransition):
            cancel_order(order)

    def test_refunded_is_declared_but_out_of_reach_in_phase6(self, user, product):
        order = placed_order(user, product.variants.first())

        # The enum exists so the later refund workflow needs no breaking change,
        # but an unpaid order cannot jump there and no Phase 6 path enters it.
        assert Order.Status.REFUNDED in Order.Status.values
        assert not order.can_transition_to(Order.Status.REFUNDED)
        assert Order.ALLOWED_TRANSITIONS[Order.Status.CANCELLED] == ()


# =============================================================================
# Sweeper vs placed orders
# =============================================================================


class TestSweeperAndOrders:
    def test_the_sweeper_never_touches_a_placed_order(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        order.reservations.update(expires_at=timezone.now() - timedelta(hours=1))

        released = sweep_expired_holds()

        assert released == 0
        hold = order.reservations.get()
        assert hold.status == Reservation.Status.ACTIVE
        assert Stock.objects.get(variant=variant).reserved == 1


# =============================================================================
# Customer account pages
# =============================================================================


class TestOrderPages:
    def test_the_account_nav_links_to_orders(self):
        targets = [target for _, target, _ in ACCOUNT_SECTIONS if target]

        assert "account:orders" in targets
        assert reverse("account:orders") == "/account/orders/"

    def test_the_history_lists_your_orders_newest_first(self, client, user, product):
        variant = product.variants.first()
        first = placed_order(user, variant, stock=20)
        second = placed_order(user, variant, stock=None)
        sign_in(client)

        response = client.get(reverse("account:orders"))

        assert response.status_code == 200
        html = response.content.decode()
        assert first.number in html and second.number in html
        assert html.index(second.number) < html.index(first.number)
        assert "Pending payment" in html

    def test_someone_elses_orders_are_invisible_and_404(self, client, user, other_user, product):
        theirs = placed_order(other_user, product.variants.first())
        sign_in(client)

        listing = client.get(reverse("account:orders"))
        detail = client.get(reverse("account:order-detail", args=[theirs.number]))

        assert theirs.number not in listing.content.decode()
        assert detail.status_code == 404

    def test_anonymous_visitors_are_sent_to_login(self, client, user, product):
        order = placed_order(user, product.variants.first())

        listing = client.get(reverse("account:orders"))
        detail = client.get(reverse("account:order-detail", args=[order.number]))

        assert listing.status_code == 302
        assert detail.status_code == 302
        assert "login" in detail["Location"]

    def test_the_detail_page_shows_lines_address_and_timeline(self, client, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, 2)
        sign_in(client)

        response = client.get(reverse("account:order-detail", args=[order.number]))

        assert response.status_code == 200
        html = response.content.decode()
        assert variant.sku in html
        assert "Ada Lovelace" in html
        assert "Order placed" in html

    def test_cancelling_from_the_account_page_releases_the_stock(self, client, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        start_payment(order)
        sign_in(client)

        response = client.post(reverse("account:order-cancel", args=[order.number]))
        assert response.status_code == 302
        page = client.get(response["Location"])

        assert "was cancelled" in message_texts(page)
        order.refresh_from_db()
        assert order.status == Order.Status.CANCELLED
        assert order.payment.status == Payment.Status.CANCELLED
        assert order.reservations.get().status == Reservation.Status.RELEASED
        assert Stock.objects.get(variant=variant).reserved == 0

    def test_a_paid_order_refuses_the_cancel_button(self, client, user, product):
        order = place_and_pay(user, product.variants.first())
        sign_in(client)

        response = client.post(reverse("account:order-cancel", args=[order.number]))
        page = client.get(response["Location"])

        assert "no longer be cancelled" in message_texts(page)
        order.refresh_from_db()
        assert order.status == Order.Status.PAID

    def test_the_cancel_endpoint_ignores_foreign_orders(self, client, user, other_user, product):
        theirs = placed_order(other_user, product.variants.first())
        sign_in(client)

        response = client.post(reverse("account:order-cancel", args=[theirs.number]))

        assert response.status_code == 404
        theirs.refresh_from_db()
        assert theirs.status == Order.Status.PENDING_PAYMENT

    def test_the_history_page_stays_flat(
        self, client, user, product, django_assert_max_num_queries
    ):
        variant = product.variants.first()
        placed_order(user, variant, stock=50)
        for _ in range(5):
            placed_order(user, variant, stock=None)
        sign_in(client)

        # 12 page queries + 1 for the navbar bell badge (Phase 17 unread count).
        with django_assert_max_num_queries(13):
            response = client.get(reverse("account:orders"))

        assert response.status_code == 200


# =============================================================================
# Order API
# =============================================================================


class TestOrderApi:
    def test_the_root_advertises_the_orders_endpoint(self, api_client):
        response = api_client.get(reverse("v1:root"))

        assert response.status_code == 200
        assert response.json()["endpoints"]["orders"].endswith("/api/v1/orders/")

    def test_orders_require_a_customer(self, api_client):
        assert api_client.get(reverse("v1:order-list")).status_code in (401, 403)

    def test_the_list_is_scoped_and_summarised(self, api_client, user, other_user, product):
        variant = product.variants.first()
        mine = placed_order(user, variant, stock=20)
        placed_order(other_user, variant, stock=None)
        api_client.force_authenticate(user)

        response = api_client.get(reverse("v1:order-list"))

        assert response.status_code == 200
        results = response.json()["results"]
        numbers = [row["number"] for row in results]
        assert mine.number in numbers
        assert len(numbers) == 1
        row = results[0]
        assert row["item_count"] == 1
        assert row["payment_status"] is None
        assert row["status"] == "pending_payment"

    def test_the_detail_is_a_snapshot_without_provider_secrets(self, api_client, user, product):
        variant = product.variants.first()
        order = place_and_pay(user, variant, 2)
        api_client.force_authenticate(user)

        response = api_client.get(reverse("v1:order-detail", args=[order.number]))

        assert response.status_code == 200
        body = response.json()
        assert body["number"] == order.number
        assert body["status"] == "paid"
        assert body["payment"]["status"] == "succeeded"
        assert "provider_reference" not in body["payment"]
        assert order.payment.provider_reference not in response.content.decode()
        assert body["items"][0]["sku"] == variant.sku
        assert body["shipping_address"]["full_name"] == "Ada Lovelace"
        assert body["paid_at"] is not None

    def test_someone_elses_order_is_404(self, api_client, user, other_user, product):
        theirs = placed_order(other_user, product.variants.first())
        api_client.force_authenticate(user)

        response = api_client.get(reverse("v1:order-detail", args=[theirs.number]))

        assert response.status_code == 404

    def test_the_list_response_is_never_cached(self, api_client, user):
        api_client.force_authenticate(user)

        response = api_client.get(reverse("v1:order-list"))

        assert "no-cache" in response["Cache-Control"]


# =============================================================================
# Admin
# =============================================================================


class TestAdminOrderActions:
    def test_mark_processing_opens_a_shipment(self, admin_client, user, product):
        order = place_and_pay(user, product.variants.first())

        response = admin_client.post(
            reverse("admin:orders_order_changelist"),
            {"action": "mark_processing", ACTION_CHECKBOX_NAME: [str(order.pk)]},
        )

        assert response.status_code == 302
        order.refresh_from_db()
        assert order.status == Order.Status.PROCESSING
        shipment = order.shipments.get()
        assert shipment.status == Shipment.Status.PROCESSING

    def test_cancelling_an_unpaid_order_from_the_admin(self, admin_client, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)

        admin_client.post(
            reverse("admin:orders_order_changelist"),
            {"action": "cancel_unpaid", ACTION_CHECKBOX_NAME: [str(order.pk)]},
        )

        order.refresh_from_db()
        assert order.status == Order.Status.CANCELLED
        assert Stock.objects.get(variant=variant).reserved == 0

    def test_a_paid_order_cannot_be_cancelled_from_the_admin(self, admin_client, user, product):
        order = place_and_pay(user, product.variants.first())

        response = admin_client.post(
            reverse("admin:orders_order_changelist"),
            {"action": "cancel_unpaid", ACTION_CHECKBOX_NAME: [str(order.pk)]},
            follow=True,
        )

        order.refresh_from_db()
        assert order.status == Order.Status.PAID
        assert "cannot go from" in response.content.decode()

    def test_the_order_change_page_renders_the_facts(self, admin_client, user, product):
        order = placed_order(user, product.variants.first())

        response = admin_client.get(reverse("admin:orders_order_change", args=[order.pk]))

        assert response.status_code == 200
        html = response.content.decode()
        assert order.number in html
        assert "Pending payment" in html

    def test_shipment_actions_walk_the_parcel(self, admin_client, user, product):
        order = place_and_pay(user, product.variants.first())
        mark_processing(order)
        shipment = order.shipments.get()

        admin_client.post(
            reverse("admin:orders_shipment_changelist"),
            {"action": "mark_shipped", ACTION_CHECKBOX_NAME: [str(shipment.pk)]},
        )
        shipment.refresh_from_db()
        order.refresh_from_db()
        assert shipment.status == Shipment.Status.SHIPPED
        assert order.status == Order.Status.SHIPPED


# =============================================================================
# The full HTTP flow (bag -> validate -> place -> pay)
# =============================================================================


class TestPlaceOrderFlow:
    def _place(self, client, user, variant):
        """Bag -> address -> validate -> place; returns (number, payment, checkout_id)."""
        sign_in(client)
        add_to_bag(client, variant, 2)
        address = make_address(user)
        client.post(reverse("shop:checkout-address"), {"address_id": address.pk})
        client.post(reverse("shop:checkout-validate"), {})

        page = client.get(reverse("shop:checkout"))
        match = re.search(r'name="checkout_id" value="(\d+)"', page.content.decode())
        assert match, "the checkout page must offer the handoff its session id"
        checkout_id = match.group(1)

        placed = client.post(reverse("shop:checkout-place"), {"checkout_id": checkout_id})
        assert placed.status_code == 302
        number = placed["Location"].split("/")[-2]
        assert placed["Location"] == reverse("shop:checkout-payment", args=[number])
        return number, checkout_id

    def test_bag_to_confirmation_the_whole_flow(self, client, user, product):
        variant = product.variants.first()
        seed_stock(variant, 3)

        number, checkout_id = self._place(client, user, variant)

        order = Order.objects.get(number=number)
        payment = Payment.objects.get(order=order)
        assert payment.status == Payment.Status.PENDING

        # A double-clicked "Place order" lands on the same payment step.
        again = client.post(reverse("shop:checkout-place"), {"checkout_id": checkout_id})
        assert again["Location"] == reverse("shop:checkout-payment", args=[number])
        assert Order.objects.count() == 1

        payment_page = client.get(again["Location"])
        assert payment_page.status_code == 200
        html = payment_page.content.decode()
        assert number in html
        assert "Simulate successful payment" in html
        assert "Development provider" in html

        done = client.post(
            reverse("payments:simulate", args=[payment.pk]), {"outcome": "succeeded"}
        )
        assert done.status_code == 302
        confirmation = client.get(done["Location"])
        assert confirmation.status_code == 200
        assert "Paid" in confirmation.content.decode()

        order.refresh_from_db()
        payment.refresh_from_db()
        assert order.status == Order.Status.PAID
        assert payment.status == Payment.Status.SUCCEEDED
        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved) == (1, 0)
        assert order.checkout.cart.status == Cart.Status.CONVERTED

        # The payment step now hands back to the confirmation page.
        follow_up = client.get(reverse("shop:checkout-payment", args=[number]))
        assert follow_up.status_code == 302
        assert follow_up["Location"] == reverse("shop:checkout-done", args=[number])

    def test_a_failed_payment_cancels_and_gives_the_stock_back(self, client, user, product):
        variant = product.variants.first()
        seed_stock(variant, 3)
        number, _ = self._place(client, user, variant)
        order = Order.objects.get(number=number)
        payment = Payment.objects.get(order=order)

        done = client.post(
            reverse("payments:simulate", args=[payment.pk]),
            {"outcome": "failed", "reason": "Card declined"},
        )
        confirmation = client.get(done["Location"])
        html = confirmation.content.decode()

        assert "This order was cancelled" in html
        assert "Card declined" in html
        order.refresh_from_db()
        payment.refresh_from_db()
        assert order.status == Order.Status.CANCELLED
        assert payment.status == Payment.Status.FAILED
        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved) == (3, 0)

    def test_placing_without_a_checkout_is_404(self, client, user):
        sign_in(client)

        response = client.post(reverse("shop:checkout-place"), {})

        assert response.status_code == 404

    def test_the_payment_and_confirmation_pages_are_yours_alone(
        self, client, user, other_user, product
    ):
        variant = product.variants.first()
        mine = placed_order(user, variant, stock=20)
        theirs = placed_order(other_user, variant, stock=None)
        sign_in(client)

        payment = client.get(reverse("shop:checkout-payment", args=[theirs.number]))
        confirmation = client.get(reverse("shop:checkout-done", args=[theirs.number]))
        own_payment = client.get(reverse("shop:checkout-payment", args=[mine.number]))

        assert payment.status_code == 404
        assert confirmation.status_code == 404
        assert own_payment.status_code == 200
        assert "Pay for your order" in own_payment.content.decode()
