"""JSON API for the notification center (Phase 17 §36).

Session-authenticated and customer-scoped, exactly like the account API:

* every queryset is filtered by ``request.user`` first, so an id in a URL can only ever
  resolve to the caller's own row -- someone else's notification is a 404, never a 403
  that confirms the id exists (IDOR);
* reads take the ``notifications`` DRF scope, writes take ``notifications_write``, and
  the two write-loop endpoints (read-all, preferences) *additionally* pass the project's
  own sliding-window :class:`apps.notifications.throttling.NotificationThrottle`, so a
  script cannot turn them into a free write loop even inside its rate budget;
* preferences go through :func:`apps.notifications.services.preferences.set_preferences`,
  which coerces mandatory categories back to enabled -- a tampered payload cannot silence
  orders, payments, delivery, support or account messages.
"""

from __future__ import annotations

from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.notifications.models import Channel, Notification
from apps.notifications.services import in_app, preferences
from apps.notifications.throttling import NotificationThrottle

__all__ = [
    "NotificationListView",
    "NotificationPreferencesView",
    "NotificationReadAllView",
    "NotificationReadView",
]


def _serialize(row: Notification) -> dict:
    """One in-app row. Content plus navigation -- nothing the customer cannot already
    see in the center, and never anybody else's row."""
    return {
        "id": row.pk,
        "notification_type": row.notification_type,
        "category": row.category,
        "title": row.title,
        "body": row.body,
        "action_url": row.action_url or "",
        "priority": row.priority,
        "read_at": row.read_at,
        "is_unread": row.is_unread,
        "created_at": row.created_at,
    }


def _throttled(throttle: NotificationThrottle, action: str, ident) -> Response | None:
    """Shared 429 for the sliding-window write limits (§24)."""
    decision = throttle.check(action, ident)
    if not decision.blocked:
        return None
    return Response(
        {"detail": "Too many requests.", "retry_after": decision.retry_after},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
        headers={"Retry-After": str(decision.retry_after or throttle.window)},
    )


class NotificationListView(APIView):
    """``GET /api/v1/notifications/`` -- the caller's in-app rows, newest first.

    ``?unread=1`` narrows to unread; the payload carries the live ``unread_count`` so a
    client can render its badge from the same response.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notifications"

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs) -> Response:
        queryset = Notification.objects.filter(user=request.user, channel=Channel.IN_APP).order_by(
            "-created_at", "-pk"
        )
        if request.query_params.get("unread") == "1":
            queryset = queryset.filter(read_at__isnull=True)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request)
        response = paginator.get_paginated_response([_serialize(row) for row in page])
        # The count rides on the same payload so a client renders its list and its badge
        # from one round trip.
        response.data["unread_count"] = in_app.unread_count(request.user)
        return response


class NotificationReadView(APIView):
    """``POST /api/v1/notifications/<pk>/read/`` -- mark one row read.

    Idempotent: reading an already-read row reports its state without error, and a row
    that is not the caller's is a 404.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notifications_write"

    def post(self, request, pk: int, *args, **kwargs) -> Response:
        row = in_app.mark_read(request.user, pk)
        if row is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {"notification": _serialize(row), "unread_count": in_app.unread_count(request.user)}
        )


class NotificationReadAllView(APIView):
    """``POST /api/v1/notifications/read-all/`` -- mark everything read.

    The classic write-loop endpoint: DRF scope *and* the project's sliding window, the
    latter keyed by user so one client cannot burn another's budget.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notifications_write"

    def post(self, request, *args, **kwargs) -> Response:
        throttle = NotificationThrottle()
        blocked = _throttled(throttle, "read_all", request.user.pk)
        if blocked is not None:
            return blocked
        marked = in_app.mark_all_read(request.user)
        throttle.record("read_all", request.user.pk)
        return Response({"marked": marked, "unread_count": 0})


class NotificationPreferencesView(APIView):
    """``GET``/``PUT /api/v1/notifications/preferences/`` -- the category toggles.

    The GET payload flags ``mandatory`` rows so a client can render them disabled; PUT
    runs the same server-side coercion the HTML form does, so sending them as ``false``
    changes nothing that matters.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notifications"

    def get(self, request, *args, **kwargs) -> Response:
        return Response({"preferences": preferences.preference_rows(request.user)})

    def put(self, request, *args, **kwargs) -> Response:
        throttle = NotificationThrottle()
        blocked = _throttled(throttle, "preferences", request.user.pk)
        if blocked is not None:
            return blocked
        payload = request.data
        if not isinstance(payload, dict):
            return Response(
                {"detail": "Expected an object of category toggles."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        preferences.set_preferences(request.user, payload)
        throttle.record("preferences", request.user.pk)
        return Response({"preferences": preferences.preference_rows(request.user)})
