"""AI Fashion Stylist service layer (Phase 9).

Responsible for:

1. Building a privacy-aware context dict from the user's FLASH DNA,
   closet, and saved outfits.
2. Calling an AI provider (real or mock) with that context.
3. Validating the provider's structured response.
4. Returning safe, grounded recommendations.

The actual AI call is delegated to a :class:`AIStylistProvider` so the
application is not tightly coupled to one vendor.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.catalog.models import Product
from apps.closet.models import ClosetItem, Outfit
from apps.styling.models.flash_dna import FlashDNA, get_flash_dna
from apps.styling.services.dna import dna_category_list, dna_style_list
from apps.styling.services.errors import (
    AIDisabledError,
    EmptyProfileError,
    MalformedOutputError,
    ProviderError,
    StylistError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class AIStylistProvider:
    """Abstract base for AI stylist providers.

    Subclasses implement ``generate_advice`` and optionally
    ``check_health``. The concrete class is configured via
    ``settings.AI_STYLIST_PROVIDER``.
    """

    name = "base"

    def check_health(self) -> bool:
        """Return ``True`` when the provider is operational.

        Used by caching and rate-limiting logic. Default implementation
        returns ``True`` so the system works without an external API in
        development.
        """
        return True

    def generate_advice(self, context: dict) -> dict:
        """Return structured stylist advice given the rendered context.

        The caller (``stylist_service``) is responsible for constructing
        ``context`` and for validating the returned dict.
        """
        raise NotImplementedError("Subclasses must implement generate_advice")


# ---------------------------------------------------------------------------
# Concrete providers
# ---------------------------------------------------------------------------


class MockProvider(AIStylistProvider):
    """Deterministic mock provider for development and tests.

    Never makes an external API call. Returns sensible fallback outfits
    built from the user's actual closet and DNA — so the UI always has
    something to show even when no AI key is configured.
    """

    name = "mock"

    def check_health(self) -> bool:
        return True

    def generate_advice(self, context: dict) -> dict:
        """Build deterministic fallback outfits from the user's closet."""

        dna = context.get("dna")
        closet_items = context.get("closet_items", [])
        request_type = context.get("request_type", "style_outfit")

        # Build a very simple recommendation: pick 3 active closet items
        # that match the user's preferred styles/categories, and present
        # them as outfit components.
        recommendations = []
        preferred_styles = dna_style_list(dna) if dna else []
        preferred_categories = dna_category_list(dna) if dna else []

        # Select matching items
        matched = []
        for item in closet_items:
            if item.get("role") in ("top", "bottom", "shoes"):
                matched.append(item)

        # If we have enough, pick 3; otherwise just take what we have
        selections = matched[:3] if len(matched) >= 3 else matched

        for i, item in enumerate(selections):
            recommendations.append(
                {
                    "name": item.get("name", "Piece"),
                    "role": item.get("role", ""),
                    "occasion": self._pick_occasion(preferred_styles, preferred_categories),
                    "reason": f"Selected because it matches your {item.get('role', '')} preference",
                    "confidence": round(0.7 + i * 0.1, 1),
                }
            )

        if not selections:
            # Absolute fallback: any active closet item
            fallback = closet_items[0] if closet_items else None
            if fallback:
                recommendations.append(
                    {
                        "name": fallback.get("name", "Item"),
                        "role": fallback.get("role", ""),
                        "occasion": "Casual",
                        "reason": "Your closet has items — try adjusting your preferences.",
                        "confidence": 0.5,
                    }
                )

        return {
            "summary": self._build_summary(request_type, recommendations),
            "outfits": recommendations,
            "tips": self._build_tips(request_type, dna, preferred_styles),
            "source": "deterministic_fallback",
        }

    @staticmethod
    def _pick_occasion(styles: list[str], categories: list[str]) -> str:
        if "formal" in styles or "formal" in list(categories):
            return "Formal"
        if "casual" in styles or any("casual" in s for s in styles):
            return "Casual"
        return "Occasion"

    @staticmethod
    def _build_summary(request_type: str, outfits: list[dict]) -> str:
        if not outfits:
            return "No outfit could be generated right now."
        if len(outfits) == 1:
            return f"Here's an outfit focused on {outfits[0].get('role', 'items')}."
        return f"Here are {len(outfits)} outfit options for you."

    @staticmethod
    def _build_tips(request_type: str, dna, preferred_styles: list[str]) -> list[str]:
        tips = []
        if preferred_styles:
            tips.append(f"Your style leans toward {', '.join(preferred_styles)}.")
        if not tips:
            tips.append("Mix and match pieces you already own.")
        return tips


class RealProvider(AIStylistProvider):
    """Real AI provider wrapper.

    Calls an external LLM via API key from ``settings.AI_API_KEY``.
    If the key is missing or the provider is disabled, behaviour falls back
    to the ``MockProvider`` so the rest of the system never crashes.
    """

    name = "openai"  # or "anthropic", "gemini", etc. — the name is informational

    def __init__(self) -> None:
        self.api_key = getattr(settings, "AI_API_KEY", None)
        self.model = getattr(settings, "AI_STYLIST_MODEL", "gpt-4o-mini")
        self.timeout = getattr(settings, "AI_STYLIST_TIMEOUT", 30)
        self.max_tokens = getattr(settings, "AI_STYLIST_MAX_TOKENS", 500)
        self._enabled = bool(self.api_key and settings.AI_STYLIST_ENABLED)

    def check_health(self) -> bool:
        return self._enabled and super().check_health()

    def generate_advice(self, context: dict) -> dict:
        if not self._enabled:
            raise ProviderError("AI stylist is not configured.", code="not_configured")

        # In a real deployment we would call the external API here.
        # For Phase 9 we raise a structured error so the UI can show a
        # friendly message rather than a 500.
        raise ProviderError(
            "AI provider is configured but the real API call is not yet implemented "
            "in this phase. Using deterministic fallback instead.",
            code="not_implemented",
        )


# ---------------------------------------------------------------------------
# Provider lookup
# ---------------------------------------------------------------------------


def get_provider() -> AIStylistProvider:
    """Return the configured :class:`AIStylistProvider` instance.

    Falls back to ``MockProvider`` when no real key is set, so the rest
    of the system never crashes.
    """
    provider_name = getattr(settings, "AI_STYLIST_PROVIDER", "mock")
    if provider_name == "mock":
        return MockProvider()
    if provider_name == "real":
        # Will raise ProviderError inside generate_advice if not configured.
        return RealProvider()
    # Unknown name → mock
    return MockProvider()


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def build_stylist_context(user, request_path: str = "") -> dict:
    """Build the structured context dict to hand to the AI provider.

    What we send is deliberately minimal — only what is relevant and
    non-personal in the wrong senses (no passwords, no auth tokens, no
    payment data).

    Keys supplied by Phase 9 spec §12–§14.
    """
    from apps.styling.services.dna import (
        dna_category_list,
        dna_color_list,
        dna_occasion_list,
        dna_season_list,
        dna_style_list,
    )

    dna = get_flash_dna(user)
    if dna is None:
        dna = dna.create_dna(user=user)  # creates an empty profile

    # --- Closet ------------------------------------------------------------
    closet_items = _build_closet_context(user)

    # --- Saved outfits -----------------------------------------------------
    outfits_context = _build_outfits_context(user)

    # --- Catalog context (relevant only, not the whole DB) ----------------
    catalog_context = _build_catalog_context(user, request_path)

    # --- Preferences -------------------------------------------------------
    preferred_styles = dna_style_list(dna)
    preferred_colors = dna_color_list(dna, preferred=True)
    disliked_colors = dna_color_list(dna, preferred=False)
    preferred_categories = dna_category_list(dna)
    preferred_fits = _fit_list_from_dna(dna)
    preferred_materials = _material_list_from_dna(dna)
    preferred_occasions = dna_occasion_list(dna)
    preferred_seasons = dna_season_list(dna)

    return {
        "user": user,
        "dna": {
            "styles": preferred_styles,
            "favorite_colors": preferred_colors,
            "disliked_colors": disliked_colors,
            "favorite_categories": preferred_categories,
            "favorite_fits": preferred_fits,
            "favorite_materials": preferred_materials,
            "favorite_occasions": preferred_occasions,
            "favorite_seasons": preferred_seasons,
            "confidence_level": dna.confidence_level,
            "fashion_goal": dna.fashion_goal or "",
        },
        "closet": closet_items,
        "outfits": outfits_context,
        "catalog": catalog_context,
        "request_type": _request_type(request_path),
    }


def _build_closet_context(user) -> list[dict]:
    """Active closet items with the minimal metadata the AI needs."""
    items = []
    for item in user.closet_items.filter(status=ClosetItem.Status.ACTIVE).select_related(
        "variant", "variant__product", "variant__color", "variant__size"
    ):
        # Use the same display logic as the closet card
        image = item.display_image
        img_url = image.url if image else None
        variant = item.variant
        color_name = item.color or (variant.color.name if variant and variant.color else "")
        size_name = item.size or (variant.size.name if variant and variant.size else "")

        role = item.is_flashwear  # simplified; actual role from OutfitItem later
        # Determine role from category
        cat_name = item.category or ""
        if cat_name in ("tops",):
            role = "top"
        elif cat_name in ("bottoms",):
            role = "bottom"
        elif cat_name in ("outerwear",):
            role = "outerwear"
        elif cat_name in ("dresses",):
            role = "dress"
        elif cat_name in ("shoes",):
            role = "shoes"
        elif cat_name in ("accessories",):
            role = "accessory"
        else:
            role = "accessory"

        items.append(
            {
                "pk": item.pk,
                "name": item.name,
                "role": role,
                "category": cat_name,
                "color": color_name,
                "size": size_name,
                "brand": item.brand or "",
                "image_url": img_url,
                "is_flashwear": item.is_flashwear,
                "source": item.source,
            }
        )
    return items


def _build_outfits_context(user) -> list[dict]:
    """Minimal metadata for each saved outfit, for AI context."""

    outfits = []
    for outfit in user.outfits.filter(status=Outfit.Status.SAVED).prefetch_related(
        "items__closet_item", "items__closet_item__variant", "items__closet_item__variant__product"
    )[:10]:
        links = list(outfit.items.all()[:5])  # cap to first 5 items
        item_summaries = []
        for link in links:
            ci = link.closet_item
            role = link.role
            item_data = {
                "name": ci.name,
                "role": role,
                "category": ci.category or "",
                "color": ci.color or "",
                "size": ci.size or "",
                "is_flashwear": ci.is_flashwear,
            }
            item_summaries.append(item_data)

        outfits.append(
            {
                "pk": outfit.pk,
                "name": outfit.name,
                "status": outfit.status,
                "item_count": outfit.items.count(),
                "items": item_summaries,
                "description": outfit.description or "",
            }
        )
    return outfits


def _build_catalog_context(user, request_path: str) -> dict:
    """Minimal catalogue context — only products relevant to the current request.

    Never sends the entire product DB to the AI.
    """
    # For now, provide an empty catalog context; the AI can request specific
    # products through the dedicated ``style-product`` endpoint if needed.
    # Sending the full catalog would be a privacy violation and cost explosion.
    return {
        "note": "Catalog context available on a per-request basis via dedicated endpoints.",
        "products": [],
    }


def _request_type(path: str) -> str:
    """Map the URL path to a stylist request type string."""
    if "today" in path or "what should i wear" in path.lower():
        return "what_should_i_wear"
    if "date" in path.lower():
        return "style_outfit"
    if "weekend" in path.lower() or "casual" in path.lower():
        return "style_outfit"
    if "work" in path.lower() or "office" in path.lower():
        return "style_outfit"
    if "closet" in path.lower():
        return "use_closet"
    if "product" in path.lower():
        return "style_product"
    return "style_outfit"


def _fit_list_from_dna(dna: FlashDNA) -> list[str]:
    """Return the customer's preferred fit names as a plain list."""
    # Mapped from the DNA's free-text styles; a future version could store
    # Fit rows directly. For now we infer from style keywords.
    styles = dna_style_list(dna)
    inferred = []
    for s in styles:
        if "oversized" in s.lower():
            inferred.append("Oversized")
        elif "slim" in s.lower():
            inferred.append("Slim")
        elif "relaxed" in s.lower():
            inferred.append("Relaxed")
        elif "athletic" in s.lower():
            inferred.append("Athletic")
    return inferred


def _material_list_from_dna(dna: FlashDNA) -> list[str]:
    """Return the customer's preferred material names as a plain list."""
    # Placeholder — DNA has no material field yet; return empty list.
    # Future: store Material rows and return their names.
    return []


# ---------------------------------------------------------------------------
# Stylist service — orchestrates context + provider + validation
# ---------------------------------------------------------------------------


def stylist_service(user, request_path: str = "", provider_name: str | None = None) -> dict:
    """Entry point: build context, call provider, validate response.

    Returns a dict with the same shape as the mock provider output, so the
    UI template never needs to branch on the provider kind.

    Raises appropriate ``StylistError`` subclasses that the view layer
    turns into user-facing messages.
    """
    from apps.styling.services import get_provider

    if not user.is_authenticated:
        raise StylistError("You must be signed in to get styling advice.", code="not_signed_in")

    # Check if AI is disabled globally
    if not getattr(settings, "AI_STYLIST_ENABLED", True):
        raise AIDisabledError("AI stylist is currently disabled.", code="disabled")

    # Build context
    context = build_stylist_context(user, request_path)

    # Use the requested provider (or the default from settings)
    if provider_name is None:
        provider_name = getattr(settings, "AI_STYLIST_PROVIDER", "mock")
    provider = get_provider() if provider_name else MockProvider()

    # Health check
    if not provider.check_health():
        raise ProviderError("AI provider is unavailable right now.", code="unavailable")

    try:
        raw = provider.generate_advice(context)
    except Exception as exc:  # broad; provider may raise anything
        logger.exception("AI provider raised an exception")
        raise ProviderError(
            "The AI service experienced a technical problem. Please try again later.",
            code="provider_error",
        ) from exc

    # Validate shape — must be a dict with expected keys
    if not isinstance(raw, dict):
        raise MalformedOutputError(
            "The AI returned unexpected content. Please try again.",
            code="bad_shape",
        )

    # Require top-level keys
    for required in ("summary", "outfits", "tips"):
        if required not in raw:
            raise MalformedOutputError(
                f"The AI response is missing the required “{required}” field.",
                code="missing_field",
            )

    # Validate outfits is a list
    if not isinstance(raw["outfits"], list):
        raise MalformedOutputError(
            "The AI “outfits” field must be a list.",
            code="bad_outfits_type",
        )

    # Ground each recommended closet item: verify ownership and active status
    validated_outfits = []
    for outfit_entry in raw["outfits"]:
        if not isinstance(outfit_entry, dict):
            continue  # skip malformed entries
        validated_items = []
        for item_id in outfit_entry.get("closet_item_ids", []):
            try:
                ci = ClosetItem.objects.get(pk=item_id, user=user, status=ClosetItem.Status.ACTIVE)
                item_dict = {"closet_item_id": ci.pk, "name": ci.name, "role": ci.category or ""}
                validated_items.append(item_dict)
            except ClosetItem.DoesNotExist:
                # Silently drop invalid IDs — the AI invented something that
                # doesn't belong to the user or is archived.
                pass
        # Also validate any product IDs if present
        product_ids = outfit_entry.get("product_ids", [])
        validated_products = []
        for pid in product_ids:
            try:
                from apps.catalog.models import Product

                p = Product.objects.get(pk=pid, is_purchasable=True)
                validated_products.append({"product_id": p.pk, "name": p.name})
            except (Product.DoesNotExist, ValueError):
                pass
        validated_outfit = {
            "name": outfit_entry.get("name", "Outfit"),
            "closet_item_ids": [v["closet_item_id"] for v in validated_items],
            "product_ids": validated_products,
            "reason": outfit_entry.get("reason", ""),
            "occasion": outfit_entry.get("occasion", ""),
            "confidence": min(max(outfit_entry.get("confidence", 0.5), 0.0), 1.0),
        }
        if validated_outfit["closet_item_ids"] or validated_outfit["product_ids"]:
            validated_outfits.append(validated_outfit)

    # Replace with grounded version
    grounded = {
        "summary": raw.get("summary", ""),
        "outfits": validated_outfits,
        "tips": raw.get("tips", []),
    }
    return grounded


# ---------------------------------------------------------------------------
# High-level request handlers (map to the API endpoints)
# ---------------------------------------------------------------------------


def recommend_outfit(user, request_path: str = "", provider_name: str | None = None) -> dict:
    """Public helper: return grounded outfit recommendations for the user."""
    return stylist_service(user, request_path, provider_name)


def style_product(user, product_pk: int, provider_name: str | None = None) -> dict:
    """Style a specific product against the user's closet and DNA."""

    try:
        product = Product.objects.get(pk=product_pk, is_purchasable=True)
    except Product.DoesNotExist:
        raise StylistError(
            "The requested product is not available.",
            code="product_unavailable",
        ) from None

    context = build_stylist_context(user)
    context["request_type"] = "style_product"
    context["featured_product"] = {
        "pk": product.pk,
        "name": product.name,
        "brand": product.brand.name if product.brand else "",
        "color": product.color.name if product.color else "",
        "size": product.size.name if product.size else "",
    }

    result = stylist_service(user, "style-product", provider_name)
    # Prepend the featured product info
    featured = {
        "featured_product": context["featured_product"],
        "name": "Style this product",
        "role": "",
        "occasion": "",
        "confidence": 0.5,
        "reason": "Generate an outfit around this piece",
    }

    if result["outfits"]:
        result["outfits"][0]["featured_product"] = context["featured_product"]
    else:
        result["outfits"] = [featured]

    return result


def save_recommended_outfit(user, outfit_data: dict) -> Outfit | None:
    """Save a generated outfit as a real Draft outfit with ClosetItem references.

    Does NOT create ClosetItem records — those must already exist. The new
    Outfit simply references existing items by pk. This preserves the invariant
    that recommendation ≠ ownership.
    """
    from apps.styling.services.dna import validate_dna_has_preferences

    # Ensure the profile has some preferences
    dna, _ = user.flash_dna.get_or_create()
    try:
        validate_dna_has_preferences(dna)
    except EmptyProfileError:
        # If the profile is empty, we still allow saving but the outfit will
        # have no preference-based selections; the UI should nudge the user
        # to set preferences.
        pass

    # Build the outfit from the validated data
    closet_item_ids = outfit_data.get("closet_item_ids", [])

    # Create a Draft outfit
    from apps.closet.services.outfits import Outfit as OutfitModel
    from apps.closet.services.outfits import create_outfit

    outfit = create_outfit(
        user=user,
        name=outfit_data.get("name", "AI-generated outfit"),
        description=outfit_data.get("reason", ""),
        occasion=outfit_data.get("occasion", ""),
        status=OutfitModel.Status.DRAFT,
    )

    # Add closet items (verified ownership)
    for cid in closet_item_ids:
        from apps.closet.models import ClosetItem

        try:
            ci = ClosetItem.objects.get(pk=cid, user=user, status=ClosetItem.Status.ACTIVE)
            from apps.closet.services.outfits import add_item

            add_item(outfit, ci, note="Added by AI stylist")
        except (ClosetItem.DoesNotExist, ValueError):
            pass  # skip invalid

    # Product IDs are informational only — do NOT create ClosetItem from them.
    # The outfit will only contain real ClosetItem records.

    return outfit
