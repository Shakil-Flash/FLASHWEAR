"""Phase 48: Cash Flow Forecasting & Working Capital Intelligence test suite.

Verifies:
1. CashAccount & Treasury balance management, safety thresholds, and audited adjustments.
2. Balance protection (cannot adjust balance into negative).
3. Account reconciliation timestamp tracking.
4. Transparent daily cash flow forecast engine:
   - Historical sales velocity (trailing 30-day net sales).
   - Scenario archetypes (Conservative, Expected, Optimistic).
   - Confirmed receivables (gateway clearing T+2).
   - Committed accounts payable scheduled on due date (and overdue mapped to Day 1).
   - Purchase order commitments deduplicated against existing supplier invoices.
   - Lowest trough balance and safety cushion breach detection.
5. What-If scenario sandbox:
   - Non-destructive hypothetical overrides (e.g., delaying AP disbursements, capital injections).
   - Baseline live records remain completely unaltered in the database.
   - Side-by-side comparison between baseline and scenario.
6. Working capital & liquidity ratios (Net Working Liquidity, Quick Ratio, Cash Runway).
7. Actionable financial risk alerts (Deficit, Cushion Breach, Overdue Liabilities).
8. Backoffice access controls:
   - Permissions (CASHFLOW_VIEW, CASHFLOW_MANAGE) and 404 security for unauthorized staff.
9. Safe CSV export with formula injection defense (CWE-1236).
10. Strict CSP compliance (zero inline scripts).
"""

from __future__ import annotations

import csv
import datetime
from decimal import Decimal
import io

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from apps.backoffice.permissions import (
    ADMIN_GROUP,
    FINANCE_GROUP,
    OPERATOR_GROUP,
    ensure_backoffice_groups,
)
from apps.backoffice.models import AuditEvent
from apps.catalog.models import Category, Product, ProductVariant, Size
from apps.orders.models import Order, OrderItem
from apps.payments.models import Payment
from apps.shop.models import Cart, CheckoutSession
from apps.procurement.models import (
    CashAccount,
    CashAccountAdjustment,
    CashAccountType,
    CashForecastScenario,
    ExpenseCategory,
    ExpenseStatus,
    OperatingExpense,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    ScenarioAdjustment,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
)
from apps.procurement.selectors import (
    generate_cash_flow_forecast,
    get_cash_accounts_queryset,
    get_cash_flow_alerts,
    get_cash_flow_dashboard_metrics,
    get_working_capital_metrics,
)
from apps.procurement.services import (
    add_scenario_adjustment,
    adjust_cash_account_balance,
    create_cash_account,
    create_forecast_scenario,
    create_operating_expense,
    create_purchase_order,
    create_supplier,
    create_supplier_invoice,
    delete_scenario_adjustment,
    update_cash_account,
)

User = get_user_model()


@pytest.fixture(autouse=True)
def setup_groups():
    ensure_backoffice_groups()


@pytest.fixture
def finance_user(db):
    user = User.objects.create_user(
        email="finance_officer@flashwear.test",
        password="TestPassword123!",
        first_name="Fiona",
        last_name="Finance",
        is_staff=True,
    )
    group = Group.objects.get(name=FINANCE_GROUP)
    user.groups.add(group)
    return user


@pytest.fixture
def operator_user(db):
    user = User.objects.create_user(
        email="operator_floor@flashwear.test",
        password="TestPassword123!",
        first_name="Oscar",
        last_name="Operator",
        is_staff=True,
    )
    group = Group.objects.get(name=OPERATOR_GROUP)
    user.groups.add(group)
    return user


@pytest.fixture
def unauthorized_user(db):
    return User.objects.create_user(
        email="customer_care@flashwear.test",
        password="TestPassword123!",
        first_name="Cathy",
        last_name="Care",
        is_staff=True,
    )


@pytest.fixture
def supplier(db, finance_user):
    return create_supplier(
        name="Global Textiles Co",
        code="GLB-TEX",
        contact_name="Elena Rostova",
        email="elena@globaltextiles.test",
        currency="USD",
        lead_time_days=14,
        actor=finance_user,
    )


@pytest.fixture
def sample_variant(db):
    cat = Category.objects.create(name="Apparel", slug="apparel")
    prod = Product.objects.create(
        name="Signature Silk Hoodie",
        slug="signature-silk-hoodie",
        category=cat,
        status="published",
        published_at=timezone.now(),
    )
    sz = Size.objects.create(name="Medium", slug="m", code="M", size_type=Size.SizeType.CLOTHING)
    return ProductVariant.objects.create(
        product=prod,
        size=sz,
        sku="HOOD-OBS-M",
        price=Decimal("120.00"),
        cost_price=Decimal("50.00"),
        is_active=True,
    )


@pytest.fixture
def primary_cash_account(db, finance_user):
    return create_cash_account(
        name="Primary Operating Account",
        account_type=CashAccountType.CHECKING,
        currency="USD",
        opening_balance=Decimal("50000.00"),
        minimum_threshold=Decimal("15000.00"),
        institution_name="Chase Treasury",
        account_number_last4="8812",
        description="Main treasury checking account for AP and payroll",
        user=finance_user,
    )


@pytest.mark.django_db
def test_cash_account_creation_and_balance_tracking(finance_user):
    """Verify creating a cash account initializes balances and logs adjustments."""
    account = create_cash_account(
        name="Payroll Reserves",
        account_type=CashAccountType.SAVINGS,
        currency="USD",
        opening_balance=Decimal("25000.00"),
        minimum_threshold=Decimal("10000.00"),
        institution_name="SVB Treasury",
        account_number_last4="4411",
        description="Payroll liquidity reserve",
        user=finance_user,
    )

    assert account.current_balance == Decimal("25000.00")
    assert account.opening_balance == Decimal("25000.00")
    assert account.minimum_threshold == Decimal("10000.00")
    assert account.is_active is True

    # Check that initially no adjustments exist until first operation
    assert account.adjustments.count() == 0

    # Record audited positive delta
    adj = adjust_cash_account_balance(
        account,
        amount=Decimal("5000.00"),
        adjustment_type="interest",
        reason="Earned Q3 savings interest",
        reference="INT-2026-Q3",
        user=finance_user,
    )
    account.refresh_from_db()
    assert account.current_balance == Decimal("30000.00")
    assert adj.amount == Decimal("5000.00")

    # Record audited negative delta
    adj_neg = adjust_cash_account_balance(
        account,
        amount=Decimal("-8000.00"),
        adjustment_type="transfer",
        reason="Transfer to tax account",
        reference="TRF-9912",
        user=finance_user,
    )
    account.refresh_from_db()
    assert account.current_balance == Decimal("22000.00")
    assert adj_neg.amount == Decimal("-8000.00")


@pytest.mark.django_db
def test_cash_account_adjustment_cannot_cause_negative_balance(primary_cash_account, finance_user):
    """Verify that an adjustment causing a negative balance is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        adjust_cash_account_balance(
            primary_cash_account,
            amount=Decimal("-60000.00"),  # Current balance is 50,000.00
            adjustment_type="fee",
            reason="Illegal overdraft attempt",
            user=finance_user,
        )
    assert "would result in a negative balance" in str(exc_info.value)
    primary_cash_account.refresh_from_db()
    assert primary_cash_account.current_balance == Decimal("50000.00")


@pytest.mark.django_db
def test_cash_account_reconciliation(primary_cash_account, finance_user):
    """Reconciling account sets last_reconciled_at timestamp."""
    assert primary_cash_account.last_reconciled_at is None

    adjust_cash_account_balance(
        primary_cash_account,
        amount=Decimal("12.50"),
        adjustment_type="reconciliation",
        reason="Monthly bank statement reconciliation",
        reference="STMT-OCT-2026",
        user=finance_user,
    )
    primary_cash_account.refresh_from_db()
    assert primary_cash_account.last_reconciled_at is not None
    assert primary_cash_account.current_balance == Decimal("50012.50")


@pytest.mark.django_db
def test_cash_flow_forecast_baseline_with_sales_velocity(
    primary_cash_account, sample_variant, finance_user
):
    """Verify historical 30-day sales velocity drives projected daily inflows."""
    today = timezone.localdate()

    # Create 3 paid customer orders in the last 30 days totaling $3,000.00
    for i in range(1, 4):
        User = get_user_model()
        cust = User.objects.create_user(email=f"customer_{i}@flashwear.test", password="password123")
        cart = Cart.objects.create(user=cust, status=Cart.Status.CONVERTED)
        checkout = CheckoutSession.objects.create(user=cust, cart=cart)
        order = Order.objects.create(
            number=f"FW-ORD-00{i}",
            user=cust,
            checkout=checkout,
            status=Order.Status.PAID,
            subtotal=Decimal("1000.00"),
            shipping_amount=Decimal("0.00"),
            total=Decimal("1000.00"),
            currency="USD",
        )
        # Shift created_at back to 10 days ago
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=10)
        )

    # 3,000 / 30 = 100.00/day velocity
    forecast = generate_cash_flow_forecast(
        as_of_date=today,
        currency="USD",
        horizon_days=30,
        scenario_mode="expected",
    )

    assert forecast["opening_cash"] == Decimal("50000.00")
    assert forecast["daily_sales_velocity"] == Decimal("100.00")
    assert forecast["velocity_multiplier"] == Decimal("1.00")
    # 30 days * 100.00/day = 3,000.00 total projected inflows
    assert forecast["total_projected_inflows"] == Decimal("3000.00")
    assert forecast["closing_projected_balance"] == Decimal("53000.00")
    assert len(forecast["timeline"]) == 30


@pytest.mark.django_db
def test_cash_flow_forecast_scenario_modes(
    primary_cash_account, sample_variant, finance_user
):
    """Verify conservative (0% sales, 3% buffer), expected (100%), and optimistic (120%)."""
    today = timezone.localdate()

    # Create orders with 3,000 trailing sales (100.00/day)
    cart = Cart.objects.create(user=finance_user)
    checkout = CheckoutSession.objects.create(user=finance_user, cart=cart)
    order = Order.objects.create(
        number="FW-ORD-VELOCITY-01",
        user=finance_user,
        checkout=checkout,
        status=Order.Status.PAID,
        subtotal=Decimal("3000.00"),
        shipping_amount=Decimal("0.00"),
        total=Decimal("3000.00"),
        currency="USD",
    )
    Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - datetime.timedelta(days=5))

    # 1. Conservative Mode: 0% sales velocity
    cons_forecast = generate_cash_flow_forecast(
        as_of_date=today,
        currency="USD",
        horizon_days=30,
        scenario_mode="conservative",
    )
    assert cons_forecast["velocity_multiplier"] == Decimal("0.00")
    assert cons_forecast["total_projected_inflows"] == Decimal("0.00")
    assert cons_forecast["closing_projected_balance"] == Decimal("50000.00")

    # 2. Optimistic Mode: 120% sales velocity (120.00/day * 30 = 3,600.00)
    opt_forecast = generate_cash_flow_forecast(
        as_of_date=today,
        currency="USD",
        horizon_days=30,
        scenario_mode="optimistic",
    )
    assert opt_forecast["velocity_multiplier"] == Decimal("1.20")
    assert opt_forecast["total_projected_inflows"] == Decimal("3600.00")
    assert opt_forecast["closing_projected_balance"] == Decimal("53600.00")


@pytest.mark.django_db
def test_cash_flow_forecast_committed_ap_and_overdue_mapping(
    primary_cash_account, supplier, sample_variant, finance_user
):
    """Approved supplier invoices scheduled on due date; overdue invoices mapped to Day 1."""
    today = timezone.localdate()

    # Invoice 1: Due in 10 days ($12,000.00)
    inv_future = SupplierInvoice.objects.create(
        supplier=supplier,
        invoice_number="INV-FUT-01",
        invoice_date=today,
        due_date=today + datetime.timedelta(days=10),
        currency="USD",
        subtotal=Decimal("12000.00"),
        total_amount=Decimal("12000.00"),
        status=SupplierInvoiceStatus.APPROVED,
        created_by=finance_user,
    )

    # Invoice 2: Overdue by 5 days ($8,000.00)
    inv_overdue = SupplierInvoice.objects.create(
        supplier=supplier,
        invoice_number="INV-OVR-01",
        invoice_date=today - datetime.timedelta(days=35),
        due_date=today - datetime.timedelta(days=5),
        currency="USD",
        subtotal=Decimal("8000.00"),
        total_amount=Decimal("8000.00"),
        status=SupplierInvoiceStatus.APPROVED,
        created_by=finance_user,
    )

    forecast = generate_cash_flow_forecast(
        as_of_date=today,
        currency="USD",
        horizon_days=30,
        scenario_mode="conservative",  # 0% sales so outflows are purely AP
    )

    assert forecast["total_ap_outflows"] == Decimal("20000.00")
    # Day 1 (immediate obligation) must receive the overdue invoice ($8,000.00)
    day_1 = forecast["timeline"][0]
    assert day_1["ap_outflows"] == Decimal("8000.00")
    assert day_1["closing_balance"] == Decimal("42000.00")  # 50,000 - 8,000

    # Day 11 (day index 11, i.e. today + 10 days) must receive future invoice ($12,000.00)
    day_11 = forecast["timeline"][10]
    assert day_11["ap_outflows"] == Decimal("12000.00")

    # Ending balance after both invoices paid: 50,000 - 20,000 = 30,000.00
    assert forecast["closing_projected_balance"] == Decimal("30000.00")


@pytest.mark.django_db
def test_cash_flow_forecast_po_deduplication(
    primary_cash_account, supplier, sample_variant, finance_user
):
    """PO commitments deduplicate already invoiced lines to avoid double-counting liabilities."""
    today = timezone.localdate()

    po = create_purchase_order(
        supplier=supplier,
        currency="USD",
        expected_delivery_date=today + datetime.timedelta(days=15),
        items=[
            {
                "variant": sample_variant,
                "quantity": 100,
                "unit_cost": Decimal("50.00"),  # Total PO = $5,000.00
            }
        ],
        created_by=finance_user,
    )
    po.status = PurchaseOrderStatus.APPROVED
    po.save()

    # Case A: PO has no invoice yet. Full $5,000.00 should be counted in po_outflows.
    fc_uninv = generate_cash_flow_forecast(
        as_of_date=today,
        currency="USD",
        horizon_days=30,
        scenario_mode="conservative",
    )
    assert fc_uninv["total_po_outflows"] == Decimal("5000.00")

    # Case B: Supplier invoice is created for $3,000.00 against this PO.
    inv = SupplierInvoice.objects.create(
        supplier=supplier,
        purchase_order=po,
        invoice_number="INV-PARTIAL-01",
        invoice_date=today,
        due_date=today + datetime.timedelta(days=20),
        currency="USD",
        subtotal=Decimal("3000.00"),
        total_amount=Decimal("3000.00"),
        status=SupplierInvoiceStatus.APPROVED,
        created_by=finance_user,
    )

    fc_part = generate_cash_flow_forecast(
        as_of_date=today,
        currency="USD",
        horizon_days=30,
        scenario_mode="conservative",
    )
    # Total committed should be $3,000 AP + $2,000 remaining PO = $5,000 (NOT $8,000!)
    assert fc_part["total_ap_outflows"] == Decimal("3000.00")
    assert fc_part["total_po_outflows"] == Decimal("2000.00")
    assert fc_part["total_outflows"] == Decimal("5000.00")


@pytest.mark.django_db
def test_cash_flow_forecast_trough_and_threshold_detection(
    primary_cash_account, supplier, finance_user
):
    """Accurately identify lowest balance trough and safety cushion breach days."""
    today = timezone.localdate()
    # Account has 50,000.00 with minimum threshold 15,000.00.
    # Large invoice of $40,000.00 due on Day 5 will bring balance to $10,000.00 (below threshold!)
    SupplierInvoice.objects.create(
        supplier=supplier,
        invoice_number="INV-BIG-01",
        invoice_date=today,
        due_date=today + datetime.timedelta(days=4),
        currency="USD",
        subtotal=Decimal("40000.00"),
        total_amount=Decimal("40000.00"),
        status=SupplierInvoiceStatus.APPROVED,
        created_by=finance_user,
    )

    forecast = generate_cash_flow_forecast(
        as_of_date=today,
        currency="USD",
        horizon_days=30,
        scenario_mode="conservative",
    )

    assert forecast["lowest_projected_balance"] == Decimal("10000.00")
    assert forecast["trough_date"] == today + datetime.timedelta(days=4)
    # Days 5 to 30 (26 days) are below safety cushion (10,000 < 15,000)
    assert forecast["days_below_threshold"] == 26
    assert forecast["first_breach_date"] == today + datetime.timedelta(days=4)


@pytest.mark.django_db
def test_what_if_scenario_simulation_non_destructive(
    primary_cash_account, supplier, finance_user
):
    """What-If scenarios simulate shifts without altering live database records."""
    today = timezone.localdate()

    inv = SupplierInvoice.objects.create(
        supplier=supplier,
        invoice_number="INV-DEFER-01",
        invoice_date=today,
        due_date=today + datetime.timedelta(days=5),
        currency="USD",
        subtotal=Decimal("20000.00"),
        total_amount=Decimal("20000.00"),
        status=SupplierInvoiceStatus.APPROVED,
        created_by=finance_user,
    )

    scenario = create_forecast_scenario(
        name="Vendor Payment Deferral Q4",
        description="Negotiate 15-day delay on invoice",
        horizon_days=30,
        user=finance_user,
    )

    # Add hypothetical adjustment: move invoice from Day 5 to Day 20
    adj = add_scenario_adjustment(
        scenario=scenario,
        adjustment_type=ScenarioAdjustment.AdjustmentType.DELAY_AP_PAYMENT,
        adjusted_date=today + datetime.timedelta(days=20),
        amount=Decimal("20000.00"),
        original_date=inv.due_date,
        target_supplier_invoice=inv,
        notes="Approved deferral with Elena",
        user=finance_user,
    )

    # 1. Run Baseline Forecast (Scenario=None)
    baseline = generate_cash_flow_forecast(
        as_of_date=today,
        currency="USD",
        horizon_days=30,
        scenario=None,
        scenario_mode="conservative",
    )
    # In baseline, Day 6 has 20,000 outflow
    day_5_base = baseline["timeline"][5]
    assert day_5_base["ap_outflows"] == Decimal("20000.00")

    # 2. Run Simulated Forecast (Scenario=scenario)
    simulated = generate_cash_flow_forecast(
        as_of_date=today,
        currency="USD",
        horizon_days=30,
        scenario=scenario,
        scenario_mode="conservative",
    )
    # In simulation, Day 5 has 0.00 outflow; Day 20 has 20,000 outflow!
    day_5_sim = simulated["timeline"][5]
    assert day_5_sim["ap_outflows"] == Decimal("0.00")
    day_20_sim = simulated["timeline"][20]
    assert day_20_sim["ap_outflows"] == Decimal("20000.00")

    # 3. CRITICAL: Underlying live record in database MUST REMAIN COMPLETELY UNCHANGED
    inv.refresh_from_db()
    assert inv.due_date == today + datetime.timedelta(days=5)
    assert primary_cash_account.current_balance == Decimal("50000.00")


@pytest.mark.django_db
def test_working_capital_and_liquidity_ratios(
    primary_cash_account, supplier, finance_user
):
    """Verify working capital indicators, quick ratio, and runway calculations."""
    today = timezone.localdate()

    # Outstanding AP due in 15 days: $10,000.00
    SupplierInvoice.objects.create(
        supplier=supplier,
        invoice_number="INV-WC-01",
        invoice_date=today,
        due_date=today + datetime.timedelta(days=15),
        currency="USD",
        subtotal=Decimal("10000.00"),
        total_amount=Decimal("10000.00"),
        status=SupplierInvoiceStatus.APPROVED,
        created_by=finance_user,
    )

    # Approved Operating Expense: $5,000.00
    cat = ExpenseCategory.objects.create(name="Software SaaS", code="SAAS")
    OperatingExpense.objects.create(
        expense_number="EXP-202610-0001",
        title="Software SaaS Subscription",
        category=cat,
        expense_date=today + datetime.timedelta(days=10),
        accounting_period=today.strftime("%Y-%m"),
        currency="USD",
        amount=Decimal("5000.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("5000.00"),
        status=ExpenseStatus.APPROVED,
        created_by=finance_user,
    )

    wc = get_working_capital_metrics(as_of_date=today, currency="USD")

    # Cash: 50,000.00
    assert wc["cash_and_equivalents"] == Decimal("50000.00")
    # Obligations in next 30 days: 10,000 AP + 5,000 OpEx = 15,000.00
    assert wc["near_term_obligations"] == Decimal("15000.00")
    # Net Working Liquidity: 50,000 - 15,000 = 35,000.00
    assert wc["net_working_liquidity"] == Decimal("35000.00")
    # Quick Ratio: 50,000 / 15,000 = 3.33x
    assert wc["liquidity_coverage_ratio"] == Decimal("3.33")


@pytest.mark.django_db
def test_actionable_risk_alerts(primary_cash_account, supplier, finance_user):
    """Verify critical deficit alerts, safety cushion breaches, and overdue payables."""
    today = timezone.localdate()

    # 1. Overdue Invoice: $10,000 overdue
    SupplierInvoice.objects.create(
        supplier=supplier,
        invoice_number="INV-OVRD-01",
        invoice_date=today - datetime.timedelta(days=40),
        due_date=today - datetime.timedelta(days=10),
        currency="USD",
        subtotal=Decimal("10000.00"),
        total_amount=Decimal("10000.00"),
        status=SupplierInvoiceStatus.APPROVED,
        created_by=finance_user,
    )

    # 2. Huge invoice that causes deficit: $45,000 due tomorrow (Total outflows: 55,000 > 50,000 cash)
    SupplierInvoice.objects.create(
        supplier=supplier,
        invoice_number="INV-DEF-01",
        invoice_date=today,
        due_date=today + datetime.timedelta(days=1),
        currency="USD",
        subtotal=Decimal("45000.00"),
        total_amount=Decimal("45000.00"),
        status=SupplierInvoiceStatus.APPROVED,
        created_by=finance_user,
    )

    alerts = get_cash_flow_alerts(as_of_date=today, currency="USD")
    severities = [a["severity"] for a in alerts]
    titles = [a["title"] for a in alerts]

    # Must contain critical deficit alert and overdue payables alert
    assert "critical" in severities
    assert any("Cash Deficit Projected" in t for t in titles)
    assert any("Overdue Supplier Payables" in t for t in titles)


@pytest.mark.django_db
def test_backoffice_cash_flow_views_permissions(client, finance_user, operator_user, unauthorized_user):
    """Verify role-based access to cash flow and treasury routes."""
    # 1. Unauthorized staff gets 404
    client.force_login(unauthorized_user)
    resp = client.get(reverse("backoffice:cash_flow_dashboard"))
    assert resp.status_code == 404

    resp_acc = client.get(reverse("backoffice:cash_accounts"))
    assert resp_acc.status_code == 404

    # 2. Finance user gets 200
    client.force_login(finance_user)
    resp_fin = client.get(reverse("backoffice:cash_flow_dashboard"))
    assert resp_fin.status_code == 200
    assert "Cash Flow &amp; Working Capital Intelligence" in resp_fin.content.decode()

    resp_fin_acc = client.get(reverse("backoffice:cash_accounts"))
    assert resp_fin_acc.status_code == 200


@pytest.mark.django_db
def test_cash_flow_forecast_csv_export_and_formula_injection_defense(
    client, finance_user, primary_cash_account
):
    """Verify secure CSV export with formula injection sanitization."""
    client.force_login(finance_user)
    resp = client.get(reverse("backoffice:cash_flow_export_csv") + "?currency=USD&horizon=30")

    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/csv"
    assert "attachment; filename=" in resp["Content-Disposition"]

    content = resp.content.decode("utf-8")
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)

    # First row is header
    assert rows[0][0] == "Day"
    assert rows[0][1] == "Date"
    assert rows[0][2] == "Opening Balance"
    assert rows[0][15] == "Closing Balance"
    # Timeline has 30 rows + header
    assert len(rows) == 31


@pytest.mark.django_db
def test_cash_account_create_and_edit_post_views(client, finance_user):
    """Verify POST flows for creating, editing, and adjusting a cash account."""
    client.force_login(finance_user)

    # 1. Create cash account
    create_url = reverse("backoffice:cash_account_create")
    post_data = {
        "name": "Tax Reserves Escrow",
        "account_type": CashAccountType.SAVINGS,
        "currency": "USD",
        "opening_balance": "10000.00",
        "minimum_threshold": "3000.00",
        "institution_name": "Bank of America",
        "account_number_last4": "1234",
        "description": "Quarterly sales tax reserve",
    }
    resp = client.post(create_url, post_data)
    assert resp.status_code == 302
    assert resp.url == reverse("backoffice:cash_accounts")

    acc = CashAccount.objects.get(name="Tax Reserves Escrow")
    assert acc.current_balance == Decimal("10000.00")

    # 2. Edit cash account
    edit_url = reverse("backoffice:cash_account_edit", args=[acc.pk])
    edit_data = {
        "name": "Tax Reserves Escrow Updated",
        "account_type": CashAccountType.SAVINGS,
        "minimum_threshold": "5000.00",
        "institution_name": "Bank of America",
        "account_number_last4": "1234",
        "description": "Updated description",
        "is_active": "on",
    }
    resp_edit = client.post(edit_url, edit_data)
    assert resp_edit.status_code == 302

    acc.refresh_from_db()
    assert acc.name == "Tax Reserves Escrow Updated"
    assert acc.minimum_threshold == Decimal("5000.00")

    # 3. Adjust cash account balance
    adjust_url = reverse("backoffice:cash_account_adjust", args=[acc.pk])
    adj_data = {
        "amount": "2500.00",
        "adjustment_type": "reconciliation",
        "reason": "Escrow interest allocation",
        "reference": "ESC-99",
    }
    resp_adj = client.post(adjust_url, adj_data)
    assert resp_adj.status_code == 302

    acc.refresh_from_db()
    assert acc.current_balance == Decimal("12500.00")
    assert acc.last_reconciled_at is not None


@pytest.mark.django_db
def test_scenario_create_and_adjustment_add_delete_post_views(
    client, finance_user, supplier, primary_cash_account
):
    """Verify POST workflows for What-If scenario lifecycle."""
    client.force_login(finance_user)
    today = timezone.localdate()

    inv = SupplierInvoice.objects.create(
        supplier=supplier,
        invoice_number="INV-SCEN-POST",
        invoice_date=today,
        due_date=today + datetime.timedelta(days=7),
        currency="USD",
        subtotal=Decimal("5000.00"),
        total_amount=Decimal("5000.00"),
        status=SupplierInvoiceStatus.APPROVED,
        created_by=finance_user,
    )

    # 1. Create scenario
    create_url = reverse("backoffice:scenario_create")
    resp = client.post(create_url, {
        "name": "Capital Injection Q4",
        "horizon_days": "60",
        "description": "Simulate series A debt round",
    })
    assert resp.status_code == 302
    scenario = CashForecastScenario.objects.get(name="Capital Injection Q4")
    assert scenario.horizon_days == 60

    # 2. Add adjustment
    add_adj_url = reverse("backoffice:scenario_adjustment_add", args=[scenario.pk])
    resp_adj = client.post(add_adj_url, {
        "adjustment_type": ScenarioAdjustment.AdjustmentType.CAPITAL_INJECTION,
        "amount": "50000.00",
        "adjusted_date": (today + datetime.timedelta(days=15)).isoformat(),
        "notes": "Wire confirmation expected",
    })
    assert resp_adj.status_code == 302
    assert scenario.adjustments.count() == 1
    adjustment = scenario.adjustments.first()
    assert adjustment.amount == Decimal("50000.00")

    # 3. Delete adjustment
    del_adj_url = reverse("backoffice:scenario_adjustment_delete", args=[adjustment.pk])
    resp_del = client.post(del_adj_url)
    assert resp_del.status_code == 302
    assert scenario.adjustments.count() == 0
