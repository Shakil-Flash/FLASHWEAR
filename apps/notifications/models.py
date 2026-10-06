"""Notification models (Phase 17): the authoritative record of every message sent.

Four tables, each with one job:

* :class:`Notification` -- one row per (recipient, business event, channel). The row *is*
  the delivery record: status, attempts and failure live here, so the back office reads the
  truth rather than a log line. Uniqueness on ``(user, idempotency_key, channel)`` is what
  makes a retried webhook or a replayed task unable to duplicate a customer's message --
  the constraint is enforced by the database, so two concurrent dispatches race to one row.
* :class:`NotificationPreference` -- one row per (recipient, category), created lazily with
  the category defaults from the type registry. Mandatory categories (order, payment,
  delivery, support, account security) are ignored by the dispatcher, not merely defaulted:
  a tampered POST can flip the stored boolean but the message still goes out.
* :class:`NotificationSubscription` -- "tell me about this object" (FLASH Drop interest).
  Generic on purpose: the notification app must not model every domain it watches.
* :class:`UnsubscribeToken` -- an opaque, random, single-use-per-user handle for one-click
  marketing unsubscribe. Deliberately *not* the user id in base64: the URL carries no
  information about who it belongs to until the database resolves it.

Everything is timezone-aware (``timezone.now`` via ``auto_now`` fields) and append-friendly:
``metadata`` holds the explicit, already-safe template context a re-render needs (order
number, totals) and never credentials, tokens or raw payment details.
"""

from __future__ import annotations

import secrets
from typing import Any

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

__all__ = [
    "Channel",
    "Notification",
    "NotificationCategory",
    "NotificationPreference",
    "NotificationStatus",
    "NotificationSubscription",
    "NotificationType",
    "Priority",
    "UnsubscribeToken",
]

internal_path = RegexValidator(
    r"^/(?!/)[^?\s]*$",
    _("Action URLs must be site-relative paths like /orders/1/."),
)


def _generate_token() -> str:
    """256 bits of randomness: unguessable, and carrying no user data by construction."""
    return secrets.token_urlsafe(32)


class Channel(models.TextChoices):
    """Where a notification is delivered. Initial set per Phase 17 §3; extensible."""

    IN_APP = "in_app", _("In app")
    EMAIL = "email", _("Email")


class NotificationStatus(models.TextChoices):
    """Delivery lifecycle (Phase 17 §6).

    ``PENDING`` rows are created for future-scheduled sends; ``QUEUED`` means a channel
    sender owns the row; ``SENT`` means the provider *accepted* it -- never "the customer
    read it". ``DELIVERED`` is reserved for providers that actually report delivery (none
    of ours do, so nothing ever sets it: a fake delivery state would be a lie).
    """

    PENDING = "pending", _("Pending")
    QUEUED = "queued", _("Queued")
    SENT = "sent", _("Sent")
    DELIVERED = "delivered", _("Delivered")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")


class Priority(models.TextChoices):
    LOW = "low", _("Low")
    NORMAL = "normal", _("Normal")
    HIGH = "high", _("High")


class NotificationCategory(models.TextChoices):
    """Preference groups. A notification type maps to exactly one category."""

    ACCOUNT = "account", _("Account & security")
    ORDERS = "orders", _("Order updates")
    PAYMENTS = "payments", _("Payment updates")
    DELIVERY = "delivery", _("Delivery updates")
    SUPPORT = "support", _("Support responses")
    LOYALTY = "loyalty", _("FLASH Points")
    PROMOTIONS = "promotions", _("Promotions")
    DROPS = "drops", _("FLASH Drops")
    CREATOR = "creator", _("Creator")
    LOOP = "loop", _("FLASH Loop")
    QUESTS = "quests", _("Quests & badges")
    RECOMMENDATIONS = "recommendations", _("Recommendations")


class NotificationType(models.TextChoices):
    """Controlled vocabulary (Phase 17 §4). Values are stable; labels may be reworded."""

    # Account
    ACCOUNT_CREATED = "account_created", _("Account created")
    PASSWORD_CHANGED = "password_changed", _("Password changed")
    EMAIL_CHANGED = "email_changed", _("Email changed")
    LOGIN_SECURITY = "login_security", _("Login security")
    # Orders
    ORDER_PLACED = "order_placed", _("Order placed")
    ORDER_CONFIRMED = "order_confirmed", _("Order confirmed")
    ORDER_PROCESSING = "order_processing", _("Order processing")
    ORDER_SHIPPED = "order_shipped", _("Order shipped")
    ORDER_DELIVERED = "order_delivered", _("Order delivered")
    ORDER_CANCELLED = "order_cancelled", _("Order cancelled")
    # Returns & Exchanges (Phase 23)
    RETURN_REQUESTED = "return_requested", _("Return requested")
    RETURN_APPROVED = "return_approved", _("Return approved")
    RETURN_REJECTED = "return_rejected", _("Return rejected")
    RETURN_RECEIVED = "return_received", _("Return received")
    INSPECTION_COMPLETED = "inspection_completed", _("Return inspected")
    EXCHANGE_PROCESSED = "exchange_processed", _("Exchange processed")
    # Payments
    PAYMENT_PENDING = "payment_pending", _("Payment pending")
    PAYMENT_SUCCESS = "payment_success", _("Payment successful")
    PAYMENT_FAILED = "payment_failed", _("Payment failed")
    PAYMENT_REFUNDED = "payment_refunded", _("Payment refunded")
    REFUND_INITIATED = "refund_initiated", _("Refund initiated")
    REFUND_COMPLETED = "refund_completed", _("Refund completed")
    REFUND_FAILED = "refund_failed", _("Refund failed")
    # Delivery (carrier-level events; customer-facing order milestones use ORDER_*)
    SHIPMENT_CREATED = "shipment_created", _("Shipment created")
    SHIPMENT_SHIPPED = "shipment_shipped", _("Shipment shipped")
    SHIPMENT_DELAYED = "shipment_delayed", _("Shipment delayed")
    SHIPMENT_DELIVERED = "shipment_delivered", _("Shipment delivered")
    # Support
    SUPPORT_TICKET_CREATED = "support_ticket_created", _("Support ticket created")
    SUPPORT_AGENT_REPLY = "support_agent_reply", _("Support agent reply")
    SUPPORT_TICKET_UPDATED = "support_ticket_updated", _("Support ticket updated")
    SUPPORT_TICKET_RESOLVED = "support_ticket_resolved", _("Support ticket resolved")
    # Loyalty
    POINTS_EARNED = "points_earned", _("FLASH Points earned")
    POINTS_REDEEMED = "points_redeemed", _("FLASH Points redeemed")
    POINTS_EXPIRING = "points_expiring", _("FLASH Points expiring")
    # Promotions
    PROMOTION_AVAILABLE = "promotion_available", _("Promotion available")
    PROMOTION_EXPIRING = "promotion_expiring", _("Promotion expiring")
    # Drops
    DROP_UPCOMING = "drop_upcoming", _("Drop upcoming")
    DROP_LIVE = "drop_live", _("Drop live")
    DROP_ENDED = "drop_ended", _("Drop ended")
    # Creator
    CREATOR_APPLICATION_UPDATED = "creator_application_updated", _("Creator application updated")
    CREATOR_POST_MODERATION = "creator_post_moderation", _("Creator post moderation")
    CREATOR_POST_FEATURED = "creator_post_featured", _("Creator post featured")
    # FLASH Loop
    LOOP_LISTING_APPROVED = "loop_listing_approved", _("Listing approved")
    LOOP_LISTING_REJECTED = "loop_listing_rejected", _("Listing rejected")
    TRADE_IN_UPDATED = "trade_in_updated", _("Trade-in updated")
    RECYCLING_UPDATED = "recycling_updated", _("Recycling updated")
    # Quests
    QUEST_COMPLETED = "quest_completed", _("Quest completed")
    QUEST_REWARD_GRANTED = "quest_reward_granted", _("Quest reward granted")
    BADGE_EARNED = "badge_earned", _("Badge earned")


class NotificationQuerySet(models.QuerySet):
    def unread(self):
        return self.filter(read_at__isnull=True)


class Notification(models.Model):
    """One delivery of one business event to one recipient on one channel."""

    STATUS_TRANSITIONS = {
        NotificationStatus.PENDING: {
            NotificationStatus.QUEUED,
            NotificationStatus.CANCELLED,
        },
        NotificationStatus.QUEUED: {
            NotificationStatus.SENT,
            NotificationStatus.FAILED,
            NotificationStatus.CANCELLED,
        },
        NotificationStatus.SENT: {NotificationStatus.DELIVERED},
        NotificationStatus.FAILED: {NotificationStatus.QUEUED},  # a staff retry
        NotificationStatus.DELIVERED: set(),
        NotificationStatus.CANCELLED: set(),
    }

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name=_("user"),
    )
    notification_type = models.CharField(
        _("notification type"),
        max_length=40,
        choices=NotificationType.choices,
        db_index=True,
    )
    category = models.CharField(
        _("category"),
        max_length=24,
        choices=NotificationCategory.choices,
        help_text=_("Denormalised from the type registry so preference filters never join."),
    )
    channel = models.CharField(_("channel"), max_length=16, choices=Channel.choices)
    title = models.CharField(_("title"), max_length=200)
    body = models.TextField(_("body"), blank=True)
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=NotificationStatus.choices,
        default=NotificationStatus.PENDING,
        db_index=True,
    )
    priority = models.CharField(
        _("priority"), max_length=8, choices=Priority.choices, default=Priority.NORMAL
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)
    scheduled_for = models.DateTimeField(
        _("scheduled for"),
        null=True,
        blank=True,
        help_text=_("Future delivery time; the queue sweeper promotes the row when due."),
    )
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    read_at = models.DateTimeField(_("read at"), null=True, blank=True)
    failed_at = models.DateTimeField(_("failed at"), null=True, blank=True)
    failure_reason = models.CharField(_("failure reason"), max_length=300, blank=True)
    attempts = models.PositiveSmallIntegerField(
        _("attempts"),
        default=0,
        help_text=_("Channel-sender attempts; doubles as the delivery claim (CAS)."),
    )
    related_object_type = models.CharField(
        _("related object type"),
        max_length=32,
        blank=True,
        help_text=_("Domain noun, e.g. ``order``. Never a raw foreign key."),
    )
    related_object_id = models.PositiveIntegerField(_("related object id"), null=True, blank=True)
    action_url = models.CharField(
        _("action URL"),
        max_length=300,
        blank=True,
        validators=[internal_path],
        help_text=_("Site-relative link to the surface this notification points at."),
    )
    metadata = models.JSONField(
        _("metadata"),
        default=dict,
        blank=True,
        help_text=_("Explicit, non-sensitive template context (order number, totals)."),
    )
    idempotency_key = models.CharField(
        _("idempotency key"),
        max_length=140,
        blank=True,
        default="",
        help_text=_("Deterministic business-event key, e.g. order:12:payment_success."),
    )

    objects = NotificationQuerySet.as_manager()

    class Meta:
        verbose_name = _("notification")
        verbose_name_plural = _("notifications")
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["user", "-created_at"], name="notif_user_created_idx"),
            models.Index(fields=["user", "read_at"], name="notif_user_read_idx"),
            models.Index(fields=["notification_type"], name="notif_type_idx"),
            models.Index(fields=["channel", "status"], name="notif_channel_status_idx"),
            models.Index(fields=["status", "-created_at"], name="notif_status_created_idx"),
        ]
        constraints = [
            # Race-safe deduplication: two concurrent dispatches of the same business event
            # on the same channel cannot both insert (Phase 17 §32).
            models.UniqueConstraint(
                fields=["user", "idempotency_key", "channel"],
                condition=~models.Q(idempotency_key=""),
                name="notif_user_key_channel_uniq",
            ),
            models.CheckConstraint(
                condition=~models.Q(action_url__startswith="//")
                & ~models.Q(action_url__startswith="http")
                & ~models.Q(action_url__startswith="mailto:"),
                name="notif_action_url_internal",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.notification_type} -> {self.user_id} [{self.channel}/{self.status}]"

    # -- state ---------------------------------------------------------------

    def can_transition_to(self, status: str) -> bool:
        return status in self.STATUS_TRANSITIONS.get(self.status, set())

    def transition_to(self, status: str, *, failure_reason: str = "") -> Notification:
        """Move to ``status`` or raise. One place decides what a state change means."""
        if not self.can_transition_to(status):
            raise InvalidNotificationTransition(self.status, status)
        self.status = status
        now = timezone.now()
        if status in (NotificationStatus.QUEUED, NotificationStatus.PENDING):
            self.failure_reason = ""
            self.failed_at = None
        if status in (NotificationStatus.SENT, NotificationStatus.DELIVERED):
            self.sent_at = self.sent_at or now
        if status == NotificationStatus.FAILED:
            self.failed_at = now
            self.failure_reason = (failure_reason or "Delivery failed.")[:300]
        self.save(
            update_fields=[
                "status",
                "failure_reason",
                "failed_at",
                "sent_at",
                "updated_at",
            ]
        )
        return self

    # -- read state ----------------------------------------------------------

    @property
    def is_unread(self) -> bool:
        return self.read_at is None

    def mark_read(self) -> Notification:
        """Idempotent: a second mark keeps the original timestamp."""
        if self.read_at is None:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at", "updated_at"])
        return self

    @property
    def safe_metadata(self) -> dict[str, Any]:
        """Metadata as a plain dict (JSONField already stores primitives only)."""
        return dict(self.metadata or {})


class InvalidNotificationTransition(Exception):
    """Raised when a delivery status change is illegal (programmer error, not user error)."""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"Notification cannot go from {current!r} to {target!r}.")
        self.current = current
        self.target = target


class NotificationPreference(models.Model):
    """Per-recipient, per-category channel switches (Phase 17 §14).

    Rows are created lazily from the registry defaults, so a customer who never opens the
    preferences screen still behaves as designed. ``mandatory`` lives in code (the type
    registry), not here: a row can be edited by a buggy form or a hostile request and the
    dispatcher still refuses to suppress a mandatory category.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
        verbose_name=_("user"),
    )
    category = models.CharField(_("category"), max_length=24, choices=NotificationCategory.choices)
    email_enabled = models.BooleanField(_("email enabled"), default=True)
    in_app_enabled = models.BooleanField(_("in-app enabled"), default=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("notification preference")
        verbose_name_plural = _("notification preferences")
        ordering = ("category",)
        constraints = [
            models.UniqueConstraint(
                fields=["user", "category"], name="notif_pref_user_category_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category} prefs for user {self.user_id}"


class NotificationSubscription(models.Model):
    """A standing "tell me about this object" watch (Phase 17: FLASH Drop interest).

    Generic (type + id) because the notification app must not import the domains it
    watches; the sweep task resolves the referenced rows through the domain's own models.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_subscriptions",
        verbose_name=_("user"),
    )
    notification_type = models.CharField(
        _("topic"), max_length=40, choices=NotificationType.choices
    )
    related_object_type = models.CharField(_("related object type"), max_length=32)
    related_object_id = models.PositiveIntegerField(_("related object id"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("notification subscription")
        verbose_name_plural = _("notification subscriptions")
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "user",
                    "notification_type",
                    "related_object_type",
                    "related_object_id",
                ],
                name="notif_sub_user_topic_uniq",
            ),
        ]

    def __str__(self) -> str:
        target = f"{self.related_object_type}:{self.related_object_id}"
        return f"{self.user_id} -> {self.notification_type} ({target})"


class UnsubscribeToken(models.Model):
    """Opaque one-click unsubscribe handle for marketing email (Phase 17 §16).

    Random, stored server-side, unique-indexed: the URL contains nothing that identifies
    the recipient, tampering reduces to guessing 256 bits, and a lookup (not a decode)
    decides who it belongs to. Transactional categories have no token on purpose -- they
    are not unsubscribable.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_unsubscribe_token",
        verbose_name=_("user"),
    )
    token = models.CharField(
        _("token"),
        max_length=64,
        unique=True,
        default=_generate_token,
        editable=False,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("unsubscribe token")
        verbose_name_plural = _("unsubscribe tokens")

    def __str__(self) -> str:
        return f"unsubscribe token for user {self.user_id}"
