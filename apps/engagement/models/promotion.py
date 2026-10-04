"""Promotions: codes customers redeem against an order (Phase 7).

One authoritative engine lives in ``apps.engagement.services.promotions``; the model carries the
facts and the constraints that keep an operator from saving something the engine would have to
reject at checkout time:

* ``discount_value`` is a percentage in ``(0, 100]`` for PERCENTAGE and a positive currency
  amount for FIXED -- two ``CheckConstraint`` clauses, not one.
* The window must be ordered (``starts_at < ends_at``); both ends are required.
* ``used_count`` is the fast, lock-protected counter the engine checks against ``usage_limit``.
  The database ``used_count <= usage_limit`` constraint is the backstop: even a bug that skipped
  the lock cannot oversell the promotion. ``PromotionUsage`` rows (created with the order) are the
  auditable truth behind the denormalised counter.

``PromotionUsage`` is what ties a discount to the order it discounted. It is PROTECTed on both
ends because it is financial history, and unique per ``(promotion, order)`` so a replayed handoff
cannot double-charge -- or double-discount -- anything.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel


class Promotion(TimestampedModel):
    """A discount code with a window, limits and a stacking policy."""

    class DiscountType(models.TextChoices):
        PERCENTAGE = "percentage", _("Percentage")
        FIXED = "fixed", _("Fixed amount")

    code = models.CharField(
        _("code"),
        max_length=32,
        unique=True,
        help_text=_(
            "Normalised (trimmed, upper-cased) by the engine before comparison. "
            "Letters, digits, hyphen and underscore only."
        ),
    )
    name = models.CharField(_("name"), max_length=100)
    description = models.TextField(
        _("description"),
        blank=True,
        help_text=_("Operator-facing terms; not shown to customers unless a template prints it."),
    )
    discount_type = models.CharField(
        _("discount type"),
        max_length=16,
        choices=DiscountType.choices,
    )
    discount_value = models.DecimalField(
        _("discount value"),
        max_digits=10,
        decimal_places=2,
        help_text=_("Percent of the eligible subtotal (max 100) or a fixed currency amount."),
    )
    starts_at = models.DateTimeField(
        _("starts at"),
        help_text=_("Inclusive window opening, in the active time zone."),
    )
    ends_at = models.DateTimeField(
        _("ends at"),
        help_text=_("Exclusive window closing, in the active time zone."),
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_("An inactive promotion is rejected even inside its window."),
    )
    usage_limit = models.PositiveIntegerField(
        _("usage limit"),
        null=True,
        blank=True,
        help_text=_("Global cap across all customers. Blank means unlimited."),
    )
    used_count = models.PositiveIntegerField(
        _("used count"),
        default=0,
        help_text=_(
            "Denormalised under a row lock at consumption time; auditable against "
            "the PromotionUsage rows."
        ),
    )
    per_user_limit = models.PositiveIntegerField(
        _("per-user limit"),
        null=True,
        blank=True,
        help_text=_("How many times one customer may use it. Blank means unlimited."),
    )
    min_order_amount = models.DecimalField(
        _("minimum order amount"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Eligible subtotal must reach this amount before the code applies."),
    )
    allow_loyalty_redemption = models.BooleanField(
        _("allow loyalty redemption"),
        default=True,
        help_text=_(
            "Uncheck to stack this promotion with FLASH Points redemption. "
            "Some deep discounts exclude points by policy."
        ),
    )

    class Meta:
        verbose_name = _("promotion")
        verbose_name_plural = _("promotions")
        ordering = ("-starts_at",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(discount_value__gt=0),
                name="engagement_promotion_value_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_type="fixed") | models.Q(discount_value__lte=100),
                name="engagement_promotion_percent_at_most_100",
            ),
            models.CheckConstraint(
                condition=models.Q(starts_at__lt=models.F("ends_at")),
                name="engagement_promotion_window_ordered",
            ),
            models.CheckConstraint(
                condition=models.Q(usage_limit__isnull=True)
                | models.Q(used_count__lte=models.F("usage_limit")),
                name="engagement_promotion_used_within_limit",
            ),
            models.CheckConstraint(
                condition=models.Q(per_user_limit__isnull=True) | models.Q(per_user_limit__gt=0),
                name="engagement_promotion_per_user_limit_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(min_order_amount__gte=0),
                name="engagement_promotion_min_order_nonnegative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.discount_label})"

    @property
    def discount_label(self) -> str:
        """Human summary for admin lists and checkout messages, currency-symbol-free.

        ``normalize`` strips trailing zeros without going through scientific notation
        (``f"{Decimal('10.00'):g}"`` keeps the zeros; ``:g`` on a Decimal is not ``:g`` on a
        float), so a 10% promotion reads "10% off", not "10.00% off".
        """
        value = self.discount_value.normalize()
        if self.discount_type == self.DiscountType.PERCENTAGE:
            return f"{value:f}% off"
        return f"{value:f} off"


class PromotionUsage(TimestampedModel):
    """One promotion applied to one order -- the auditable side of ``used_count``."""

    promotion = models.ForeignKey(
        Promotion,
        on_delete=models.PROTECT,
        related_name="usages",
        verbose_name=_("promotion"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="promotion_usages",
        verbose_name=_("user"),
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.PROTECT,
        related_name="promotion_usages",
        verbose_name=_("order"),
        help_text=_("The order the discount was granted on; unique per promotion."),
    )
    discount_amount = models.DecimalField(
        _("discount amount"),
        max_digits=10,
        decimal_places=2,
        help_text=_("What the promotion actually took off this order's eligible subtotal."),
    )

    class Meta:
        verbose_name = _("promotion usage")
        verbose_name_plural = _("promotion usages")
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["promotion", "order"],
                name="engagement_one_usage_per_promotion_and_order",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_amount__gt=0),
                name="engagement_promotion_usage_amount_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["promotion", "user"], name="engagement_usage_promo_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.promotion.code} on {self.order.number}: -{self.discount_amount}"
