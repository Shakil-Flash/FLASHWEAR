"""Recommendation signal extraction and candidate generation (Phase 10).

This module owns the "downstream" side of the recommendation pipeline:
* extracting structured signals from FLASH DNA, closet, outfits, orders, reviews
* generating candidate product sets from the catalog
* applying visibility and inventory filters
"""

from __future__ import annotations

import logging
from collections import defaultdict

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.db.models import Q

from apps.catalog.models import Product
from apps.closet.models.items import ClosetItem
from apps.closet.models.outfits import Outfit
from apps.orders.models import Order, OrderItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Central configurable signal weights
# ---------------------------------------------------------------------------

# Default weights — can be overridden per-signal-type via
# ``RECOMMENDATION_SIGNAL_WEIGHTS`` in Django settings.
# The keys match ``RecommendationSignal.SIGNAL_CHOICES`` entries.
DEFAULT_SIGNAL_WEIGHTS: dict[str, float] = {
    # DNA signals
    "dna_style": 2.0,
    "dna_color": 2.0,
    "dna_category": 2.0,
    "dna_fit": 1.5,
    "dna_material": 1.5,
    "dna_occasion": 1.0,
    "dna_season": 1.0,
    "dna_brand": 1.5,
    "dna_price": 1.0,
    # Closet & outfit signals
    "closet_complement": 2.5,
    "outfit_completion": 2.0,
    "purchase_history": 2.0,
    "review_preference": 1.5,
    # Popularity / recency
    "popularity": 1.0,
    "recency": 1.0,
    # Category / brand / color matching
    "category_match": 1.5,
    "brand_match": 1.5,
    "color_match": 1.5,
    "style_match": 1.5,
}


def get_signal_weights() -> dict[str, float]:
    """Return the active signal weight mapping.

    Merges ``DEFAULT_SIGNAL_WEIGHTS`` with any overrides from
    ``settings.RECOMMENDATION_SIGNAL_WEIGHTS``.
    """
    weights: dict[str, float] = dict(DEFAULT_SIGNAL_WEIGHTS)
    overrides = getattr(settings, "RECOMMENDATION_SIGNAL_WEIGHTS", {})
    for key, value in overrides.items():
        if key in weights:
            weights[key] = value
        else:
            logger.warning("Unknown signal weight key: %s", key)
    return weights


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------


def extract_dna_signals(user) -> dict[str, list[str] | None]:
    """Extract FLASH DNA preference lists for the given user.

    Returns a dict mapping signal type to a list of matching values, or None
    when the user has no FLASH DNA profile.
    """
    from apps.styling.services.dna import (
        dna_category_list,
        dna_color_list,
        dna_fit_list,
        dna_material_list,
        dna_occasion_list,
        dna_season_list,
        dna_style_fn,
    )

    try:
        dna = user.flash_dna
    except ObjectDoesNotExist:
        return None

    styles_val = dna_style_fn(dna)
    return {
        "styles": styles_val,
        "colors": dna_color_list(dna, preferred=True),
        "disliked_colors": dna_color_list(dna, preferred=False),
        "categories": dna_category_list(dna),
        "fits": dna_fit_list(styles_val),
        "materials": dna_material_list([]),  # placeholder - no material data from DNA yet
        "occasions": dna_occasion_list(dna),
        "seasons": dna_season_list(dna),
        "brands": [b.name for b in dna.preferred_brands.all()],
        "price_range": dna.preferred_price_range,
    }


def dna_fit_list(dna_styles: list[str]) -> list[str]:
    """Infer preferred fits from DNA style keywords.

    Accepts a pre-extracted list of style strings instead of a DNA object,
    avoiding circular import issues with dna_style_fn.
    """
    inferred = []
    for s in dna_styles:
        if "oversized" in s.lower():
            inferred.append("Oversized")
        elif "slim" in s.lower():
            inferred.append("Slim")
        elif "relaxed" in s.lower():
            inferred.append("Relaxed")
        elif "athletic" in s.lower():
            inferred.append("Athletic")
    return inferred


def dna_material_list(dna_styles: list[str]) -> list[str]:
    """Return the customer's preferred material names as a plain list.

    Accepts a pre-extracted list of style strings to avoid circular import
    issues with the FlashDNA model import.
    """
    # Placeholder — DNA has no material field yet; return empty list.
    # Future: store Material rows and return their names.
    return []


def extract_closet_signals(user) -> dict[str, list[str] | None]:
    """Extract signals from the user's active closet.

    Returns a dict or None when the user has no closet items.
    """
    try:
        items = user.closet_items.filter(status=ClosetItem.Status.ACTIVE)
    except ObjectDoesNotExist:
        return None

    if not items.exists():
        return None

    categories = [item.category for item in items if item.category]
    colors = [item.color or "" for item in items if item.color]
    styles = [item.style or "" for item in items if item.style]

    return {
        "closet_complement": None,  # computed later per-candidate
        "category_match": categories or None,
        "color_match": colors or None,
        "style_match": styles or None,
    }


def extract_outfit_signals(user) -> dict[str, list[str] | None]:
    """Extract signals from the user's saved outfits.

    Returns a dict or None when the user has no saved outfits.
    """
    try:
        outfits = user.outfits.filter(status=Outfit.Status.SAVED)[:10]
    except ObjectDoesNotExist:
        return None

    if not outfits.exists():
        return None

    # Collect categories, colors, styles from outfit items
    categories: set[str] = set()
    colors: set[str] = set()
    styles: set[str] = set()

    for outfit in outfits:
        for item in outfit.items.all()[:5]:
            ci = item.closet_item
            if ci.category:
                categories.add(ci.category)
            if ci.color:
                colors.add(ci.color)
            if ci.style:
                styles.add(ci.style)

    return {
        "outfit_completion": None,  # computed per-candidate
        "category_match": list(categories) or None,
        "color_match": list(colors) or None,
        "style_match": list(styles) or None,
    }


def extract_purchase_signals(user) -> dict[str, list[str] | None]:
    """Extract signals from the user's purchase history.

    Returns a dict or None when the user has no purchases.
    """
    try:
        order_items = OrderItem.objects.filter(
            order__user=user, order__status=Order.Status.DELIVERED
        ).select_related("variant", "variant__product", "variant__color", "variant__size")
    except ObjectDoesNotExist:
        return None

    if not order_items.exists():
        return None

    categories: set[str] = set()
    colors: set[str] = set()
    styles: set[str] = set()
    brands: set[str] = set()

    for item in order_items:
        variant = item.variant
        product = variant.product if variant else None
        if product:
            if product.category:
                categories.add(product.category)
            if product.brand:
                brands.add(product.brand)
        if variant:
            if variant.color:
                colors.add(variant.color.name)
            if variant.style:
                styles.add(variant.style)

    return {
        "purchase_history": None,  # computed later; just indicates presence
        "category_match": list(categories) or None,
        "color_match": list(colors) or None,
        "style_match": list(styles) or None,
        "brand_match": list(brands) or None,
    }


def extract_review_signals(user) -> dict[str, list[str] | None]:
    """Extract signals from the user's review preferences.

    Returns a dict or None when the user has no reviews.
    """
    from apps.engagement.models import Review

    try:
        reviews = Review.objects.filter(author=user, status=Review.Status.PUBLISHED)
    except ObjectDoesNotExist:
        return None

    if not reviews.exists():
        return None

    # Collect categories, colors, styles from reviewed products
    categories: set[str] = set()
    _colors: set[str] = set()
    _styles: set[str] = set()

    for review in reviews:
        product = review.product
        if product:
            if product.category:
                categories.add(product.category)
            if product.brand:
                pass  # brand captured separately if needed
            # Note: reviews don't directly store color/style, we infer from product

    return {
        "review_preference": None,  # presence flag; specifics computed per-candidate
        "category_match": list(categories) or None,
    }


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------


def _build_base_queryset(user) -> models.QuerySet[Product]:
    """Build a base product queryset obeying all catalog visibility rules.

    This is the foundation every candidate set starts from. It mirrors
    ``ProductQuerySet.published()`` but returns a fresh queryset so callers
    can add their own filters.
    """
    from apps.catalog.models import Product as CatProduct

    return (
        CatProduct.objects.filter(
            status=CatProduct.Status.ACTIVE,
        )
        .filter(
            Q(published_at__lte=None) | Q(published_at__lte=models.functions.Now()),
        )
        .filter(models.Q(category__is_active=True) | models.Q(category__isnull=True))
        .filter(models.Q(brand__is_active=True) | models.Q(brand__isnull=True))
    )


def _apply_dna_filters(
    queryset: models.QuerySet[Product],
    signals: dict[str, list[str] | bool | None],
) -> models.QuerySet[Product]:
    """Apply FLASH DNA-based filters to a product queryset.

    DNA signals that can narrow the candidate set:
    * style match
    * color match (favorite or disliked)
    * category match
    * fit match
    * material match
    * occasion match
    * season match
    * brand match
    * price range match
    """
    dna_styles = signals.get("dna_styles")
    _dna_colors = signals.get("dna_colors")
    _dna_disliked_colors = signals.get("dna_disliked_colors")
    _dna_categories = signals.get("dna_categories")
    _dna_fits = signals.get("dna_fits")
    _dna_materials = signals.get("dna_materials")
    _dna_occasions = signals.get("dna_occasions")
    _dna_seasons = signals.get("dna_seasons")
    _dna_brands = signals.get("dna_brands")
    _dna_price_range = signals.get("dna_price_range")

    if dna_styles:
        queryset = queryset.filter(
            Q(styles__contains=[s for s in dna_styles if s])
            | Q(...)  # style keywords will be handled via catalog attributes
        )

    # ... additional DNA filter logic

    return queryset


def _apply_closet_filters(
    queryset: models.QuerySet[Product],
    signals: dict[str, list[str] | None],
    user_closet: list[dict],
) -> models.QuerySet[Product]:
    """Apply closet-complement filters to narrow/broaden candidates.

    The closet complement signal gives higher scores to products that
    complement what the user already owns, but we still keep a broad
    candidate set so diversity is possible.
    """
    # We do NOT heavily filter here — the scoring engine handles complement
    # via weighted signals. We only exclude categories the user actively
    # avoids via disliked colors, which is already handled upstream.
    return queryset


def _apply_purchase_filters(
    queryset: models.QuerySet[Product],
    signals: dict[str, list[str] | None],
) -> models.QuerySet[Product]:
    """Apply purchase-history-based filtering.

    Prevents recommending products the user already owns (by category/brand),
    but does not eliminate the entire catalog — the scoring engine penalizes
    owned items rather than excluding them outright.
    """
    # Light touch: we only add a mild penalty in scoring, not hard filters.
    return queryset


def generate_candidates(
    user, context: str = "personalized", max_candidates: int = 200
) -> models.QuerySet[Product]:
    """Generate a candidate product set for the given user and context.

    The pipeline:
    1. Start from the full visible catalog (obeying visibility rules).
    2. Extract signals from FLASH DNA, closet, outfits, purchases, reviews.
    3. Apply lightweight filters (visibility only — no heavy exclusion).
    4. Return the candidate queryset, limited for efficiency.

    The caller (scoring engine) is responsible for further narrowing/diversifying
    via the score, not by pre-filtering the candidate set.
    """
    # 1. Base catalog
    qs = _build_base_queryset(user)

    # 2. Extract signals
    _dna_signals = extract_dna_signals(user) or {}
    _closet_signals = extract_closet_signals(user) or {}
    _outfit_signals = extract_outfit_signals(user) or {}
    _purchase_signals = extract_purchase_signals(user) or {}
    _review_signals = extract_review_signals(user) or {}

    # 3. Lightweight filtering — visibility only
    # (Heavy exclusion is the scoring engine's job, not the candidate generator's)

    # 4. Cap for efficiency
    qs = qs[: max_candidates * 2]  # oversample slightly for diversity

    return qs


# ---------------------------------------------------------------------------
# Scoring engine
# -------------------------------------------------------------------------__


def score_product(
    product: Product, user, dna_signals: dict | None, context_scores: dict | None = None
) -> dict:
    """Score a single product for the given user.

    Returns a dict with a structured score breakdown so the output is
    explainable. The engine is deterministic given identical inputs.

    The score is a weighted sum of signal matches. No ML, no black-box
    scoring — every contributor is explicit and inspectable.
    """
    if context_scores is None:
        context_scores = {}

    weights = get_signal_weights()

    # Initialize all signal contributions to 0
    contributions: dict[str, float] = {
        "dna_style": 0.0,
        "dna_color": 0.0,
        "dna_category": 0.0,
        "dna_fit": 0.0,
        "dna_material": 0.0,
        "dna_occasion": 0.0,
        "dna_season": 0.0,
        "dna_brand": 0.0,
        "dna_price": 0.0,
        "closet_complement": 0.0,
        "outfit_completion": 0.0,
        "purchase_history": 0.0,
        "review_preference": 0.0,
        "popularity": 0.0,
        "recency": 0.0,
        "category_match": 0.0,
        "brand_match": 0.0,
        "color_match": 0.0,
        "style_match": 0.0,
    }

    total = 0.0

    # ---- DNA signals -------------------------------------------------------

    if dna_signals:
        styles = dna_signals.get("styles") or []
        colors = dna_signals.get("colors") or []
        disliked_colors = dna_signals.get("disliked_colors") or []
        categories = dna_signals.get("categories") or []
        fits = dna_signals.get("fits") or []
        materials = dna_signals.get("materials") or []
        occasions = dna_signals.get("occasions") or []
        seasons = dna_signals.get("seasons") or []
        brands = dna_signals.get("brands") or []
        price_range = dna_signals.get("price_range")

        # Style match
        if styles and product.styles:
            style_matches = sum(1 for s in styles if s in (product.styles or "").split(","))
            if style_matches:
                contributions["dna_style"] = weights.get("dna_style", 2.0) * (
                    style_matches / max(len(styles), 1)
                )

        # Color match (favorites)
        if colors and product.color:
            color_matches = sum(1 for c in colors if (product.color or "").lower() == c.lower())
            if color_matches:
                contributions["dna_color"] = weights.get("dna_color", 2.0) * (
                    color_matches / max(len(colors), 1)
                )

        # Disliked colors penalize
        if disliked_colors and product.color:
            dc_matches = sum(
                1 for c in disliked_colors if (product.color or "").lower() == c.lower()
            )
            if dc_matches:
                contributions["dna_color"] -= (
                    weights.get("dna_color", 2.0)
                    * (dc_matches / max(len(disliked_colors), 1))
                    * 0.5
                )

        # Category match
        if categories and product.category:
            cat_matches = sum(
                1 for c in categories if (product.category or "").lower() == c.lower()
            )
            if cat_matches:
                contributions["dna_category"] = weights.get("dna_category", 2.0) * (
                    cat_matches / max(len(categories), 1)
                )

        # Fit match
        if fits and product.fit:
            fit_matches = sum(1 for f in fits if (product.fit or "").lower() == f.lower())
            if fit_matches:
                contributions["dna_fit"] = weights.get("dna_fit", 1.5) * (
                    fit_matches / max(len(fits), 1)
                )

        # Material match
        if materials and product.materials:
            mat_matches = sum(
                1
                for m in materials
                if any(m.lower() in mat.name.lower() for mat in product.materials.all())
            )
            if mat_matches:
                contributions["dna_material"] = weights.get("dna_material", 1.5) * (
                    mat_matches / max(len(materials), 1)
                )

        # Occasion match
        if occasions and product.occasion:
            occ_matches = sum(1 for o in occasions if (product.occasion or "").lower() == o.lower())
            if occ_matches:
                contributions["dna_occasion"] = weights.get("dna_occasion", 1.0) * (
                    occ_matches / max(len(occasions), 1)
                )

        # Season match
        if seasons and product.season:
            sea_matches = sum(1 for s in seasons if (product.season or "").lower() == s.lower())
            if sea_matches:
                contributions["dna_season"] = weights.get("dna_season", 1.0) * (
                    sea_matches / max(len(seasons), 1)
                )

        # Brand match
        if brands and product.brand:
            brand_matches = sum(1 for b in brands if (product.brand or "").lower() == b.lower())
            if brand_matches:
                contributions["dna_brand"] = weights.get("dna_brand", 1.5) * (
                    brand_matches / max(len(brands), 1)
                )

        # Price range match
        if price_range:
            try:
                price_val = float(product.price or 0)
                pr_map = {
                    "under_25": price_val < 25,
                    "under_50": price_val < 50,
                    "under_100": price_val < 100,
                    "under_150": price_val < 150,
                    "above_150": price_val >= 150,
                }
                if pr_map.get(price_range):
                    contributions["dna_price"] = weights.get("dna_price", 1.0)
            except (ValueError, TypeError):
                pass

    # ---- Closet complement -------------------------------------------------

    # A product gets a complement score if it's a different category/role
    # from what the user already owns, but belongs to the same broad family.
    # Example: user has black jeans → recommend overshirts, not more jeans.
    closet_items = user.closet_items.filter(status=ClosetItem.Status.ACTIVE)
    if closet_items.exists():
        user_categories = {str(ci.category) for ci in closet_items if ci.category}
        product_cat = product.category or ""
        if product_cat and user_categories:
            # If the product's category is different from what the user owns,
            # give a modest complement boost.
            if product_cat not in user_categories:
                contributions["closet_complement"] = weights.get("closet_complement", 2.5) * 0.5
            else:
                # Same category: small penalty to encourage diversity
                contributions["closet_complement"] = weights.get("closet_complement", 2.5) * -0.2

    # ---- Outfit completion ------------------------------------------------

    # If the user has saved outfits, check if this product completes one.
    from apps.orders.models import OrderItem

    outfit_items = user.outfits.filter(status="SAVED").prefetch_related("items__closet_item")[:5]
    if outfit_items.exists():
        # Check if product matches any outfit's dominant category/color
        for outfit in outfit_items:
            outfit_cats = {
                str(ci.closet_item.category)
                for ci in outfit.items.all()[:5]
                if ci.closet_item.category
            }
            product_cat = product.category or ""
            if product_cat in outfit_cats:
                contributions["outfit_completion"] = weights.get("outfit_completion", 2.0) * 0.5
                break  # first match wins

    # ---- Purchase history -------------------------------------------------

    # Mild penalty for products matching already-purchased categories/brands
    # (not an exclusion — the user may want more, just not the same thing).
    try:
        purchased_categories = (
            OrderItem.objects.filter(order__user=user, order__status="DELIVERED")
            .values_list("variant__product__category", flat=True)
            .distinct()
        )
        purchased_brands = (
            OrderItem.objects.filter(order__user=user, order__status="DELIVERED")
            .values_list("variant__product__brand__name", flat=True)
            .distinct()
        )

        product_cat = product.category or ""
        product_brand = product.brand or ""

        if product_cat and product_cat in purchased_categories:
            contributions["purchase_history"] = weights.get("purchase_history", 2.0) * -0.3
        if product_brand and product_brand in purchased_brands:
            contributions["purchase_history"] += weights.get("purchase_history", 2.0) * -0.15
    except Exception:
        pass  # best-effort; never break the scoring engine

    # ---- Review preference ------------------------------------------------

    # Products matching reviewed categories get a small boost.
    try:
        from apps.engagement.models import Review as EngagementReview

        reviewed_cats = (
            EngagementReview.objects.filter(author=user, status="PUBLISHED")
            .values_list("product__category", flat=True)
            .distinct()
        )
        product_cat = product.category or ""
        if product_cat and product_cat in reviewed_cats:
            contributions["review_preference"] = weights.get("review_preference", 1.5) * 0.3
    except Exception:
        pass

    # ---- Popularity -------------------------------------------------------

    # Simple popularity based on catalog ordering or review count.
    # We use the existing catalog sort/facet infrastructure.
    try:
        from apps.catalog.services import discovery

        # Get facet count for this product's categories as a popularity proxy
        facet_counts = discovery.get_facet_counts(user=user if user.is_authenticated else None)
        cat_name = product.category or ""
        if cat_name and cat_name in facet_counts.get("categories", {}):
            pop_raw = facet_counts["categories"][cat_name]
            # Normalize to 0-1 range roughly
            contributions["popularity"] = min(float(pop_raw) / 100.0, 1.0) if pop_raw else 0.0
        else:
            contributions["popularity"] = 0.5  # neutral default
    except Exception:
        contributions["popularity"] = 0.5

    # ---- Recency ----------------------------------------------------------

    # Products with newer published_at get a small boost.
    try:
        now = models.functions.Now()
        days_old = (
            (now - models.F("published_at")).total_seconds() / 86400
            if product.published_at
            else 999
        )
        if days_old <= 90:
            contributions["recency"] = weights.get("recency", 1.0) * (
                1.0 - min(days_old / 90.0, 1.0)
            )
        elif days_old <= 365:
            contributions["recency"] = weights.get("recency", 1.0) * (
                0.5 - min((days_old - 90) / 275.0, 0.5)
            )
        else:
            contributions["recency"] = weights.get("recency", 1.0) * 0.0
    except Exception:
        contributions["recency"] = 0.0

    # ---- Category match (explicit signal) --------------------------------

    # This is a separate signal from DNA category — it's a direct category
    # filter match used by the recommendation UI.
    # (Handled separately from dna_category above for UI transparency.)
    if context_scores and "category_match" in context_scores:
        contributions["category_match"] = context_scores["category_match"]

    # ---- Brand match (explicit signal) -----------------------------------

    if context_scores and "brand_match" in context_scores:
        contributions["brand_match"] = context_scores["brand_match"]

    # ---- Color match (explicit signal) ------------------------------------

    if context_scores and "color_match" in context_scores:
        contributions["color_match"] = context_scores["color_match"]

    # ---- Style match (explicit signal) ------------------------------------

    if context_scores and "style_match" in context_scores:
        contributions["style_match"] = context_scores["style_match"]

    # ---- Compute total ----------------------------------------------------

    total = sum(contributions.values())

    # Normalize to a rough 0-1 confidence scale for UI display
    # (optional; the raw total is also preserved)
    confidence = max(0.0, min(1.0, total / 10.0))  # rough scaling

    return {
        "total_raw": round(total, 4),
        "total_final": round(total, 4),
        "contributions": contributions,
        "confidence": round(confidence, 3),
    }


# ---------------------------------------------------------------------------
# Diversity post-processing
# ---------------------------------------------------------------------------


def apply_diversity_constraints(
    scored: list[dict],
    max_per_category: int = 2,
    max_per_brand: int = 2,
    max_per_color: int = 2,
) -> list[dict]:
    """Apply deterministic diversity constraints to a list of scored products.

    Constraints are applied in score order (highest first). When a product
    would violate a constraint, it is skipped (not re-scored); the next
    product in score order is considered instead.

    Constraints:
    * At most ``max_per_category`` products from the same category.
    * At most ``max_per_brand`` products from the same brand.
    * At most ``max_per_color`` products sharing the same primary color.

    The input list is not modified; a new list is returned.
    """
    result: list[dict] = []
    category_counts: dict[str, int] = defaultdict(int)
    brand_counts: dict[str, int] = defaultdict(int)
    color_counts: dict[str, int] = defaultdict(int)

    for entry in scored:
        product_info = entry.get("product_info", {})
        if not product_info:
            continue

        cat = product_info.get("category") or "uncategorized"
        brand = product_info.get("brand") or "unknown"
        primary_color = product_info.get("primary_color") or "unknown"

        # Check constraints
        cat_ok = category_counts[cat] < max_per_category
        brand_ok = brand_counts[brand] < max_per_brand
        color_ok = color_counts[primary_color] < max_per_color

        if cat_ok and brand_ok and color_ok:
            # Apply
            result.append(entry)
            category_counts[cat] += 1
            brand_counts[brand] += 1
            color_counts[primary_color] += 1
        # else: skip — next product in score order will be considered

    return result


# ---------------------------------------------------------------------------
# Cold-start handling
# ---------------------------------------------------------------------------


def cold_start_recommendations(
    user, context: str = "personalized", max_results: int = 10
) -> list[dict]:
    """Generate recommendations when the user has no personalization data.

    Fallback strategy (in order of priority):
    1. Popular products across the catalog (with availability).
    2. Recently published products.
    3. Products with high review scores / ratings.
    4. A diverse mix across categories/brands.

    The engine still runs in full — it just replaces DNA/closet/purchase
    signals with catalog-level defaults.
    """

    # Start from the full visible catalog
    qs = _build_base_queryset(user)

    # Sort by popularity (review count / rating) then recency
    # For now, just order by published_at descending as a reasonable default
    qs = qs.order_by("-published_at", "-id")[: max_results * 2]

    # Score each product with neutral (non-personalized) weights
    results: list[dict] = []
    for product in qs:
        score = score_product(product, user, dna_signals=None)
        results.append(
            {
                "product": product,
                "score": score,
                "source": "cold_start",
            }
        )

    # Sort by score total (highest first), cap results
    results.sort(key=lambda r: r["score"]["total_raw"], reverse=True)
    return results[:max_results]


# ---------------------------------------------------------------------------
# Main recommendation entry point
# ---------------------------------------------------------------------------


def get_recommendations(
    user,
    context: str = "personalized",
    max_results: int = 10,
    diversity: bool = True,
    request_path: str = "",
) -> dict:
    """Entry point: generate grounded recommendations for a user.

    Returns a dict with the same shape the stylist service uses, so the UI
    never needs to branch on the provider kind.

    Pipeline:
    1. Generate candidate products.
    2. Score each candidate.
    3. Apply diversity constraints (if enabled).
    4. Build explanation strings.
    5. Return grounded result dict.
    """
    # 1. Generate candidates
    candidates = generate_candidates(user, context=context, max_candidates=max_results * 2)

    # 2. Score each candidate
    scored: list[dict] = []
    for product in candidates:
        dna_signals = extract_dna_signals(user)
        score = score_product(product, user, dna_signals=dna_signals)
        scored.append(
            {
                "product": product,
                "score": score,
                "product_info": {
                    "product_id": product.pk,
                    "name": product.name,
                    "brand": product.brand.name if product.brand else "",
                    "category": product.category or "",
                    "price": float(product.price or 0),
                    "primary_color": product.color.name if product.color else "",
                    "is_purchasable": product.is_purchasable,
                },
            }
        )

    # 3. Apply diversity constraints
    if diversity:
        scored = apply_diversity_constraints(
            scored, max_per_category=2, max_per_brand=2, max_per_color=2
        )

    # 4. Sort by final score (highest first)
    scored.sort(key=lambda e: e["score"]["total_raw"], reverse=True)

    # 5. Build explanation strings and format output
    results: list[dict] = []
    for entry in scored[:max_results]:
        score = entry["score"]
        product = entry["product"]
        contributions = score.get("contributions", {})

        # Build a human-readable reason from the top contributing signals
        reason_parts: list[str] = []
        top_signals = sorted(contributions.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
        for sig_name, weight in top_signals:
            if weight == 0:
                continue
            # Map signal name to human-readable text
            sig_label = sig_name.replace("_", " ").title()
            contributions_pct = abs(weight) / max(abs(score["total_raw"]), 0.1) * 100
            reason_parts.append(f"{sig_label} ({contributions_pct:.0f}%)")

        reason = "; ".join(reason_parts) if reason_parts else "Recommended for you"

        results.append(
            {
                "product": product,
                "score": score,
                "reason": reason,
                "source": "deterministic_engine",
            }
        )

    # 6. Return grounded dict (same shape as stylist output for UI compatibility)
    return {
        "summary": f"We found {len(results)} items recommended for you.",
        "outfits": [],  # no outfit-specific results in this context
        "tips": _build_tips(context, results),
        "source": results[0]["source"] if results else "no_candidates",
    }


def _build_tips(context: str, results: list[dict]) -> list[str]:
    """Build tip strings based on the recommendation context and results."""
    tips: list[str] = []

    if not results:
        tips.append(
            "Add some preferences to your FLASH DNA profile for more personalized recommendations."
        )
        return tips

    # Gather top signals from the results
    all_contributions: dict[str, float] = defaultdict(float)
    for r in results:
        score = r.get("score", {})
        for sig, val in score.get("contributions", {}).items():
            all_contributions[sig] += val

    if all_contributions:
        top_sig = max(all_contributions.items(), key=lambda x: abs(x[1]))
        sig_name = top_sig[0].replace("_", " ").title()
        tips.append(f"Your style leans toward {sig_name}.")

    # Context-specific tips
    if context == "closet_complement":
        tips.append("These items complement pieces already in your closet.")
    elif context == "outfit_completion":
        tips.append("These items could complete one of your saved outfits.")
    elif context == "similar_products":
        tips.append("These products are similar to ones you've viewed or owned.")

    if not tips:
        tips.append("Mix and match with items you already own.")

    return tips[:3]
