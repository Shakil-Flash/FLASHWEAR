"""Admin registration for FlashDNA preferences (Phase 9 & 21)."""

from django.contrib import admin

from apps.styling.models.flash_dna import FlashDNA


@admin.register(FlashDNA)
class FlashDNAAdmin(admin.ModelAdmin):
    """Admin configuration for customer fashion DNA preferences."""

    list_display = ("user", "style_goal", "created_at", "updated_at")
    list_filter = ("style_goal", "created_at", "updated_at")
    search_fields = ("user__email", "user__first_name", "user__last_name")
    readonly_fields = ("created_at", "updated_at")
