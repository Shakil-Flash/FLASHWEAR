"""Phase 51 Automated Test Suite: Unified Notification Center & Customer Communication Preferences.

Comprehensive coverage of:
1. Customer Notification Center (inbox, archived, tabs, mark-read, mark-all, dismiss, dismiss-all).
2. Data Integrity & Object-Level Authorization (IDOR protection, DB record preservation on dismissal).
3. Communication Preferences (multi-channel, mandatory locking, profile sync, phone requirement).
4. Transactional vs Promotional Communication Rules.
5. Provider Webhook Verification (HMAC-SHA256, timestamp drift, replay protection, status transitions).
6. Staff Backoffice Operations (search filter, failure rate metric, safe retry consent guard).
7. Marketing Automation / Retention Campaign Integration.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import time
from datetime import timedelta
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Profile
from apps.backoffice.permissions import ADMIN_GROUP, NOTIFICATIONS_MANAGE, NOTIFICATIONS_VIEW
from apps.backoffice.selectors.notifications import notification_metrics, notifications as bo_notifications
from django.contrib.auth.models import Group
from apps.engagement.models.retention import (
    CampaignObjective,
    CampaignRecipient,
    CampaignStatus,
    CustomerSegmentChoice,
    RecipientStatus,
    RetentionCampaign,
)
from apps.engagement.services.retention import launch_retention_campaign
from apps.notifications.models import (
    Channel,
    DeliveryWebhookEvent,
    Notification,
    NotificationCategory,
    NotificationPreference,
    NotificationStatus,
    NotificationType,
)
from apps.notifications.services import events, in_app, preferences
from apps.notifications.services.delivery_retry import retry_failed_notification
from django.test import RequestFactory
from apps.notifications.services.webhooks import (
    WebhookAuthenticationError,
    WebhookProcessingError,
    process_delivery_webhook,
    verify_webhook_signature,
)

User = get_user_model()


@pytest.fixture
def user_a(db):
    user = User.objects.create_user(
        email="user_a@example.com",
        password="ValidPassword123!",
    )
    profile = user.profile
    profile.phone = "+1234567890"
    profile.marketing_opt_in = True
    profile.marketing_sms_opt_in = True
    profile.marketing_push_opt_in = True
    profile.save()
    preferences.set_preferences(
        user,
        {
            NotificationCategory.PROMOTIONS: {
                "email": True,
                "in_app": True,
                "sms": True,
                "push": True,
            }
        },
    )
    return user


@pytest.fixture
def user_b(db):
    user = User.objects.create_user(
        email="user_b@example.com",
        password="ValidPassword123!",
    )
    profile = user.profile
    profile.marketing_opt_in = False
    profile.marketing_sms_opt_in = False
    profile.marketing_push_opt_in = False
    profile.save()
    preferences.set_preferences(
        user,
        {
            NotificationCategory.PROMOTIONS: {
                "email": False,
                "in_app": False,
                "sms": False,
                "push": False,
            }
        },
    )
    return user


@pytest.fixture
def staff_user(db):
    user = User.objects.create_user(
        email="staff_op@example.com",
        password="ValidPassword123!",
        is_staff=True,
        is_superuser=True,
    )
    grp, _ = Group.objects.get_or_create(name=ADMIN_GROUP)
    user.groups.add(grp)
    return user


# ==============================================================================
# 1. Customer Notification Center: List, Detail, Read, Dismiss, Tabs
# ==============================================================================

@pytest.mark.django_db
def test_notification_center_unauthenticated_redirects(client):
    url = reverse("notifications:center")
    response = client.get(url)
    assert response.status_code == 302
    assert "/login" in response.url or "/account" in response.url


@pytest.mark.django_db
def test_notification_center_inbox_and_archived_tabs(client, user_a):
    client.force_login(user_a)

    # Create 2 active notifications and 1 dismissed notification
    notif1 = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.ORDER_CONFIRMED,
        category=NotificationCategory.ORDERS,
        channel=Channel.IN_APP,
        status=NotificationStatus.SENT,
        title="Order #101 Confirmed",
        body="Your order is confirmed.",
        idempotency_key="order:101",
    )
    notif2 = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.POINTS_EARNED,
        category=NotificationCategory.LOYALTY,
        channel=Channel.IN_APP,
        status=NotificationStatus.SENT,
        title="Points Earned",
        body="You earned 50 points.",
        idempotency_key="points:50",
    )
    notif_archived = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.PASSWORD_CHANGED,
        category=NotificationCategory.ACCOUNT,
        channel=Channel.IN_APP,
        status=NotificationStatus.SENT,
        title="Password Updated",
        body="Your password changed.",
        idempotency_key="pwd:1",
        dismissed_at=timezone.now(),
    )

    # Default inbox view: shows notif1 and notif2, but not notif_archived
    resp = client.get(reverse("notifications:center"))
    assert resp.status_code == 200
    assert resp.context["active_tab"] == "inbox"
    items = list(resp.context["notifications"])
    assert notif1 in items
    assert notif2 in items
    assert notif_archived not in items

    # Archived tab view: shows notif_archived, but not notif1 or notif2
    resp_archived = client.get(reverse("notifications:center") + "?tab=archived")
    assert resp_archived.status_code == 200
    assert resp_archived.context["active_tab"] == "archived"
    archived_items = list(resp_archived.context["notifications"])
    assert notif_archived in archived_items
    assert notif1 not in archived_items


@pytest.mark.django_db
def test_notification_detail_marks_read_and_prevents_idor(client, user_a, user_b):
    notif_a = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.ORDER_CONFIRMED,
        category=NotificationCategory.ORDERS,
        channel=Channel.IN_APP,
        status=NotificationStatus.SENT,
        title="Your order",
        body="Order details",
        idempotency_key="key_a",
    )
    assert notif_a.is_unread

    # User B cannot access User A's notification
    client.force_login(user_b)
    resp_b = client.get(reverse("notifications:detail", kwargs={"pk": notif_a.pk}))
    assert resp_b.status_code == 404

    # User A accesses: gets 200 (renders notification detail) and marks read
    client.force_login(user_a)
    resp_a = client.get(reverse("notifications:detail", kwargs={"pk": notif_a.pk}))
    assert resp_a.status_code == 200
    notif_a.refresh_from_db()
    assert notif_a.is_read
    assert notif_a.read_at is not None


@pytest.mark.django_db
def test_mark_read_action_and_read_all(client, user_a, user_b):
    notif1 = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.ORDER_CONFIRMED,
        category=NotificationCategory.ORDERS,
        channel=Channel.IN_APP,
        status=NotificationStatus.SENT,
        title="1",
        body="1",
        idempotency_key="k1",
    )
    notif2 = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.ORDER_CONFIRMED,
        category=NotificationCategory.ORDERS,
        channel=Channel.IN_APP,
        status=NotificationStatus.SENT,
        title="2",
        body="2",
        idempotency_key="k2",
    )

    client.force_login(user_b)
    # User B cannot mark User A's notification read
    resp = client.post(reverse("notifications:mark-read", kwargs={"pk": notif1.pk}))
    assert resp.status_code == 404

    client.force_login(user_a)
    # User A marks notif1 read
    resp = client.post(reverse("notifications:mark-read", kwargs={"pk": notif1.pk}))
    assert resp.status_code == 302
    notif1.refresh_from_db()
    assert notif1.is_read
    notif2.refresh_from_db()
    assert notif2.is_unread

    # Mark all read
    resp = client.post(reverse("notifications:read-all"))
    assert resp.status_code == 302
    notif2.refresh_from_db()
    assert notif2.is_read


@pytest.mark.django_db
def test_dismissal_preserves_audit_records_and_hides_from_inbox(client, user_a, user_b):
    notif = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.ORDER_CONFIRMED,
        category=NotificationCategory.ORDERS,
        channel=Channel.IN_APP,
        status=NotificationStatus.SENT,
        title="Invoice Notice",
        body="Your receipt is available.",
        idempotency_key="invoice:1",
    )

    # User B cannot dismiss User A's notification
    client.force_login(user_b)
    resp = client.post(reverse("notifications:dismiss", kwargs={"pk": notif.pk}))
    assert resp.status_code == 404

    client.force_login(user_a)
    resp = client.post(reverse("notifications:dismiss", kwargs={"pk": notif.pk}))
    assert resp.status_code == 302

    # Verification: record is NOT deleted, but dismissed_at is set!
    notif.refresh_from_db()
    assert notif.dismissed_at is not None
    assert Notification.objects.filter(pk=notif.pk).exists()

    # Active queryset excludes it, dismissed queryset includes it
    assert not Notification.objects.active().filter(pk=notif.pk).exists()
    assert Notification.objects.dismissed().filter(pk=notif.pk).exists()


@pytest.mark.django_db
def test_dismiss_all_action(client, user_a):
    for i in range(3):
        Notification.objects.create(
            user=user_a,
            notification_type=NotificationType.POINTS_EARNED,
            category=NotificationCategory.LOYALTY,
            channel=Channel.IN_APP,
            status=NotificationStatus.SENT,
            title=f"Points {i}",
            body=f"Points body {i}",
            idempotency_key=f"points_key_{i}",
        )
    assert Notification.objects.filter(user=user_a, dismissed_at__isnull=True).count() == 3

    client.force_login(user_a)
    resp = client.post(reverse("notifications:dismiss-all"))
    assert resp.status_code == 302

    # All active notifications are now dismissed, none deleted
    assert Notification.objects.filter(user=user_a, dismissed_at__isnull=True).count() == 0
    assert Notification.objects.filter(user=user_a, dismissed_at__isnull=False).count() == 3


# ==============================================================================
# 2. Communication Preferences: Multi-Channel, Mandatory Locking & Phone Checks
# ==============================================================================

@pytest.mark.django_db
def test_preferences_mandatory_categories_remain_locked(user_a):
    # Try setting mandatory categories (orders, payments) to False
    payload = {
        NotificationCategory.ORDERS: {"email": False, "in_app": False, "sms": False, "push": False},
        NotificationCategory.PAYMENTS: {"email": False, "in_app": False, "sms": False, "push": False},
        NotificationCategory.PROMOTIONS: {"email": False, "in_app": False, "sms": False, "push": False},
    }
    preferences.set_preferences(user_a, payload)

    eff_orders = preferences.effective_channels(user_a, NotificationType.ORDER_SHIPPED)
    # Mandatory categories MUST always have email and in_app
    assert Channel.EMAIL in eff_orders
    assert Channel.IN_APP in eff_orders

    eff_promos = preferences.effective_channels(user_a, NotificationType.PROMOTION_AVAILABLE)
    assert Channel.EMAIL not in eff_promos
    assert Channel.IN_APP not in eff_promos


@pytest.mark.django_db
def test_preferences_sync_with_profile_opt_in_flags(user_a):
    profile = user_a.profile
    assert profile.marketing_opt_in is True
    assert profile.marketing_sms_opt_in is True
    assert profile.marketing_push_opt_in is True

    # Opt out of promotions on all channels
    payload = {
        NotificationCategory.PROMOTIONS: {"email": False, "in_app": False, "sms": False, "push": False},
    }
    preferences.set_preferences(user_a, payload)

    profile.refresh_from_db()
    assert profile.marketing_opt_in is False
    assert profile.marketing_sms_opt_in is False
    assert profile.marketing_push_opt_in is False


@pytest.mark.django_db
def test_preferences_sms_opt_in_requires_phone(user_b):
    # User B has no phone
    profile = user_b.profile
    assert not profile.phone

    payload = {
        NotificationCategory.PROMOTIONS: {"email": True, "in_app": True, "sms": True, "push": True},
    }
    # Setting sms=True without a phone must safely force sms=False without DB constraint crash
    preferences.set_preferences(user_b, payload)

    profile.refresh_from_db()
    assert profile.marketing_sms_opt_in is False
    pref = NotificationPreference.objects.get(user=user_b, category=NotificationCategory.PROMOTIONS)
    assert pref.sms_enabled is False


@pytest.mark.django_db
def test_unsubscribe_token_revokes_marketing(client, user_a):
    tok = preferences.ensure_unsubscribe_token(user_a)
    url = reverse("notifications:unsubscribe", kwargs={"token": tok.token})

    # Unsubscribe is accessible without login
    resp = client.post(url)
    assert resp.status_code == 200

    user_a.profile.refresh_from_db()
    assert user_a.profile.marketing_opt_in is False
    assert user_a.profile.marketing_sms_opt_in is False
    assert user_a.profile.marketing_push_opt_in is False

    # Second unsubscribe is completely idempotent
    resp_again = client.post(url)
    assert resp_again.status_code == 200


# ==============================================================================
# 3. Transactional vs Promotional Rules
# ==============================================================================

@pytest.mark.django_db
def test_transactional_notification_emits_even_when_opted_out(user_b):
    # User B has opted out of all marketing
    assert user_b.profile.marketing_opt_in is False

    # Emit transactional ORDER_SHIPPED
    notifs = events.emit(
        notification_type=NotificationType.ORDER_SHIPPED,
        user=user_b,
        idempotency_key="tx_order_123",
        context={"order_number": "FW-12345", "tracking_number": "TRK123"},
    )
    assert len(notifs) >= 1
    assert any(n.channel == Channel.IN_APP for n in notifs)
    assert any(n.channel == Channel.EMAIL for n in notifs)


@pytest.mark.django_db
def test_promotional_notification_suppressed_when_opted_out(user_b):
    assert user_b.profile.marketing_opt_in is False

    # Emit promotional PROMOTION_AVAILABLE
    notifs = events.emit(
        notification_type=NotificationType.PROMOTION_AVAILABLE,
        user=user_b,
        idempotency_key="promo_spring_sale",
        context={"promotion_name": "Spring Sale"},
    )
    # Email channel must NOT be dispatched because user has opted out!
    email_notifs = [n for n in notifs if n.channel == Channel.EMAIL]
    assert len(email_notifs) == 0


# ==============================================================================
# 4. Webhook Verification & Delivery Architecture
# ==============================================================================

@pytest.mark.django_db
def test_webhook_signature_verification_and_tamper_detection(settings):
    secret = "test_webhook_secret_key_123"
    settings.NOTIFICATIONS_WEBHOOK_SECRET = secret

    rf = RequestFactory()
    payload = json.dumps({"event_id": "evt_001", "status": "delivered"}).encode("utf-8")
    now_ts = int(time.time())

    # Valid signature
    signed_content = f"{now_ts}.".encode("utf-8") + payload
    sig = hmac.new(secret.encode("utf-8"), signed_content, hashlib.sha256).hexdigest()

    req = rf.post("/notifications/webhook/generic/", data=payload, content_type="application/json")
    req.headers = {"X-Signature": f"t={now_ts},v1={sig}"}

    # Must verify successfully
    assert verify_webhook_signature(
        req,
        provider="generic",
        secret=secret,
    ) is True

    # Tampered signature must fail
    req_bad = rf.post("/notifications/webhook/generic/", data=payload, content_type="application/json")
    req_bad.headers = {"X-Signature": f"t={now_ts},v1=tampered_signature"}
    with pytest.raises(WebhookAuthenticationError):
        verify_webhook_signature(
            req_bad,
            provider="generic",
            secret=secret,
        )

    # Expired timestamp (> 300s drift) must fail
    old_ts = now_ts - 600
    old_signed = f"{old_ts}.".encode("utf-8") + payload
    old_sig = hmac.new(secret.encode("utf-8"), old_signed, hashlib.sha256).hexdigest()
    req_expired = rf.post("/notifications/webhook/generic/", data=payload, content_type="application/json")
    req_expired.headers = {"X-Signature": f"t={old_ts},v1={old_sig}"}
    with pytest.raises(WebhookAuthenticationError):
        verify_webhook_signature(
            req_expired,
            provider="generic",
            secret=secret,
        )


@pytest.mark.django_db
def test_webhook_status_transition_and_replay_protection(user_a):
    notif = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.ORDER_CONFIRMED,
        category=NotificationCategory.ORDERS,
        channel=Channel.EMAIL,
        status=NotificationStatus.SENT,
        title="Delivery Test",
        body="Body",
        idempotency_key="wh_test_key_1",
    )

    # Process delivery confirmation webhook
    res1 = process_delivery_webhook(
        provider="sendgrid",
        payload={
            "event_id": "sg_evt_1001",
            "event_type": "delivered",
            "notification_id": notif.pk,
            "status": "delivered",
            "email": user_a.email,
        },
    )
    assert res1["status"] == "processed"
    assert res1["notification_id"] == notif.pk
    assert res1["notification_status"] == NotificationStatus.DELIVERED

    notif.refresh_from_db()
    assert notif.status == NotificationStatus.DELIVERED

    # Replay protection: Submitting identical (provider, event_id) must be short-circuited as already_processed
    res2 = process_delivery_webhook(
        provider="sendgrid",
        payload={
            "event_id": "sg_evt_1001",
            "event_type": "delivered",
            "notification_id": notif.pk,
            "status": "delivered",
            "email": user_a.email,
        },
    )
    assert res2["status"] == "already_processed"


@pytest.mark.django_db
def test_webhook_failure_transition_records_sanitized_reason(user_a):
    notif = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.ORDER_CONFIRMED,
        category=NotificationCategory.ORDERS,
        channel=Channel.EMAIL,
        status=NotificationStatus.SENT,
        title="Bounce Test",
        body="Body",
        idempotency_key="wh_bounce_1",
    )

    process_delivery_webhook(
        provider="sendgrid",
        payload={
            "event_id": "sg_evt_bounce_2002",
            "event_type": "bounced",
            "notification_id": notif.pk,
            "status": "bounced",
            "reason": "550 5.1.1 Recipient mailbox not found",
        },
    )

    notif.refresh_from_db()
    assert notif.status == NotificationStatus.FAILED
    assert notif.failed_at is not None
    assert "550 5.1.1" in notif.failure_reason


# ==============================================================================
# 5. Staff Backoffice Operations: Search, Metrics & Safe Retry
# ==============================================================================

@pytest.mark.django_db
def test_backoffice_search_and_metrics(user_a, user_b):
    Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.ORDER_CONFIRMED,
        category=NotificationCategory.ORDERS,
        channel=Channel.EMAIL,
        status=NotificationStatus.SENT,
        title="Alpha Unique Order",
        body="Alpha",
        idempotency_key="alpha_key",
    )
    Notification.objects.create(
        user=user_b,
        notification_type=NotificationType.POINTS_EARNED,
        category=NotificationCategory.LOYALTY,
        channel=Channel.EMAIL,
        status=NotificationStatus.FAILED,
        title="Beta Failed Rewards",
        body="Beta",
        failure_reason="SMTP connection timeout error",
        idempotency_key="beta_key",
    )

    # Search filter matching title
    qs_title = bo_notifications(q="Alpha Unique")
    assert qs_title.count() == 1
    assert qs_title.first()["idempotency_key"] == "alpha_key"

    # Search filter matching failure reason
    qs_reason = bo_notifications(q="timeout error")
    assert qs_reason.count() == 1
    assert qs_reason.first()["idempotency_key"] == "beta_key"

    # Metrics calculation
    metrics = notification_metrics()
    assert metrics["total"] >= 2
    assert metrics["failed"] >= 1
    assert "failure_rate" in metrics
    assert metrics["failure_rate"] > 0


@pytest.mark.django_db
def test_staff_safe_retry_cannot_bypass_marketing_consent(staff_user, user_b):
    # User B has opted out of marketing
    user_b.profile.marketing_opt_in = False
    user_b.profile.save()

    failed_promo = Notification.objects.create(
        user=user_b,
        notification_type=NotificationType.PROMOTION_AVAILABLE,
        category=NotificationCategory.PROMOTIONS,
        channel=Channel.EMAIL,
        status=NotificationStatus.FAILED,
        title="Flash Promo",
        body="Promo text",
        failure_reason="Temporary rate limit",
        idempotency_key="promo_fail_1",
    )

    # Staff retry MUST fail because customer has opted out!
    success, msg = retry_failed_notification(failed_promo.pk)
    assert success is False
    assert "opted out" in msg

    failed_promo.refresh_from_db()
    assert failed_promo.status == NotificationStatus.FAILED


@pytest.mark.django_db
def test_staff_safe_retry_requeues_eligible_transactional_notification(staff_user, user_a):
    failed_order_email = Notification.objects.create(
        user=user_a,
        notification_type=NotificationType.ORDER_CONFIRMED,
        category=NotificationCategory.ORDERS,
        channel=Channel.EMAIL,
        status=NotificationStatus.FAILED,
        title="Order Retry Test",
        body="Order body",
        failure_reason="SMTP connection dropped",
        idempotency_key="order_fail_1",
    )

    success, msg = retry_failed_notification(failed_order_email.pk)
    assert success is True
    assert "re-queued" in msg

    failed_order_email.refresh_from_db()
    assert failed_order_email.status == NotificationStatus.QUEUED
    assert failed_order_email.attempts == 0
    assert failed_order_email.failure_reason == ""


# ==============================================================================
# 6. Marketing Automation (Retention Campaign) Integration
# ==============================================================================

@pytest.mark.django_db
def test_retention_campaign_launch_enforces_consent_and_emits_notifications(user_a, user_b):
    from decimal import Decimal
    from apps.orders.models import Order, OrderItem
    from apps.shop.models import Cart, CheckoutSession
    from apps.accounts.models import Address
    from apps.catalog.models import Category, Product, ProductVariant

    cat, _ = Category.objects.get_or_create(slug="retention-cat", defaults={"name": "Retention Cat", "is_active": True})
    prod, _ = Product.objects.get_or_create(slug="retention-prod", defaults={"name": "Retention Prod", "category": cat, "status": Product.Status.ACTIVE})
    variant, _ = ProductVariant.objects.get_or_create(sku="RETENTION-SKU-1", defaults={"product": prod, "price": Decimal("50.00"), "is_active": True})

    def _create_order(user, num, total_amt):
        cart, _ = Cart.objects.get_or_create(user=user, status=Cart.Status.CONVERTED, defaults={"session_key": ""})
        addr, _ = Address.objects.get_or_create(user=user, defaults={"full_name": "Test User", "phone": "+1234567890", "line1": "Street 1", "city": "City", "postal_code": "12345", "country": "US"})
        checkout = CheckoutSession.objects.create(user=user, cart=cart, shipping_address=addr, status=CheckoutSession.Status.CONVERTED)
        ord_obj = Order.objects.create(
            number=num,
            user=user,
            checkout=checkout,
            status=Order.Status.PAID,
            currency="USD",
            subtotal=total_amt,
            shipping_amount=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total=total_amt,
            shipping_code="standard",
            shipping_name="Standard Delivery",
            paid_at=timezone.now(),
        )
        OrderItem.objects.create(
            order=ord_obj,
            variant=variant,
            sku=variant.sku,
            product_name=prod.name,
            quantity=1,
            unit_price=total_amt,
            line_total=total_amt,
        )
        return ord_obj

    _create_order(user_a, f"FW-P51-A-{user_a.pk}", Decimal("150.00"))
    _create_order(user_b, f"FW-P51-B-{user_b.pk}", Decimal("120.00"))

    # User A is opted in; User B is opted out
    campaign = RetentionCampaign.objects.create(
        name="Win-Back High Spenders",
        slug="win-back-high-spenders",
        objective=CampaignObjective.WIN_BACK,
        target_segment=CustomerSegmentChoice.ALL_CUSTOMERS,
        status=CampaignStatus.DRAFT,
        message_subject="Special VIP Offer",
        message_body="Come back and enjoy 20% off your next purchase.",
    )

    launch_retention_campaign(campaign)

    # Check recipient records
    recip_a = CampaignRecipient.objects.get(campaign=campaign, user=user_a)
    assert recip_a.status == RecipientStatus.SENT

    recip_b = CampaignRecipient.objects.get(campaign=campaign, user=user_b)
    assert recip_b.status == RecipientStatus.EXCLUDED_UNSUBSCRIBED

    # Check unified notification emission for User A
    notifs_a = Notification.objects.filter(
        user=user_a,
        category=NotificationCategory.PROMOTIONS,
        notification_type=NotificationType.PROMOTION_AVAILABLE,
    )
    assert notifs_a.exists()
    assert any("Win-Back" in n.title or "offer" in n.title.lower() for n in notifs_a)

    # Check that User B did not receive any promotional notification
    notifs_b = Notification.objects.filter(
        user=user_b,
        category=NotificationCategory.PROMOTIONS,
    )
    assert not notifs_b.exists()
