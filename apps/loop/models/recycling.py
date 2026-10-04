"""Recycling requests (Phase 13).

A :class:`RecycleRequest` moves an eligible item out of circulation
entirely. There is deliberately **no** monetary reward in this phase, and
no automated claim about environmental impact: the ``impact`` JSON field
is written by staff/system logic only (never by the customer) and holds
only facts the platform can back, such as the material category the item
was sorted into. Fabricated CO₂ numbers are a future, documented
calculation, not a v1 feature.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel

__all__ = ["RecycleRequest"]


class RecycleRequest(TimestampedModel):
    """A customer submitting an eligible item to the recycling programme."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", _("Submitted")
        ACCEPTED = "accepted", _("Accepted")
        RECEIVED = "received", _("Received")
        PROCESSED = "processed", _("Processed")
        REJECTED = "rejected", _("Rejected")
        CANCELLED = "cancelled", _("Cancelled")

    loop_item = models.OneToOneField(
        "loop.LoopItem",
        on_delete=models.PROTECT,
        related_name="recycle_request",
        verbose_name=_("loop item"),
        help_text=_("The recycling item. One request per item."),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recycle_requests",
        verbose_name=_("customer"),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.SUBMITTED,
        db_index=True,
    )
    instructions = models.TextField(
        _("instructions"),
        blank=True,
        help_text=_("Plain-text drop-off or packaging notes for the customer."),
    )
    material_category = models.CharField(
        _("material category"),
        max_length=120,
        blank=True,
        help_text=_("Material the item was sorted into. Set at processing."),
    )
    impact = models.JSONField(
        _("environmental impact"),
        default=dict,
        blank=True,
        help_text=_("Staff-written facts only (e.g. sorting outcome). No fabricated claims."),
    )
    received_at = models.DateTimeField(_("received at"), null=True, blank=True)
    processed_at = models.DateTimeField(_("processed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("recycle request")
        verbose_name_plural = _("recycle requests")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "status"], name="loop_recycle_user_status_idx"),
            models.Index(fields=["status", "-created_at"], name="loop_recycle_status_idx"),
            models.Index(fields=["loop_item"], name="loop_recycle_item_idx"),
        ]

    def __str__(self) -> str:
        return f"Recycle #{self.pk} ({self.status})"

    # -- request lifecycle ----------------------------------------------------------

    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        Status.SUBMITTED: (Status.ACCEPTED, Status.REJECTED, Status.CANCELLED),
        Status.ACCEPTED: (Status.RECEIVED, Status.CANCELLED),
        Status.RECEIVED: (Status.PROCESSED, Status.CANCELLED),
        Status.PROCESSED: (),
        Status.REJECTED: (),
        Status.CANCELLED: (),
    }

    def can_transition_to(self, target: str) -> bool:
        return target in self.ALLOWED_TRANSITIONS.get(self.status, ())
