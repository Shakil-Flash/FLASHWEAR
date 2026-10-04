"""Styling API (Phase 9).

Session-authenticated, customer-scoped.  Every endpoint scopes through
``request.user`` so one customer can never read or write another's data.

Namespace: ``v1`` (registered in ``config/api/v1/urls.py``).
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.styling.services import (
    recommend_outfit,
    save_recommended_outfit,
    style_product,
)
from apps.styling.services.errors import (
    StylistError,
)

logger = logging.getLogger(__name__)

User = get_user_model()


class FlashDNAView(APIView):
    """GET/PUT ``/api/v1/flash-dna/`` — profile CRUD."""

    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        try:
            profile = request.user.flash_dna
        except ObjectDoesNotExist:
            return Response(
                {"detail": "FLASH DNA profile not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        # Return a sanitized payload — no internal db IDs exposed unless relevant.
        return Response(
            {
                "styles": profile.styles,
                "favorite_colors": [c.hex_code for c in profile.favorite_colors.all()],
                "disliked_colors": [c.hex_code for c in profile.disliked_colors.all()],
                "preferred_categories": [c.name for c in profile.preferred_categories.all()],
                "preferred_fits": [f.name for f in profile.preferred_fits],
                "preferred_materials": [m.name for m in profile.preferred_materials.all()],
                "preferred_occasions": [o.name for o in profile.preferred_occasions.all()],
                "preferred_seasons": [s.name for s in profile.preferred_seasons.all()],
                "preferred_price_range": profile.preferred_price_range,
                "preferred_brands": [b.name for b in profile.preferred_brands.all()],
                "confidence_level": profile.confidence_level,
                "fashion_goal": profile.fashion_goal or "",
                "is_complete": profile.is_complete,
            }
        )

    def put(self, request) -> Response:
        """Update profile from a flat payload."""
        try:
            profile = request.user.flash_dna
        except ObjectDoesNotExist:
            from apps.styling.services.dna import create_dna

            profile = create_dna(request.user, **self._payload(request))

        for key, value in self._payload(request).items():
            if hasattr(profile, key):
                setattr(profile, key, value)
        try:
            profile.full_clean()
        except Exception as exc:
            from apps.styling.services.errors import EmptyProfileError

            if isinstance(exc, EmptyProfileError):
                return Response(
                    {"detail": exc.message},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        profile.save()
        return Response({"detail": "FLASH DNA updated."})

    @staticmethod
    def _payload(request) -> dict:
        """Extract the minimal fields the profile accepts from the PUT body."""
        data = request.data or {}
        allowed = {
            "styles",
            "favorite_colors",
            "disliked_colors",
            "preferred_categories",
            "preferred_fits",
            "preferred_materials",
            "preferred_occasions",
            "preferred_seasons",
            "preferred_price_range",
            "preferred_brands",
            "confidence_level",
            "fashion_goal",
        }
        return {k: v for k, v in data.items() if k in allowed}


class StylistRecommendView(APIView):
    """POST ``/api/v1/stylist/recommend/`` — get outfit recommendations."""

    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs) -> Response:
        try:
            result = recommend_outfit(request.user, request.path, request.data.get("provider"))
        except StylistError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:  # broad; safety net
            logger.exception("Stylist unexpected error")
            return Response(
                {"detail": "The stylist service is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)


class StylistStyleProductView(APIView):
    """POST ``/api/v1/stylist/style-product/`` — style a specific product."""

    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs) -> Response:
        product_pk = request.data.get("product_pk")
        if not product_pk:
            return Response(
                {"detail": "product_pk is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = style_product(request.user, int(product_pk))
        except StylistError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Stylist style-product error")
            return Response(
                {"detail": "The stylist service is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)


class StylistSaveOutfitView(APIView):
    """POST ``/api/v1/stylist/recommendations/<id>/save/`` — save an outfit."""

    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs) -> Response:
        try:
            outfit = save_recommended_outfit(request.user, request.data)
        except StylistError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Stylist save error")
            return Response(
                {"detail": "Could not save the outfit right now."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            {
                "detail": "Outfit saved.",
                "outfit_pk": outfit.pk,
                "outfit_name": outfit.name,
            }
        )


class FlashDNACheckView(APIView):
    """GET ``/api/v1/flash-dna/check/`` — quick check for profile existence."""

    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        has_it = request.user.flash_dna is not None
        return Response({"has_profile": has_it})
