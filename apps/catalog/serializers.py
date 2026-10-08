"""Catalogue API serializers.

Read-only by design: Phase 3 exposes the catalogue, and writes go through Django admin where the
validation, the audit trail and the staff permissions already exist.

Two field decisions worth stating out loud, because both are things that leak by accident:

* **``cost_price`` is never serialized.** It is staff-only information, and a public API that
  returned it would hand every margin to anyone who opened devtools.
* **``barcode`` is not serialized either.** The SKU already identifies a variant, and a barcode is
  fulfilment data rather than shopping data.

Money comes out of DRF as a **string** (``"49.00"``). That is DRF's default and it is deliberate:
JSON numbers are IEEE-754 doubles, and a client doing arithmetic on a price in JavaScript loses the
cents sooner or later. ``COERCE_DECIMAL_TO_STRING = False`` would be the change to make if this API
were ever consumed by something that must do its own money maths.

Declaration order is load-bearing: ``ProductDetailSerializer`` embeds the taxonomy serializers, so
they are defined before it.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.catalog.models import (
    Brand,
    Category,
    Collection,
    Color,
    Product,
    ProductImage,
    ProductVariant,
    Size,
)

# --------------------------------------------------------------------------------------
# References: nested objects, minimal fields, no recursion
# --------------------------------------------------------------------------------------


class ColorBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Color
        fields = ("name", "slug", "hex_code")


class SizeBriefSerializer(serializers.ModelSerializer):
    """``code`` is what a shopper reads (``XL``); ``name`` is the spelled-out label."""

    class Meta:
        model = Size
        fields = ("code", "name")


class CategoryBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("name", "slug")


class BrandBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ("name", "slug")


# --------------------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------------------


class CategorySerializer(serializers.ModelSerializer):
    """Category with the numbers a client needs to decide whether to descend.

    ``product_count`` counts *published* products in this category and its descendants, so a client
    never sees a shelf advertised with products it cannot fetch. Both counts are annotated by the
    view; absent (rather than a misleading ``0``) when an unannotated instance is serialized.
    """

    parent = CategoryBriefSerializer(read_only=True)
    children = CategoryBriefSerializer(many=True, read_only=True)
    url = serializers.SerializerMethodField()
    image = serializers.ImageField(read_only=True)
    product_count = serializers.SerializerMethodField()
    subcategory_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = (
            "id",
            "name",
            "slug",
            "url",
            "description",
            "parent",
            "children",
            "image",
            "display_order",
            "product_count",
            "subcategory_count",
        )

    def get_url(self, category: Category) -> str:
        return category.get_absolute_url()

    def get_product_count(self, category: Category) -> int | None:
        return getattr(category, "published_product_count", None)

    def get_subcategory_count(self, category: Category) -> int | None:
        return getattr(category, "published_child_count", None)


class BrandSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    logo = serializers.ImageField(read_only=True)
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Brand
        fields = (
            "id",
            "name",
            "slug",
            "url",
            "description",
            "logo",
            "website_url",
            "product_count",
        )

    def get_url(self, brand: Brand) -> str:
        return brand.get_absolute_url()

    def get_product_count(self, brand: Brand) -> int | None:
        return getattr(brand, "published_product_count", None)


class CollectionSerializer(serializers.ModelSerializer):
    """Collection with its live flag.

    ``is_live`` combines "active" with "inside the date window" -- the same check the storefront
    page makes. A client and a browser must agree on whether a collection exists.
    """

    url = serializers.SerializerMethodField()
    hero_image = serializers.ImageField(read_only=True)
    banner_image = serializers.ImageField(read_only=True)
    is_live = serializers.BooleanField(read_only=True)
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = (
            "id",
            "name",
            "slug",
            "url",
            "description",
            "hero_image",
            "banner_image",
            "is_featured",
            "starts_at",
            "ends_at",
            "is_live",
            "product_count",
        )

    def get_url(self, collection: Collection) -> str:
        return collection.get_absolute_url()

    def get_product_count(self, collection: Collection) -> int | None:
        return getattr(collection, "published_product_count", None)


# --------------------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------------------


class ProductImageSerializer(serializers.ModelSerializer):
    """One photograph.

    ``alt_text`` is not decoration: it is what a screen reader announces, and omitting it would make
    the gallery unusable for a blind shopper. ``color`` scopes the shot so a client can show the
    right photography when a colourway is chosen.
    """

    id = serializers.IntegerField(read_only=True)
    url = serializers.ImageField(source="image", read_only=True)
    width = serializers.SerializerMethodField()
    height = serializers.SerializerMethodField()
    color = ColorBriefSerializer(read_only=True)
    image_source = serializers.CharField(read_only=True)

    class Meta:
        model = ProductImage
        fields = (
            "id",
            "url",
            "alt_text",
            "position",
            "is_primary",
            "width",
            "height",
            "color",
            "source_url",
            "photographer",
            "attribution",
            "license",
            "image_source",
        )

    def get_width(self, image: ProductImage) -> int | None:
        return getattr(image.image, "width", None)

    def get_height(self, image: ProductImage) -> int | None:
        return getattr(image.image, "height", None)


class ProductImageCreateSerializer(serializers.Serializer):
    """Staff serializer for uploading or importing product images via API."""

    image = serializers.ImageField(required=False, allow_null=True)
    source_url = serializers.URLField(required=False, allow_blank=True)
    candidate_image_url = serializers.URLField(required=False, allow_blank=True)
    alt_text = serializers.CharField(max_length=200)
    position = serializers.IntegerField(default=0, required=False)
    is_primary = serializers.BooleanField(default=False, required=False)
    variant_id = serializers.IntegerField(required=False, allow_null=True)
    photographer = serializers.CharField(max_length=200, required=False, allow_blank=True)
    attribution = serializers.CharField(max_length=255, required=False, allow_blank=True)
    license = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_alt_text(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError(_("Alt text is required."))
        return cleaned

    def validate(self, attrs):
        from apps.catalog.image_fetcher import fetch_and_validate_image_file

        image = attrs.get("image")
        source_url = (attrs.get("source_url") or "").strip()
        candidate_url = (attrs.get("candidate_image_url") or "").strip() or None

        if not image and not source_url:
            raise serializers.ValidationError(_("Provide an image file or an external image URL."))

        if source_url and not image:
            try:
                downloaded = fetch_and_validate_image_file(source_url, candidate_url=candidate_url)
                attrs["image"] = downloaded.file
                if not attrs.get("photographer") and downloaded.suggested_photographer:
                    attrs["photographer"] = downloaded.suggested_photographer
                if not attrs.get("attribution") and downloaded.suggested_attribution:
                    attrs["attribution"] = downloaded.suggested_attribution
            except DjangoValidationError as err:
                msg = err.messages if hasattr(err, "messages") else str(err)
                raise serializers.ValidationError({"source_url": msg}) from err
            except Exception as err:
                raise serializers.ValidationError({"source_url": str(err)}) from err

        return attrs


class ProductVariantSerializer(serializers.ModelSerializer):
    """A purchasable SKU.

    ``available`` is deliberately absent: stock arrives with the Phase 4 inventory app, and a
    hard-coded ``true`` here would be a claim the catalogue cannot keep.
    """

    color = ColorBriefSerializer(read_only=True)
    size = SizeBriefSerializer(read_only=True)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    compare_at_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    is_discounted = serializers.BooleanField(read_only=True)

    class Meta:
        model = ProductVariant
        fields = ("sku", "color", "size", "price", "compare_at_price", "is_discounted")


class ProductListSerializer(serializers.ModelSerializer):
    """Card-shaped payload: what a grid needs, and nothing that costs a query per product.

    ``price_min`` / ``price_max`` are declared as ``DecimalField`` reading the annotations applied
    by :func:`apps.catalog.selectors.with_price_range`. Declaring them as ``SerializerMethodField``
    would look equivalent and is not: a method field returns a ``Decimal`` that DRF's JSON encoder
    renders as a **float**, so ``49.90`` would reach a JavaScript client as ``49.9``. Reading the
    annotation through ``DecimalField`` keeps the documented string money all the way out.
    """

    url = serializers.SerializerMethodField()
    brand = BrandBriefSerializer(read_only=True)
    category = CategoryBriefSerializer(read_only=True)
    price_min = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, default=None
    )
    price_max = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, default=None
    )
    image = serializers.SerializerMethodField()
    image_alt = serializers.SerializerMethodField()
    colors = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            "id",
            "name",
            "slug",
            "url",
            "short_description",
            "brand",
            "category",
            "price_min",
            "price_max",
            "image",
            "image_alt",
            "colors",
            "is_on_sale",
            "is_featured",
            "is_new",
            "published_at",
        )

    def get_url(self, product: Product) -> str:
        return product.get_absolute_url()

    def get_image(self, product: Product) -> str | None:
        image = product.primary_image
        return image.image.url if image else None

    def get_image_alt(self, product: Product) -> str:
        image = product.primary_image
        return image.alt_text if image else product.name

    def get_colors(self, product: Product) -> list:
        return ColorBriefSerializer(product.available_colors, many=True).data


class ProductDetailSerializer(ProductListSerializer):
    """Full product payload, including every purchasable variant.

    Adds the descriptive fields to :class:`ProductListSerializer`'s card fields. The view's queryset
    eager-loads every related row, so serialising this costs a fixed number of queries rather than
    one per product.
    """

    fit = serializers.SlugRelatedField(slug_field="name", read_only=True)
    materials = serializers.SlugRelatedField(many=True, slug_field="name", read_only=True)
    tags = serializers.SlugRelatedField(many=True, slug_field="name", read_only=True)
    collections = CollectionSerializer(many=True, read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)

    class Meta(ProductListSerializer.Meta):
        fields = (
            *ProductListSerializer.Meta.fields,
            "description",
            "fit",
            "materials",
            "tags",
            "collections",
            "variants",
            "images",
            "created_at",
            "updated_at",
        )


class VisualSearchResultSerializer(serializers.Serializer):
    """Card and matching information for visual discovery results."""

    id = serializers.IntegerField(source="product.pk")
    name = serializers.CharField(source="product.name")
    slug = serializers.CharField(source="product.slug")
    url = serializers.SerializerMethodField()
    primary_image_url = serializers.SerializerMethodField()
    category = serializers.CharField(source="product.category.name", default="")
    brand = serializers.CharField(source="product.brand.name", default="")
    score = serializers.FloatField()
    reasons = serializers.ListField(child=serializers.CharField())
    matched_signals = serializers.DictField(default=dict)

    def get_url(self, obj) -> str:
        return obj.product.get_absolute_url()

    def get_primary_image_url(self, obj) -> str | None:
        img = obj.product.primary_image
        return img.image.url if img and img.image else None


__all__ = [
    "BrandBriefSerializer",
    "BrandSerializer",
    "CategoryBriefSerializer",
    "CategorySerializer",
    "CollectionSerializer",
    "ColorBriefSerializer",
    "ProductDetailSerializer",
    "ProductImageSerializer",
    "ProductListSerializer",
    "ProductVariantSerializer",
    "SizeBriefSerializer",
    "VisualSearchResultSerializer",
]
