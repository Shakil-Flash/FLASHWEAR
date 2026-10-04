"""Fashion attributes: the reusable vocabularies a product is described with.

Four vocabularies exist because a shopper filters on them, and each one has a distinct shape:

``Size``
    Needs a *type* as well as a label, because "M" is meaningless without knowing whether it is a
    t-shirt, a waist measurement or a shoe. Sizes are rows, never a hard-coded list in code: a
    product may be sized XS-XXL, EU 40-45, "One size", or something a brand invents, and the
    storefront has to be able to render all of them without a deploy.

``Color``
    A swatch needs a hex value, but the hex is presentation only. Identity is the row (and its
    slug), never the value, so a brand can re-tune "Black" from ``#000000`` to ``#111111``
    without orphaning variants.

``Material`` / ``Fit``
    Small controlled vocabularies. They are rows rather than free text because Phase 3 does not
    build filtering, but Phase 4 search and the later FLASH DNA work need them relational: a
    recommendation cannot join against a comma-separated string.

Every model here inherits :class:`~apps.catalog.models.base.SluggedModel`, so each gets a unique
slug, an active flag, a display order and optional SEO metadata.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import SluggedModel


def _relative_luminance(red: int, green: int, blue: int) -> float:
    """WCAG 2.1 relative luminance of an 8-bit colour."""
    total = 0.0
    for value, weight in ((red, 0.2126), (green, 0.7152), (blue, 0.0722)):
        channel = value / 255
        if channel <= 0.03928:
            linear = channel / 12.92
        else:
            linear = ((channel + 0.055) / 1.055) ** 2.4
        total += weight * linear
    return total


class Size(SluggedModel):
    """One entry in a sizing system.

    ``name`` is what the customer reads ("Medium", "EU 42", "One size"); ``code`` is the compact
    token a SKU is built from ("M", "42", "OS"). Uniqueness is per ``size_type``, because "42" as a
    waist and "42" as a shoe are different rows that must not merge into one.
    """

    class SizeType(models.TextChoices):
        CLOTHING = "clothing", _("Clothing (XS-XXL)")
        NUMERIC = "numeric", _("Numeric (waist, chest)")
        SHOE = "shoe", _("Shoe (EU, UK, US)")
        ONE_SIZE = "one_size", _("One size")
        CUSTOM = "custom", _("Brand-specific")

    code = models.CharField(
        _("code"),
        max_length=32,
        help_text=_("Compact token used in SKUs, e.g. M, 42, OS."),
    )
    size_type = models.CharField(
        _("size type"),
        max_length=16,
        choices=SizeType.choices,
        default=SizeType.CLOTHING,
        help_text=_("Which sizing system this code belongs to."),
    )

    class Meta:
        verbose_name = _("size")
        verbose_name_plural = _("sizes")
        ordering = ("size_type", "display_order", "code")
        constraints = [
            models.UniqueConstraint(
                fields=["size_type", "code"],
                name="catalog_size_unique_per_type",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    size_type__in=["clothing", "numeric", "shoe", "one_size", "custom"]
                ),
                name="catalog_size_valid_type",
            ),
        ]
        indexes = [
            models.Index(fields=["size_type", "display_order"], name="cat_size_type_order_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        # Codes end up inside SKUs and URLs, so they are canonicalised rather than trusted.
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)


class Color(SluggedModel):
    """A named colour with a swatch.

    ``hex_code`` exists so the storefront can draw a circle and the admin can be colour-blind
    friendly. It is **not** identity: two ``Color`` rows may share a hex while meaning different
    things to a brand ("Bone" vs "Cream"), and re-tuning one colour must not touch any variant.
    """

    hex_code = models.CharField(
        _("hex code"),
        max_length=7,
        help_text=_("Swatch colour only, e.g. #0A1F44. Never used to identify a variant."),
    )

    class Meta:
        verbose_name = _("color")
        verbose_name_plural = _("colors")
        ordering = ("display_order", "name")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(hex_code__regex=r"^#[0-9A-F]{6}$"),
                name="catalog_color_valid_hex",
            )
        ]
        indexes = [
            models.Index(fields=["is_active", "display_order"], name="cat_color_active_order_idx"),
        ]

    def save(self, *args, **kwargs):
        self.hex_code = (self.hex_code or "").strip().upper()
        super().save(*args, **kwargs)

    @property
    def rgb(self) -> tuple[int, int, int]:
        """The hex value as 8-bit channels."""
        value = self.hex_code.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    @property
    def swatch_text_color(self) -> str:
        """Near-black or white -- whichever contrasts better on this swatch.

        A colour name is always rendered next to its swatch, so the label has to stay legible
        whatever hex a merchandiser enters. Picks the winner by WCAG contrast ratio rather than a
        brightness threshold.
        """
        luminance = _relative_luminance(*self.rgb)
        on_white = 1.05 / (luminance + 0.05)
        on_black = (luminance + 0.05) / 0.05
        return "#FFFFFF" if on_white >= on_black else "#0F172A"

    def clean(self):
        super().clean()
        value = (self.hex_code or "").strip().upper()
        if len(value) != 7 or not value.startswith("#"):
            raise ValidationError(
                {"hex_code": _("Use a hex value like #0A1F44.")},
                code="bad_hex",
            )
        try:
            int(value[1:], 16)
        except ValueError as error:
            raise ValidationError(
                {"hex_code": _("Use a hex value like #0A1F44.")},
                code="bad_hex",
            ) from error


class Material(SluggedModel):
    """A fabric or composition, e.g. Organic Cotton.

    Products carry several of these through a many-to-many field: "68% Cotton, 32% Polyester" is
    two rows, not a string, so filtering by fabric is a join rather than a text search.
    """

    class Meta:
        verbose_name = _("material")
        verbose_name_plural = _("materials")
        ordering = ("display_order", "name")
        indexes = [
            models.Index(
                fields=["is_active", "display_order"], name="cat_material_active_order_idx"
            ),
        ]


class Fit(SluggedModel):
    """How a garment is cut: Slim, Relaxed, Oversized, Wide Leg.

    One primary fit per product, nullable. This is structured data because FLASH DNA and the
    recommendation engine will match on it; a free-text "fit notes" paragraph cannot be filtered.
    """

    class Meta:
        verbose_name = _("fit")
        verbose_name_plural = _("fits")
        ordering = ("display_order", "name")
        indexes = [
            models.Index(fields=["is_active", "display_order"], name="cat_fit_active_order_idx"),
        ]


class ProductTag(SluggedModel):
    """A merchandising label: Bestseller, Trending, Limited, Sustainable, Premium.

    Tags are **not** a substitute for business-critical booleans. ``Product.is_new`` and
    ``Product.is_featured`` stay explicit columns because they drive homepage slots, are filtered
    on, and must be queryable and indexable; "New" also appears as a tag only when merchandising
    wants the word on the card. Business state belongs in a column; a human label belongs in a tag.
    """

    class Meta:
        verbose_name = _("product tag")
        verbose_name_plural = _("product tags")
        ordering = ("display_order", "name")
        indexes = [
            models.Index(fields=["is_active", "display_order"], name="cat_tag_active_order_idx"),
        ]
