# Admin: core app

from django.contrib import admin

from apps.core.models import (
    Campaign,
    EditorialStory,
    HomepageSection,
    SectionProduct,
    SiteConfiguration,
)


@admin.register(SiteConfiguration)
class SiteConfigurationAdmin(admin.ModelAdmin):
    """Admin interface for the singleton site configuration."""

    list_display = (
        "site_name",
        "tagline",
        "support_email",
        "maintenance_mode",
        "updated_at",
    )
    fields = (
        "site_name",
        "tagline",
        "announcement",
        "maintenance_mode",
        "default_seo_title",
        "default_seo_description",
        "support_email",
    )
    readonly_fields = ("updated_at",)

    def has_delete_permission(self, request, obj=None) -> bool:
        """Prevent deleting the singleton (it must always exist)."""
        return False

    def has_add_permission(self, request) -> bool:
        """Only allow adding the configuration once."""
        return not SiteConfiguration.objects.exists()


class SectionProductInline(admin.TabularInline):
    model = SectionProduct
    extra = 1
    autocomplete_fields = ["product"]


@admin.register(HomepageSection)
class HomepageSectionAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "section_type",
        "display_order",
        "is_enabled",
        "campaign",
        "starts_at",
        "ends_at",
    )
    list_filter = ("section_type", "is_enabled", "campaign")
    search_fields = ("title", "subtitle")
    inlines = [SectionProductInline]


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "starts_at", "ends_at", "is_active")
    list_filter = ("is_active",)
    search_fields = ("title", "description")
    prepopulated_fields = {"slug": ("title",)}


@admin.register(EditorialStory)
class EditorialStoryAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "slug",
        "display_order",
        "is_published",
        "campaign",
        "starts_at",
        "ends_at",
    )
    list_filter = ("is_published", "campaign")
    search_fields = ("title", "subtitle", "story")
    prepopulated_fields = {"slug": ("title",)}
    filter_horizontal = ("linked_products",)
