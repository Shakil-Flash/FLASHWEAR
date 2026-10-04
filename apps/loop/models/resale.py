"""The public resale listing (Phase 13).

A :class:`ResaleListing` is the marketplace-facing half of a resale
:class:`~apps.loop.models.LoopItem`. The loop item owns the lifecycle
and the ownership evidence; the listing owns the public face: the slug
URL, the asking price a shopper sees, and the listing-specific states
(``PENDING_REVIEW -> ACTIVE -> RESERVED -> SOLD``).

One-to-one with the loop item on purpose: a listing is never a
second-hand "product" that could accumulate stock, variants or prices
of its own. A resale row is a unique unit, and it must never touch
first-party :class:`~apps.catalog.models.ProductVariant` stock -- that
inventory belongs to the catalogue and stays authoritative.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel
from apps.catalog.slugs import unique_slug

__all__ = ["ResaleListing"]


class ResaleListing(TimestampedModel):
    """One resale unit on the public second-hand shelf."""

    class Status(models.TextChoices):
        """Listing lifecycle, driven by the moderation services."""

        PENDING_REVIEW = "pending_review", _("Pending review")
        ACTIVE = "active", _("Active")
        RESERVED = "reserved", _("Reserved")
        SOLD = "sold", _("Sold")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")

    loop_item = models.OneToOneField(
        "loop.LoopItem",
        on_delete=models.PROTECT,
        related_name="resale_listing",
        verbose_name=_("loop item"),
        help_text=_("The resale item this listing sells. One listing per item."),
    )
    seller = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="resale_listings",
        verbose_name=_("seller"),
        help_text=_("Always the verified owner; never client-supplied."),
    )
    slug = models.SlugField(
        _("slug"),
        max_length=200,
        unique=True,
        help_text=_("Public URL handle. Derived, then frozen."),
    )
    asking_price = models.DecimalField(
        _("asking price"),
        max_digits=10,
        decimal_places=2,
        help_text=_("The seller-proposed price, validated at submission."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING_REVIEW,
        db_index=True,
    )
    listed_at = models.DateTimeField(_("listed at"), null=True, blank=True)
    sold_at = models.DateTimeField(_("sold at"), null=True, blank=True)
    expired_at = models.DateTimeField(_("expired at"), null=True, blank=True)

    class Meta:
        verbose_name = _("resale listing")
        verbose_name_plural = _("resale listings")
        ordering = ("-listed_at", "-id")
        indexes = [
            models.Index(fields=["status", "-listed_at"], name="loop_listing_status_listed_idx"),
            models.Index(fields=["seller", "status"], name="loop_listing_seller_status_idx"),
            models.Index(fields=["loop_item"], name="loop_listing_item_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.slug} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.slug:
            # Slug from the product name when available, else the loop item id,
            # so the URL is human-readable but never collides.
            base = self.loop_item.product.slug if self.loop_item.product_id else None
            if not base:
                base = f"preloved-{self.loop_item_id or 'item'}"
            self.slug = unique_slug(
                ResaleListing,
                base,
                instance=self,
                max_length=self._meta.get_field("slug").max_length,
            )
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("loop:resale-detail", kwargs={"slug": self.slug})

    @property
    def is_public(self) -> bool:
        """Whether a stranger may view this listing right now."""
        return self.status == self.Status.ACTIVE

    @property
    def is_saleable(self) -> bool:
        """Active listing on an item that is still listed (not sold elsewhere)."""
        return self.is_public and self.loop_item.status == "listed"

    # -- listing lifecycle ----------------------------------------------------------

    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        Status.PENDING_REVIEW: (Status.ACTIVE, Status.CANCELLED),
        Status.ACTIVE: (Status.RESERVED, Status.SOLD, Status.CANCELLED, Status.EXPIRED),
        Status.RESERVED: (Status.SOLD, Status.ACTIVE, Status.CANCELLED),
        Status.SOLD: (),
        Status.CANCELLED: (),
        Status.EXPIRED: (),
    }

    def can_transition_to(self, target: str) -> bool:
        return target in self.ALLOWED_TRANSITIONS.get(self.status, ())

    def stamp(self, field: str) -> None:
        """Stamp a lifecycle timestamp once (first write wins)."""
        if getattr(self, field) is None:
            setattr(self, field, timezone.now())
