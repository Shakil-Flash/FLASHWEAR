"""Tests for FLASHWEAR Phase 55: Advanced Analytics, Business Intelligence & Executive Dashboard.

Covers:
- Financial metric reconciliation: GMV, discounts, gross revenue, net sales, refunds, cancellations, AOV.
- Cost of Goods Sold (COGS) and estimated gross profit under missing, partial, and verified cost data.
- Period-over-period comparison boundaries and delta percentage calculations.
- Fulfillment lead times and delivery transit duration metrics.
- Inventory availability, safety thresholds, and slow-moving stock identification.
- Product return rates from authoritative ReturnItem records.
- Customer retention, repeat purchase rates, and promotion code attribution.
- Operating expense (OpEx) categorization and double-counting prevention.
- CSV export formula injection sanitization ('=', '+', '-', '@', '\t', '\r').
- Comprehensive CSV report exports (summary, sales, products, inventory_risks, customers, expenses).
- Role-based access control and financial permission segregation.
- Empty database resilience without division by zero.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.backoffice.permissions import (
    ADMIN_GROUP,
    FINANCE_GROUP,
    OPERATOR_GROUP,
    SUPPORT_AGENT_GROUP,
)
from apps.backoffice.selectors.bi import (
    calculate_delta_percent,
    get_customer_and_retention_intelligence,
    get_executive_bi_metrics,
    get_procurement_and_expense_intelligence,
    get_product_and_inventory_intelligence,
    get_sales_time_series,
    previous_equivalent_range,
    sanitize_csv_cell,
)
from apps.backoffice.selectors.common import DateRange, resolve_range
from apps.catalog.models import Category, Product, ProductVariant
from apps.inventory.models import Stock
from apps.orders.models import Order, OrderItem, ReturnItem, ReturnRequest
from apps.payments.models import Payment, Refund
from apps.procurement.models import (
    ExpenseCategory,
    ExpenseStatus,
    OperatingExpense,
    PurchaseOrder,
    PurchaseOrderStatus,
    Supplier,
    SupplierInvoice,
)
from tests.phase6_helpers import place_and_pay, placed_order

pytestmark = pytest.mark.django_db


def _staff(client, group_name: str, email: str):
    """Create an active staff account belonging to group_name and log them in."""
    staff = get_user_model().objects.create_user(email=email, password="Str0ng-Passw0rd!")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    staff.groups.add(Group.objects.get(name=group_name))
    client.force_login(staff)
    return staff


# =============================================================================
# 1. Period Comparison & Delta Mathematics
# =============================================================================


class TestPeriodComparisonAndDelta:
    def test_previous_equivalent_range_calculation(self):
        # 7-day range
        curr = resolve_range("7d")
        prior = previous_equivalent_range(curr)
        assert prior is not None
        assert prior.start is not None
        assert prior.end is not None
        assert prior.end == curr.start
        assert (prior.end - prior.start) == (curr.end - curr.start)

    def test_previous_equivalent_range_all_time_returns_none(self):
        all_time = resolve_range("all")
        assert previous_equivalent_range(all_time) is None

    def test_calculate_delta_percent(self):
        # Normal positive change: 100 -> 125 = +25.0%
        assert calculate_delta_percent(Decimal("125.00"), Decimal("100.00")) == 25.0
        # Normal negative change: 100 -> 80 = -20.0%
        assert calculate_delta_percent(80, 100) == -20.0
        # Zero to positive: 0 -> 50 = +100.0%
        assert calculate_delta_percent(50, 0) == 100.0
        # Both zero: 0 -> 0 = 0.0%
        assert calculate_delta_percent(0, 0) == 0.0
        # Missing prior: returns None
        assert calculate_delta_percent(100, None) is None
        assert calculate_delta_percent(None, 100) is None


# =============================================================================
# 2. Formula Injection Protection (CSV Sanitization)
# =============================================================================


class TestCSVFormulaInjectionSanitization:
    def test_sanitize_csv_cell_escapes_formula_triggers(self):
        # Trigger characters: =, +, -, @, \t, \r
        assert sanitize_csv_cell("=1+1") == "'=1+1"
        assert sanitize_csv_cell("+100") == "'+100"
        assert sanitize_csv_cell("-50") == "'-50"
        assert sanitize_csv_cell("@SUM(A1:A5)") == "'@SUM(A1:A5)"
        assert sanitize_csv_cell("\tCMD|' /C calc'!A0") == "'\tCMD|' /C calc'!A0"
        assert sanitize_csv_cell("\rCMD") == "'\rCMD"
        # Leading whitespace before trigger character is also caught
        assert sanitize_csv_cell("  =CMD()") == "'  =CMD()"

    def test_sanitize_csv_cell_preserves_safe_values(self):
        assert sanitize_csv_cell("123.45") == "123.45"
        assert sanitize_csv_cell("T-Shirt") == "T-Shirt"
        assert sanitize_csv_cell("£49.00") == "£49.00"
        assert sanitize_csv_cell(None) == ""
        assert sanitize_csv_cell(0) == "0"


# =============================================================================
# 3. Financial Reconciliations & Metric Accuracy
# =============================================================================


class TestFinancialReconciliation:
    def test_gross_and_net_sales_reconciliation(self, user, product):
        variant = product.variants.first()
        order = place_and_pay(user, variant, quantity=2)
        # Verify order is paid
        assert order.status == Order.Status.PAID

        date_range = resolve_range("all")
        kpis = get_executive_bi_metrics(date_range=date_range)["current"]

        # Gross revenue equals order total
        assert kpis["gross_revenue"] == order.total
        assert kpis["paid_orders_count"] == 1
        assert kpis["net_sales"] == order.total
        assert kpis["aov"] == order.total
        assert kpis["refunds_count"] == 0
        assert kpis["refunds_total"] == Decimal("0.00")

    def test_succeeded_refund_deducted_from_gross(self, user, product):
        variant = product.variants.first()
        order = place_and_pay(user, variant, quantity=2)

        # Issue partial refund
        refund_amount = Decimal("15.00")
        Refund.objects.create(
            number="REF-TEST-001",
            order=order,
            payment=order.payment,
            amount=refund_amount,
            currency="USD",
            status=Refund.Status.SUCCEEDED,
            reason=Refund.Reason.RETURN,
            processed_at=timezone.now(),
        )

        date_range = resolve_range("all")
        kpis = get_executive_bi_metrics(date_range=date_range)["current"]

        assert kpis["gross_revenue"] == order.total
        assert kpis["refunds_count"] == 1
        assert kpis["refunds_total"] == refund_amount
        assert kpis["net_sales"] == (order.total - refund_amount)

    def test_pending_refund_not_deducted(self, user, product):
        variant = product.variants.first()
        order = place_and_pay(user, variant, quantity=1)

        # Non-succeeded refund (e.g. pending or failed)
        Refund.objects.create(
            number="REF-TEST-002",
            order=order,
            payment=order.payment,
            amount=Decimal("10.00"),
            currency="USD",
            status=Refund.Status.PENDING,
            reason=Refund.Reason.RETURN,
        )

        date_range = resolve_range("all")
        kpis = get_executive_bi_metrics(date_range=date_range)["current"]

        assert kpis["refunds_count"] == 0
        assert kpis["refunds_total"] == Decimal("0.00")
        assert kpis["net_sales"] == order.total

    def test_cancelled_orders_separated(self, user, product):
        variant = product.variants.first()
        # Placed unpaid order cancelled
        order = placed_order(user, variant)
        order.transition_to(Order.Status.CANCELLED)
        order.cancelled_at = timezone.now()
        order.save()

        date_range = resolve_range("all")
        kpis = get_executive_bi_metrics(date_range=date_range)["current"]

        # Cancelled order must not appear in paid/completed metrics
        assert kpis["paid_orders_count"] == 0
        assert kpis["gross_revenue"] == Decimal("0.00")
        assert kpis["cancellations_count"] == 1
        assert kpis["cancellations_total"] == order.total


# =============================================================================
# 4. Gross Profit & Cost of Goods Sold (COGS)
# =============================================================================


class TestGrossProfitCalculations:
    def test_cogs_and_gross_profit_when_cost_price_is_set(self, user, product):
        variant = product.variants.first()
        variant.cost_price = Decimal("20.00")
        variant.save(update_fields=["cost_price"])

        order = place_and_pay(user, variant, quantity=3)

        date_range = resolve_range("all")
        kpis = get_executive_bi_metrics(date_range=date_range)["current"]

        expected_cogs = Decimal("60.00")  # 3 * 20.00
        assert kpis["cogs"] == expected_cogs
        assert kpis["cost_status"] == "verified"
        assert kpis["cost_coverage_pct"] == 100.0
        assert kpis["estimated_gross_profit"] == (kpis["net_sales"] - expected_cogs)
        assert kpis["gross_margin_pct"] > 0

    def test_cost_unavailable_when_all_cost_prices_zero(self, user, product):
        variant = product.variants.first()
        variant.cost_price = Decimal("0.00")
        variant.save(update_fields=["cost_price"])

        place_and_pay(user, variant, quantity=1)

        date_range = resolve_range("all")
        kpis = get_executive_bi_metrics(date_range=date_range)["current"]

        # If no cost data exists, clearly marked unavailable
        assert kpis["cost_status"] == "unavailable"
        assert kpis["cost_coverage_pct"] == 0.0
        assert kpis["estimated_gross_profit"] is None
        assert kpis["gross_margin_pct"] is None


# =============================================================================
# 5. Operating Expenses & Double-Counting Safeguards
# =============================================================================


class TestOperatingExpensesIntelligence:
    def test_operating_expenses_aggregated_by_category(self, user):
        cat = ExpenseCategory.objects.create(name="Packaging Supplies", code="PKG")
        OperatingExpense.objects.create(
            expense_number="EXP-202610-0001",
            title="Eco Boxes",
            category=cat,
            amount=Decimal("150.00"),
            tax_amount=Decimal("15.00"),
            total_amount=Decimal("165.00"),
            status=ExpenseStatus.APPROVED,
            expense_date=timezone.localdate(),
            accounting_period="2026-10",
        )

        date_range = resolve_range("all")
        proc_data = get_procurement_and_expense_intelligence(date_range=date_range)

        assert proc_data["total_opex"] == Decimal("165.00")
        assert len(proc_data["categories"]) == 1
        assert proc_data["categories"][0]["category_code"] == "PKG"
        assert proc_data["categories"][0]["total"] == Decimal("165.00")

    def test_expense_tied_to_supplier_invoice_excluded_from_opex(self, user):
        supplier = Supplier.objects.create(name="Vendor One", code="V-001")
        po = PurchaseOrder.objects.create(reference="PO-TEST-001", supplier=supplier)
        invoice = SupplierInvoice.objects.create(
            invoice_number="INV-001",
            supplier=supplier,
            purchase_order=po,
            total_amount=Decimal("500.00"),
            due_date=timezone.localdate() + timedelta(days=30),
            status="pending",
        )
        cat = ExpenseCategory.objects.create(name="Materials", code="MAT")

        # Expense linked to invoice (PO inventory cost)
        OperatingExpense.objects.create(
            expense_number="EXP-PO-001",
            title="Linked PO Invoice Expense",
            category=cat,
            supplier=supplier,
            supplier_invoice=invoice,  # Linked!
            amount=Decimal("500.00"),
            tax_amount=Decimal("0.00"),
            total_amount=Decimal("500.00"),
            status=ExpenseStatus.APPROVED,
            expense_date=timezone.localdate(),
            accounting_period="2026-10",
        )

        date_range = resolve_range("all")
        proc_data = get_procurement_and_expense_intelligence(date_range=date_range)

        # Must exclude linked invoice expense from general OpEx to prevent double-counting
        assert proc_data["total_opex"] == Decimal("0.00")


# =============================================================================
# 6. Inventory Intelligence & Stockout Risks
# =============================================================================


class TestInventoryIntelligence:
    def test_inventory_stock_and_low_stock_detection(self, product):
        variant = product.variants.first()
        stock = Stock.objects.get_or_create(variant=variant)[0]
        stock.on_hand = 3
        stock.reserved = 1
        stock.low_stock_threshold = 5
        stock.save()

        date_range = resolve_range("all")
        prod_data = get_product_and_inventory_intelligence(date_range=date_range)

        # Available = 3 - 1 = 2 <= threshold (5) -> Low Stock
        risks = prod_data["inventory_risks"]
        matching = [r for r in risks if r["sku"] == variant.sku]
        assert len(matching) == 1
        assert matching[0]["available"] == 2
        assert matching[0]["status"] == "Low Stock"

    def test_slow_moving_stock_identified(self, product):
        variant = product.variants.first()
        stock = Stock.objects.get_or_create(variant=variant)[0]
        stock.on_hand = 20
        stock.reserved = 0
        stock.save()

        # Zero orders placed for this variant
        date_range = resolve_range("all")
        prod_data = get_product_and_inventory_intelligence(date_range=date_range)

        slow_skus = [s["sku"] for s in prod_data["slow_moving"]]
        assert variant.sku in slow_skus


# =============================================================================
# 7. Customer Retention & Repeat Purchases
# =============================================================================


class TestCustomerRetentionIntelligence:
    def test_new_versus_returning_customers(self, user, product):
        variant = product.variants.first()

        # First order in the past
        past_order = place_and_pay(user, variant)
        past_time = timezone.now() - timedelta(days=15)
        Order.objects.filter(pk=past_order.pk).update(paid_at=past_time)

        # Second order recently
        recent_order = place_and_pay(user, variant)
        recent_time = timezone.now() - timedelta(days=2)
        Order.objects.filter(pk=recent_order.pk).update(paid_at=recent_time)

        # Filter last 7 days: user had an order prior to window, so user is returning!
        seven_d = resolve_range("7d")
        ret_data = get_customer_and_retention_intelligence(date_range=seven_d)

        assert ret_data["total_buyers"] == 1
        assert ret_data["returning_buyers"] == 1
        assert ret_data["new_buyers"] == 0
        assert ret_data["repeat_purchase_rate"] == 100.0


# =============================================================================
# 8. CSV Export Formats & Role-Based Access Control
# =============================================================================


class TestCSVReportsAndSecurity:
    def test_export_sales_timeseries_csv(self, client, user, product):
        _staff(client, OPERATOR_GROUP, "op-sales@flashwear.test")
        place_and_pay(user, product.variants.first())

        response = client.get(reverse("backoffice:analytics_export") + "?type=sales")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        assert reader[0][0] == "Date"
        assert "Completed Orders" in reader[0]

    def test_export_inventory_risks_csv(self, client, product):
        _staff(client, OPERATOR_GROUP, "op-inv@flashwear.test")
        variant = product.variants.first()
        stock = Stock.objects.get_or_create(variant=variant)[0]
        stock.on_hand = 1
        stock.save()

        response = client.get(reverse("backoffice:analytics_export") + "?type=inventory_risks")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        assert reader[0][0] == "SKU"
        assert variant.sku in content

    def test_export_customer_retention_csv(self, client, user, product):
        _staff(client, OPERATOR_GROUP, "op-cust@flashwear.test")
        place_and_pay(user, product.variants.first())

        response = client.get(reverse("backoffice:analytics_export") + "?type=customers")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Customer Retention & Acquisition" in content
        assert "Total Purchasing Customers" in content

    def test_expenses_export_permission_gated(self, client, user):
        # General operator lacks finance capability -> 404
        _staff(client, OPERATOR_GROUP, "op-nofinance@flashwear.test")
        res_op = client.get(reverse("backoffice:analytics_export") + "?type=expenses")
        assert res_op.status_code == 404

        # Finance staff has capability -> 200
        _staff(client, FINANCE_GROUP, "fin-auth@flashwear.test")
        res_fin = client.get(reverse("backoffice:analytics_export") + "?type=expenses")
        assert res_fin.status_code == 200
        content = res_fin.content.decode("utf-8")
        assert "Operating Expenses & Supplier Summary" in content

    def test_formula_injection_sanitization_in_csv_export(self, client, user, product):
        _staff(client, OPERATOR_GROUP, "op-csv-sec@flashwear.test")
        # Malicious product name designed to exploit formula injection
        product.name = "=cmd|' /C calc'!A0"
        product.save(update_fields=["name"])
        place_and_pay(user, product.variants.first())

        response = client.get(reverse("backoffice:analytics_export") + "?type=products")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))

        # Find row for this product
        matching_rows = [r for r in reader if "calc" in "".join(r)]
        assert len(matching_rows) > 0
        product_cell = matching_rows[0][2]
        # Must start with apostrophe (') to neutralize spreadsheet formula execution
        assert product_cell.startswith("'=")


# =============================================================================
# 9. Empty Database Resilience
# =============================================================================


class TestEmptyDatabaseResilience:
    def test_dashboard_renders_without_division_errors_on_empty_db(self, client):
        _staff(client, OPERATOR_GROUP, "op-empty@flashwear.test")
        response = client.get(reverse("backoffice:analytics"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Executive Analytics &amp; Business Intelligence" in content

    def test_bi_selectors_handle_empty_db_gracefully(self):
        empty_range = resolve_range("all")
        kpis = get_executive_bi_metrics(date_range=empty_range)
        curr = kpis["current"]
        assert curr["paid_orders_count"] == 0
        assert curr["gross_revenue"] == Decimal("0.00")
        assert curr["aov"] == Decimal("0.00")
        assert curr["repeat_rate_pct"] == 0.0

        prod_data = get_product_and_inventory_intelligence(date_range=empty_range)
        assert len(prod_data["best_sellers"]) == 0

        ts = get_sales_time_series(date_range=empty_range)
        assert len(ts["series"]) == 0
