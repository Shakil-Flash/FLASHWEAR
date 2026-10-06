"""Catalogue admin.

The admin is the *only* write path for the catalogue in Phase 3, so it is where the model invariants
have to become usable: an inline that refuses a duplicate combination with a sentence a merchandiser
can act on beats a 500 with a constraint name in it.

Three conventions here:

* **Nothing is a raw widget.** Every model with a price, a slug or an image gets a form, so the
  ``ModelForm`` can translate database constraints into field errors.
* **Inlines save on the parent.** Variant and image inlines save with the product, so publishing a
  garment with its sizes is one save rather than one save per size.
* **Read-only facts stay visible.** ``cost_price``, SKU and margin appear in the variant list but
  never in a public serializer, which is exactly why they are on this screen.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.catalog import services
from apps.catalog.forms import (
    BrandAdminForm,
    CategoryAdminForm,
    CollectionAdminForm,
    ProductAdminForm,
    ProductImageForm,
    ProductVariantForm,
)
from apps.catalog.models import (
    Brand,
    Category,
    Collection,
    Color,
    Fit,
    Material,
    Product,
    ProductImage,
    ProductTag,
    ProductVariant,
    Size,
)


class ProductImageInline(admin.TabularInline):
    """Photographs, ordered and primary-flagged, on the product form."""

    model = ProductImage
    form = ProductImageForm
    extra = 1
    fields = (
        "preview",
        "image",
        "source_url",
        "candidate_image_url",
        "alt_text",
        "variant",
        "position",
        "is_primary",
        "photographer",
        "attribution",
        "license",
        "source_badge",
    )
    ordering = ("position", "id")
    readonly_fields = ("color_name", "preview", "source_badge")
    autocomplete_fields = ("variant",)

    @admin.display(description=_("Colour"))
    def color_name(self, image: ProductImage):
        """Named ``color_name`` rather than ``color`` so it cannot shadow the model property."""
        variant = image.variant
        return variant.color.name if variant and variant.color_id else "—"

    @admin.display(description=_("Preview"))
    def preview(self, image: ProductImage):
        if not image.pk or not image.image:
            return format_html('<span style="color:#9ca3af;font-size:12px;">{}</span>', "—")
        try:
            url = image.image.url
        except Exception:
            return "—"
        img_style = (
            "max-height:75px;max-width:90px;object-fit:cover;"
            "border-radius:6px;border:1px solid #e5e7eb;"
        )
        return format_html(
            '<a href="{url}" target="_blank" rel="noopener noreferrer">'
            '<img src="{url}" style="{style}" alt="{alt}" />'
            '</a>',
            url=url,
            style=img_style,
            alt=image.alt_text or "Preview",
        )

    @admin.display(description=_("Source"))
    def source_badge(self, image: ProductImage):
        if not image.pk:
            return "—"
        if image.source_url:
            from urllib.parse import urlsplit

            domain = urlsplit(image.source_url).netloc or "external"
            link_style = "color:#0284c7;text-decoration:underline;"
            return format_html(
                '<span class="source-badge-external">'
                '🌐 External (<a href="{url}" target="_blank" rel="noopener noreferrer"'
                ' style="{link_style}">{domain}</a>)'
                '</span>',
                url=image.source_url,
                domain=domain,
                link_style=link_style,
            )
        return format_html(
            '<span class="source-badge-upload">{}</span>',
            _("📁 Uploaded File"),
        )


class ProductVariantInline(admin.TabularInline):
    """Sizes and colourways, each with its own SKU and price.

    ``extra = 0`` on purpose: a merchandiser should add the sizes a garment actually comes in, not
    be shown four empty rows that suggest "one size fits all" is a reasonable default.
    """

    model = ProductVariant
    form = ProductVariantForm
    extra = 0
    fields = ("sku", "color", "size", "price", "compare_at_price", "cost_price", "is_active")
    readonly_fields = ("margin",)
    autocomplete_fields = ("color", "size")

    @admin.display(description=_("Margin"))
    def margin(self, variant: ProductVariant) -> str:
        """Read-only: a merchandiser sees the margin they are setting, not just the numbers."""
        margin = variant.margin_percent
        return "—" if margin is None else f"{margin}%"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """The catalogue's main screen."""

    form = ProductAdminForm
    list_display = (
        "name",
        "sku_summary",
        "category",
        "brand",
        "status_badge",
        "price_summary",
        "is_featured",
        "is_new",
        "updated_at",
    )
    list_filter = (
        "status",
        "is_featured",
        "is_new",
        "category",
        "brand",
        "fit",
        "collections",
        "materials",
        "tags",
        "published_at",
        "updated_at",
    )
    search_fields = ("name", "slug", "short_description", "description", "variants__sku")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("brand", "category", "fit")
    filter_horizontal = ("collections", "materials", "tags")
    inlines = (ProductVariantInline, ProductImageInline)
    # One click on Save creates or updates every variant and image above.
    save_on_top = True
    readonly_fields = ("created_at", "updated_at", "published_at_preview")

    class Media:
        css = {"all": ("admin/css/image_url_manager.css",)}
        js = ("admin/js/image_url_manager.js",)

    fieldsets = (
        (None, {"fields": ("name", "slug", "short_description", "description")}),
        (
            _("Classification"),
            {
                "fields": ("category", "brand", "fit", "materials", "tags", "collections"),
                "description": _(
                    "A garment belongs to exactly one category. Brand is optional; collections are "
                    "how the same garment appears in more than one story."
                ),
            },
        ),
        (
            _("Publishing"),
            {
                "fields": (
                    "status",
                    "is_featured",
                    "is_new",
                    "published_at_preview",
                ),
                "description": _(
                    "Draft is invisible everywhere. Active appears on the storefront and in the "
                    "public API. Archived keeps the record but removes it from both."
                ),
            },
        ),
        (
            _("Search engines"),
            {
                "classes": ("collapse",),
                "fields": ("seo_title", "seo_description"),
                "description": _(
                    "Optional. Leave blank and FLASHWEAR derives a title and description from the "
                    "product itself."
                ),
            },
        ),
        (_("Timestamps"), {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    @admin.display(description=_("Variants"))
    def sku_summary(self, product: Product) -> str:
        total = getattr(product, "variant_count", None)
        if total is None:
            total = product.variants.count()
        return f"{total} SKU"

    @admin.display(description=_("Price"), ordering="id")
    def price_summary(self, product: Product) -> str:
        low, high = product.price_range
        if low is None:
            return "—"
        return f"{low:,.2f}" if low == high else f"{low:,.2f} – {high:,.2f}"

    @admin.display(description=_("Status"))
    def status_badge(self, product: Product):
        colors = {
            Product.Status.DRAFT: "#64748b",
            Product.Status.ACTIVE: "#059669",
            Product.Status.ARCHIVED: "#b91c1c",
        }
        label = product.get_status_display()
        # Scheduled-but-future: active status, not yet visible. Worth saying explicitly, because
        # "I set it to Active and nothing happened" is the most common publishing surprise.
        if product.status == Product.Status.ACTIVE and product.published_at:
            if product.published_at > timezone.now():
                label = f"{label} (scheduled)"
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            colors.get(product.status, "#0f172a"),
            label,
        )

    @admin.display(description=_("Published at"))
    def published_at_preview(self, product: Product):
        if not product.published_at:
            return "Set automatically when the product goes Active."
        return product.published_at

    def get_queryset(self, request):
        """Add the variant count the list column shows, in the same query as the page.

        Without this, the ``SKU`` column would run a COUNT per row: the classic admin N+1 that
        turns a 50-product list page into 51 queries. Active variants are prefetched as well,
        because ``price_summary`` reads :attr:`Product.price_range` -- and an unprefetched product
        aggregates its variants, one query per row, for the same reason.
        """
        return (
            super()
            .get_queryset(request)
            .select_related("category", "brand")
            .annotate(variant_count=Count("variants", distinct=True))
            .prefetch_related(
                "collections",
                "materials",
                "tags",
                Prefetch(
                    "variants",
                    queryset=ProductVariant.objects.filter(is_active=True).select_related(
                        "color", "size"
                    ),
                ),
            )
        )

    @admin.action(description=_("Publish selected products"))
    def publish_selected(self, request, queryset):
        """Bulk-publish through the service so every invariant is applied, not just the status.

        Setting ``status = ACTIVE`` in the list editor would skip ``published_at`` and the
        "category must be active" rule. Going through :func:`apps.catalog.services.publish` is the
        only way to make fifty products behave like fifty individual saves.
        """
        published = 0
        for product in queryset:
            try:
                services.publish(product)
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"{product}: {'; '.join(exc.messages)}",
                    level=messages.ERROR,
                )
                continue
            published += 1
        self.message_user(
            request,
            _("%(count)d product published.") % {"count": published},
            level=messages.SUCCESS,
        )

    @admin.action(description=_("Archive selected products"))
    def archive_selected(self, request, queryset):
        archived = 0
        for product in queryset:
            services.archive(product)
            archived += 1
        self.message_user(
            request,
            _("%(count)d product archived.") % {"count": archived},
            level=messages.SUCCESS,
        )

    actions = ("publish_selected", "archive_selected")


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    """Flat access to every SKU -- for price audits and lookups by barcode."""

    form = ProductVariantForm
    list_display = (
        "sku",
        "product",
        "option_label",
        "price",
        "margin",
        "is_active",
        "product_status",
    )
    list_filter = ("is_active", "color", "size", "product__status", "product__category")
    search_fields = ("sku", "barcode", "product__name")
    autocomplete_fields = ("product", "color", "size")
    readonly_fields = ("margin_percent_display", "created_at", "updated_at")

    @admin.display(description=_("Margin"))
    def margin(self, variant: ProductVariant) -> str:
        margin = variant.margin_percent
        return "—" if margin is None else f"{margin}%"

    @admin.display(description=_("Margin"))
    def margin_percent_display(self, variant: ProductVariant):
        margin = variant.margin_percent
        if margin is None:
            return "No cost price recorded."
        return format_html("<strong>{}%</strong>", margin)

    @admin.display(description=_("Product status"))
    def product_status(self, variant: ProductVariant) -> str:
        return variant.product.get_status_display()


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    form = ProductImageForm
    list_display = (
        "preview_thumb",
        "product",
        "alt_text",
        "color_name",
        "position",
        "is_primary",
        "source_badge",
    )
    list_filter = ("is_primary", "variant__color__name", "product__category")
    search_fields = ("alt_text", "product__name", "source_url", "photographer")
    autocomplete_fields = ("product", "variant")
    readonly_fields = ("preview_thumb", "color_name", "source_badge")
    actions = ("make_primary",)

    fieldsets = (
        (None, {"fields": ("product", "image", "source_url", "candidate_image_url")}),
        (_("Gallery & Variation"), {"fields": ("alt_text", "variant", "position", "is_primary")}),
        (_("Attribution & Licensing"), {"fields": ("photographer", "attribution", "license")}),
        (_("Overview"), {"fields": ("preview_thumb", "source_badge")}),
    )

    class Media:
        css = {"all": ("admin/css/image_url_manager.css",)}
        js = ("admin/js/image_url_manager.js",)

    @admin.display(description=_("Image"))
    def preview_thumb(self, image: ProductImage):
        if not image.pk or not image.image:
            return format_html('<span style="color:#9ca3af;font-size:12px;">—</span>')
        try:
            url = image.image.url
        except Exception:
            return "—"
        img_style = "max-height:80px;border-radius:8px;border:1px solid #e5e7eb;"
        return format_html(
            '<a href="{url}" target="_blank" rel="noopener noreferrer">'
            '<img src="{url}" style="{style}" alt="{alt}" />'
            '</a>',
            url=url,
            style=img_style,
            alt=image.alt_text or "Preview",
        )

    @admin.display(description=_("Source"))
    def source_badge(self, image: ProductImage):
        if not image.pk:
            return "—"
        if image.source_url:
            from urllib.parse import urlsplit

            domain = urlsplit(image.source_url).netloc or "external"
            link_style = "color:#0284c7;text-decoration:underline;"
            return format_html(
                '<span class="source-badge-external">'
                '🌐 External (<a href="{url}" target="_blank" rel="noopener noreferrer"'
                ' style="{link_style}">{domain}</a>)'
                '</span>',
                url=image.source_url,
                domain=domain,
                link_style=link_style,
            )
        return format_html(
            '<span class="source-badge-upload">{}</span>',
            _("📁 Uploaded File"),
        )

    @admin.display(description=_("Colour"), ordering="variant__color__name")
    def color_name(self, image: ProductImage):
        """The model's ``color`` property, surfaced under a name that does not shadow it."""
        return image.color.name if image.color_id else "Shared"

    @admin.action(description=_("Promote selected images to primary"))
    def make_primary(self, request, queryset):
        """One primary image per product, enforced in the model; this just fixes the intent.

        Declared as a method rather than a module-level function on purpose: Django resolves an
        action name with ``getattr(self, name)``, so a bare function assigned to ``actions`` after
        the class body is silently ignored and the action never appears in the UI.
        """
        promoted = 0
        for image in queryset:
            if image.is_primary:
                continue
            services.promote_primary_image(image.product, image)
            promoted += 1
        self.message_user(
            request,
            _("%(count)d image(s) promoted to primary.") % {"count": promoted},
            level=messages.SUCCESS,
        )


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    form = CategoryAdminForm
    list_display = (
        "name",
        "parent",
        "display_order",
        "is_active",
        "direct_product_count",
        "published_subtree_count",
    )
    list_filter = ("is_active", "parent")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("depth", "created_at", "updated_at")
    autocomplete_fields = ("parent",)

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "slug",
                    "parent",
                    "description",
                    "image",
                    "image_url",
                    "display_order",
                )
            },
        ),
        (_("Visibility"), {"fields": ("is_active",)}),
        (_("Checks"), {"classes": ("collapse",), "fields": ("depth",)}),
        (_("Timestamps"), {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    class Media:
        css = {"all": ("admin/css/image_url_manager.css",)}
        js = ("admin/js/image_url_manager.js",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(direct_products=Count("products", distinct=True))
            .select_related("parent")
        )

    @admin.display(description=_("Depth"))
    def depth(self, category: Category) -> str:
        """How many ancestors sit above this node -- makes an accidental deep tree obvious."""
        return category.depth

    @admin.display(description=_("Products (direct)"), ordering="direct_products")
    def direct_product_count(self, category: Category) -> int:
        return getattr(category, "direct_products", 0)

    @admin.display(description=_("Published (subtree)"))
    def published_subtree_count(self, category: Category) -> int:
        """Published products at or below this node -- what a shopper would find here."""
        return _published_in_subtree(category)

    def delete_model(self, request, obj: Category):
        if obj.products.exists():
            self.message_user(
                request,
                _("That category still holds products, so it cannot be deleted. Hide it instead."),
                level=messages.ERROR,
            )
            return
        super().delete_model(request, obj)


def _published_in_subtree(category: Category) -> int:
    from apps.catalog.selectors import published_category_counts

    return published_category_counts().get(category.pk, 0)


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    form = BrandAdminForm
    list_display = ("name", "website_url", "is_active", "display_order", "product_count")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "slug",
                    "website_url",
                    "logo",
                    "logo_url",
                    "description",
                    "display_order",
                )
            },
        ),
        (_("Visibility"), {"fields": ("is_active",)}),
        (_("Timestamps"), {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    class Media:
        css = {"all": ("admin/css/image_url_manager.css",)}
        js = ("admin/js/image_url_manager.js",)

    def get_queryset(self, request):
        # ``product_total`` rather than ``products``: ``Brand.products`` is the reverse
        # relation, and annotating with the same name is a ValueError, not a shadow.
        return (
            super().get_queryset(request).annotate(product_total=Count("products", distinct=True))
        )

    @admin.display(description=_("Products"), ordering="product_total")
    def product_count(self, brand: Brand) -> int:
        return getattr(brand, "product_total", 0)


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    form = CollectionAdminForm
    list_display = (
        "name",
        "window",
        "live_badge",
        "is_featured",
        "display_order",
        "product_count",
    )
    list_filter = ("is_active", "is_featured")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("live_badge", "product_count", "created_at", "updated_at")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "slug",
                    "description",
                    "hero_image",
                    "hero_image_url",
                    "banner_image",
                    "banner_image_url",
                    "display_order",
                    "is_active",
                    "is_featured",
                )
            },
        ),
        (
            _("Schedule"),
            {
                "fields": ("starts_at", "ends_at"),
                "description": _(
                    "Both are optional. With a window set, the collection page only exists between "
                    "these dates; without one, it runs until you switch it off."
                ),
            },
        ),
        (
            _("Membership"),
            {
                "classes": ("collapse",),
                "fields": ("product_count",),
                "description": _(
                    "Products are added to a collection from the product form, where the "
                    "collection list is a filter list."
                ),
            },
        ),
        (
            _("Timestamps"),
            {
                "classes": ("collapse",),
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    class Media:
        css = {"all": ("admin/css/image_url_manager.css",)}
        js = ("admin/js/image_url_manager.js",)


    def get_queryset(self, request):
        # ``product_total`` rather than ``products``: ``Brand.products`` is the reverse
        # relation, and annotating with the same name is a ValueError, not a shadow.
        return (
            super().get_queryset(request).annotate(product_total=Count("products", distinct=True))
        )

    @admin.display(description=_("Status"))
    def live_badge(self, collection: Collection):
        if not collection.is_active:
            return format_html('<span style="color:#64748b">Inactive</span>')
        if collection.is_current:
            return format_html('<span style="color:#059669;font-weight:600">Live</span>')
        return format_html('<span style="color:#b45309;font-weight:600">Outside window</span>')

    @admin.display(description=_("Window"))
    def window(self, collection: Collection) -> str:
        if not collection.starts_at and not collection.ends_at:
            return "Always"
        start = collection.starts_at.strftime("%j %b") if collection.starts_at else "…"
        end = collection.ends_at.strftime("%j %b") if collection.ends_at else "…"
        return f"{start} → {end}"

    @admin.display(description=_("Products"), ordering="product_total")
    def product_count(self, collection: Collection) -> int:
        return getattr(collection, "product_total", 0)


@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "swatch",
        "hex_code",
        "display_order",
        "is_active",
        "variant_count",
    )
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("swatch", "created_at", "updated_at")

    def get_queryset(self, request):
        # ``Color.variants`` is the reverse relation; the alias must differ from it.
        return (
            super().get_queryset(request).annotate(variant_total=Count("variants", distinct=True))
        )

    @admin.display(description=_("Swatch"))
    def swatch(self, color: Color) -> str:
        if not color.hex_code:
            return "—"
        return format_html(
            '<span style="display:inline-block;width:24px;height:24px;border-radius:9999px;'
            'background:{};border:1px solid #cbd5e1"></span> {}',
            color.hex_code,
            color.hex_code,
        )

    @admin.display(description=_("Variants"), ordering="variant_total")
    def variant_count(self, color: Color) -> int:
        return getattr(color, "variant_total", 0)


@admin.register(Size)
class SizeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "display_order", "is_active", "variant_count")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        # ``Color.variants`` is the reverse relation; the alias must differ from it.
        return (
            super().get_queryset(request).annotate(variant_total=Count("variants", distinct=True))
        )

    @admin.display(description=_("Variants"), ordering="variant_total")
    def variant_count(self, size: Size) -> int:
        return getattr(size, "variant_total", 0)


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "display_order", "product_count")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        # ``product_total`` rather than ``products``: ``Brand.products`` is the reverse
        # relation, and annotating with the same name is a ValueError, not a shadow.
        return (
            super().get_queryset(request).annotate(product_total=Count("products", distinct=True))
        )

    @admin.display(description=_("Products"), ordering="product_total")
    def product_count(self, material: Material) -> int:
        return getattr(material, "product_total", 0)


@admin.register(Fit)
class FitAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "description", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")


@admin.register(ProductTag)
class ProductTagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "product_count")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        # ``product_total`` rather than ``products``: ``Brand.products`` is the reverse
        # relation, and annotating with the same name is a ValueError, not a shadow.
        return (
            super().get_queryset(request).annotate(product_total=Count("products", distinct=True))
        )

    @admin.display(description=_("Products"), ordering="product_total")
    def product_count(self, tag: ProductTag) -> int:
        return getattr(tag, "product_total", 0)
