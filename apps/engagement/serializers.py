"""Engagement API serializers (Phase 7).

``ReviewSerializer`` serves both the public list and a customer's own detail/edit: content
fields are writable, everything server-derived (author, status, ``verified_purchase``) is
read-only, so no payload can claim a purchase it did not make or publish itself.

Creation goes through the *service*, not ``ModelSerializer.create``: eligibility and the
qualifying order are re-derived server-side there, which is the whole point of the
verified-purchase rule.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.engagement.models import Review
from apps.engagement.services import reviews as review_services
from apps.engagement.services.errors import ReviewError


class ReviewSerializer(serializers.ModelSerializer):
    """A review's content plus its server-owned moderation/purchase facts."""

    author = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = (
            "id",
            "author",
            "rating",
            "title",
            "body",
            "verified_purchase",
            "status",
            "created_at",
        )
        read_only_fields = (
            "id",
            "author",
            "verified_purchase",
            "status",
            "created_at",
        )

    def get_author(self, obj) -> str:
        full_name = obj.author.get_full_name()
        return full_name or obj.author.email

    def validate_rating(self, value: int) -> int:
        if not 1 <= int(value) <= 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return int(value)

    def validate_title(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A review needs a title.")
        return value

    def validate_body(self, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 3:
            raise serializers.ValidationError("A review needs a title and some words.")
        return value

    def create(self, validated_data):
        """Eligibility and the qualifying order are re-derived, never taken from the payload."""
        try:
            return review_services.create_review(
                user=self.context["request"].user,
                product=self.context["product"],
                rating=validated_data["rating"],
                title=validated_data["title"],
                body=validated_data["body"],
            )
        except ReviewError as exc:
            raise serializers.ValidationError({exc.code: exc.message}) from exc

    def update(self, instance, validated_data):
        """Owner edit through the service so a published review returns to moderation."""
        return review_services.update_review(
            instance,
            rating=validated_data.get("rating", instance.rating),
            title=validated_data.get("title", instance.title),
            body=validated_data.get("body", instance.body),
        )


class PromotionValidateSerializer(serializers.Serializer):
    """Preview request for ``POST /api/v1/checkout/promotion/``.

    The subtotal is a *preview* input: the server computes the discount from it but never
    persists anything -- checkout validation re-computes against the real cart.
    """

    code = serializers.CharField(max_length=64)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0)
