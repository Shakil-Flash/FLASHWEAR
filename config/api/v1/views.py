"""API v1 views."""

from __future__ import annotations

from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView


class ApiRootView(APIView):
    """Machine-readable index of the endpoints exposed by API v1.

    Marked ``never_cache``: the index changes when endpoints are added, so a cached copy
    would advertise URLs that no longer exist.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    renderer_classes = [JSONRenderer]

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs) -> Response:
        """Return a link to every endpoint currently available in v1."""
        return Response(
            {
                "service": "flashwear",
                "version": "v1",
                "endpoints": {
                    "health": reverse("v1:health", request=request),
                    "products": reverse("v1:product-list", request=request),
                    "categories": reverse("v1:category-list", request=request),
                    "collections": reverse("v1:collection-list", request=request),
                    "brands": reverse("v1:brand-list", request=request),
                    "account_register": reverse("v1:account-register", request=request),
                    "account_me": reverse("v1:account-me", request=request),
                    "account_password": reverse("v1:account-password", request=request),
                    "addresses": reverse("v1:address-list", request=request),
                    "orders": reverse("v1:order-list", request=request),
                },
            }
        )
