# Admin: core app

from django.contrib import admin

from apps.core.models import SiteConfiguration


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
