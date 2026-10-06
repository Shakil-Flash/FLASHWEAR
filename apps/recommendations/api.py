"""Recommendation APIs (Phase 10).

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

from apps.analytics.services import record_event
from apps.recommendations.services import (
    get_recommendations,
)

logger = logging.getLogger(__name__)

User = get_user_model()


class RecommendationRootView(APIView):
    """GET ``/api/v1/recommendations/`` — root endpoint for recommendations."""

    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        """Return the available recommendation contexts."""
        return Response(
            {
                "contexts": [
                    "personalized",
                    "closet_complement",
                    "outfit_completion",
                    "similar_products",
                    "new_for_you",
                ],
                "user_has_dna": user_has_flash_dna(request.user),
                "user_has_closet": user_has_closet(request.user),
            }
        )


def user_has_flash_dna(user) -> bool:
    """Check if user has a FLASH DNA profile."""
    try:
        _ = user.flash_dna
        return True
    except ObjectDoesNotExist:
        return False


def user_has_closet(user) -> bool:
    """Check if user has active closet items."""
    from apps.closet.models.items import ClosetItem

    return user.closet_items.filter(status=ClosetItem.Status.ACTIVE).exists()


class ForYouView(APIView):
    """GET/POST ``/api/v1/recommendations/for-you/`` — personalized recommendations."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def get(self, request) -> Response:
        """Get personalized recommendations for the user."""
        try:
            result = get_recommendations(
                request.user,
                context="personalized",
                max_results=10,
                diversity=True,
                request_path=request.path,
            )
        except Exception:
            logger.exception("Error generating for-you recommendations")
            return Response(
                {"detail": "Could not generate recommendations at this time."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        record_event(
            "recommendation_view",
            request=request,
            object_type="surface",
            metadata={"surface": "for_you"},
        )
        return Response(result)


class ClosetComplementView(APIView):
    """GET ``/api/v1/recommendations/closet/`` — closet complement recommendations."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def get(self, request) -> Response:
        """Get closet complement recommendations."""
        try:
            result = get_recommendations(
                request.user,
                context="closet_complement",
                max_results=10,
                diversity=True,
                request_path=request.path,
            )
        except Exception:
            logger.exception("Error generating closet complement recommendations")
            return Response(
                {"detail": "Could not generate recommendations at this time."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)


class OutfitCompletionView(APIView):
    """GET ``/api/v1/recommendations/outfit/<id>/`` — complete the outfit."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def get(self, request, outfit_id: int) -> Response:
        """Get outfit completion recommendations."""
        try:
            from apps.closet.models import Outfit

            _outfit = Outfit.objects.get(
                pk=outfit_id, user=request.user, status=Outfit.Status.SAVED
            )
        except ObjectDoesNotExist:
            return Response(
                {"detail": "Outfit not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            result = get_recommendations(
                request.user,
                context="outfit_completion",
                max_results=10,
                diversity=True,
                request_path=request.path,
            )
        except Exception:
            logger.exception("Error generating outfit completion recommendations")
            return Response(
                {"detail": "Could not generate recommendations at this time."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)


class SimilarProductsView(APIView):
    """GET ``/api/v1/products/<slug>/recommendations/`` — similar products."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def get(self, request, slug: str) -> Response:
        """Get similar products to the given product."""
        try:
            from apps.catalog.models import Product as ProductModel

            _product = ProductModel.objects.get(slug=slug, is_purchasable=True)
        except ObjectDoesNotExist:
            return Response(
                {"detail": "Product not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            result = get_recommendations(
                request.user,
                context="similar_products",
                max_results=10,
                diversity=True,
                request_path=request.path,
            )
        except Exception:
            logger.exception("Error generating similar products")
            return Response(
                {"detail": "Could not generate recommendations at this time."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)


class NewForYouView(APIView):
    """GET ``/api/v1/recommendations/new-for-you/`` — new products matching preferences."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def get(self, request) -> Response:
        """Get new products matching user preferences."""
        try:
            result = get_recommendations(
                request.user,
                context="new_for_you",
                max_results=10,
                diversity=True,
                request_path=request.path,
            )
        except Exception:
            logger.exception("Error generating new-for-you recommendations")
            return Response(
                {"detail": "Could not generate recommendations at this time."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)


class FeedbackView(APIView):
    """POST ``/api/v1/recommendations/feedback/`` — submit recommendation feedback."""

    permission_classes = [IsAuthenticated]

    def post(self, request) -> Response:
        """Submit feedback on a recommendation."""
        try:
            product_pk = request.data.get("product_pk")
            choice = request.data.get("choice")

            if not product_pk or not choice:
                return Response(
                    {"detail": "product_pk and choice are required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Validate choice
            from apps.recommendations.models import RecommendationFeedback

            try:
                choice_enum = RecommendationFeedback.FeedbackChoice(choice)
            except ValueError:
                allowed = list(RecommendationFeedback.FeedbackChoice.values)
                return Response(
                    {"detail": f"Invalid choice. Must be one of: {allowed}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Create feedback record
            feedback = RecommendationFeedback.objects.create(
                user=request.user,
                recommendation_run_id=request.data.get("run_id", "manual"),
                product_id=product_pk,
                choice=choice_enum,
            )

            # Optionally update signal weights based on feedback
            # (simplified: just record the feedback; re-scoring on next request)

            return Response(
                {
                    "detail": "Feedback recorded.",
                    "feedback_id": feedback.pk,
                    "choice": feedback.choice,
                }
            )

        except Exception:
            logger.exception("Error recording recommendation feedback")
            return Response(
                {"detail": "Could not record feedback at this time."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
