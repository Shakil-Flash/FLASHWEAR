"""Trade-in requests and the loop credit ledger (Phase 13).

Two objects:

* :class:`TradeInRequest` -- the customer's submission and the reviewer's
  workflow around it. The **estimated** credit is a deterministic policy
  output (:mod:`apps.loop.services.valuation`); the **final** credit is
  set by a human reviewer after inspection. They are different numbers
  with different meanings and the template always labels them.
* :class:`LoopCredit` -- an append-only ledger of awarded credits.
  Idempotency comes from the unique ``reference`` column: awarding the
  same completed trade-in twice writes the same reference and the second
  insert is rejected rather than double-counted.

Loop credit is **not** spendable money and is deliberately not FLASH
Points: the two are different economic concepts and mixing them would
contaminate the Phase 7 loyalty ledger. Spending credits is a future
phase behind a proper checkout integration.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel

__all__ = ["LoopCredit", "TradeInRequest"]

ZERO = Decimal("0.00")


class TradeInRequest(TimestampedModel):
    """A customer submitting an owned FLASHWEAR item for trade-in credit."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", _("Submitted")
        UNDER_REVIEW = "under_review", _("Under review")
        OFFERED = "offered", _("Offer made")
        ACCEPTED = "accepted", _("Accepted")
        DECLINED = "declined", _("Declined")
        RECEIVED = "received", _("Received")
        INSPECTED = "inspected", _("Inspected")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")

    loop_item = models.OneToOneField(
        "loop.LoopItem",
        on_delete=models.PROTECT,
        related_name="trade_in_request",
        verbose_name=_("loop item"),
        help_text=_("The trade-in item. One request per item."),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="trade_in_requests",
        verbose_name=_("customer"),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.SUBMITTED,
        db_index=True,
    )

    estimated_credit = models.DecimalField(
        _("estimated credit"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(ZERO)],
        help_text=_("Policy estimate shown to the customer. Not a promise."),
    )
    final_credit = models.DecimalField(
        _("final credit"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(ZERO)],
        help_text=_("Reviewer-set credit awarded on completion."),
    )

    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reviewer"),
    )
    reviewer_note = models.CharField(
        _("reviewer note"),
        max_length=300,
        blank=True,
        help_text=_("Internal note. Never rendered in customer templates."),
    )
    reviewed_at = models.DateTimeField(_("reviewed at"), null=True, blank=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("trade-in request")
        verbose_name_plural = _("trade-in requests")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "status"], name="loop_tradein_user_status_idx"),
            models.Index(fields=["status", "-created_at"], name="loop_tradein_status_idx"),
            models.Index(fields=["loop_item"], name="loop_tradein_item_idx"),
        ]

    def __str__(self) -> str:
        return f"Trade-in #{self.pk} ({self.status})"

    # -- request lifecycle ----------------------------------------------------------

    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        Status.SUBMITTED: (Status.UNDER_REVIEW, Status.CANCELLED),
        Status.UNDER_REVIEW: (Status.OFFERED, Status.DECLINED, Status.CANCELLED),
        Status.OFFERED: (
            Status.ACCEPTED,
            Status.DECLINED,
            Status.UNDER_REVIEW,
            Status.CANCELLED,
        ),
        Status.ACCEPTED: (Status.RECEIVED, Status.DECLINED, Status.CANCELLED),
        Status.DECLINED: (),
        Status.RECEIVED: (Status.INSPECTED, Status.CANCELLED),
        Status.INSPECTED: (Status.COMPLETED, Status.CANCELLED),
        Status.COMPLETED: (),
        Status.CANCELLED: (),
    }

    def can_transition_to(self, target: str) -> bool:
        return target in self.ALLOWED_TRANSITIONS.get(self.status, ())


class LoopCredit(TimestampedModel):
    """One awarded, still-valid loop credit. Append-only.

    The balance is the sum of ``AWARDED`` rows that have not expired;
    there is no cached balance column to drift. ``reference`` is unique
    and deterministic (``trade-in:<request_id>``), which is what makes
    awarding idempotent: a replayed completion writes the same reference
    and the database rejects the duplicate instead of paying twice.
    """

    class Status(models.TextChoices):
        AWARDED = "awarded", _("Awarded")
        EXPIRED = "expired", _("Expired")
        REVOKED = "revoked", _("Revoked")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="loop_credits",
        verbose_name=_("customer"),
    )
    amount = models.DecimalField(
        _("amount"),
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(ZERO)],
        help_text=_("Always positive. Sign lives in the status, not the amount."),
    )
    reference = models.CharField(
        _("reference"),
        max_length=64,
        unique=True,
        help_text=_("Idempotency key, e.g. 'trade-in:42'."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.AWARDED,
        db_index=True,
    )
    loop_item = models.ForeignKey(
        "loop.LoopItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credits",
        verbose_name=_("loop item"),
    )
    trade_in_request = models.ForeignKey(
        TradeInRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credits",
        verbose_name=_("trade-in request"),
    )
    awarded_at = models.DateTimeField(
        _("awarded at"),
        default=timezone.now,
        help_text=_("When the credit was awarded."),
    )
    expires_at = models.DateTimeField(
        _("expires at"),
        null=True,
        blank=True,
        help_text=_("Optional expiry. Blank means the credit does not expire."),
    )

    class Meta:
        verbose_name = _("loop credit")
        verbose_name_plural = _("loop credits")
        ordering = ("-awarded_at",)
        indexes = [
            models.Index(fields=["user", "status"], name="loop_credit_user_status_idx"),
            models.Index(fields=["status", "expires_at"], name="loop_credit_status_expires_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.reference} ({self.amount})"

    @property
    def is_valid(self) -> bool:
        """Whether this credit still counts toward the balance."""
        if self.status != self.Status.AWARDED:
            return False
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return False
        return True
