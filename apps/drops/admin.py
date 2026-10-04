"""Admin configuration for FLASH Drops (Phase 11)."""

from django.contrib import admin

from .models import (
    DropAllocation,
    DropProduct,
    FlashDrop,
)


class DropProductInline(admin.TabularInline):
    """Inline for managing DropProduct within the FlashDrop admin."""

    model = DropProduct
    extra = 1
    fields = ("product", "variant", "order_position", "is_featured", "drop_label")
    autocomplete_fields = ("product", "variant")


@admin.register(FlashDrop)
class FlashDropAdmin(admin.ModelAdmin):
    """Admin for FlashDrop."""

    list_display = (
        "name",
        "slug",
        "status",
        "starts_at",
        "ends_at",
        "published_at",
        "created_at",
    )
    list_filter = ("status", "starts_at", "ends_at")
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    date_hierarchy = "starts_at"
    inlines = [DropProductInline]
    readonly_fields = ("published_at", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("name", "slug", "description")}),
        (
            "Timing",
            {
                "fields": (
                    "starts_at",
                    "ends_at",
                    "published_at",
                    "status",
                ),
            },
        ),
        (
            "Assets",
            {
                "fields": ("hero_image",),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).with_storefront_data()


@admin.register(DropProduct)
class DropProductAdmin(admin.ModelAdmin):
    """Admin for DropProduct."""

    list_display = ("drop", "product", "variant", "order_position", "is_featured")
    list_filter = ("drop", "is_featured")
    search_fields = ("drop__name", "product__name", "variant__sku")
    autocomplete_fields = ("drop", "product", "variant")
    raw_id_fields = ("drop", "product", "variant")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("drop", "product", "variant")


@admin.register(DropAllocation)
class DropAllocationAdmin(admin.ModelAdmin):
    """Admin for DropAllocation."""

    list_display = ("drop", "claimed", "total_allocated", "remaining_display")
    list_filter = ("drop",)
    search_fields = ("drop__name",)
    autocomplete_fields = ("drop",)
    raw_id_fields = ("drop",)

    def remaining_display(self, obj) -> str:
        if obj.total_allocated:
            return f"{obj.remaining} / {obj.total_allocated}"
        return "unlimited"

    remaining_display.short_description = "Remaining"


# Register the through model for many-to-many if needed
# (FlashDrop.products is via DropProduct, so no extra registration needed)
