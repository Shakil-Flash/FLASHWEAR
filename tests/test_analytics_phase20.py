"""Phase 20: the analytics event system, attribution and the Back Office funnel.

The contracts the spec called out, each as a test: events are server-authoritative and
allowlisted, identity and attribution come from a real request, conversions are idempotent
under a double click, credential-shaped metadata never reaches the table, private surfaces
write nothing, and the funnel screen is aggregates-only behind a staff capability that
customers never hold.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.analytics.middleware import COOKIE_NAME
from apps.analytics.models import Event, VisitorAttribution
from apps.analytics.services import record_event
from apps.backoffice.permissions import MODERATOR_GROUP, OPERATOR_GROUP
from apps.backoffice.selectors.analytics import funnel_summary
from apps.backoffice.selectors.common import DateRange, resolve_range
from apps.drops.models import DropProduct, FlashDrop
from apps.support.models import SupportTicket
from apps.support.services import tickets as ticket_service
from tests.phase6_helpers import place_and_pay

pytestmark = pytest.mark.django_db


def _bo(name: str) -> str:
    return reverse(f"backoffice:{name}")


def _staff(client, group_name: str, email: str):
    """An active staff account in ``group_name`` (is_staff and group, as the desk requires)."""
    staff = get_user_model().objects.create_user(email=email, password="Str0ng-Passw0rd!")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    staff.groups.add(Group.objects.get(name=group_name))
    client.force_login(staff)
    return staff


# --------------------------------------------------------------------------------------
# Events: allowlist, identity, privacy, idempotency
# --------------------------------------------------------------------------------------


class TestEventContracts:
    def test_an_unknown_event_name_is_refused(self, user):
        assert record_event("password_was_typed", user=user) is None
        assert Event.objects.count() == 0

    def test_a_product_view_carries_identity_and_object(self, client, user, product):
        client.force_login(user)
        client.get(product.get_absolute_url())

        event = Event.objects.get(name="product_view")
        assert event.user == user
        assert event.object_type == "product"
        assert event.object_id == product.pk
        assert event.metadata["slug"] == product.slug
        assert event.visitor_id  # minted by the middleware on this very request
        assert event.session_key

    def test_the_visitor_cookie_is_hardened(self, client, product):
        response = client.get(product.get_absolute_url())
        cookie = response.cookies[COOKIE_NAME]
        assert cookie.value
        assert cookie["httponly"] is True
        assert cookie["samesite"].lower() == "lax"

    def test_credential_shaped_metadata_never_reaches_the_table(self, user):
        record_event(
            "product_view",
            user=user,
            metadata={"password": "hunter2", "api_key": "sk-live-x", "slug": "tee"},
        )
        event = Event.objects.get(name="product_view")
        dumped = json.dumps(event.metadata)
        assert "hunter2" not in dumped
        assert "sk-live-x" not in dumped
        assert event.metadata["slug"] == "tee"

    def test_conversion_events_are_idempotent(self, user):
        for _ in range(2):
            record_event(
                "checkout_started",
                user=user,
                object_type="checkout",
                object_id=7,
                idempotency_key="checkout_started:7",
            )
        assert Event.objects.filter(name="checkout_started").count() == 1

    def test_the_checkout_page_records_one_started_event_per_checkout(self, client, user, product):
        from decimal import Decimal

        from apps.shop.models import Cart, CartItem

        client.force_login(user)
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart,
            variant=product.variants.first(),
            quantity=1,
            price_snapshot=Decimal("49.00"),
        )

        url = reverse("shop:checkout")
        assert client.get(url).status_code == 200
        assert client.get(url).status_code == 200  # a revisit must not double-count
        assert Event.objects.filter(name="checkout_started").count() == 1


# --------------------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------------------


class TestAttribution:
    def test_a_campaign_landing_is_captured_on_the_first_request(self, client, product):
        client.get(
            f"{product.get_absolute_url()}"
            "?utm_source=newsletter&utm_medium=email&utm_campaign=spring"
        )
        row = VisitorAttribution.objects.get()
        assert row.utm_source == "newsletter"
        assert row.utm_medium == "email"
        assert row.utm_campaign == "spring"
        assert row.landing_page.startswith("/products/")

    def test_first_touch_landing_survives_a_later_campaign(self, client, product):
        client.get(f"{product.get_absolute_url()}?utm_source=newsletter")
        first_landing = VisitorAttribution.objects.get().landing_page

        client.get(f"{product.get_absolute_url()}?utm_source=affiliate")

        row = VisitorAttribution.objects.get()
        assert row.landing_page == first_landing  # first touch keeps the landing page
        assert row.utm_source == "affiliate"  # last touch refreshes the campaign fields

    def test_our_own_referrer_is_navigation_not_attribution(self, client, product):
        client.get(product.get_absolute_url(), HTTP_REFERER="http://testserver/products/")
        assert VisitorAttribution.objects.get().referrer == ""

    def test_organic_visitors_get_their_row_with_their_first_event(self, client, user, product):
        # No UTM: the middleware writes nothing, the event service creates the row and
        # the first authenticated event claims it for the user.
        client.force_login(user)
        client.get(product.get_absolute_url())

        row = VisitorAttribution.objects.get()
        assert row.user == user
        assert row.landing_page.startswith("/products/")
        assert row.utm_source == ""

    def test_the_back_office_writes_no_attribution(self, client):
        _staff(client, OPERATOR_GROUP, "op-attribution@flashwear.test")
        response = client.get(_bo("dashboard"))
        assert response.status_code == 200
        assert VisitorAttribution.objects.count() == 0


# --------------------------------------------------------------------------------------
# Conversions: the signals and the derived drop purchases
# --------------------------------------------------------------------------------------


class TestConversions:
    def test_a_paid_order_records_the_purchase(self, user, product):
        order = place_and_pay(user, product.variants.first())

        event = Event.objects.get(name="purchase")
        assert event.user == user
        assert event.object_id == order.pk
        assert event.idempotency_key == f"purchase:{order.pk}"
        assert event.metadata["order_number"] == order.number

    def test_drop_purchases_are_derived_from_the_order(self, user, product):
        drop = FlashDrop.objects.create(
            name="Archive Sweep",
            slug="archive-sweep-analytics",
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        DropProduct.objects.create(drop=drop, product=product)

        order = place_and_pay(user, product.variants.first())

        event = Event.objects.get(name="drop_purchase")
        assert event.object_id == drop.pk
        assert event.user == user
        assert event.idempotency_key == f"drop_purchase:{order.pk}:{drop.pk}"

    def test_opening_a_ticket_records_it_once(self, user):
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Where is my parcel?",
            description="It said delivered but nothing arrived.",
            category=SupportTicket.Category.DELIVERY,
        )

        event = Event.objects.get(name="support_ticket_created")
        assert event.object_id == ticket.pk
        assert event.user == user
        assert event.idempotency_key == f"support_ticket_created:{ticket.pk}"


# --------------------------------------------------------------------------------------
# The funnel: aggregates only, gated by analytics.view
# --------------------------------------------------------------------------------------


class TestFunnelScreen:
    def test_an_operator_may_open_it(self, client):
        _staff(client, OPERATOR_GROUP, "op-analytics@flashwear.test")
        response = client.get(_bo("analytics"))
        assert response.status_code == 200
        assert "Analytics" in response.content.decode()

    def test_a_customer_gets_a_404(self, client, user):
        client.force_login(user)
        assert client.get(_bo("analytics")).status_code == 404

    def test_a_moderator_gets_a_404(self, client):
        _staff(client, MODERATOR_GROUP, "mod-analytics@flashwear.test")
        assert client.get(_bo("analytics")).status_code == 404

    def test_the_screen_never_shows_a_visitor_identifier(self, client):
        Event.objects.create(name="product_view", visitor_id="secret-visitor-id", metadata={})
        _staff(client, OPERATOR_GROUP, "op-privacy@flashwear.test")

        html = client.get(_bo("analytics")).content.decode()
        assert "secret-visitor-id" not in html


class TestFunnelNumbers:
    def test_every_stage_counts(self, user, product):
        record_event("product_view", user=user, object_type="product", object_id=product.pk)
        record_event("product_search", user=user, metadata={"query": "hoodie"})
        record_event("cart_add", user=user, object_type="variant", object_id=1)
        record_event("checkout_started", user=user, idempotency_key="checkout_started:1")
        order = place_and_pay(user, product.variants.first())

        summary = funnel_summary(date_range=resolve_range("all"))
        assert summary["visitors"] == 1
        assert summary["product_views"] == 1
        assert summary["searches"] == 1
        assert summary["cart_adds"] == 1
        assert summary["checkout_started"] == 1
        assert summary["orders_completed"] == 1
        assert summary["conversion_rate"] == 100.0
        assert summary["revenue"] == order.total
        assert summary["aov"] == order.total
        assert summary["top_products"][0]["name"] == product.name
        assert summary["top_searches"][0]["metadata__query"] == "hoodie"

    def test_zero_visitors_is_zero_not_a_divide_error(self):
        summary = funnel_summary(date_range=resolve_range("all"))
        assert summary["conversion_rate"] == 0.0
        assert summary["aov"] is None

    def test_an_empty_window_excludes_everything(self, user, product):
        record_event("product_view", user=user, object_type="product", object_id=product.pk)
        stale = DateRange(
            "old",
            "old",
            timezone.now() - timedelta(days=400),
            timezone.now() - timedelta(days=399),
        )
        summary = funnel_summary(date_range=stale)
        assert summary["product_views"] == 0
        assert summary["visitors"] == 0
        assert summary["orders_completed"] == 0

    def test_an_open_checkout_counts_as_abandoned(self, user, product):
        from decimal import Decimal

        from apps.shop.checkout import get_or_create_checkout
        from apps.shop.models import Cart, CartItem

        cart = Cart.objects.create(user=user)
        CartItem.objects.create(
            cart=cart,
            variant=product.variants.first(),
            quantity=1,
            price_snapshot=Decimal("49.00"),
        )
        get_or_create_checkout(user, cart)

        summary = funnel_summary(date_range=resolve_range("all"))
        assert summary["abandoned_checkouts"] == 1
