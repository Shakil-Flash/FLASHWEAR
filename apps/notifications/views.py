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

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
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
    unread_only = request.GET.get("filter") == "unread"
    category = request.GET.get("category", "")
    if category not in NotificationCategory.values:
        category = ""

    qs = selectors.user_notifications(request.user, unread=unread_only, category=category)
    paginator = Paginator(qs, PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "notifications/center.html",
        {
            "page_obj": page_obj,
            "notifications": page_obj.object_list,
            "unread_only": unread_only,
            "active_category": category,
            "categories": [(value, label) for value, label in NotificationCategory.choices],
            "counts": selectors.status_counts(request.user),
        },
    )


@login_required
@require_http_methods(["GET"])
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    """One notification. Read state flips here -- opening it *is* reading it."""
    notification = selectors.user_notification(request.user, pk)
    if notification is None:
        raise Http404(_("No notification matches the given id."))
    notification.mark_read()
    return render(
        request,
        "notifications/detail.html",
        {"notification": notification},
    )


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


@require_http_methods(["GET"])
def unsubscribe(request: HttpRequest, token: str) -> HttpResponse:
    """One-click marketing unsubscribe (Phase 17 §16).

    GET, because that is what mail clients and every sender's instructions promise, and
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
