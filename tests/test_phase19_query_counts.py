"""Phase 19: query-count regression tests for the hot pages.

Measured 2026-10-05 on the SQLite test database (rich fixture data: 12 products, a filled
bag, five orders): home 5, product list 6, search 6, product detail 13, cart 10, checkout 12,
back-office dashboard 38, back-office orders 9. Budgets are the measured count plus a small
headroom, so reintroducing a per-row query (the facet aggregate storm, a second cart lookup,
one group-membership query per capability check) fails loudly while harmless single-query
drift does not.

Three non-page tests pin the *mechanisms* the savings rest on: the back-office group-name
cache, the request-level cart memo, and the single-aggregate status counts.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.backoffice.permissions import ORDERS_GROUP, group_names_for
from apps.notifications import selectors as notification_selectors
from apps.shop.models import Cart
from apps.shop.services import get_cart_for_request, get_or_create_cart

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Page budgets
# ---------------------------------------------------------------------------


def test_the_homepage_stays_within_its_query_budget(client, django_assert_max_num_queries):
    # Phase 29: Homepage resolves Content Studio managed sections alongside default rails.
    with django_assert_max_num_queries(10):
        assert client.get("/").status_code == 200


def test_the_product_list_stays_within_its_phase19_budget(client, django_assert_max_num_queries):
    # Phase 19 removed a dead facet-aggregate storm (was 19 queries on rich data).
    with django_assert_max_num_queries(9):
        assert client.get(reverse("catalog:product-list")).status_code == 200


def test_search_stays_within_its_phase19_budget(client, django_assert_max_num_queries):
    with django_assert_max_num_queries(9):
        assert client.get(reverse("catalog:product-search"), {"q": "tee"}).status_code == 200


def test_the_product_detail_stays_within_its_phase19_budget(
    client, product, django_assert_max_num_queries
):
    # 13 measured: gallery + related strip prefetches, review aggregate, breadcrumbs.
    with django_assert_max_num_queries(17):
        assert client.get(product.get_absolute_url()).status_code == 200


def test_the_cart_page_shares_one_item_fetch_between_all_its_consumers(
    client, user, product, django_assert_max_num_queries
):
    from decimal import Decimal

    from apps.shop.models import CartItem

    client.force_login(user)
    cart = Cart.objects.create(user=user)
    CartItem.objects.create(
        cart=cart,
        variant=product.variants.first(),
        quantity=2,
        price_snapshot=Decimal("49.00"),
    )
    # 10 measured with data: totals, validation and the price diff all reuse the one list.
    # Phase 30: +3 for cart_view event on fresh visitor, +1 for checkout session discounts.
    with django_assert_max_num_queries(18):
        response = client.get(reverse("shop:cart"))
    assert response.status_code == 200
    assert response.context["totals"]["item_count"] == 1


def test_the_checkout_page_stays_within_its_phase19_budget(
    client, user, product, django_assert_max_num_queries
):
    from decimal import Decimal

    from apps.shop.models import CartItem

    client.force_login(user)
    cart = Cart.objects.create(user=user)
    CartItem.objects.create(
        cart=cart,
        variant=product.variants.first(),
        quantity=1,
        price_snapshot=Decimal("49.00"),
    )
    # 12 measured in Phase 19: one bag fetch, one loyalty balance, one badge count.
    # Phase 20 adds +2 (first-touch attribution read + insert, with the user link folded
    # into it) and +4 (the idempotent checkout_started event: select, savepoint, insert,
    # release) -> 20 measured on a fresh visitor.
    with django_assert_max_num_queries(20):
        assert client.get(reverse("shop:checkout")).status_code == 200


def test_the_notification_center_stays_within_its_phase19_budget(
    client, user, django_assert_max_num_queries
):
    client.force_login(user)
    # 9 measured: status counts are one aggregate shared with the bell badge.
    with django_assert_max_num_queries(12):
        assert client.get(reverse("notifications:center")).status_code == 200


def _login_operator(client):
    operator = get_user_model().objects.create_user(
        email="phase19-budget@flashwear.test", password="Str0ng-Passw0rd!"
    )
    operator.is_staff = True
    operator.save(update_fields=["is_staff"])
    operator.groups.add(Group.objects.get(name=ORDERS_GROUP))
    client.force_login(operator)
    return operator


def test_the_backoffice_dashboard_stays_within_its_phase19_budget(
    client, django_assert_max_num_queries
):
    _login_operator(client)
    # 51 measured: operations desk overview includes analytics event counters,
    # payment health, ticket triage and inventory warnings.
    with django_assert_max_num_queries(55):
        assert client.get(reverse("backoffice:dashboard")).status_code == 200


def test_the_backoffice_orders_queue_stays_within_its_phase19_budget(
    client, django_assert_max_num_queries
):
    _login_operator(client)
    # 9 measured (was 16): no cart/wishlist/unread badge queries on back-office pages.
    with django_assert_max_num_queries(13):
        assert client.get(reverse("backoffice:orders")).status_code == 200


# ---------------------------------------------------------------------------
# Mechanisms the savings rest on
# ---------------------------------------------------------------------------


def test_group_names_are_cached_on_the_user_instance(user, django_assert_num_queries):
    user.groups.add(Group.objects.get(name=ORDERS_GROUP))
    assert group_names_for(user) == frozenset({ORDERS_GROUP})
    # The second call -- the one every capability check in a render performs -- is free.
    with django_assert_num_queries(0):
        assert group_names_for(user) == frozenset({ORDERS_GROUP})


def test_the_cart_is_fetched_once_per_request(user, django_assert_num_queries):
    class FakeRequest:
        pass

    request = FakeRequest()
    request.user = user
    request.session = type("S", (), {"session_key": None})()

    with django_assert_num_queries(1):
        first = get_cart_for_request(request)
        second = get_cart_for_request(request)
    assert first == second


def test_get_or_create_cart_reuses_the_request_memo(user, django_assert_num_queries):
    class FakeRequest:
        pass

    request = FakeRequest()
    request.user = user
    request.session = type("S", (), {"session_key": None})()

    with django_assert_num_queries(4):  # lookup, insert (+ the two savepoint bookkeeping
        # statements ``get_or_create`` needs inside a TestCase transaction)
        cart = get_or_create_cart(request)
    with django_assert_num_queries(0):
        again = get_or_create_cart(request)
    assert cart.pk == again.pk


def test_status_counts_is_a_single_aggregate(user, django_assert_num_queries):
    from apps.notifications.models import Channel, Notification, NotificationCategory

    Notification.objects.create(
        user=user,
        channel=Channel.IN_APP,
        notification_type="order_shipped",
        category=NotificationCategory.ORDERS,
        title="Shipped",
    )
    with django_assert_num_queries(1):
        counts = notification_selectors.status_counts(user)
    assert counts == {"unread": 1, "total": 1}
