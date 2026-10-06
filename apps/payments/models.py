"""Payment records and the provider event log.

A :class:`Payment` is our side of one charge attempt against one order: how much,
in which currency, through which provider, in which state. The provider's side is
captured verbatim (redacted) in :class:`PaymentEvent`, whose ``(provider,
event_id)`` uniqueness *is* the webhook idempotency guarantee: a redelivered
event inserts nothing and therefore changes nothing.

Two rules the rest of the codebase leans on:

* No card data exists anywhere in this schema (nor can it: the provider never
  sends any). Amounts and statuses only.
* State changes go through :meth:`Payment.transition_to`, which refuses jumps the
  graph does not allow -- a webhook cannot rewind a failed payment into a
  successful one.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel

__all__ = ["Payment", "PaymentEvent", "Refund"]


class Payment(TimestampedModel):
    """One charge attempt for one order."""

    class Status(models.TextChoices):
        CREATED = "created", _("Created")
        PENDING = "pending", _("Pending")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")
        REFUNDED = "refunded", _("Refunded")  # reachable only once refunds exist

    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        Status.CREATED: (Status.PENDING, Status.CANCELLED),
        Status.PENDING: (Status.SUCCEEDED, Status.FAILED, Status.CANCELLED),
        Status.SUCCEEDED: (Status.REFUNDED,),
        Status.FAILED: (),
        Status.CANCELLED: (),
        Status.REFUNDED: (),
    }

    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.PROTECT,
        related_name="payment",
        verbose_name=_("order"),
    )
    provider = models.CharField(
        _("provider"),
        max_length=32,
        help_text=_("Which integration took the money, e.g. 'development'."),
    )
    provider_reference = models.CharField(
        _("provider reference"),
        max_length=64,
        blank=True,
        db_index=True,
        help_text=_("The provider's id for this payment; what webhooks are matched on."),
    )
    amount = models.DecimalField(_("amount"), max_digits=10, decimal_places=2)
    currency = models.CharField(_("currency"), max_length=3)
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.CREATED,
        db_index=True,
    )
    failure_code = models.CharField(_("failure code"), max_length=32, blank=True)
    failure_message = models.CharField(_("failure message"), max_length=200, blank=True)
    paid_at = models.DateTimeField(_("paid at"), null=True, blank=True)

    class Meta:
        verbose_name = _("payment")
        verbose_name_plural = _("payments")
        ordering = ("-created_at",)
        # Every payments screen sorts by created_at; status is already indexed above.
        indexes = [models.Index(fields=["-created_at"], name="payments_payment_created_idx")]

    def __str__(self) -> str:
        return f"{self.order.number} {self.amount} {self.currency} ({self.status})"

    @property
    def is_settled(self) -> bool:
        """True once the attempt has reached a final state."""
        return self.status in (self.Status.SUCCEEDED, self.Status.FAILED, self.Status.CANCELLED)

    def can_transition_to(self, target: str) -> bool:
        return target in self.ALLOWED_TRANSITIONS.get(self.status, set())

    def transition_to(self, target: str, *, save: bool = True, **timestamps) -> Payment:
        """Move to ``target`` (no event is written here -- see services).

        Raises:
            ValueError: the transition is not in the graph.
        """
        if target == self.status:
            return self
        if not self.can_transition_to(target):
            raise ValueError(f"payment cannot go from {self.status} to {target}")
        self.status = target
        if target == self.Status.SUCCEEDED and self.paid_at is None:
            self.paid_at = timezone.now()
        if save:
            self.save(update_fields=["status", "paid_at", "updated_at"])
        return self


class PaymentEvent(models.Model):
    """One provider webhook (or simulated provider event), stored verbatim.

    ``payload`` holds only the fields our integration parses -- the dev provider
    never sends card data, and a real one must be configured to send tokens, not
    PANs.
    """

    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_("payment"),
    )
    provider = models.CharField(_("provider"), max_length=32)
    event_id = models.CharField(
        _("event id"),
        max_length=64,
        help_text=_("Provider-assigned id; the idempotency key."),
    )
    event_type = models.CharField(_("event type"), max_length=16, choices=Payment.Status.choices)
    payload = models.JSONField(_("payload"), default=dict, blank=True)
    received_at = models.DateTimeField(_("received at"), auto_now_add=True)

    class Meta:
        verbose_name = _("payment event")
        verbose_name_plural = _("payment events")
        ordering = ("-received_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_id"],
                name="payments_one_event_per_provider_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_id} -> {self.event_type}"


def payment_amount_matches(payment: Payment, amount: Decimal) -> bool:
    """A provider event for a different amount is not this payment's business."""
    return payment.amount == amount


class Refund(TimestampedModel):
    """One refund transaction issued against a payment.

    Amounts and statuses only -- card/credential data is never stored.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    class Reason(models.TextChoices):
        RETURN = "return", _("Return")
        DEFECTIVE = "defective", _("Defective item")
        EXCHANGE_DIFFERENCE = "exchange_difference", _("Exchange price difference")
        CANCELLATION = "cancellation", _("Order cancellation")
        GOODWILL = "goodwill", _("Customer goodwill")
        SHIPPING = "shipping", _("Shipping fee")
        OTHER = "other", _("Other")

    number = models.CharField(
        _("refund number"),
        max_length=32,
        unique=True,
        db_index=True,
        help_text=_("Public handle: REF-<date>-<random>."),
    )
    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        related_name="refunds",
        verbose_name=_("payment"),
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.PROTECT,
        related_name="refunds",
        verbose_name=_("order"),
    )
    return_request = models.ForeignKey(
        "orders.ReturnRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refunds",
        verbose_name=_("return request"),
    )
    amount = models.DecimalField(_("amount"), max_digits=10, decimal_places=2)
    currency = models.CharField(_("currency"), max_length=3)
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    reason = models.CharField(
        _("reason"),
        max_length=32,
        choices=Reason.choices,
        default=Reason.RETURN,
    )
    provider = models.CharField(_("provider"), max_length=32)
    provider_reference = models.CharField(
        _("provider reference"),
        max_length=64,
        blank=True,
        db_index=True,
        help_text=_("The provider's id for this refund."),
    )
    is_shipping_refunded = models.BooleanField(_("shipping refunded"), default=False)
    note = models.CharField(_("note"), max_length=300, blank=True)
    failure_code = models.CharField(_("failure code"), max_length=32, blank=True)
    failure_message = models.CharField(_("failure message"), max_length=200, blank=True)
    processed_at = models.DateTimeField(_("processed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("refund")
        verbose_name_plural = _("refunds")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["order", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.number} ({self.amount} {self.currency}) -> {self.status}"
