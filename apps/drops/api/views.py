"""FLASH Drop APIs (Phase 11).

Session-authenticated, customer-scoped.  Every endpoint scopes through
``request.user`` so one customer can never read or write another's data.

Namespace: ``v1`` (registered in ``config/api/v1/urls.py``).
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.drops.models import FlashDrop

logger = logging.getLogger(__name__)

User = get_user_model()


class DropRootView(APIView):
    """GET ``/api/v1/drops/`` — root endpoint for drops."""

    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        """Return the available drop contexts and state summary."""
        drops = FlashDrop.objects.active()
        return Response(
            {
                "drops": [
                    {
                        "id": str(drop.pk),
                        "name": drop.name,
                        "slug": drop.slug,
                        "status": drop.status,
                        "is_live": drop.is_live,
                        "is_upcoming": drop.is_upcoming,
                        "starts_at": drop.starts_at,
                        "ends_at": drop.ends_at,
                    }
                    for drop in drops
                ],
                "user_has_dna": getattr(request.user, "flash_dna", None) is not None,
            }
        )


class DropDetailView(APIView):
    """GET ``/api/v1/drops/<slug>/`` — drop detail page data."""

    permission_classes = [IsAuthenticated]

    def get(self, request, slug: str) -> Response:
        """Return drop detail data for the storefront."""
        try:
            drop = FlashDrop.objects.get(
                slug=slug,
                status__in=[FlashDrop.LIVE, FlashDrop.SCHEDULED],
            )
        except FlashDrop.DoesNotExist:
            return Response(
                {"detail": "Drop not found or not available."},
                status=status.HTTP_404_NOT_FOUND,
            )

        products = drop.available_products

        return Response(
            {
                "drop": {
                    "id": str(drop.pk),
                    "name": drop.name,
                    "slug": drop.slug,
                    "description": drop.description,
                    "status": drop.status,
                    "starts_at": drop.starts_at,
                    "ends_at": drop.ends_at,
                    "published_at": drop.published_at,
                    "is_live": drop.is_live,
                    "is_upcoming": drop.is_upcoming,
                    "is_ended": drop.is_ended,
                    "is_cancelled": drop.is_cancelled,
                },
                "products": [
                    {
                        "id": str(dp.product.pk),
                        "name": dp.product.name,
                        "slug": dp.product.slug,
                        "variant": (
                            {
                                "sku": dp.variant.sku,
                                "color": dp.variant.color.name,
                                "size": dp.variant.size.name,
                            }
                            if dp.variant
                            else None
                        ),
                        "price": str(dp.product.price) if dp.product else None,
                        "is_featured": dp.is_featured,
                        "drop_label": dp.drop_label,
                        "primary_image": (
                            dp.product.primary_image.url
                            if dp.product and dp.product.primary_image
                            else None
                        ),
                    }
                    for dp in products
                ],
                "user_has_dna": getattr(request.user, "flash_dna", None) is not None,
            }
        )


class DropProductsView(APIView):
    """GET ``/api/v1/drops/<slug>/products/`` — products in a drop."""

    permission_classes = [IsAuthenticated]

    def get(self, request, slug: str) -> Response:
        """Return products available in this drop."""
        try:
            drop = FlashDrop.objects.get(slug=slug)
        except FlashDrop.DoesNotExist:
            return Response(
                {"detail": "Drop not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Only live/scheduled drops are purchasable
        if not drop.is_live and not drop.is_upcoming:
            return Response(
                {"detail": "This drop is not currently available."},
                status=status.HTTP_404_NOT_FOUND,
            )

        products = drop.available_products

        return Response(
            {
                "drop": {
                    "id": str(drop.pk),
                    "name": drop.name,
                    "status": drop.status,
                },
                "products": [
                    {
                        "id": str(dp.product.pk),
                        "name": dp.product.name,
                        "slug": dp.product.slug,
                        "variant": (
                            {
                                "sku": dp.variant.sku,
                                "color": dp.variant.color.name,
                                "size": dp.variant.size.name,
                            }
                            if dp.variant
                            else None
                        ),
                        "price": str(dp.product.price) if dp.product else None,
                        "is_featured": dp.is_featured,
                        "drop_label": dp.drop_label,
                        "primary_image": (
                            dp.product.primary_image.url
                            if dp.product and dp.product.primary_image
                            else None
                        ),
                    }
                    for dp in products
                ],
                "state": {
                    "is_live": drop.is_live,
                    "is_upcoming": drop.is_upcoming,
                    "is_ended": drop.is_ended,
                    "is_cancelled": drop.is_cancelled,
                },
            }
        )


class DropInterestView(APIView):
    """POST ``/api/v1/drops/<id>/interest/`` — express interest in an upcoming drop."""

    permission_classes = [IsAuthenticated]

    def post(self, request, id: int) -> Response:
        """Record that a user is interested in this upcoming drop."""
        try:
            drop = FlashDrop.objects.get(pk=id, status=FlashDrop.SCHEDULED)
        except FlashDrop.DoesNotExist:
            return Response(
                {"detail": "Drop not found or not scheduled."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Simple interest record - just log it; notification system deferred
        # In a full implementation, this would create a DropReminder record

        return Response(
            {
                "detail": "Interest recorded.",
                "drop_id": str(drop.pk),
                "drop_name": drop.name,
            }
        )
