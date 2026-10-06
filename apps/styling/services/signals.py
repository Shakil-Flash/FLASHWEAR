"""Deterministic styling signals for Phase 21 fashion intelligence.

One module owns every controlled vocabulary the smart-outfit layer reasons about --
occasions, moods, style goals, weather context -- and the *mappings* that turn them into
plain scoring signals. Nothing here talks to a network, a model or a clock: the same inputs
always yield the same output, which is what makes generation testable and explainable.

The vocabulary is deliberately small and spec-shaped (seven occasions, seven moods, seven
goals). Everything degrades gracefully: a blank DNA profile, an unknown weather condition or
a missing mood simply contributes no signals instead of raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.utils.translation import gettext_lazy as _

from apps.styling.models.flash_dna import STYLE_GOALS

__all__ = [
    "GOAL_SIGNALS",
    "MOODS",
    "MOOD_CHOICES",
    "MOOD_SIGNALS",
    "OCCASIONS",
    "OCCASION_CHOICES",
    "OCCASION_SIGNALS",
    "WEATHER_CONDITIONS",
    "StyleSignals",
    "WeatherContext",
    "build_style_signals",
]


# ---------------------------------------------------------------------------
# Controlled vocabularies (spec Section 1 / 3 / 4)
# ---------------------------------------------------------------------------

#: ``(value, label)`` -- the seven generation situations from the spec.
OCCASIONS: list[tuple[str, str]] = [
    ("casual", _("Casual")),
    ("work", _("Work")),
    ("party", _("Party")),
    ("date", _("Date")),
    ("travel", _("Travel")),
    ("formal", _("Formal")),
    ("everyday", _("Everyday")),
]

#: The seven moods from the spec.
MOODS: list[tuple[str, str]] = [
    ("chill", _("Chill")),
    ("confident", _("Confident")),
    ("energetic", _("Energetic")),
    ("elegant", _("Elegant")),
    ("romantic", _("Romantic")),
    ("adventurous", _("Adventurous")),
    ("cozy", _("Cozy")),
]

#: Weather conditions accepted on the optional weather input (never fetched, only provided).
WEATHER_CONDITIONS: list[tuple[str, str]] = [
    ("", _("Not specified")),
    ("clear", _("Clear")),
    ("cloudy", _("Cloudy")),
    ("rain", _("Rain")),
    ("wind", _("Wind")),
    ("snow", _("Snow")),
]

OCCASION_CHOICES = [tuple(item) for item in OCCASIONS]
MOOD_CHOICES = [tuple(item) for item in MOODS]
GOAL_CHOICES = [tuple(item) for item in STYLE_GOALS]


# ---------------------------------------------------------------------------
# Signal mappings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OccasionSignals:
    """How one spec occasion weights a look.

    ``dna`` is the matching FLASH DNA occasion code (``work`` lives in DNA as ``office``);
    ``closet`` is which ``ClosetItem.occasion`` values score as appropriate; ``styles`` are
    closet-style bias codes the occasion favours.
    """

    dna: str
    closet: tuple[str, ...]
    styles: tuple[str, ...]
    formality: int
    phrase: str


OCCASION_SIGNALS: dict[str, OccasionSignals] = {
    "casual": OccasionSignals(
        dna="casual",
        closet=("casual",),
        styles=("relaxed",),
        formality=1,
        phrase="keeps things relaxed and easy",
    ),
    "work": OccasionSignals(
        dna="office",
        closet=("office",),
        styles=("smart_casual", "classic"),
        formality=3,
        phrase="reads polished enough for work",
    ),
    "party": OccasionSignals(
        dna="party",
        closet=("party", "streetwear"),
        styles=("relaxed",),
        formality=2,
        phrase="has going-out energy",
    ),
    "date": OccasionSignals(
        dna="date",
        closet=("casual", "party"),
        styles=("smart_casual", "minimal"),
        formality=2,
        phrase="strikes a considered, put-together balance",
    ),
    "travel": OccasionSignals(
        dna="travel",
        closet=("travel", "casual"),
        styles=("relaxed", "athletic"),
        formality=1,
        phrase="stays comfortable on the move",
    ),
    "formal": OccasionSignals(
        dna="formal",
        closet=("formal",),
        styles=("classic", "smart_casual"),
        formality=4,
        phrase="leans formal and refined",
    ),
    "everyday": OccasionSignals(
        dna="casual",
        closet=("casual", "streetwear"),
        styles=("relaxed",),
        formality=1,
        phrase="works for everyday wear",
    ),
}


@dataclass(frozen=True)
class MoodSignals:
    """How one mood weights a look -- style bias, palette bias and a plain-language phrase."""

    styles: tuple[str, ...]
    palette: str
    formality: int
    phrase: str


MOOD_SIGNALS: dict[str, MoodSignals] = {
    "chill": MoodSignals(
        styles=("relaxed",),
        palette="muted",
        formality=1,
        phrase="relaxed fits and muted tones",
    ),
    "confident": MoodSignals(
        styles=("smart_casual", "classic"),
        palette="bold",
        formality=3,
        phrase="sharp lines in confident colours",
    ),
    "energetic": MoodSignals(
        styles=("athletic", "relaxed"),
        palette="bright",
        formality=1,
        phrase="bright, movement-friendly pieces",
    ),
    "elegant": MoodSignals(
        styles=("classic", "smart_casual"),
        palette="neutral",
        formality=4,
        phrase="refined neutrals and clean tailoring",
    ),
    "romantic": MoodSignals(
        styles=("smart_casual", "minimal"),
        palette="soft",
        formality=2,
        phrase="soft tones and gentle shapes",
    ),
    "adventurous": MoodSignals(
        styles=("relaxed", "athletic"),
        palette="earth",
        formality=1,
        phrase="rugged, ready-for-anything pieces",
    ),
    "cozy": MoodSignals(
        styles=("relaxed", "classic"),
        palette="warm",
        formality=1,
        phrase="warm layers and soft textures",
    ),
}


@dataclass(frozen=True)
class GoalSignals:
    """How a style goal (spec Section 3) weights a look.

    ``trending`` is the ``Trendy`` goal's proxy: prefer freshly published/featured catalogue
    pieces, because the catalogue carries no "trend" column and inventing one would be noise.
    """

    styles: tuple[str, ...]
    palette: str
    formality: int
    trending: bool
    phrase: str


GOAL_SIGNALS: dict[str, GoalSignals] = {
    "minimal": GoalSignals(
        styles=("minimal", "classic"),
        palette="neutral",
        formality=2,
        trending=False,
        phrase="clean minimal lines",
    ),
    "streetwear": GoalSignals(
        styles=("relaxed", "oversized"),
        palette="bold",
        formality=1,
        trending=False,
        phrase="streetwear attitude",
    ),
    "smart_casual": GoalSignals(
        styles=("smart_casual", "classic"),
        palette="neutral",
        formality=3,
        trending=False,
        phrase="smart-casual polish",
    ),
    "formal": GoalSignals(
        styles=("classic", "smart_casual"),
        palette="neutral",
        formality=4,
        trending=False,
        phrase="formal precision",
    ),
    "trendy": GoalSignals(
        styles=(),
        palette="",
        formality=2,
        trending=True,
        phrase="the freshest new arrivals",
    ),
    "classic": GoalSignals(
        styles=("classic", "minimal"),
        palette="neutral",
        formality=3,
        trending=False,
        phrase="timeless classics",
    ),
    "athleisure": GoalSignals(
        styles=("athletic", "relaxed"),
        palette="muted",
        formality=1,
        trending=False,
        phrase="athleisure comfort",
    ),
}

#: Palette bias -> colour-name keywords that count as a match (lower-case substring match).
PALETTES: dict[str, tuple[str, ...]] = {
    "neutral": ("black", "white", "grey", "gray", "navy", "sand", "beige", "ivory", "charcoal"),
    "muted": ("grey", "gray", "sand", "stone", "taupe", "olive", "navy", "charcoal"),
    "bold": ("red", "cobalt", "yellow", "orange", "magenta", "pink", "electric"),
    "bright": ("red", "yellow", "orange", "green", "blue", "pink", "cobalt"),
    "soft": ("pink", "blush", "lilac", "lavender", "cream", "ivory", "rose"),
    "warm": ("brown", "tan", "rust", "cream", "burgundy", "maroon", "camel"),
    "earth": ("olive", "khaki", "brown", "tan", "rust", "forest", "sand"),
}

#: DNA style code -> the closet-style codes it reads as when scoring wardrobe pieces.
DNA_STYLE_TO_CLOSET_STYLE: dict[str, tuple[str, ...]] = {
    "minimal": ("minimal",),
    "streetwear": ("relaxed",),
    "smart_casual": ("smart_casual",),
    "formal": ("classic",),
    "vintage": (),
    "sporty": ("athletic",),
    "oversized": ("relaxed",),
    "classic": ("classic",),
}

#: Spec occasion -> ``Outfit.occasion`` / ``ClosetItem.occasion`` vocabulary (blank = unmapped).
OCCASION_TO_CLOSET_OCCASION: dict[str, str] = {
    "casual": "casual",
    "work": "office",
    "party": "party",
    "date": "",
    "travel": "travel",
    "formal": "formal",
    "everyday": "casual",
}

#: Style goal -> ``Outfit.style`` / ``ClosetItem.Style`` vocabulary (blank = unmapped).
GOAL_TO_CLOSET_STYLE: dict[str, str] = {
    "minimal": "minimal",
    "streetwear": "",
    "smart_casual": "smart_casual",
    "formal": "classic",
    "trendy": "",
    "classic": "classic",
    "athleisure": "athletic",
}

#: FLASH DNA price range code -> upper bound (``None`` = no ceiling, ``above_150``).
PRICE_RANGE_UPPER: dict[str, Decimal | None] = {
    "under_25": Decimal("25"),
    "under_50": Decimal("50"),
    "under_100": Decimal("100"),
    "under_150": Decimal("150"),
    "above_150": None,
}


# ---------------------------------------------------------------------------
# Weather context (spec Section 5): optional input, zero external calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeatherContext:
    """Optional weather/context input for the recommendation layer.

    Explicitly provided only -- nothing in this module ever fetches weather. When both
    fields are empty the signal bundle simply treats weather as unspecified.
    """

    temperature_c: float | None = None
    condition: str = ""

    @property
    def is_specified(self) -> bool:
        return self.temperature_c is not None or bool(self.condition)


@dataclass(frozen=True)
class WeatherSignals:
    """Derived warmth signals: ``warmth`` 0 (warm) .. 3 (freezing), plus phrase pieces."""

    warmth: int
    phrases: tuple[str, ...]


def weather_signals(weather: WeatherContext | None) -> WeatherSignals:
    """Map an optional weather input to deterministic layering signals.

    Temperature bands are deliberately coarse (nobody needs 1-degree precision to pick a
    jacket), and conditions only ever *add* warmth: rain and snow read colder than the
    thermometer says.
    """
    if weather is None or not weather.is_specified:
        return WeatherSignals(warmth=0, phrases=())

    phrases: list[str] = []
    warmth = 0
    temp = weather.temperature_c
    if temp is not None:
        if temp < 5:
            warmth = 3
        elif temp < 12:
            warmth = 2
        elif temp < 18:
            warmth = 1
        phrases.append(f"{temp:g} °C")
    if weather.condition in ("rain", "snow"):
        warmth = min(warmth + 1, 3)
        phrases.append("wet weather" if weather.condition == "rain" else "snow")
    elif weather.condition == "wind":
        warmth = min(warmth + 1, 3)
        phrases.append("wind")
    elif weather.condition == "cloudy" and warmth == 0:
        warmth = 1

    if warmth >= 2:
        lead = "Layers up for"
    elif warmth == 1:
        lead = "A light layer for"
    else:
        lead = "Breathable pieces for"
    return WeatherSignals(warmth=warmth, phrases=(f"{lead} {', '.join(phrases)}",))


# ---------------------------------------------------------------------------
# The combined signal bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StyleSignals:
    """Everything the scorer needs about one generation request, pre-normalised."""

    occasion: str
    occasion_signals: OccasionSignals
    season: str
    mood: str
    mood_signals: MoodSignals | None
    goal: str
    goal_signals: GoalSignals | None
    #: Lower-case favourite colour names (DNA), disliked colours kept apart so they can penalise.
    favorite_colors: tuple[str, ...]
    disliked_colors: tuple[str, ...]
    #: Closet-style bias codes (occasion + mood + goal + DNA styles mapped through).
    closet_style_bias: frozenset[str]
    #: The customer's own FLASH DNA style codes ("minimal", "smart_casual", ...) as stored.
    dna_styles: tuple[str, ...]
    #: Lower-case text terms for catalogue matching (styles in both code and spaced form).
    text_style_terms: frozenset[str]
    #: Palette bias name ("neutral", "muted", ...) and its keyword set.
    palette: str
    palette_terms: frozenset[str]
    brands: tuple[str, ...]
    fits: tuple[str, ...]
    materials: tuple[str, ...]
    #: FLASH DNA preferred occasion/season codes as stored (single-choice CharFields).
    dna_occasion: str
    dna_season: str
    price_upper: Decimal | None
    budget: Decimal | None
    weather: WeatherSignals
    trending: bool


def _style_terms(*groups: tuple[str, ...] | frozenset[str]) -> frozenset[str]:
    """Flatten style codes into comparable lower-case terms (``smart_casual`` -> also
    ``smart casual``), so both closet codes and catalogue free text can match."""
    terms: set[str] = set()
    for group in groups:
        for term in group:
            term = (term or "").strip().lower()
            if not term:
                continue
            terms.add(term)
            terms.add(term.replace("_", " "))
    return frozenset(terms)


def build_style_signals(
    *,
    dna,
    occasion: str,
    season: str = "",
    mood: str = "",
    budget: Decimal | None = None,
    weather: WeatherContext | None = None,
) -> StyleSignals:
    """Normalise request inputs + FLASH DNA into one deterministic signal bundle.

    ``dna`` may be ``None`` (a customer without a profile): every preference simply reads
    empty, and generation falls back to occasion/season/mood structure only.
    """
    occasion_key = occasion if occasion in OCCASION_SIGNALS else "everyday"
    occ = OCCASION_SIGNALS[occasion_key]

    mood_key = mood if mood in MOOD_SIGNALS else ""
    mood_sig = MOOD_SIGNALS.get(mood_key)

    goal = getattr(dna, "style_goal", "") or ""
    goal_sig = GOAL_SIGNALS.get(goal)

    favorite: tuple[str, ...] = ()
    disliked: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()
    fits: tuple[str, ...] = ()
    materials: tuple[str, ...] = ()
    dna_styles: tuple[str, ...] = ()
    dna_occasion = ""
    dna_season = ""
    price_upper: Decimal | None = None

    if dna is not None:
        favorite = tuple(sorted(c.name.lower() for c in dna.favorite_colors.all() if c.name))
        disliked = tuple(sorted(c.name.lower() for c in dna.disliked_colors.all() if c.name))
        brands = tuple(sorted(b.name.lower() for b in dna.preferred_brands.all() if b.name))
        fits = tuple(sorted(f.name.lower() for f in dna.preferred_fits.all() if f.name))
        materials = tuple(sorted(m.name.lower() for m in dna.preferred_materials.all() if m.name))
        raw_styles = (dna.styles or "").lower()
        dna_styles = tuple(s.strip() for s in raw_styles.split(",") if s.strip())
        dna_occasion = dna.preferred_occasions or ""
        dna_season = dna.preferred_seasons or ""
        price_upper = PRICE_RANGE_UPPER.get(dna.preferred_price_range)

    closet_bias: set[str] = set(occ.styles)
    if mood_sig:
        closet_bias.update(mood_sig.styles)
    if goal_sig:
        closet_bias.update(goal_sig.styles)
    for style in dna_styles:
        closet_bias.update(DNA_STYLE_TO_CLOSET_STYLE.get(style, ()))

    text_terms = _style_terms(dna_styles, occ.styles)
    if mood_sig:
        text_terms = _style_terms(text_terms, mood_sig.styles)
    if goal_sig:
        text_terms = _style_terms(text_terms, goal_sig.styles)

    palette_name = ""
    if mood_sig and mood_sig.palette:
        palette_name = mood_sig.palette
    elif goal_sig and goal_sig.palette:
        palette_name = goal_sig.palette

    return StyleSignals(
        occasion=occasion_key,
        occasion_signals=occ,
        season=(season or "").strip().lower(),
        mood=mood_key,
        mood_signals=mood_sig,
        goal=goal,
        goal_signals=goal_sig,
        favorite_colors=favorite,
        disliked_colors=disliked,
        closet_style_bias=frozenset(closet_bias),
        dna_styles=dna_styles,
        text_style_terms=text_terms,
        palette=palette_name,
        palette_terms=frozenset(PALETTES.get(palette_name, ())),
        brands=brands,
        fits=fits,
        materials=materials,
        dna_occasion=dna_occasion,
        dna_season=dna_season,
        price_upper=price_upper,
        budget=budget,
        weather=weather_signals(weather),
        trending=bool(goal_sig and goal_sig.trending),
    )


# ---------------------------------------------------------------------------
# Matching primitives shared by closet/catalog scoring and Complete the Look
# ---------------------------------------------------------------------------


def color_matches_favorites(color_name: str, signals: StyleSignals) -> bool:
    """True when *color_name* sits in the customer's favourite colours."""
    if not color_name or not signals.favorite_colors:
        return False
    lowered = color_name.lower()
    return any(term in lowered or lowered in term for term in signals.favorite_colors)


def color_matches_disliked(color_name: str, signals: StyleSignals) -> bool:
    """True when *color_name* sits in the customer's disliked colours."""
    if not color_name or not signals.disliked_colors:
        return False
    lowered = color_name.lower()
    return any(term in lowered or lowered in term for term in signals.disliked_colors)


def color_matches_palette(color_name: str, signals: StyleSignals) -> bool:
    """True when *color_name* fits the active palette bias (no bias = neutral pass)."""
    if not color_name:
        return False
    if not signals.palette_terms:
        return True
    lowered = color_name.lower()
    return any(term in lowered for term in signals.palette_terms)


def text_matches_styles(text: str, signals: StyleSignals) -> bool:
    """True when any style term appears in *text* (fit names, tags, category names...)."""
    if not text or not signals.text_style_terms:
        return False
    lowered = text.lower()
    return any(term in lowered for term in signals.text_style_terms)


def parse_budget(value) -> Decimal | None:
    """Parse a caller-supplied budget into a positive ``Decimal`` (``None`` when absent).

    Accepts ``"50"``, ``"50.00"``, ``Decimal(50)`` and ``float``; anything unparsable or
    non-positive reads as "no budget", so a typo never breaks generation.
    """
    if value in (None, ""):
        return None
    try:
        budget = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if budget <= 0:
        return None
    return budget
