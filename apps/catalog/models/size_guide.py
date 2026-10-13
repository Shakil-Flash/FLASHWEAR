"""Size Guide models (Phase 41).

Structured size charts and garment measurements for products and categories.
Provides clear separation between finished garment dimensions (laid flat)
and body measurements (recommended wearer size), with clean unit conversion
between cm and inches.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel


class SizeGuide(TimestampedModel):
    """Size chart associated with a specific Product or Category.

    When both exist, a product-specific guide takes precedence over the category guide.
    """

    class MeasurementType(models.TextChoices):
        GARMENT = "garment", _("Garment measurements (laid flat / finished)")
        BODY = "body", _("Body measurements (recommended body dimensions)")

    class Unit(models.TextChoices):
        CM = "cm", _("Centimeters (cm)")
        INCH = "in", _("Inches (in)")

    name = models.CharField(
        _("name"),
        max_length=120,
        help_text=_("e.g. Standard Men's Tees, Relaxed Denim Jeans."),
    )
    product = models.OneToOneField(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="size_guide",
        null=True,
        blank=True,
        verbose_name=_("product override"),
        help_text=_("Direct product-specific size guide override."),
    )
    category = models.ForeignKey(
        "catalog.Category",
        on_delete=models.CASCADE,
        related_name="size_guides",
        null=True,
        blank=True,
        verbose_name=_("category"),
        help_text=_("Category-level size guide applied when a product has no direct guide."),
    )
    measurement_type = models.CharField(
        _("measurement type"),
        max_length=16,
        choices=MeasurementType.choices,
        default=MeasurementType.GARMENT,
        help_text=_("Whether numbers indicate finished garment dimensions or body measurements."),
    )
    unit = models.CharField(
        _("default unit"),
        max_length=8,
        choices=Unit.choices,
        default=Unit.CM,
        help_text=_("Unit of measurement stored in the database for this guide."),
    )
    description = models.TextField(
        _("description / fit notes"),
        blank=True,
        help_text=_("Editorial fit guidance, measuring tips, or model specifications."),
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_("Inactive guides are hidden from storefront calculations and modals."),
    )

    class Meta:
        verbose_name = _("size guide")
        verbose_name_plural = _("size guides")
        ordering = ("name", "id")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(measurement_type__in=["garment", "body"]),
                name="catalog_sizeguide_valid_type",
            ),
            models.CheckConstraint(
                condition=models.Q(unit__in=["cm", "in"]),
                name="catalog_sizeguide_valid_unit",
            ),
        ]

    def __str__(self) -> str:
        if self.product:
            return f"{self.name} (Product: {self.product.name})"
        if self.category:
            return f"{self.name} (Category: {self.category.name})"
        return self.name

    def clean(self):
        super().clean()
        if not self.product and not self.category:
            raise ValidationError(
                _("A size guide must be attached to either a product or a category.")
            )

    @property
    def target_display(self) -> str:
        """Friendly label indicating what this size guide applies to."""
        if self.product:
            return f"Product: {self.product.name}"
        if self.category:
            return f"Category: {self.category.name}"
        return "Unassigned"


class SizeGuideRow(models.Model):
    """One size measurement row in a size guide."""

    guide = models.ForeignKey(
        SizeGuide,
        on_delete=models.CASCADE,
        related_name="rows",
        verbose_name=_("size guide"),
    )
    size = models.ForeignKey(
        "catalog.Size",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("size"),
    )
    chest_min = models.DecimalField(
        _("chest min / width"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text=_("Chest / bust minimum or exact dimension."),
    )
    chest_max = models.DecimalField(
        _("chest max"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text=_("Optional upper bound for body measurement ranges."),
    )
    waist_min = models.DecimalField(
        _("waist min / width"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text=_("Waist minimum or exact dimension."),
    )
    waist_max = models.DecimalField(
        _("waist max"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text=_("Optional upper bound for waist range."),
    )
    hips_min = models.DecimalField(
        _("hips min / width"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text=_("Hips minimum or exact dimension."),
    )
    hips_max = models.DecimalField(
        _("hips max"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text=_("Optional upper bound for hips range."),
    )
    shoulder = models.DecimalField(
        _("shoulder width"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text=_("Shoulder seam to seam measurement."),
    )
    length = models.DecimalField(
        _("garment length"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text=_("Total garment length from high shoulder point."),
    )
    inseam = models.DecimalField(
        _("inseam"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.1"))],
        help_text=_("Inside leg seam length."),
    )
    display_order = models.PositiveSmallIntegerField(
        _("display order"),
        default=0,
    )

    class Meta:
        verbose_name = _("size guide row")
        verbose_name_plural = _("size guide rows")
        ordering = ("display_order", "size__display_order", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["guide", "size"],
                name="catalog_sizeguiderow_unique_size",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.guide.name} — {self.size.code}"

    def clean(self):
        super().clean()
        for field in ("chest", "waist", "hips"):
            min_val = getattr(self, f"{field}_min")
            max_val = getattr(self, f"{field}_max")
            if min_val is not None and max_val is not None and max_val < min_val:
                raise ValidationError(
                    {
                        f"{field}_max": _(
                            "Maximum measurement cannot be less than minimum measurement."
                        )
                    }
                )

    def _convert(self, val: Decimal | None, to_unit: str) -> Decimal | None:
        if val is None:
            return None
        from_unit = self.guide.unit
        if from_unit == to_unit:
            return val
        if from_unit == SizeGuide.Unit.CM and to_unit == SizeGuide.Unit.INCH:
            return round(val / Decimal("2.54"), 1)
        if from_unit == SizeGuide.Unit.INCH and to_unit == SizeGuide.Unit.CM:
            return round(val * Decimal("2.54"), 1)
        return val

    def format_dimension(self, field_name: str, unit: str = "cm") -> str:
        """Format a dimension as single value, range, or dash."""
        if field_name in ("shoulder", "length", "inseam"):
            raw_val = getattr(self, field_name)
            conv_val = self._convert(raw_val, unit)
            return f"{conv_val}" if conv_val is not None else "—"

        min_raw = getattr(self, f"{field_name}_min")
        max_raw = getattr(self, f"{field_name}_max")
        min_conv = self._convert(min_raw, unit)
        max_conv = self._convert(max_raw, unit)

        if min_conv is not None and max_conv is not None and min_conv != max_conv:
            return f"{min_conv} - {max_conv}"
        if min_conv is not None:
            return f"{min_conv}"
        if max_conv is not None:
            return f"{max_conv}"
        return "—"

    @property
    def chest_cm(self) -> str:
        return self.format_dimension("chest", "cm")

    @property
    def chest_in(self) -> str:
        return self.format_dimension("chest", "in")

    @property
    def waist_cm(self) -> str:
        return self.format_dimension("waist", "cm")

    @property
    def waist_in(self) -> str:
        return self.format_dimension("waist", "in")

    @property
    def hips_cm(self) -> str:
        return self.format_dimension("hips", "cm")

    @property
    def hips_in(self) -> str:
        return self.format_dimension("hips", "in")

    @property
    def shoulder_cm(self) -> str:
        return self.format_dimension("shoulder", "cm")

    @property
    def shoulder_in(self) -> str:
        return self.format_dimension("shoulder", "in")

    @property
    def length_cm(self) -> str:
        return self.format_dimension("length", "cm")

    @property
    def length_in(self) -> str:
        return self.format_dimension("length", "in")

    @property
    def inseam_cm(self) -> str:
        return self.format_dimension("inseam", "cm")

    @property
    def inseam_in(self) -> str:
        return self.format_dimension("inseam", "in")
