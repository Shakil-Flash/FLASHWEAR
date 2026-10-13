"""HTML surfaces for the notification center (Phase 17 §29-§31).

Five views, all behind ``login_required`` (the decorator is the security boundary, the
same contract as the support app):

* ``center`` -- the customer's list, newest first, with unread/category filters;
* ``detail`` -- one notification; marks it read on the way past and answers 404 for rows
  belonging to somebody else (filtered query, so a probe is indistinguishable from a miss);
* ``read_all`` -- bulk mark-read, throttled (it is a write loop endpoint);
* ``preferences`` -- the category switches (mounted under ``/account/`` by the account
  URLconf), mandatory rows disabled in the form *and* coerced server-side;
* ``unsubscribe`` -- the opaque-token one-click landing page: no login (the recipient is
  the link), rate-limited per IP, idempotent, and it never reveals whose address it
  changed.
"""

from __future__ import annotations

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.notifications import selectors
from apps.notifications.forms import PreferencesForm
from apps.notifications.models import NotificationCategory
from apps.notifications.services import in_app, preferences
from apps.notifications.throttling import NotificationThrottle

logger = logging.getLogger("flashwear.notifications")

PER_PAGE = 20


def _client_ip(request: HttpRequest) -> str:
    from apps.accounts.throttling import _client_ip as client_ip

    return client_ip(request)


@login_required
@require_http_methods(["GET"])
def center(request: HttpRequest) -> HttpResponse:
    """The notification center: paginated, filterable, badge-accurate."""
    tab = request.GET.get("tab", "inbox")
    is_archived = tab == "archived" or request.GET.get("filter") == "archived"
    unread_only = request.GET.get("filter") == "unread" and not is_archived
    category = request.GET.get("category", "")
    if category not in NotificationCategory.values:
        category = ""

    qs = selectors.user_notifications(
        request.user, unread=unread_only, category=category, dismissed=is_archived
    )
    paginator = Paginator(qs, PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    counts = selectors.status_counts(request.user, include_archived=True)
    # The bell badge (context processor) would otherwise run its own unread COUNT a
    # second time during this same render.
    try:
        request._fw_unread_count = counts["unread"]
    except AttributeError:  # pragma: no cover - synthesised requests
        pass

    return render(
        request,
        "notifications/center.html",
        {
            "page_obj": page_obj,
            "notifications": page_obj.object_list,
            "unread_only": unread_only,
            "active_tab": "archived" if is_archived else "inbox",
            "active_category": category,
            "categories": [(value, label) for value, label in NotificationCategory.choices],
            "counts": counts,
        },
    )


@login_required
@require_http_methods(["GET"])
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    """One notification. Read state flips here -- opening it *is* reading it."""
    from apps.analytics.services import record_event

    notification = selectors.user_notification(request.user, pk)
    if notification is None:
        raise Http404(_("No notification matches the given id."))
    notification.mark_read()
    record_event(
        "notification_opened",
        request=request,
        user=request.user,
        object_type="notification",
        object_id=notification.pk,
        metadata={
            "category": notification.category,
            "type": notification.notification_type,
        },
    )
    return render(
        request,
        "notifications/detail.html",
        {"notification": notification},
    )


@login_required
@require_http_methods(["GET"])
def action_click(request: HttpRequest, pk: int) -> HttpResponse:
    """Track CTA attribution when a customer clicks the related action in a notification."""
    from apps.analytics.services import record_event

    notification = selectors.user_notification(request.user, pk)
    if notification is None:
        raise Http404(_("No notification matches the given id."))

    notification.mark_read()
    record_event(
        "notification_clicked",
        request=request,
        user=request.user,
        object_type="notification",
        object_id=notification.pk,
        metadata={
            "category": notification.category,
            "type": notification.notification_type,
            "action_url": notification.action_url,
        },
    )

    if notification.action_url:
        from django.utils.http import url_has_allowed_host_and_scheme

        if url_has_allowed_host_and_scheme(
            notification.action_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(notification.action_url)
    return redirect("notifications:center")


@login_required
@require_http_methods(["POST"])
def mark_read_action(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark one notification read via POST without navigating away."""
    throttle = NotificationThrottle()
    decision = throttle.check("mark_read", request.user.pk)
    if decision.blocked:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"detail": "Rate limit exceeded."}, status=429)
        messages.warning(request, _("Please wait a moment before marking another notification read."))
        return redirect("notifications:center")

    notification = in_app.mark_read(request.user, pk)
    if notification is None:
        raise Http404(_("No notification matches the given id."))

    throttle.record("mark_read", request.user.pk)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"success": True, "unread_count": in_app.unread_count(request.user)})
    messages.success(request, _("Notification marked as read."))
    return redirect("notifications:center")


@login_required
@require_http_methods(["POST"])
def dismiss(request: HttpRequest, pk: int) -> HttpResponse:
    """Dismiss one in-app notification from the user's active center."""
    throttle = NotificationThrottle()
    decision = throttle.check("dismiss", request.user.pk)
    if decision.blocked:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"detail": "Rate limit exceeded."}, status=429)
        messages.warning(request, _("Please wait a moment before dismissing another notification."))
        return redirect("notifications:center")

    notification = in_app.dismiss(request.user, pk)
    if notification is None:
        raise Http404(_("No notification matches the given id."))

    throttle.record("dismiss", request.user.pk)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"success": True, "unread_count": in_app.unread_count(request.user)})
    messages.success(request, _("Notification dismissed."))
    return redirect("notifications:center")


@login_required
@require_http_methods(["POST"])
def dismiss_all(request: HttpRequest) -> HttpResponse:
    """Dismiss all active in-app notifications from the user's active center."""
    throttle = NotificationThrottle()
    decision = throttle.check("dismiss_all", request.user.pk)
    if decision.blocked:
        messages.warning(request, _("That is a lot of dismissals in a row. Please wait a moment."))
        return redirect("notifications:center")

    count = in_app.dismiss_all(request.user)
    throttle.record("dismiss_all", request.user.pk)
    if count:
        messages.success(request, _("%(count)d notifications dismissed.") % {"count": count})
    return redirect("notifications:center")


@login_required
@require_http_methods(["POST"])
def read_all(request: HttpRequest) -> HttpResponse:
    """Mark everything read. Throttled: it is free, idempotent, and scriptable."""
    throttle = NotificationThrottle()
    decision = throttle.check("read_all", request.user.pk)
    if decision.blocked:
        messages.warning(
            request, _("That is a lot of marking-read in a row. Please wait a moment.")
        )
        return redirect("notifications:center")
    count = in_app.mark_all_read(request.user)
    throttle.record("read_all", request.user.pk)
    if count:
        messages.success(request, _("%(count)d notifications marked as read.") % {"count": count})
    return redirect("notifications:center")


@require_http_methods(["GET", "POST"])
def preferences_view(request: HttpRequest) -> HttpResponse:
    """Category switches for the signed-in customer (mounted at /account/notifications/
    behind ``login_required`` by the account URLconf, which owns that namespace)."""
    throttle = NotificationThrottle()
    if request.method == "POST":
        decision = throttle.check("preferences", request.user.pk)
        if decision.blocked:
            messages.warning(
                request,
                _("You saved your preferences several times in a row. Please wait a moment."),
            )
            return redirect("account:notifications")
        form = PreferencesForm(request.POST, user=request.user)
        if form.is_valid():
            preferences.set_preferences(request.user, form.as_payload())
            throttle.record("preferences", request.user.pk)
            messages.success(request, _("Your notification preferences were saved."))
            return redirect("account:notifications")
    else:
        form = PreferencesForm(user=request.user)

    return render(
        request,
        "notifications/preferences.html",
        {
            "form": form,
            "categories": [(value, label) for value, label in NotificationCategory.choices],
        },
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def unsubscribe(request: HttpRequest, token: str) -> HttpResponse:
    """One-click marketing unsubscribe (Phase 17 §16, Phase 51 §4).

    GET or POST, because mail clients support both RFC 8058 and one-click link clicks, and
    because the token *is* the capability: unguessable, opaque, and useless without the
    database row behind it. Deliberately **not** behind ``login_required`` -- the
    recipient clicks from an email, possibly on a device where they never signed in, and
    the response never names the address it silenced.
    """
    throttle = NotificationThrottle()
    decision = throttle.check("unsubscribe", _client_ip(request))
    if decision.blocked:
        return HttpResponse(_("Too many requests. Please try again later."), status=429)

    row = preferences.find_unsubscribe_token(token)
    if row is None:
        # Same answer for garbage, expired-looking and foreign tokens: no oracle.
        return render(
            request,
            "notifications/unsubscribe.html",
            {"valid": False},
            status=404,
        )
    throttle.record("unsubscribe", _client_ip(request))
    preferences.apply_unsubscribe(row.user)
    return render(request, "notifications/unsubscribe.html", {"valid": True})


@csrf_exempt
@require_http_methods(["POST"])
def delivery_webhook(request: HttpRequest, provider_key: str) -> HttpResponse:
    """Provider delivery status webhook endpoint (Phase 51 §5, §9).

    Requires valid signature, prevents replay attacks, updates delivery state safely.
    """
    from apps.notifications.services import webhooks

    try:
        webhooks.verify_webhook_signature(request, provider_key)
    except webhooks.WebhookAuthenticationError as exc:
        logger.warning("Rejected webhook signature for provider '%s': %s", provider_key, exc)
        return JsonResponse({"error": str(exc)}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
        if not isinstance(payload, dict):
            return JsonResponse({"error": "Payload must be a JSON object."}, status=400)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Malformed JSON payload."}, status=400)

    try:
        result = webhooks.process_delivery_webhook(provider_key, payload)
        return JsonResponse(result, status=200)
    except webhooks.WebhookProcessingError as exc:
        logger.warning("Failed processing webhook for provider '%s': %s", provider_key, exc)
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("Unexpected error processing webhook for provider '%s'", provider_key)
        return JsonResponse({"error": "Internal processing error."}, status=500)

