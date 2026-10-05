"""Phase 16: the FLASH Operations back office -- ``/operations/`` and its API.

The matrix this module walks is the one the spec writes down: who may open which screen
(and that a miss answers **404, never 403**), that every write runs through the domain
service and lands exactly one audit row, that the audit log cannot be rewritten by *any*
surface, that list screens withhold customer content and provider payloads, and that the
JSON API under ``/api/v1/backoffice/`` keeps the same contract as the HTML it mirrors.

Fixtures are built with the real services (orders are placed and paid through
``phase6_helpers``), because a desk test that hand-rolled an order would pass against a
state production can never reach.
"""

from __future__ import annotations

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.messages.constants import ERROR
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.urls import reverse

from apps.backoffice.models import AuditEvent
from apps.backoffice.permissions import (
    ADMIN_GROUP,
    AUDIT_VIEW,
    BACKOFFICE_GROUPS,
    CATALOG_GROUP,
    FINANCE_GROUP,
    INVENTORY_GROUP,
    MARKETING_GROUP,
    MODERATOR_GROUP,
    OPERATOR_GROUP,
    OPS_VIEW,
    ORDERS_GROUP,
    capabilities_for,
    has,
    navigation_for,
)
from apps.backoffice.services import audit as audit_service
from apps.backoffice.services.alerts import alerts_for
from apps.catalog.models import Product
from apps.core import metrics
from apps.engagement.models import Review
from apps.engagement.services.loyalty import balance_for
from apps.inventory.models import InventoryMovement, Stock
from apps.loop.models import LoopItem
from apps.orders.models import Order, OrderEvent
from apps.support.models import SupportTicket
from apps.support.permissions import AGENT_GROUP as SUPPORT_AGENT_GROUP
from apps.support.permissions import MANAGER_GROUP as SUPPORT_MANAGER_GROUP
from apps.support.services import tickets as ticket_service
from tests.phase6_helpers import make_address, place_and_pay, seed_stock
from tests.phase7_helpers import make_review

pytestmark = pytest.mark.django_db

PASSWORD = "Str0ng-Passw0rd!"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def bo(name: str, *args) -> str:
    return reverse(f"backoffice:{name}", args=args)


def staff_user(email: str, group_name: str | None = None):
    """An active staff account, optionally in a back-office group.

    ``is_staff`` and the group are separate on purpose: every access test below needs to
    prove that neither alone opens the desk.
    """
    user = get_user_model().objects.create_user(email=email, password=PASSWORD)
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    if group_name:
        user.groups.add(Group.objects.get(name=group_name))
    return user


def sign_in(client, group_name: str, email: str = "operator@flashwear.test"):
    """Log in a fresh staff user who holds ``group_name`` and return them."""
    user = staff_user(email, group_name)
    client.force_login(user)
    return user


def error_texts(response) -> list[str]:
    return [str(message) for message in response.context["messages"] if message.level == ERROR]


# --------------------------------------------------------------------------------------
# Access: the capability matrix
# --------------------------------------------------------------------------------------

#: ``(url_name, a group that may open it, a group that may not)`` -- one row per screen.
READ_SCREENS = [
    ("orders", ORDERS_GROUP, INVENTORY_GROUP),
    ("payments", FINANCE_GROUP, MODERATOR_GROUP),
    ("shipments", ORDERS_GROUP, INVENTORY_GROUP),
    ("customers", OPERATOR_GROUP, MODERATOR_GROUP),
    ("products", CATALOG_GROUP, ORDERS_GROUP),
    ("inventory", INVENTORY_GROUP, ORDERS_GROUP),
    ("drops", MARKETING_GROUP, ORDERS_GROUP),
    ("promotions", MARKETING_GROUP, ORDERS_GROUP),
    ("quests", MARKETING_GROUP, ORDERS_GROUP),
    ("reviews", MODERATOR_GROUP, FINANCE_GROUP),
    ("loop", MODERATOR_GROUP, FINANCE_GROUP),
    ("support", SUPPORT_AGENT_GROUP, FINANCE_GROUP),
    ("points", FINANCE_GROUP, MODERATOR_GROUP),
    ("audit", ORDERS_GROUP, OPERATOR_GROUP),
    ("staff", ADMIN_GROUP, ORDERS_GROUP),
]

ALL_SCREEN_NAMES = [name for name, _allowed, _denied in READ_SCREENS] + [
    "dashboard",
    "alerts",
    "health",
]


class TestAccessMatrix:
    def test_anonymous_is_sent_to_login(self, client):
        for name in ("dashboard", "orders", "audit"):
            response = client.get(bo(name))
            assert response.status_code == 302
            assert response.url.startswith("/accounts/login/")

    def test_the_staff_flag_alone_opens_nothing(self, client, user):
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        client.force_login(user)
        assert client.get(bo("dashboard")).status_code == 404
        assert client.get(bo("orders")).status_code == 404

    def test_a_group_without_the_staff_flag_opens_nothing(self, client, user):
        user.groups.add(Group.objects.get(name=ORDERS_GROUP))
        client.force_login(user)
        assert client.get(bo("orders")).status_code == 404

    @pytest.mark.parametrize("name,allowed,denied", READ_SCREENS)
    def test_each_screen_is_gated_by_its_own_capability(self, client, name, allowed, denied):
        allowed_key = allowed.split()[0].lower()
        sign_in(client, allowed, email=f"{allowed_key}-{name}@flashwear.test")
        assert client.get(bo(name)).status_code == 200

        client.logout()
        denied_key = denied.split()[0].lower()
        sign_in(client, denied, email=f"{denied_key}-{name}@flashwear.test")
        assert client.get(bo(name)).status_code == 404

    @pytest.mark.parametrize("name", ALL_SCREEN_NAMES)
    def test_a_superuser_opens_every_screen(self, client, admin_user, name):
        client.force_login(admin_user)
        assert client.get(bo(name)).status_code == 200

    def test_an_unknown_capability_is_an_error_not_a_silent_deny(self, admin_user):
        with pytest.raises(KeyError):
            has(admin_user, "ops.everything")

    def test_the_capability_set_follows_the_group_table(self, admin_user):
        assert capabilities_for(admin_user)  # superuser: everything
        operator = staff_user("ops@flashwear.test", OPERATOR_GROUP)
        caps = capabilities_for(operator)
        assert OPS_VIEW in caps
        assert AUDIT_VIEW not in caps

    def test_navigation_offers_only_what_the_user_may_open(self, admin_user):
        admin_nav = navigation_for(admin_user)
        admin_urls = {item["url_name"] for group in admin_nav for item in group["items"]}
        assert "backoffice:staff" in admin_urls

        operator = staff_user("sidebar@flashwear.test", OPERATOR_GROUP)
        operator_urls = {
            item["url_name"] for group in navigation_for(operator) for item in group["items"]
        }
        assert "backoffice:staff" not in operator_urls
        assert "backoffice:audit" not in operator_urls
        # Every link offered resolves -- a sidebar entry must never 404.
        for url_name in operator_urls:
            reverse(url_name)

    def test_every_backoffice_group_exists_in_the_database(self):
        names = set(Group.objects.values_list("name", flat=True))
        assert set(BACKOFFICE_GROUPS) <= names
        # Support's two rows are owned by the support app's migration, not re-seeded here.
        assert {"Support Agent", "Support Manager"} <= names


# --------------------------------------------------------------------------------------
# Dashboard, range filters, alerts
# --------------------------------------------------------------------------------------


class TestDashboard:
    def test_the_numbers_are_real_rows(self, client, admin_user, user, product):
        place_and_pay(user, product.variants.first())
        client.force_login(admin_user)

        response = client.get(bo("dashboard"))
        assert response.status_code == 200
        summary = response.context["summary"]
        assert summary["sales"]["placed"] == 1
        assert summary["sales"]["paid"] == 1
        assert summary["pipeline"][Order.Status.PAID] == 1
        # The tile reports the figure; the queue behind it carries the numbers.
        assert bo("orders") in response.content.decode()

    def test_the_window_filters_the_figures(self, client, admin_user, user, product):
        place_and_pay(user, product.variants.first())
        client.force_login(admin_user)

        assert (
            client.get(bo("dashboard"), {"range": "all"}).context["summary"]["sales"]["placed"] == 1
        )
        assert (
            client.get(bo("dashboard"), {"range": "yesterday"}).context["summary"]["sales"][
                "placed"
            ]
            == 0
        )

    def test_an_unknown_range_falls_back_to_the_default(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get(bo("dashboard"), {"range": "nonsense"})
        assert response.status_code == 200
        assert response.context["current_range"] == "7d"

    def test_every_tile_links_to_a_screen_that_exists(self, client, admin_user):
        client.force_login(admin_user)
        body = client.get(bo("dashboard")).content.decode()
        for name in ("orders", "inventory", "support", "reviews"):
            assert bo(name) in body

    def test_the_dashboard_is_not_indexed(self, client, admin_user):
        client.force_login(admin_user)
        assert "noindex, nofollow" in client.get(bo("dashboard")).content.decode()

    def test_the_sidebar_matches_the_capability_set(self, client, admin_user):
        client.force_login(admin_user)
        admin_body = client.get(bo("dashboard")).content.decode()
        assert bo("staff") in admin_body

        client.logout()
        sign_in(client, ORDERS_GROUP, email="sales@flashwear.test")
        body = client.get(bo("dashboard")).content.decode()
        assert bo("staff") not in body
        assert bo("orders") in body

    def test_the_dashboard_stays_within_its_query_budget(
        self, client, admin_user, django_assert_max_num_queries
    ):
        client.force_login(admin_user)
        with django_assert_max_num_queries(60):
            client.get(bo("dashboard"))

    def test_the_orders_queue_stays_within_its_query_budget(
        self, client, admin_user, django_assert_max_num_queries
    ):
        client.force_login(admin_user)
        with django_assert_max_num_queries(30):
            client.get(bo("orders"))


class TestAlerts:
    def _with_out_of_stock(self, product):
        seed_stock(product.variants.first(), 0)

    def test_the_rules_are_deterministic(self, admin_user, product):
        self._with_out_of_stock(product)
        assert alerts_for(admin_user) == alerts_for(admin_user)

    def test_severity_is_a_word_not_only_a_colour(self, admin_user, product):
        self._with_out_of_stock(product)
        fired = alerts_for(admin_user)
        assert fired
        assert all(alert.label in {"Critical", "Warning", "Info"} for alert in fired)
        assert any(alert.label == "Critical" for alert in fired)

    def test_alerts_are_filtered_by_capability(self, admin_user, product):
        self._with_out_of_stock(product)
        assert "out_of_stock" in {alert.key for alert in alerts_for(admin_user)}

        sales = staff_user("sales-alerts@flashwear.test", ORDERS_GROUP)
        assert "out_of_stock" not in {alert.key for alert in alerts_for(sales)}

    def test_a_clean_shop_fires_nothing(self, admin_user):
        assert alerts_for(admin_user) == []

    def test_the_alerts_page_explains_a_fired_rule(self, client, admin_user, product):
        self._with_out_of_stock(product)
        client.force_login(admin_user)
        body = client.get(bo("alerts")).content.decode()
        assert "Variants out of stock" in body
        assert "Critical" in body

    def test_the_severity_filter_is_allowlisted(self, client, admin_user, product):
        self._with_out_of_stock(product)
        client.force_login(admin_user)
        assert client.get(bo("alerts"), {"severity": "critical"}).status_code == 200
        assert client.get(bo("alerts"), {"severity": "everything"}).status_code == 200


class TestSystemHealth:
    """``/operations/health/``: live probes, honest telemetry, no leaked configuration."""

    def test_the_page_answers_with_live_probes(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get(bo("health"))
        assert response.status_code == 200
        report = response.context["report"]
        checks = {row["label"]: row["status"] for row in report["checks"]}
        # The broker is skipped under eager execution -- honest, not optimistically "ok".
        assert checks == {"Database": "ok", "Cache": "ok", "Celery broker": "skipped"}
        assert report["healthy"] is True
        # Environment labels only: no URL, hostname or credential reaches the page.
        assert "redis://" not in response.content.decode()

    def test_a_down_probe_is_reported_not_hidden(self, client, admin_user, monkeypatch):
        from apps.core import health as health_checks

        monkeypatch.setattr(health_checks, "check_database", lambda: "down")
        client.force_login(admin_user)
        response = client.get(bo("health"))
        report = response.context["report"]
        checks = {row["label"]: row["status"] for row in report["checks"]}
        assert checks["Database"] == "down"
        assert report["healthy"] is False
        assert "A dependency is down" in response.content.decode()

    def test_the_counters_that_exist_reach_the_page(self, client, admin_user):
        metrics.mark_request(500, 120)
        metrics.mark_request(200, 40)
        client.force_login(admin_user)
        # The middleware counts this very response *after* the view renders its context,
        # so the snapshot sees exactly the two calls above.
        report = client.get(bo("health")).context["report"]
        assert report["metrics"]["requests"]["5xx"] == 1
        assert report["metrics"]["requests"]["2xx"] == 1

    def test_queue_depth_admits_it_cannot_be_measured(self, client, admin_user):
        client.force_login(admin_user)
        report = client.get(bo("health")).context["report"]
        # No Redis in tests: the number must be absent, never fabricated.
        assert report["metrics"]["queue_depth"] is None
        assert report["metrics"]["queue_depth_measured"] is False

    def test_the_page_carries_the_same_alert_rules_as_the_alerts_screen(
        self, client, admin_user, product
    ):
        seed_stock(product.variants.first(), 0)  # fires the out-of-stock rule
        client.force_login(admin_user)
        response = client.get(bo("health"))
        # One rule set, three screens -- this page must not grow rules of its own.
        assert response.context["alerts"] == alerts_for(admin_user)
        assert "Variants out of stock" in response.content.decode()

    def test_the_screen_is_gated_like_the_rest_of_the_overview(self, client):
        client.force_login(staff_user("health-no-group@flashwear.test"))
        assert client.get(bo("health")).status_code == 404

        client.logout()
        assert client.get(bo("health")).status_code == 302


# --------------------------------------------------------------------------------------
# Sales: orders, customers, payments
# --------------------------------------------------------------------------------------


class TestOrdersQueue:
    @pytest.fixture
    def order(self, user, product):
        return place_and_pay(user, product.variants.first())

    @pytest.fixture
    def manager(self, client):
        user = staff_user("order-desk@flashwear.test", ORDERS_GROUP)
        client.force_login(user)
        return user

    def test_the_queue_lists_the_order(self, client, manager, order):
        body = client.get(bo("orders")).content.decode()
        assert order.number in body

    def test_the_status_filter_only_honours_known_states(self, client, manager, order):
        assert order.number in client.get(bo("orders"), {"status": "paid"}).content.decode()
        assert (
            order.number not in client.get(bo("orders"), {"status": "cancelled"}).content.decode()
        )
        # An unknown value is dropped rather than honoured literally.
        assert order.number in client.get(bo("orders"), {"status": "nonsense"}).content.decode()

    def test_a_crafted_sort_cannot_break_the_page(self, client, manager, order):
        response = client.get(bo("orders"), {"sort": "-total); drop table order--"})
        assert response.status_code == 200
        assert order.number in response.content.decode()

    def test_search_by_number_and_email(self, client, manager, order, user):
        assert order.number in client.get(bo("orders"), {"q": order.number}).content.decode()
        assert order.number in client.get(bo("orders"), {"q": user.email}).content.decode()

    def test_the_detail_page_shows_the_timeline(self, client, manager, order):
        body = client.get(bo("order_detail", order.number)).content.decode()
        assert order.number in body

    def test_an_unknown_order_number_is_404(self, client, manager):
        assert client.get(bo("order_detail", "FW-2099-999999")).status_code == 404

    def test_marking_processing_runs_the_service_and_audits_it(self, client, manager, order):
        response = client.post(bo("order_action", order.number), {"action": "processing"})
        assert response.status_code == 302
        order.refresh_from_db()
        assert order.status == Order.Status.PROCESSING
        assert order.shipments.exists()
        row = AuditEvent.objects.get(action="order.status", object_id=order.number)
        assert row.actor == manager
        assert row.metadata["transition"] == "processing"

    def test_an_illegal_transition_is_refused_with_no_audit_row(self, client, manager, order):
        response = client.post(bo("order_action", order.number), {"action": "deliver"}, follow=True)
        order.refresh_from_db()
        assert order.status == Order.Status.PAID
        assert error_texts(response)
        assert not AuditEvent.objects.filter(action="order.status", object_id=order.number).exists()

    def test_a_get_never_changes_state(self, client, manager, order):
        assert client.get(bo("order_action", order.number)).status_code == 405
        order.refresh_from_db()
        assert order.status == Order.Status.PAID

    def test_a_viewer_cannot_advance_an_order(self, client, order, user):
        sign_in(client, OPERATOR_GROUP, email="viewer@flashwear.test")
        response = client.post(bo("order_action", order.number), {"action": "processing"})
        assert response.status_code == 404
        order.refresh_from_db()
        assert order.status == Order.Status.PAID

    def test_an_internal_note_is_written_to_the_timeline_and_the_log(self, client, manager, order):
        client.post(bo("order_note", order.number), {"note": "Called the customer."})
        assert OrderEvent.objects.filter(
            order=order, event_type=OrderEvent.Type.NOTE, note="Called the customer."
        ).exists()
        assert AuditEvent.objects.filter(action="order.note").exists()


class TestCustomerPrivacy:
    def test_the_customer_screen_carries_counters_not_content(
        self, client, admin_user, user, product
    ):
        order = place_and_pay(user, product.variants.first())
        make_address(user, line1="42 Secret Lane")
        make_review(
            order,
            user,
            product,
            body="A review body that must never appear on an operations screen.",
        )

        client.force_login(admin_user)
        body = client.get(bo("customers")).content.decode()
        assert user.email in body
        assert "42 Secret Lane" not in body
        assert "must never appear on an operations screen" not in body


class TestPaymentPrivacy:
    @pytest.fixture
    def payment(self, user, product):
        from apps.payments.models import Payment

        order = place_and_pay(user, product.variants.first())
        payment = Payment.objects.get(order=order)
        event = payment.events.first()
        event.payload = {"provider": "development", "note": "SENSITIVE-PAYLOAD"}
        event.save(update_fields=["payload"])
        return payment

    def test_the_desk_never_sees_the_provider_payload(self, client, admin_user, payment):
        client.force_login(admin_user)
        assert "SENSITIVE-PAYLOAD" not in client.get(bo("payments")).content.decode()
        detail = client.get(bo("payment_detail", payment.pk))
        assert detail.status_code == 200
        assert "SENSITIVE-PAYLOAD" not in detail.content.decode()


# --------------------------------------------------------------------------------------
# Merchandising: inventory and the catalogue bulk picker
# --------------------------------------------------------------------------------------


class TestInventory:
    @pytest.fixture
    def variant(self, product):
        variant = product.variants.first()
        seed_stock(variant, 10)
        return variant

    @pytest.fixture
    def keeper(self, client):
        user = staff_user("stock@flashwear.test", INVENTORY_GROUP)
        client.force_login(user)
        return user

    def test_the_page_shows_counters_and_the_form(self, client, keeper, variant):
        body = client.get(bo("inventory")).content.decode()
        assert variant.sku in body

    def test_a_positive_adjustment_moves_stock_and_is_audited(self, client, keeper, variant):
        response = client.post(
            bo("inventory_adjust"),
            {
                "variant": variant.pk,
                "delta": 5,
                "kind": "received",
                "note": "Delivery 77",
                "reason": "Purchase order 77",
            },
        )
        assert response.status_code == 302
        assert Stock.objects.get(variant=variant).on_hand == 15
        movement = InventoryMovement.objects.get(variant=variant, kind="received")
        assert movement.on_hand_delta == 5
        assert movement.user == keeper
        row = AuditEvent.objects.get(action="inventory.adjust")
        assert row.metadata["delta"] == 5

    def test_removing_more_than_exists_is_refused(self, client, keeper, variant):
        seed_stock(variant, 0)
        response = client.post(
            bo("inventory_adjust"),
            {
                "variant": variant.pk,
                "delta": -5,
                "kind": "adjustment",
                "note": "",
                "reason": "Shrinkage",
            },
            follow=True,
        )
        assert error_texts(response)
        assert Stock.objects.get(variant=variant).on_hand == 0
        assert not InventoryMovement.objects.filter(variant=variant).exists()
        assert not AuditEvent.objects.filter(action="inventory.adjust").exists()

    def test_a_zero_delta_writes_nothing(self, client, keeper, variant):
        response = client.post(
            bo("inventory_adjust"),
            {
                "variant": variant.pk,
                "delta": 0,
                "kind": "adjustment",
                "note": "",
                "reason": "Nothing at all",
            },
            follow=True,
        )
        assert error_texts(response)
        assert not InventoryMovement.objects.filter(variant=variant).exists()

    def test_a_viewer_cannot_adjust_stock(self, client, variant):
        sign_in(client, OPERATOR_GROUP, email="viewer-stock@flashwear.test")
        response = client.post(
            bo("inventory_adjust"),
            {
                "variant": variant.pk,
                "delta": 5,
                "kind": "received",
                "note": "",
                "reason": "Not allowed",
            },
        )
        assert response.status_code == 404
        assert Stock.objects.get(variant=variant).on_hand == 10

    def test_a_get_never_moves_stock(self, client, keeper, variant):
        assert client.get(bo("inventory_adjust")).status_code == 405
        assert Stock.objects.get(variant=variant).on_hand == 10


class TestCatalogueBulk:
    @pytest.fixture
    def cataloguer(self, client):
        user = staff_user("catalogue@flashwear.test", CATALOG_GROUP)
        client.force_login(user)
        return user

    def test_publishing_the_selection_runs_the_service_and_is_audited(
        self, client, cataloguer, draft_product
    ):
        response = client.post(
            bo("products_bulk"),
            {
                "selected": [draft_product.pk],
                "action": "publish",
                "reason": "Spring drop",
            },
        )
        assert response.status_code == 302
        draft_product.refresh_from_db()
        assert draft_product.status == draft_product.Status.ACTIVE
        assert AuditEvent.objects.filter(action="catalog.publish").exists()

    def test_one_bad_row_is_reported_while_the_rest_still_move(
        self, client, cataloguer, draft_product, make_product, inactive_category
    ):
        parked = make_product(
            name="Parked Tee", category=inactive_category, status=Product.Status.DRAFT
        )
        response = client.post(
            bo("products_bulk"),
            {
                "selected": [draft_product.pk, parked.pk],
                "action": "publish",
                "reason": "Publish everything",
            },
            follow=True,
        )
        draft_product.refresh_from_db()
        parked.refresh_from_db()
        assert draft_product.status == draft_product.Status.ACTIVE
        assert parked.status == parked.Status.DRAFT
        assert error_texts(response)

    def test_an_empty_selection_changes_nothing(self, client, cataloguer, draft_product):
        response = client.post(
            bo("products_bulk"),
            {"action": "publish", "reason": "Nothing selected"},
            follow=True,
        )
        draft_product.refresh_from_db()
        assert draft_product.status == draft_product.Status.DRAFT
        assert error_texts(response)
        assert not AuditEvent.objects.filter(action="catalog.publish").exists()

    def test_a_viewer_cannot_bulk_edit(self, client, draft_product):
        sign_in(client, ORDERS_GROUP, email="sales-catalogue@flashwear.test")
        response = client.post(
            bo("products_bulk"),
            {"selected": [draft_product.pk], "action": "publish", "reason": "no"},
        )
        assert response.status_code == 404


# --------------------------------------------------------------------------------------
# Community: reviews, FLASH Loop, support, points
# --------------------------------------------------------------------------------------


class TestModeration:
    @pytest.fixture
    def pending_review(self, user, product):
        order = place_and_pay(user, product.variants.first())
        return make_review(
            order,
            user,
            product,
            status=Review.Status.PENDING,
            body="Awaiting a moderators careful eye before it goes live.",
        )

    @pytest.fixture
    def moderator(self, client):
        user = staff_user("moderator@flashwear.test", MODERATOR_GROUP)
        client.force_login(user)
        return user

    def test_the_queue_offers_the_pending_review(self, client, moderator, pending_review):
        body = client.get(bo("reviews")).content.decode()
        assert "Awaiting a moderators careful eye" in body
        assert pending_review.author.email in body

    def test_bulk_publish_runs_the_engagement_service_and_is_audited(
        self, client, moderator, pending_review
    ):
        response = client.post(
            bo("reviews_bulk"),
            {
                "selected": [pending_review.pk],
                "status": Review.Status.PUBLISHED,
                "reason": "Reads fine.",
            },
        )
        assert response.status_code == 302
        pending_review.refresh_from_db()
        assert pending_review.status == Review.Status.PUBLISHED
        row = AuditEvent.objects.get(action="review.moderate")
        assert pending_review.pk in row.metadata["reviews"]

    def test_an_empty_selection_is_refused(self, client, moderator, pending_review):
        response = client.post(
            bo("reviews_bulk"),
            {"status": Review.Status.PUBLISHED, "reason": "Nothing chosen"},
            follow=True,
        )
        pending_review.refresh_from_db()
        assert pending_review.status == Review.Status.PENDING
        assert error_texts(response)

    def test_a_viewer_cannot_moderate(self, client, pending_review):
        sign_in(client, FINANCE_GROUP, email="finance-moderation@flashwear.test")
        response = client.post(
            bo("reviews_bulk"),
            {
                "selected": [pending_review.pk],
                "status": Review.Status.PUBLISHED,
                "reason": "no",
            },
        )
        assert response.status_code == 404
        pending_review.refresh_from_db()
        assert pending_review.status == Review.Status.PENDING

    @pytest.fixture
    def submitted_item(self, user):
        return LoopItem.objects.create(
            user=user,
            type=LoopItem.Type.RESALE,
            status=LoopItem.Status.SUBMITTED,
            condition=LoopItem.Condition.GOOD,
            title="Resold hoodie",
        )

    def test_a_loop_decision_is_taken_by_the_loop_service_and_audited(
        self, client, moderator, submitted_item
    ):
        response = client.post(bo("loop_action", submitted_item.pk), {"action": "start_review"})
        assert response.status_code == 302
        submitted_item.refresh_from_db()
        assert submitted_item.status == LoopItem.Status.UNDER_REVIEW
        assert submitted_item.reviewed_by == moderator
        assert AuditEvent.objects.filter(action="loop.moderate").exists()

    def test_an_unknown_decision_writes_nothing(self, client, moderator, submitted_item):
        response = client.post(
            bo("loop_action", submitted_item.pk), {"action": "invent"}, follow=True
        )
        assert error_texts(response)
        submitted_item.refresh_from_db()
        assert submitted_item.status == LoopItem.Status.SUBMITTED
        assert not AuditEvent.objects.filter(action="loop.moderate").exists()

    def test_a_viewer_cannot_decide_a_loop_item(self, client, submitted_item):
        sign_in(client, FINANCE_GROUP, email="finance-loop@flashwear.test")
        response = client.post(bo("loop_action", submitted_item.pk), {"action": "start_review"})
        assert response.status_code == 404


class TestSupportQueue:
    @pytest.fixture
    def ticket(self, user):
        return ticket_service.open_ticket(
            customer=user,
            subject="Where is my parcel?",
            description="CONFIDENTIAL-TRANSCRIPT the courier vanished.",
            category=SupportTicket.Category.DELIVERY,
        )

    @pytest.fixture
    def lead(self, client):
        user = staff_user("support-lead@flashwear.test", SUPPORT_MANAGER_GROUP)
        client.force_login(user)
        return user

    def test_the_queue_shows_metadata_but_never_the_transcript(self, client, lead, ticket):
        body = client.get(bo("support")).content.decode()
        assert ticket.number in body
        assert "Where is my parcel?" in body
        assert "CONFIDENTIAL-TRANSCRIPT" not in body

    def test_assignment_is_taken_by_the_desks_service_and_audited(self, client, lead, ticket):
        agent = staff_user("support-agent@flashwear.test", SUPPORT_AGENT_GROUP)
        response = client.post(bo("support_assign", ticket.number), {"agent": agent.pk})
        assert response.status_code == 302
        ticket.refresh_from_db()
        assert ticket.assigned_to == agent
        row = AuditEvent.objects.get(action="support.assign")
        assert row.metadata["to"] == agent.email

    def test_a_non_staff_assignee_is_refused(self, client, lead, ticket, other_user):
        response = client.post(
            bo("support_assign", ticket.number), {"agent": other_user.pk}, follow=True
        )
        ticket.refresh_from_db()
        assert ticket.assigned_to is None
        assert error_texts(response)

    def test_a_legal_status_change_runs_and_is_audited(self, client, lead, ticket):
        response = client.post(
            bo("support_status", ticket.number), {"status": SupportTicket.Status.CLOSED}
        )
        assert response.status_code == 302
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.CLOSED
        assert AuditEvent.objects.filter(action="support.status").exists()

    def test_an_illegal_status_change_is_refused_with_no_audit_row(self, client, lead, ticket):
        ticket.status = SupportTicket.Status.CLOSED
        ticket.save(update_fields=["status", "updated_at"])
        response = client.post(
            bo("support_status", ticket.number),
            {"status": SupportTicket.Status.OPEN},
            follow=True,
        )
        ticket.refresh_from_db()
        assert ticket.status == SupportTicket.Status.CLOSED
        assert error_texts(response)
        assert not AuditEvent.objects.filter(action="support.status").exists()

    def test_a_user_outside_the_desk_cannot_open_the_queue(self, client, ticket):
        sign_in(client, FINANCE_GROUP, email="finance-support@flashwear.test")
        assert client.get(bo("support")).status_code == 404
        assert (
            client.post(
                bo("support_status", ticket.number), {"status": SupportTicket.Status.CLOSED}
            ).status_code
            == 404
        )


class TestPointsLedger:
    @pytest.fixture
    def finance(self, client):
        user = staff_user("finance@flashwear.test", FINANCE_GROUP)
        client.force_login(user)
        return user

    def test_the_page_lists_the_ledger_and_the_form(self, client, finance, user):
        body = client.get(bo("points")).content.decode()
        assert user.email in body

    def test_a_manual_credit_is_a_ledger_row_and_is_audited(self, client, finance, user):
        response = client.post(
            bo("points_adjust"),
            {"user": user.pk, "amount": 100, "reason": "Goodwill for the delay"},
        )
        assert response.status_code == 302
        assert balance_for(user) == 100
        row = AuditEvent.objects.get(action="loyalty.adjust")
        assert row.metadata["amount"] == 100

    def test_a_zero_adjustment_is_refused_by_the_ledger(self, client, finance, user):
        response = client.post(
            bo("points_adjust"),
            {"user": user.pk, "amount": 0, "reason": "No-op attempt"},
            follow=True,
        )
        assert error_texts(response)
        assert balance_for(user) == 0
        assert not AuditEvent.objects.filter(action="loyalty.adjust").exists()

    def test_the_balance_is_the_ledger_not_a_column(self, client, finance, user):
        client.post(bo("points_adjust"), {"user": user.pk, "amount": -250, "reason": "Correction"})
        assert balance_for(user) == -250
        assert AuditEvent.objects.get(action="loyalty.adjust").metadata["amount"] == -250

    def test_a_non_loyalty_operator_cannot_adjust(self, client, user):
        sign_in(client, MODERATOR_GROUP, email="mod-points@flashwear.test")
        response = client.post(bo("points_adjust"), {"user": user.pk, "amount": 10, "reason": "no"})
        assert response.status_code == 404
        assert balance_for(user) == 0


# --------------------------------------------------------------------------------------
# Governance: the audit log and staff roles
# --------------------------------------------------------------------------------------


class TestAuditLog:
    def test_a_manager_reads_the_trail_and_a_viewer_gets_a_404(self, client, admin_user):
        audit_service.record(
            actor=admin_user,
            domain="orders",
            action="order.status",
            object_type="orders.order",
            object_id="FW-2026-000001",
            object_repr="FW-2026-000001",
        )
        client.force_login(admin_user)
        body = client.get(bo("audit")).content.decode()
        assert "order.status" in body

        client.logout()
        sign_in(client, OPERATOR_GROUP, email="viewer-audit@flashwear.test")
        assert client.get(bo("audit")).status_code == 404

    def test_an_existing_row_cannot_be_rewritten(self, admin_user):
        event = AuditEvent.objects.create(
            actor=admin_user,
            actor_label=admin_user.email,
            domain=AuditEvent.Domain.ORDERS,
            action="order.status",
            object_type="orders.order",
        )
        event.reason = "edited after the fact"
        with pytest.raises(ValidationError):
            event.save()

    def test_an_existing_row_cannot_be_deleted(self, admin_user):
        event = AuditEvent.objects.create(
            actor=admin_user,
            actor_label=admin_user.email,
            domain=AuditEvent.Domain.ORDERS,
            action="order.status",
            object_type="orders.order",
        )
        with pytest.raises(ValidationError):
            event.delete()
        assert AuditEvent.objects.filter(pk=event.pk).exists()

    def test_the_actor_label_is_a_snapshot_of_the_email_at_the_time(self, admin_user):
        actor = staff_user("before@flashwear.test", ORDERS_GROUP)
        audit_service.record(
            actor=actor,
            domain="orders",
            action="order.note",
            object_type="orders.order",
            object_id="FW-2026-000002",
            object_repr="FW-2026-000002",
        )
        actor.email = "after@flashwear.test"
        actor.save(update_fields=["email"])

        event = AuditEvent.objects.get(action="order.note")
        assert event.actor_label == "before@flashwear.test"
        assert event.actor.email == "after@flashwear.test"

    def test_the_admin_registration_is_read_only(self):
        model_admin = django_admin.site._registry[AuditEvent]
        request = RequestFactory().get("/")
        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False


class TestStaffRoles:
    @pytest.fixture
    def root(self, client, admin_user):
        client.force_login(admin_user)
        return admin_user

    @pytest.fixture
    def target(self):
        return staff_user("new-operator@flashwear.test")

    def test_the_page_lists_staff_accounts_only(self, client, root, user, target):
        body = client.get(bo("staff")).content.decode()
        assert target.email in body
        assert user.email not in body  # customers are never staff

    def test_granting_a_group_is_audited(self, client, root, target):
        response = client.post(bo("staff_update", target.pk), {"add": [ORDERS_GROUP]})
        assert response.status_code == 302
        assert target.groups.filter(name=ORDERS_GROUP).exists()
        row = AuditEvent.objects.get(action="staff.role")
        assert row.metadata["added"] == [ORDERS_GROUP]

    def test_revoking_a_group_is_audited_too(self, client, root, target):
        target.groups.add(Group.objects.get(name=ORDERS_GROUP))
        client.post(bo("staff_update", target.pk), {"remove": [ORDERS_GROUP]})
        assert not target.groups.filter(name=ORDERS_GROUP).exists()
        row = AuditEvent.objects.get(action="staff.role")
        assert row.metadata["removed"] == [ORDERS_GROUP]

    def test_a_group_the_app_does_not_own_is_refused(self, client, root, target):
        response = client.post(
            bo("staff_update", target.pk),
            {"add": ["Super Users"]},
            follow=True,
        )
        assert error_texts(response)
        assert not target.groups.exists()
        assert not AuditEvent.objects.filter(action="staff.role").exists()

    def test_a_non_staff_account_cannot_be_targeted(self, client, root, user):
        assert client.post(bo("staff_update", user.pk), {"add": [ORDERS_GROUP]}).status_code == 404

    def test_only_an_administrator_may_change_roles(self, client, target):
        sign_in(client, ORDERS_GROUP, email="sales-roles@flashwear.test")
        assert (
            client.post(bo("staff_update", target.pk), {"add": [ORDERS_GROUP]}).status_code == 404
        )
        assert not target.groups.exists()


# --------------------------------------------------------------------------------------
# The JSON API
# --------------------------------------------------------------------------------------


class TestBackOfficeApi:
    def test_anonymous_is_refused(self, api_client):
        for path in ("/api/v1/backoffice/", "/api/v1/backoffice/dashboard/"):
            assert api_client.get(path).status_code == 403

    def test_the_root_advertises_the_read_endpoints(self, api_client, admin_user):
        api_client.force_login(admin_user)
        response = api_client.get("/api/v1/backoffice/")
        assert response.status_code == 200
        assert set(response.data) == {"dashboard", "alerts", "orders", "audit"}

    def test_the_index_lists_the_namespace(self, api_client, admin_user):
        api_client.force_login(admin_user)
        endpoints = api_client.get("/api/v1/").data["endpoints"]
        assert endpoints["backoffice"].endswith("/api/v1/backoffice/")

    def test_the_dashboard_carries_the_same_numbers(self, api_client, admin_user, user, product):
        place_and_pay(user, product.variants.first())
        api_client.force_login(admin_user)
        response = api_client.get("/api/v1/backoffice/dashboard/")
        assert response.status_code == 200
        assert response.data["sales"]["placed"] == 1

    def test_the_dashboard_range_is_allowlisted(self, api_client, admin_user):
        api_client.force_login(admin_user)
        assert (
            api_client.get("/api/v1/backoffice/dashboard/", {"range": "today"}).status_code == 200
        )
        # Unknown keys fall back to the default window rather than 500.
        assert (
            api_client.get("/api/v1/backoffice/dashboard/", {"range": "nonsense"}).status_code
            == 200
        )

    def test_the_orders_endpoint_ships_six_fields_and_no_more(
        self, api_client, admin_user, user, product
    ):
        order = place_and_pay(user, product.variants.first())
        api_client.force_login(admin_user)
        response = api_client.get("/api/v1/backoffice/orders/")
        assert response.status_code == 200
        row = response.data["results"][0]
        assert set(row) == {"number", "status", "total", "currency", "created_at", "customer"}
        assert row["number"] == order.number
        assert row["customer"] == user.email

    def test_the_orders_endpoint_filters_by_status(self, api_client, admin_user, user, product):
        place_and_pay(user, product.variants.first())
        api_client.force_login(admin_user)
        paid = api_client.get("/api/v1/backoffice/orders/", {"status": "paid"}).data
        cancelled = api_client.get("/api/v1/backoffice/orders/", {"status": "cancelled"}).data
        assert paid["count"] == 1
        assert cancelled["count"] == 0

    def test_the_audit_endpoint_returns_the_trail(self, api_client, admin_user):
        audit_service.record(
            actor=admin_user,
            domain="orders",
            action="order.status",
            object_type="orders.order",
            object_id="FW-2026-000003",
            object_repr="FW-2026-000003",
        )
        api_client.force_login(admin_user)
        response = api_client.get("/api/v1/backoffice/audit/")
        assert response.status_code == 200
        row = response.data["results"][0]
        assert row["action"] == "order.status"
        assert row["actor"] == admin_user.email

    def test_a_missing_capability_is_404_not_403(self, api_client):
        # An Inventory Manager may read the shop but not the order queue...
        stock = staff_user("api-stock@flashwear.test", INVENTORY_GROUP)
        api_client.force_login(stock)
        assert api_client.get("/api/v1/backoffice/orders/").status_code == 404
        assert api_client.get("/api/v1/backoffice/dashboard/").status_code == 200

        # ...and the operator group may read orders but not the manager-only audit log.
        viewer = staff_user("api-operator@flashwear.test", OPERATOR_GROUP)
        api_client.force_login(viewer)
        assert api_client.get("/api/v1/backoffice/orders/").status_code == 200
        assert api_client.get("/api/v1/backoffice/audit/").status_code == 404

    def test_the_staff_flag_alone_holds_nothing(self, api_client, user):
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        api_client.force_login(user)
        assert api_client.get("/api/v1/backoffice/dashboard/").status_code == 404

    def test_a_customer_cannot_reach_the_api(self, api_client, user):
        api_client.force_login(user)
        assert api_client.get("/api/v1/backoffice/").status_code == 404

    def test_a_superuser_reads_every_endpoint(self, api_client, admin_user):
        api_client.force_login(admin_user)
        for path in (
            "/api/v1/backoffice/",
            "/api/v1/backoffice/dashboard/",
            "/api/v1/backoffice/alerts/",
            "/api/v1/backoffice/orders/",
            "/api/v1/backoffice/audit/",
        ):
            assert api_client.get(path).status_code == 200

    def test_alerts_are_capability_filtered_on_the_api_too(self, api_client, product):
        seed_stock(product.variants.first(), 0)

        api_client.force_login(staff_user("api-admin@flashwear.test", ADMIN_GROUP))
        keys = {row["key"] for row in api_client.get("/api/v1/backoffice/alerts/").data}
        assert "out_of_stock" in keys

        api_client.force_login(staff_user("api-sales@flashwear.test", ORDERS_GROUP))
        keys = {row["key"] for row in api_client.get("/api/v1/backoffice/alerts/").data}
        assert "out_of_stock" not in keys

    def test_every_alert_link_points_at_an_existing_screen(self, api_client, product):
        seed_stock(product.variants.first(), 0)
        api_client.force_login(staff_user("api-links@flashwear.test", ADMIN_GROUP))
        screens = {
            reverse(name)
            for name in (
                "backoffice:inventory",
                "backoffice:payments",
                "backoffice:orders",
                "backoffice:support",
                "backoffice:reviews",
                "backoffice:drops",
                "backoffice:promotions",
            )
        }
        for row in api_client.get("/api/v1/backoffice/alerts/").data:
            assert row["label"] in {"Critical", "Warning", "Info"}
            assert any(row["url"].endswith(path) for path in screens)

    def test_the_capability_table_only_names_real_capabilities(self):
        from apps.backoffice.api import CAPABILITY_BY_ENDPOINT
        from apps.backoffice.permissions import CAPABILITY_GROUPS

        assert set(CAPABILITY_BY_ENDPOINT.values()) <= set(CAPABILITY_GROUPS)
