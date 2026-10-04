"""FLASH DNA domain services (Phase 9).

Read-only queries and mutation helpers for the FlashDNA profile, with
ownership enforcement and privacy guards.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from apps.styling.models.flash_dna import (
    FlashDNA,
    get_flash_dna,
)
from apps.styling.services.errors import DNAError, EmptyProfileError

User = get_user_model()


def get_user_dna(user) -> FlashDNA | None:
    """Return the profile for *user*, or None if it does not exist yet.

    Ownership is enforced by the one-to-one link; callers must still check
    ``request.user`` at the view level.
    """
    return get_flash_dna(user)


def has_dna(user) -> bool:
    """True when the user already has a FlashDNA profile."""
    return user.flash_dna is not None


def create_dna(user, **preferences) -> FlashDNA:
    """Create a new FlashDNA profile for *user*.

    Raises ``DNAError`` if a profile already exists or validation fails.
    """
    if user.flash_dna is not None:
        raise DNAError("This user already has a FLASH DNA profile.", code="already_exists")
    preferences.setdefault("user", user)
    try:
        profile = FlashDNA.objects.create(**preferences)
    except ValidationError as exc:
        raise DNAError(exc.message, code="validation") from exc
    return profile


def update_dna(user, **preferences) -> FlashDNA:
    """Update an existing FlashDNA profile for *user*.

    Raises ``DNAError`` if the profile does not exist or validation fails.
    """
    try:
        profile = user.flash_dna
    except FlashDNA.DoesNotExist:
        raise DNAError("This user has no FLASH DNA profile yet.", code="not_found") from None
    for key, value in preferences.items():
        if hasattr(profile, key):
            setattr(profile, key, value)
    try:
        profile.full_clean()
    except ValidationError as exc:
        raise DNAError(exc.message, code="validation") from exc
    profile.save()
    return profile


def reset_dna(user) -> FlashDNA:
    """Delete and recreate an empty FlashDNA profile for *user*.

    Useful for onboarding re-starts.
    """
    try:
        profile = user.flash_dna
    except FlashDNA.DoesNotExist:
        profile = create_dna(user)
    else:
        profile.delete()
        profile = create_dna(user)
    return profile


def validate_dna_has_preferences(dna: FlashDNA) -> None:
    """Raise ``EmptyProfileError`` if the profile has zero explicit preferences."""
    total = (
        bool(dna.styles)
        + dna.favorite_colors.count()
        + dna.disliked_colors.count()
        + dna.preferred_categories.count()
        + dna.preferred_fits.count()
        + dna.preferred_materials.count()
        + dna.preferred_occasions.count()
        + dna.preferred_seasons.count()
        + (1 if dna.preferred_price_range else 0)
        + dna.preferred_brands.count()
        + bool(dna.fashion_goal)
    )
    if total == 0:
        raise EmptyProfileError("This FLASH DNA profile has no preferences set.", code="empty")


def dna_style_list(dna: FlashDNA) -> list[str]:
    """Return the customer's style preferences as a plain list."""
    if not dna.styles:
        return []
    return [s.strip() for s in dna.styles.split(",") if s.strip()]


def dna_category_list(dna: FlashDNA) -> list[str]:
    """Return the customer's preferred category names."""
    return [c.name for c in dna.preferred_categories.all()]
