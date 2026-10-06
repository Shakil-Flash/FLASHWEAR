"""Smart styling studio views (Phase 21, spec Sections 3-6).

Three signed-in pages behind ``/account/studio/`` -- the occasion-driven generator, the
mood picker and the style-goal picker -- plus two public, read-only pieces on the
storefront: the lazy "Complete the Look" fragment the product page fetches with htmx, and
the click tracker that proves a recommendation led somewhere.

Everything follows the house rules: ``GET`` renders (so a look is shareable and
browser-back-safe), state changes ``POST`` then redirect, every parameter is validated
against a controlled vocabulary before it reaches a service, and analytics go through
:func:`apps.analytics.services.record_event` only -- never a second event system.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from apps.analytics.services import record_event
from apps.catalog.models import Product
from apps.closet.models import ClosetItem, OutfitItem
from apps.closet.services.errors import OutfitError
from apps.styling.models.flash_dna import STYLE_GOALS, FlashDNA, get_flash_dna
from apps.styling.services import signals as maps
from apps.styling.services.complete_look import complete_look
from apps.styling.services.outfit_generation import (
    OutfitContext,
    generate_outfit,
    parse_keep,
    parse_skip,
    save_suggestion,
    tokens_for_saving,
)
from apps.styling.services.signals import WeatherContext

__all__ = [
    "complete_look_fragment",
    "recommendation_click",
    "studio",
    "studio_goals",
    "studio_mood",
]

#: Untrusted weather input is coerced into this window before it can reach scoring.
WEATHER_TEMP_MIN = -60.0
WEATHER_TEMP_MAX = 60.0

#: Seeds are small, human-friendly integers; the modulus keeps URLs short and band-safe.
SEED_MODULUS = 10_000


# ---------------------------------------------------------------------------
# Shared parameter handling (GET query strings and POST bodies parse identically)
# ---------------------------------------------------------------------------


def _float_param(raw, low: float, high: float) -> float | None:
    """Parse a bounded float; anything unparsable or out of range reads as "unset"."""
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not low <= value <= high:
        return None
    return value


def _int_param(raw) -> int:
    """Parse a non-negative seed; a typo degrades to 0 rather than an error page."""
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, value) % SEED_MODULUS


def _studio_context(data) -> OutfitContext:
    """Build one validated generation context from a QueryDict or JSON body.

    Every value is checked against its controlled vocabulary before it reaches a service,
    so a hand-edited URL can change the look but never break it -- and an invalid season
    can never reach ``Outfit.full_clean()`` through a later save.
    """
    occasion = data.get("occasion") or "everyday"
    if occasion not in dict(maps.OCCASION_CHOICES):
        occasion = "everyday"

    season = data.get("season") or ""
    if season not in ClosetItem.Season.values:
        season = ""

    mood = data.get("mood") or ""
    if mood not in dict(maps.MOOD_CHOICES):
        mood = ""

    condition = data.get("weather_cond") or ""
    if condition not in [value for value, _label in maps.WEATHER_CONDITIONS]:
        condition = ""

    weather = WeatherContext(
        temperature_c=_float_param(data.get("weather_temp"), WEATHER_TEMP_MIN, WEATHER_TEMP_MAX),
        condition=condition,
    )
    return OutfitContext(
        occasion=occasion,
        season=season,
        mood=mood,
        budget=maps.parse_budget(data.get("budget")),
        weather=weather,
        seed=_int_param(data.get("seed")),
        pins=parse_keep(data.get("keep") or ""),
        skips=parse_skip(data.get("skip") or ""),
    )


def _context_query(context: OutfitContext) -> dict[str, str]:
    """The URL that replays *exactly* this look (defaults omitted to keep URLs short)."""
    query: dict[str, str] = {}
    if context.occasion and context.occasion != "everyday":
        query["occasion"] = context.occasion
    if context.season:
        query["season"] = context.season
    if context.mood:
        query["mood"] = context.mood
    if context.budget is not None:
        query["budget"] = str(context.budget)
    if context.weather.temperature_c is not None:
        query["weather_temp"] = f"{context.weather.temperature_c:g}"
    if context.weather.condition:
        query["weather_cond"] = context.weather.condition
    if context.seed:
        query["seed"] = str(context.seed)
    if context.pins:
        query["keep"] = ",".join(f"{role}:{token}" for role, token in sorted(context.pins.items()))
    if context.skips:
        query["skip"] = ",".join(sorted(context.skips))
    return query


def _redirect_with(url_name: str, query: dict[str, str]) -> HttpResponseRedirect:
    """PRG redirect to a studio route, carrying the replay query (or not at all)."""
    url = reverse(url_name)
    if query:
        url = f"{url}?{urlencode(query)}"
    return redirect(url)


def _emit_generation(request, suggestion, *, mood_page: bool = False) -> None:
    """One analytics row per customer-visible look (plus the mood dimension when asked)."""
    metadata = {
        "occasion": suggestion.context.occasion,
        "season": suggestion.season,
        "mood": suggestion.context.mood,
        "shape": suggestion.shape,
        "pieces": len(suggestion.pieces),
        "closet": len(suggestion.closet_pieces),
        "catalog": len(suggestion.catalog_pieces),
        "seed": suggestion.context.seed,
    }
    record_event(
        "outfit_generated",
        request=request,
        object_type="outfit",
        metadata=metadata,
    )
    if mood_page:
        record_event(
            "mood_outfit_generated",
            request=request,
            object_type="outfit",
            metadata=metadata,
        )


def _studio_template_context(request, suggestion, context: OutfitContext) -> dict:
    """Everything the studio templates need, including replay fields for nested forms."""
    regen_query = _context_query(context)
    regen_query.pop("keep", None)
    regen_query.pop("skip", None)
    regen_query["seed"] = str(context.seed + 1)

    return {
        "suggestion": suggestion,
        "keep_all": tokens_for_saving(suggestion.pieces),
        "context_fields": sorted(_context_query(context).items()),
        "regenerate_url": f"{reverse('account:studio')}?{urlencode(regen_query)}",
        "seed": context.seed,
        "page_back": "studio",
        "click_src": "studio",
        "occasion_choices": maps.OCCASION_CHOICES,
        "season_choices": [("", _("Any season")), *ClosetItem.Season.choices],
        "mood_choices": [("", _("No mood")), *maps.MOOD_CHOICES],
        "weather_choices": maps.WEATHER_CONDITIONS,
        "values": {
            "occasion": context.occasion,
            "season": context.season,
            "mood": context.mood,
            "budget": "" if context.budget is None else str(context.budget),
            "weather_temp": (
                ""
                if context.weather.temperature_c is None
                else f"{context.weather.temperature_c:g}"
            ),
            "weather_cond": context.weather.condition,
        },
    }


# ---------------------------------------------------------------------------
# The studio (occasion-driven generator)
# ---------------------------------------------------------------------------


def studio(request):
    """``/account/studio/`` -- the Smart Outfit Generator.

    ``GET`` renders a look for the validated query parameters (and emits the generation
    event); ``POST`` handles the three actions that change state -- replace one piece,
    save the look, regenerate -- each of which redirects back to a replayable ``GET``.
    """
    if request.method == "POST":
        return _studio_action(request)

    context = _studio_context(request.GET)
    suggestion = generate_outfit(request.user, context)
    _emit_generation(request, suggestion)
    return render(
        request, "account/studio.html", _studio_template_context(request, suggestion, context)
    )


def _studio_action(request):
    """Replace / save / regenerate, then PRG back to the exact look that resulted."""
    action = request.POST.get("action", "")
    context = _studio_context(request.POST)
    query = _context_query(context)

    if action == "regenerate":
        query.pop("keep", None)
        query.pop("skip", None)
        query["seed"] = str(context.seed + 1)
        return _redirect_with("account:studio", query)

    if action == "replace":
        role = request.POST.get("role", "")
        previous = request.POST.get("piece", "")
        back = "account:studio-mood" if request.POST.get("back") == "mood" else "account:studio"
        valid_roles = set(OutfitItem.Role.values)
        if role not in valid_roles or not previous.startswith(("c", "p")):
            messages.warning(request, _("That piece could not be replaced."))
            return _redirect_with("account:studio", query)

        # Keep every other piece pinned; drop the outgoing one from play entirely so the
        # re-pick cannot choose it again. The redirect replays this exact context.
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
        query = _context_query(context)
        return _redirect_with(back, query)

    if action == "save":
        suggestion = generate_outfit(request.user, context)
        name = (request.POST.get("name") or "").strip()
        try:
            outfit = save_suggestion(request.user, suggestion, name=name)
        except OutfitError as exc:
            messages.error(request, exc.message)
            return _redirect_with("account:studio", query)
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
        messages.success(request, _("Look saved to your outfits."))
        return redirect("account:outfit-detail", outfit.pk)

    return _redirect_with("account:studio", {})


# ---------------------------------------------------------------------------
# Mood -> outfit
# ---------------------------------------------------------------------------


@require_GET
def studio_mood(request):
    """``/account/studio/mood/`` -- pick a mood, get a look built for it.

    The picker is plain links (shareable URLs, works without JavaScript); choosing a mood
    renders the same look components the studio uses, scored with that mood's palette and
    style bias, and emits both generation events.
    """
    context = _studio_context(request.GET)
    suggestion = None
    if context.mood:
        suggestion = generate_outfit(request.user, context)
        _emit_generation(request, suggestion, mood_page=True)

    regen_query = _context_query(context)
    template_context = {
        "mood_cards": [
            (code, label, maps.MOOD_SIGNALS[code].phrase) for code, label in maps.MOOD_CHOICES
        ],
        "selected_mood": context.mood,
        "occasion": context.occasion,
        "suggestion": suggestion,
        "keep_all": tokens_for_saving(suggestion.pieces) if suggestion else "",
        "context_fields": sorted(_context_query(context).items()),
        "seed": context.seed,
        "page_back": "mood",
        "click_src": "mood",
        "regenerate_url": "",
    }
    if suggestion:
        regen_query["seed"] = str(context.seed + 1)
        template_context["regenerate_url"] = (
            f"{reverse('account:studio-mood')}?{urlencode(regen_query)}"
        )
    return render(request, "account/studio_mood.html", template_context)


# ---------------------------------------------------------------------------
# Style goals
# ---------------------------------------------------------------------------


def studio_goals(request):
    """``/account/studio/goals/`` -- current goal plus the seven-goal picker.

    ``GET`` renders the picker; ``POST`` validates the goal against the FLASH DNA
    vocabulary, persists it and redirects (PRG) with a confirmation message.
    """
    if request.method == "POST":
        return _goal_save(request)

    dna = get_flash_dna(request.user)
    current_goal = dna.style_goal if dna else ""
    return render(
        request,
        "account/studio_goals.html",
        {
            "goal_choices": STYLE_GOALS,
            "current_goal": current_goal,
            "current_goal_label": str(dict(STYLE_GOALS).get(current_goal, "")),
        },
    )


def _goal_save(request) -> HttpResponseRedirect:
    """Persist the chosen style goal; invalid input never reaches the model."""
    goal = request.POST.get("goal") or ""
    valid = {value for value, _label in STYLE_GOALS}
    if goal not in valid:
        messages.error(request, _("Pick one of the style goals."))
        return redirect("account:studio-goals")

    # The row must be born with the goal: FlashDNA.save() full-cleans, and an empty
    # profile (zero preferences) is rejected by design.
    dna, created = FlashDNA.objects.get_or_create(user=request.user, defaults={"style_goal": goal})
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
    label = str(dict(STYLE_GOALS).get(goal, goal))
    messages.success(request, _("Style goal set to %(label)s.") % {"label": label})
    return redirect("account:studio-goals")


# ---------------------------------------------------------------------------
# Storefront: Complete the Look (lazy fragment) and the click tracker
# ---------------------------------------------------------------------------


@require_GET
def complete_look_fragment(request, slug: str):
    """``GET /products/<slug>/complete-look/`` -- the PDP widget's inner HTML.

    Public (anonymous visitors shop too), always friendly: no complementary pieces is a
    calm empty state, never an error. The product page loads this with ``hx-trigger="load"``
    so the product detail view keeps its query-count budget untouched.
    """
    product = get_object_or_404(Product.objects.published(), slug=slug)
    look = complete_look(product, user=request.user)
    record_event(
        "complete_look_viewed",
        request=request,
        object_type="product",
        object_id=product.pk,
        metadata={"category": look.source_category, "items": len(look.items)},
    )
    return render(request, "catalog/complete_look.html", {"look": look, "product": product})


@require_GET
def recommendation_click(request):
    """``GET /track/look/?next=<internal path>`` -- attributed outbound hop.

    Only internal destinations are followed (an open-redirect test lives in the Phase 21
    suite); anything else falls back to the product list. The event records where the
    click went so ``recommendation_clicked`` can be joined to what the customer saw.
    """
    next_url = request.GET.get("next") or ""
    # Internal destinations only: a scheme-relative "//host" or an off-site absolute URL
    # is a phishing vector, not a recommendation.
    allowed = (
        next_url.startswith("/")
        and not next_url.startswith("//")
        and (
            url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            )
        )
    )
    destination = next_url if allowed else ""
    record_event(
        "recommendation_clicked",
        request=request,
        object_type="product",
        metadata={
            "next": destination or "/",
            "source": (request.GET.get("src") or "")[:40],
        },
    )
    return redirect(destination or "catalog:product-list")
