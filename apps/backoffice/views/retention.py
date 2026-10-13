"""Backoffice views for Customer Retention & Repeat-Purchase Intelligence (Phase 49).

Handles the retention executive dashboard, explainable segments explorer,
individual customer retention profiles, campaign management, preview, and safe CSV export.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.backoffice.permissions import (
    CAMPAIGNS_MANAGE,
    RETENTION_MANAGE,
    RETENTION_VIEW,
    backoffice_access,
)
from apps.backoffice.services import audit
from apps.backoffice.views.base import render_bo
from apps.engagement.models.promotion import Promotion
from apps.engagement.models.retention import (
    CampaignObjective,
    CampaignRecipient,
    CampaignStatus,
    CustomerSegmentChoice,
    RecipientStatus,
    RetentionCampaign,
)
from apps.engagement.selectors.retention import (
    get_campaigns_queryset,
    get_cohort_retention_matrix,
    get_customer_purchase_data,
    get_customer_retention_profile,
    get_customer_segments_summary,
    get_retention_overview_metrics,
    get_retention_recommendations,
)
from apps.engagement.services.retention import (
    ExpiredPromotionError,
    IneligibleCampaignStateError,
    RetentionCampaignError,
    cancel_retention_campaign,
    create_retention_campaign,
    launch_retention_campaign,
    preview_campaign_recipients,
    update_retention_campaign,
)


@backoffice_access(RETENTION_VIEW)
def retention_dashboard(request):
    """Executive dashboard for Customer Retention & Repeat-Purchase Intelligence."""
    metrics = get_retention_overview_metrics()
    cohort_matrix = get_cohort_retention_matrix(max_cohorts=6)
    segments_summary = get_customer_segments_summary()
    recommendations = get_retention_recommendations(limit=6)
    recent_campaigns = get_campaigns_queryset()[:5]

    return render_bo(
        request,
        "backoffice/retention/dashboard.html",
        active="retention",
        metrics=metrics,
        cohort_matrix=cohort_matrix,
        segments_summary=segments_summary,
        recommendations=recommendations,
        recent_campaigns=recent_campaigns,
        segment_choices=CustomerSegmentChoice.choices,
    )


@backoffice_access(RETENTION_VIEW)
def retention_segments_view(request):
    """Customer directory filterable by explainable retention segments."""
    selected_segment = request.GET.get("segment", "all")
    q = request.GET.get("q", "").strip().lower()

    all_data = get_customer_purchase_data()

    # Filter by segment
    filtered_data = []
    for c in all_data:
        if selected_segment != "all" and selected_segment not in c["segments"]:
            continue
        if q:
            email = c["email"].lower()
            name = c["user"].get_full_name().lower()
            if q not in email and q not in name:
                continue
        filtered_data.append(c)

    # Sort
    sort_key = request.GET.get("sort", "-net_spend")
    reverse = sort_key.startswith("-")
    field = sort_key.lstrip("-")
    if field in ("net_spend", "orders_count", "days_since_last_order", "avg_order_value"):
        filtered_data.sort(key=lambda x: x.get(field, 0), reverse=reverse)

    paginator = Paginator(filtered_data, 20)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    return render_bo(
        request,
        "backoffice/retention/segments.html",
        active="retention",
        page=page_obj,
        total_count=len(filtered_data),
        selected_segment=selected_segment,
        segment_choices=CustomerSegmentChoice.choices,
        q=q,
        sort=sort_key,
    )


@backoffice_access(RETENTION_VIEW)
def retention_customer_detail_view(request, pk: int):
    """Detailed retention profile and personalized context for one customer."""
    profile_data = get_customer_retention_profile(user_id=pk)
    if not profile_data:
        raise Http404("Customer has no completed purchases or does not exist.")

    return render_bo(
        request,
        "backoffice/retention/customer_detail.html",
        active="retention",
        profile=profile_data,
    )


@backoffice_access(RETENTION_VIEW)
def retention_campaigns_list_view(request):
    """List of retention campaigns with performance attribution indicators."""
    status_filter = request.GET.get("status", "")
    campaigns = get_campaigns_queryset()

    if status_filter:
        campaigns = campaigns.filter(status=status_filter)

    paginator = Paginator(campaigns, 15)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    return render_bo(
        request,
        "backoffice/retention/campaign_list.html",
        active="retention",
        page=page_obj,
        status_filter=status_filter,
        status_choices=CampaignStatus.choices,
    )


@backoffice_access(CAMPAIGNS_MANAGE)
def retention_campaign_create_view(request):
    """Creates a new retention campaign."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        objective = request.POST.get("objective", CampaignObjective.WIN_BACK)
        target_segment = request.POST.get("target_segment", CustomerSegmentChoice.AT_RISK)
        message_subject = request.POST.get("message_subject", "").strip()
        message_body = request.POST.get("message_body", "").strip()
        promo_id = request.POST.get("promotion_id")
        attribution_days = int(request.POST.get("attribution_window_days", 14) or 14)
        internal_notes = request.POST.get("internal_notes", "").strip()

        promo = None
        if promo_id:
            promo = Promotion.objects.filter(pk=promo_id).first()

        try:
            campaign = create_retention_campaign(
                name=name,
                objective=objective,
                target_segment=target_segment,
                message_subject=message_subject,
                message_body=message_body,
                promotion=promo,
                attribution_window_days=attribution_days,
                created_by=request.user,
                internal_notes=internal_notes,
            )
            audit.record(
                action="retention_campaign.create",
                actor=request.user,
                target_id=campaign.pk,
                target_label=campaign.name,
                meta={"objective": campaign.objective, "segment": campaign.target_segment},
            )
            messages.success(request, _("Retention campaign created successfully."))
            return redirect("backoffice:retention_campaign_detail", pk=campaign.pk)
        except Exception as e:
            messages.error(request, _("Failed to create campaign: %(err)s") % {"err": str(e)})

    promotions = Promotion.objects.filter(is_active=True, ends_at__gt=timezone.now()).order_by("-created_at")
    return render_bo(
        request,
        "backoffice/retention/campaign_form.html",
        active="retention",
        campaign=None,
        objectives=CampaignObjective.choices,
        segments=CustomerSegmentChoice.choices,
        promotions=promotions,
    )


@backoffice_access(RETENTION_VIEW)
def retention_campaign_detail_view(request, pk: int):
    """Details, recipient eligibility preview, and attribution performance of a campaign."""
    campaign = get_object_or_404(
        RetentionCampaign.objects.select_related("promotion", "created_by"),
        pk=pk,
    )

    preview = None
    recipients_qs = campaign.recipients.select_related("user", "attributed_order").order_by("-created_at")

    if campaign.status in (CampaignStatus.DRAFT, CampaignStatus.REVIEW, CampaignStatus.SCHEDULED):
        preview = preview_campaign_recipients(campaign)
    else:
        # Show actual recipients
        preview = {
            "campaign": campaign,
            "total_evaluated": recipients_qs.count(),
            "eligible_count": recipients_qs.filter(status=RecipientStatus.SENT).count(),
            "excluded_count": recipients_qs.exclude(status__in=[RecipientStatus.SENT, RecipientStatus.ATTRIBUTED]).count(),
            "recipients": recipients_qs[:100],
        }

    # Aggregate attribution stats
    total_attributed_revenue = sum(
        (r.attributed_revenue for r in recipients_qs if r.status == RecipientStatus.ATTRIBUTED),
        Decimal("0.00"),
    )
    attributed_orders_count = recipients_qs.filter(status=RecipientStatus.ATTRIBUTED).count()

    return render_bo(
        request,
        "backoffice/retention/campaign_detail.html",
        active="retention",
        campaign=campaign,
        preview=preview,
        total_attributed_revenue=total_attributed_revenue,
        attributed_orders_count=attributed_orders_count,
    )


@backoffice_access(CAMPAIGNS_MANAGE)
def retention_campaign_edit_view(request, pk: int):
    """Edits an existing retention campaign while in editable status."""
    campaign = get_object_or_404(RetentionCampaign, pk=pk)
    if not campaign.is_editable:
        messages.error(request, _("Cannot edit a campaign in '%(status)s' status.") % {"status": campaign.status})
        return redirect("backoffice:retention_campaign_detail", pk=campaign.pk)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        objective = request.POST.get("objective", campaign.objective)
        target_segment = request.POST.get("target_segment", campaign.target_segment)
        message_subject = request.POST.get("message_subject", "").strip()
        message_body = request.POST.get("message_body", "").strip()
        promo_id = request.POST.get("promotion_id")
        attribution_days = int(request.POST.get("attribution_window_days", 14) or 14)
        internal_notes = request.POST.get("internal_notes", "").strip()

        promo = None
        if promo_id:
            promo = Promotion.objects.filter(pk=promo_id).first()

        try:
            update_retention_campaign(
                campaign,
                name=name,
                objective=objective,
                target_segment=target_segment,
                message_subject=message_subject,
                message_body=message_body,
                promotion=promo,
                attribution_window_days=attribution_days,
                internal_notes=internal_notes,
            )
            audit.record(
                action="retention_campaign.update",
                actor=request.user,
                target_id=campaign.pk,
                target_label=campaign.name,
                meta={"objective": campaign.objective, "segment": campaign.target_segment},
            )
            messages.success(request, _("Campaign updated successfully."))
            return redirect("backoffice:retention_campaign_detail", pk=campaign.pk)
        except Exception as e:
            messages.error(request, _("Failed to update campaign: %(err)s") % {"err": str(e)})

    promotions = Promotion.objects.filter(is_active=True, ends_at__gt=timezone.now()).order_by("-created_at")
    return render_bo(
        request,
        "backoffice/retention/campaign_form.html",
        active="retention",
        campaign=campaign,
        objectives=CampaignObjective.choices,
        segments=CustomerSegmentChoice.choices,
        promotions=promotions,
    )


@backoffice_access(CAMPAIGNS_MANAGE)
def retention_campaign_action_view(request, pk: int):
    """Executes state machine actions on a campaign (launch, cancel)."""
    if request.method != "POST":
        raise Http404("POST only.")

    campaign = get_object_or_404(RetentionCampaign, pk=pk)
    action = request.POST.get("action")

    if action == "launch":
        try:
            launch_retention_campaign(campaign, actor=request.user)
            audit.record(
                action="retention_campaign.launch",
                actor=request.user,
                target_id=campaign.pk,
                target_label=campaign.name,
                meta={"recipients_count": campaign.recipients.count()},
            )
            messages.success(request, _("Campaign '%(name)s' launched successfully!") % {"name": campaign.name})
        except ExpiredPromotionError as e:
            messages.error(request, str(e))
        except IneligibleCampaignStateError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, _("Error launching campaign: %(err)s") % {"err": str(e)})

    elif action == "cancel":
        reason = request.POST.get("cancel_reason", "Cancelled by authorized staff")
        try:
            cancel_retention_campaign(campaign, actor=request.user, reason=reason)
            audit.record(
                action="retention_campaign.cancel",
                actor=request.user,
                target_id=campaign.pk,
                target_label=campaign.name,
                meta={"reason": reason},
            )
            messages.success(request, _("Campaign '%(name)s' was cancelled.") % {"name": campaign.name})
        except Exception as e:
            messages.error(request, _("Error cancelling campaign: %(err)s") % {"err": str(e)})

    return redirect("backoffice:retention_campaign_detail", pk=campaign.pk)


@backoffice_access(RETENTION_VIEW)
def retention_export_csv_view(request):
    """Exports customer retention summary with CWE-1236 CSV injection mitigation."""
    selected_segment = request.GET.get("segment", "all")
    all_data = get_customer_purchase_data()

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="flashwear_retention_{timezone.localdate().isoformat()}.csv"'

    writer = csv.writer(response)
    # Header
    writer.writerow([
        "Customer ID",
        "Completed Orders",
        "First Order Date",
        "Last Order Date",
        "Days Inactive",
        "Average Interval (Days)",
        "Net Spend",
        "Average Order Value",
        "Segments",
    ])

    for c in all_data:
        if selected_segment != "all" and selected_segment not in c["segments"]:
            continue

        # Formula injection mitigation
        def sanitize(val):
            s = str(val or "")
            if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
                return f"'{s}"
            return s

        writer.writerow([
            sanitize(c["user_id"]),
            sanitize(c["orders_count"]),
            sanitize(c["first_order_date"].isoformat() if c["first_order_date"] else ""),
            sanitize(c["last_order_date"].isoformat() if c["last_order_date"] else ""),
            sanitize(c["days_since_last_order"]),
            sanitize(c["avg_interval_days"] if c["avg_interval_days"] is not None else "N/A"),
            sanitize(f"{c['net_spend']:.2f}"),
            sanitize(f"{c['avg_order_value']:.2f}"),
            sanitize("; ".join(c["segments"])),
        ])

    return response
