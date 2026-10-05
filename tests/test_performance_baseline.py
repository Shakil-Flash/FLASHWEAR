"""Phase 18: performance baseline -- query budgets for representative pages.

Measured 2026-10-05 (SQLite test database, small fixtures): product list 11 queries,
category list 1, notification center 10, notifications API 5, back-office delivery
inspection 13 (dashboard/orders-queue budgets live in Phase 16's suite). Budgets are
the measured count plus headroom, so an N+1 or a dropped ``select_related`` fails
loudly while harmless single-query drift does not.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.backoffice.permissions import ORDERS_GROUP

pytestmark = pytest.mark.django_db


def test_the_product_list_stays_within_its_query_budget(client, django_assert_max_num_queries):
    with django_assert_max_num_queries(15):
        assert client.get(reverse("catalog:product-list")).status_code == 200


def test_the_category_list_is_a_single_digit_query_page(client, django_assert_max_num_queries):
    with django_assert_max_num_queries(4):
        assert client.get(reverse("catalog:category-list")).status_code == 200


def test_the_notification_center_stays_within_its_query_budget(
    client, user, django_assert_max_num_queries
):
    client.force_login(user)
    with django_assert_max_num_queries(14):
        assert client.get(reverse("notifications:center")).status_code == 200


def test_the_notification_api_stays_within_its_query_budget(
    client, user, django_assert_max_num_queries
):
    client.force_login(user)
    with django_assert_max_num_queries(8):
        assert client.get("/api/v1/notifications/").status_code == 200


def test_the_delivery_inspection_screen_stays_within_its_query_budget(
    client, django_assert_max_num_queries
):
    operator = get_user_model().objects.create_user(
        email="perf-baseline@flashwear.test", password="Str0ng-Passw0rd!"
    )
    operator.is_staff = True
    operator.save(update_fields=["is_staff"])
    operator.groups.add(Group.objects.get(name=ORDERS_GROUP))
    client.force_login(operator)
    with django_assert_max_num_queries(17):
        assert client.get(reverse("backoffice:notifications")).status_code == 200
