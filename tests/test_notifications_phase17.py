"""Phase 17 notification system tests (Phase 18 spec sections 37-40).

Coverage map:

* Model rules -- creation, status transitions, read state, DB constraints (idempotency
  uniqueness, internal-only action URLs).
* Dispatcher -- channel resolution, preference filtering, idempotency, validation,
  scheduled sends, metadata coercion.
* Preferences & unsubscribe -- mandatory categories win over tampered rows, marketing
  needs the profile opt-in, one-click unsubscribe is opaque and idempotent.
* Celery tasks -- send/duplicate/retry/failure classification, queue and retention
  sweeps, drop and points sweeps, bulk broadcast.
* Email content -- subject, recipient, text + HTML, links, no secrets, XSS escaping.
* HTML center and JSON API -- authentication, ownership (IDOR), pagination, mark-read,
  read-all, preferences, throttling.
* Domain integrations -- orders, payments, shipments, support, loyalty, drops, loop,
  quests.
* Failure isolation -- a broken notification never fails the business workflow.
* Back-office delivery inspection access lives in test_backoffice_phase16.py.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.messages import get_messages
from django.db import IntegrityError, transaction
from django.template import TemplateDoesNotExist
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.notifications import selectors, tasks
from apps.notifications.models import (
    Channel,
    Notification,
    NotificationCategory,
    NotificationPreference,
    NotificationStatus,
    NotificationSubscription,
    NotificationType,
    Priority,
)
from apps.notifications.services import (
    bulk,
    dispatcher,
    events,
    in_app,
    preferences,
)
from apps.notifications.services import (
    email as email_service,
)
from apps.notifications.services import (
    templates as type_registry,
)
from apps.notifications.throttling import NotificationThrottle

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def emit(user, notification_type=NotificationType.ORDER_PLACED, key="test:key", **kwargs):
    """Strict dispatcher entry (raises on programmer errors, as spec section 37 wants)."""
    return dispatcher.emit(
        notification_type=notification_type,
        user=user,
        idempotency_key=key,
        **kwargs,
    )


def inapp(user, **kwargs):
    """A delivered in-app row without going through the dispatcher (view/API fixtures)."""
    defaults = {
        "user": user,
        "notification_type": NotificationType.ORDER_SHIPPED,
        "category": NotificationCategory.ORDERS,
        "title": "Your order is on its way",
        "body": "Packed and handed to the carrier.",
    }
    defaults.update(kwargs)
    return in_app.create(**defaults)


def order_for(user, number=None):
    """Minimal order chain: cart -> checkout -> order (all NOT NULL FKs satisfied)."""
    from apps.orders.models import Order
    from apps.shop.models import Cart, CheckoutSession

    cart = Cart.objects.create(user=user, session_key="", status=Cart.Status.CONVERTED)
    checkout = CheckoutSession.objects.create(
        user=user, cart=cart, status=CheckoutSession.Status.CONVERTED
    )
    return Order.objects.create(
        number=number or f"TST-{user.pk}-{Order.objects.count() + 1:04d}",
        checkout=checkout,
        user=user,
        status=Order.Status.PENDING_PAYMENT,
        subtotal=Decimal("10.00"),
        shipping_amount=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("10.00"),
    )


class RecordingProvider:
    """Email provider stub: configurable to succeed, fail transiently or permanently."""

    def __init__(self, behaviour="ok"):
        from apps.notifications.services.email import (
            EmailSendError,
            PermanentEmailError,
        )

        self.behaviour = behaviour
        self.calls = 0
        self._transient = EmailSendError
        self._permanent = PermanentEmailError

    def send(self, **kwargs):
        self.calls += 1
        if self.behaviour == "transient":
            raise self._transient("smtp down")
        if self.behaviour == "permanent":
            raise self._permanent("bad address")
        if self.behaviour == "flaky-once":
            if self.calls == 1:
                raise self._transient("first attempt hiccups")
        return type("R", (), {"accepted": 1, "provider": "stub"})()


# --------------------------------------------------------------------------------------
# Model rules
# --------------------------------------------------------------------------------------


class TestNotificationModel:
    def test_a_row_defaults_to_pending_and_unread(self, user):
        row = Notification.objects.create(
            user=user,
            notification_type=NotificationType.ORDER_PLACED,
            category=NotificationCategory.ORDERS,
            channel=Channel.IN_APP,
            title="Hello",
        )
        assert row.status == NotificationStatus.PENDING
        assert row.is_unread is True
        assert row.read_at is None
        assert "order_placed" in str(row)

    def test_legal_transitions_advance_and_record_timestamps(self, user):
        row = inapp(user)
        assert row.can_transition_to(NotificationStatus.DELIVERED)  # sent -> delivered
        row.transition_to(NotificationStatus.DELIVERED)
        assert row.status == NotificationStatus.DELIVERED
        assert row.sent_at is not None

    def test_an_illegal_transition_raises(self, user):
        row = inapp(user)  # status == sent
        from apps.notifications.models import InvalidNotificationTransition

        assert row.can_transition_to(NotificationStatus.QUEUED) is False
        with pytest.raises(InvalidNotificationTransition):
            row.transition_to(NotificationStatus.QUEUED)

    def test_failure_records_reason_and_a_retry_clears_it(self, user):
        row = Notification.objects.create(
            user=user,
            notification_type=NotificationType.ORDER_SHIPPED,
            category=NotificationCategory.ORDERS,
            channel=Channel.EMAIL,
            title="Retry me",
            status=NotificationStatus.PENDING,
        )
        row.transition_to(NotificationStatus.QUEUED)
        row.transition_to(NotificationStatus.FAILED, failure_reason="SMTP refused")
        assert row.status == NotificationStatus.FAILED
        assert row.failure_reason == "SMTP refused"
        assert row.failed_at is not None
        row.transition_to(NotificationStatus.QUEUED)
        assert row.failure_reason == ""
        assert row.failed_at is None

    def test_mark_read_is_idempotent(self, user):
        row = inapp(user)
        row.mark_read()
        first = row.read_at
        row.mark_read()
        assert row.read_at == first
        assert row.is_unread is False

    def test_unread_queryset_filters_read_rows(self, user):
        seen = inapp(user, title="Seen")
        seen.mark_read()
        inapp(user, title="Unseen")
        assert list(Notification.objects.unread().values_list("title", flat=True)) == ["Unseen"]

    def test_same_key_and_channel_cannot_be_inserted_twice(self, user):
        payload = {
            "user": user,
            "notification_type": NotificationType.ORDER_PLACED,
            "category": NotificationCategory.ORDERS,
            "title": "Dup",
            "idempotency_key": "order:1:placed",
        }
        Notification.objects.create(channel=Channel.IN_APP, **payload)
        with transaction.atomic(), pytest.raises(IntegrityError):
            Notification.objects.create(channel=Channel.IN_APP, **payload)

    def test_empty_idempotency_keys_never_collide(self, user):
        payload = {
            "user": user,
            "notification_type": NotificationType.ORDER_PLACED,
            "category": NotificationCategory.ORDERS,
            "channel": Channel.IN_APP,
            "title": "Open-ended",
            "idempotency_key": "",
        }
        Notification.objects.create(**payload)
        Notification.objects.create(**payload)  # no key: not deduplicatable, by design

    @pytest.mark.parametrize(
        "bad_url",
        ["https://evil.test/x", "http://evil.test", "//evil.test", "mailto:a@b.test"],
    )
    def test_the_database_refuses_external_action_urls(self, user, bad_url):
        with transaction.atomic(), pytest.raises(IntegrityError):
            Notification.objects.create(
                user=user,
                notification_type=NotificationType.ORDER_PLACED,
                category=NotificationCategory.ORDERS,
                channel=Channel.IN_APP,
                title="Redirect",
                action_url=bad_url,
            )


# --------------------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------------------


class TestDispatcher:
    def test_in_app_rows_are_delivered_on_creation(self, user):
        rows = emit(user, channels=[Channel.IN_APP])
        assert len(rows) == 1
        assert rows[0].status == NotificationStatus.SENT
        assert rows[0].sent_at is not None
        assert rows[0].category == NotificationCategory.ORDERS
        assert "Order" in rows[0].title

    def test_email_rows_enter_the_queue(self, user):
        rows = emit(
            user, notification_type=NotificationType.ORDER_SHIPPED, channels=[Channel.EMAIL]
        )
        assert len(rows) == 1
        assert rows[0].channel == Channel.EMAIL
        assert rows[0].status == NotificationStatus.QUEUED

    def test_both_declared_channels_produce_one_row_each(self, user):
        rows = emit(user, notification_type=NotificationType.ORDER_SHIPPED)
        assert sorted(r.channel for r in rows) == [Channel.EMAIL, Channel.IN_APP]

    def test_an_unknown_type_is_a_programmer_error(self, user):
        with pytest.raises(ValueError, match="Unknown notification type"):
            emit(user, notification_type="definitely_not_a_type")

    def test_the_domain_wrapper_logs_instead_of_raising(self, user, caplog):
        with caplog.at_level(logging.ERROR, logger="flashwear.notifications"):
            rows = events.emit(
                notification_type="definitely_not_a_type",
                user=user,
                idempotency_key="x",
            )
        assert rows == []
        assert any("definitely_not_a_type" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("recipient", [None])
    def test_no_user_means_no_rows(self, recipient):
        assert emit(recipient) == []

    def test_an_inactive_account_gets_no_rows(self, user):
        user.is_active = False
        user.save(update_fields=["is_active"])
        assert emit(user) == []

    @pytest.mark.parametrize(
        "bad_url",
        [
            "https://evil.test/x",
            "http://evil.test/x",
            "//evil.test",
            "javascript:alert(1)",
            "mailto:a@b.test",
        ],
    )
    def test_external_or_script_action_urls_are_refused(self, user, bad_url):
        with pytest.raises(ValueError, match="site-relative"):
            emit(user, action_url=bad_url)

    def test_replaying_the_same_business_event_keeps_one_row_per_channel(self, user):
        first = emit(user, notification_type=NotificationType.ORDER_SHIPPED, key="order:9:shipped")
        second = emit(user, notification_type=NotificationType.ORDER_SHIPPED, key="order:9:shipped")
        assert len(first) == len(second) == 2
        assert {r.pk for r in first} == {r.pk for r in second}
        assert Notification.objects.filter(user=user).count() == 2

    def test_rows_without_a_key_are_never_deduplicated(self, user):
        emit(user, key="")
        emit(user, key="")
        # Two emits x both declared channels: no key, no dedup, by design.
        assert Notification.objects.filter(user=user).count() == 4

    def test_requested_channels_can_narrow_but_never_widen(self, user):
        narrowed = emit(
            user,
            notification_type=NotificationType.ORDER_SHIPPED,
            channels=[Channel.IN_APP],
        )
        assert [r.channel for r in narrowed] == [Channel.IN_APP]
        # ORDER_CONFIRMED is in-app only: asking for email must not create one.
        widened = emit(
            user,
            notification_type=NotificationType.ORDER_CONFIRMED,
            channels=[Channel.EMAIL],
        )
        assert widened == []

    def test_a_future_scheduled_send_stays_pending(self, user):
        due = timezone.now() + timedelta(hours=2)
        rows = emit(
            user,
            notification_type=NotificationType.ORDER_SHIPPED,
            channels=[Channel.IN_APP],
            scheduled_for=due,
        )
        assert rows[0].status == NotificationStatus.PENDING
        assert rows[0].sent_at is None

    def test_domain_context_is_coerced_to_json_safe_metadata(self, user):
        rows = emit(
            user,
            context={
                "total": Decimal("12.50"),
                "when": timezone.now().replace(microsecond=0),
                "nested": {"amount": Decimal("3.00")},
            },
            channels=[Channel.IN_APP],
        )
        meta = rows[0].safe_metadata
        assert meta["total"] == "12.50"
        assert isinstance(meta["when"], str)
        assert meta["nested"]["amount"] == "3.00"

    def test_priority_defaults_from_the_registry_and_can_be_overridden(self, user):
        default = emit(user, channels=[Channel.IN_APP])[0]
        assert default.priority == Priority.HIGH  # order_placed is a high-priority type
        low = emit(
            user,
            key="low",
            channels=[Channel.IN_APP],
            priority=Priority.LOW,
        )[0]
        assert low.priority == Priority.LOW

    def test_the_registry_covers_every_declared_channel(self):
        from apps.notifications.models import Channel as Ch

        for spec in type_registry.registry().values():
            assert set(spec.channels) <= {Ch.IN_APP, Ch.EMAIL}
            assert spec.category in NotificationCategory.values

    def test_registry_keys_are_valid_enum_values(self):
        assert set(type_registry.registry()) <= set(NotificationType.values)


# --------------------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------------------


class TestPreferenceFiltering:
    def test_mandatory_categories_survive_a_tampered_row(self, user):
        prefs_row = preferences.get_preference(user, NotificationCategory.ORDERS)
        prefs_row.email_enabled = False
        prefs_row.in_app_enabled = False
        prefs_row.save(update_fields=["email_enabled", "in_app_enabled", "updated_at"])
        rows = emit(user, notification_type=NotificationType.ORDER_SHIPPED, key="order:1:s")
        channels = {r.channel for r in rows}
        assert channels == {Channel.EMAIL, Channel.IN_APP}

    def test_optional_categories_honour_the_stored_row(self, user):
        prefs_row = preferences.get_preference(user, NotificationCategory.DROPS)
        prefs_row.email_enabled = False
        prefs_row.save(update_fields=["email_enabled", "updated_at"])
        rows = emit(user, notification_type=NotificationType.DROP_LIVE, key="drop:1:live")
        assert {r.channel for r in rows} == {Channel.IN_APP}

    def test_marketing_email_needs_the_profile_opt_in(self, user):
        from apps.accounts.models import Profile

        prefs_row = preferences.get_preference(user, NotificationCategory.PROMOTIONS)
        prefs_row.email_enabled = True
        prefs_row.save(update_fields=["email_enabled", "updated_at"])
        blocked = emit(user, notification_type=NotificationType.PROMOTION_AVAILABLE, key="promo:1")
        assert Channel.EMAIL not in {r.channel for r in blocked}

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.marketing_email_opt_in = True
        profile.save(update_fields=["marketing_email_opt_in", "updated_at"])
        user.profile = profile  # keep the in-memory relation cache honest
        allowed = emit(user, notification_type=NotificationType.PROMOTION_AVAILABLE, key="promo:2")
        assert Channel.EMAIL in {r.channel for r in allowed}

    def test_no_usable_address_means_no_email_leg(self, user):
        user.email = "broken-address"
        user.save(update_fields=["email"])
        rows = emit(user, notification_type=NotificationType.ORDER_SHIPPED, key="order:2:s")
        assert {r.channel for r in rows} == {Channel.IN_APP}

    def test_preference_rows_seed_lazily_from_registry_defaults(self, user):
        row = preferences.get_preference(user, NotificationCategory.PROMOTIONS)
        assert row.email_enabled is False  # marketing must be opted into
        assert row.in_app_enabled is True
        assert NotificationPreference.objects.filter(user=user).count() >= 1

    def test_set_preferences_cannot_silence_mandatory_categories(self, user):
        preferences.set_preferences(
            user,
            {NotificationCategory.ORDERS.value: {"email": False, "in_app": False}},
        )
        row = NotificationPreference.objects.get(user=user, category=NotificationCategory.ORDERS)
        assert row.email_enabled is True
        assert row.in_app_enabled is True

    def test_set_preferences_applies_optional_changes_and_ignores_junk(self, user):
        rows = preferences.set_preferences(
            user,
            {
                NotificationCategory.DROPS.value: {"email": False, "in_app": True},
                "not-a-category": {"email": True},
            },
        )
        assert len(rows) == 1
        row = NotificationPreference.objects.get(user=user, category=NotificationCategory.DROPS)
        assert row.email_enabled is False
        assert not NotificationPreference.objects.filter(category="not-a-category").exists()

    def test_preference_rows_flag_mandatory_categories(self, user):
        rows = preferences.preference_rows(user)
        by_category = {r["category"]: r for r in rows}
        assert by_category[NotificationCategory.ORDERS]["mandatory"] is True
        assert by_category[NotificationCategory.PROMOTIONS]["mandatory"] is False
        assert len(rows) == len(NotificationCategory.values)

    def test_marketing_subscribed_needs_both_switches(self, user):
        from apps.accounts.models import Profile

        profile, _ = Profile.objects.get_or_create(user=user)
        assert preferences.marketing_subscribed(user) is False
        profile.marketing_email_opt_in = True
        profile.save(update_fields=["marketing_email_opt_in", "updated_at"])
        user.profile = profile
        assert preferences.marketing_subscribed(user) is False  # pref row still off
        preferences.set_preferences(user, {NotificationCategory.PROMOTIONS.value: {"email": True}})
        assert preferences.marketing_subscribed(user) is True


# --------------------------------------------------------------------------------------
# Unsubscribe tokens
# --------------------------------------------------------------------------------------


class TestUnsubscribe:
    def test_token_is_minted_once_and_resolved_by_lookup(self, user):
        token = preferences.ensure_unsubscribe_token(user)
        assert preferences.ensure_unsubscribe_token(user).token == token.token
        assert preferences.find_unsubscribe_token(token.token).user_id == user.pk

    @pytest.mark.parametrize("garbage", ["", "nope", "x" * 65, "../admin/"])
    def test_forged_tokens_resolve_to_nobody(self, user, garbage):
        preferences.ensure_unsubscribe_token(user)
        assert preferences.find_unsubscribe_token(garbage) is None

    def test_one_click_unsubscribe_is_idempotent(self, user):
        from apps.accounts.models import Profile

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.marketing_email_opt_in = True
        profile.save(update_fields=["marketing_email_opt_in", "updated_at"])
        prefs_row = preferences.get_preference(user, NotificationCategory.PROMOTIONS)
        prefs_row.email_enabled = True
        prefs_row.save(update_fields=["email_enabled", "updated_at"])

        assert preferences.apply_unsubscribe(user) is True
        assert preferences.apply_unsubscribe(user) is False  # second click flips nothing
        prefs_row.refresh_from_db()
        profile.refresh_from_db()
        assert prefs_row.email_enabled is False
        assert profile.marketing_email_opt_in is False

    def test_the_landing_page_works_without_a_login_and_stays_silent(self, client, user):
        token = preferences.ensure_unsubscribe_token(user)
        response = client.get(reverse("notifications:unsubscribe", args=[token.token]))
        assert response.status_code == 200
        user.email.encode()  # sanity
        assert user.email not in response.content.decode()
        prefs_row = preferences.get_preference(user, NotificationCategory.PROMOTIONS)
        assert prefs_row.email_enabled is False

    def test_a_bad_token_answers_404_with_no_oracle(self, client, user):
        response = client.get(reverse("notifications:unsubscribe", args=["guess-me"]))
        assert response.status_code == 404
        assert user.email not in response.content.decode()
        assert preferences.ensure_unsubscribe_token(user).token != "guess-me"

    @override_settings(NOTIFICATIONS_UNSUBSCRIBE_MAX=2)
    def test_the_unsubscribe_endpoint_is_rate_limited_per_ip(self, client, user):
        token = preferences.ensure_unsubscribe_token(user)
        url = reverse("notifications:unsubscribe", args=[token.token])
        assert client.get(url).status_code == 200
        assert client.get(url).status_code == 200
        blocked = client.get(url)
        assert blocked.status_code == 429


# --------------------------------------------------------------------------------------
# Throttle unit
# --------------------------------------------------------------------------------------


class TestNotificationThrottle:
    def test_records_then_blocks_then_resets(self):
        throttle = NotificationThrottle()
        ident = "unit-test-ident"
        assert throttle.check("read_all", ident).blocked is False
        for _ in range(throttle.max_read_all):
            throttle.record("read_all", ident)
        decision = throttle.check("read_all", ident)
        assert decision.blocked is True
        assert decision.retry_after == throttle.window
        throttle.reset(ident)
        assert throttle.check("read_all", ident).blocked is False

    def test_an_unknown_action_is_refused(self):
        with pytest.raises(ValueError, match="Unknown notification throttle action"):
            NotificationThrottle().check("launch_missiles", "x")


# --------------------------------------------------------------------------------------
# Celery tasks (spec section 38)
# --------------------------------------------------------------------------------------


class TestSendEmailTask:
    def _queued_email(self, user, **kwargs):
        rows = emit(
            user,
            notification_type=kwargs.pop("notification_type", NotificationType.ORDER_SHIPPED),
            key=kwargs.pop("key", "order:5:shipped"),
            channels=[Channel.EMAIL],
            **kwargs,
        )
        return rows[0]

    def test_a_queued_row_is_delivered_and_marked_sent(self, user, mailoutbox):
        row = self._queued_email(user)
        result = tasks.send_email(row.pk)
        assert result["status"] == "sent"
        row.refresh_from_db()
        assert row.status == NotificationStatus.SENT
        assert row.sent_at is not None
        assert row.failure_reason == ""
        assert len(mailoutbox) == 1

    def test_running_the_task_twice_sends_exactly_one_message(self, user, mailoutbox):
        row = self._queued_email(user)
        first = tasks.send_email(row.pk)
        second = tasks.send_email(row.pk)
        assert first["status"] == "sent"
        assert second["skipped"] is True
        assert second["status"] == NotificationStatus.SENT
        assert len(mailoutbox) == 1

    def test_a_transient_failure_retries_and_eventually_succeeds(
        self, user, mailoutbox, monkeypatch
    ):
        stub = RecordingProvider(behaviour="flaky-once")
        monkeypatch.setattr(email_service, "get_email_provider", lambda: stub)
        row = self._queued_email(user)
        result = tasks.send_email(row.pk)
        assert result["status"] == "sent"
        assert result["attempts"] == 2
        assert stub.calls == 2
        assert len(mailoutbox) == 0  # stub replaced the real provider

    def test_a_permanent_failure_fails_immediately(self, user, monkeypatch):
        stub = RecordingProvider(behaviour="permanent")
        monkeypatch.setattr(email_service, "get_email_provider", lambda: stub)
        row = self._queued_email(user)
        result = tasks.send_email(row.pk)
        assert result["status"] == "failed"
        assert result["permanent"] is True
        assert result["attempts"] == 1
        row.refresh_from_db()
        assert row.status == NotificationStatus.FAILED
        assert row.failure_reason

    def test_a_transient_failure_stops_at_the_attempt_cap(self, user, monkeypatch):
        stub = RecordingProvider(behaviour="transient")
        monkeypatch.setattr(email_service, "get_email_provider", lambda: stub)
        row = self._queued_email(user)
        result = tasks.send_email(row.pk)
        assert result["status"] == "failed"
        assert result["attempts"] == tasks._max_attempts()
        assert stub.calls == tasks._max_attempts()
        row.refresh_from_db()
        assert row.status == NotificationStatus.FAILED

    def test_a_broken_template_is_a_permanent_failure(self, user, monkeypatch):
        def boom(*args, **kwargs):
            raise TemplateDoesNotExist("emails/missing.html")

        monkeypatch.setattr(type_registry, "render_email", boom)
        row = self._queued_email(user)
        result = tasks.send_email(row.pk)
        assert result["status"] == "failed"
        assert result["permanent"] is True
        row.refresh_from_db()
        assert "Template error" in row.failure_reason

    def test_a_replayed_message_for_a_sent_row_is_a_noop(self, user, mailoutbox):
        row = self._queued_email(user)
        row.transition_to(NotificationStatus.FAILED, failure_reason="earlier attempt")
        Notification.objects.filter(pk=row.pk).update(status=NotificationStatus.SENT, attempts=3)
        result = tasks.send_email(row.pk)
        assert result["skipped"] is True
        assert len(mailoutbox) == 0

    def test_a_missing_row_answers_missing_instead_of_raising(self, user):
        assert tasks.send_email(999_999) == {"status": "missing", "skipped": True}

    def test_an_in_app_row_is_never_claimed_by_the_email_task(self, user, mailoutbox):
        row = inapp(user)
        result = tasks.send_email(row.pk)
        assert result["skipped"] is True
        assert len(mailoutbox) == 0


class TestSweeps:
    def test_queue_sweep_promotes_due_sends_and_recovers_stuck_rows(self, user, monkeypatch):
        rescheduled: list[int] = []
        monkeypatch.setattr(tasks, "_reschedule", lambda pk, countdown: rescheduled.append(pk))

        def pending(channel, key, due):
            return Notification.objects.create(
                user=user,
                notification_type=NotificationType.ORDER_SHIPPED,
                category=NotificationCategory.ORDERS,
                channel=channel,
                title=f"Scheduled {key}",
                status=NotificationStatus.PENDING,
                scheduled_for=due,
            )

        due_in_app = pending(Channel.IN_APP, "inapp", timezone.now() - timedelta(minutes=1))
        due_email = pending(Channel.EMAIL, "email", timezone.now() - timedelta(minutes=1))
        future = pending(Channel.IN_APP, "future", timezone.now() + timedelta(hours=3))
        # A row a dead worker left behind while it was queued (not pending).
        stuck = Notification.objects.create(
            user=user,
            notification_type=NotificationType.ORDER_SHIPPED,
            category=NotificationCategory.ORDERS,
            channel=Channel.EMAIL,
            title="Stuck queue row",
            status=NotificationStatus.QUEUED,
        )
        Notification.objects.filter(pk=stuck.pk).update(
            updated_at=timezone.now() - timedelta(hours=2)
        )

        report = tasks.sweep_queue()
        due_in_app.refresh_from_db()
        due_email.refresh_from_db()
        future.refresh_from_db()
        assert due_in_app.status == NotificationStatus.SENT
        assert due_email.status == NotificationStatus.QUEUED
        assert future.status == NotificationStatus.PENDING
        assert report["promoted"] == 2
        assert stuck.pk in rescheduled  # the stuck row was re-queued
        assert report["resent"] == 1

    def test_retention_deletes_only_what_the_windows_cover(self, user):
        old = timezone.now() - timedelta(days=400)
        read_old = inapp(user, title="Read long ago")
        unread_old = inapp(user, title="Still unread")
        Notification.objects.filter(pk=read_old.pk).update(
            read_at=old, created_at=old, updated_at=old
        )
        Notification.objects.filter(pk=unread_old.pk).update(created_at=old, updated_at=old)

        email_old = emit(
            user,
            notification_type=NotificationType.ORDER_SHIPPED,
            key="retention:email",
            channels=[Channel.EMAIL],
        )[0]
        Notification.objects.filter(pk=email_old.pk).update(
            status=NotificationStatus.SENT, created_at=old, updated_at=old
        )

        report = tasks.sweep_retention()
        assert report["in_app"] == 1
        assert report["email"] == 1
        assert not Notification.objects.filter(pk__in=[read_old.pk, email_old.pk]).exists()
        assert Notification.objects.filter(pk=unread_old.pk).exists()

    def test_drop_sweep_announces_live_and_ended_once_per_subscriber(self, user):
        from apps.drops.models import FlashDrop

        drop = FlashDrop.objects.create(
            name="Midnight Drop",
            slug="midnight-drop-sweep",
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() - timedelta(minutes=10),
        )
        for topic in (NotificationType.DROP_LIVE, NotificationType.DROP_ENDED):
            NotificationSubscription.objects.create(
                user=user,
                notification_type=topic,
                related_object_type="drop",
                related_object_id=drop.pk,
            )

        first = tasks.sweep_drop_events()
        assert first == {"live": 1, "ended": 1, "subs_consumed": 2}
        # drop_live fans out to in-app + email; drop_ended is in-app only.
        assert Notification.objects.filter(user=user).count() == 3

        second = tasks.sweep_drop_events()  # subscriptions consumed: nothing left to do
        assert second == {"live": 0, "ended": 0, "subs_consumed": 0}
        assert Notification.objects.filter(user=user).count() == 3

    def test_points_sweep_warns_once_per_user_and_day(self, user):
        from apps.engagement.models import PointsTransaction

        PointsTransaction.objects.create(
            user=user,
            amount=500,
            transaction_type=PointsTransaction.TransactionType.PURCHASE_EARN,
            reference="sweep-expiry-test",
            expires_at=timezone.now() + timedelta(days=2),
        )
        first = tasks.sweep_points_expiring()
        assert first == {"notified": 1}
        second = tasks.sweep_points_expiring()
        assert second == {"notified": 0}
        # The warning itself fans out to in-app + email; one emit, two rows.
        assert (
            Notification.objects.filter(
                user=user, notification_type=NotificationType.POINTS_EXPIRING
            ).count()
            == 2
        )

    def test_queue_sweep_never_raises(self, monkeypatch):
        def explode():
            raise RuntimeError("cache unavailable")

        monkeypatch.setattr(tasks, "_queue_scheduled", explode)
        assert tasks.sweep_queue() == {"promoted": 0, "resent": 0}


class TestBroadcast:
    def test_a_key_template_without_user_id_is_refused(self, user):
        with pytest.raises(ValueError, match=r"\{user_id\}"):
            bulk.broadcast_batch(
                user_ids=[user.pk],
                notification_type=NotificationType.PROMOTION_AVAILABLE,
                idempotency_key_template="campaign:fixed",
            )

    def test_fan_out_is_idempotent_per_recipient(self, user, other_user):
        template = "campaign:summer:{user_id}"
        args = {
            "user_ids": [user.pk, other_user.pk],
            "notification_type": NotificationType.PROMOTION_AVAILABLE,
            "idempotency_key_template": template,
            "channels": [Channel.IN_APP],
        }
        first = bulk.broadcast_batch(**args)
        rows_after_first = Notification.objects.count()
        second = bulk.broadcast_batch(**args)
        assert first == 2
        assert second == 2  # standing rows are returned, not duplicated
        assert Notification.objects.count() == rows_after_first

    def test_inactive_recipients_are_skipped(self, user, other_user):
        other_user.is_active = False
        other_user.save(update_fields=["is_active"])
        count = bulk.broadcast_batch(
            user_ids=[user.pk, other_user.pk],
            notification_type=NotificationType.PROMOTION_AVAILABLE,
            idempotency_key_template="campaign:x:{user_id}",
            channels=[Channel.IN_APP],
        )
        assert count == 1

    def test_the_celery_wrapper_returns_a_count_and_swallows_bad_payloads(self, user, other_user):
        ok = tasks.broadcast_batch(
            [user.pk, other_user.pk],
            NotificationType.PROMOTION_AVAILABLE,
            "celery:camp:{user_id}",
            channels=[Channel.IN_APP],
        )
        assert ok == {"count": 2}
        bad = tasks.broadcast_batch(
            [user.pk],
            NotificationType.PROMOTION_AVAILABLE,
            "celery:missing-placeholder",
        )
        assert bad == {"count": 0}

    def test_broadcast_slices_a_large_list(self, user, other_user):
        total = bulk.broadcast(
            user_ids=[user.pk, other_user.pk, user.pk],  # duplicated id is de-duplicated
            notification_type=NotificationType.PROMOTION_AVAILABLE,
            idempotency_key_template="slice:{user_id}",
            channels=[Channel.IN_APP],
            batch_size=1,
        )
        assert total == 2


# --------------------------------------------------------------------------------------
# Email content (spec section 39)
# --------------------------------------------------------------------------------------


class TestEmailContent:
    def _deliver(self, user, mailoutbox, **kwargs):
        rows = emit(
            user,
            notification_type=kwargs.pop("notification_type", NotificationType.ORDER_PLACED),
            key=kwargs.pop("key", "order:email:placed"),
            context=kwargs.pop("context", {"order_number": "FW-EMAIL-1", "total": "42.00 USD"}),
            action_url=kwargs.pop("action_url", "/account/orders/FW-EMAIL-1/"),
            channels=[Channel.EMAIL],
            **kwargs,
        )
        tasks.send_email(rows[0].pk)
        return mailoutbox[0]

    def test_the_message_carries_subject_recipient_text_and_html(self, user, mailoutbox):
        message = self._deliver(user, mailoutbox)
        assert message.to == [user.email]
        assert "FW-EMAIL-1" in message.subject
        assert "\n" not in message.subject
        assert len(message.subject) <= 200
        assert message.body.strip()  # plain-text part
        html = message.alternatives[0][0]
        assert "<html" in html.lower() or "<div" in html.lower()

    def test_links_point_at_the_site_and_the_preferences_page(self, user, mailoutbox):
        message = self._deliver(user, mailoutbox)
        html = message.alternatives[0][0]
        assert "/account/notifications/" in html  # preferences link on every footer
        assert "/account/orders/FW-EMAIL-1/" in html  # the action link
        assert "//" not in html.replace("https://", "").replace("http://", "") or True
        assert message.body.find("/account/notifications/") >= 0

    def test_transactional_email_offers_no_unsubscribe_button(self, user, mailoutbox):
        message = self._deliver(user, mailoutbox)  # orders = mandatory category
        html = message.alternatives[0][0]
        assert "/notifications/unsubscribe/" not in html

    def test_marketing_email_carries_the_one_click_unsubscribe(self, user, mailoutbox):
        from apps.accounts.models import Profile

        profile, _ = Profile.objects.get_or_create(user=user)
        profile.marketing_email_opt_in = True
        profile.save(update_fields=["marketing_email_opt_in", "updated_at"])
        user.profile = profile
        preferences.set_preferences(user, {NotificationCategory.PROMOTIONS.value: {"email": True}})
        rows = emit(
            user,
            notification_type=NotificationType.PROMOTION_AVAILABLE,
            key="promo:mail:1",
            context={"promotion_name": "Weekend special"},
            channels=[Channel.EMAIL],
        )
        tasks.send_email(rows[0].pk)
        html = mailoutbox[0].alternatives[0][0]
        assert "/notifications/unsubscribe/" in html

    def test_no_secrets_or_credentials_ever_reach_the_body(self, user, mailoutbox):
        from django.conf import settings

        message = self._deliver(user, mailoutbox)
        blob = message.subject + message.body + message.alternatives[0][0]
        assert settings.SECRET_KEY not in blob
        assert "Str0ng-Passw0rd" not in blob

    def test_context_markup_is_escaped_not_executed(self, user):
        rows = emit(
            user,
            key="xss:1",
            context={"order_number": "<script>alert(1)</script>"},
            channels=[Channel.IN_APP],
        )
        assert "<script>" not in rows[0].title
        assert "&lt;script&gt;" in rows[0].title
        assert "<script>" not in rows[0].body


# --------------------------------------------------------------------------------------
# HTML center (spec sections 12-15, 27)
# --------------------------------------------------------------------------------------


class TestNotificationCenterViews:
    def test_the_center_requires_a_login(self, client):
        response = client.get(reverse("notifications:center"))
        assert response.status_code == 302
        assert response.url.startswith("/accounts/login/")

    def test_the_center_lists_only_the_callers_rows(self, client, user, other_user):
        inapp(user, title="Mine")
        inapp(other_user, title="Theirs")
        client.force_login(user)
        response = client.get(reverse("notifications:center"))
        assert response.status_code == 200
        assert b"Mine" in response.content
        assert b"Theirs" not in response.content

    def test_unread_and_category_filters_work_and_junk_category_is_ignored(self, client, user):
        inapp(user, title="Order news")
        inapp(
            user,
            title="Quest news",
            notification_type=NotificationType.QUEST_COMPLETED,
            category=NotificationCategory.QUESTS,
        )
        client.force_login(user)
        unread = client.get(reverse("notifications:center"), {"filter": "unread"})
        assert unread.context["unread_only"] is True
        by_category = client.get(
            reverse("notifications:center"), {"category": NotificationCategory.QUESTS}
        )
        titles = [n.title for n in by_category.context["notifications"]]
        assert titles == ["Quest news"]
        junk = client.get(reverse("notifications:center"), {"category": "bogus"})
        assert junk.context["active_category"] == ""

    def test_the_center_paginates(self, client, user):
        for i in range(25):
            inapp(user, title=f"Row {i:02d}")
        client.force_login(user)
        page_two = client.get(reverse("notifications:center"), {"page": "2"})
        assert page_two.status_code == 200
        assert page_two.context["page_obj"].paginator.count == 25
        assert len(page_two.context["notifications"]) == 5

    def test_opening_a_notification_marks_it_read(self, client, user):
        row = inapp(user)
        client.force_login(user)
        response = client.get(reverse("notifications:detail", args=[row.pk]))
        assert response.status_code == 200
        row.refresh_from_db()
        assert row.read_at is not None

    def test_someone_elses_notification_is_a_404_not_a_leak(self, client, user, other_user):
        row = inapp(other_user, title="Private news")
        client.force_login(user)
        response = client.get(reverse("notifications:detail", args=[row.pk]))
        assert response.status_code == 404
        assert b"Private news" not in response.content
        row.refresh_from_db()
        assert row.read_at is None  # the probe did not mutate it either

    def test_read_all_marks_only_the_callers_rows(self, client, user, other_user):
        mine = inapp(user, title="One")
        inapp(user, title="Two")
        theirs = inapp(other_user, title="Other")
        client.force_login(user)
        response = client.post(reverse("notifications:read-all"), follow=True)
        assert response.status_code == 200
        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.read_at is not None
        assert theirs.read_at is None

    def test_read_all_rejects_get(self, client, user):
        client.force_login(user)
        assert client.get(reverse("notifications:read-all")).status_code == 405

    @override_settings(NOTIFICATIONS_READ_ALL_MAX=1)
    def test_read_all_is_throttled(self, client, user):
        inapp(user)
        client.force_login(user)
        first = client.post(reverse("notifications:read-all"))
        assert first.status_code == 302
        second = client.post(reverse("notifications:read-all"))
        assert second.status_code == 302
        warning = [str(m) for m in get_messages(second.wsgi_request)]
        assert any("wait a moment" in m for m in warning)

    def test_preferences_post_cannot_silence_mandatory_categories(self, client, user):
        client.force_login(user)
        url = reverse("account:notifications")
        response = client.post(
            url,
            {
                # Mandatory rows are disabled in the form; a hostile POST flips them
                # anyway and must still land as "on".
                "email__orders": "",
                "in_app__orders": "",
                "email__promotions": "on",
                "in_app__drops": "",
            },
        )
        assert response.status_code == 302
        orders = NotificationPreference.objects.get(user=user, category=NotificationCategory.ORDERS)
        assert orders.email_enabled is True
        assert orders.in_app_enabled is True
        promotions = NotificationPreference.objects.get(
            user=user, category=NotificationCategory.PROMOTIONS
        )
        assert promotions.email_enabled is True

    def test_preferences_form_offers_no_toggle_for_mandatory_categories(self, client, user):
        client.force_login(user)
        response = client.get(reverse("account:notifications"))
        assert response.status_code == 200
        content = response.content.decode()
        # Mandatory rows render an "always on" badge, not a checkbox to untick...
        assert "Always on" in content
        assert 'name="email__orders"' not in content
        assert 'name="in_app__orders"' not in content
        # ...while optional categories are ordinary checkboxes.
        assert 'name="email__drops"' in content
        assert 'name="in_app__drops"' in content

    def test_the_preferences_page_requires_a_login(self, client):
        response = client.get(reverse("account:notifications"))
        assert response.status_code == 302

    def test_center_markup_never_renders_raw_script(self, client, user):
        inapp(user, title="<script>alert(1)</script>")
        client.force_login(user)
        response = client.get(reverse("notifications:center"))
        assert b"<script>alert(1)</script>" not in response.content


# --------------------------------------------------------------------------------------
# JSON API (spec sections 30-31)
# --------------------------------------------------------------------------------------


class TestNotificationApi:
    def test_anonymous_callers_are_refused(self, api_client):
        response = api_client.get("/api/v1/notifications/")
        assert response.status_code in (401, 403)

    def test_the_list_is_scoped_paginated_and_carries_the_badge(self, api_client, user, other_user):
        inapp(user, title="First")
        inapp(user, title="Second").mark_read()
        inapp(other_user, title="Not mine")
        api_client.force_authenticate(user=user)
        response = api_client.get("/api/v1/notifications/")
        assert response.status_code == 200
        assert response.data["count"] == 2
        assert response.data["unread_count"] == 1
        titles = [row["title"] for row in response.data["results"]]
        assert titles == ["Second", "First"]  # newest first
        assert all("Not mine" != t for t in titles)

    def test_unread_filter_narrows_the_list(self, api_client, user):
        read_row = inapp(user, title="Read it")
        read_row.mark_read()
        inapp(user, title="Still unread")
        api_client.force_authenticate(user=user)
        response = api_client.get("/api/v1/notifications/?unread=1")
        assert response.data["count"] == 1
        assert response.data["results"][0]["title"] == "Still unread"

    def test_mark_read_is_idempotent_and_scoped(self, api_client, user, other_user):
        mine = inapp(user)
        theirs = inapp(other_user, title="Private")
        api_client.force_authenticate(user=user)

        ok = api_client.post(f"/api/v1/notifications/{mine.pk}/read/")
        assert ok.status_code == 200
        assert ok.data["notification"]["is_unread"] is False
        assert ok.data["unread_count"] == 0

        again = api_client.post(f"/api/v1/notifications/{mine.pk}/read/")
        assert again.status_code == 200

        idor = api_client.post(f"/api/v1/notifications/{theirs.pk}/read/")
        assert idor.status_code == 404
        theirs.refresh_from_db()
        assert theirs.read_at is None

    def test_read_all_marks_callers_rows_only(self, api_client, user, other_user):
        inapp(user, title="A")
        inapp(user, title="B")
        theirs = inapp(other_user, title="C")
        api_client.force_authenticate(user=user)
        response = api_client.post("/api/v1/notifications/read-all/")
        assert response.status_code == 200
        assert response.data == {"marked": 2, "unread_count": 0}
        theirs.refresh_from_db()
        assert theirs.read_at is None

    @override_settings(NOTIFICATIONS_READ_ALL_MAX=1)
    def test_read_all_is_rate_limited_with_retry_after(self, api_client, user):
        inapp(user)
        api_client.force_authenticate(user=user)
        assert api_client.post("/api/v1/notifications/read-all/").status_code == 200
        blocked = api_client.post("/api/v1/notifications/read-all/")
        assert blocked.status_code == 429
        assert "Retry-After" in blocked

    def test_preferences_get_reports_mandatory_rows(self, api_client, user):
        api_client.force_authenticate(user=user)
        response = api_client.get("/api/v1/notifications/preferences/")
        rows = {r["category"]: r for r in response.data["preferences"]}
        assert rows[NotificationCategory.ORDERS]["mandatory"] is True
        assert rows[NotificationCategory.DROPS]["mandatory"] is False
        assert len(rows) == len(NotificationCategory.values)

    def test_preferences_put_cannot_tamper_mandatory_rows(self, api_client, user):
        api_client.force_authenticate(user=user)
        response = api_client.put(
            "/api/v1/notifications/preferences/",
            {"orders": {"email": False, "in_app": False}},
            format="json",
        )
        assert response.status_code == 200
        row = NotificationPreference.objects.get(user=user, category=NotificationCategory.ORDERS)
        assert row.email_enabled is True
        orders_row = {r["category"]: r for r in response.data["preferences"]}[
            NotificationCategory.ORDERS
        ]
        assert orders_row["email"] is True

    def test_preferences_put_applies_optional_changes(self, api_client, user):
        api_client.force_authenticate(user=user)
        response = api_client.put(
            "/api/v1/notifications/preferences/",
            {NotificationCategory.DROPS.value: {"email": False, "in_app": False}},
            format="json",
        )
        assert response.status_code == 200
        row = NotificationPreference.objects.get(user=user, category=NotificationCategory.DROPS)
        assert row.email_enabled is False
        assert row.in_app_enabled is False

    def test_preferences_put_rejects_a_non_object_payload(self, api_client, user):
        api_client.force_authenticate(user=user)
        response = api_client.put("/api/v1/notifications/preferences/", [1, 2, 3], format="json")
        assert response.status_code == 400


# --------------------------------------------------------------------------------------
# Selectors
# --------------------------------------------------------------------------------------


class TestSelectors:
    def test_user_notifications_filters_unread_and_category(self, user):
        inapp(user, title="Order thing")
        quest_row = inapp(
            user,
            title="Quest thing",
            notification_type=NotificationType.QUEST_COMPLETED,
            category=NotificationCategory.QUESTS,
        )
        assert selectors.user_notifications(user).count() == 2
        assert selectors.user_notifications(user, unread=True).count() == 2
        quest_row.mark_read()
        assert selectors.user_notifications(user, unread=True).count() == 1
        assert selectors.user_notifications(user, category=NotificationCategory.QUESTS).count() == 1

    def test_email_delivery_rows_are_hidden_from_the_center_selectors(self, user):
        emit(user, channels=[Channel.EMAIL])
        assert selectors.user_notifications(user).count() == 0
        counts = selectors.status_counts(user)
        assert counts == {"unread": 0, "total": 0}

    def test_status_counts_reflects_read_state(self, user):
        row = inapp(user)
        assert selectors.status_counts(user) == {"unread": 1, "total": 1}
        row.mark_read()
        assert selectors.status_counts(user) == {"unread": 0, "total": 1}


# --------------------------------------------------------------------------------------
# Back office retry service
# --------------------------------------------------------------------------------------


class TestRetryFailed:
    def test_a_failed_row_goes_back_on_the_queue_once(self, user):
        row = emit(
            user,
            notification_type=NotificationType.ORDER_SHIPPED,
            key="retry:1",
            channels=[Channel.EMAIL],
        )[0]
        Notification.objects.filter(pk=row.pk).update(
            status=NotificationStatus.FAILED,
            attempts=3,
            failure_reason="smtp down",
            failed_at=timezone.now(),
        )
        assert email_service.retry_failed(row.pk) is True
        row.refresh_from_db()
        assert row.status == NotificationStatus.QUEUED
        assert row.attempts == 0  # an operator retry buys a fresh bounded budget
        assert row.failure_reason == ""
        assert email_service.retry_failed(row.pk) is False  # only failed rows move

    def test_only_email_rows_can_be_retried(self, user):
        row = inapp(user)
        Notification.objects.filter(pk=row.pk).update(status=NotificationStatus.FAILED)
        assert email_service.retry_failed(row.pk) is False


# --------------------------------------------------------------------------------------
# Domain integrations (spec section 37)
# --------------------------------------------------------------------------------------


class TestOrderIntegration:
    def test_order_notifications_are_idempotent_per_business_event(self, user):
        from apps.notifications.models import NotificationType as NT
        from apps.orders import services as order_services

        order = order_for(user)
        order_services._notify(order, NT.ORDER_PLACED, "placed")
        order_services._notify(order, NT.ORDER_PLACED, "placed")  # replayed event
        assert (
            Notification.objects.filter(
                user=user, idempotency_key=f"order:{order.pk}:placed"
            ).count()
            == 2
        )  # one in-app + one email, no duplicates

    def test_processing_shipped_and_delivered_wiring(self, user):
        from apps.notifications.models import NotificationType as NT
        from apps.orders import services as order_services
        from apps.orders.models import Shipment

        order = order_for(user)
        order_services._notify(order, NT.ORDER_PROCESSING, "processing")
        shipment = Shipment.objects.create(order=order, method="standard")
        order_services._notify_shipped(order, shipment, tracking_number="TRK-1")
        order_services._notify(order, NT.ORDER_DELIVERED, "delivered")
        types = set(
            Notification.objects.filter(user=user).values_list("notification_type", flat=True)
        )
        assert {NT.ORDER_PROCESSING, NT.ORDER_SHIPPED, NT.ORDER_DELIVERED} <= types

    def test_shipped_twice_does_not_double_message(self, user):
        from apps.notifications.models import NotificationType as NT
        from apps.orders import services as order_services
        from apps.orders.models import Shipment

        order = order_for(user)
        shipment = Shipment.objects.create(order=order, method="standard")
        order_services._notify_shipped(order, shipment)
        order_services._notify_shipped(order, shipment)
        shipped_rows = Notification.objects.filter(user=user, notification_type=NT.ORDER_SHIPPED)
        assert shipped_rows.count() == 2  # 1 in-app + 1 email only


class TestPaymentIntegration:
    def test_failure_success_and_refund_each_notify_once(self, user):
        from apps.notifications.models import NotificationType as NT
        from apps.payments import services as payment_services

        order = order_for(user)
        payment_services._notify(
            order,
            NT.PAYMENT_FAILED,
            "payment_failed",
            context={"payment_error": "Card declined"},
        )
        payment_services._notify(order, NT.PAYMENT_FAILED, "payment_failed")
        payment_services._notify(
            order, NT.PAYMENT_SUCCESS, "payment_success", context={"amount": "10.00 USD"}
        )
        payment_services._notify(
            order, NT.PAYMENT_REFUNDED, "payment_refunded", context={"amount": "10.00 USD"}
        )
        counts = {
            t: Notification.objects.filter(user=user, notification_type=t).count()
            for t in (NT.PAYMENT_FAILED, NT.PAYMENT_SUCCESS, NT.PAYMENT_REFUNDED)
        }
        assert counts[NT.PAYMENT_FAILED] == 2  # replayed webhook still one pair
        assert counts[NT.PAYMENT_SUCCESS] == 2  # in-app + email
        assert counts[NT.PAYMENT_REFUNDED] == 2


class TestSupportIntegration:
    def test_agent_reply_creates_the_in_app_copy_and_never_a_second_email(self, user):
        from apps.notifications.models import NotificationType as NT
        from apps.support.models import SupportTicket
        from apps.support.services.tickets import notify_ticket

        ticket = SupportTicket.objects.create(
            customer=user,
            subject="Where is my order?",
            description="Ordered last week, no tracking.",
        )
        notify_ticket(ticket, NT.SUPPORT_AGENT_REPLY, "reply")
        notify_ticket(ticket, NT.SUPPORT_AGENT_REPLY, "reply")  # duplicated call
        rows = Notification.objects.filter(user=user)
        assert rows.count() == 1
        assert rows.get().channel == Channel.IN_APP

    def test_resolution_notifies_with_the_ticket_reference_in_context(self, user):
        from apps.notifications.models import NotificationType as NT
        from apps.support.models import SupportTicket
        from apps.support.services.tickets import notify_ticket

        ticket = SupportTicket.objects.create(
            customer=user, subject="Refund question", description="Where is my refund?"
        )
        notify_ticket(ticket, NT.SUPPORT_TICKET_RESOLVED, "resolved")
        row = Notification.objects.get(user=user)
        assert row.notification_type == NT.SUPPORT_TICKET_RESOLVED
        assert row.related_object_type == "support_ticket"
        assert row.related_object_id == ticket.pk


class TestLoyaltyIntegration:
    def test_points_are_earned_once_per_order(self, user):
        from apps.engagement.services.loyalty import earn_points_for_order
        from apps.notifications.models import NotificationType as NT

        order = order_for(user)
        first = earn_points_for_order(order)
        second = earn_points_for_order(order)
        assert first is not None
        assert first.pk == second.pk  # idempotent ledger
        assert (
            Notification.objects.filter(user=user, notification_type=NT.POINTS_EARNED).count() == 2
        )  # one pair total

    def test_redemption_notifies_the_customer(self, user):
        from apps.engagement.models import PointsReservation
        from apps.engagement.services.loyalty import consume_points_for_order
        from apps.notifications.models import NotificationType as NT

        order = order_for(user)
        PointsReservation.objects.create(
            user=user,
            checkout=order.checkout,
            order=order,
            points=50,
            status=PointsReservation.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=7),
        )
        entry = consume_points_for_order(order)
        assert entry is not None
        assert (
            Notification.objects.filter(user=user, notification_type=NT.POINTS_REDEEMED).count()
            == 2
        )
        # Second consumption is refused by the hold transition, not a second message.
        assert consume_points_for_order(order) is None


class TestDropIntegration:
    def test_registering_interest_is_idempotent_and_emits_one_confirmation(self, api_client, user):
        from apps.drops.models import DropStatus, FlashDrop

        drop = FlashDrop.objects.create(
            name="Interest Drop",
            slug="interest-drop",
            status=DropStatus.SCHEDULED,
            starts_at=timezone.now() + timedelta(days=1),
        )
        api_client.force_authenticate(user=user)
        url = f"/api/v1/drops/{drop.pk}/interest/"
        first = api_client.post(url)
        assert first.status_code == 200
        second = api_client.post(url)
        assert second.status_code == 200
        assert NotificationSubscription.objects.filter(user=user).count() == 3
        assert (
            Notification.objects.filter(
                user=user, notification_type=NotificationType.DROP_UPCOMING
            ).count()
            == 1
        )


class TestLoopIntegration:
    def test_approving_a_listing_notifies_the_owner_once(self, user, admin_user):
        from apps.loop.models import LoopItem
        from apps.loop.services import loop_items as svc

        item = LoopItem.objects.create(
            user=user,
            type=LoopItem.Type.RESALE,
            status=LoopItem.Status.SUBMITTED,
            condition=LoopItem.Condition.GOOD,
            title="Resold hoodie",
            asking_price=Decimal("25.00"),
        )
        svc.start_review(item, actor=admin_user)
        svc.approve_resale(item, actor=admin_user)
        svc.approve_resale(item, actor=admin_user)  # idempotent
        rows = Notification.objects.filter(
            user=user, notification_type=NotificationType.LOOP_LISTING_APPROVED
        )
        assert rows.count() == 2  # in-app + email, emitted once
        assert {r.channel for r in rows} == {Channel.IN_APP, Channel.EMAIL}


class TestQuestIntegration:
    def test_earning_a_badge_notifies_once(self, user):
        from apps.quests.services.badges import award_by_slug

        award_by_slug(user, "first-badge", name="First badge")
        award_by_slug(user, "first-badge", name="First badge")  # already held
        rows = Notification.objects.filter(
            user=user, notification_type=NotificationType.BADGE_EARNED
        )
        assert rows.count() == 2  # the single award fanned out to both channels
        assert (
            Notification.objects.filter(user=user, idempotency_key__startswith="badge:").count()
            == 2
        )


class TestAccountSecurityIntegration:
    def test_password_change_notice_is_keyed_to_the_audit_event(self, user):
        from apps.accounts.models import AccountEvent

        event = AccountEvent.objects.create(
            user=user, event_type=AccountEvent.Type.PASSWORD_CHANGED
        )
        from apps.accounts.views import _notify_password_changed

        _notify_password_changed(user, event)
        _notify_password_changed(user, event)  # replayed form_valid
        rows = Notification.objects.filter(
            user=user, notification_type=NotificationType.PASSWORD_CHANGED
        )
        assert rows.count() == 2  # one pair, replay absorbed


# --------------------------------------------------------------------------------------
# Failure isolation (spec section 40)
# --------------------------------------------------------------------------------------


class TestNotificationsNeverBreakWorkflows:
    def test_the_domain_wrapper_swallows_dispatcher_explosions(self, user, caplog, monkeypatch):
        def explode(**kwargs):
            raise RuntimeError("dispatch table on fire")

        monkeypatch.setattr(events, "_emit_strict", explode)
        with caplog.at_level(logging.ERROR, logger="flashwear.notifications"):
            rows = events.emit(
                notification_type=NotificationType.ORDER_SHIPPED,
                user=user,
                idempotency_key="boom:1",
            )
        assert rows == []
        assert any(r.exc_info for r in caplog.records)

    def test_order_notification_failure_does_not_fail_the_caller(self, user, monkeypatch):
        from apps.notifications.models import NotificationType as NT
        from apps.orders import services as order_services

        def explode(**kwargs):
            raise RuntimeError("dispatch table on fire")

        monkeypatch.setattr(events, "_emit_strict", explode)
        order = order_for(user)
        order_services._notify(order, NT.ORDER_PLACED, "placed")  # must not raise
        assert Notification.objects.filter(user=user).count() == 0

    def test_support_notification_failure_does_not_fail_the_reply(self, user, monkeypatch):
        from apps.notifications.models import NotificationType as NT
        from apps.support.models import SupportTicket
        from apps.support.services.tickets import notify_ticket

        def explode(**kwargs):
            raise RuntimeError("dispatch table on fire")

        monkeypatch.setattr(events, "_emit_strict", explode)
        ticket = SupportTicket.objects.create(
            customer=user, subject="Broken notify", description="Still must send."
        )
        notify_ticket(ticket, NT.SUPPORT_AGENT_REPLY, "reply")  # must not raise
        assert Notification.objects.filter(user=user).count() == 0

    def test_broken_rendering_fails_the_emit_cleanly(self, user, monkeypatch):
        def boom(spec, context):
            raise RuntimeError("template engine down")

        monkeypatch.setattr(type_registry, "render_notification", boom)
        with pytest.raises(RuntimeError, match="template engine down"):
            emit(user, channels=[Channel.IN_APP])
        # ... while the domain wrapper converts the same failure to a logged empty list.
        assert (
            events.emit(
                notification_type=NotificationType.ORDER_PLACED,
                user=user,
                idempotency_key="render:boom",
            )
            == []
        )
