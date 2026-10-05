"""Stock levels, the append-only movement ledger and checkout reservations.

Three models, one invariant:

* :class:`Stock` -- two counters per variant: ``on_hand`` (the warehouse) and
  ``reserved`` (what validated checkouts are holding). ``available`` is their
  difference, and a check constraint makes ``reserved <= on_hand`` structural, so
  overselling cannot be stored even if application arithmetic were wrong.
* :class:`InventoryMovement` -- every change to either counter, signed and
  referenced. The ledger is evidence: it explains how a counter got to where it is.
* :class:`Reservation` -- one row per held (checkout, variant). Reservations move
  ACTIVE -> RELEASED / EXPIRED / CONSUMED exactly once; the conditional updates in
  :mod:`apps.inventory.services` make replays harmless.

Orders reference reservations (``inventory.Reservation.order``), not the other way
round: :mod:`apps.orders` stays independent of inventory, which keeps the migration
graph a tree.
"""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel

__all__ = ["InventoryMovement", "Reservation", "Stock"]


class Stock(TimestampedModel):
    """Units on hand for one variant and how many are held by checkouts."""

    variant = models.OneToOneField(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="stock",
        verbose_name=_("variant"),
    )
    on_hand = models.PositiveIntegerField(
        _("on hand"),
        default=0,
        help_text=_("Units physically in the warehouse."),
    )
    reserved = models.PositiveIntegerField(
        _("reserved"),
        default=0,
        help_text=_("Units held by active checkout reservations."),
    )

    class Meta:
        verbose_name = _("stock level")
        verbose_name_plural = _("stock levels")
        ordering = ("variant__sku",)
        constraints = [
            models.CheckConstraint(
                condition=Q(reserved__lte=F("on_hand")),
                name="inventory_stock_reserved_within_on_hand",
            ),
        ]

    def __str__(self) -> str:
        return _("%(sku)s: %(available)s available (%(on_hand)s on hand, %(reserved)s held)") % {
            "sku": self.variant.sku,
            "available": self.available,
            "on_hand": self.on_hand,
            "reserved": self.reserved,
        }

    @property
    def available(self) -> int:
        """Units that may still be sold or held."""
        return self.on_hand - self.reserved

    @classmethod
    def get_for_variant(cls, variant) -> Stock:
        """The variant's stock row, created on first use with zero counters."""
        stock, _ = cls.objects.get_or_create(variant=variant)
        return stock


class InventoryMovement(models.Model):
    """Append-only record of one counter change. Never edited, never deleted."""

    class Kind(models.TextChoices):
        RECEIVED = "received", _("Received")
        ADJUSTMENT = "adjustment", _("Adjustment")
        RESERVED = "reserved", _("Reserved")
        RELEASED = "released", _("Released")
        EXPIRED = "expired", _("Hold expired")
        SOLD = "sold", _("Sold")

    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="inventory_movements",
        verbose_name=_("variant"),
    )
    kind = models.CharField(_("kind"), max_length=16, choices=Kind.choices)
    on_hand_delta = models.IntegerField(
        _("on hand delta"),
        default=0,
        help_text=_("Signed change to ``on_hand``; 0 for hold-only movements."),
    )
    reserved_delta = models.IntegerField(
        _("reserved delta"),
        default=0,
        help_text=_("Signed change to ``reserved``; 0 for stock-only movements."),
    )
    reference = models.CharField(
        _("reference"),
        max_length=64,
        blank=True,
        db_index=True,
        help_text=_("What caused this, e.g. 'order:FW-...' or 'reservation:<pk>'."),
    )
    note = models.CharField(_("note"), max_length=200, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("user"),
        help_text=_("The operator behind a manual adjustment, if any."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("inventory movement")
        verbose_name_plural = _("inventory movements")
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(fields=["variant", "created_at"]),
            # The ledger's default view is global (no variant), newest first.
            models.Index(fields=["-created_at"], name="inventory_movement_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.variant.sku} {self.kind} {self.on_hand_delta:+d}/{self.reserved_delta:+d}"


class Reservation(TimestampedModel):
    """A hold on stock for one checkout session and one variant.

    The partial unique constraint allows at most one *active* hold per
    (checkout, variant): re-validating a checkout refreshes its holds rather than
    stacking a second one, and history (released rows) is preserved for the ledger.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        RELEASED = "released", _("Released")
        EXPIRED = "expired", _("Expired")
        CONSUMED = "consumed", _("Consumed")

    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="reservations",
        verbose_name=_("variant"),
    )
    checkout = models.ForeignKey(
        "shop.CheckoutSession",
        on_delete=models.PROTECT,
        related_name="reservations",
        verbose_name=_("checkout"),
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reservations",
        verbose_name=_("order"),
        help_text=_("Set once the hold belongs to a placed order; the sweeper never touches it."),
    )
    quantity = models.PositiveIntegerField(
        _("quantity"),
        validators=[MinValueValidator(1)],
        help_text=_("Units held."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    expires_at = models.DateTimeField(
        _("expires at"),
        help_text=_("After this moment the sweeper may return the hold to the pool."),
    )

    class Meta:
        verbose_name = _("reservation")
        verbose_name_plural = _("reservations")
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["checkout", "variant"],
                condition=Q(status="active"),
                name="inventory_one_active_hold_per_checkout_variant",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="inventory_reservation_quantity_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["order", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.variant.sku} x{self.quantity} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE
