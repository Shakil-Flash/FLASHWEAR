"""Deterministic smart-outfit generation (Phase 21, spec Section 1).

Given a customer, an occasion and optionally a season/mood/budget/weather context, this
service composes an outfit from **FLASH DNA + closet pieces + buyable catalogue products**
and explains every choice in plain language. No randomness, no ML, no external calls: the
same inputs always produce the same look, which is what lets "regenerate", "replace one
item" and URL-driven pages replay exactly what the customer saw.

The shape of a look:

* **separates** -- top + bottom + shoes (plus an outerwear layer when it is cold, and an
  optional accessory when something genuinely fits the signals),
* **dress** -- a dress + shoes (+ the same optional slots) when the occasion or mood calls
  for it *and* a dress candidate exists.

Closet pieces are the customer's own clothes (scored with a small "you already own it"
bonus); catalogue pieces fill the gaps and stay individually cartable. Availability, budget
and disliked colours are hard filters; everything else is a weighted, explainable score --
see :mod:`apps.styling.services.signals` for the vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from apps.closet.models import ClosetItem, Outfit, OutfitItem
from apps.closet.services import outfits as outfit_service
from apps.closet.services.closet import get_available_closet_items
from apps.closet.services.errors import OutfitError
from apps.styling.models.flash_dna import get_flash_dna
from apps.styling.services import signals as maps
from apps.styling.services.catalog_pool import product_price, suggestable_products
from apps.styling.services.signals import (
    StyleSignals,
    WeatherContext,
    build_style_signals,
    color_matches_disliked,
    color_matches_favorites,
    color_matches_palette,
    text_matches_styles,
)

__all__ = [
    "ROLE_ORDER",
    "OutfitContext",
    "OutfitSuggestion",
    "SuggestedPiece",
    "generate_outfit",
    "parse_keep",
    "parse_skip",
    "save_suggestion",
    "suggestion_to_dict",
    "tokens_for_saving",
]

Role = OutfitItem.Role

#: Display order for the slots of a look (dress first: it replaces top + bottom).
ROLE_ORDER: tuple[str, ...] = (
    Role.DRESS.value,
    Role.TOP.value,
    Role.BOTTOM.value,
    Role.OUTERWEAR.value,
    Role.SHOES.value,
    Role.ACCESSORY.value,
)

#: Outfit slot -> wardrobe category ("top" -> "tops"), plain strings on both sides.
CATEGORY_FOR_ROLE: dict[str, str] = {
    slot.value: category.value for category, slot in outfit_service.ROLE_FOR_CATEGORY.items()
}

#: Occasions (and moods) where a dress is a valid body layer when a candidate exists.
DRESS_OCCASIONS = frozenset({"party", "date", "formal"})
DRESS_MOODS = frozenset({"elegant", "romantic", "confident"})

#: Cold seasons: an outerwear layer joins the look (also driven by weather warmth).
WARM_SEASONS = frozenset({"autumn", "winter"})

#: How much owning a piece beats buying it (kept modest so strong catalogue fits still win).
CLOSET_SOURCE_BONUS = 1.0

#: A scored candidate within this distance of the best still counts as "as good" -- the
#: seed rotates *within* the band, so regeneration changes the look without downgrading it.
SEED_BAND = 1.0

#: Accessories only join when something actually matches the signals (no random extras).
ACCESSORY_MIN_SCORE = 2.0

#: Reasons kept per piece and for the look as a whole (cards stay readable).
MAX_PIECE_REASONS = 4
MAX_LOOK_REASONS = 5

#: Valid piece tokens: ``c<closet id>`` / ``p<product id>``.
_TOKEN_PREFIXES = ("c", "p")


# ---------------------------------------------------------------------------
# Context and output shapes
# ---------------------------------------------------------------------------


@dataclass
class OutfitContext:
    """One generation request.

    Everything here round-trips through a query string, so the page a customer sees can be
    replayed exactly -- and regenerated deterministically from the same URL.
    """

    occasion: str = "everyday"
    season: str = ""
    mood: str = ""
    budget: Decimal | None = None
    weather: WeatherContext = field(default_factory=WeatherContext)
    seed: int = 0
    #: slot -> piece token to keep in place (the pieces of a look being replaced one by one).
    pins: dict[str, str] = field(default_factory=dict)
    #: piece tokens excluded from the whole look (the piece a "replace" swapped out).
    skips: frozenset[str] = frozenset()


@dataclass
class SuggestedPiece:
    """One slot of a generated look: a closet garment or a buyable catalogue product."""

    role: str
    source: str  # "closet" | "catalog"
    closet_item: ClosetItem | None = None
    product: object | None = None
    price: Decimal | None = None
    score: float = 0.0
    reasons: tuple[str, ...] = ()

    @property
    def pk(self) -> int | None:
        if self.source == "closet":
            return self.closet_item.pk if self.closet_item else None
        return self.product.pk if self.product else None

    @property
    def token(self) -> str:
        prefix = "c" if self.source == "closet" else "p"
        return f"{prefix}{self.pk}" if self.pk is not None else ""


@dataclass
class OutfitSuggestion:
    """The generated look: ordered pieces, explainable reasons, graceful warnings."""

    context: OutfitContext
    pieces: list[SuggestedPiece]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    shape: str  # "separates" | "dress" | "empty"
    season: str = ""  # effective season used for scoring and saving ("" = unspecified)

    @property
    def closet_pieces(self) -> list[SuggestedPiece]:
        return [p for p in self.pieces if p.source == "closet"]

    @property
    def catalog_pieces(self) -> list[SuggestedPiece]:
        return [p for p in self.pieces if p.source == "catalog"]


# ---------------------------------------------------------------------------
# Query-string helpers (regenerate / replace round-trip through the URL)
# ---------------------------------------------------------------------------


def parse_keep(raw: str) -> dict[str, str]:
    """``"top:c3,shoes:p7"`` -> ``{"top": "c3", "shoes": "p7"}`` (invalid entries dropped)."""
    valid_slots = frozenset(role.value for role in Role)
    pins: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        role, sep, token = chunk.strip().partition(":")
        if sep and role in valid_slots and token:
            pins[role] = token
    return pins


def parse_skip(raw: str) -> frozenset[str]:
    """``"c3,p9"`` -> the set of excluded piece tokens."""
    return frozenset(
        t.strip() for t in (raw or "").split(",") if t.strip() and t.strip()[:1] in _TOKEN_PREFIXES
    )


def tokens_for_saving(pieces: list[SuggestedPiece], *, replace_role: str = "") -> str:
    """Encode a look's kept pieces as a ``keep`` value for a follow-up replace request.

    The slot being replaced is left out (it has to re-pick) -- the caller adds the outgoing
    piece's token to ``skip`` so it cannot win the slot again.
    """
    return ",".join(
        f"{piece.role}:{piece.token}"
        for piece in pieces
        if piece.token and piece.role != replace_role
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _season_fit(item_season: str, season: str) -> tuple[float, str | None]:
    """Season affinity for a closet piece (catalogue products carry no season column)."""
    item_season = (item_season or "").lower()
    if not season or season == "all_season" or not item_season or item_season == "all_season":
        return 0.5, None
    if item_season == season:
        return 1.5, f"Right for {season}."
    return -1.0, None


def _score_closet_item(
    item: ClosetItem, *, signals: StyleSignals, season: str
) -> tuple[float, tuple[str, ...]]:
    """Weighted, explainable score for one wardrobe piece. Hard filters run before this."""
    score = CLOSET_SOURCE_BONUS
    reasons: list[str] = []

    color = item.color or ""
    if color:
        if color_matches_favorites(color, signals):
            score += 3.0
            reasons.append(f"Matches your favourite colour, {color.title()}.")
        elif signals.palette and color_matches_palette(color, signals):
            score += 1.5
            reasons.append(f"Fits the {signals.palette} palette.")

    if item.style and item.style in signals.closet_style_bias:
        score += 2.5
        reasons.append(f"Matches your {item.style.replace('_', ' ')} preference.")

    if item.occasion:
        if item.occasion in signals.occasion_signals.closet:
            score += 2.0
            reasons.append(f"Works for {signals.occasion} plans.")
        elif item.occasion == signals.dna_occasion:
            score += 1.0

    season_score, season_reason = _season_fit(item.season, season)
    score += season_score
    if season_reason:
        reasons.append(season_reason)

    brand = (item.brand or "").lower()
    if brand and brand in signals.brands:
        score += 1.5
        reasons.append(f"Your preferred brand, {item.brand}.")

    material = (item.material or "").lower()
    if material and material in signals.materials:
        score += 1.0
        reasons.append(f"In {item.material}, a fabric you prefer.")

    return score, tuple(reasons[:MAX_PIECE_REASONS])


def _score_product(
    product, *, signals: StyleSignals, price: Decimal | None
) -> tuple[float, tuple[str, ...]]:
    """Weighted, explainable score for one catalogue product (buyable variants only)."""
    score = 0.0
    reasons: list[str] = []

    color_names = [c.name for c in product.available_colors if c.name]
    favorite_hit = next((c for c in color_names if color_matches_favorites(c, signals)), None)
    if favorite_hit:
        score += 3.0
        reasons.append(f"Comes in {favorite_hit}, one of your favourite colours.")
    elif (
        signals.palette
        and color_names
        and any(color_matches_palette(c, signals) for c in color_names)
    ):
        score += 1.5
        reasons.append(f"Fits the {signals.palette} palette.")

    fit_name = product.fit.name if product.fit_id and product.fit else ""
    category_name = product.category.name if product.category else ""
    tag_names = " ".join(tag.name for tag in product.tags.all())
    if text_matches_styles(" ".join((fit_name, tag_names, category_name)), signals):
        score += 1.5
        reasons.append("Styled to match your preferences.")

    if product.brand_id and product.brand:
        brand = product.brand.name.lower()
        if brand and brand in signals.brands:
            score += 1.5
            reasons.append(f"Your preferred brand, {product.brand.name}.")

    for material in product.materials.all():
        if material.name.lower() in signals.materials:
            score += 1.0
            reasons.append(f"In {material.name}, a fabric you prefer.")
            break

    if price is not None and signals.price_upper is not None:
        if price <= signals.price_upper:
            score += 1.0
            reasons.append("Within your usual price range.")
        else:
            score -= 0.5

    if signals.trending and (product.is_new or product.is_featured):
        score += 1.0
        reasons.append("One of the freshest pieces in the shop.")

    return score, tuple(reasons[:MAX_PIECE_REASONS])


def _is_excluded(item_or_product, *, signals: StyleSignals, from_catalog: bool) -> bool:
    """Hard filter: disliked colours.

    A closet piece is one physical colourway, so a disliked colour drops it entirely. A
    catalogue product drops only when *every* buyable colour is disliked -- colourways are
    the customer's pick on the product page, and one liked colour keeps it in play.
    """
    if not signals.disliked_colors:
        return False
    if not from_catalog:
        color = (item_or_product.color or "").lower()
        return bool(color) and color_matches_disliked(color, signals)
    colors = [c.name for c in item_or_product.available_colors if c.name]
    if not colors:
        return False
    return all(color_matches_disliked(c, signals) for c in colors)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _ranked(
    closet_items: list[ClosetItem],
    catalog_products: list,
    *,
    signals: StyleSignals,
    season: str,
) -> list[tuple[object, float, tuple[str, ...]]]:
    """Score both pools and sort them together, deterministically.

    Best score first; ties break toward owned pieces, then name, then id -- so the order
    never depends on dict iteration or database luck.
    """
    scored: list[tuple[object, float, tuple[str, ...]]] = []
    for item in closet_items:
        score, reasons = _score_closet_item(item, signals=signals, season=season)
        scored.append((item, score, reasons))
    for product in catalog_products:
        score, reasons = _score_product(product, signals=signals, price=product_price(product))
        scored.append((product, score, reasons))
    scored.sort(
        key=lambda entry: (
            -entry[1],
            0 if isinstance(entry[0], ClosetItem) else 1,
            (getattr(entry[0], "name", "") or "").lower(),
            entry[0].pk or 0,
        )
    )
    return scored


def _token_of(candidate) -> str:
    prefix = "c" if isinstance(candidate, ClosetItem) else "p"
    return f"{prefix}{candidate.pk}"


def _pick(
    ranked: list[tuple[object, float, tuple[str, ...]]],
    *,
    role: str,
    context: OutfitContext,
    used: set[str],
    min_score: float | None = None,
) -> SuggestedPiece | None:
    """Choose one piece for a slot.

    Pins win outright (an explicit "keep this" beats the score, including the accessory
    floor); skips and already-used tokens never win. With no pin, the seed rotates within
    the top score band, so regeneration is a peer swap rather than a downgrade -- and a pin
    whose piece vanished degrades to a normal pick instead of an empty slot.
    """
    base = [
        entry
        for entry in ranked
        if entry[0] is not None
        and _token_of(entry[0]) not in context.skips
        and _token_of(entry[0]) not in used
    ]
    if not base:
        return None

    pin = context.pins.get(role)
    pinned = False
    if pin:
        hits = [entry for entry in base if _token_of(entry[0]) == pin]
        if hits:
            base, pinned = hits, True

    if not pinned and min_score is not None and base[0][1] < min_score:
        return None

    best = base[0][1]
    band = [entry for entry in base if entry[1] >= best - SEED_BAND]
    candidate, score, reasons = band[context.seed % len(band)]

    source = "closet" if isinstance(candidate, ClosetItem) else "catalog"
    piece = SuggestedPiece(
        role=role,
        source=source,
        closet_item=candidate if source == "closet" else None,
        product=None if source == "closet" else candidate,
        price=None if source == "closet" else product_price(candidate),
        score=score,
        reasons=reasons[:MAX_PIECE_REASONS],
    )
    if piece.token:
        used.add(piece.token)
    return piece


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _look_reasons(
    signals: StyleSignals,
    context: OutfitContext,
    season: str,
    has_catalog: bool,
) -> tuple[str, ...]:
    """The human-readable "why this look" lines (spec Section 1)."""
    reasons: list[str] = []
    label = str(dict(maps.OCCASION_CHOICES).get(signals.occasion, signals.occasion))
    reasons.append(f"Built for {label} -- it {signals.occasion_signals.phrase}.")

    bits: list[str] = []
    if signals.dna_styles:
        styles = ", ".join(s.replace("_", " ") for s in signals.dna_styles[:3])
        bits.append(f"your preferred {styles} style")
    if signals.palette:
        bits.append(f"{signals.palette} colour palette")
    elif signals.favorite_colors:
        favorites = ", ".join(c.title() for c in signals.favorite_colors[:3])
        bits.append(f"palette of {favorites}")
    if bits:
        reasons.append("Matches " + " and ".join(bits) + ".")

    if signals.mood and signals.mood_signals:
        mood_label = str(dict(maps.MOOD_CHOICES).get(signals.mood, signals.mood))
        reasons.append(f"Styled for a {mood_label} mood: {signals.mood_signals.phrase}.")
    if signals.goal and signals.goal_signals:
        goal_label = str(dict(maps.GOAL_CHOICES).get(signals.goal, signals.goal))
        reasons.append(f"Guided by your {goal_label} style goal -- {signals.goal_signals.phrase}.")
    if season and season != "all_season":
        reasons.append(f"Seasoned for {season}.")
    if signals.weather.phrases:
        reasons.append(f"{signals.weather.phrases[0]}.")
    if context.budget is not None and has_catalog:
        reasons.append(f"Catalogue pieces fit your {context.budget} budget.")

    return tuple(str(reason) for reason in reasons[:MAX_LOOK_REASONS])


def generate_outfit(user, context: OutfitContext | None = None) -> OutfitSuggestion:
    """Build one deterministic look for *user* under *context*.

    Flow: signals -> pools (one closet query + one catalogue query) -> slot plan -> per-slot
    scoring with pins/skips/seed rotation -> reasons and warnings. Missing data (no DNA, no
    closet, no stock) degrades to fewer signals or fewer slots -- never an exception.
    """
    context = context or OutfitContext()
    dna = get_flash_dna(user)
    signals = build_style_signals(
        dna=dna,
        occasion=context.occasion,
        season=context.season,
        mood=context.mood,
        budget=context.budget,
        weather=context.weather,
    )
    season = signals.season or signals.dna_season
    if season not in ClosetItem.Season.values:
        season = ""  # never let an unvalidated string reach Outfit.full_clean()

    # --- pools: two queries in total, everything else reads prefetch caches ---
    closet_by_category: dict[str, list[ClosetItem]] = {}
    closet_total = 0
    if getattr(user, "is_authenticated", False):
        for item in get_available_closet_items(user):
            closet_total += 1
            if _is_excluded(item, signals=signals, from_catalog=False):
                continue
            closet_by_category.setdefault(item.category, []).append(item)

    catalog_by_category: dict[str, list] = {}
    budget = context.budget
    for raw_category, products in suggestable_products().items():
        kept = []
        for product in products:
            if _is_excluded(product, signals=signals, from_catalog=True):
                continue
            if budget is not None:
                price = product_price(product)
                if price is None or price > budget:
                    continue
            kept.append(product)
        if kept:
            catalog_by_category[str(raw_category)] = kept

    has_dress = bool(closet_by_category.get("dresses")) or bool(catalog_by_category.get("dresses"))

    # --- slot plan -----------------------------------------------------------
    plan: list[str] = []
    dress_ok = (
        signals.occasion in DRESS_OCCASIONS or (signals.mood in DRESS_MOODS and has_dress)
    ) and has_dress
    if dress_ok:
        plan.extend((Role.DRESS.value, Role.SHOES.value))
        shape = "dress"
    else:
        plan.extend((Role.TOP.value, Role.BOTTOM.value, Role.SHOES.value))
        shape = "separates"
    if signals.weather.warmth >= 2 or season in WARM_SEASONS:
        plan.append(Role.OUTERWEAR.value)
    plan.append(Role.ACCESSORY.value)

    # --- select each slot ----------------------------------------------------
    used: set[str] = set()
    pieces: list[SuggestedPiece] = []
    missing: list[str] = []
    for role in plan:
        category = CATEGORY_FOR_ROLE[role]
        ranked = _ranked(
            closet_by_category.get(category, []),
            catalog_by_category.get(category, []),
            signals=signals,
            season=season,
        )
        min_score = ACCESSORY_MIN_SCORE if role == Role.ACCESSORY.value else None
        piece = _pick(ranked, role=role, context=context, used=used, min_score=min_score)
        if piece is None:
            if role == Role.ACCESSORY.value:
                continue  # optional slot: silence, not a warning
            missing.append(role)
            continue
        pieces.append(piece)

    # --- warnings ------------------------------------------------------------
    if not pieces:
        return OutfitSuggestion(
            context=context,
            pieces=[],
            reasons=(),
            warnings=(
                "We could not build a look yet. Add a few pieces to your closet "
                "or widen your filters.",
            ),
            shape="empty",
            season=season,
        )

    warnings: list[str] = []
    owned = [p for p in pieces if p.source == "closet"]
    if not owned:
        if closet_total == 0:
            warnings.append("Your closet is empty, so everything here comes from the catalogue.")
        else:
            warnings.append(
                "Nothing in your closet matched this look, so it leans on the catalogue."
            )
    if missing:
        labels = ", ".join(role.replace("_", " ") for role in missing)
        warnings.append(f"No fitting piece found for: {labels}.")

    pieces.sort(key=lambda piece: ROLE_ORDER.index(piece.role))
    has_catalog = any(p.source == "catalog" for p in pieces)
    return OutfitSuggestion(
        context=context,
        pieces=pieces,
        reasons=_look_reasons(signals, context, season, has_catalog),
        warnings=tuple(warnings),
        shape=shape,
        season=season,
    )


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def save_suggestion(user, suggestion: OutfitSuggestion, *, name: str = "") -> Outfit:
    """Persist the look's **closet** pieces as a saved outfit (one transaction).

    Catalogue pieces are shopping suggestions, not owned garments, so they never enter the
    outfit join table -- they stay on the look with their add-to-bag action. Raises
    ``OutfitError`` when there is nothing owned to save, so the caller can show a friendly
    message instead of silently creating an empty outfit.
    """
    closet_pieces = [p for p in suggestion.pieces if p.source == "closet" and p.closet_item]
    if not closet_pieces:
        raise OutfitError(
            "There is nothing to save yet -- this look is made of catalogue pieces. "
            "Add pieces to your closet first, or shop the look instead.",
            code="no_closet_pieces",
        )

    context = suggestion.context
    label = str(dict(maps.OCCASION_CHOICES).get(context.occasion, context.occasion))
    outfit_name = (name or "").strip() or f"{label or 'New'} look"
    outfit_name = outfit_name[:120]

    dna = get_flash_dna(user)
    goal = dna.style_goal if dna else ""
    season = suggestion.season if suggestion.season in ClosetItem.Season.values else ""

    with transaction.atomic():
        outfit = outfit_service.create_outfit(
            user,
            name=outfit_name,
            description=" ".join(suggestion.reasons)[:2000],
            occasion=maps.OCCASION_TO_CLOSET_OCCASION.get(context.occasion, ""),
            season=season,
            style=maps.GOAL_TO_CLOSET_STYLE.get(goal, ""),
            status=Outfit.Status.SAVED,
        )
        for piece in closet_pieces:
            note = piece.reasons[0] if piece.reasons else ""
            outfit_service.add_item(outfit, piece.closet_item, note=note)
    return outfit


# ---------------------------------------------------------------------------
# Serialisation (API responses; templates consume the dataclasses directly)
# ---------------------------------------------------------------------------


def _piece_dict(piece: SuggestedPiece) -> dict:
    """JSON-safe view of one piece (variant choices included for add-to-bag)."""
    data: dict = {
        "role": piece.role,
        "source": piece.source,
        "token": piece.token,
        "price": str(piece.price) if piece.price is not None else None,
        "reasons": list(piece.reasons),
        "score": round(piece.score, 3),
    }
    if piece.source == "closet" and piece.closet_item:
        item = piece.closet_item
        image = item.display_image
        data.update(
            {
                "closet_item_id": item.pk,
                "name": item.name,
                "brand": item.brand,
                "color": item.color,
                "size": item.size,
                "image": image.url if image else None,
            }
        )
    elif piece.product:
        product = piece.product
        image = product.primary_image
        data.update(
            {
                "product_id": product.pk,
                "slug": product.slug,
                "name": product.name,
                "brand": product.brand.name if product.brand else "",
                "url": product.get_absolute_url(),
                "image": image.image.url if image else None,
                "variants": [
                    {
                        "id": variant.pk,
                        "label": variant.option_label,
                        "price": str(variant.price) if variant.price is not None else None,
                    }
                    for variant in product.purchasable_variants
                ],
            }
        )
    return data


def suggestion_to_dict(suggestion: OutfitSuggestion) -> dict:
    """JSON-safe view of the whole look for the studio API."""
    return {
        "shape": suggestion.shape,
        "occasion": suggestion.context.occasion,
        "season": suggestion.season,
        "mood": suggestion.context.mood,
        "budget": (
            str(suggestion.context.budget) if suggestion.context.budget is not None else None
        ),
        "seed": suggestion.context.seed,
        "pieces": [_piece_dict(piece) for piece in suggestion.pieces],
        "reasons": list(suggestion.reasons),
        "warnings": list(suggestion.warnings),
        "saveable": bool(suggestion.closet_pieces),
    }
