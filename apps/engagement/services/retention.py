"""Customer Retention & Repeat-Purchase Intelligence services (Phase 49).

Handles retention campaign lifecycle, consent-enforced recipient evaluation,
idempotent message dispatches, and multi-day order attribution.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.engagement.models.retention import (
    CampaignObjective,
    CampaignRecipient,
    CampaignStatus,
    CustomerSegmentChoice,
    RecipientStatus,
    RetentionCampaign,
)
from apps.engagement.selectors.retention import get_customer_purchase_data
from apps.notifications.services.preferences import (
    marketing_subscribed,
    user_can_receive_email,
)
from apps.orders.models import Order

logger = logging.getLogger("flashwear.engagement.retention")


class RetentionCampaignError(Exception):
    """Base exception for retention campaign operations."""


class IneligibleCampaignStateError(RetentionCampaignError):
    """Raised when an invalid state transition is attempted."""


class ExpiredPromotionError(RetentionCampaignError):
    """Raised when launching a campaign with an expired or inactive promotion."""


def create_retention_campaign(
    *,
    name: str,
    objective: str,
    target_segment: str,
    message_subject: str,
    message_body: str,
    promotion=None,
    starts_at=None,
    ends_at=None,
    attribution_window_days: int = 14,
    created_by=None,
    internal_notes: str = "",
) -> RetentionCampaign:
    """Creates a new retention campaign in DRAFT status."""
    base_slug = slugify(name) or "campaign"
    slug = base_slug
    counter = 1
    while RetentionCampaign.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    campaign = RetentionCampaign(
        name=name,
        slug=slug,
        objective=objective,
        target_segment=target_segment,
        status=CampaignStatus.DRAFT,
        message_subject=message_subject,
        message_body=message_body,
        promotion=promotion,
        starts_at=starts_at,
        ends_at=ends_at,
        attribution_window_days=attribution_window_days,
        created_by=created_by,
        internal_notes=internal_notes,
    )
    campaign.full_clean()
    campaign.save()
    return campaign


def update_retention_campaign(
    campaign: RetentionCampaign,
    *,
    name: str | None = None,
    objective: str | None = None,
    target_segment: str | None = None,
    message_subject: str | None = None,
    message_body: str | None = None,
    promotion=None,
    starts_at=None,
    ends_at=None,
    attribution_window_days: int | None = None,
    internal_notes: str | None = None,
) -> RetentionCampaign:
    """Updates campaign fields if still in editable status."""
    if not campaign.is_editable:
        raise IneligibleCampaignStateError(
            f"Cannot edit campaign in '{campaign.status}' status."
        )

    if name is not None:
        campaign.name = name
    if objective is not None:
        campaign.objective = objective
    if target_segment is not None:
        campaign.target_segment = target_segment
    if message_subject is not None:
        campaign.message_subject = message_subject
    if message_body is not None:
        campaign.message_body = message_body
    if promotion is not None:
        campaign.promotion = promotion
    if starts_at is not None:
        campaign.starts_at = starts_at
    if ends_at is not None:
        campaign.ends_at = ends_at
    if attribution_window_days is not None:
        campaign.attribution_window_days = attribution_window_days
    if internal_notes is not None:
        campaign.internal_notes = internal_notes

    campaign.save()
    return campaign


def preview_campaign_recipients(campaign: RetentionCampaign) -> dict[str, Any]:
    """Evaluates audience segment against marketing consent and account state.
    
    Returns preview summary with list of evaluated recipients.
    Does NOT modify database records.
    """
    all_customer_data = get_customer_purchase_data()
    target_seg = campaign.target_segment

    evaluated: list[dict[str, Any]] = []
    eligible_count = 0
    excluded_count = 0

    for c in all_customer_data:
        # Check segment membership
        if target_seg != CustomerSegmentChoice.ALL_CUSTOMERS and target_seg not in c["segments"]:
            continue

        user = c["user"]
        is_active = getattr(user, "is_active", False)
        is_deactivated = getattr(user, "is_deactivated", False)
        has_email = user_can_receive_email(user)
        consented = marketing_subscribed(user)

        status = RecipientStatus.ELIGIBLE
        exclusion_reason = ""

        if not is_active or is_deactivated:
            status = RecipientStatus.EXCLUDED_INACTIVE
            exclusion_reason = "Account is inactive or closed"
            excluded_count += 1
        elif not has_email:
            status = RecipientStatus.EXCLUDED_NO_EMAIL
            exclusion_reason = "No valid email address on file"
            excluded_count += 1
        elif not consented:
            status = RecipientStatus.EXCLUDED_UNSUBSCRIBED
            exclusion_reason = "Customer has opted out of marketing emails"
            excluded_count += 1
        else:
            eligible_count += 1

        evaluated.append({
            "user": user,
            "email": user.email,
            "orders_count": c["orders_count"],
            "net_spend": c["net_spend"],
            "days_since_last_order": c["days_since_last_order"],
            "status": status,
            "exclusion_reason": exclusion_reason,
        })

    return {
        "campaign": campaign,
        "total_evaluated": len(evaluated),
        "eligible_count": eligible_count,
        "excluded_count": excluded_count,
        "recipients": evaluated,
    }


@transaction.atomic
def launch_retention_campaign(
    campaign: RetentionCampaign,
    *,
    actor=None,
) -> RetentionCampaign:
    """Launches campaign, populating idempotent recipients and enforcing privacy consent."""
    if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.REVIEW, CampaignStatus.SCHEDULED):
        raise IneligibleCampaignStateError(
            f"Cannot launch campaign with status '{campaign.status}'."
        )

    # Validate promotion if attached
    if campaign.promotion:
        now = timezone.now()
        promo = campaign.promotion
        if not promo.is_active or promo.ends_at <= now:
            raise ExpiredPromotionError(
                f"Cannot launch campaign with expired or inactive promotion '{promo.code}'."
            )

    preview = preview_campaign_recipients(campaign)
    recipients_data = preview["recipients"]

    now = timezone.now()

    for item in recipients_data:
        user = item["user"]
        status = item["status"]
        exclusion_reason = item["exclusion_reason"]
        key = f"campaign:{campaign.pk}:user:{user.pk}"

        # If eligible, mark as SENT
        dispatched_at = now if status == RecipientStatus.ELIGIBLE else None
        final_status = RecipientStatus.SENT if status == RecipientStatus.ELIGIBLE else status

        CampaignRecipient.objects.update_or_create(
            campaign=campaign,
            user=user,
            defaults={
                "status": final_status,
                "exclusion_reason": exclusion_reason,
                "dispatched_at": dispatched_at,
                "idempotency_key": key,
            },
        )

    campaign.status = CampaignStatus.ACTIVE
    if not campaign.starts_at:
        campaign.starts_at = now
    campaign.save()

    logger.info(
        "Retention campaign '%s' (ID %d) launched by %s with %d eligible recipients.",
        campaign.name,
        campaign.pk,
        getattr(actor, "email", "system"),
        preview["eligible_count"],
    )

    return campaign


def cancel_retention_campaign(
    campaign: RetentionCampaign,
    *,
    actor=None,
    reason: str = "",
) -> RetentionCampaign:
    """Cancels an active or draft campaign."""
    if campaign.status in (CampaignStatus.COMPLETED, CampaignStatus.CANCELLED):
        raise IneligibleCampaignStateError(
            f"Campaign is already in final status '{campaign.status}'."
        )

    campaign.status = CampaignStatus.CANCELLED
    if reason:
        campaign.internal_notes = f"{campaign.internal_notes}\nCancelled: {reason}".strip()
    campaign.save()

    logger.info(
        "Retention campaign '%s' (ID %d) cancelled by %s. Reason: %s",
        campaign.name,
        campaign.pk,
        getattr(actor, "email", "system"),
        reason,
    )
    return campaign


def attribute_order_to_campaigns(order: Order) -> list[CampaignRecipient]:
    """Inspects a completed order and attributes revenue to active campaign recipients.
    
    Attribution Rules:
    1. Customer must have been dispatched a campaign within its attribution window.
    2. Alternatively, order redeemed the campaign's associated promotion code.
    3. Idempotent: Does not double-attribute if already linked to this order.
    """
    if not order.user_id:
        return []

    now = order.created_at
    attributed_recipients: list[CampaignRecipient] = []

    # Find candidates
    candidates = (
        CampaignRecipient.objects.filter(
            user=order.user,
            status__in=[RecipientStatus.SENT, RecipientStatus.DELIVERED],
        )
        .select_related("campaign__promotion")
        .order_by("-dispatched_at")
    )

    for rec in candidates:
        campaign = rec.campaign
        window_days = campaign.attribution_window_days or 14
        cutoff = now - timedelta(days=window_days)

        is_attributed = False
        promo_used = False

        # Match 1: Dispatched within attribution window
        if rec.dispatched_at and rec.dispatched_at >= cutoff:
            is_attributed = True

        # Match 2: Exact promotion code match
        if campaign.promotion and order.promotion_code:
            if order.promotion_code.upper() == campaign.promotion.code.upper():
                is_attributed = True
                promo_used = True

        if is_attributed:
            rec.status = RecipientStatus.ATTRIBUTED
            rec.attributed_order = order
            rec.attributed_at = now
            rec.attributed_revenue = order.total
            rec.promotion_used = promo_used
            rec.save(update_fields=["status", "attributed_order", "attributed_at", "attributed_revenue", "promotion_used", "updated_at"])
            attributed_recipients.append(rec)
            # Prevent double-attributing across multiple campaigns for the exact same order unless appropriate
            break

    return attributed_recipients
