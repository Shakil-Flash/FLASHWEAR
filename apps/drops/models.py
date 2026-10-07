"""FLASH Drop models (Phase 11).

Dedicated limited-release system on top of the existing catalog → cart →
checkout → payment → order → inventory pipeline.  Drops add controlled
release behaviour; they never create a parallel commerce system.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models import Product
from apps.catalog.validators import validate_catalog_image


class DropStatus(models.TextChoices):
    """Controlled drop states — derived from publication + start/end time + cancellation."""

    DRAFT = "draft", _("Draft")
    SCHEDULED = "scheduled", _("Scheduled")
    LIVE = "live", _("Live")
    ENDED = "ended", _("Ended")
    CANCELLED = "cancelled", _("Cancelled")


class FlashDropManager(models.Manager):
    """Custom manager for FlashDrop with state filtering."""

    def active(self):
        """Drops that are live or scheduled."""
        return self.filter(status__in=[DropStatus.LIVE, DropStatus.SCHEDULED])

    def live(self):
        """Drops that are currently live."""
        return self.filter(status=DropStatus.LIVE)

    def upcoming(self):
        """Drops that are scheduled but not yet live."""
        return self.filter(status=DropStatus.SCHEDULED)

    def ended(self):
        """Drops that have ended."""
        return self.filter(status=DropStatus.ENDED)

    def cancelled(self):
        """Drops that have been cancelled."""
        return self.filter(status=DropStatus.CANCELLED)

    def visible_to(self, user):
        """Drops visible to this user."""
        return self.filter(status__in=[DropStatus.LIVE, DropStatus.SCHEDULED])

    def with_storefront_data(self):
        """Eager-load everything a drop landing page touches."""
        return self.prefetch_related(
            "products__product",
            "products__variant__color",
            "products__variant__size",
        )


class FlashDrop(models.Model):
    """A limited-release, time-sensitive product drop.

    Sits above the catalogue.  Products in a drop remain the same Product/ProductVariant
    that belong to the normal catalogue; the drop only controls *when* they are purchasable
    and *how* they are presented.
    """

    name = models.CharField(_("name"), max_length=200)
    slug = models.SlugField(_("slug"), max_length=200, unique=True)
    description = models.TextField(_("description"), blank=True)
    hero_image = models.ImageField(
        _("hero image"),
        upload_to="drops/heroes/",
        blank=True,
        null=True,
        # Model-level: the Django admin exposes this field, and its default form would
        # only run Django's built-in extension check, not the content-first rules.
        validators=[validate_catalog_image],
        help_text=_("Featured image for the drop landing page."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=DropStatus.choices,
        default=DropStatus.DRAFT,
        help_text=_("Derived from publication, start/end time, and cancellation."),
    )
    starts_at = models.DateTimeField(
        _("starts at"),
        null=True,
        blank=True,
        help_text=_("When this drop becomes purchasable."),
    )
    ends_at = models.DateTimeField(
        _("ends at"),
        null=True,
        blank=True,
        help_text=_("When this drop stops being purchasable."),
    )
    published_at = models.DateTimeField(
        _("published at"),
        null=True,
        blank=True,
        help_text=_("When this drop first became visible on the storefront."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = FlashDropManager()

    class Meta:
        verbose_name = _("flash drop")
        verbose_name_plural = _("flash drops")
        ordering = ("-starts_at", "-created_at")
        indexes = [
            models.Index(fields=["status", "-starts_at"], name="drop_status_starts_idx"),
            models.Index(fields=["status", "-ends_at"], name="drop_status_ends_idx"),
            models.Index(fields=["status", "-published_at"], name="drop_status_published_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"

    def save(self, *args, **kwargs) -> None:
        """Derive status on every save and stamp published_at on first launch."""
        derived = self.effective_state

        # If status changed from DRAFT/SCHEDULED to LIVE, stamp published_at
        was_draft_or_scheduled = self.pk is None or (
            FlashDrop.objects.filter(pk=self.pk).values("status").first().get("status", "draft")
            in (DropStatus.DRAFT, DropStatus.SCHEDULED)
        )

        if derived == DropStatus.LIVE and was_draft_or_scheduled:
            if self.published_at is None:
                self.published_at = timezone.now()

        self.status = derived
        super().save(*args, **kwargs)

    @property
    def is_draft(self) -> bool:
        return self.status == DropStatus.DRAFT

    @property
    def is_scheduled(self) -> bool:
        return self.status == DropStatus.SCHEDULED

    @property
    def is_live(self) -> bool:
        return self.status == DropStatus.LIVE

    @property
    def is_ended(self) -> bool:
        return self.status == DropStatus.ENDED

    @property
    def is_cancelled(self) -> bool:
        return self.status == DropStatus.CANCELLED

    @property
    def effective_state(self) -> DropStatus:
        """Derive the effective public state from publication + time + cancellation.

        Rules (ordered by priority):
        1. Cancelled → CANCELLED
        2. End time passed → ENDED
        3. Start time not yet reached → SCHEDULED
        4. Start time reached → LIVE
        5. Otherwise → DRAFT
        """
        if self.is_cancelled:
            return DropStatus.CANCELLED

        if self.ends_at and timezone.now() > self.ends_at:
            return DropStatus.ENDED

        if self.starts_at and timezone.now() < self.starts_at:
            return DropStatus.SCHEDULED

        if self.starts_at and timezone.now() >= self.starts_at:
            return DropStatus.LIVE

        return DropStatus.DRAFT

    @property
    def available_products(self):
        """Return products that are purchasable given the current state."""
        from apps.drops.models import DropProduct

        return DropProduct.objects.filter(drop=self, is_active=True).select_related(
            "product", "variant"
        )


class DropProduct(models.Model):
    """Explicit relationship between a FlashDrop and a Product/Variant.

    Does not duplicate Product price — the variant's price is the source of truth.
    Supports display ordering, featured flag, and optional drop-specific label.
    Fields are declared before Meta per Django style guide.
    """

    drop = models.ForeignKey(
        FlashDrop,
        on_delete=models.CASCADE,
        related_name="products",
        verbose_name=_("drop"),
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="drop_products",
        verbose_name=_("product"),
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="drop_products",
        verbose_name=_("variant"),
    )
    order_position = models.PositiveIntegerField(
        _("display order"),
        default=0,
        help_text=_("Lower numbers appear first on the drop page."),
    )
    is_featured = models.BooleanField(
        _("featured"),
        default=False,
        help_text=_("Show in featured slots on the drop landing page."),
    )
    drop_label = models.CharField(
        _("drop label"),
        max_length=128,
        blank=True,
        help_text=_("Optional label displayed on the product card, e.g. 'Launch Edition'."),
    )

    class Meta:
        verbose_name = _("drop product")
        verbose_name_plural = _("drop products")
        ordering = ("order_position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["drop", "product", "variant"],
                name="drop_unique_product_variant",
                condition=Q(variant__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["drop", "product"],
                name="drop_unique_product_no_variant",
                condition=Q(variant__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        if self.variant:
            return f"{self.product.name} ({self.variant}) — {self.drop}"
        return f"{self.product.name} — {self.drop}"


class DropAllocation(models.Model):
    """Optional track allocation of limited quantity to a drop.

    If a drop has a limited allocation, this tracks how much has been claimed
    against the real inventory.  The physical stock is always owned by inventory;

    never use an unsafe ``remaining = remaining - 1`` without proper locking/atomicity.
    Use the existing order/inventory transaction flow.
    """

    drop = models.ForeignKey(
        FlashDrop,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name=_("drop"),
    )
    # Optional: if the drop pre-allocates a fixed quantity
    total_allocated = models.PositiveIntegerField(
        _("total allocated"),
        null=True,
        blank=True,
        help_text=_("Fixed total quantity allocated to this drop. Null = unlimited."),
    )
    claimed = models.PositiveIntegerField(
        _("claimed"),
        default=0,
        help_text=_("How many units have been claimed through orders."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("drop allocation")
        verbose_name_plural = _("drop allocations")
        constraints = [
            models.CheckConstraint(
                condition=Q(claimed__lte=models.F("total_allocated")),
                name="drop_allocation_claimed_within_limit",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.drop} allocation: {self.claimed}" + (
            f"/ {self.total_allocated}" if self.total_allocated else ""
        )

    @property
    def remaining(self) -> int:
        """Remaining allocated units — always use via .remaining (never -=)."""
        if self.total_allocated:
            return max(0, self.total_allocated - self.claimed)
        return 0
