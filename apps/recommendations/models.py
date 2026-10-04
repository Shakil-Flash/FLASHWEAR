"""Recommendation signals and scores models (Phase 10)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

#: Signal type choices for ``RecommendationSignal``.
#: The keys match the ``signal_type`` field values.
SIGNAL_CHOICES = [
    ("dna_style", "DNA style match"),
    ("dna_color", "DNA color match"),
    ("dna_category", "DNA category match"),
    ("dna_fit", "DNA fit match"),
    ("dna_material", "DNA material match"),
    ("dna_occasion", "DNA occasion match"),
    ("dna_season", "DNA season match"),
    ("dna_brand", "DNA brand preference"),
    ("dna_price", "DNA price range preference"),
    ("closet_complement", "Closet complement"),
    ("outfit_completion", "Outfit completion"),
    ("purchase_history", "Purchase history"),
    ("review_preference", "Review preference"),
    ("popularity", "Product popularity"),
    ("recency", "Product recency"),
    ("category_match", "Category match"),
    ("brand_match", "Brand match"),
    ("color_match", "Color match"),
    ("style_match", "Style match"),
]


class RecommendationSignal(models.Model):
    """A structured signal contributing to a recommendation score.

    Each signal records a weighted preference or behavior that the recommendation
    engine uses to rank products. Signals are user-scoped and time-aware.

    Signals are intentionally narrow in scope — they capture *what* happened,
    not *why*. The scoring engine interprets them.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recommendation_signals",
        verbose_name=_("user"),
    )

    signed_at = models.DateTimeField(_("signed at"), auto_now_add=True)

    signal_type = models.CharField(_("signal type"), max_length=32, choices=SIGNAL_CHOICES)

    # ---- Signal value -------------------------------------------------------

    # Generic value storage. The meaning depends on signal_type.
    # For categorical signals (style, color, etc.): the matching value text.
    # For numerical signals (popularity, recency): the raw value.
    value_text = models.CharField(_("value text"), max_length=128, blank=True)
    value_num = models.FloatField(_("value number"), null=True, blank=True)

    # ---- Strength ----------------------------------------------------------

    strength = models.FloatField(
        _("strength"),
        default=1.0,
        help_text=_(
            "How strongly this signal should weight recommendations. "
            "Default 1.0. Can be overridden per-signal-type in settings."
        ),
    )

    def __str__(self) -> str:
        return f"Signal {self.signal_type} for user {self.user_id}"

    # ---- Deduplication / recency -------------------------------------------

    # Allow the same signal kind to be recorded multiple times (e.g. multiple
    # purchases). The `signed_at` timestamp and a lightweight identifier
    # distinguish them.
    reference_id = models.CharField(
        _("reference id"),
        max_length=64,
        blank=True,
        help_text=_(
            "Optional identifier linking this signal to an external record "
            "(e.g. order PK, product PK)."
        ),
    )

    # ---- Audit ------------------------------------------------------------

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Recommendation Signal")
        verbose_name_plural = _("Recommendation Signals")
        ordering = ("-signed_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["user", "signal_type", "reference_id"],
                condition=Q(reference_id__isnull=False),
                name="unique_signal_per_user_type_ref",
            ),
            models.CheckConstraint(
                check=models.Q(strength__gt=0),
                name="signal_strength_positive",
            ),
        ]


class RecommendationScore(models.Model):
    """A structured score for a single product against a user's profile.

    Instances are created by the scoring engine during a recommendation run.
    They are NOT persisted long-term — they exist only for the duration of
    a single recommendation query, then are discarded.

    Each score breaks down into weighted signals so the output is explainable.
    """

    # ---- Identity ------------------------------------------------------------

    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.PROTECT,
        verbose_name=_("product"),
    )

    # ---- User context ------------------------------------------------------

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recommendation_scores",
        verbose_name=_("user"),
    )

    # ---- Score breakdown ----------------------------------------------------

    # Raw total before diversity adjustments.
    total_raw = models.FloatField(_("total raw"), default=0.0)

    # Total after diversity constraints applied.
    total_final = models.FloatField(_("total final"), default=0.0)

    # -------- Individual signal contributions --------------------------------

    # These fields store the weighted contribution of each signal category.
    # They are set by the scoring engine; they are not user-editable.

    dna_style_weight = models.FloatField(_("dna style weight"), default=0.0)
    dna_color_weight = models.FloatField(_("dna color weight"), default=0.0)
    dna_category_weight = models.FloatField(_("dna category weight"), default=0.0)
    dna_fit_weight = models.FloatField(_("dna fit weight"), default=0.0)
    dna_material_weight = models.FloatField(_("dna material weight"), default=0.0)
    dna_occasion_weight = models.FloatField(_("dna occasion weight"), default=0.0)
    dna_season_weight = models.FloatField(_("dna season weight"), default=0.0)
    dna_brand_weight = models.FloatField(_("dna brand weight"), default=0.0)
    dna_price_weight = models.FloatField(_("dna price weight"), default=0.0)

    closet_complement_weight = models.FloatField(_("closet complement weight"), default=0.0)
    outfit_completion_weight = models.FloatField(_("outfit completion weight"), default=0.0)
    purchase_history_weight = models.FloatField(_("purchase history weight"), default=0.0)
    review_preference_weight = models.FloatField(_("review preference weight"), default=0.0)
    popularity_weight = models.FloatField(_("popularity weight"), default=0.0)
    recency_weight = models.FloatField(_("recency weight"), default=0.0)
    category_match_weight = models.FloatField(_("category match weight"), default=0.0)
    brand_match_weight = models.FloatField(_("brand match weight"), default=0.0)
    color_match_weight = models.FloatField(_("color match weight"), default=0.0)
    style_match_weight = models.FloatField(_("style match weight"), default=0.0)

    # ---- Audit ------------------------------------------------------------

    calculated_at = models.DateTimeField(_("calculated at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Recommendation Score")
        verbose_name_plural = _("Recommendation Scores")
        ordering = ("-calculated_at",)

    def __str__(self) -> str:
        return f"Score product {self.product_id} for user {self.user_id}: total={self.total_final:.2f}"


class FeedbackChoice(models.TextChoices):
    NOT_INTERESTED = "not_interested", _("Not interested")
    NOT_MY_STYLE = "not_my_style", _("Not my style")
    ALREADY_OWN = "already_own", _("Already own")
    TOO_EXPENSIVE = "too_expensive", _("Too expensive")
    WRONG_CATEGORY = "wrong_category", _("Wrong category")


class RecommendationFeedback(models.Model):
    """User feedback on a recommendation.

    Allows users to indicate interest or disinterest in recommended products.
    Feedback is persisted as a lightweight signal so the engine can re-rank
    on the next request.

    Design goals:
    - Minimal data captured (no behavioral profiling beyond the explicit choice).
    - Always tied to a specific recommendation context (type + run ID).
    - Ownership-scoped: a user can only own their own feedback.
    """

    # ---- Identity ------------------------------------------------------------

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recommendation_feedback",
        verbose_name=_("user"),
    )

    # The recommendation run this feedback refers to. This lets us batch-process
    # feedback without exposing internal score IDs.
    recommendation_run_id = models.CharField(
        _("recommendation run id"),
        max_length=64,
        help_text=_(
            "Identifier for the recommendation query this feedback refers to. "
            "Allows batch invalidation or re-scoring."
        ),
    )

    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.PROTECT,
        verbose_name=_("product"),
    )

    choice = models.CharField(
        _("choice"),
        max_length=32,
        choices=FeedbackChoice.choices,
    )

    # ---- Audit ------------------------------------------------------------

    given_at = models.DateTimeField(_("given at"), auto_now_add=True)

    def __str__(self) -> str:
        return f"Feedback {self.choice} on product {self.product_id} by user {self.user_id} run {self.recommendation_run_id}"

    class Meta:
        verbose_name = _("Recommendation Feedback")
        verbose_name_plural = _("Recommendation Feedback")
        ordering = ("-given_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["user", "recommendation_run_id", "product"],
                name="unique_user_feedback_per_run_product",
            ),
        ]