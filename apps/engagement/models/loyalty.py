"""FLASH Points: the ledger and its reservations (Phase 7).

Two objects, deliberately mirroring the Phase 6 stock pair (``StockMovement`` /
``Reservation``):

* :class:`PointsTransaction` -- the **append-only ledger**. The balance is the sum of its signed
  amounts minus what is currently reserved; there is no cached balance column to drift.
  Idempotency comes from the unique ``(reference, transaction_type)`` pair: a webhook replay or a
  double-connected sweeper writes the same reference and the second insert is rejected, not
  double-counted.
* :class:`PointsReservation` -- points a checkout is holding so a second checkout cannot spend
  them. ``ACTIVE -> RELEASED / EXPIRED / CONSUMED`` exactly once via conditional ``UPDATE``
  statements (see ``apps.engagement.services.loyalty``), exactly like stock holds.

Reservation rules worth knowing:

* A reservation attached to a **placed order is never swept** -- the same rule stock holds follow.
  It is released when the order is cancelled and consumed when the payment succeeds.
* Checkout-only reservations expire on their own timer, so an abandoned checkout returns the
  points to the pool.

Sign convention per type is enforced by constraints: earns are positive, redemptions and
expirations negative, adjustments may go either way, and nothing is ever zero.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel


class PointsTransaction(TimestampedModel):
    """One movement against a customer's FLASH Points balance."""

    class TransactionType(models.TextChoices):
        PURCHASE_EARN = "purchase_earn", _("Purchase earn")
        BONUS = "bonus", _("Bonus")
        REDEMPTION = "redemption", _("Redemption")
        REFUND_REVERSAL = "refund_reversal", _("Refund reversal")
        ADMIN_ADJUSTMENT = "admin_adjustment", _("Admin adjustment")
        EXPIRATION = "expiration", _("Expiration")

    POSITIVE_TYPES = (TransactionType.PURCHASE_EARN, TransactionType.BONUS)
    NEGATIVE_TYPES = (
        TransactionType.REDEMPTION,
        TransactionType.REFUND_REVERSAL,
        TransactionType.EXPIRATION,
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="points_transactions",
        verbose_name=_("user"),
    )
    amount = models.IntegerField(
        _("amount"),
        help_text=_("Signed: positive credits the balance, negative debits it."),
    )
    transaction_type = models.CharField(
        _("type"),
        max_length=24,
        choices=TransactionType.choices,
    )
    reference = models.CharField(
        _("reference"),
        max_length=64,
        help_text=_(
            "What caused this movement, e.g. 'order:FW-20260101-AB12CD34' or "
            "'expiry:42'. Unique with the type, which is what makes awards and "
            "redemptions idempotent."
        ),
    )
    expires_at = models.DateTimeField(
        _("expires at"),
        null=True,
        blank=True,
        help_text=_(
            "Set on earning rows only: the sweeper writes an offsetting EXPIRATION "
            "movement after this moment. NULL means the points never expire."
        ),
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="points_transactions",
        verbose_name=_("order"),
        help_text=_("Set when an order caused the movement."),
    )
    note = models.CharField(
        _("note"),
        max_length=200,
        blank=True,
        help_text=_("Optional operator context for adjustments and reversals."),
    )

    class Meta:
        verbose_name = _("points transaction")
        verbose_name_plural = _("points transactions")
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["reference", "transaction_type"],
                name="engagement_points_reference_type_unique",
            ),
            models.CheckConstraint(
                condition=~models.Q(reference=""),
                name="engagement_points_reference_required",
            ),
            models.CheckConstraint(
                condition=~models.Q(amount=0),
                name="engagement_points_amount_nonzero",
            ),
            models.CheckConstraint(
                condition=~models.Q(transaction_type__in=["purchase_earn", "bonus"])
                | models.Q(amount__gt=0),
                name="engagement_points_earn_types_positive",
            ),
            models.CheckConstraint(
                condition=~models.Q(
                    transaction_type__in=["redemption", "refund_reversal", "expiration"]
                )
                | models.Q(amount__lt=0),
                name="engagement_points_spend_types_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="engagement_points_user_idx"),
            models.Index(fields=["expires_at"], name="engagement_points_expiry_idx"),
            models.Index(fields=["order"], name="engagement_points_order_idx"),
            # The all-users points ledger: equality on transaction_type, a date window
            # and "-created_at" ordering, with no user in the predicate (so the
            # user-prefixed index cannot serve it). Equality-first also orders the
            # unfiltered view for free -- both readings use the same index.
            models.Index(
                fields=["transaction_type", "-created_at"],
                name="engagement_points_type_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} {self.amount:+d} ({self.transaction_type}) @{self.reference}"


class PointsReservation(TimestampedModel):
    """Points held for one checkout session so no other checkout can spend them."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        RELEASED = "released", _("Released")
        EXPIRED = "expired", _("Expired")
        CONSUMED = "consumed", _("Consumed")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="points_reservations",
        verbose_name=_("user"),
    )
    checkout = models.ForeignKey(
        "shop.CheckoutSession",
        on_delete=models.PROTECT,
        related_name="points_reservations",
        verbose_name=_("checkout"),
        help_text=_("The checkout that requested the hold."),
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="points_reservations",
        verbose_name=_("order"),
        help_text=_(
            "Set once the hold belongs to a placed order; the sweeper then never "
            "touches it -- it is released on cancellation or consumed on payment."
        ),
    )
    points = models.PositiveIntegerField(
        _("points"),
        help_text=_("Whole points held."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    expires_at = models.DateTimeField(
        _("expires at"),
        help_text=_(
            "Checkout-only holds: after this moment the sweeper may return the points "
            "to the pool. Ignored once the hold is attached to an order."
        ),
    )

    class Meta:
        verbose_name = _("points reservation")
        verbose_name_plural = _("points reservations")
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["checkout"],
                condition=models.Q(status="active"),
                name="engagement_one_active_points_hold_per_checkout",
            ),
            models.CheckConstraint(
                condition=models.Q(points__gt=0),
                name="engagement_points_reservation_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="engagement_points_hold_idx"),
            models.Index(fields=["order", "status"], name="engagement_hold_order_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.points} pts ({self.status}) for {self.checkout_id}"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE
