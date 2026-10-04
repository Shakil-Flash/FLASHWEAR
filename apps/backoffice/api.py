"""Back Office API (``/api/v1/backoffice/``).

Conventions match the rest of the API surface, with one addition: **404 for a missing
capability**, exactly as the HTML routes do. An operations API that answers ``403`` has
told a scanner where the operations platform lives; a route that does not exist tells it
nothing. Domain failures from a service still map to their own structured payload.

Reads only. Every mutating action lives behind the HTML forms (which post CSRF tokens and
flash the result to a human); there is deliberately no second, parallel write path with
its own rules to keep in sync.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

from apps.backoffice import selectors
from apps.backoffice.permissions import (
    AUDIT_VIEW,
    CUSTOMERS_VIEW,
    INVENTORY_VIEW,
    OPS_VIEW,
    ORDERS_VIEW,
    PAYMENTS_VIEW,
    has,
)
from apps.backoffice.selectors.dashboard import dashboard_summary
from apps.backoffice.services.alerts import alerts_for

__all__ = [
    "CAPABILITY_BY_ENDPOINT",
    "BackOfficeAlertsView",
    "BackOfficeAuditView",
    "BackOfficeDashboardView",
    "BackOfficeOrdersView",
    "BackOfficeRootView",
]


class _BackOfficeView:
    """Mixin: an authenticated user who holds ``OPS_VIEW`` and this view's capability.

    Missing capability raises ``NotFound`` (404) -- the same contract as
    :func:`apps.backoffice.permissions.backoffice_access`, so the API and the HTML cannot
    disagree about whether a screen exists.
    """

    permission_classes = [IsAuthenticated]
    capability: str | None = None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not has(request.user, OPS_VIEW):
            raise NotFound()
        if self.capability is not None and not has(request.user, self.capability):
            raise NotFound()


class _Page:
    """Minimal paging: the configured page size, clamped, never the whole table."""

    page_size = settings.BACKOFFICE_PAGE_SIZE

    def paginate(self, request, queryset):
        try:
            page_number = int(request.query_params.get("page", "1"))
        except (TypeError, ValueError):
            page_number = 1
        page_number = max(page_number, 1)
        start = (page_number - 1) * self.page_size
        rows = list(queryset[start : start + self.page_size + 1])
        has_next = len(rows) > self.page_size
        return rows[: self.page_size], has_next


class BackOfficeRootView(_BackOfficeView, APIView):
    """``GET /api/v1/backoffice/`` -- the endpoints this namespace exposes."""

    capability = None

    def get(self, request):
        return Response(
            {
                "dashboard": reverse("v1:backoffice-dashboard", request=request),
                "alerts": reverse("v1:backoffice-alerts", request=request),
                "orders": reverse("v1:backoffice-orders", request=request),
                "audit": reverse("v1:backoffice-audit", request=request),
            }
        )


class BackOfficeDashboardView(_BackOfficeView, APIView):
    """``GET /api/v1/backoffice/dashboard/?range=7d`` -- the same numbers as the page."""

    capability = None

    def get(self, request):
        date_range = selectors.resolve_range(request.query_params.get("range"))
        summary = dashboard_summary(date_range=date_range)
        summary["range"] = {
            "key": summary["range"].key,
            "label": str(summary["range"].label),
        }
        return Response(summary)


class BackOfficeAlertsView(_BackOfficeView, APIView):
    """``GET /api/v1/backoffice/alerts/`` -- every rule that fired, for this caller."""

    capability = None

    def get(self, request):
        return Response(
            [
                {
                    "key": alert.key,
                    "severity": alert.severity,
                    "label": alert.label,
                    "count": alert.count,
                    "title": alert.title,
                    "detail": alert.detail,
                    "url": reverse(alert.url_name, request=request),
                }
                for alert in alerts_for(request.user)
            ]
        )


class _OrderRowSerializer(serializers.Serializer):
    number = serializers.CharField()
    status = serializers.CharField()
    total = serializers.DecimalField(max_digits=10, decimal_places=2)
    currency = serializers.CharField()
    created_at = serializers.DateTimeField()
    customer = serializers.EmailField(source="user.email", default=None)


class BackOfficeOrdersView(_BackOfficeView, _Page, APIView):
    """``GET /api/v1/backoffice/orders/?status=&q=&range=`` -- the queue, paged.

    Six fields and no more: an operations feed does not need address lines, and every
    field it does not ask for is a field it cannot leak.
    """

    capability = ORDERS_VIEW

    def get(self, request):
        date_range = selectors.resolve_range(request.query_params.get("range"))
        queryset = selectors.orders(
            q=request.query_params.get("q", ""),
            status=request.query_params.get("status", ""),
            date_range=date_range,
            sort=request.query_params.get("sort", ""),
        )
        rows, has_next = self.paginate(request, queryset)
        return Response(
            {
                "count": queryset.count(),
                "next": bool(has_next),
                "results": _OrderRowSerializer(rows, many=True).data,
            }
        )


class _AuditRowSerializer(serializers.Serializer):
    created_at = serializers.DateTimeField()
    actor = serializers.CharField(source="actor_label")
    domain = serializers.CharField()
    action = serializers.CharField()
    object_type = serializers.CharField()
    object_id = serializers.CharField()
    object_repr = serializers.CharField()
    reason = serializers.CharField()
    metadata = serializers.JSONField()


class BackOfficeAuditView(_BackOfficeView, _Page, APIView):
    """``GET /api/v1/backoffice/audit/?action=&domain=&range=`` -- the trail, read-only."""

    capability = AUDIT_VIEW

    def get(self, request):
        date_range = selectors.resolve_range(request.query_params.get("range"))
        queryset = selectors.audit_events(
            action=request.query_params.get("action", ""),
            domain=request.query_params.get("domain", ""),
            date_range=date_range,
            sort=request.query_params.get("sort", ""),
        )
        rows, has_next = self.paginate(request, queryset)
        return Response(
            {
                "count": queryset.count(),
                "next": bool(has_next),
                "results": _AuditRowSerializer(rows, many=True).data,
            }
        )


# Which capability each endpoint demands -- a single table the URL module's docs and the
# tests can both read, so a permission change has one place to be recorded.
CAPABILITY_BY_ENDPOINT = {
    "orders": ORDERS_VIEW,
    "audit": AUDIT_VIEW,
    "alerts": OPS_VIEW,
    "dashboard": OPS_VIEW,
    "inventory": INVENTORY_VIEW,
    "payments": PAYMENTS_VIEW,
    "customers": CUSTOMERS_VIEW,
}
