"""Admin registration for recommendations signals and scores (Phase 10)."""

from django.contrib import admin

from apps.recommendations.models import RecommendationScore, RecommendationSignal


@admin.register(RecommendationSignal)
class RecommendationSignalAdmin(admin.ModelAdmin):
    """Admin configuration for user recommendation behavioral signals."""

    list_display = ("user", "signal_type", "strength", "signed_at")
    list_filter = ("signal_type", "signed_at")
    search_fields = ("user__email",)
    readonly_fields = ("signed_at",)


@admin.register(RecommendationScore)
class RecommendationScoreAdmin(admin.ModelAdmin):
    """Admin configuration for computed recommendation scores."""

    list_display = ("user", "product", "total_final", "calculated_at")
    list_filter = ("calculated_at",)
    search_fields = ("user__email", "product__name", "product__slug")
    readonly_fields = ("calculated_at",)
