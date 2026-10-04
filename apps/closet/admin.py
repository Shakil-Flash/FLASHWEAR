"""Admin for the Phase 8 closet and outfit models."""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.closet.models import ClosetItem, Outfit, OutfitItem


class OutfitItemInline(admin.TabularInline):
    """One garment placed in one outfit, in a role and a position."""

    model = OutfitItem
    extra = 0
    fields = ("closet_item", "role", "position", "note")
    autocomplete_fields = ("closet_item",)
    raw_id_fields = ("outfit",)
    show_change_link = True
    verbose_name = _("outfit item")
    verbose_name_plural = _("outfit items")


@admin.register(ClosetItem)
class ClosetItemAdmin(admin.ModelAdmin):
    """Admin for a wardrobe piece -- reachable only by staff."""

    list_display = (
        "name",
        "category",
        "source",
        "status",
        "created_at",
    )
    list_filter = ("category", "source", "status")
    search_fields = ("name", "brand", "color")
    date_hierarchy = "created_at"
    list_select_related = ("user",)
    readonly_fields = ("source", "order_item", "unit", "variant", "created_at", "updated_at")
    raw_id_fields = ("user",)


@admin.register(Outfit)
class OutfitAdmin(admin.ModelAdmin):
    """Admin for an outfit -- its rows are edited inline."""

    list_display = (
        "name",
        "user",
        "status",
        "created_at",
        "updated_at",
    )
    list_filter = ("status",)
    search_fields = ("name",)
    date_hierarchy = "created_at"
    list_select_related = ("user",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (OutfitItemInline,)
