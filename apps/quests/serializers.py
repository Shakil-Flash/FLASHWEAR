"""DRF serializers for the quests API (Phase 14).

Every field is declared ``read_only`` (or the payload is empty): the API is a *read* of
server-derived progress plus one participation action, and no serializer here accepts a
progress number, a completion flag or a reward. :class:`QuestProgressSerializer` reads a
:class:`apps.quests.services.progress.QuestProgress` -- the same dataclass the HTML views
render, so both surfaces can only ever show the identical derivation.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.quests.models import Badge


class BadgeSerializer(serializers.ModelSerializer):
    """A badge definition (never the holder row -- ownership is implied by the response)."""

    class Meta:
        model = Badge
        fields = ("slug", "name", "description")
        read_only_fields = fields


class QuestProgressSerializer(serializers.Serializer):
    """One quest plus *this* user's progress on it. All reads; nothing is writable."""

    slug = serializers.CharField(source="quest.slug")
    name = serializers.CharField(source="quest.name")
    description = serializers.CharField(source="quest.description", allow_blank=True)
    quest_type = serializers.CharField(source="quest.quest_type")
    objective_type = serializers.CharField(source="quest.objective_type")
    repeat_period = serializers.CharField(source="quest.repeat_period")
    state = serializers.CharField()
    target = serializers.IntegerField()
    progress = serializers.IntegerField()
    percent = serializers.IntegerField()
    status = serializers.CharField(allow_blank=True)
    period_key = serializers.CharField(allow_blank=True)
    points_reward = serializers.IntegerField(source="quest.points_reward")
    badge = BadgeSerializer(source="quest.badge", allow_null=True, required=False)
    started = serializers.BooleanField(source="is_started")
    completed = serializers.BooleanField(source="is_completed")
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    completed_at = serializers.DateTimeField(required=False, allow_null=True)


class EmptySerializer(serializers.Serializer):
    """A POST with no writable fields -- the start action takes no client input at all."""

    def to_internal_value(self, data):
        return {}
