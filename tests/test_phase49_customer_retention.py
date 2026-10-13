"""Phase 49: Customer Retention & Repeat-Purchase Intelligence Test Suite.

Verifies:
1. Repeat-Purchase Rate (RPR) metrics and calculations across completed orders.
2. Inter-purchase intervals (average days between successive orders).
3. Returning customer revenue contribution percentage.
4. Descriptive and predictive Customer Lifetime Value (CLV).
5. Cohort retention analysis matrix (M0 to M6).
6. Dynamic customer behavioral segmentation (New, Repeat, Active, Loyal, High Spender, At-Risk, Churned).
7. Explainable retention recommendations (Win-back, unspent loyalty points, catalog exploration).
8. Retention campaign management (draft, review, launch, cancel).
9. Consent-enforced dispatch & suppression of opted-out customers.
10. Multi-day revenue attribution within campaign window (14 days).
11. Backoffice permission barriers (RETENTION_VIEW, CAMPAIGNS_MANAGE, 404 for unauthorized staff).
12. Strict CSP compliance (zero inline scripts, valid semantic markup).
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Address, Profile
from apps.backoffice.permissions import (
    ADMIN_GROUP,
    MARKETING_GROUP,
    OPERATOR_GROUP,
    ensure_backoffice_groups,
)
from apps.catalog.models import Category, Product, ProductVariant
from apps.notifications.models import NotificationCategory
from apps.notifications.services import preferences
from apps.engagement.models import (
    CampaignObjective,
    CampaignRecipient,
    CustomerSegmentChoice,
    PointsTransaction,
    Promotion,
    RecipientStatus,
    RetentionCampaign,
)
from apps.engagement.selectors import (
    get_campaigns_queryset,
    get_cohort_retention_matrix,
    get_customer_purchase_data,
    get_customer_retention_profile,
    get_customer_segments_summary,
    get_retention_overview_metrics,
    get_retention_recommendations,
)
from apps.engagement.services.retention import (
    attribute_order_to_campaigns,
    cancel_retention_campaign,
    create_retention_campaign,
    launch_retention_campaign,
    update_retention_campaign,
)
from apps.orders.models import Order, OrderItem
from apps.shop.models import Cart, CheckoutSession

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_groups():
    ensure_backoffice_groups()


@pytest.fixture
def base_product(db):
    cat, _ = Category.objects.get_or_create(
        slug="streetwear",
        defaults={"name": "Streetwear", "is_active": True},
    )
    prod, _ = Product.objects.get_or_create(
        slug="cyber-hoodie",
        defaults={
            "name": "Cyber Hoodie",
            "category": cat,
            "status": Product.Status.ACTIVE,
        },
    )
    variant, _ = ProductVariant.objects.get_or_create(
        sku="CYBER-HOODIE-M",
        defaults={
            "product": prod,
            "price": Decimal("80.00"),
            "is_active": True,
        },
    )
    return prod, variant


@pytest.fixture
def make_order(base_product):
    prod, variant = base_product
    counter = [0]

    def _create(user, days_ago=0, total=Decimal("100.00"), status=Order.Status.PAID):
        counter[0] += 1
        placed = timezone.now() - timedelta(days=days_ago)

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.marketing_email_opt_in = True
        profile.save(update_fields=["marketing_email_opt_in"])
        preferences.set_preferences(user, {NotificationCategory.PROMOTIONS.value: {"email": True}})

        cart, _ = Cart.objects.get_or_create(
            user=user,
            status=Cart.Status.CONVERTED,
            defaults={"session_key": ""},
        )
        addr, _ = Address.objects.get_or_create(
            user=user,
            defaults={
                "full_name": "Alex Mercer",
                "phone": "+44 7700 900333",
                "line1": "10 Fashion Way",
                "city": "London",
                "postal_code": "EC1A 1BB",
                "country": "GB",
            },
        )
        checkout = CheckoutSession.objects.create(
            user=user,
            cart=cart,
            shipping_address=addr,
            status=CheckoutSession.Status.CONVERTED,
        )
        order = Order.objects.create(
            number=f"FW-{user.pk}-{counter[0]}-{days_ago}",
            user=user,
            checkout=checkout,
            status=status,
            currency="GBP",
            subtotal=total,
            shipping_amount=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total=total,
            shipping_code="standard",
            shipping_name="Standard Delivery",
            paid_at=placed if status in (Order.Status.PAID, Order.Status.DELIVERED) else None,
        )
        OrderItem.objects.create(
            order=order,
            variant=variant,
            sku=variant.sku,
            product_name=prod.name,
            quantity=1,
            unit_price=total,
            line_total=total,
        )
        Order.objects.filter(pk=order.pk).update(created_at=placed)
        order.refresh_from_db()
        return order

    return _create


@pytest.fixture
def staff_users(db):
    marketing_user = User.objects.create_user(
        email="marketing@flashwear.local",
        password="ValidPassword123!",
        is_staff=True,
    )
    marketing_user.groups.add(Group.objects.get(name=MARKETING_GROUP))

    admin_user = User.objects.create_user(
        email="admin@flashwear.local",
        password="ValidPassword123!",
        is_staff=True,
    )
    admin_user.groups.add(Group.objects.get(name=ADMIN_GROUP))

    operator_user = User.objects.create_user(
        email="operator@flashwear.local",
        password="ValidPassword123!",
        is_staff=True,
    )
    operator_user.groups.add(Group.objects.get(name=OPERATOR_GROUP))

    unauthorized_user = User.objects.create_user(
        email="unauthorized@flashwear.local",
        password="ValidPassword123!",
        is_staff=False,
    )

    return {
        "marketing": marketing_user,
        "admin": admin_user,
        "operator": operator_user,
        "unauthorized": unauthorized_user,
    }


# =============================================================================
# 1. Retention Metrics & Mathematics
# =============================================================================


def test_repeat_purchase_rate_and_revenue_metrics(make_order):
    user_single = User.objects.create_user(email="single@test.local", password="Pw1")
    user_repeat = User.objects.create_user(email="repeat@test.local", password="Pw2")

    # Single buyer: 1 order
    make_order(user_single, days_ago=40, total=Decimal("50.00"), status=Order.Status.PAID)

    # Repeat buyer: 3 orders
    make_order(user_repeat, days_ago=50, total=Decimal("100.00"), status=Order.Status.PAID)
    make_order(user_repeat, days_ago=30, total=Decimal("100.00"), status=Order.Status.PAID)
    make_order(user_repeat, days_ago=10, total=Decimal("100.00"), status=Order.Status.PAID)

    metrics = get_retention_overview_metrics()

    assert metrics["total_customers"] == 2
    assert metrics["first_time_customers"] == 1
    assert metrics["repeat_customers"] == 1
    # RPR: 1 / 2 = 50.0%
    assert metrics["repeat_purchase_rate"] == 50.0

    # Revenue: Total = 350.00, Repeat = 200.00
    assert metrics["total_net_revenue"] == Decimal("350.00")
    assert metrics["total_repeat_revenue"] == Decimal("200.00")
    # Share: 200 / 350 = 57.1%
    assert metrics["returning_revenue_share_pct"] == 57.1

    # Purchase frequency: 4 orders / 2 customers = 2.0
    assert metrics["avg_purchase_frequency"] == 2.0

    # Inter-purchase interval for repeat buyer: (50-30=20, 30-10=20) -> avg 20.0 days
    assert metrics["avg_inter_purchase_interval_days"] == 20.0


def test_customer_lifetime_value_calculations(make_order):
    u = User.objects.create_user(email="clv@test.local", password="Pw1")
    make_order(u, days_ago=20, total=Decimal("150.00"), status=Order.Status.PAID)
    make_order(u, days_ago=5, total=Decimal("50.00"), status=Order.Status.PAID)

    metrics = get_retention_overview_metrics()
    assert metrics["total_customers"] == 1
    assert metrics["clv_historical_descriptive"] == Decimal("200.00")
    assert metrics["clv_predictive_estimate"] > Decimal("0.00")


# =============================================================================
# 2. Cohort Retention Matrix
# =============================================================================


def test_cohort_retention_matrix(make_order):
    u1 = User.objects.create_user(email="c1@test.local", password="Pw1")
    u2 = User.objects.create_user(email="c2@test.local", password="Pw2")

    # Both placed first order 65 days ago (~2 months ago)
    make_order(u1, days_ago=65, total=Decimal("80.00"), status=Order.Status.PAID)
    make_order(u2, days_ago=65, total=Decimal("80.00"), status=Order.Status.PAID)

    # u1 placed a second order 10 days ago (subsequent month)
    make_order(u1, days_ago=10, total=Decimal("60.00"), status=Order.Status.PAID)

    matrix = get_cohort_retention_matrix(max_cohorts=4)
    assert len(matrix) >= 1
    first_cohort = [c for c in matrix if c["cohort_size"] == 2][0]
    assert first_cohort["retention_rates"][0] == 100.0
    # u1 returned in a subsequent month
    assert any(rate > 0 for m, rate in first_cohort["retention_rates"].items() if m > 0)


# =============================================================================
# 3. Customer Behavioral Segmentation
# =============================================================================


def test_customer_segmentation_classification(make_order):
    # Active repeat customer: 3 orders, recent order 5 days ago
    u_active = User.objects.create_user(email="active@test.local", password="Pw1")
    make_order(u_active, days_ago=60, total=Decimal("100.00"))
    make_order(u_active, days_ago=30, total=Decimal("100.00"))
    make_order(u_active, days_ago=5, total=Decimal("100.00"))

    # High spender / Loyal customer: 6 orders, £720
    u_loyal = User.objects.create_user(email="loyal@test.local", password="Pw2")
    for d in [100, 80, 60, 40, 20, 2]:
        make_order(u_loyal, days_ago=d, total=Decimal("120.00"))

    # At-risk customer: 2 orders, last order 75 days ago (between 45 and 120 days)
    u_risk = User.objects.create_user(email="risk@test.local", password="Pw3")
    make_order(u_risk, days_ago=100, total=Decimal("70.00"))
    make_order(u_risk, days_ago=75, total=Decimal("70.00"))

    # Churned customer: 1 order, placed 150 days ago (> 120 days)
    u_churn = User.objects.create_user(email="churn@test.local", password="Pw4")
    make_order(u_churn, days_ago=150, total=Decimal("50.00"))

    summary = get_customer_segments_summary()
    assert summary["total_purchasers"] == 4

    p_active = get_customer_retention_profile(u_active.pk)
    assert "repeat" in p_active["segments"]
    assert "active" in p_active["segments"]

    p_loyal = get_customer_retention_profile(u_loyal.pk)
    assert "loyal" in p_loyal["segments"]
    assert "high_spenders" in p_loyal["segments"]

    p_risk = get_customer_retention_profile(u_risk.pk)
    assert "at_risk" in p_risk["segments"]

    p_churn = get_customer_retention_profile(u_churn.pk)
    assert "churned" in p_churn["segments"]


# =============================================================================
# 4. Explainable Retention Recommendations
# =============================================================================


def test_retention_recommendations_generation(make_order):
    # Customer with unspent loyalty points (>= 500 points, inactive >= 30 days)
    u_loyalty = User.objects.create_user(email="points@test.local", password="Pw1")
    make_order(u_loyalty, days_ago=35, total=Decimal("120.00"))
    PointsTransaction.objects.create(
        user=u_loyalty,
        amount=600,
        transaction_type=PointsTransaction.TransactionType.BONUS,
        reference=f"bonus:{u_loyalty.pk}",
    )

    # Customer at risk of churn (>= 2 orders, inactive > 45 days)
    u_lapsed = User.objects.create_user(email="lapsed@test.local", password="Pw2")
    make_order(u_lapsed, days_ago=120, total=Decimal("90.00"))
    make_order(u_lapsed, days_ago=90, total=Decimal("90.00"))

    recs = get_retention_recommendations(limit=10)

    # Must contain unspent points recommendation
    points_recs = [r for r in recs if r["type"] == "LOYALTY_UNSPENT_POINTS"]
    assert len(points_recs) >= 1
    assert points_recs[0]["user_id"] == u_loyalty.pk
    assert "600 Points" in points_recs[0]["title"]

    # Must contain win back recommendation
    win_back_recs = [r for r in recs if r["type"] == "WIN_BACK_LAPSED"]
    assert len(win_back_recs) >= 1
    assert win_back_recs[0]["user_id"] == u_lapsed.pk
    assert "90 Days Lapsed" in win_back_recs[0]["title"]


# =============================================================================
# 5. Retention Campaign Lifecycle & Idempotent Outreach
# =============================================================================


def test_retention_campaign_crud_and_status_lifecycle(staff_users):
    admin = staff_users["admin"]

    promo = Promotion.objects.create(
        code="RETENTION15",
        name="Retention 15% Off",
        discount_type=Promotion.DiscountType.PERCENTAGE,
        discount_value=Decimal("15.00"),
        is_active=True,
        starts_at=timezone.now() - timedelta(days=1),
        ends_at=timezone.now() + timedelta(days=30),
    )

    campaign = create_retention_campaign(
        name="VIP Win Back",
        objective=CampaignObjective.WIN_BACK,
        target_segment=CustomerSegmentChoice.AT_RISK,
        message_subject="Special 15% VIP Reward",
        message_body="Hi {{ first_name }}, here is 15% off your next FLASH order.",
        promotion=promo,
        attribution_window_days=14,
        created_by=admin,
    )

    assert campaign.status == "draft"
    assert campaign.is_editable is True
    assert campaign.promotion == promo

    # Update
    updated = update_retention_campaign(
        campaign,
        name="VIP Win Back Autumn",
        message_subject="Updated Subject",
    )
    assert updated.name == "VIP Win Back Autumn"
    assert updated.message_subject == "Updated Subject"

    # Cancel
    cancelled = cancel_retention_campaign(campaign, actor=admin, reason="Strategic change")
    assert cancelled.status == "cancelled"
    assert cancelled.is_editable is False


def test_campaign_launch_consent_enforcement_and_suppression(staff_users, make_order):
    admin = staff_users["admin"]

    # Customer 1: At-risk, marketing subscribed (default)
    u_opted_in = User.objects.create_user(email="optedin@test.local", password="Pw1")
    make_order(u_opted_in, days_ago=70, total=Decimal("100.00"))
    p1, _ = Profile.objects.get_or_create(user=u_opted_in)
    p1.marketing_email_opt_in = True
    p1.save(update_fields=["marketing_email_opt_in"])
    preferences.set_preferences(u_opted_in, {NotificationCategory.PROMOTIONS.value: {"email": True}})

    # Customer 2: At-risk, explicitly opted-out
    u_opted_out = User.objects.create_user(email="optedout@test.local", password="Pw2")
    make_order(u_opted_out, days_ago=70, total=Decimal("100.00"))
    p2, _ = Profile.objects.get_or_create(user=u_opted_out)
    p2.marketing_email_opt_in = False
    p2.save(update_fields=["marketing_email_opt_in"])
    preferences.set_preferences(u_opted_out, {NotificationCategory.PROMOTIONS.value: {"email": False}})

    campaign = create_retention_campaign(
        name="Test Consent Enforcement",
        objective=CampaignObjective.WIN_BACK,
        target_segment=CustomerSegmentChoice.AT_RISK,
        message_subject="Exclusive Win Back",
        message_body="Welcome back deal",
        created_by=admin,
    )

    launched = launch_retention_campaign(campaign, actor=admin)
    assert launched.status == "active"

    # u_opted_in should have received outreach
    assert CampaignRecipient.objects.filter(
        campaign=campaign, user=u_opted_in, status=RecipientStatus.SENT
    ).exists()

    # u_opted_out should NOT have received outreach
    assert not CampaignRecipient.objects.filter(
        campaign=campaign, user=u_opted_out, status=RecipientStatus.SENT
    ).exists()
    assert CampaignRecipient.objects.filter(
        campaign=campaign, user=u_opted_out, status=RecipientStatus.EXCLUDED_UNSUBSCRIBED
    ).exists()


# =============================================================================
# 6. Revenue Attribution
# =============================================================================


def test_revenue_attribution_within_campaign_window(staff_users, make_order):
    admin = staff_users["admin"]
    u = User.objects.create_user(email="buyer@test.local", password="Pw1")
    make_order(u, days_ago=75, total=Decimal("80.00"))

    campaign = create_retention_campaign(
        name="Attribution Test Campaign",
        objective=CampaignObjective.WIN_BACK,
        target_segment=CustomerSegmentChoice.AT_RISK,
        message_subject="Special Discount",
        message_body="Enjoy 10% off",
        attribution_window_days=14,
        created_by=admin,
    )
    launch_retention_campaign(campaign, actor=admin)

    # Recipient was sent campaign outreach
    recipient = CampaignRecipient.objects.get(campaign=campaign, user=u)
    assert recipient.status == RecipientStatus.SENT

    # Customer places a new order 2 days later
    new_order = make_order(u, days_ago=0, total=Decimal("150.00"), status=Order.Status.PAID)

    # Run attribution
    attributed = attribute_order_to_campaigns(new_order)
    assert len(attributed) == 1

    recipient.refresh_from_db()
    assert recipient.status == RecipientStatus.ATTRIBUTED
    assert recipient.attributed_order == new_order
    assert recipient.attributed_revenue == Decimal("150.00")

    camp_annotated = get_campaigns_queryset().get(pk=campaign.pk)
    assert camp_annotated.attributed_count == 1
    assert camp_annotated.total_attributed_revenue == Decimal("150.00")


# =============================================================================
# 7. Backoffice Access Controls & Security (404 for unauthorized)
# =============================================================================


def test_retention_views_access_control(staff_users, client):
    marketing_user = staff_users["marketing"]
    unauthorized_user = staff_users["unauthorized"]

    # 1. Anonymous user redirected to login
    anon_client = Client()
    resp = anon_client.get(reverse("backoffice:retention_dashboard"))
    assert resp.status_code == 302

    # 2. Unauthorized user receives 404
    client.force_login(unauthorized_user)
    resp = client.get(reverse("backoffice:retention_dashboard"))
    assert resp.status_code == 404

    resp = client.get(reverse("backoffice:retention_segments"))
    assert resp.status_code == 404

    resp = client.get(reverse("backoffice:retention_campaigns"))
    assert resp.status_code == 404

    # 3. Marketing staff with RETENTION_VIEW can access dashboard and segments
    client.force_login(marketing_user)
    resp = client.get(reverse("backoffice:retention_dashboard"))
    assert resp.status_code == 200
    assert "Repeat-Purchase Rate" in resp.content.decode("utf-8")

    resp = client.get(reverse("backoffice:retention_segments"))
    assert resp.status_code == 200

    resp = client.get(reverse("backoffice:retention_campaigns"))
    assert resp.status_code == 200


def test_retention_csv_export(staff_users, client, make_order):
    marketing_user = staff_users["marketing"]
    u = User.objects.create_user(email="export@test.local", password="Pw1")
    make_order(u, days_ago=10, total=Decimal("125.00"))

    client.force_login(marketing_user)
    resp = client.get(reverse("backoffice:retention_export_csv"))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/csv; charset=utf-8"
    content = resp.content.decode("utf-8")
    assert str(u.pk) in content
    assert "125.00" in content


# =============================================================================
# 8. Strict CSP Compliance
# =============================================================================


def test_retention_templates_strict_csp_compliance(staff_users, client):
    admin_user = staff_users["admin"]
    client.force_login(admin_user)

    urls_to_test = [
        reverse("backoffice:retention_dashboard"),
        reverse("backoffice:retention_segments"),
        reverse("backoffice:retention_campaigns"),
        reverse("backoffice:retention_campaign_create"),
    ]

    handler = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
    inline_script = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/)", re.I)
    js_url = re.compile(r"javascript:", re.IGNORECASE)

    for url in urls_to_test:
        resp = client.get(url)
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")

        # Zero inline script tags allowed
        assert not inline_script.search(content), f"Inline script found in {url}"
        # Zero inline onclick/onsubmit handlers
        assert not handler.search(content), f"Inline event handler found in {url}"
        assert not js_url.search(content), f"javascript: URL found in {url}"
