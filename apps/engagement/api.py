"""Engagement API: product reviews and promotion validation (Phase 7).

Reads follow the storefront's rules exactly (published only, allowlisted sort, paginated with
the global page size); writes are session-authenticated and author-scoped, and a non-purchaser
is rejected before any payload is read (403, not 400) because eligibility is about *who*, not
*what*.

The promotion endpoint is a preview: it reports the discount a code would give on a subtotal
the client sends, and persists nothing. Internal facts -- usage counters, limits, other
customers' activity -- never appear in any payload.
"""

from __future__ import annotations

from django.db.models import Q
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework import status as http_status
from rest_framework.exceptions import NotAuthenticated, NotFound, PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Product
from apps.engagement.models import Review
from apps.engagement.serializers import PromotionValidateSerializer, ReviewSerializer
from apps.engagement.services import reviews as review_services
from apps.engagement.services.discounts import compute_discounts
from apps.engagement.services.errors import EngagementError

__all__ = ["ProductReviewsView", "PromotionValidateView", "ReviewDetailView"]

TRUE_VALUES = {"1", "true", "yes", "on"}


def _published_product(slug: str) -> Product:
    product = Product.objects.published().filter(slug=slug).first()
    if product is None:
        raise NotFound("Not found.")
    return product


class ProductReviewsView(APIView):
    """``GET`` published reviews (allowlisted sort/filter) and ``POST`` a new one."""

    permission_classes = [AllowAny]

    @method_decorator(never_cache)
    def get(self, request, slug: str):
        product = _published_product(slug)
        sort = request.query_params.get("sort", review_services.DEFAULT_SORT)
        verified_only = request.query_params.get("verified", "").lower() in TRUE_VALUES
        queryset = review_services.public_queryset(product, sort=sort, verified_only=verified_only)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = ReviewSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request, slug: str):
        product = _published_product(slug)
        if not request.user.is_authenticated:
            raise NotAuthenticated("Sign in to review this product.")
        allowed, reason = review_services.eligibility(request.user, product)
        if not allowed:
            raise PermissionDenied(review_services.ineligible_message(reason))
        serializer = ReviewSerializer(
            data=request.data, context={"request": request, "product": product}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=http_status.HTTP_201_CREATED)


class ReviewDetailView(APIView):
    """``GET/PATCH/DELETE /api/v1/reviews/<pk>/`` -- public read, author-only write.

    A review that is neither published nor the caller's is a 404: ids must not confirm the
    existence of hidden rows.
    """

    permission_classes = [AllowAny]

    def _resolve(self, request, pk: int) -> Review:
        queryset = Review.objects.select_related("author").filter(pk=pk)
        if request.user.is_authenticated:
            queryset = queryset.filter(Q(status=Review.Status.PUBLISHED) | Q(author=request.user))
        else:
            queryset = queryset.filter(status=Review.Status.PUBLISHED)
        review = queryset.first()
        if review is None:
            raise NotFound("Not found.")
        return review

    @method_decorator(never_cache)
    def get(self, request, pk: int):
        review = self._resolve(request, pk)
        return Response(ReviewSerializer(review).data)

    def patch(self, request, pk: int):
        if not request.user.is_authenticated:
            raise NotAuthenticated("Sign in to edit your review.")
        review = self._resolve(request, pk)
        if review.author_id != request.user.pk:
            raise NotFound("Not found.")
        serializer = ReviewSerializer(
            review, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ReviewSerializer(serializer.instance).data)

    def delete(self, request, pk: int):
        if not request.user.is_authenticated:
            raise NotAuthenticated("Sign in to delete your review.")
        review = self._resolve(request, pk)
        if review.author_id != request.user.pk:
            raise NotFound("Not found.")
        review.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class PromotionValidateView(APIView):
    """``POST /api/v1/checkout/promotion/`` -- preview what a code would give."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "sensitive"

    def post(self, request):
        form = PromotionValidateSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        try:
            breakdown = compute_discounts(
                user=request.user,
                subtotal=form.validated_data["subtotal"],
                promotion_code=form.validated_data["code"],
                loyalty_points=0,
            )
        except EngagementError as exc:
            return Response(
                {"valid": False, "detail": exc.message, "code": exc.code},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "valid": True,
                "code": breakdown.promotion_code,
                "discount": str(breakdown.promotion_discount),
                "display": breakdown.promotion.discount_label,
            }
        )
