"""Styling API (Phase 9, extended in Phase 21).

Session-authenticated, customer-scoped.  Every endpoint scopes through
``request.user`` so one customer can never read or write another's data; the
Phase 21 studio endpoints reuse the exact services behind the Style Studio pages
and record the same analytics events.

Namespace: ``v1`` (registered in ``config/api/v1/urls.py``).
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.services import record_event
from apps.catalog.models import Product
from apps.closet.models import OutfitItem
from apps.closet.services.errors import OutfitError
from apps.styling.models.flash_dna import STYLE_GOALS, FlashDNA, get_flash_dna
from apps.styling.services import (
    recommend_outfit,
    save_recommended_outfit,
    style_product,
)
from apps.styling.services.complete_look import complete_look, complete_look_to_dict
from apps.styling.services.errors import (
    StylistError,
)
from apps.styling.services.outfit_generation import (
    generate_outfit,
    save_suggestion,
    suggestion_to_dict,
)
from apps.styling.views import (
    _context_query,
    _emit_generation,
    _studio_context,
)

logger = logging.getLogger(__name__)

User = get_user_model()


class FlashDNAView(APIView):
    """GET/PUT ``/api/v1/flash-dna/`` — profile CRUD."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def get(self, request) -> Response:
        try:
            profile = request.user.flash_dna
        except ObjectDoesNotExist:
            return Response(
                {"detail": "FLASH DNA profile not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        # Return a sanitized payload — no internal db IDs exposed unless relevant.
        return Response(
            {
                "styles": profile.styles,
                "favorite_colors": [c.hex_code for c in profile.favorite_colors.all()],
                "disliked_colors": [c.hex_code for c in profile.disliked_colors.all()],
                "preferred_categories": [c.name for c in profile.preferred_categories.all()],
                "preferred_fits": [f.name for f in profile.preferred_fits],
                "preferred_materials": [m.name for m in profile.preferred_materials.all()],
                "preferred_occasions": [o.name for o in profile.preferred_occasions.all()],
                "preferred_seasons": [s.name for s in profile.preferred_seasons.all()],
                "preferred_price_range": profile.preferred_price_range,
                "preferred_brands": [b.name for b in profile.preferred_brands.all()],
                "confidence_level": profile.confidence_level,
                "fashion_goal": profile.fashion_goal or "",
                "is_complete": profile.is_complete,
            }
        )

    def put(self, request) -> Response:
        """Update profile from a flat payload."""
        try:
            profile = request.user.flash_dna
        except ObjectDoesNotExist:
            from apps.styling.services.dna import create_dna

            profile = create_dna(request.user, **self._payload(request))

        for key, value in self._payload(request).items():
            if hasattr(profile, key):
                setattr(profile, key, value)
        try:
            profile.full_clean()
        except Exception as exc:
            from apps.styling.services.errors import EmptyProfileError

            if isinstance(exc, EmptyProfileError):
                return Response(
                    {"detail": exc.message},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        profile.save()
        return Response({"detail": "FLASH DNA updated."})

    @staticmethod
    def _payload(request) -> dict:
        """Extract the minimal fields the profile accepts from the PUT body."""
        data = request.data or {}
        allowed = {
            "styles",
            "favorite_colors",
            "disliked_colors",
            "preferred_categories",
            "preferred_fits",
            "preferred_materials",
            "preferred_occasions",
            "preferred_seasons",
            "preferred_price_range",
            "preferred_brands",
            "confidence_level",
            "fashion_goal",
        }
        return {k: v for k, v in data.items() if k in allowed}


class StylistRecommendView(APIView):
    """POST ``/api/v1/stylist/recommend/`` — get outfit recommendations."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def post(self, request, *args, **kwargs) -> Response:
        try:
            result = recommend_outfit(request.user, request.path, request.data.get("provider"))
        except StylistError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:  # broad; safety net
            logger.exception("Stylist unexpected error")
            return Response(
                {"detail": "The stylist service is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)


class StylistStyleProductView(APIView):
    """POST ``/api/v1/stylist/style-product/`` — style a specific product."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def post(self, request, *args, **kwargs) -> Response:
        product_pk = request.data.get("product_pk")
        if not product_pk:
            return Response(
                {"detail": "product_pk is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = style_product(request.user, int(product_pk))
        except StylistError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Stylist style-product error")
            return Response(
                {"detail": "The stylist service is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)


class StylistSaveOutfitView(APIView):
    """POST ``/api/v1/stylist/recommendations/<id>/save/`` — save an outfit."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def post(self, request, *args, **kwargs) -> Response:
        try:
            outfit = save_recommended_outfit(request.user, request.data)
        except StylistError as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Stylist save error")
            return Response(
                {"detail": "Could not save the outfit right now."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(
            {
                "detail": "Outfit saved.",
                "outfit_pk": outfit.pk,
                "outfit_name": outfit.name,
            }
        )


class FlashDNACheckView(APIView):
    """GET ``/api/v1/flash-dna/check/`` — quick check for profile existence."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def get(self, request) -> Response:
        has_it = request.user.flash_dna is not None
        return Response({"has_profile": has_it})


# ---------------------------------------------------------------------------
# Phase 21: Style Studio API — the same deterministic generator the pages use,
# so an app client and the htmx pages count as one analytics stream.
# ---------------------------------------------------------------------------


class StudioGenerateView(APIView):
    """POST ``/api/v1/studio/outfit/`` — build, swap or mood-generate one look.

    The JSON body takes the same validated parameters the studio page does (``occasion``,
    ``season``, ``mood``, ``budget``, ``weather_temp``, ``weather_cond``, ``seed``) plus
    ``action``: ``generate`` (default), ``replace`` (needs ``role`` and ``piece``) or
    ``mood`` (requires ``mood``). Every visible look emits ``outfit_generated``; a swap
    emits ``outfit_item_replaced``; a mood adds ``mood_outfit_generated`` — identical to
    what the web pages record.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def post(self, request, *args, **kwargs) -> Response:
        data = request.data or {}
        action = str(data.get("action") or "generate")
        if action not in {"generate", "replace", "mood"}:
            return Response(
                {
                    "detail": "action must be one of generate, replace, mood.",
                    "code": "invalid_action",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        context = _studio_context(data)
        if action == "mood" and not context.mood:
            return Response(
                {"detail": "mood is required for action=mood.", "code": "mood_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if action == "replace":
            return self._replace(request, data, context)

        suggestion = generate_outfit(request.user, context)
        _emit_generation(request, suggestion, mood_page=action == "mood")
        return Response({**suggestion_to_dict(suggestion), "replay": dict(_context_query(context))})

    @staticmethod
    def _replace(request, data, context) -> Response:
        """Re-roll one role while everything else stays pinned (and old piece retired)."""
        role = str(data.get("role") or "")
        previous = str(data.get("piece") or "")
        if role not in set(OutfitItem.Role.values) or not previous.startswith(("c", "p")):
            return Response(
                {
                    "detail": "role and piece are required to replace a piece.",
                    "code": "invalid_piece",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        context.pins.pop(role, None)
        context.skips = frozenset(set(context.skips) | {previous})
        suggestion = generate_outfit(request.user, context)
        replacement = next((piece for piece in suggestion.pieces if piece.role == role), None)
        record_event(
            "outfit_item_replaced",
            request=request,
            object_type="outfit",
            metadata={
                "role": role,
                "previous": previous,
                "replacement": replacement.token if replacement else "",
            },
        )
        return Response({**suggestion_to_dict(suggestion), "replay": dict(_context_query(context))})


class StudioSaveView(APIView):
    """POST ``/api/v1/studio/outfit/save/`` — persist a generated look into the wardrobe.

    Same rules as the page's Save button: a catalog-only look cannot be saved into the
    closet, and the domain refusal comes back as ``400`` with the stable ``code`` instead
    of a stack trace.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def post(self, request, *args, **kwargs) -> Response:
        data = request.data or {}
        context = _studio_context(data)
        name = str(data.get("name") or "").strip()
        suggestion = generate_outfit(request.user, context)
        try:
            outfit = save_suggestion(request.user, suggestion, name=name)
        except OutfitError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )
        record_event(
            "outfit_saved",
            request=request,
            object_type="outfit",
            object_id=outfit.pk,
            metadata={
                "occasion": suggestion.context.occasion,
                "shape": suggestion.shape,
                "pieces": len(suggestion.pieces),
            },
        )
        return Response(
            {
                "detail": "Outfit saved.",
                "outfit_pk": outfit.pk,
                "outfit_name": outfit.name,
            }
        )


class StudioGoalView(APIView):
    """GET/POST ``/api/v1/studio/goal/`` — read or set the standing style goal."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "expensive"

    def get(self, request, *args, **kwargs) -> Response:
        dna = get_flash_dna(request.user)
        return Response(
            {
                "goal": dna.style_goal if dna else "",
                "choices": [{"value": value, "label": label} for value, label in STYLE_GOALS],
            }
        )

    def post(self, request, *args, **kwargs) -> Response:
        goal = str((request.data or {}).get("goal") or "")
        if goal not in {value for value, _label in STYLE_GOALS}:
            return Response(
                {"detail": "goal must be one of the style goals.", "code": "invalid_goal"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # FlashDNA.save() full-cleans and refuses an empty profile, so the row is created
        # with the goal in one step instead of an empty shell that could never be saved.
        dna, created = FlashDNA.objects.get_or_create(
            user=request.user, defaults={"style_goal": goal}
        )
        previous = "" if created else dna.style_goal
        if not created and previous != goal:
            dna.style_goal = goal
            dna.save()
        record_event(
            "style_goal_selected",
            request=request,
            object_type="style_goal",
            metadata={"goal": goal, "previous": previous},
        )
        return Response({"goal": goal, "label": str(dict(STYLE_GOALS).get(goal, goal))})


class StudioCompleteLookView(APIView):
    """GET ``/api/v1/studio/complete-look/<slug>/`` — the PDP widget as JSON.

    Public, like the widget it mirrors: a shopper who has not signed in still sees the
    complementary pieces, and "nothing fits" is an empty list with a friendly warning,
    never an error status. Unscoped throttle like the storefront pages around it.
    """

    permission_classes = [AllowAny]

    def get(self, request, slug: str, *args, **kwargs) -> Response:
        product = get_object_or_404(Product.objects.published(), slug=slug)
        look = complete_look(product, user=request.user)
        record_event(
            "complete_look_viewed",
            request=request,
            object_type="product",
            object_id=product.pk,
            metadata={"category": look.source_category, "items": len(look.items)},
        )
        return Response(complete_look_to_dict(look))
