"""Notification delivery screens for the Back Office (Phase 17 §22).

Three routes under ``/operations/notifications/``:

* the list -- the nine inspection fields plus the §23 metric strip, filters allowlisted
  by the selector, paged like every other screen. Bodies are never rendered here;
* the detail -- one row *with* its title/body, gated by the same capability, so content
  is reachable only by staff who were granted the notifications capability explicitly;
* the retry -- POST-only, authorized by ``NOTIFICATIONS_MANAGE``, and idempotent: the
  service only moves a row that is ``failed`` right now, so double-clicking, a stale page
  or two operators at once cannot queue a second delivery.
"""

from __future__ import annotations

import json

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from apps.backoffice import selectors
from apps.backoffice.permissions import NOTIFICATIONS_MANAGE, NOTIFICATIONS_VIEW, backoffice_access
from apps.backoffice.views.base import range_from_request, render_bo
from apps.notifications.services import preferences
from apps.notifications.services.delivery_retry import retry_failed_notification

__all__ = ["notification_detail", "notification_retry", "notifications"]


@backoffice_access(NOTIFICATIONS_VIEW)
def notifications(request):
    """``/operations/notifications/`` -- delivery inspection with the metrics strip."""
    date_range = range_from_request(request)
    q = request.GET.get("q", "")
    queryset = selectors.notifications(
        status=request.GET.get("status", ""),
        channel=request.GET.get("channel", ""),
        notification_type=request.GET.get("type", ""),
        failed_only=request.GET.get("failed") == "1",
        sort=request.GET.get("sort", ""),
        q=q,
    )
    queryset = queryset.filter(**date_range.as_query)
    return render_bo(
        request,
        "backoffice/notifications.html",
        active="notifications",
        page=selectors.paginate(request, queryset),
        metrics=selectors.notification_metrics(),
        filters={
            "status": request.GET.get("status", ""),
            "channel": request.GET.get("channel", ""),
            "type": request.GET.get("type", ""),
            "failed": request.GET.get("failed", ""),
            "q": q,
        },
        status_choices=selectors.STATUS_CHOICES,
        channel_choices=selectors.CHANNEL_CHOICES,
        type_choices=selectors.TYPE_CHOICES,
        can_retry=_can_retry(request.user),
    )


def _can_retry(user) -> bool:
    from apps.backoffice.permissions import has

    return has(user, NOTIFICATIONS_MANAGE)


@backoffice_access(NOTIFICATIONS_VIEW)
def notification_detail(request, pk: int):
    """``/operations/notifications/<pk>/`` -- one row with its content, consent, and audit events."""
    row = selectors.notification_detail(pk)
    if row is None:
        raise Http404
    
    events = selectors.notification_delivery_events(pk)
    user = row.user
    consent_info = {
        "marketing_email": preferences.marketing_subscribed(user),
        "marketing_sms": preferences.marketing_sms_subscribed(user),
        "marketing_push": preferences.marketing_push_subscribed(user),
        "is_active": user.is_active,
    }

    return render_bo(
        request,
        "backoffice/notification_detail.html",
        active="notifications",
        row=row,
        events=events,
        consent_info=consent_info,
        metadata_json=json.dumps(row.safe_metadata, indent=2, default=str),
        can_retry=_can_retry(request.user),
    )


@backoffice_access(NOTIFICATIONS_MANAGE)
@require_POST
def notification_retry(request, pk: int):
    """Re-queue one failed notification safely without bypassing consent or safety checks."""
    success, msg = retry_failed_notification(pk)
    if success:
        messages.success(request, msg)
    else:
        messages.warning(request, msg)
    return redirect("backoffice:notifications")
