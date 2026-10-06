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
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.backoffice.models import AuditEvent
from apps.backoffice.services.audit import record as record_audit
from apps.catalog import selectors, seo, services
from apps.catalog.forms import (
    BrandAdminForm,
    BulkAssignBrandForm,
    BulkAssignCategoryForm,
    BulkAssignCollectionForm,
    BulkUpdateTagsForm,
    CategoryAdminForm,
    CollectionAdminForm,
    ProductAdminForm,
    ProductImageForm,
    ProductVariantForm,
)
from apps.catalog.import_export import (
    execute_catalog_import,
    export_catalog_to_csv,
    validate_catalog_csv,
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
from apps.catalog.readiness import evaluate_product_readiness
from apps.catalog.seo import breadcrumb_schema


class ProductImageInline(admin.TabularInline):
    """Photographs, ordered and primary-flagged, on the product form."""

    model = ProductImage
    form = ProductImageForm
    verbose_name = _("Product Image")
    verbose_name_plural = _("Images & Media Gallery")
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


class StockStatusFilter(admin.SimpleListFilter):
    """Filter products by available inventory stock level."""

    title = _("Stock status")
    parameter_name = "stock_status"

    def lookups(self, request, model_admin):
        return (
            ("in_stock", _("In Stock (>5)")),
            ("low_stock", _("Low Stock (1–5)")),
            ("out_of_stock", _("Out of Stock (0)")),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == "in_stock":
            return queryset.filter(variants__stock__on_hand__gt=5).distinct()
        elif val == "low_stock":
            return queryset.filter(
                variants__stock__on_hand__gte=1, variants__stock__on_hand__lte=5
            ).distinct()
        elif val == "out_of_stock":
            return queryset.filter(
                Q(variants__stock__on_hand__isnull=True) | Q(variants__stock__on_hand=0)
            ).distinct()
        return queryset


class ReadinessFilter(admin.SimpleListFilter):
    """Filter products by content readiness for storefront publishing."""

    title = _("Readiness")
    parameter_name = "readiness"

    def lookups(self, request, model_admin):
        return (
            ("ready", _("Ready for Publishing")),
            ("needs_attention", _("Needs Attention / Warnings")),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == "ready":
            return (
                queryset.filter(
                    images__is_primary=True,
                    variants__is_active=True,
                    variants__price__gt=0,
                    category__is_active=True,
                )
                .exclude(short_description="")
                .distinct()
            )
        elif val == "needs_attention":
            return queryset.exclude(
                images__is_primary=True,
                variants__is_active=True,
                variants__price__gt=0,
                category__is_active=True,
            ).distinct()
        return queryset


class ProductVariantInline(admin.TabularInline):
    """Sizes and colourways, each with its own SKU, price, and authoritative stock."""

    model = ProductVariant
    form = ProductVariantForm
    verbose_name = _("Product Variant, Pricing & Stock")
    verbose_name_plural = _("Variants, Pricing & Stock")
    extra = 0
    fields = (
        "sku",
        "color",
        "size",
        "price",
        "compare_at_price",
        "cost_price",
        "margin",
        "stock_status",
        "stock_adjustment",
        "is_active",
    )
    readonly_fields = ("margin", "stock_status")
    autocomplete_fields = ("color", "size")

    @admin.display(description=_("Margin"))
    def margin(self, variant: ProductVariant) -> str:
        """Read-only: a merchandiser sees the margin they are setting, not just the numbers."""
        margin = variant.margin_percent
        return "—" if margin is None else f"{margin}%"

    @admin.display(description=_("Stock"))
    def stock_status(self, variant: ProductVariant):
        if not variant.pk:
            return "—"
        stock = getattr(variant, "stock", None)
        if not stock:
            return format_html(
                '<span class="stock-badge stock-badge-out">{}</span> '
                '<span style="font-size:11px;color:#64748b;">({})</span>',
                _("0 avail"),
                _("0 on hand"),
            )
        avail = stock.available
        if avail > 5:
            cls_name = "stock-badge-in"
        elif avail > 0:
            cls_name = "stock-badge-low"
        else:
            cls_name = "stock-badge-out"
        return format_html(
            '<span class="stock-badge {}">{} avail</span> '
            '<span style="font-size:11px;color:#64748b;">({} on hand, {} held)</span>',
            cls_name,
            avail,
            stock.on_hand,
            stock.reserved,
        )


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """The catalogue's primary management dashboard."""

    form = ProductAdminForm
    list_per_page = 50
    save_on_top = True

    list_display = (
        "primary_image_thumb",
        "name",
        "sku_summary",
        "category",
        "brand",
        "price_summary",
        "stock_status_badge",
        "status_badge",
        "readiness_badge",
        "preview_link",
        "updated_at",
        "created_at",
    )
    list_filter = (
        "status",
        StockStatusFilter,
        ReadinessFilter,
        "is_featured",
        "is_new",
        "category",
        "brand",
        "fit",
        "collections",
        "tags",
        "published_at",
        "created_at",
        "updated_at",
    )
    search_fields = (
        "name",
        "slug",
        "short_description",
        "description",
        "variants__sku",
        "brand__name",
        "category__name",
    )
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("brand", "category", "fit")
    filter_horizontal = ("collections", "materials", "tags")
    inlines = (ProductVariantInline, ProductImageInline)

    class Media:
        css = {"all": ("admin/css/image_url_manager.css",)}
        js = ("admin/js/image_url_manager.js",)

    fieldsets = (
        (
            _("General & Basic Information"),
            {
                "fields": ("name", "slug", "short_description", "description"),
                "description": _(
                    "Core product identity, naming, and customer-facing descriptions."
                ),
            },
        ),
        (
            _("Categories & Brand"),
            {
                "fields": ("category", "brand", "fit", "materials"),
                "description": _(
                    "Primary product hierarchy, brand ownership, garment silhouette fit, "
                    "and textile materials."
                ),
            },
        ),
        (
            _("Collections & Merchandising"),
            {
                "fields": ("collections", "tags", "is_featured", "is_new"),
                "description": _(
                    "Seasonal collections, thematic tags, and front-page merchandising highlights."
                ),
            },
        ),
        (
            _("Publishing & Readiness"),
            {
                "fields": (
                    "status",
                    "preview_storefront_action",
                    "published_at_preview",
                    "readiness_checklist",
                ),
                "description": _(
                    "Publishing state workflow (Draft, Active, Archived) and editorial "
                    "readiness validation."
                ),
            },
        ),
        (
            _("Variants, Pricing & Inventory Matrix"),
            {
                "classes": ("collapse",),
                "fields": ("variant_matrix_table",),
                "description": _(
                    "Live matrix of SKUs, options, pricing, margins, and warehouse "
                    "inventory levels."
                ),
            },
        ),
        (
            _("Search Engines & Social SEO"),
            {
                "classes": ("collapse",),
                "fields": (
                    "seo_title",
                    "seo_description",
                    "seo_canonical_preview",
                    "seo_serp_preview",
                    "seo_social_preview",
                ),
                "description": _(
                    "Search engine optimization (Google SERP) and OpenGraph / Twitter "
                    "social card previews."
                ),
            },
        ),
        (
            _("Timestamps & System Audit"),
            {"classes": ("collapse",), "fields": ("created_at", "updated_at")},
        ),
    )

    readonly_fields = (
        "created_at",
        "updated_at",
        "published_at_preview",
        "readiness_checklist",
        "preview_storefront_action",
        "variant_matrix_table",
        "seo_canonical_preview",
        "seo_serp_preview",
        "seo_social_preview",
    )

    # ----------------------------------------------------------------------------------
    # Queryset & Performance Optimizations (Zero N+1)
    # ----------------------------------------------------------------------------------

    def get_queryset(self, request):
        """Add the variant count the list column shows, in the same query as the page.

        Without this, the ``SKU`` column would run a COUNT per row: the classic admin N+1 that
        turns a 50-product list page into 51 queries. Variants (with stock, color, size) and
        images are prefetched so price_range, primary_image, readiness, and stock status
        read from memory without per-row queries.
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
                    queryset=ProductVariant.objects.select_related("color", "size", "stock"),
                ),
                Prefetch(
                    "images",
                    queryset=ProductImage.objects.all(),
                ),
            )
        )

    # ----------------------------------------------------------------------------------
    # Display Methods for Dashboard
    # ----------------------------------------------------------------------------------

    @admin.display(description=_("Image"))
    def primary_image_thumb(self, product: Product):
        image_obj = product.primary_image
        if not image_obj or not image_obj.image:
            return mark_safe(
                '<div style="width:38px;height:38px;border-radius:4px;background:#f1f5f9;'
                'display:flex;align-items:center;justify-content:center;color:#94a3b8;'
                'font-size:10px;border:1px solid #e2e8f0;">—</div>'
            )
        try:
            url = image_obj.image.url
        except Exception:
            return mark_safe('<span style="color:#94a3b8;">—</span>')

        return format_html(
            '<img src="{url}" style="width:38px;height:38px;object-fit:cover;'
            'border-radius:4px;border:1px solid #cbd5e1;" alt="{alt}" />',
            url=url,
            alt=image_obj.alt_text or product.name,
        )

    @admin.display(description=_("Variants"), ordering="variant_count")
    def sku_summary(self, product: Product) -> str:
        total = getattr(product, "variant_count", None)
        if total is None:
            total = product.variants.count()
        return f"{total} SKU"

    @admin.display(description=_("Price"))
    def price_summary(self, product: Product) -> str:
        low, high = product.price_range
        if low is None:
            return "—"
        return f"{low:,.2f}" if low == high else f"{low:,.2f} – {high:,.2f}"

    @admin.display(description=_("Stock"))
    def stock_status_badge(self, product: Product):
        variants = list(product.variants.all())
        if not variants:
            return format_html(
                '<span class="stock-badge stock-badge-out">{}</span>',
                _("0 out of stock"),
            )
        on_hand = sum(getattr(v.stock, "on_hand", 0) for v in variants if hasattr(v, "stock"))
        reserved = sum(getattr(v.stock, "reserved", 0) for v in variants if hasattr(v, "stock"))
        avail = max(0, on_hand - reserved)
        if avail > 5:
            return format_html(
                '<span class="stock-badge stock-badge-in">{}</span>',
                _("%(avail)d in stock") % {"avail": avail},
            )
        elif avail > 0:
            return format_html(
                '<span class="stock-badge stock-badge-low">{}</span>',
                _("%(avail)d low stock") % {"avail": avail},
            )
        else:
            return format_html(
                '<span class="stock-badge stock-badge-out">{}</span>',
                _("0 out of stock"),
            )

    @admin.display(description=_("Status"))
    def status_badge(self, product: Product):
        colors = {
            Product.Status.DRAFT: "#64748b",
            Product.Status.ACTIVE: "#059669",
            Product.Status.ARCHIVED: "#b91c1c",
        }
        label = product.get_status_display()
        if product.status == Product.Status.ACTIVE and product.published_at:
            if product.published_at > timezone.now():
                label = f"{label} (scheduled)"
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            colors.get(product.status, "#0f172a"),
            label,
        )

    @admin.display(description=_("Readiness"))
    def readiness_badge(self, product: Product):
        readiness = evaluate_product_readiness(product)
        if readiness.is_ready:
            return format_html(
                '<span class="readiness-badge readiness-badge-ready"'
                ' title="All content checks passed">✓ Ready</span>'
            )
        tooltip = escape("; ".join(readiness.warnings))
        return format_html(
            '<span class="readiness-badge readiness-badge-warn"'
            ' title="{}">⚠ {} warnings</span>',
            tooltip,
            readiness.warning_count,
        )

    @admin.display(description=_("Preview"))
    def preview_link(self, product: Product):
        url = reverse("admin:catalog_product_preview", args=[product.pk])
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer"'
            ' class="admin-preview-btn">👁 Preview</a>',
            url,
        )

    @admin.display(description=_("Published at"))
    def published_at_preview(self, product: Product):
        if not product.published_at:
            return "Set automatically when the product goes Active."
        return product.published_at

    @admin.display(description=_("Storefront Actions"))
    def preview_storefront_action(self, product: Product):
        if not product.pk:
            return _("Save product first to preview.")
        preview_url = reverse("admin:catalog_product_preview", args=[product.pk])
        store_url = reverse("catalog:product-detail", kwargs={"slug": product.slug})
        btn_dark = (
            "background:#0f172a;color:#ffffff;padding:6px 14px;border-radius:6px;"
            "font-weight:600;font-size:12px;text-decoration:none;display:inline-flex;"
            "align-items:center;gap:6px;"
        )
        btn_light = (
            "background:#f1f5f9;color:#0f172a;border:1px solid #cbd5e1;padding:6px 14px;"
            "border-radius:6px;font-weight:600;font-size:12px;text-decoration:none;"
            "display:inline-flex;align-items:center;gap:6px;"
        )
        return format_html(
            '<div style="display:flex;gap:10px;align-items:center;padding:4px 0;">'
            '<a href="{preview}" target="_blank" rel="noopener noreferrer" class="button" '
            'style="{btn_dark}">'
            '👁 Admin Preview</a>'
            '<a href="{store}" target="_blank" rel="noopener noreferrer" class="button" '
            'style="{btn_light}">'
            '↗ View Storefront</a>'
            '</div>',
            preview=preview_url,
            store=store_url,
            btn_dark=btn_dark,
            btn_light=btn_light,
        )

    # ----------------------------------------------------------------------------------
    # Read-only Form Previews (Readiness, Variant Matrix, SEO)
    # ----------------------------------------------------------------------------------

    @admin.display(description=_("Readiness Checklist"))
    def readiness_checklist(self, product: Product):
        if not product.pk:
            return _("Save product first to evaluate readiness.")
        readiness = evaluate_product_readiness(product)
        if readiness.is_ready:
            return format_html(
                '<div style="color:#059669;font-weight:600;padding:6px 0;">'
                '✓ Product is ready for storefront publication. All editorial checks passed.'
                '</div>'
            )
        items = "".join(
            f"<li style='margin-bottom:4px;'>{escape(w)}</li>" for w in readiness.warnings
        )
        return format_html(
            '<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:6px;'
            'padding:12px;color:#92400e;">'
            '<strong style="display:block;margin-bottom:6px;">'
            '⚠️ Recommended before publishing ({count} items):'
            '</strong>'
            '<ul style="margin:0;padding-left:20px;font-size:12px;">{items}</ul>'
            '</div>',
            count=readiness.warning_count,
            items=mark_safe(items),
        )

    @admin.display(description=_("Variant Matrix"))
    def variant_matrix_table(self, product: Product):
        if not product.pk:
            return _("Save product first to view variant matrix.")
        variants = list(
            product.variants.select_related("color", "size", "stock").order_by(
                "color__display_order", "size__display_order", "sku"
            )
        )
        if not variants:
            return format_html(
                '<div style="color:#64748b;font-style:italic;">No variants created yet.</div>'
            )

        rows = []
        for v in variants:
            stk = getattr(v, "stock", None)
            on_hand = stk.on_hand if stk else 0
            avail = stk.available if stk else 0
            margin = v.margin_percent
            margin_str = f"{margin}%" if margin is not None else "—"
            price_str = f"{v.price:,.2f}" if v.price is not None else "—"
            compare_str = f"{v.compare_at_price:,.2f}" if v.compare_at_price else "—"
            status_badge = (
                mark_safe('<span style="color:#059669;font-weight:600;">Active</span>')
                if v.is_active
                else mark_safe('<span style="color:#94a3b8;">Inactive</span>')
            )
            rows.append(
                format_html(
                    '<tr>'
                    '<td style="font-family:monospace;font-weight:600;">{}</td>'
                    '<td>{}</td>'
                    '<td>{}</td>'
                    '<td>{}</td>'
                    '<td>{}</td>'
                    '<td>{}</td>'
                    '<td><strong>{}</strong> avail ({} on hand)</td>'
                    '<td>{}</td>'
                    '</tr>',
                    v.sku,
                    v.color.name if v.color else "—",
                    v.size.code if v.size else "—",
                    price_str,
                    compare_str,
                    margin_str,
                    avail,
                    on_hand,
                    status_badge,
                )
            )

        return format_html(
            '<table class="variant-matrix-table">'
            '<thead><tr>'
            '<th>SKU</th><th>Colour</th><th>Size</th><th>Price</th>'
            '<th>Compare At</th><th>Margin</th><th>Inventory</th><th>Status</th>'
            '</tr></thead>'
            '<tbody>{}</tbody>'
            '</table>',
            mark_safe("".join(rows)),
        )

    @admin.display(description=_("Canonical URL"))
    def seo_canonical_preview(self, product: Product):
        if not product.pk:
            return "—"
        meta = seo.product_metadata(product)
        return meta.get("canonical_url", "")

    @admin.display(description=_("Google Search Preview"))
    def seo_serp_preview(self, product: Product):
        if not product.pk:
            return "—"
        meta = seo.product_metadata(product)
        return format_html(
            '<div class="seo-preview-box">'
            '<div class="seo-preview-title">{}</div>'
            '<div class="seo-preview-url">{}</div>'
            '<div class="seo-preview-desc">{}</div>'
            '</div>',
            meta.get("seo_title", ""),
            meta.get("canonical_url", ""),
            meta.get("seo_description", ""),
        )

    @admin.display(description=_("Social Share Preview (Open Graph)"))
    def seo_social_preview(self, product: Product):
        if not product.pk:
            return "—"
        meta = seo.product_metadata(product)
        img_url = meta.get("og_image", "")
        img_tag = (
            format_html(
                '<img src="{url}" style="width:100%;height:140px;object-fit:cover;'
                'border-radius:6px 6px 0 0;" alt="Social preview" />',
                url=img_url,
            )
            if img_url
            else mark_safe(
                '<div style="height:80px;background:#f1f5f9;display:flex;align-items:center;'
                'justify-content:center;color:#94a3b8;font-size:12px;">No social image</div>'
            )
        )
        return format_html(
            '<div style="max-width:400px;border:1px solid #cbd5e1;border-radius:6px;'
            'background:#ffffff;overflow:hidden;">'
            '{img_tag}'
            '<div style="padding:10px 12px;">'
            '<div style="font-weight:700;font-size:13px;color:#0f172a;">{title}</div>'
            '<div style="font-size:12px;color:#64748b;margin-top:4px;">{desc}</div>'
            '</div>'
            '</div>',
            img_tag=img_tag,
            title=meta.get("og_title", ""),
            desc=meta.get("og_description", ""),
        )

    # ----------------------------------------------------------------------------------
    # Custom URLs (Preview, CSV Export, CSV Import)
    # ----------------------------------------------------------------------------------

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/preview/",
                self.admin_site.admin_view(self.preview_view),
                name="catalog_product_preview",
            ),
            path(
                "export-csv/",
                self.admin_site.admin_view(self.export_csv_view),
                name="catalog_product_export_csv",
            ),
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv_view),
                name="catalog_product_import_csv",
            ),
        ]
        return custom_urls + urls

    def preview_view(self, request, object_id):
        """Staff-only preview rendering storefront template with draft status indicator."""
        product = get_object_or_404(selectors.product_detail_queryset(), pk=object_id)
        if not self.has_view_permission(request, product):
            raise PermissionDenied

        color_slug = request.GET.get("color", "")
        size_code = request.GET.get("size", "").upper()
        matrix = services.build_variant_matrix(
            product,
            color=Color.objects.filter(slug=color_slug, is_active=True).first()
            if color_slug
            else None,
            size=Size.objects.filter(code=size_code, is_active=True).first()
            if size_code
            else None,
        )
        selected = matrix.selected
        gallery = services.product_gallery(product)
        color_images = gallery["by_color"].get(selected.color_id, []) if selected else []
        gallery_images = [*color_images, *gallery["shared"]]

        breadcrumbs = [
            {"name": "Home", "url": request.build_absolute_uri("/")},
            {
                "name": product.category.name,
                "url": request.build_absolute_uri(product.category.get_absolute_url()),
            },
            {
                "name": product.name,
                "url": request.build_absolute_uri(product.get_absolute_url()),
            },
        ]

        context = {
            "product": product,
            "matrix": matrix,
            "selected_variant": selected,
            "selected_color": selected.color if selected else None,
            "selected_size": selected.size if selected else None,
            "gallery": gallery,
            "hero_image": gallery_images[0] if gallery_images else None,
            "gallery_images": gallery_images,
            "product_schema": seo.product_schema(product, request=request),
            "breadcrumb_schema": breadcrumb_schema(breadcrumbs),
            "breadcrumbs": breadcrumbs,
            "is_preview": True,
            **seo.product_metadata(product, request=request),
        }
        return render(request, "catalog/product_detail.html", context)

    def export_csv_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        csv_data = export_catalog_to_csv(Product.objects.all())
        response = HttpResponse(csv_data, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="flashwear_full_catalog.csv"'
        record_audit(
            actor=request.user,
            domain=AuditEvent.Domain.CATALOG,
            action="catalog.bulk",
            object_type="catalog.product",
            object_repr="Exported full catalog to CSV",
        )
        return response

    def import_csv_view(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied

        validation_result = None
        raw_csv_data = ""

        if request.method == "POST":
            if "validate_only" in request.POST and request.FILES.get("csv_file"):
                file = request.FILES["csv_file"]
                raw_csv_data = file.read().decode("utf-8-sig", errors="replace")
                validation_result = validate_catalog_csv(raw_csv_data)

            elif "commit_import" in request.POST and request.POST.get("raw_csv_data"):
                raw_csv_data = request.POST["raw_csv_data"]
                res = execute_catalog_import(raw_csv_data, user=request.user)
                if res.success:
                    self.message_user(
                        request,
                        _(
                            "CSV Import Successful: %(p_c)d products created, "
                            "%(p_u)d updated; %(v_c)d variants created, %(v_u)d updated."
                        )
                        % {
                            "p_c": res.products_created,
                            "p_u": res.products_updated,
                            "v_c": res.variants_created,
                            "v_u": res.variants_updated,
                        },
                        level=messages.SUCCESS,
                    )
                    return redirect("admin:catalog_product_changelist")
                else:
                    self.message_user(
                        request,
                        _("Import failed due to validation errors."),
                        level=messages.ERROR,
                    )
                    validation_result = validate_catalog_csv(raw_csv_data)

        context = {
            **self.admin_site.each_context(request),
            "title": _("Catalog CSV Import"),
            "validation_result": validation_result,
            "raw_csv_data": raw_csv_data,
        }
        return render(request, "admin/catalog/product/import_csv.html", context)

    # ----------------------------------------------------------------------------------
    # Audit Logging & Stock Adjustments on Saves
    # ----------------------------------------------------------------------------------

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        action_name = "catalog.update" if change else "catalog.create"
        record_audit(
            actor=request.user,
            domain=AuditEvent.Domain.CATALOG,
            action=action_name,
            object_type="catalog.product",
            object_id=obj.pk,
            object_repr=str(obj),
            metadata={"changed_fields": list(form.changed_data)} if change else {},
        )

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for instance in instances:
            instance.save()
        formset.save_m2m()

        if formset.model == ProductVariant:
            from apps.inventory.services import InsufficientStock, adjust_stock

            can_inventory = (
                request.user.is_superuser
                or request.user.has_perm("inventory.change_stock")
                or "Inventory Manager" in [g.name for g in request.user.groups.all()]
            )
            for variant_form in formset.forms:
                if (
                    variant_form.instance.pk
                    and variant_form.cleaned_data.get("stock_adjustment")
                ):
                    delta = variant_form.cleaned_data["stock_adjustment"]
                    if delta != 0:
                        if not can_inventory:
                            messages.error(
                                request,
                                _(
                                    "Permission denied: You do not have permission to "
                                    "adjust inventory stock for SKU %(sku)s."
                                )
                                % {"sku": variant_form.instance.sku},
                            )
                            continue
                        try:
                            adjust_stock(
                                variant_form.instance,
                                delta,
                                user=request.user,
                                note="Admin variant inline adjustment",
                                reference="admin_variant_edit",
                            )
                            messages.success(
                                request,
                                _("Adjusted stock by %(delta)+d units for SKU %(sku)s.")
                                % {"delta": delta, "sku": variant_form.instance.sku},
                            )
                        except InsufficientStock as exc:
                            messages.error(
                                request,
                                _("Stock adjustment failed for SKU %(sku)s: %(err)s")
                                % {"sku": variant_form.instance.sku, "err": str(exc)},
                            )
            record_audit(
                actor=request.user,
                domain=AuditEvent.Domain.CATALOG,
                action="catalog.variant",
                object_type="catalog.product",
                object_id=form.instance.pk,
                object_repr=str(form.instance),
            )

        elif formset.model == ProductImage:
            record_audit(
                actor=request.user,
                domain=AuditEvent.Domain.CATALOG,
                action="catalog.image",
                object_type="catalog.product",
                object_id=form.instance.pk,
                object_repr=str(form.instance),
            )

    # ----------------------------------------------------------------------------------
    # Bulk Admin Actions (Safe & Validated)
    # ----------------------------------------------------------------------------------

    @admin.action(description=_("Publish selected products"))
    def publish_selected(self, request, queryset):
        published = 0
        for product in queryset:
            try:
                services.publish(product)
                record_audit(
                    actor=request.user,
                    domain=AuditEvent.Domain.CATALOG,
                    action="catalog.publish",
                    object_type="catalog.product",
                    object_id=product.pk,
                    object_repr=str(product),
                )
                published += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"{product}: {'; '.join(exc.messages)}",
                    level=messages.ERROR,
                )
        if published:
            self.message_user(
                request,
                _("%(count)d product(s) published successfully.") % {"count": published},
                level=messages.SUCCESS,
            )

    @admin.action(description=_("Unpublish selected products (revert to Draft)"))
    def unpublish_selected(self, request, queryset):
        count = 0
        for product in queryset:
            services.unpublish(product)
            record_audit(
                actor=request.user,
                domain=AuditEvent.Domain.CATALOG,
                action="catalog.unpublish",
                object_type="catalog.product",
                object_id=product.pk,
                object_repr=str(product),
            )
            count += 1
        self.message_user(
            request,
            _("%(count)d product(s) reverted to Draft.") % {"count": count},
            level=messages.SUCCESS,
        )

    @admin.action(description=_("Activate selected products"))
    def activate_selected(self, request, queryset):
        return self.publish_selected(request, queryset)

    @admin.action(description=_("Deactivate selected products"))
    def deactivate_selected(self, request, queryset):
        return self.unpublish_selected(request, queryset)

    @admin.action(description=_("Archive selected products"))
    def archive_selected(self, request, queryset):
        archived = 0
        for product in queryset:
            services.archive(product)
            record_audit(
                actor=request.user,
                domain=AuditEvent.Domain.CATALOG,
                action="catalog.archive",
                object_type="catalog.product",
                object_id=product.pk,
                object_repr=str(product),
            )
            archived += 1
        self.message_user(
            request,
            _("%(count)d product(s) archived.") % {"count": archived},
            level=messages.SUCCESS,
        )

    @admin.action(description=_("Bulk assign category..."))
    def bulk_assign_category(self, request, queryset):
        if request.POST.get("apply"):
            form = BulkAssignCategoryForm(request.POST)
            if form.is_valid():
                category = form.cleaned_data["category"]
                updated = 0
                for product in queryset:
                    product.category = category
                    try:
                        product.full_clean()
                        product.save(update_fields=["category", "updated_at"])
                        updated += 1
                    except ValidationError as exc:
                        self.message_user(
                            request,
                            f"{product}: {'; '.join(exc.messages)}",
                            level=messages.ERROR,
                        )
                record_audit(
                    actor=request.user,
                    domain=AuditEvent.Domain.CATALOG,
                    action="catalog.bulk",
                    object_type="catalog.product",
                    object_repr=f"Assigned category '{category.name}' to {updated} products",
                    metadata={"category_id": category.pk, "updated_count": updated},
                )
                self.message_user(
                    request,
                    _("Assigned category '%(cat)s' to %(count)d product(s).")
                    % {"cat": category.name, "count": updated},
                    level=messages.SUCCESS,
                )
                return HttpResponseRedirect(request.get_full_path())
        else:
            form = BulkAssignCategoryForm()

        return render(
            request,
            "admin/catalog/product/bulk_action.html",
            {
                "title": _("Bulk Assign Category"),
                "description": _("Select the new category for all chosen products."),
                "form": form,
                "queryset": queryset,
                "action_name": "bulk_assign_category",
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            },
        )

    @admin.action(description=_("Bulk assign brand..."))
    def bulk_assign_brand(self, request, queryset):
        if request.POST.get("apply"):
            form = BulkAssignBrandForm(request.POST)
            if form.is_valid():
                brand = form.cleaned_data["brand"]
                updated = 0
                for product in queryset:
                    product.brand = brand
                    try:
                        product.full_clean()
                        product.save(update_fields=["brand", "updated_at"])
                        updated += 1
                    except ValidationError as exc:
                        self.message_user(
                            request,
                            f"{product}: {'; '.join(exc.messages)}",
                            level=messages.ERROR,
                        )
                brand_label = brand.name if brand else "None (In-house)"
                record_audit(
                    actor=request.user,
                    domain=AuditEvent.Domain.CATALOG,
                    action="catalog.bulk",
                    object_type="catalog.product",
                    object_repr=f"Assigned brand '{brand_label}' to {updated} products",
                    metadata={
                        "brand_id": brand.pk if brand else None,
                        "updated_count": updated,
                    },
                )
                self.message_user(
                    request,
                    _("Assigned brand '%(brand)s' to %(count)d product(s).")
                    % {"brand": brand_label, "count": updated},
                    level=messages.SUCCESS,
                )
                return HttpResponseRedirect(request.get_full_path())
        else:
            form = BulkAssignBrandForm()

        return render(
            request,
            "admin/catalog/product/bulk_action.html",
            {
                "title": _("Bulk Assign Brand"),
                "description": _(
                    "Select the brand to assign to chosen products (or leave blank to remove)."
                ),
                "form": form,
                "queryset": queryset,
                "action_name": "bulk_assign_brand",
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            },
        )

    @admin.action(description=_("Bulk add/remove collection..."))
    def bulk_assign_collection(self, request, queryset):
        if request.POST.get("apply"):
            form = BulkAssignCollectionForm(request.POST)
            if form.is_valid():
                coll = form.cleaned_data["collection"]
                action_type = form.cleaned_data["action_type"]
                for product in queryset:
                    if action_type == "add":
                        product.collections.add(coll)
                    else:
                        product.collections.remove(coll)
                record_audit(
                    actor=request.user,
                    domain=AuditEvent.Domain.CATALOG,
                    action="catalog.bulk",
                    object_type="catalog.product",
                    object_repr=(
                        f"Bulk {action_type} '{coll.name}' on {queryset.count()} products"
                    ),
                    metadata={"collection_id": coll.pk, "action": action_type},
                )
                self.message_user(
                    request,
                    _("Updated collection '%(coll)s' on %(count)d product(s).")
                    % {"coll": coll.name, "count": queryset.count()},
                    level=messages.SUCCESS,
                )
                return HttpResponseRedirect(request.get_full_path())
        else:
            form = BulkAssignCollectionForm()

        return render(
            request,
            "admin/catalog/product/bulk_action.html",
            {
                "title": _("Bulk Manage Collection"),
                "description": _("Add or remove selected products to/from a collection."),
                "form": form,
                "queryset": queryset,
                "action_name": "bulk_assign_collection",
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            },
        )

    @admin.action(description=_("Bulk add tags..."))
    def bulk_update_tags(self, request, queryset):
        if request.POST.get("apply"):
            form = BulkUpdateTagsForm(request.POST)
            if form.is_valid():
                tag_names = [t.strip() for t in form.cleaned_data["tags"].split(",") if t.strip()]
                tag_objects = []
                for name in tag_names:
                    tag_obj, _tag_created = ProductTag.objects.get_or_create(
                        name=name, defaults={"slug": slugify(name)}
                    )
                    tag_objects.append(tag_obj)
                for product in queryset:
                    product.tags.add(*tag_objects)
                record_audit(
                    actor=request.user,
                    domain=AuditEvent.Domain.CATALOG,
                    action="catalog.bulk",
                    object_type="catalog.product",
                    object_repr=f"Bulk added tags {tag_names} to {queryset.count()} products",
                )
                self.message_user(
                    request,
                    _("Added tags to %(count)d product(s).") % {"count": queryset.count()},
                    level=messages.SUCCESS,
                )
                return HttpResponseRedirect(request.get_full_path())
        else:
            form = BulkUpdateTagsForm()

        return render(
            request,
            "admin/catalog/product/bulk_action.html",
            {
                "title": _("Bulk Add Tags"),
                "description": _("Specify tags to append to all chosen products."),
                "form": form,
                "queryset": queryset,
                "action_name": "bulk_update_tags",
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
            },
        )

    @admin.action(description=_("Derive missing SEO title and description"))
    def derive_seo_metadata(self, request, queryset):
        updated = 0
        for product in queryset:
            changed = False
            if not product.seo_title:
                product.seo_title = product.name[:70]
                changed = True
            if not product.seo_description:
                desc = product.short_description or product.description
                if desc:
                    product.seo_description = desc[:160]
                    changed = True
            if changed:
                product.save(update_fields=["seo_title", "seo_description", "updated_at"])
                updated += 1
        record_audit(
            actor=request.user,
            domain=AuditEvent.Domain.CATALOG,
            action="catalog.bulk",
            object_type="catalog.product",
            object_repr=f"Derived SEO copy for {updated} products",
        )
        self.message_user(
            request,
            _("Derived SEO metadata for %(count)d product(s).") % {"count": updated},
            level=messages.SUCCESS,
        )

    @admin.action(description=_("Export selected products to CSV"))
    def export_selected_to_csv(self, request, queryset):
        csv_data = export_catalog_to_csv(queryset)
        response = HttpResponse(csv_data, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="flashwear_catalog_export.csv"'
        record_audit(
            actor=request.user,
            domain=AuditEvent.Domain.CATALOG,
            action="catalog.bulk",
            object_type="catalog.product",
            object_repr=f"Exported {queryset.count()} selected products to CSV",
        )
        return response

    actions = (
        "publish_selected",
        "unpublish_selected",
        "activate_selected",
        "deactivate_selected",
        "archive_selected",
        "bulk_assign_category",
        "bulk_assign_brand",
        "bulk_assign_collection",
        "bulk_update_tags",
        "derive_seo_metadata",
        "export_selected_to_csv",
    )

    # ----------------------------------------------------------------------------------
    # Permissions
    # ----------------------------------------------------------------------------------

    def has_view_permission(self, request, obj=None):
        if not request.user.is_authenticated or not request.user.is_staff:
            return False
        return (
            request.user.is_superuser
            or request.user.has_perm("catalog.view_product")
            or "Catalog Manager" in [g.name for g in request.user.groups.all()]
            or "Back Office Operator" in [g.name for g in request.user.groups.all()]
        )

    def has_change_permission(self, request, obj=None):
        if not request.user.is_authenticated or not request.user.is_staff:
            return False
        return (
            request.user.is_superuser
            or request.user.has_perm("catalog.change_product")
            or "Catalog Manager" in [g.name for g in request.user.groups.all()]
        )

    def has_add_permission(self, request):
        if not request.user.is_authenticated or not request.user.is_staff:
            return False
        return (
            request.user.is_superuser
            or request.user.has_perm("catalog.add_product")
            or "Catalog Manager" in [g.name for g in request.user.groups.all()]
        )

    def has_delete_permission(self, request, obj=None):
        if not request.user.is_authenticated or not request.user.is_staff:
            return False
        return (
            request.user.is_superuser
            or request.user.has_perm("catalog.delete_product")
        )


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
