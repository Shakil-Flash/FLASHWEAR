"""The central circular-fashion record and its photos (Phase 13).

One :class:`LoopItem` is one physical garment entering the FLASH Loop. It is a
**layer around commerce, not a replacement for it**: the item references the
authoritative catalogue row (``product`` / ``variant``) instead of copying its
name, brand, size or material, and it references the ownership evidence that
lets the customer list it (``order_item`` from a delivered order, and/or
``closet_item`` from the wardrobe).

Provenance is server-written. ``closet_item`` and ``order_item`` are the only
acceptable ownership evidence in this phase, and both are resolved by
:mod:`apps.loop.services.ownership` -- a payload can point at a row it does not
own, but it can never make the service agree that it does.

The ``status`` column is a controlled state machine (see
:attr:`ALLOWED_TRANSITIONS`). Which states are reachable depends on ``type``:
a resale item walks ``DRAFT -> SUBMITTED -> UNDER_REVIEW -> APPROVED -> LISTED
-> SOLD``; a trade-in walks ``... -> TRADE_IN_ACCEPTED -> TRADE_IN_COMPLETED``;
a recycling walk ends at ``RECYCLE_COMPLETED``. The service layer enforces both
the graph and the per-type gating, so no view or API payload can jump.

``authenticity_status`` is deliberately **not** seller-settable. It starts
``UNVERIFIED`` (which means exactly that -- "we have not checked", never
"counterfeit") and only staff moderation may move it.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel
from apps.catalog.validators import EXTENSIONS_BY_FORMAT, validate_catalog_image

__all__ = ["LoopItem", "LoopItemImage", "loop_image_upload_to"]

ZERO = Decimal("0.00")

# Terminal states: a loop item in one of these can never change again, and does
# not block a new loop cycle for the same physical garment. Defined before the
# model because ``Meta.constraints`` is evaluated at class-creation time.
_TERMINAL_STATUSES = (
    "rejected",
    "sold",
    "cancelled",
    "trade_in_completed",
    "recycle_completed",
)


def loop_image_upload_to(instance: LoopItemImage, filename: str) -> str:
    """Safe, collision-proof storage path for a loop photo.

    Same policy as the catalogue and the closet: the file name is
    attacker-controlled, so the stored name is a random UUID, and the extension
    comes from the content-verified allow-list -- an unrecognised declaration
    yields a file with **no** extension rather than ``.svg``. The bytes were
    checked by :func:`apps.catalog.validators.validate_catalog_image` before
    this runs.
    """
    from uuid import uuid4

    allowed = {ext for exts in EXTENSIONS_BY_FORMAT.values() for ext in exts}
    _, _, declared = (filename or "").rpartition(".")
    suffix = f".{declared.lower()}" if f".{declared.lower()}" in allowed else ""
    return f"loop/{instance.loop_item_id or 'unassigned'}/{uuid4().hex}{suffix}"


class LoopItem(TimestampedModel):
    """One physical garment in the FLASH Loop lifecycle."""

    class Type(models.TextChoices):
        """The loop path. The type controls which states are reachable."""

        RESALE = "resale", _("Resale")
        TRADE_IN = "trade_in", _("Trade-in")
        RECYCLE = "recycle", _("Recycle")

    class Status(models.TextChoices):
        """Controlled lifecycle. ``ALLOWED_TRANSITIONS`` is the graph."""

        DRAFT = "draft", _("Draft")
        SUBMITTED = "submitted", _("Submitted")
        UNDER_REVIEW = "under_review", _("Under review")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        LISTED = "listed", _("Listed")
        RESERVED = "reserved", _("Reserved")
        SOLD = "sold", _("Sold")
        TRADE_IN_ACCEPTED = "trade_in_accepted", _("Trade-in accepted")
        TRADE_IN_COMPLETED = "trade_in_completed", _("Trade-in completed")
        RECYCLE_ACCEPTED = "recycle_accepted", _("Recycle accepted")
        RECYCLE_COMPLETED = "recycle_completed", _("Recycle completed")
        CANCELLED = "cancelled", _("Cancelled")

    class Condition(models.TextChoices):
        """Seller-submitted condition. Unverified until moderation reads it."""

        NEW_WITH_TAGS = "new_with_tags", _("New with tags")
        LIKE_NEW = "like_new", _("Like new")
        EXCELLENT = "excellent", _("Excellent")
        GOOD = "good", _("Good")
        FAIR = "fair", _("Fair")
        DAMAGED = "damaged", _("Damaged")

    class AuthenticityStatus(models.TextChoices):
        """Authentication state. Staff-only; never seller-settable."""

        UNVERIFIED = "unverified", _("Unverified")
        PENDING = "pending", _("Pending verification")
        VERIFIED = "verified", _("Verified")
        REJECTED = "rejected", _("Rejected")

    # Ownership: the account that owns the physical garment. PROTECT (the
    # account-area convention) so closing an account keeps its loop history.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="loop_items",
        verbose_name=_("owner"),
    )

    type = models.CharField(
        _("loop type"),
        max_length=16,
        choices=Type.choices,
        help_text=_("The path this item enters: resale, trade-in or recycling."),
    )
    status = models.CharField(
        _("status"),
        max_length=24,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    # Authoritative catalogue links. The catalogue remains the source of truth for
    # name/brand/size/colour/material; nothing is duplicated onto this row.
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="loop_items",
        verbose_name=_("product"),
        help_text=_("The garment's catalogue row; resolves all display details."),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="loop_items",
        verbose_name=_("variant"),
        help_text=_("The specific SKU (colour/size) being circulated."),
    )

    # Ownership evidence. Server-written by the ownership service; a payload can
    # never set these to a row it does not own.
    order_item = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="loop_items",
        verbose_name=_("order line"),
        help_text=_("The delivered order line this unit came from, when known."),
    )
    closet_item = models.ForeignKey(
        "closet.ClosetItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="loop_items",
        verbose_name=_("closet item"),
        help_text=_("The wardrobe row for this physical unit, when it is in the closet."),
    )

    # Seller-submitted, moderation-unverified facts.
    condition = models.CharField(
        _("condition"),
        max_length=16,
        choices=Condition.choices,
        help_text=_("Seller-stated condition; treated as unverified until reviewed."),
    )
    condition_notes = models.TextField(
        _("condition notes"),
        blank=True,
        help_text=_("Plain-text seller notes (worn twice, stitching mark, ...)."),
    )
    title = models.CharField(
        _("title"),
        max_length=200,
        blank=True,
        help_text=_("Resale title; falls back to the catalogue product name."),
    )
    description = models.TextField(
        _("description"),
        blank=True,
        help_text=_("Plain-text resale description. No HTML."),
    )

    # Resale only. Null for trade-in and recycling items.
    asking_price = models.DecimalField(
        _("asking price"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(ZERO)],
        help_text=_("Seller-proposed price. Validated against the configured bounds."),
    )

    # Staff-controlled moderation state.
    authenticity_status = models.CharField(
        _("authenticity status"),
        max_length=16,
        choices=AuthenticityStatus.choices,
        default=AuthenticityStatus.UNVERIFIED,
        help_text=_("Staff-controlled. Unverified means 'not checked', not 'fake'."),
    )
    moderation_note = models.CharField(
        _("moderation note"),
        max_length=300,
        blank=True,
        help_text=_("Internal note. Never rendered in customer-facing templates."),
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reviewer"),
    )
    reviewed_at = models.DateTimeField(_("reviewed at"), null=True, blank=True)

    # Lifecycle stamps.
    listed_at = models.DateTimeField(_("listed at"), null=True, blank=True)
    sold_at = models.DateTimeField(_("sold at"), null=True, blank=True)

    class Meta:
        verbose_name = _("loop item")
        verbose_name_plural = _("loop items")
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(asking_price__isnull=True)
                | models.Q(asking_price__gte=0),
                name="loop_item_asking_price_non_negative",
            ),
            # One *active* loop workflow per physical unit. Terminal rows (sold,
            # rejected, completed, cancelled) do not block a new cycle -- a garment
            # can circulate again. The order-item constraint only fires when there
            # is no closet row: multi-unit purchases are listed per closet unit.
            models.UniqueConstraint(
                fields=["closet_item"],
                condition=models.Q(closet_item__isnull=False)
                & ~models.Q(status__in=_TERMINAL_STATUSES),
                name="loop_item_one_active_per_closet_item",
            ),
            models.UniqueConstraint(
                fields=["order_item"],
                condition=models.Q(order_item__isnull=False)
                & models.Q(closet_item__isnull=True)
                & ~models.Q(status__in=_TERMINAL_STATUSES),
                name="loop_item_one_active_per_order_item",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="loop_item_user_status_idx"),
            models.Index(fields=["user", "type"], name="loop_item_user_type_idx"),
            models.Index(fields=["product", "status"], name="loop_item_product_status_idx"),
            models.Index(fields=["-created_at"], name="loop_item_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()} #{self.pk}"

    # -- state machine ---------------------------------------------------------------

    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        Status.DRAFT: (Status.SUBMITTED, Status.CANCELLED),
        Status.SUBMITTED: (Status.UNDER_REVIEW, Status.CANCELLED),
        Status.UNDER_REVIEW: (
            Status.APPROVED,
            Status.REJECTED,
            Status.TRADE_IN_ACCEPTED,
            Status.RECYCLE_ACCEPTED,
            Status.CANCELLED,
        ),
        Status.APPROVED: (Status.LISTED, Status.CANCELLED),
        Status.LISTED: (Status.RESERVED, Status.SOLD, Status.CANCELLED),
        Status.RESERVED: (Status.SOLD, Status.LISTED, Status.CANCELLED),
        Status.SOLD: (),
        Status.REJECTED: (),
        Status.TRADE_IN_ACCEPTED: (Status.TRADE_IN_COMPLETED, Status.CANCELLED),
        Status.TRADE_IN_COMPLETED: (),
        Status.RECYCLE_ACCEPTED: (Status.RECYCLE_COMPLETED, Status.CANCELLED),
        Status.RECYCLE_COMPLETED: (),
        Status.CANCELLED: (),
    }

    @classmethod
    def terminal_statuses(cls) -> tuple[str, ...]:
        """States a loop item can never leave."""
        return _TERMINAL_STATUSES

    def can_transition_to(self, target: str) -> bool:
        return target in self.ALLOWED_TRANSITIONS.get(self.status, ())

    @property
    def is_active(self) -> bool:
        """Whether this item is inside a live workflow (not terminal)."""
        return self.status not in _TERMINAL_STATUSES

    @property
    def is_resale(self) -> bool:
        return self.type == self.Type.RESALE

    @property
    def is_trade_in(self) -> bool:
        return self.type == self.Type.TRADE_IN

    @property
    def is_recycle(self) -> bool:
        return self.type == self.Type.RECYCLE

    @property
    def display_title(self) -> str:
        """Resale title, else the catalogue product name."""
        return self.title or (self.product.name if self.product_id else "")

    # -- validation ------------------------------------------------------------------

    def clean(self):
        super().clean()
        errors: dict[str, ValidationError] = {}
        if self.variant_id and self.product_id and self.variant.product_id != self.product_id:
            errors["variant"] = ValidationError(
                _("That variant belongs to a different product."),
                code="variant_product_mismatch",
            )
        if self.asking_price is not None and self.type != self.Type.RESALE:
            errors["asking_price"] = ValidationError(
                _("An asking price belongs on a resale item."),
                code="price_only_for_resale",
            )
        if self.type == self.Type.RESALE and self.asking_price is None:
            errors["asking_price"] = ValidationError(
                _("A resale item needs an asking price."), code="missing_price"
            )
        if errors:
            raise ValidationError(errors)


class LoopItemImage(TimestampedModel):
    """One photo of a loop item: front, back, label, condition detail or extra."""

    class Kind(models.TextChoices):
        FRONT = "front", _("Front")
        BACK = "back", _("Back")
        LABEL = "label", _("Label")
        DETAIL = "detail", _("Condition detail")
        ADDITIONAL = "additional", _("Additional")

    loop_item = models.ForeignKey(
        LoopItem,
        on_delete=models.CASCADE,
        related_name="images",
        verbose_name=_("loop item"),
    )
    image = models.ImageField(
        _("image"),
        upload_to=loop_image_upload_to,
        validators=[validate_catalog_image],
        help_text=_("JPEG, PNG or WebP, within the catalogue image limits."),
    )
    kind = models.CharField(
        _("kind"),
        max_length=16,
        choices=Kind.choices,
        default=Kind.FRONT,
        help_text=_("What the photo shows. Determines its place in the gallery."),
    )
    alt_text = models.CharField(
        _("alt text"),
        max_length=200,
        help_text=_("Describe the photo for screen readers."),
    )
    position = models.PositiveIntegerField(
        _("position"),
        default=0,
        help_text=_("Gallery order. Lower numbers come first."),
    )

    class Meta:
        verbose_name = _("loop item image")
        verbose_name_plural = _("loop item images")
        ordering = ("position", "id")
        indexes = [
            models.Index(fields=["loop_item", "position"], name="loop_image_item_pos_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.loop_item} photo {self.position}"
