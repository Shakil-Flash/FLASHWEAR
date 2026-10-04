"""Outfit composition: an outfit and the explicit rows that build it (Phase 8).

Composition is a real join table, never an opaque JSON list: the future recommendation
engine reads structure (which slot a piece fills, in what order) straight from the schema,
and the database can protect the references.

``OutfitItem.closet_item`` is **PROTECT**, not CASCADE: an outfit is wardrobe history, so a
garment cannot be hard-deleted out from under it. Removing a piece from the wardrobe while
it is worn by an outfit is refused with a friendly error -- archive the item instead, and
the outfit keeps rendering it (marked archived). Deleting an *outfit* is the mirror case:
its rows go, the garments stay.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel
from apps.closet.models.items import ClosetItem

__all__ = ["Outfit", "OutfitItem"]


class Outfit(TimestampedModel):
    """One customer's named outfit, from a sketch to a saved look."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SAVED = "saved", _("Saved")
        ARCHIVED = "archived", _("Archived")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="outfits",
        verbose_name=_("user"),
        help_text=_("CASCADE: outfits are private account data and leave with the account."),
    )
    name = models.CharField(_("name"), max_length=120)
    description = models.TextField(_("description"), blank=True)
    season = models.CharField(
        _("season"),
        max_length=16,
        choices=ClosetItem.Season.choices,
        blank=True,
        default="",
    )
    occasion = models.CharField(
        _("occasion"),
        max_length=16,
        choices=ClosetItem.Occasion.choices,
        blank=True,
        default="",
    )
    style = models.CharField(
        _("style"),
        max_length=16,
        choices=ClosetItem.Style.choices,
        blank=True,
        default="",
    )
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.DRAFT
    )

    class Meta:
        verbose_name = _("outfit")
        verbose_name_plural = _("outfits")
        ordering = ("-updated_at",)
        indexes = [
            models.Index(fields=["user", "status"], name="outfit_user_status_idx"),
            models.Index(fields=["user", "-updated_at"], name="outfit_user_updated_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["draft", "saved", "archived"]),
                name="outfit_status_valid",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class OutfitItem(models.Model):
    """One garment placed in one outfit, in a role and a position."""

    class Role(models.TextChoices):
        """Structural slots. The future recommender reads these to know that
        TOP+BOTTOM+SHOES and DRESS+SHOES are different shapes of outfit."""

        TOP = "top", _("Top")
        BOTTOM = "bottom", _("Bottom")
        OUTERWEAR = "outerwear", _("Outerwear")
        DRESS = "dress", _("Dress")
        SHOES = "shoes", _("Shoes")
        ACCESSORY = "accessory", _("Accessory")

    outfit = models.ForeignKey(
        Outfit,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("outfit"),
    )
    closet_item = models.ForeignKey(
        "closet.ClosetItem",
        on_delete=models.PROTECT,
        related_name="worn_in",
        verbose_name=_("closet item"),
        help_text=_(
            "PROTECT: outfits are wardrobe history -- a garment worn by an outfit cannot be "
            "hard-deleted; archive it instead."
        ),
    )
    role = models.CharField(_("role"), max_length=16, choices=Role.choices)
    position = models.PositiveIntegerField(
        _("position"),
        default=0,
        help_text=_("Display order inside the outfit; the service keeps positions dense."),
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("outfit item")
        verbose_name_plural = _("outfit items")
        ordering = ("position",)
        indexes = [
            models.Index(fields=["outfit", "position"], name="outfit_item_outfit_pos_idx"),
        ]
        constraints = [
            # No unique(outfit, position): a multi-row reorder would have to pass through
            # duplicate positions mid-transaction without deferrable constraints. Density is
            # the service's job, inside one transaction; this keeps the floor sane.
            models.CheckConstraint(
                condition=models.Q(position__gte=0),
                name="outfit_item_position_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    role__in=["top", "bottom", "outerwear", "dress", "shoes", "accessory"]
                ),
                name="outfit_item_role_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.outfit.name}: {self.closet_item.name} ({self.role})"
