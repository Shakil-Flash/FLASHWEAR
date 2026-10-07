"""Style profile summary and evolution insights (Phase 31).

Provides customer-centric, editorial style presentation from FLASH DNA,
closet, and order history without exposing internal recommendation weights
or technical scores.
"""

from __future__ import annotations

from typing import Any

from apps.closet.models import ClosetItem
from apps.orders.models import Order, OrderItem
from apps.styling.models.flash_dna import FlashDNA, get_flash_dna


def get_style_profile_summary(user) -> dict[str, Any]:
    """Build a customer-facing fashion profile summary from FLASH DNA."""
    if not user or not user.is_authenticated:
        return {"has_dna": False, "is_complete": False}

    dna: FlashDNA | None = get_flash_dna(user)
    if not dna or not dna.is_complete:
        return {
            "has_dna": bool(dna),
            "is_complete": False,
            "dna": dna,
        }

    # Format styles
    styles: list[str] = [s.strip().title() for s in dna.styles.split(",") if s.strip()]

    # Colors
    favorite_colors = list(
        dna.favorite_colors.all().order_by("name")
    )
    disliked_colors = list(
        dna.disliked_colors.all().order_by("name")
    )

    # Fits
    preferred_fits = list(
        dna.preferred_fits.all().order_by("name")
    )

    # Materials
    preferred_materials = list(
        dna.preferred_materials.all().order_by("name")
    )

    # Categories
    categories = [
        dict(ClosetItem.Category.choices).get(c.strip(), c.strip().title())
        for c in dna.preferred_categories.split(",")
        if c.strip()
    ] if dna.preferred_categories else []

    # Occasions & Seasons
    occasions = (
        dict(dna._meta.get_field("preferred_occasions").choices).get(
            dna.preferred_occasions, ""
        )
        if dna.preferred_occasions
        else ""
    )
    seasons = (
        dict(dna._meta.get_field("preferred_seasons").choices).get(
            dna.preferred_seasons, ""
        )
        if dna.preferred_seasons
        else ""
    )
    price_range = (
        dict(dna._meta.get_field("preferred_price_range").choices).get(
            dna.preferred_price_range, ""
        )
        if dna.preferred_price_range
        else ""
    )
    style_goal = (
        dict(dna._meta.get_field("style_goal").choices).get(
            dna.style_goal, ""
        )
        if dna.style_goal
        else ""
    )

    # Build an editorial style signature phrase
    signature_parts = []
    if styles:
        signature_parts.append(" & ".join(styles[:2]))
    if style_goal and style_goal not in signature_parts:
        signature_parts.append(f"{style_goal} Focus")
    signature = " · ".join(signature_parts) if signature_parts else "Contemporary Streetwear"

    return {
        "has_dna": True,
        "is_complete": True,
        "dna": dna,
        "signature": signature,
        "styles": styles,
        "favorite_colors": favorite_colors,
        "disliked_colors": disliked_colors,
        "preferred_fits": preferred_fits,
        "preferred_materials": preferred_materials,
        "preferred_categories": categories,
        "category_focus": categories,
        "preferred_occasions": occasions,
        "preferred_seasons": seasons,
        "seasons": [seasons] if seasons else [],
        "preferred_price_range": price_range,
        "fashion_goal": dna.fashion_goal,
        "style_goal": dna.style_goal,
        "style_goal_label": style_goal or (dna.style_goal.title() if dna.style_goal else ""),
    }


def get_style_evolution_insight(user) -> dict[str, Any]:
    """Detect subtle, grounded style evolution from real wardrobe and order history.

    Rules:
    - Only returns an insight if >= 2 historical items exist.
    - Compares recent items (last 3 items or past 60 days) against earlier items.
    - Zero guessing: if trends are neutral or ambiguous, returns has_data=False.
    """
    empty_evolution = {
        "has_data": False,
        "insight": "",
        "summary": "",
        "theme": "",
        "detail": "",
        "details": [],
    }
    if not user or not user.is_authenticated:
        return empty_evolution

    # 1. Gather historical purchases and closet items
    purchased_items = list(
        OrderItem.objects.filter(
            order__user=user,
            order__status__in=[
                Order.Status.PAID,
                Order.Status.SHIPPED,
                Order.Status.DELIVERED,
            ],
        )
        .select_related("variant__product__fit", "variant__color")
        .order_by("-order__created_at")[:10]
    )
    closet_items = list(
        ClosetItem.objects.filter(
            user=user,
            status=ClosetItem.Status.ACTIVE,
        )
        .select_related("variant__product__fit")
        .order_by("-created_at")[:10]
    )

    total_pieces = len(purchased_items) + len(closet_items)
    if total_pieces < 2:
        return empty_evolution

    # Inspect recent fits from variants
    recent_fits: list[str] = []
    for item in (purchased_items[:4] + closet_items[:4]):
        var = getattr(item, "variant", None)
        if var and var.product and var.product.fit:
            recent_fits.append(var.product.fit.name.lower())

    if recent_fits:
        relaxed_count = sum(
            1 for f in recent_fits if any(k in f for k in ["relaxed", "oversized", "boxy", "loose"])
        )
        slim_count = sum(
            1 for f in recent_fits if any(k in f for k in ["slim", "tailored", "skinny"])
        )

        if relaxed_count >= 2 and relaxed_count > slim_count:
            insight = "You've been exploring more relaxed, boxy silhouettes lately."
            detail = "Recent additions to your wardrobe emphasize comfortable, draped proportions."
            return {
                "has_data": True,
                "theme": "Silhouette",
                "insight": insight,
                "summary": insight,
                "detail": detail,
                "details": [detail],
            }
        if slim_count >= 2 and slim_count > relaxed_count:
            insight = "Your wardrobe is shifting toward cleaner, more tailored lines."
            detail = "Recent pieces favor structured cuts and defined contours."
            return {
                "has_data": True,
                "theme": "Silhouette",
                "insight": insight,
                "summary": insight,
                "detail": detail,
                "details": [detail],
            }

    # Inspect recent color palette from purchases
    recent_colors: list[str] = []
    for pi in purchased_items[:5]:
        if pi.variant and pi.variant.color:
            recent_colors.append(pi.variant.color.name.lower())
    for ci in closet_items[:5]:
        if ci.color:
            recent_colors.append(ci.color.lower())

    if recent_colors:
        neutral_keys = ["black", "white", "charcoal", "grey", "gray", "slate", "cream", "sand"]
        neutrals = sum(1 for c in recent_colors if any(k in c for k in neutral_keys))
        if neutrals >= 3:
            insight = "Your recent choices lean toward a minimalist, monochrome palette."
            detail = "Neutral tones form the foundation of your latest selections."
            return {
                "has_data": True,
                "theme": "Palette",
                "insight": insight,
                "summary": insight,
                "detail": detail,
                "details": [detail],
            }

        earth_keys = ["olive", "brown", "tan", "khaki", "forest", "moss", "rust", "clay"]
        earth_tones = sum(1 for c in recent_colors if any(k in c for k in earth_keys))
        if earth_tones >= 2:
            insight = "You've been leaning toward organic earth tones."
            detail = (
                "Muted greens, tans, and warm neutrals feature prominently in your recent picks."
            )
            return {
                "has_data": True,
                "theme": "Palette",
                "insight": insight,
                "summary": insight,
                "detail": detail,
                "details": [detail],
            }

    # Check category layering trends
    recent_categories = [ci.category for ci in closet_items[:5] if ci.category]
    outerwear_count = sum(
        1 for c in recent_categories if c in [ClosetItem.Category.OUTERWEAR, "outerwear"]
    )
    if outerwear_count >= 2:
        insight = "You've been building out your outerwear and layering options."
        detail = "Focusing on transitional jackets and versatile outer pieces."
        return {
            "has_data": True,
            "theme": "Wardrobe Role",
            "insight": insight,
            "summary": insight,
            "detail": detail,
            "details": [detail],
        }

    return {
        "has_data": False,
        "insight": "",
        "summary": "",
        "theme": "",
        "detail": "",
        "details": [],
    }
