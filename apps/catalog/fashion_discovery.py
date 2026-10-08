"""Fashion Discovery 2.0: Mood, Occasion & Curated Wardrobe Discovery (Phase 34).

Deterministic, zero-ML, prefetch-safe.
Connects catalog products, categories, fits, tags, materials, and Flash DNA
into a natural fashion discovery experience.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Case, IntegerField, Q, QuerySet, Value, When
from django.urls import reverse

from apps.styling.models.flash_dna import get_flash_dna

__all__ = [
    "MOOD_CATALOG",
    "OCCASION_CATALOG",
    "filter_by_mood",
    "filter_by_occasion",
    "get_discovery_context",
    "get_mood_by_slug",
    "get_mood_catalog",
    "get_occasion_by_slug",
    "get_occasion_catalog",
    "normalize_mood_slug",
    "normalize_occasion_slug",
]

# =============================================================================
# 1. Controlled Mood & Occasion Catalogs
# =============================================================================

MOOD_CATALOG: dict[str, dict[str, Any]] = {
    "minimal": {
        "slug": "minimal",
        "name": "Minimal",
        "tagline": "Clean lines, pure silhouettes & essential tones",
        "description": (
            "Understated staples, neutral palettes, and refined fits designed "
            "for calm, timeless dressing."
        ),
        "tags": ["minimal", "clean", "basics", "essential", "neutral", "monochrome"],
        "fits": ["regular", "slim"],
        "dna_styles": ["minimal", "classic"],
        "keywords": ["minimal", "essential", "clean", "basic", "pure", "monochrome", "neutral"],
        "accent": "slate",
        "image_hint": "minimalist wardrobe staples in clean monochrome",
    },
    "street": {
        "slug": "street",
        "name": "Street",
        "tagline": "Oversized silhouettes & contemporary urban culture",
        "description": (
            "Heavyweight tees, drop-shoulder hoodies, and cargo layers "
            "with bold streetwear presence."
        ),
        "tags": ["streetwear", "oversized", "graphic", "urban", "modern", "denim", "utility"],
        "fits": ["oversized", "relaxed"],
        "dna_styles": ["streetwear", "oversized"],
        "keywords": [
            "street",
            "streetwear",
            "oversized",
            "hoodie",
            "cargo",
            "graphic",
            "denim",
            "urban",
        ],
        "accent": "indigo",
        "image_hint": "oversized contemporary streetwear silhouette",
    },
    "smart_casual": {
        "slug": "smart_casual",
        "name": "Smart Casual",
        "tagline": "Polished ease from sunrise to dinner",
        "description": (
            "Tailored chinos, structured overshirts, fine knits, and clean collars "
            "that transition effortlessly."
        ),
        "tags": ["smart-casual", "tailored", "workwear", "oxford", "polo", "knitwear", "refined"],
        "fits": ["regular", "slim"],
        "dna_styles": ["smart_casual", "classic"],
        "keywords": [
            "smart",
            "casual",
            "oxford",
            "polo",
            "chino",
            "blazer",
            "tailored",
            "knitwear",
        ],
        "accent": "sky",
        "image_hint": "refined smart casual styling and tailored drape",
    },
    "relaxed": {
        "slug": "relaxed",
        "name": "Relaxed",
        "tagline": "Laid-back comfort & easy drape",
        "description": (
            "Soft washes, breathable loungewear, and roomy fits made for "
            "unwinding without losing form."
        ),
        "tags": ["relaxed", "loungewear", "casual", "comfort", "easy", "chill", "soft"],
        "fits": ["relaxed", "oversized"],
        "dna_styles": ["sporty", "minimal"],
        "keywords": ["relaxed", "chill", "comfort", "lounge", "jogger", "fleece", "soft", "easy"],
        "accent": "amber",
        "image_hint": "effortless relaxed cotton clothing and drape",
    },
    "bold": {
        "slug": "bold",
        "name": "Bold",
        "tagline": "Statement pieces, vibrant colors & expressive cuts",
        "description": (
            "Standout outerwear, vibrant pigments, and high-impact silhouettes "
            "for dressing with confidence."
        ),
        "tags": ["bold", "statement", "vibrant", "pattern", "print", "color", "accent"],
        "fits": [],
        "dna_styles": ["trendy", "streetwear"],
        "keywords": ["bold", "statement", "vibrant", "pattern", "print", "bright", "standout"],
        "accent": "rose",
        "image_hint": "expressive high-contrast statement fashion piece",
    },
    "classic": {
        "slug": "classic",
        "name": "Classic",
        "tagline": "Heritage craftsmanship & enduring style",
        "description": (
            "Pieces that transcend seasonal trends — oxford shirts, trench layers, "
            "and heritage cotton."
        ),
        "tags": ["classic", "timeless", "heritage", "vintage", "traditional"],
        "fits": ["regular"],
        "dna_styles": ["classic", "formal"],
        "keywords": [
            "classic",
            "heritage",
            "timeless",
            "vintage",
            "traditional",
            "cotton",
            "oxford",
        ],
        "accent": "emerald",
        "image_hint": "timeless heritage menswear silhouette",
    },
    "everyday": {
        "slug": "everyday",
        "name": "Everyday",
        "tagline": "Dependable daily rotation & versatile essentials",
        "description": (
            "The foundation of your wardrobe: reliable tees, versatile trousers, "
            "and go-to daily outerwear."
        ),
        "tags": ["everyday", "casual", "essential", "basics", "daily"],
        "fits": ["regular", "relaxed"],
        "dna_styles": ["minimal", "sporty"],
        "keywords": ["everyday", "daily", "essential", "basics", "casual", "tee"],
        "accent": "teal",
        "image_hint": "versatile everyday apparel foundation",
    },
    "night_out": {
        "slug": "night_out",
        "name": "Night Out",
        "tagline": "After-dark refinement, sleek tones & sharp cuts",
        "description": (
            "Deeper palettes, elevated fabrics, and evening-ready tailoring "
            "for dinners, dates, and celebrations."
        ),
        "tags": ["party", "evening", "night-out", "refined", "sleek", "date-night", "black"],
        "fits": ["slim", "regular"],
        "dna_styles": ["formal", "smart_casual"],
        "keywords": ["evening", "night", "party", "sleek", "black", "dress", "dark", "tailored"],
        "accent": "purple",
        "image_hint": "sleek monochromatic evening wear piece",
    },
}

OCCASION_CATALOG: dict[str, dict[str, Any]] = {
    "everyday": {
        "slug": "everyday",
        "name": "Everyday",
        "tagline": "Effortless pieces for whatever the day holds",
        "description": (
            "Daily staples engineered for all-day comfort, errand runs, and casual coffee walks."
        ),
        "tags": ["everyday", "casual", "essential", "basics"],
        "fits": ["regular", "relaxed"],
        "dna_occasion": "casual",
        "keywords": ["everyday", "daily", "staple", "basics", "essential"],
        "icon": "sparkles",
    },
    "work": {
        "slug": "work",
        "name": "Work & Office",
        "tagline": "Desk to meeting rooms with crisp confidence",
        "description": (
            "Polished button-downs, refined trousers, structured overshirts, "
            "and understated tailoring."
        ),
        "tags": ["work", "office", "tailored", "smart-casual", "formal", "workwear"],
        "fits": ["regular", "slim"],
        "dna_occasion": "office",
        "keywords": ["workwear", "office", "trouser", "blazer", "tailored", "oxford"],
        "icon": "briefcase",
    },
    "university": {
        "slug": "university",
        "name": "University & Campus",
        "tagline": "Smart casual campus style & comfortable utility",
        "description": (
            "Durable denim, layered sweatshirts, graphic tees, and utility "
            "overshirts for campus life."
        ),
        "tags": ["campus", "university", "casual", "denim", "streetwear", "utility"],
        "fits": ["relaxed", "oversized", "regular"],
        "dna_occasion": "university",
        "keywords": ["university", "campus", "skate", "backpack", "denim"],
        "icon": "academic-cap",
    },
    "date_night": {
        "slug": "date_night",
        "name": "Date Night",
        "tagline": "Considered, refined & subtly memorable",
        "description": (
            "Thoughtfully put-together shirts, sleek jackets, and well-fitted dark bottoms."
        ),
        "tags": ["date-night", "date", "refined", "romantic", "smart-casual"],
        "fits": ["slim", "regular"],
        "dna_occasion": "date",
        "keywords": ["romantic", "dinner", "cocktail", "date night"],
        "icon": "heart",
    },
    "party": {
        "slug": "party",
        "name": "Party & Going Out",
        "tagline": "High-energy statements & evening flair",
        "description": (
            "Statement textures, standout outerwear, and celebratory looks ready for the night."
        ),
        "tags": ["party", "evening", "celebration", "statement", "night-out"],
        "fits": [],
        "dna_occasion": "party",
        "keywords": ["party", "celebration", "sequin", "statement", "glam"],
        "icon": "musical-note",
    },
    "travel": {
        "slug": "travel",
        "name": "Travel & Transit",
        "tagline": "Lightweight, crease-resistant & comfortable on the move",
        "description": (
            "Modular layering, breathable textiles, zippered storage, "
            "and relaxed travel silhouettes."
        ),
        "tags": ["travel", "comfortable", "utility", "layers", "relaxed"],
        "fits": ["relaxed", "regular"],
        "dna_occasion": "travel",
        "keywords": ["travel", "commute", "packable", "transit"],
        "icon": "paper-airplane",
    },
    "weekend": {
        "slug": "weekend",
        "name": "Weekend & Leisure",
        "tagline": "Off-duty style for relaxed escapes and brunch",
        "description": (
            "Casual linen blends, easy-fitting shorts and trousers, and effortless overshirts."
        ),
        "tags": ["weekend", "relaxed", "casual", "comfort", "outdoor"],
        "fits": ["relaxed", "regular"],
        "dna_occasion": "casual",
        "keywords": ["weekend", "brunch", "resort", "vacation"],
        "icon": "sun",
    },
    "formal": {
        "slug": "formal",
        "name": "Formal & Events",
        "tagline": "Impeccable tailoring for life's biggest milestones",
        "description": (
            "Crisp formal wear, bespoke silhouettes, and premium fabrics tailored to perfection."
        ),
        "tags": ["formal", "evening", "ceremony", "wedding", "tailored"],
        "fits": ["slim", "regular"],
        "dna_occasion": "formal",
        "keywords": ["formal", "wedding", "ceremony", "suit", "tuxedo", "black tie"],
        "icon": "badge-check",
    },
}

MOOD_ALIASES: dict[str, str] = {
    "streetwear": "street",
    "chill": "relaxed",
    "smart-casual": "smart_casual",
    "smart": "smart_casual",
    "night-out": "night_out",
    "nightout": "night_out",
    "evening": "night_out",
    "clean": "minimal",
}

OCCASION_ALIASES: dict[str, str] = {
    "casual": "everyday",
    "office": "work",
    "campus": "university",
    "date": "date_night",
    "date-night": "date_night",
    "datenight": "date_night",
    "night-out": "party",
    "nightout": "party",
    "leisure": "weekend",
}


def normalize_mood_slug(raw_slug: str | None) -> str:
    """Normalize a mood slug or alias to canonical catalog slug."""
    if not raw_slug:
        return ""
    slug = str(raw_slug).strip().lower().replace("-", "_")
    if slug in MOOD_CATALOG:
        return slug
    # Check aliases
    alias = str(raw_slug).strip().lower()
    if alias in MOOD_ALIASES:
        return MOOD_ALIASES[alias]
    if slug in MOOD_ALIASES:
        return MOOD_ALIASES[slug]
    return ""


def normalize_occasion_slug(raw_slug: str | None) -> str:
    """Normalize an occasion slug or alias to canonical catalog slug."""
    if not raw_slug:
        return ""
    slug = str(raw_slug).strip().lower().replace("-", "_")
    if slug in OCCASION_CATALOG:
        return slug
    # Check aliases
    alias = str(raw_slug).strip().lower()
    if alias in OCCASION_ALIASES:
        return OCCASION_ALIASES[alias]
    if slug in OCCASION_ALIASES:
        return OCCASION_ALIASES[slug]
    return ""


def get_mood_by_slug(slug: str | None) -> dict[str, Any] | None:
    """Retrieve mood definition dictionary or None."""
    canon = normalize_mood_slug(slug)
    return MOOD_CATALOG.get(canon)


def get_occasion_by_slug(slug: str | None) -> dict[str, Any] | None:
    """Retrieve occasion definition dictionary or None."""
    canon = normalize_occasion_slug(slug)
    return OCCASION_CATALOG.get(canon)


def get_mood_catalog() -> list[dict[str, Any]]:
    """Return all moods formatted for presentation cards and navigation."""
    items = []
    for slug, data in MOOD_CATALOG.items():
        item = dict(data)
        item["url"] = f"{reverse('catalog:product-list')}?mood={slug}"
        item["discovery_url"] = f"{reverse('catalog:discovery-hub')}?mood={slug}"
        items.append(item)
    return items


def get_occasion_catalog() -> list[dict[str, Any]]:
    """Return all occasions formatted for presentation chips and navigation."""
    items = []
    for slug, data in OCCASION_CATALOG.items():
        item = dict(data)
        item["url"] = f"{reverse('catalog:product-list')}?occasion={slug}"
        item["discovery_url"] = f"{reverse('catalog:discovery-hub')}?occasion={slug}"
        items.append(item)
    return items


# =============================================================================
# 2. Query Filtering & Deterministic Personalization
# =============================================================================


def filter_by_mood(queryset: QuerySet, mood_slug: str, *, user: Any = None) -> QuerySet:
    """Filter product queryset by mood and apply deterministic Flash DNA personalization."""
    canon = normalize_mood_slug(mood_slug)
    if not canon or canon not in MOOD_CATALOG:
        return queryset

    definition = MOOD_CATALOG[canon]
    match_q = Q()

    # Match active tags
    if definition.get("tags"):
        match_q |= Q(tags__slug__in=definition["tags"], tags__is_active=True)

    # Match active fits
    if definition.get("fits"):
        match_q |= Q(fit__slug__in=definition["fits"], fit__is_active=True)

    # Match keywords in product title, short_description or category name
    for kw in definition.get("keywords", []):
        match_q |= (
            Q(name__icontains=kw)
            | Q(short_description__icontains=kw)
            | Q(category__name__icontains=kw)
            | Q(category__slug__icontains=kw)
        )

    filtered = queryset.filter(match_q).distinct()

    # Flash DNA Personalization (Truthful & Deterministic — never fabricated)
    dna = get_flash_dna(user) if user and getattr(user, "is_authenticated", False) else None
    if dna:
        score_cases = []
        # Fit synergy (preferred_fits is ManyToMany)
        fav_fit_ids = list(dna.preferred_fits.values_list("id", flat=True))
        if fav_fit_ids:
            score_cases.append(When(fit_id__in=fav_fit_ids, then=Value(5)))

        # Color synergy (favorite_colors is ManyToMany)
        fav_color_ids = list(dna.favorite_colors.values_list("id", flat=True))
        if fav_color_ids:
            score_cases.append(When(variants__color_id__in=fav_color_ids, then=Value(4)))

        # Preferred styles
        if getattr(dna, "styles", None):
            user_styles = [s.strip().lower() for s in dna.styles.split(",") if s.strip()]
            for s in user_styles:
                score_cases.append(When(tags__slug__icontains=s, then=Value(3)))

        if score_cases:
            filtered = filtered.annotate(
                dna_match_score=Case(*score_cases, default=Value(0), output_field=IntegerField())
            ).order_by("-dna_match_score", "-is_featured", "-published_at", "-id")

    return filtered


def filter_by_occasion(queryset: QuerySet, occasion_slug: str, *, user: Any = None) -> QuerySet:
    """Filter product queryset by occasion and apply deterministic Flash DNA personalization."""
    canon = normalize_occasion_slug(occasion_slug)
    if not canon or canon not in OCCASION_CATALOG:
        return queryset

    definition = OCCASION_CATALOG[canon]
    match_q = Q()

    # Match active tags
    if definition.get("tags"):
        match_q |= Q(tags__slug__in=definition["tags"], tags__is_active=True)

    # Match active fits
    if definition.get("fits"):
        match_q |= Q(fit__slug__in=definition["fits"], fit__is_active=True)

    # Match keywords in title, description, or category
    for kw in definition.get("keywords", []):
        match_q |= (
            Q(name__icontains=kw)
            | Q(short_description__icontains=kw)
            | Q(category__name__icontains=kw)
            | Q(category__slug__icontains=kw)
        )

    filtered = queryset.filter(match_q).distinct()

    # Flash DNA Personalization (Truthful & Deterministic — never fabricated)
    dna = get_flash_dna(user) if user and getattr(user, "is_authenticated", False) else None
    if dna:
        score_cases = []
        dna_occ = definition.get("dna_occasion", "")
        if dna_occ and getattr(dna, "occasions", None):
            user_occs = [o.strip().lower() for o in dna.occasions.split(",") if o.strip()]
            if dna_occ in user_occs:
                score_cases.append(When(pk__isnull=False, then=Value(6)))

        fav_fit_ids = list(dna.preferred_fits.values_list("id", flat=True))
        if fav_fit_ids:
            score_cases.append(When(fit_id__in=fav_fit_ids, then=Value(4)))

        fav_color_ids = list(dna.favorite_colors.values_list("id", flat=True))
        if fav_color_ids:
            score_cases.append(When(variants__color_id__in=fav_color_ids, then=Value(3)))

        if score_cases:
            filtered = filtered.annotate(
                dna_match_score=Case(*score_cases, default=Value(0), output_field=IntegerField())
            ).order_by("-dna_match_score", "-is_featured", "-published_at", "-id")

    return filtered


def get_discovery_context(
    request: Any, mood_slug: str | None = None, occasion_slug: str | None = None
) -> dict[str, Any]:
    """Assemble discovery hub context including moods, occasions, and active filters."""
    moods = get_mood_catalog()
    occasions = get_occasion_catalog()

    active_mood = get_mood_by_slug(mood_slug)
    active_occasion = get_occasion_by_slug(occasion_slug)

    user = getattr(request, "user", None)
    dna = get_flash_dna(user) if user and getattr(user, "is_authenticated", False) else None

    return {
        "moods": moods,
        "occasions": occasions,
        "active_mood": active_mood,
        "active_occasion": active_occasion,
        "has_personalization": bool(dna),
        "user_style_goal": getattr(dna, "style_goal", "") if dna else "",
    }
