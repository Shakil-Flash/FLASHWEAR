"""FLASH DNA: user-owned structured fashion preference profile (Phase 9).

This is not an unstructured JSON blob. Every important preference is a named,
validated, server-enforced field so the AI stylist and future recommendation
engine can query and join against it reliably.

Reuses existing controlled vocabularies where appropriate ( ClosetItem,
Catalog Fit/Material/Color, Occasion, Season ).
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.attributes import Color, Fit, Material
from apps.closet.models.items import ClosetItem

__all__ = ["DNA_OCCASIONS", "DNA_SEASONS", "DNA_STYLES", "STYLE_GOALS", "FlashDNA"]


# ---------------------------------------------------------------------------
# Controlled vocabularies (re-used from existing app choices where possible)
# ---------------------------------------------------------------------------

DNA_STYLES = [
    ("minimal", "Minimal"),
    ("streetwear", "Streetwear"),
    ("smart_casual", "Smart casual"),
    ("formal", "Formal"),
    ("vintage", "Vintage"),
    ("sporty", "Sporty"),
    ("oversized", "Oversized"),
    ("classic", "Classic"),
]

# Phase 21 style goals: a single optional direction the customer is dressing towards.
# Kept separate from ``DNA_STYLES`` on purpose -- a goal is a *direction*, not a liked
# style, so choosing one must never rewrite the preference list the stylist already reads.
STYLE_GOALS = [
    ("minimal", "Minimal"),
    ("streetwear", "Streetwear"),
    ("smart_casual", "Smart casual"),
    ("formal", "Formal"),
    ("trendy", "Trendy"),
    ("classic", "Classic"),
    ("athleisure", "Athleisure"),
]

DNA_OCCASIONS = [
    ("casual", "Casual"),
    ("office", "Office"),
    ("university", "University"),
    ("date", "Date"),
    ("party", "Party"),
    ("wedding", "Wedding"),
    ("travel", "Travel"),
    ("formal", "Formal"),
]

DNA_SEASONS = [
    ("all_season", "All season"),
    ("spring", "Spring"),
    ("summer", "Summer"),
    ("autumn", "Autumn"),
    ("winter", "Winter"),
]


# ---------------------------------------------------------------------------
# The profile model
# ---------------------------------------------------------------------------


class FlashDNA(models.Model):
    """One user's structured fashion preference profile.

    Each field is a deliberately chosen type — not a free-for-all JSON blob — so
    the AI stylist and any future recommendation engine can safely query and
    validate preferences without guessing at keys or types.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="flash_dna",
        verbose_name=_("user"),
        help_text=_("CASCADE: DNA is private user data and leaves with the account."),
    )

    # --- Preferred styles ----------------------------------------------------

    styles = models.CharField(
        _("preferred styles"),
        max_length=64,
        blank=True,
        help_text=_(
            "Comma-separated list from: Minimal, Streetwear, Smart casual, Formal, "
            "Vintage, Sporty, Oversized, Classic."
        ),
    )

    # --- Favorite colors ----------------------------------------------------

    favorite_colors = models.ManyToManyField(
        Color,
        blank=True,
        related_name="favorite_dna",
        verbose_name=_("favorite colors"),
        help_text=_("Colors the customer loves. Rows from the catalogue colour system."),
    )

    # --- Disliked colors ----------------------------------------------------

    disliked_colors = models.ManyToManyField(
        Color,
        blank=True,
        related_name="disliked_dna",
        verbose_name=_("disliked colors"),
        help_text=_(
            "Colors the customer actively avoids. Rows from the catalogue colour system. "
            "Take precedence over favourite colours when both are set."
        ),
    )

    # --- Preferred categories -----------------------------------------------

    preferred_categories = models.CharField(
        _("preferred categories"),
        max_length=32,
        blank=True,
        choices=ClosetItem.Category.choices,
        help_text=_("Wardrobe categories the customer reaches for most. From the closet taxonomy."),
    )

    # --- Preferred fits -----------------------------------------------------

    preferred_fits = models.ManyToManyField(
        Fit,
        blank=True,
        verbose_name=_("preferred fits"),
        help_text=_(
            "Structured fit labels from the catalogue (e.g. Slim, Relaxed, Oversized). "
            "These are queryable, unlike free-text fit notes."
        ),
    )

    # --- Preferred materials ------------------------------------------------

    preferred_materials = models.ManyToManyField(
        Material,
        blank=True,
        verbose_name=_("preferred materials"),
        help_text=_("Fabric/composition labels from the catalogue. Filterable, not free text."),
    )

    # --- Preferred occasions ------------------------------------------------

    preferred_occasions = models.CharField(
        _("preferred occasions"),
        max_length=32,
        blank=True,
        choices=DNA_OCCASIONS,
        help_text=_("Events where the customer wants to look appropriate."),
    )

    # --- Preferred seasons --------------------------------------------------

    preferred_seasons = models.CharField(
        _("preferred seasons"),
        max_length=32,
        blank=True,
        choices=DNA_SEASONS,
        help_text=_("Time of year the customer prefers."),
    )

    # --- Preferred price range ----------------------------------------------

    class PriceRange(models.TextChoices):
        UNDER_25 = ("under_25", "Under $25")
        UNDER_50 = ("under_50", "Under $50")
        UNDER_100 = ("under_100", "Under $100")
        UNDER_150 = ("under_150", "Under $150")
        ABOVE_150 = ("above_150", "Above $150")

    preferred_price_range = models.CharField(
        _("preferred price range"),
        max_length=20,
        blank=True,
        choices=PriceRange.choices,
        help_text=_("Upper bound only. The customer's typical spending limit."),
    )

    # --- Preferred brands ---------------------------------------------------

    preferred_brands = models.ManyToManyField(
        "catalog.Brand",
        blank=True,
        verbose_name=_("preferred brands"),
        help_text=_("Brands the customer tends to choose. From the catalogue."),
    )

    # --- Style confidence ---------------------------------------------------

    confidence_level = models.CharField(
        _("style confidence"),
        max_length=16,
        blank=True,
        choices=[
            ("high", "High"),
            ("medium", "Medium"),
            ("low", "Low"),
        ],
        default="medium",
        help_text=_(
            "How confident the customer is in their own style sense. Used by the AI "
            "stylist to weight explicit preferences."
        ),
    )

    # --- Fashion goals ------------------------------------------------------

    fashion_goal = models.CharField(
        _("fashion goal"),
        max_length=120,
        blank=True,
        help_text=_(
            "What the customer is working towards, "
            "e.g. Build a capsule wardrobe, "
            "Dress more professionally, "
            "Experiment with color."
        ),
    )

    # --- Style goal (Phase 21) ----------------------------------------------

    style_goal = models.CharField(
        _("style goal"),
        max_length=20,
        blank=True,
        choices=STYLE_GOALS,
        help_text=_(
            "Optional single style direction (Minimal, Streetwear, ...). "
            "Additive: it weights generation and recommendations without touching "
            "the preferred-styles list above."
        ),
    )

    # --- Audit ------------------------------------------------------------

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("FLASH DNA")
        verbose_name_plural = _("FLASH DNA")
        ordering = ("-updated_at",)

    def __str__(self) -> str:
        return f"FLASH DNA — {self.user.email}"

    def clean(self) -> None:
        """Validate field constraints."""

        # On an unsaved row the M2M relations cannot be read (Django raises), so only the
        # plain fields participate in the emptiness check before the first INSERT; every
        # later validation (update_dna's full_clean) re-checks the complete profile.
        if self.pk is None:
            total_fields = (
                bool(self.styles)
                + bool(self.preferred_categories)
                + bool(self.preferred_occasions)
                + bool(self.preferred_seasons)
                + (1 if self.preferred_price_range else 0)
                + bool(self.fashion_goal)
                + (1 if self.style_goal else 0)
            )
        else:
            # At least some preferences should be set; a profile with zero fields is unhelpful.
            # The M2M groups are booleans, not sizes: the check below only asks "is anything
            # set at all", and three of these fields are CharFields (whose ``count()`` would
            # be str.count and raise on an empty argument list).
            total_fields = (
                bool(self.styles)
                + bool(self.favorite_colors.exists())
                + bool(self.disliked_colors.exists())
                + bool(self.preferred_categories)
                + bool(self.preferred_fits.exists())
                + bool(self.preferred_materials.exists())
                + bool(self.preferred_occasions)
                + bool(self.preferred_seasons)
                + (1 if self.preferred_price_range else 0)
                + bool(self.preferred_brands.exists())
                + bool(self.fashion_goal)
                + (1 if self.style_goal else 0)
            )
        if total_fields == 0:
            raise ValidationError(
                _("At least one preference must be set so the AI stylist has something to weight."),
                code="empty_profile",
            )

    def save(self, *args, **kwargs) -> None:  # noqa: DJ012
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def is_complete(self) -> bool:
        """Whether the profile has enough explicit preferences for the AI to weight them.

        "Enough" means *any* preference at all -- the same bar as the create-time emptiness
        check. The scalar fields are CharFields (plain truthiness); the M2M groups can only
        be read once the row exists, so an unsaved profile is judged on scalars alone.
        """
        scalars = (
            self.styles,
            self.preferred_categories,
            self.preferred_occasions,
            self.preferred_seasons,
            self.preferred_price_range,
            self.fashion_goal,
            self.style_goal,
        )
        if any(scalars):
            return True
        if self.pk is None:
            return False
        return any(
            qs.exists()
            for qs in (
                self.favorite_colors.all(),
                self.disliked_colors.all(),
                self.preferred_fits.all(),
                self.preferred_materials.all(),
                self.preferred_brands.all(),
            )
        )


# ---------------------------------------------------------------------------
# Module-level convenience helpers (used by the AI context builder)
# ---------------------------------------------------------------------------


def get_flash_dna(user) -> FlashDNA | None:
    """Return the user's DNA profile, or None if one does not exist yet.

    Anonymous visitors (the storefront's default) have no profile relation at all, so the
    authentication check comes first. The row is read from the table rather than through
    ``user.flash_dna``: that reverse descriptor caches a miss, so code that looked before
    the first profile existed (a goal save, say) would keep seeing None on the same
    instance even after the row was written.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return FlashDNA.objects.filter(user=user.pk).first()


def dna_style_list(dna: FlashDNA) -> list[str]:
    """Return the customer's style preferences as a plain list."""
    if not dna.styles:
        return []
    return [s.strip() for s in dna.styles.split(",") if s.strip()]


def dna_color_list(dna: FlashDNA, preferred: bool = True) -> list[str]:
    """Return favourite (preferred=True) or disliked (preferred=False) colour names."""
    qs = dna.favorite_colors if preferred else dna.disliked_colors
    return [c.name for c in qs.all()]


def dna_category_list(dna: FlashDNA) -> list[str]:
    """Return the customer's preferred category names."""
    return [c.name for c in dna.preferred_categories.all()]


def dna_occasion_list(dna: FlashDNA) -> list[str]:
    """Return the customer's preferred occasion names."""
    return [o.name for o in dna.preferred_occasions.all()]


def dna_season_list(dna: FlashDNA) -> list[str]:
    """Return the customer's preferred season names."""
    return [s.name for s in dna.preferred_seasons.all()]
