"""Customer reviews (Phase 7).

The policy lives here in constraints so no caller can route around it:

* **One review per customer per product.** A shopper who buys again edits their existing review
  rather than stacking a second one.
* **Purchase-gated.** Creating a review requires a qualifying paid order containing the product,
  which makes ``verified_purchase`` a server-derived record of that check -- not a claim. The field
  is ``editable=False`` and appears in no form and no serializer payload.
* **Nothing is public until moderated.** Rows are born ``pending``; every public surface (product
  page, storefront API) selects ``published`` only. Moderation moves status and stamps
  ``published_at`` / ``moderated_at``; it never deletes, so the audit trail survives.

Rating is constrained at both levels: a validator for a friendly form error and a database
``CheckConstraint`` for everything that bypasses forms.
"""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel


class Review(TimestampedModel):
    """One customer's assessment of one product, awaiting or past moderation."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        PUBLISHED = "published", _("Published")
        REJECTED = "rejected", _("Rejected")
        HIDDEN = "hidden", _("Hidden")

    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.PROTECT,
        related_name="reviews",
        verbose_name=_("product"),
        help_text=_("PROTECT: reviews are part of the product's commercial history."),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviews",
        verbose_name=_("author"),
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.PROTECT,
        related_name="reviews",
        verbose_name=_("qualifying order"),
        help_text=_(
            "The paid order that earned this customer the right to review; "
            "it is also what the verified-purchase flag was derived from."
        ),
    )
    rating = models.PositiveSmallIntegerField(
        _("rating"),
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text=_("1 (poor) to 5 (excellent)."),
    )
    title = models.CharField(_("title"), max_length=120)
    body = models.TextField(
        _("review"),
        help_text=_("What the customer thought, in their own words."),
    )
    verified_purchase = models.BooleanField(
        _("verified purchase"),
        default=True,
        editable=False,
        help_text=_(
            "Server-derived when the review was created from a qualifying paid order. "
            "Never writable from a form or API payload."
        ),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        help_text=_("Pending until a moderator publishes it."),
    )
    published_at = models.DateTimeField(
        _("published at"),
        null=True,
        blank=True,
        help_text=_("When the review first became public."),
    )
    moderated_at = models.DateTimeField(
        _("moderated at"),
        null=True,
        blank=True,
        help_text=_("When a moderator last changed the status."),
    )

    class Meta:
        verbose_name = _("review")
        verbose_name_plural = _("reviews")
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["author", "product"],
                name="engagement_one_review_per_user_and_product",
            ),
            models.CheckConstraint(
                condition=models.Q(rating__gte=1, rating__lte=5),
                name="engagement_review_rating_between_1_and_5",
            ),
        ]
        indexes = [
            models.Index(fields=["product", "status"], name="engagement_review_prod_idx"),
            models.Index(fields=["status", "-created_at"], name="engagement_review_feed_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.product.slug} by {self.author.email}: {self.rating}/5 ({self.status})"

    @property
    def is_public(self) -> bool:
        return self.status == self.Status.PUBLISHED
