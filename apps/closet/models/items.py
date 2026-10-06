"""The wardrobe: one row per physical garment a customer owns (Phase 8).

Two sources share one shape so filtering, search and outfit building never care where a
piece came from:

* **MANUAL** -- clothing the customer already owns; ``variant`` and ``order_item`` stay empty.
* **PURCHASED** -- a physical unit of a FLASHWEAR variant, created only by the explicit
  "add to closet" action on a *delivered* order. Provenance (``order_item`` + ``unit``) is
  written by the service, never by a payload, and the ``source`` column is
  ``editable=False`` so no form can forge it.

**One row per physical unit.** Two identical shirts are two rows: no ``unique(user, variant)``
collapses ownership, which keeps condition tracking, resale and FLASH Loop possible later.
The duplicate guard lives on ``(order_item, unit)`` instead, so re-clicking "add to closet"
cannot mint a second copy of a unit the customer already has.

Catalogue metadata for a purchased unit (name, brand, colour, size, material) is snapshotted
into the text columns at add time: filters stay uniform across sources, and a wardrobe row
does not drift when the catalogue is re-merchandised. The ``variant`` link stays authoritative
for product imagery and the product page. Image *files* are never copied -- a purchased item
renders the catalogue's own imagery through :attr:`display_image`.
"""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel
from apps.catalog.validators import EXTENSIONS_BY_FORMAT, validate_catalog_image

__all__ = ["ClosetItem", "closet_image_upload_to"]


def closet_image_upload_to(instance: ClosetItem, filename: str) -> str:
    """Safe, collision-proof storage path for a manual wardrobe photo.

    Mirrors the catalogue's upload rule: a random UUID name (the client's filename is
    attacker-controlled) and an extension drawn from the content-verified allow-list, so an
    unrecognised declaration yields a file with **no** extension rather than ``.svg``. The
    bytes themselves were checked by :func:`apps.catalog.validators.validate_catalog_image`
    before this runs.
    """
    from uuid import uuid4

    allowed = {ext for exts in EXTENSIONS_BY_FORMAT.values() for ext in exts}
    _, _, declared = (filename or "").rpartition(".")
    suffix = f".{declared.lower()}" if f".{declared.lower()}" in allowed else ""
    return f"closet/{instance.user_id or 'unassigned'}/{uuid4().hex}{suffix}"


class ClosetItem(TimestampedModel):
    """One physical garment in one customer's private wardrobe."""

    class Source(models.TextChoices):
        """Where the piece came from. Server-assigned; two real workflows, no dead vocabulary."""

        MANUAL = "manual", _("Manual")
        PURCHASED = "purchased", _("Purchased")

    class Category(models.TextChoices):
        """Wardrobe categories -- the outfit vocabulary, deliberately not the catalogue's
        merchandising taxonomy (which is a hierarchical storefront construct)."""

        TOPS = "tops", _("Tops")
        BOTTOMS = "bottoms", _("Bottoms")
        OUTERWEAR = "outerwear", _("Outerwear")
        DRESSES = "dresses", _("Dresses")
        SHOES = "shoes", _("Shoes")
        ACCESSORIES = "accessories", _("Accessories")

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        ARCHIVED = "archived", _("Archived")

    class Season(models.TextChoices):
        """Controlled season vocabulary; blank means "unspecified", not a free-for-all."""

        ALL_SEASON = "all_season", _("All season")
        SPRING = "spring", _("Spring")
        SUMMER = "summer", _("Summer")
        AUTUMN = "autumn", _("Autumn")
        WINTER = "winter", _("Winter")

    class Occasion(models.TextChoices):
        CASUAL = "casual", _("Casual")
        FORMAL = "formal", _("Formal")
        PARTY = "party", _("Party")
        OFFICE = "office", _("Office")
        TRAVEL = "travel", _("Travel")
        STREETWEAR = "streetwear", _("Streetwear")

    class Style(models.TextChoices):
        MINIMAL = "minimal", _("Minimal")
        SMART_CASUAL = "smart_casual", _("Smart casual")
        CLASSIC = "classic", _("Classic")
        RELAXED = "relaxed", _("Relaxed")
        ATHLETIC = "athletic", _("Athletic")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="closet_items",
        verbose_name=_("user"),
        help_text=_("CASCADE: a wardrobe is private account data and leaves with the account."),
    )
    source = models.CharField(
        _("source"),
        max_length=16,
        choices=Source.choices,
        default=Source.MANUAL,
        editable=False,
        help_text=_("Server-assigned: manual, or a delivered order's physical unit."),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="closet_items",
        verbose_name=_("variant"),
        help_text=_("PROTECT: the FLASHWEAR link; empty for manually added clothing."),
    )
    order_item = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="closet_items",
        verbose_name=_("order line"),
        help_text=_(
            "PROTECT: which delivered order line this physical unit came from; "
            "together with the unit number it is the duplicate guard."
        ),
    )
    unit = models.PositiveIntegerField(
        _("unit"),
        default=1,
        editable=False,
        validators=[MinValueValidator(1)],
        help_text=_("Which identical unit of the same order line this row is (1, 2, ...)."),
    )
    category = models.CharField(_("category"), max_length=16, choices=Category.choices)
    name = models.CharField(_("name"), max_length=180)
    brand = models.CharField(_("brand"), max_length=120, blank=True)
    color = models.CharField(_("colour"), max_length=60, blank=True)
    size = models.CharField(_("size"), max_length=32, blank=True)
    material = models.CharField(_("material"), max_length=120, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    season = models.CharField(
        _("season"), max_length=16, choices=Season.choices, blank=True, default=""
    )
    occasion = models.CharField(
        _("occasion"), max_length=16, choices=Occasion.choices, blank=True, default=""
    )
    style = models.CharField(
        _("style"), max_length=16, choices=Style.choices, blank=True, default=""
    )
    image = models.ImageField(
        _("photo"),
        upload_to=closet_image_upload_to,
        blank=True,
        validators=[validate_catalog_image],
        help_text=_(
            "Optional photo for manually added clothing (PNG, JPEG or WebP, within the "
            "catalogue image limits). FLASHWEAR pieces use the product's own imagery."
        ),
    )
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.ACTIVE
    )

    class Meta:
        verbose_name = _("closet item")
        verbose_name_plural = _("closet items")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "status"], name="closet_item_user_status_idx"),
            models.Index(fields=["user", "category"], name="closet_item_user_category_idx"),
            models.Index(fields=["user", "source"], name="closet_item_user_source_idx"),
            models.Index(fields=["user", "-created_at"], name="closet_item_user_created_idx"),
        ]
        constraints = [
            # The duplicate guard: one physical unit per order line, enforced by the database
            # so a double-click (or a race) cannot mint a second copy of unit 1.
            models.UniqueConstraint(
                fields=["order_item", "unit"],
                condition=models.Q(order_item__isnull=False),
                name="closet_item_unit_per_order_line",
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=["manual", "purchased"]),
                name="closet_item_source_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    category__in=["tops", "bottoms", "outerwear", "dresses", "shoes", "accessories"]
                ),
                name="closet_item_category_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["active", "archived"]),
                name="closet_item_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(unit__gte=1),
                name="closet_item_unit_positive",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    # -- presentation ----------------------------------------------------------

    @property
    def is_flashwear(self) -> bool:
        """True when the piece traces back to a FLASHWEAR variant."""
        return self.variant_id is not None

    @property
    def display_image(self):
        """The photo to render: the uploaded one, else the catalogue's primary image.

        Never copies image files (Phase 8 rule): a purchased item borrows the product's own
        imagery, and a missing image is ``None`` for the template to fall back on rather
        than an error.
        """
        if self.image:
            return self.image
        if self.variant_id:
            primary = self.variant.product.primary_image
            if primary is not None:
                return primary.image
        return None

    @property
    def is_in_outfits(self) -> bool:
        """Whether any outfit references this piece (guards hard removal)."""
        return self.worn_in.exists()
