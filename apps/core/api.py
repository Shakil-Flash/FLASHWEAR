"""REST API views (non-template) for the core app."""

from __future__ import annotations

from django.views.decorators.cache import never_cache
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@never_cache
@api_view(["GET"])
@permission_classes([AllowAny])
def health(request) -> Response:
    """Liveness/readiness probe.

    Returns a fixed JSON payload so no environment or dependency detail leaks, and is
    marked ``never_cache`` so an intermediary cannot mask an outage with a cached 200.
    """
    return Response({"status": "ok"}, status=200)
