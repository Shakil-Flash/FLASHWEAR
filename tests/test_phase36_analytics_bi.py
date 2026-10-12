"""Phase 36: Analytics, Business Intelligence & Merchandising Insights tests.

Covers:
- Metric calculations: gross sales, net sales, completed orders, AOV
- Order/payment status handling: pending/failed payments excluded from revenue
- Refunds and cancellation deductions
- Date boundaries and timezone behavior (presets and custom date ranges)
- Empty datasets: graceful 0.0 rates without division errors
- Funnel progression and step-by-step conversion rates
- Merchandising insights: best sellers, zero sales, high views / low sales, weak searches
- Access control: staff capabilities (ANALYTICS_VIEW) and customer/unauthorized 404s
- CSV export: format, headers, permission gating, no customer PII
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.analytics.services import record_event
from apps.backoffice.permissions import (
    MODERATOR_GROUP,
    OPERATOR_GROUP,
    SUPPORT_AGENT_GROUP,
)
from apps.backoffice.selectors.analytics import (
    analytics_dashboard_data,
    customer_and_support_summary,
    funnel_summary,
    merchandising_insights,
    sales_trends,
)
from apps.backoffice.selectors.common import DateRange, resolve_range
from apps.backoffice.selectors.dashboard import dashboard_summary
from apps.backoffice.views.base import range_from_request
from apps.orders.models import Order
from apps.payments.services import process_refund
from apps.support.models import SupportTicket
from apps.support.services import tickets as ticket_service
from tests.phase6_helpers import place_and_pay, placed_order

pytestmark = pytest.mark.django_db


def _staff(client, group_name: str, email: str):
    """An active staff account in ``group_name``."""
    staff = get_user_model().objects.create_user(email=email, password="Str0ng-Passw0rd!")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    staff.groups.add(Group.objects.get(name=group_name))
    client.force_login(staff)
    return staff


# =============================================================================
# 1. Date Range & Timezone Boundaries
# =============================================================================


class TestDateBoundaries:
    def test_preset_ranges_resolve_correctly(self):
        today_range = resolve_range("today")
        assert today_range.key == "today"
        assert today_range.start is not None
        assert today_range.end is not None
        assert today_range.start < today_range.end

        seven_d = resolve_range("7d")
        assert seven_d.key == "7d"
        assert (seven_d.end - seven_d.start).days == 7

        all_time = resolve_range("all")
        assert all_time.is_all_time is True
        assert all_time.start is None
        assert all_time.end is None

    def test_custom_date_range_valid(self):
        cr = resolve_range("custom", start_date="2026-10-01", end_date="2026-10-10")
        assert cr.key == "custom"
        assert cr.label == "2026-10-01 to 2026-10-10"
        assert cr.start.date() == date(2026, 10, 1)
        # End is exclusive (midnight next day)
        assert cr.end.date() == date(2026, 10, 11)

    def test_custom_date_range_out_of_order_is_swapped(self):
        cr = resolve_range("custom", start_date="2026-10-15", end_date="2026-10-05")
        assert cr.key == "custom"
        assert cr.start.date() == date(2026, 10, 5)
        assert cr.end.date() == date(2026, 10, 16)

    def test_custom_date_range_invalid_falls_back_to_7d(self):
        cr = resolve_range("custom", start_date="not-a-date", end_date="2026-10-10")
        assert cr.key == "7d"

    def test_range_from_request_parses_get_parameters(self):
        rf = RequestFactory()
        req1 = rf.get("/operations/analytics/?range=today")
        assert range_from_request(req1).key == "today"

        req2 = rf.get("/operations/analytics/?from=2026-10-01&to=2026-10-05")
        cr = range_from_request(req2)
        assert cr.key == "custom"
        assert cr.start.date() == date(2026, 10, 1)


# =============================================================================
# 2. Sales, Refunds, Cancellations & Order Status Semantics
# =============================================================================


class TestSalesAndOrderMetrics:
    def test_gross_and_net_sales_with_completed_orders(self, user, product):
        order1 = place_and_pay(user, product.variants.first())
        order2 = place_and_pay(user, product.variants.first())

        summary = funnel_summary(date_range=resolve_range("all"))
        assert summary["orders_completed"] == 2
        assert summary["gross_revenue"] == order1.total + order2.total
        assert summary["net_revenue"] == summary["gross_revenue"]
        assert summary["aov"] == summary["gross_revenue"] / 2

    def test_pending_and_failed_orders_are_excluded_from_completed_sales(self, user, product):
        placed_order(user, product.variants.first())  # PENDING_PAYMENT

        summary = funnel_summary(date_range=resolve_range("all"))
        assert summary["orders_completed"] == 0
        assert summary["gross_revenue"] == Decimal("0.00")
        assert summary["net_revenue"] == Decimal("0.00")
        assert summary["aov"] is None
        # Placed order is counted in funnel stage 4
        assert summary["orders_placed"] == 1

    def test_refunds_deducted_from_gross_to_form_net_sales(self, user, product):
        order = place_and_pay(user, product.variants.first())
        refund_amount = Decimal("15.00")
        process_refund(order, amount=refund_amount)

        summary = funnel_summary(date_range=resolve_range("all"))
        assert summary["gross_revenue"] == order.total
        assert summary["refunds_amount"] == refund_amount
        assert summary["refunds_count"] == 1
        assert summary["net_revenue"] == order.total - refund_amount

    def test_cancellations_tracked_separately(self, user, product):
        order = placed_order(user, product.variants.first())
        order.transition_to(Order.Status.CANCELLED)

        cs = customer_and_support_summary(date_range=resolve_range("all"))
        assert cs["cancellations_count"] == 1
        assert cs["cancellations_total"] == order.total

    def test_empty_dataset_returns_clean_zeroes_without_division_errors(self):
        stale = DateRange(
            "empty",
            "empty",
            timezone.now() - timedelta(days=500),
            timezone.now() - timedelta(days=490),
        )
        summary = funnel_summary(date_range=stale)
        assert summary["visitors"] == 0
        assert summary["orders_completed"] == 0
        assert summary["conversion_rate"] == 0.0
        assert summary["aov"] is None
        assert summary["gross_revenue"] == Decimal("0.00")
        assert summary["net_revenue"] == Decimal("0.00")


# =============================================================================
# 3. Customer Retention: New vs Returning Customers
# =============================================================================


class TestCustomerInsights:
    def test_new_versus_returning_customers(self, user, other_user, product):
        # Order 1 placed by user in window 1
        order1 = place_and_pay(user, product.variants.first())
        past_time = timezone.now() - timedelta(days=15)
        Order.objects.filter(pk=order1.pk).update(paid_at=past_time, created_at=past_time)

        # Window 2: last 7 days
        # user orders again (returning)
        place_and_pay(user, product.variants.first())
        # other_user orders for the first time (new)
        place_and_pay(other_user, product.variants.first())

        seven_d = resolve_range("7d")
        cs = customer_and_support_summary(date_range=seven_d)
        assert cs["new_buyers"] == 1
        assert cs["returning_buyers"] == 1


# =============================================================================
# 4. Shopping Funnel Progression & Step Conversions
# =============================================================================


class TestShoppingFunnel:
    def test_funnel_stages_and_step_conversion_rates(self, user, product):
        record_event("product_view", user=user, object_type="product", object_id=product.pk)
        record_event("cart_add", user=user, object_type="variant", object_id=1)
        record_event("checkout_started", user=user, idempotency_key="checkout:1")
        place_and_pay(user, product.variants.first())

        summary = funnel_summary(date_range=resolve_range("all"))
        assert summary["visitors"] == 1
        assert summary["product_views"] == 1
        assert summary["cart_adds"] == 1
        assert summary["checkout_started"] == 1
        assert summary["orders_placed"] == 1
        assert summary["orders_completed"] == 1

        steps = summary["step_conversion"]
        assert steps["view_to_cart"] == 100.0
        assert steps["cart_to_checkout"] == 100.0
        assert steps["checkout_to_order"] == 100.0
        assert steps["order_to_paid"] == 100.0
        assert summary["conversion_rate"] == 100.0


# =============================================================================
# 5. Product & Merchandising Insights
# =============================================================================


class TestMerchandisingInsights:
    def test_best_sellers_and_zero_sales(self, user, product, make_product, make_variant):
        # product sold 2 units
        place_and_pay(user, product.variants.first(), quantity=2)

        # unsold product
        unsold = make_product(name="Unsold Jacket", slug="unsold-jacket")
        make_variant(unsold)

        merch = merchandising_insights(date_range=resolve_range("all"))
        assert len(merch["best_sellers"]) >= 1
        assert merch["best_sellers"][0]["product_id"] == product.pk
        assert merch["best_sellers"][0]["units_sold"] == 2

        assert merch["zero_sales_count"] >= 1
        zero_pids = [p["id"] for p in merch["zero_sales_sample"]]
        assert unsold.pk in zero_pids
        assert product.pk not in zero_pids

    def test_high_views_with_low_purchases(self, user, product):
        # 5 views recorded on product, but 0 sales
        for _ in range(5):
            record_event("product_view", user=user, object_type="product", object_id=product.pk)

        merch = merchandising_insights(date_range=resolve_range("all"))
        assert len(merch["high_view_low_purchase"]) >= 1
        item = merch["high_view_low_purchase"][0]
        assert item["product_id"] == product.pk
        assert item["views"] == 5
        assert item["units_sold"] == 0

    def test_weak_and_zero_result_searches(self, user):
        record_event("product_search", user=user, metadata={"query": "hoodie", "result_count": 5})
        record_event(
            "product_search", user=user, metadata={"query": "neon trackpants", "result_count": 0}
        )

        merch = merchandising_insights(date_range=resolve_range("all"))
        top_queries = [s["metadata__query"] for s in merch["top_searches"]]
        weak_queries = [s["metadata__query"] for s in merch["weak_searches"]]

        assert "hoodie" in top_queries
        assert "neon trackpants" in top_queries
        assert "neon trackpants" in weak_queries
        assert "hoodie" not in weak_queries

    def test_wishlist_and_recommendation_insights(self, user, product):
        record_event("wishlist_add", user=user, object_type="product", object_id=product.pk)
        record_event("recommendation_view", user=user)
        record_event("recommendation_click", user=user)

        merch = merchandising_insights(date_range=resolve_range("all"))
        assert len(merch["top_wishlisted"]) >= 1
        assert merch["top_wishlisted"][0]["product_id"] == product.pk
        assert merch["recommendations"]["views"] == 1
        assert merch["recommendations"]["clicks"] == 1
        assert merch["recommendations"]["ctr"] == 100.0


# =============================================================================
# 6. Support Tickets & Sales Trends
# =============================================================================


class TestSupportAndTrends:
    def test_support_ticket_metrics_and_resolution_time(self, user):
        ticket = ticket_service.open_ticket(
            customer=user,
            subject="Damaged zipper",
            description="Zipper broke on first wear",
            category=SupportTicket.Category.PRODUCT,
        )
        now = timezone.now()
        SupportTicket.objects.filter(pk=ticket.pk).update(
            status=SupportTicket.Status.RESOLVED,
            resolved_at=now,
        )

        cs = customer_and_support_summary(date_range=resolve_range("all"))
        assert cs["tickets_opened"] == 1
        assert cs["tickets_resolved"] == 1
        assert cs["avg_resolution_hours"] is not None

    def test_sales_trends_series_for_charting(self, user, product):
        place_and_pay(user, product.variants.first())

        trends = sales_trends(date_range=resolve_range("all"))
        assert len(trends["series"]) >= 1
        assert trends["max_orders"] >= 1
        assert trends["max_revenue"] > Decimal("0.00")
        assert "height_pct" in trends["series"][0]


# =============================================================================
# 7. Dashboard Selector Integration
# =============================================================================


class TestDashboardSummaryIntegration:
    def test_dashboard_summary_contains_enriched_metrics(self, user, product):
        order = place_and_pay(user, product.variants.first())
        process_refund(order, amount=Decimal("10.00"))

        summary = dashboard_summary(date_range=resolve_range("all"))
        assert "net" in summary["sales"]
        assert summary["sales"]["gross"] == order.total
        assert summary["sales"]["refunds_count"] == 1
        assert summary["sales"]["refunds_total"] == Decimal("10.00")
        assert summary["sales"]["net"] == order.total - Decimal("10.00")

        # Full BI data includes merchandising and customer/support breakdowns
        bi_data = analytics_dashboard_data(date_range=resolve_range("all"))
        assert bi_data["customers_and_support"]["new_buyers"] == 1
        assert "best_sellers" in bi_data["merchandising"]
        assert "zero_sales_count" in bi_data["merchandising"]


# =============================================================================
# 8. Access Control & Staff Permissions
# =============================================================================


class TestAnalyticsAccessControl:
    def test_anonymous_user_is_redirected_to_login(self, client):
        response = client.get(reverse("backoffice:analytics"))
        assert response.status_code == 302
        assert "login" in response.url

    def test_regular_customer_gets_404(self, client, user):
        client.force_login(user)
        response = client.get(reverse("backoffice:analytics"))
        assert response.status_code == 404

    def test_moderator_without_analytics_view_gets_404(self, client):
        _staff(client, MODERATOR_GROUP, "mod@flashwear.test")
        response = client.get(reverse("backoffice:analytics"))
        assert response.status_code == 404

    def test_operator_with_analytics_view_gets_200(self, client):
        _staff(client, OPERATOR_GROUP, "operator@flashwear.test")
        response = client.get(reverse("backoffice:analytics"))
        assert response.status_code == 200
        assert "Analytics" in response.content.decode()

    def test_custom_range_filters_apply_on_view(self, client):
        _staff(client, OPERATOR_GROUP, "operator-range@flashwear.test")
        response = client.get(reverse("backoffice:analytics") + "?from=2026-10-01&to=2026-10-07")
        assert response.status_code == 200
        html = response.content.decode()
        assert "2026-10-01 to 2026-10-07" in html


# =============================================================================
# 9. CSV Export & Privacy
# =============================================================================


class TestAnalyticsCSVExport:
    def test_export_summary_csv(self, client, user, product):
        _staff(client, OPERATOR_GROUP, "op-export@flashwear.test")
        place_and_pay(user, product.variants.first())

        response = client.get(reverse("backoffice:analytics_export") + "?type=summary")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")
        assert 'filename="flashwear-analytics-summary' in response["Content-Disposition"]

        content = response.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        assert reader[0] == ["Metric", "Value"]
        # Ensure no customer PII leaked
        assert user.email not in content
        assert "Gross Revenue (GBP)" in content
        assert "Completed Orders" in content

    def test_export_products_csv(self, client, user, product):
        _staff(client, OPERATOR_GROUP, "op-export-prod@flashwear.test")
        place_and_pay(user, product.variants.first(), quantity=3)

        response = client.get(reverse("backoffice:analytics_export") + "?type=products")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        assert reader[0] == ["Rank", "Product ID", "Product Name", "Units Sold", "Revenue (GBP)"]
        assert len(reader) >= 2
        assert product.name in content

    def test_export_funnel_csv(self, client):
        _staff(client, OPERATOR_GROUP, "op-export-funnel@flashwear.test")
        response = client.get(reverse("backoffice:analytics_export") + "?type=funnel")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        assert reader[0] == ["Stage", "Count", "Step Conversion Rate (%)"]
        assert "Visitors" in content
        assert "Product Views" in content

    def test_export_is_permission_gated(self, client, user):
        # Customer gets 404
        client.force_login(user)
        assert client.get(reverse("backoffice:analytics_export")).status_code == 404

        # Staff without capability gets 404
        _staff(client, SUPPORT_AGENT_GROUP, "agent@flashwear.test")
        assert client.get(reverse("backoffice:analytics_export")).status_code == 404
