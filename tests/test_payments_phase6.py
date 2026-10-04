"""Payments Phase 6: the provider abstraction, webhooks and the money state machine.

Two paths deliver events -- the signed HTTP webhook and the development
simulation buttons -- and both land in
:func:`apps.payments.services.handle_provider_event`, so these tests exercise
one pipeline twice. The signature helper in :mod:`tests.phase6_helpers`
produces exactly what
:func:`apps.payments.providers.development.verify_webhook` consumes: the tested
path *is* the production path.
"""

import time
from dataclasses import replace

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.urls import reverse

from apps.inventory.models import Reservation, Stock
from apps.orders.models import Order, OrderEvent
from apps.orders.services import cancel_order
from apps.payments.models import Payment, PaymentEvent
from apps.payments.providers import WebhookError, get_provider, provider_name
from apps.payments.providers.development import make_provider_event, sign_payload
from apps.payments.services import cancel_payment, handle_provider_event, start_payment
from tests.phase6_helpers import (
    place_and_pay,
    placed_order,
    sign_in,
    signed_headers,
    webhook_body,
)

pytestmark = pytest.mark.django_db


# =============================================================================
# Registry
# =============================================================================


class TestProviderRegistry:
    def test_the_configured_provider_resolves(self):
        assert provider_name() == "development"
        assert get_provider().name == "development"

    def test_an_unknown_provider_refuses_to_load(self):
        with pytest.raises(ImproperlyConfigured) as exc:
            get_provider("stripe")

        assert "stripe" in str(exc.value)


# =============================================================================
# Starting an attempt
# =============================================================================


class TestStartPayment:
    def test_starting_creates_and_registers_the_attempt(self, user, product):
        order = placed_order(user, product.variants.first())

        payment = start_payment(order)

        assert payment.status == Payment.Status.PENDING
        assert payment.provider == "development"
        assert payment.provider_reference == f"dev-{payment.pk}"
        assert payment.amount == order.total
        assert payment.currency == order.currency
        assert order.events.filter(event_type=OrderEvent.Type.PAYMENT_PENDING).count() == 1

    def test_restarting_reuses_the_same_attempt(self, user, product):
        order = placed_order(user, product.variants.first())

        first = start_payment(order)
        second = start_payment(order)

        assert first.pk == second.pk
        assert Payment.objects.filter(order=order).count() == 1
        assert order.events.filter(event_type=OrderEvent.Type.PAYMENT_PENDING).count() == 1

    def test_a_new_order_gets_its_own_payment(self, user, product):
        variant = product.variants.first()
        first = placed_order(user, variant, stock=20)
        second = placed_order(user, variant, stock=None)

        start_payment(first)
        start_payment(second)

        assert Payment.objects.filter(order=first).count() == 1
        assert Payment.objects.filter(order=second).count() == 1
        assert first.number != second.number


# =============================================================================
# Applying provider events (the service layer)
# =============================================================================


class TestProviderEvents:
    def test_success_pays_the_order_and_consumes_the_stock(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)

        record = handle_provider_event("development", make_provider_event(payment, "succeeded"))

        assert record.event_type == Payment.Status.SUCCEEDED
        payment.refresh_from_db()
        order.refresh_from_db()
        assert payment.status == Payment.Status.SUCCEEDED
        assert payment.paid_at is not None
        assert order.status == Order.Status.PAID
        assert order.paid_at is not None
        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved) == (9, 0)
        assert order.reservations.get().status == Reservation.Status.CONSUMED
        assert order.events.filter(event_type=OrderEvent.Type.PAYMENT_SUCCEEDED).count() == 1
        assert order.events.filter(event_type=OrderEvent.Type.STOCK_CONSUMED).exists()

    def test_a_redelivered_event_applies_exactly_once(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)
        event = make_provider_event(payment, "succeeded")

        handle_provider_event("development", event)
        handle_provider_event("development", event)

        assert PaymentEvent.objects.filter(payment=payment).count() == 1
        assert Stock.objects.get(variant=variant).on_hand == 9
        assert order.events.filter(event_type=OrderEvent.Type.PAYMENT_SUCCEEDED).count() == 1

    def test_a_second_distinct_event_cannot_reapply_a_settled_payment(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)

        handle_provider_event("development", make_provider_event(payment, "succeeded"))
        handle_provider_event(
            "development",
            replace(make_provider_event(payment, "succeeded"), event_id="dev_other"),
        )

        assert PaymentEvent.objects.filter(payment=payment).count() == 2
        assert Stock.objects.get(variant=variant).on_hand == 9
        assert order.events.filter(event_type=OrderEvent.Type.PAYMENT_SUCCEEDED).count() == 1

    def test_failure_cancels_the_order_and_releases_the_stock(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)

        handle_provider_event(
            "development", make_provider_event(payment, "failed", reason="Card declined")
        )

        payment.refresh_from_db()
        order.refresh_from_db()
        assert payment.status == Payment.Status.FAILED
        assert payment.failure_message == "Card declined"
        assert order.status == Order.Status.CANCELLED
        assert order.reservations.get().status == Reservation.Status.RELEASED
        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved) == (10, 0)
        assert order.events.filter(event_type=OrderEvent.Type.PAYMENT_FAILED).count() == 1

    def test_a_provider_cancel_cancels_the_order(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)

        handle_provider_event("development", make_provider_event(payment, "cancelled"))

        order.refresh_from_db()
        assert order.status == Order.Status.CANCELLED
        assert order.reservations.get().status == Reservation.Status.RELEASED
        assert Stock.objects.get(variant=variant).reserved == 0
        assert order.events.filter(event_type=OrderEvent.Type.PAYMENT_CANCELLED).count() == 1

    def test_an_unknown_reference_is_refused(self, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        event = make_provider_event(payment, "succeeded")
        event = replace(event, payment_reference="dev-999999")

        with pytest.raises(WebhookError, match="No payment with reference"):
            handle_provider_event("development", event)

    def test_a_mismatched_amount_is_refused(self, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        event = make_provider_event(payment, "succeeded")
        event = replace(event, amount=payment.amount + 1)

        with pytest.raises(WebhookError, match="amount/currency"):
            handle_provider_event("development", event)

    def test_an_impossible_transition_is_refused(self, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        event = make_provider_event(payment, "succeeded")
        event = replace(event, event_type=Payment.Status.CREATED)

        with pytest.raises(WebhookError, match="cannot go from"):
            handle_provider_event("development", event)

    def test_events_for_another_provider_are_refused(self, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)

        with pytest.raises(WebhookError, match="not accepted"):
            handle_provider_event("stripe", make_provider_event(payment, "succeeded"))

    def test_money_for_an_order_that_was_already_given_up_stays_visible(self, user, product):
        """A late success on a cancelled order is a manual refund, never silent."""
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        cancel_order(order, event_type=OrderEvent.Type.STATUS_CHANGED)
        payment = start_payment(order)

        handle_provider_event("development", make_provider_event(payment, "succeeded"))

        order.refresh_from_db()
        assert order.status == Order.Status.CANCELLED
        assert Payment.objects.get(pk=payment.pk).status == Payment.Status.SUCCEEDED
        notes = [event.note for event in order.events.filter(event_type=OrderEvent.Type.NOTE)]
        assert any("manual refund required" in note for note in notes)
        stock = Stock.objects.get(variant=variant)
        assert (stock.on_hand, stock.reserved) == (10, 0)


# =============================================================================
# Cancelling an attempt
# =============================================================================


class TestCancelPayment:
    def test_cancelling_the_attempt_cancels_the_order_and_releases_stock(self, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)

        cancel_payment(payment, reason="Changed my mind")

        payment.refresh_from_db()
        order.refresh_from_db()
        assert payment.status == Payment.Status.CANCELLED
        assert order.status == Order.Status.CANCELLED
        assert order.reservations.get().status == Reservation.Status.RELEASED
        assert Stock.objects.get(variant=variant).reserved == 0
        assert PaymentEvent.objects.filter(payment=payment).count() == 1

    def test_cancelling_twice_is_a_no_op(self, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)

        cancel_payment(payment)
        cancel_payment(payment)

        payment.refresh_from_db()
        assert PaymentEvent.objects.filter(payment=payment).count() == 1
        assert payment.status == Payment.Status.CANCELLED

    def test_a_succeeded_payment_cannot_be_cancelled(self, user, product):
        order = place_and_pay(user, product.variants.first())
        payment = order.payment

        returned = cancel_payment(payment)

        assert returned.status == Payment.Status.SUCCEEDED
        assert Order.objects.get(pk=order.pk).status == Order.Status.PAID

    def test_cancelling_before_provider_registration_completes(self, user, product):
        order = placed_order(user, product.variants.first())
        payment = Payment.objects.create(
            order=order,
            provider="development",
            amount=order.total,
            currency=order.currency,
        )

        cancel_payment(payment)

        payment.refresh_from_db()
        assert payment.status == Payment.Status.CANCELLED
        assert payment.provider_reference == f"dev-{payment.pk}"
        assert Order.objects.get(pk=order.pk).status == Order.Status.CANCELLED


# =============================================================================
# The signed webhook endpoint
# =============================================================================


class TestWebhookEndpoint:
    def _post(self, client, raw: bytes, *, sign=True, **sign_kwargs):
        headers = signed_headers(raw, **sign_kwargs) if sign else {}
        return client.post(
            reverse("payments:webhook", args=["development"]),
            data=raw,
            content_type="application/json",
            headers=headers,
        )

    def test_a_signed_delivery_is_accepted_and_applied(self, client, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)
        raw = webhook_body(payment, "succeeded")

        response = self._post(client, raw)

        assert response.status_code == 200
        assert response.json()["received"] is True
        assert PaymentEvent.objects.filter(payment=payment).exists()
        order.refresh_from_db()
        assert order.status == Order.Status.PAID
        assert Stock.objects.get(variant=variant).on_hand == 9

    def test_a_bad_signature_is_rejected(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        raw = webhook_body(payment, "succeeded")

        response = self._post(client, raw, secret="not-the-webhook-secret")

        assert response.status_code == 401
        assert Order.objects.get(pk=order.pk).status == Order.Status.PENDING_PAYMENT

    def test_missing_headers_are_rejected(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        raw = webhook_body(payment, "succeeded")

        response = client.post(
            reverse("payments:webhook", args=["development"]),
            data=raw,
            content_type="application/json",
        )

        assert response.status_code == 401

    def test_a_tampered_body_fails_the_signature(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        signed = webhook_body(payment, "succeeded")
        headers = signed_headers(signed)
        tampered = webhook_body(payment, "failed")

        response = client.post(
            reverse("payments:webhook", args=["development"]),
            data=tampered,
            content_type="application/json",
            headers=headers,
        )

        assert response.status_code == 401

    def test_a_stale_timestamp_is_refused(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        raw = webhook_body(payment, "succeeded")
        old = str(int(time.time()) - 4000)

        response = self._post(client, raw, timestamp=old)

        assert response.status_code == 400
        assert "tolerance" in response.json()["detail"]

    def test_an_unknown_provider_is_404(self, client):
        response = client.post(
            reverse("payments:webhook", args=["stripe"]),
            data=b"{}",
            content_type="application/json",
        )

        assert response.status_code == 404

    def test_an_unknown_payment_reference_is_409(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        raw = webhook_body(payment, "succeeded", reference="dev-999999")

        response = self._post(client, raw)

        assert response.status_code == 409

    def test_a_mismatched_amount_is_409(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        raw = webhook_body(payment, "succeeded", amount="0.01")

        response = self._post(client, raw)

        assert response.status_code == 409
        assert "amount/currency" in response.json()["detail"]

    def test_a_malformed_body_is_400_even_when_signed(self, client, user, product):
        order = placed_order(user, product.variants.first())
        start_payment(order)

        response = self._post(client, b"this is not json")

        assert response.status_code == 400

    def test_an_unsupported_event_type_is_400(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        raw = webhook_body(payment, "succeeded").replace(b'"succeeded"', b'"too-early"')

        response = self._post(client, raw)

        assert response.status_code == 400

    def test_a_redelivered_delivery_changes_nothing(self, client, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)
        raw = webhook_body(payment, "succeeded")
        headers = signed_headers(raw)

        first = client.post(
            reverse("payments:webhook", args=["development"]),
            data=raw,
            content_type="application/json",
            headers=headers,
        )
        second = client.post(
            reverse("payments:webhook", args=["development"]),
            data=raw,
            content_type="application/json",
            headers=headers,
        )

        assert first.status_code == second.status_code == 200
        assert PaymentEvent.objects.filter(payment=payment).count() == 1
        assert Stock.objects.get(variant=variant).on_hand == 9

    def test_a_get_is_not_allowed(self, client):
        response = client.get(reverse("payments:webhook", args=["development"]))

        assert response.status_code == 405


# =============================================================================
# Development simulation / cancel endpoints
# =============================================================================


class TestSimulationEndpoints:
    def test_simulating_success_lands_on_the_confirmation(self, client, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)
        sign_in(client)

        response = client.post(
            reverse("payments:simulate", args=[payment.pk]), {"outcome": "succeeded"}
        )

        assert response.status_code == 302
        assert response["Location"] == reverse("shop:checkout-done", args=[order.number])
        order.refresh_from_db()
        assert order.status == Order.Status.PAID
        assert Stock.objects.get(variant=variant).on_hand == 9

    def test_pending_can_be_left_and_then_settled(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        sign_in(client)

        client.post(reverse("payments:simulate", args=[payment.pk]), {"outcome": "pending"})
        payment.refresh_from_db()
        assert payment.status == Payment.Status.PENDING

        client.post(reverse("payments:simulate", args=[payment.pk]), {"outcome": "succeeded"})
        payment.refresh_from_db()
        assert payment.status == Payment.Status.SUCCEEDED
        assert PaymentEvent.objects.filter(payment=payment).count() == 2

    def test_settled_payments_redirect_instead_of_reprocessing(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        sign_in(client)
        client.post(reverse("payments:simulate", args=[payment.pk]), {"outcome": "succeeded"})

        response = client.post(
            reverse("payments:simulate", args=[payment.pk]), {"outcome": "failed"}
        )

        assert response.status_code == 302
        payment.refresh_from_db()
        assert payment.status == Payment.Status.SUCCEEDED

    def test_you_cannot_simulate_someone_elses_payment(self, client, user, other_user, product):
        order = placed_order(other_user, product.variants.first())
        payment = start_payment(order)
        sign_in(client)

        response = client.post(
            reverse("payments:simulate", args=[payment.pk]), {"outcome": "succeeded"}
        )

        assert response.status_code == 404

    def test_an_unknown_outcome_is_400(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)
        sign_in(client)

        response = client.post(reverse("payments:simulate", args=[payment.pk]), {"outcome": "free"})

        assert response.status_code == 400

    def test_simulation_requires_a_signed_in_customer(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)

        response = client.post(
            reverse("payments:simulate", args=[payment.pk]), {"outcome": "succeeded"}
        )

        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_simulation_disappears_for_a_real_provider(self, client, user, product):
        order = placed_order(user, product.variants.first())
        payment = start_payment(order)  # under the development provider...
        sign_in(client)

        with override_settings(PAYMENT_PROVIDER="stripe"):
            response = client.post(
                reverse("payments:simulate", args=[payment.pk]), {"outcome": "succeeded"}
            )

        assert response.status_code == 404

    def test_the_cancel_button_cancels_payment_order_and_stock(self, client, user, product):
        variant = product.variants.first()
        order = placed_order(user, variant, stock=10)
        payment = start_payment(order)
        sign_in(client)

        response = client.post(reverse("payments:cancel", args=[payment.pk]))

        assert response.status_code == 302
        payment.refresh_from_db()
        order.refresh_from_db()
        assert payment.status == Payment.Status.CANCELLED
        assert order.status == Order.Status.CANCELLED
        assert Stock.objects.get(variant=variant).reserved == 0

    def test_the_cancel_button_ignores_foreign_payments(self, client, user, other_user, product):
        order = placed_order(other_user, product.variants.first())
        payment = start_payment(order)
        sign_in(client)

        response = client.post(reverse("payments:cancel", args=[payment.pk]))

        assert response.status_code == 404


# =============================================================================
# Signature helper (unit)
# =============================================================================


class TestSignature:
    def test_signatures_depend_on_the_timestamp_and_the_body(self):
        stamp, first = sign_payload(b'{"a": 1}', timestamp="1000")
        _, same = sign_payload(b'{"a": 1}', timestamp="1000")
        _, other_secret = sign_payload(b'{"a": 1}', timestamp="1000", secret="different")
        _, other_body = sign_payload(b'{"a": 2}', timestamp="1000")

        assert stamp == "1000"
        assert first == same
        assert first != other_secret
        assert first != other_body

    def test_the_signature_format_is_the_documented_one(self):
        _, signature = sign_payload(b"{}", timestamp="0")

        assert signature.startswith("sha256=")
        assert len(signature) == len("sha256=") + 64
