"""Content Studio Back Office views (Phase 29).

Allows authorized staff to visually manage:
- Homepage sections (Hero, Featured Collections, Stories, Moods, Drops, etc.)
- Editorial fashion stories (e.g. After Dark)
- Seasonal marketing campaigns (e.g. Autumn Edit)
- Desktop and mobile preview simulator
"""

from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.backoffice.forms_content import (
    CampaignForm,
    EditorialStoryForm,
    HomepageSectionForm,
)
from apps.backoffice.models import AuditEvent
from apps.backoffice.permissions import (
    MARKETING_MANAGE,
    MARKETING_VIEW,
    backoffice_access,
    has,
)
from apps.backoffice.services.audit import record as record_audit
from apps.backoffice.views.base import render_bo
from apps.core.models import Campaign, EditorialStory, HomepageSection
from apps.core.services.content import (
    get_active_campaigns,
    get_active_editorial_stories,
    get_active_homepage_sections,
)

__all__ = [
    "content_campaign_delete",
    "content_campaign_edit",
    "content_campaigns",
    "content_editorial",
    "content_editorial_delete",
    "content_editorial_edit",
    "content_homepage",
    "content_homepage_delete",
    "content_homepage_edit",
    "content_homepage_toggle",
    "content_preview",
]


@backoffice_access(MARKETING_VIEW)
def content_homepage(request):
    """``/operations/content/homepage/`` -- manage visual homepage sections."""
    sections = (
        HomepageSection.objects.all()
        .select_related("campaign", "linked_collection", "linked_category", "linked_drop")
        .prefetch_related("section_products")
        .order_by("display_order", "id")
    )

    q = request.GET.get("q", "").strip()
    if q:
        sections = sections.filter(title__icontains=q)

    sec_type = request.GET.get("type", "").strip()
    if sec_type:
        sections = sections.filter(section_type=sec_type)

    paginator = Paginator(sections, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    can_manage = has(request.user, MARKETING_MANAGE)
    now = timezone.now()

    return render_bo(
        request,
        "backoffice/content/homepage_list.html",
        active="content_homepage",
        page=page_obj,
        sections=page_obj.object_list,
        total_count=sections.count(),
        can_manage=can_manage,
        now=now,
        type_choices=HomepageSection.SectionType.choices,
        filters={"q": q, "type": sec_type},
    )


@backoffice_access(MARKETING_VIEW)
def content_homepage_edit(request, pk=None):
    """Create or edit a homepage section and its hero/visual styling."""
    instance = get_object_or_404(HomepageSection, pk=pk) if pk else None
    can_manage = has(request.user, MARKETING_MANAGE)

    if request.method == "POST":
        if not can_manage:
            raise Http404

        form = HomepageSectionForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            section = form.save()
            action = "content.section.update" if instance else "content.section.create"
            record_audit(
                actor=request.user,
                domain=AuditEvent.Domain.MARKETING,
                action=action,
                object_type="HomepageSection",
                object_id=str(section.pk),
                object_repr=str(section),
                reason=f"{'Updated' if instance else 'Created'} section '{section.title}'",
            )
            messages.success(request, f"Homepage section '{section.title}' saved successfully.")
            return redirect("backoffice:content_homepage")
        messages.error(request, "Please correct the errors below.")
    else:
        form = HomepageSectionForm(instance=instance)

    return render_bo(
        request,
        "backoffice/content/homepage_form.html",
        active="content_homepage",
        form=form,
        section=instance,
        can_manage=can_manage,
    )


@require_POST
@backoffice_access(MARKETING_MANAGE)
def content_homepage_toggle(request, pk):
    """Quick enable/disable switch for a homepage section."""
    section = get_object_or_404(HomepageSection, pk=pk)
    section.is_enabled = not section.is_enabled
    section.save()

    record_audit(
        actor=request.user,
        domain=AuditEvent.Domain.MARKETING,
        action="content.section.toggle",
        object_type="HomepageSection",
        object_id=str(section.pk),
        object_repr=str(section),
        reason=f"Toggled section to {'enabled' if section.is_enabled else 'disabled'}",
    )
    messages.success(
        request,
        f"Section '{section.title}' {'enabled' if section.is_enabled else 'disabled'}.",
    )
    return redirect("backoffice:content_homepage")


@require_POST
@backoffice_access(MARKETING_MANAGE)
def content_homepage_delete(request, pk):
    """Remove a homepage section."""
    section = get_object_or_404(HomepageSection, pk=pk)
    title = section.title
    sec_pk = section.pk
    section.delete()

    record_audit(
        actor=request.user,
        domain=AuditEvent.Domain.MARKETING,
        action="content.section.delete",
        object_type="HomepageSection",
        object_id=str(sec_pk),
        object_repr=title,
        reason=f"Deleted homepage section '{title}'",
    )
    messages.success(request, f"Homepage section '{title}' removed.")
    return redirect("backoffice:content_homepage")


@backoffice_access(MARKETING_VIEW)
def content_preview(request):
    """``/operations/content/preview/`` -- Desktop/mobile preview simulator."""
    device = request.GET.get("device", "desktop")
    if device not in ("desktop", "mobile"):
        device = "desktop"

    show_unpublished = request.GET.get("unpublished", "1") == "1"
    sections = get_active_homepage_sections(include_unpublished=show_unpublished)
    stories = get_active_editorial_stories(include_unpublished=show_unpublished)
    campaigns = get_active_campaigns()

    return render_bo(
        request,
        "backoffice/content/preview.html",
        active="content_homepage",
        device=device,
        sections=sections,
        stories=stories,
        campaigns=campaigns,
        show_unpublished=show_unpublished,
    )


@backoffice_access(MARKETING_VIEW)
def content_editorial(request):
    stories = (
        EditorialStory.objects.all()
        .select_related("campaign", "linked_collection", "linked_category")
        .order_by("display_order", "-created_at")
    )


    q = request.GET.get("q", "").strip()
    if q:
        stories = stories.filter(title__icontains=q)

    paginator = Paginator(stories, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    can_manage = has(request.user, MARKETING_MANAGE)
    now = timezone.now()

    return render_bo(
        request,
        "backoffice/content/editorial_list.html",
        active="content_editorial",
        page=page_obj,
        stories=page_obj.object_list,
        total_count=stories.count(),
        can_manage=can_manage,
        now=now,
        filters={"q": q},
    )


@backoffice_access(MARKETING_VIEW)
def content_editorial_edit(request, pk=None):
    """Create or edit an editorial fashion story."""
    instance = get_object_or_404(EditorialStory, pk=pk) if pk else None
    can_manage = has(request.user, MARKETING_MANAGE)

    if request.method == "POST":
        if not can_manage:
            raise Http404

        form = EditorialStoryForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            story = form.save()
            action = "content.editorial.update" if instance else "content.editorial.create"
            record_audit(
                actor=request.user,
                domain=AuditEvent.Domain.MARKETING,
                action=action,
                object_type="EditorialStory",
                object_id=str(story.pk),
                object_repr=str(story),
                reason=f"{'Updated' if instance else 'Created'} editorial story '{story.title}'",
            )
            messages.success(request, f"Editorial story '{story.title}' saved successfully.")
            return redirect("backoffice:content_editorial")
        messages.error(request, "Please correct the errors below.")
    else:
        form = EditorialStoryForm(instance=instance)

    return render_bo(
        request,
        "backoffice/content/editorial_form.html",
        active="content_editorial",
        form=form,
        story=instance,
        can_manage=can_manage,
    )


@require_POST
@backoffice_access(MARKETING_MANAGE)
def content_editorial_delete(request, pk):
    """Remove an editorial story."""
    story = get_object_or_404(EditorialStory, pk=pk)
    title = story.title
    story_pk = story.pk
    story.delete()

    record_audit(
        actor=request.user,
        domain=AuditEvent.Domain.MARKETING,
        action="content.editorial.delete",
        object_type="EditorialStory",
        object_id=str(story_pk),
        object_repr=title,
        reason=f"Deleted editorial story '{title}'",
    )
    messages.success(request, f"Editorial story '{title}' removed.")
    return redirect("backoffice:content_editorial")


@backoffice_access(MARKETING_VIEW)
def content_campaigns(request):
    """``/operations/content/campaigns/`` -- list seasonal marketing campaigns."""
    campaigns = Campaign.objects.all().order_by("-starts_at")

    q = request.GET.get("q", "").strip()
    if q:
        campaigns = campaigns.filter(title__icontains=q)

    paginator = Paginator(campaigns, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    can_manage = has(request.user, MARKETING_MANAGE)
    now = timezone.now()

    return render_bo(
        request,
        "backoffice/content/campaign_list.html",
        active="content_campaigns",
        page=page_obj,
        campaigns=page_obj.object_list,
        total_count=campaigns.count(),
        can_manage=can_manage,
        now=now,
        filters={"q": q},
    )


@backoffice_access(MARKETING_VIEW)
def content_campaign_edit(request, pk=None):
    """Create or edit a seasonal campaign."""
    instance = get_object_or_404(Campaign, pk=pk) if pk else None
    can_manage = has(request.user, MARKETING_MANAGE)

    if request.method == "POST":
        if not can_manage:
            raise Http404

        form = CampaignForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            campaign = form.save()
            action = "content.campaign.update" if instance else "content.campaign.create"
            record_audit(
                actor=request.user,
                domain=AuditEvent.Domain.MARKETING,
                action=action,
                object_type="Campaign",
                object_id=str(campaign.pk),
                object_repr=str(campaign),
                reason=f"{'Updated' if instance else 'Created'} campaign '{campaign.title}'",
            )
            messages.success(request, f"Campaign '{campaign.title}' saved successfully.")
            return redirect("backoffice:content_campaigns")
        messages.error(request, "Please correct the errors below.")
    else:
        form = CampaignForm(instance=instance)

    return render_bo(
        request,
        "backoffice/content/campaign_form.html",
        active="content_campaigns",
        form=form,
        campaign=instance,
        can_manage=can_manage,
    )


@require_POST
@backoffice_access(MARKETING_MANAGE)
def content_campaign_delete(request, pk):
    """Remove a campaign."""
    campaign = get_object_or_404(Campaign, pk=pk)
    title = campaign.title
    camp_pk = campaign.pk
    campaign.delete()

    record_audit(
        actor=request.user,
        domain=AuditEvent.Domain.MARKETING,
        action="content.campaign.delete",
        object_type="Campaign",
        object_id=str(camp_pk),
        object_repr=title,
        reason=f"Deleted campaign '{title}'",
    )
    messages.success(request, f"Campaign '{title}' removed.")
    return redirect("backoffice:content_campaigns")
