"""Admin for FLASH Quests & Rewards (Phase 14).

Staff author quests and badges; staff **observe** participation. The participation and
badge-holder tables are deliberately view-only (no add, no change): awarding a completion
by hand would be the exact "self-award from outside the engine" path the rest of the phase
closes off, and the FLASH Points ledger already has its own operator adjustment tool for
anything a manager actually needs to grant.
"""

from __future__ import annotations

from django.contrib import admin

from apps.quests.models import Badge, Quest, UserBadge, UserQuest


@admin.register(Badge)
class BadgeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "holders_count")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    @admin.display(description="Holders")
    def holders_count(self, obj: Badge) -> int:
        return obj.holders.count()


@admin.register(Quest)
class QuestAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "quest_type",
        "objective_type",
        "target",
        "repeat_period",
        "publish_state",
        "state_now",
        "points_reward",
        "start_at",
        "end_at",
        "sort_order",
    )
    list_filter = ("publish_state", "quest_type", "objective_type", "repeat_period")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="State now")
    def state_now(self, obj: Quest) -> str:
        """The derived lifecycle -- admins configure windows, never stored state."""
        return Quest.State(obj.state_at()).label


@admin.register(UserQuest)
class UserQuestAdmin(admin.ModelAdmin):
    """View-only participation rows: progress is the engine's to write, not ours."""

    list_display = ("user", "quest", "status", "progress", "period_key", "completed_at")
    list_filter = ("status", "quest")
    search_fields = ("user__email", "quest__slug")
    readonly_fields = (
        "user",
        "quest",
        "period_key",
        "status",
        "progress",
        "started_at",
        "completed_at",
        "reward_reference",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(UserBadge)
class UserBadgeAdmin(admin.ModelAdmin):
    """View-only badge holdings, for the same reason as participation rows."""

    list_display = ("user", "badge", "source", "created_at")
    search_fields = ("user__email", "badge__slug")
    readonly_fields = ("user", "badge", "source", "created_at", "updated_at")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
