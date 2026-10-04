"""Creator serializers (Phase 12).

DRF serializers for creator API endpoints. All serializers use
the project's existing patterns: explicit fields, read-only fields
for computed values, and proper validation.
"""

from rest_framework import serializers

from .models import (
    CreatorApplication,
    CreatorPost,
    CreatorPostLike,
    CreatorPostMedia,
    CreatorPostOutfit,
    CreatorPostProduct,
    CreatorPostReport,
    CreatorPostSave,
    CreatorPostStatus,
    CreatorProfile,
)

# ── CreatorProfile ──────────────────────────────────────────────────


class CreatorProfileSerializer(serializers.ModelSerializer):
    """Serializer for CreatorProfile — public and admin views."""

    user_email = serializers.EmailField(source="user.email", read_only=True)
    is_approved = serializers.BooleanField(read_only=True)
    is_pending = serializers.BooleanField(read_only=True)
    is_rejected = serializers.BooleanField(read_only=True)
    is_suspended = serializers.BooleanField(read_only=True)

    class Meta:
        model = CreatorProfile
        fields = [
            "id",
            "user",
            "user_email",
            "display_name",
            "slug",
            "bio",
            "avatar",
            "cover_image",
            "website",
            "instagram_handle",
            "tiktok_handle",
            "location",
            "status",
            "is_featured",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["user", "slug", "created_at", "updated_at"]

    def validate_slug(self, value):
        """Ensure slug is unique."""
        if (
            CreatorProfile.objects.filter(slug=value)
            .exclude(pk=self.instance.pk if self.instance else None)
            .exists()
        ):
            raise serializers.ValidationError("A creator with this slug already exists.")
        return value


# ── CreatorApplication ─────────────────────────────────────────────


class CreatorApplicationSerializer(serializers.ModelSerializer):
    """Serializer for CreatorApplication — submission and moderation."""

    applicant_email = serializers.EmailField(source="applicant.email", read_only=True)
    reviewer_email = serializers.EmailField(
        source="reviewer.email", read_only=True, allow_null=True
    )

    class Meta:
        model = CreatorApplication
        fields = [
            "id",
            "applicant",
            "applicant_email",
            "status",
            "requested_display_name",
            "application_bio",
            "social_links",
            "portfolio_url",
            "reviewer",
            "reviewer_email",
            "reviewer_note",
            "submitted_at",
            "reviewed_at",
        ]
        read_only_fields = ["applicant", "submitted_at", "reviewed_at", "reviewer_note"]

    def validate_status(self, value):
        """Only allow valid status transitions."""
        # This is a simplification; full transition logic is in services
        return value


# ── CreatorPost ────────────────────────────────────────────────────


class CreatorPostSerializer(serializers.ModelSerializer):
    """Serializer for CreatorPost — public and admin views."""

    creator_display_name = serializers.CharField(source="creator.display_name", read_only=True)
    creator_slug = serializers.CharField(source="creator.slug", read_only=True)
    is_published = serializers.BooleanField(read_only=True)
    is_draft = serializers.BooleanField(read_only=True)
    like_count = serializers.ReadOnlyField()
    save_count = serializers.ReadOnlyField()

    class Meta:
        model = CreatorPost
        fields = [
            "id",
            "creator",
            "creator_display_name",
            "creator_slug",
            "title",
            "slug",
            "caption",
            "cover_image",
            "status",
            "published_at",
            "scheduled_at",
            "view_count",
            "like_count",
            "save_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "creator",
            "slug",
            "view_count",
            "like_count",
            "save_count",
            "published_at",
            "created_at",
        ]

    def validate_status(self, value):
        """Validate status transition."""
        # Basic validation - full logic in services
        valid_statuses = [
            CreatorPostStatus.DRAFT,
            CreatorPostStatus.PENDING_REVIEW,
            CreatorPostStatus.PUBLISHED,
            CreatorPostStatus.REJECTED,
            CreatorPostStatus.ARCHIVED,
        ]
        if value not in valid_statuses:
            raise serializers.ValidationError(f"Invalid status: {value}")
        return value


# ── CreatorPostMedia ───────────────────────────────────────────────


class CreatorPostMediaSerializer(serializers.ModelSerializer):
    """Serializer for CreatorPostMedia."""

    class Meta:
        model = CreatorPostMedia
        fields = [
            "id",
            "post",
            "image",
            "alt_text",
            "sort_order",
            "is_primary",
            "created_at",
        ]
        read_only_fields = ["post", "created_at"]

    def validate(self, data):
        """Ensure at least one media exists per post (validated in service layer)."""
        return data


# ── CreatorPostProduct ─────────────────────────────────────────────


class CreatorPostProductSerializer(serializers.ModelSerializer):
    """Serializer for CreatorPostProduct — product tagging."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True)
    product_price = serializers.ReadOnlyField(
        help_text="Price comes from the catalog/order system, not duplicated here."
    )

    class Meta:
        model = CreatorPostProduct
        fields = [
            "id",
            "post",
            "product",
            "product_name",
            "product_sku",
            "product_price",
            "display_order",
            "label",
            "created_at",
        ]
        read_only_fields = ["post", "created_at", "product_price"]

    def validate_product(self, value):
        """Only published products may be tagged."""
        if not value.is_published:
            raise serializers.ValidationError(
                "Only published products may be tagged in creator posts."
            )
        return value


# ── CreatorPostOutfit ──────────────────────────────────────────────


class CreatorPostOutfitSerializer(serializers.ModelSerializer):
    """Serializer for CreatorPostOutfit — outfit tagging."""

    outfit_name = serializers.CharField(source="outfit.name", read_only=True)
    outfit_description = serializers.CharField(source="outfit.description", read_only=True)

    class Meta:
        model = CreatorPostOutfit
        fields = [
            "id",
            "post",
            "outfit",
            "outfit_name",
            "outfit_description",
            "display_order",
            "created_at",
        ]
        read_only_fields = ["post", "created_at"]


# ── CreatorPostLike ────────────────────────────────────────────────


class CreatorPostLikeSerializer(serializers.ModelSerializer):
    """Serializer for CreatorPostLike."""

    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = CreatorPostLike
        fields = ["id", "user", "user_email", "post", "created_at"]
        read_only_fields = ["user", "post", "created_at"]


# ── CreatorPostSave ────────────────────────────────────────────────


class CreatorPostSaveSerializer(serializers.ModelSerializer):
    """Serializer for CreatorPostSave."""

    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = CreatorPostSave
        fields = ["id", "user", "user_email", "post", "created_at"]
        read_only_fields = ["user", "post", "created_at"]


# ── CreatorPostReport ──────────────────────────────────────────────


class CreatorPostReportSerializer(serializers.ModelSerializer):
    """Serializer for CreatorPostReport."""

    reporter_email = serializers.EmailField(source="user.email", read_only=True)
    reason_display = serializers.CharField(source="get_reason_display", read_only=True)

    class Meta:
        model = CreatorPostReport
        fields = [
            "id",
            "user",
            "reporter_email",
            "post",
            "reason",
            "reason_display",
            "details",
            "status",
            "resolved_at",
            "resolved_by",
        ]
        read_only_fields = ["user", "post", "resolved_at", "resolved_by"]

    def validate(self, data):
        """Creators cannot report their own content."""
        if data["user"].role == "creator" and data["post"].creator.user == data["user"]:
            raise serializers.ValidationError("Creators cannot report their own content.")
        return data
