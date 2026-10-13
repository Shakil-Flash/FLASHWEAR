"""Customer Retention & Repeat-Purchase Intelligence models (Phase 49).

Provides data models for:
1. Retention Campaigns: Target segment campaigns with message templates and optional promotion links.
2. Campaign Recipients: Individual recipient tracking, marketing consent enforcement,
   idempotent delivery records, and 14-day attribution tracking.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel


class CampaignObjective(models.TextChoices):
    WIN_BACK = "win_back", _("Win-Back Dormant Customers")
    REPEAT_PURCHASE = "repeat_purchase", _("Second Purchase Incentive")
    LOYALTY_APPRECIATION = "loyalty_appreciation", _("Loyalty & VIP Appreciation")
    CROSS_SELL = "cross_sell", _("Cross-Category Complementary")
    WELCOME_BACK = "welcome_back", _("Welcome Back Active Customers")


class CustomerSegmentChoice(models.TextChoices):
    ALL_CUSTOMERS = "all", _("All Purchasing Customers")
    NEW_CUSTOMERS = "new", _("New Customers (1 Order)")
    REPEAT_CUSTOMERS = "repeat", _("Repeat Customers (2+ Orders)")
    RECENTLY_ACTIVE = "active", _("Recently Active (≤30 Days)")
    LOYAL_CUSTOMERS = "loyal", _("Loyal / High Frequency (3+ Orders)")
    HIGH_SPENDERS = "high_spenders", _("High Spenders (VIP)")
    AT_RISK = "at_risk", _("At-Risk / Slipping (Lapsed Normal Interval)")
    CHURNED = "churned", _("Churned / Dormant (>120 Days Inactive)")


class CampaignStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    REVIEW = "review", _("Under Review")
    SCHEDULED = "scheduled", _("Scheduled")
    ACTIVE = "active", _("Active")
    COMPLETED = "completed", _("Completed")
    CANCELLED = "cancelled", _("Cancelled")


class RecipientStatus(models.TextChoices):
    ELIGIBLE = "eligible", _("Eligible")
    EXCLUDED_UNSUBSCRIBED = "excluded_unsubscribed", _("Excluded: Marketing Opt-Out")
    EXCLUDED_INACTIVE = "excluded_inactive", _("Excluded: Inactive / Closed Account")
    EXCLUDED_NO_EMAIL = "excluded_no_email", _("Excluded: Invalid Email Address")
    EXCLUDED_COOLDOWN = "excluded_cooldown", _("Excluded: In Re-engagement Cooldown")
    SENT = "sent", _("Sent")
    DELIVERED = "delivered", _("Delivered")
    ATTRIBUTED = "attributed", _("Attributed Order")
    SKIPPED = "skipped", _("Skipped")


class RetentionCampaign(TimestampedModel):
    """A structured marketing campaign targeting a customer retention segment."""

    name = models.CharField(_("campaign name"), max_length=120)
    slug = models.SlugField(_("slug"), max_length=140, unique=True)
    objective = models.CharField(
        _("objective"),
        max_length=32,
        choices=CampaignObjective.choices,
        default=CampaignObjective.WIN_BACK,
    )
    target_segment = models.CharField(
        _("target segment"),
        max_length=32,
        choices=CustomerSegmentChoice.choices,
        default=CustomerSegmentChoice.AT_RISK,
    )
    status = models.CharField(
        _("status"),
        max_length=24,
        choices=CampaignStatus.choices,
        default=CampaignStatus.DRAFT,
        db_index=True,
    )
    message_subject = models.CharField(_("message subject"), max_length=150)
    message_body = models.TextField(_("message body / template"))
    
    # Optional promotion integration (reuses existing engagement.Promotion engine)
    promotion = models.ForeignKey(
        "engagement.Promotion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retention_campaigns",
        verbose_name=_("associated promotion"),
        help_text=_("Optional promo code included with this campaign."),
    )

    starts_at = models.DateTimeField(_("starts at"), null=True, blank=True)
    ends_at = models.DateTimeField(_("ends at"), null=True, blank=True)
    
    # Attribution window (in days) - default 14 days post dispatch
    attribution_window_days = models.PositiveIntegerField(
        _("attribution window (days)"),
        default=14,
        help_text=_("Days following dispatch where orders by recipient can be attributed."),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_retention_campaigns",
        verbose_name=_("created by"),
    )
    internal_notes = models.TextField(_("internal notes"), blank=True)

    class Meta:
        verbose_name = _("retention campaign")
        verbose_name_plural = _("retention campaigns")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.name} ({self.get_status_display()})"

    @property
    def is_editable(self) -> bool:
        return self.status in (CampaignStatus.DRAFT, CampaignStatus.REVIEW)

    @property
    def is_active(self) -> bool:
        return self.status == CampaignStatus.ACTIVE


class CampaignRecipient(TimestampedModel):
    """An individual recipient evaluated or contacted as part of a RetentionCampaign.
    
    Idempotency and consent enforcement:
    - (campaign, user) is UNIQUE to guarantee no duplicate messages.
    - Captures status transitions: ELIGIBLE -> SENT -> ATTRIBUTED.
    - Exclusions clearly document why marketing was withheld (opt-out, cooldown, inactive).
    """

    campaign = models.ForeignKey(
        RetentionCampaign,
        on_delete=models.CASCADE,
        related_name="recipients",
        verbose_name=_("campaign"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="retention_campaign_records",
        verbose_name=_("user"),
    )
    status = models.CharField(
        _("status"),
        max_length=32,
        choices=RecipientStatus.choices,
        default=RecipientStatus.ELIGIBLE,
        db_index=True,
    )
    exclusion_reason = models.CharField(
        _("exclusion reason"),
        max_length=120,
        blank=True,
    )
    dispatched_at = models.DateTimeField(
        _("dispatched at"),
        null=True,
        blank=True,
    )
    
    # 14-day Attribution
    attributed_order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retention_attributions",
        verbose_name=_("attributed order"),
    )
    attributed_at = models.DateTimeField(
        _("attributed at"),
        null=True,
        blank=True,
    )
    attributed_revenue = models.DecimalField(
        _("attributed revenue"),
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    promotion_used = models.BooleanField(
        _("promotion used"),
        default=False,
    )

    idempotency_key = models.CharField(
        _("idempotency key"),
        max_length=100,
        unique=True,
        help_text=_("Deterministic key preventing duplicate records: campaign:{id}:user:{id}"),
    )

    class Meta:
        verbose_name = _("campaign recipient")
        verbose_name_plural = _("campaign recipients")
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "user"],
                name="engagement_campaign_recipient_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.campaign.name} -> {self.user.email} [{self.status}]"
