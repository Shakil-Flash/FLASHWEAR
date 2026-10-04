"""Session-authenticated quests API (Phase 14).

Reads of server-derived progress plus one participation action. The contract that matters
is what is *absent*: no endpoint completes a quest, awards points or mints a badge, and no
payload field can set ``progress``. Completion belongs to the progress engine alone, which
is why :class:`QuestStartAPIView` accepts an empty body -- there is nothing a client could
usefully say about joining.

All rows are scoped to ``request.user``: there is no ``user`` parameter to forge and no
path that reads another customer's participation (IDOR is structurally impossible rather
than filtered per-view).
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.engagement.services import loyalty
from apps.quests import selectors
from apps.quests.models import UserQuest
from apps.quests.serializers import BadgeSerializer, QuestProgressSerializer
from apps.quests.services import progress as progress_service
from apps.quests.services.badges import sync_achievements
from apps.quests.services.errors import QuestError


class QuestListAPIView(APIView):
    """``GET /api/v1/quests/`` -- every published quest with the caller's progress."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        views = [
            progress_service.progress_for(request.user, quest)
            for quest in selectors.published_quests()
        ]
        serializer = QuestProgressSerializer(views, many=True)
        return Response({"count": len(serializer.data), "results": serializer.data})


class QuestDetailAPIView(APIView):
    """``GET /api/v1/quests/<slug>/`` -- one quest, one caller's progress (404 if unpublished)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, slug: str):
        quest = get_object_or_404(selectors.published_quests(), slug=slug)
        view = progress_service.progress_for(request.user, quest)
        return Response(QuestProgressSerializer(view).data)


class QuestStartAPIView(APIView):
    """``POST /api/v1/quests/<slug>/start/`` -- join a quest. Empty body, no progress input.

    Errors map the domain ``code`` straight through (``400`` for an ineligible quest), the
    same vocabulary the HTML views show as a message.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, slug: str):
        quest = get_object_or_404(selectors.published_quests(), slug=slug)
        try:
            row = progress_service.start_quest(request.user, quest)
        except QuestError as exc:
            return Response({"code": exc.code, "detail": exc.message}, status=400)
        view = progress_service.progress_for(request.user, quest)
        return Response(
            {"status": row.status, "progress": QuestProgressSerializer(view).data},
        )


class RewardsAPIView(APIView):
    """``GET /api/v1/rewards/`` -- the caller's badges, points and quest tallies.

    Achievement rules are re-checked on the way in (lazy, like the HTML dashboard), so an
    order placed without ever hitting a quest endpoint still surfaces its badge here.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        sync_achievements(request.user)
        rows = selectors.user_quest_rows(request.user)
        return Response(
            {
                "points_balance": loyalty.balance_for(request.user),
                "points_held": loyalty.held_for(request.user),
                "badges": BadgeSerializer(
                    [holder.badge for holder in selectors.user_badges(request.user)], many=True
                ).data,
                "completed_quests": rows.filter(status=UserQuest.Status.COMPLETED).count(),
                "active_quests": rows.filter(status=UserQuest.Status.ACTIVE).count(),
            },
        )
