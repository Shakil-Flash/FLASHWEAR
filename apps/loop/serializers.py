"""FLASH Loop serializers (Phase 13).

The privacy contract lives here:

* write serializers accept **only** the fields a customer may set
  (ownership reference, type, condition, notes, price). ``status``,
  ``authenticity_status``, ``user``, ``seller`` and the evidence
  columns are not writable, so a ``{"status": "approved"}`` payload
  silently does nothing rather than escalating anything;
* read serializers never expose ``moderation_note``,
  ``reviewer_note``, order numbers, addresses or the seller's
  email. The public listing serializer speaks only in product
  facts, price, condition and an optional seller display name.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from apps.loop.models import (
    LoopCredit,
    LoopItem,
    LoopItemImage,
    RecycleRequest,
    ResaleListing,
    TradeInRequest,
)

__all__ = [
    "LoopCreditSerializer",
    "LoopItemCreateSerializer",
    "LoopItemSerializer",
    "RecycleRequestCreateSerializer",
    "RecycleRequestSerializer",
    "ResaleListingDetailSerializer",
    "ResaleListingListSerializer",
    "TradeInRequestCreateSerializer",
    "TradeInRequestSerializer",
]


class VariantSummarySerializer(serializers.Serializer):
    """Read-only colour/size summary for a loop item's variant."""

    sku = serializers.CharField()
    option_label = serializers.CharField()
    color = serializers.CharField(source="color.name", default=None)
    size = serializers.CharField(source="size.name", default=None)


class ProductSummarySerializer(serializers.Serializer):
    """Catalogue truth for the detail pages: name, brand, category, price."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    slug = serializers.CharField()
    brand = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    product_url = serializers.CharField(source="get_absolute_url", read_only=True)

    def get_brand(self, obj) -> str | None:
        return obj.brand.name if obj.brand else None

    def get_category(self, obj) -> str | None:
        return obj.category.name if obj.category else None


class LoopImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoopItemImage
        fields = ("id", "kind", "alt_text", "position", "image")
        read_only_fields = ("id", "position")


class LoopItemSerializer(serializers.ModelSerializer):
    """Customer-facing view of one's own (or a public) loop item.

    Moderation internals (``moderation_note``, ``reviewed_by``) are
    deliberately absent; the authenticity state is exposed as the
    label-only value it is.
    """

    product = ProductSummarySerializer(read_only=True)
    variant = VariantSummarySerializer(read_only=True)
    condition_label = serializers.CharField(source="get_condition_display", read_only=True)
    type_label = serializers.CharField(source="get_type_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    authenticity_label = serializers.CharField(
        source="get_authenticity_status_display", read_only=True
    )
    images = LoopImageSerializer(many=True, read_only=True)

    class Meta:
        model = LoopItem
        fields = (
            "id",
            "type",
            "type_label",
            "status",
            "status_label",
            "condition",
            "condition_label",
            "condition_notes",
            "title",
            "description",
            "asking_price",
            "authenticity_status",
            "authenticity_label",
            "product",
            "variant",
            "images",
            "created_at",
            "listed_at",
        )
        read_only_fields = fields


class LoopItemCreateSerializer(serializers.Serializer):
    """Write side: ownership reference + seller facts. Nothing else."""

    ownership_source = serializers.ChoiceField(
        choices=(("closet", "closet"), ("order", "order")),
        help_text="Which kind of evidence row the id below points at.",
    )
    ownership_id = serializers.IntegerField(min_value=1)
    type = serializers.ChoiceField(choices=LoopItem.Type.choices)
    condition = serializers.ChoiceField(choices=LoopItem.Condition.choices)
    condition_notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    title = serializers.CharField(required=False, allow_blank=True, max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    asking_price = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )

    def validate(self, attrs):
        loop_type = attrs.get("type")
        price = attrs.get("asking_price")
        if loop_type == LoopItem.Type.RESALE and price is None:
            raise serializers.ValidationError(
                {"asking_price": "A resale listing needs an asking price."}
            )
        if loop_type != LoopItem.Type.RESALE and price is not None:
            raise serializers.ValidationError(
                {"asking_price": "An asking price only applies to resale."}
            )
        return attrs

    def ownership_kwargs(self) -> dict:
        key = (
            "closet_item_id"
            if self.validated_data["ownership_source"] == "closet"
            else "order_item_id"
        )
        return {key: self.validated_data["ownership_id"]}


class ResaleListingListSerializer(serializers.ModelSerializer):
    """Public shelf card. Product facts and price only -- no seller PII."""

    condition = serializers.CharField(source="loop_item.condition")
    condition_label = serializers.CharField(source="loop_item.get_condition_display")
    title = serializers.SerializerMethodField()
    product = ProductSummarySerializer(source="loop_item.product", read_only=True)
    variant = VariantSummarySerializer(source="loop_item.variant", read_only=True)
    authenticity_status = serializers.CharField(source="loop_item.authenticity_status")
    primary_image = serializers.SerializerMethodField()
    url = serializers.CharField(source="get_absolute_url", read_only=True)

    class Meta:
        model = ResaleListing
        fields = (
            "slug",
            "url",
            "title",
            "asking_price",
            "condition",
            "condition_label",
            "authenticity_status",
            "product",
            "variant",
            "primary_image",
            "listed_at",
        )
        read_only_fields = fields

    def get_title(self, obj) -> str:
        return obj.loop_item.display_title

    def get_primary_image(self, obj) -> str | None:
        image = next(
            (img for img in obj.loop_item.images.all() if img.position == 0),
            None,
        )
        if image is None:
            images = list(obj.loop_item.images.all())
            image = images[0] if images else None
        return image.image.url if image else None


class ResaleListingDetailSerializer(ResaleListingListSerializer):
    """Public detail: adds the circularity facts and a public seller name.

    The seller name is the customer's display name only (never the
    email, phone or address); it is elided entirely when the account
    has no name set.
    """

    seller_display_name = serializers.SerializerMethodField()
    description = serializers.CharField(source="loop_item.description", read_only=True)
    condition_notes = serializers.CharField(source="loop_item.condition_notes", read_only=True)
    circularity = serializers.SerializerMethodField()

    class Meta(ResaleListingListSerializer.Meta):
        fields = (
            *ResaleListingListSerializer.Meta.fields,
            "seller_display_name",
            "description",
            "condition_notes",
            "circularity",
            "status",
        )

    def get_seller_display_name(self, obj) -> str | None:
        # first + last only: this User model's ``get_full_name()`` falls back
        # to the email address when no name is set, which must never become
        # the public "seller" field.
        name = f"{obj.seller.first_name} {obj.seller.last_name}".strip()
        return name or None

    def get_circularity(self, obj) -> dict:
        from apps.loop.services.circularity import circularity_info

        return circularity_info(obj.loop_item)


class TradeInRequestSerializer(serializers.ModelSerializer):
    """Customer view of their trade-in. Estimated vs final, clearly labelled."""

    item_title = serializers.CharField(source="loop_item.display_title", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = TradeInRequest
        fields = (
            "id",
            "status",
            "status_label",
            "estimated_credit",
            "final_credit",
            "item_title",
            "created_at",
            "reviewed_at",
            "completed_at",
        )
        read_only_fields = fields


class TradeInRequestCreateSerializer(serializers.Serializer):
    """POST /loop/trade-ins/ -- create and submit a trade-in in one call."""

    ownership_source = serializers.ChoiceField(choices=(("closet", "closet"), ("order", "order")))
    ownership_id = serializers.IntegerField(min_value=1)
    condition = serializers.ChoiceField(choices=LoopItem.Condition.choices)
    condition_notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)

    def ownership_kwargs(self) -> dict:
        key = (
            "closet_item_id"
            if self.validated_data["ownership_source"] == "closet"
            else "order_item_id"
        )
        return {key: self.validated_data["ownership_id"]}


class RecycleRequestSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    item_title = serializers.CharField(source="loop_item.display_title", read_only=True)

    class Meta:
        model = RecycleRequest
        fields = (
            "id",
            "status",
            "status_label",
            "item_title",
            "instructions",
            "material_category",
            "created_at",
            "received_at",
            "processed_at",
        )
        read_only_fields = (
            "id",
            "status",
            "status_label",
            "item_title",
            "instructions",
            "material_category",
            "created_at",
            "received_at",
            "processed_at",
        )


class RecycleRequestCreateSerializer(serializers.Serializer):
    """POST /loop/recycling/ -- create and submit a recycling request."""

    ownership_source = serializers.ChoiceField(choices=(("closet", "closet"), ("order", "order")))
    ownership_id = serializers.IntegerField(min_value=1)
    condition = serializers.ChoiceField(choices=LoopItem.Condition.choices)
    condition_notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)

    def ownership_kwargs(self) -> dict:
        key = (
            "closet_item_id"
            if self.validated_data["ownership_source"] == "closet"
            else "order_item_id"
        )
        return {key: self.validated_data["ownership_id"]}


class LoopCreditSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = LoopCredit
        fields = ("id", "amount", "status", "status_label", "reference", "awarded_at", "expires_at")
        read_only_fields = fields
