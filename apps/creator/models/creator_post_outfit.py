"""Creator post outfit tagging model (Phase 12)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CreatorPostOutfit(models.Model):
    """Outfit tagging on a creator post.

    Integrates with Phase 8 Outfit model. Only appropriate/visible outfits
    may be attached. Private customer outfits must never become publicly
    visible through creator content.

    Uses PROTECT on outfit to prevent deletion if referenced by published posts.
    """

    class Meta:
        verbose_name = _("creator post outfit")
        verbose_name_plural = _("creator post outfits")
        constraints = [
            models.UniqueConstraint(
                fields=["post", "outfit"],
                name="unique_post_outfit_tag",
            ),
        ]
        indexes = [
            models.Index(fields=["post"], name="post_outfit_post_idx"),
            models.Index(fields=["outfit"], name="post_outfit_outfit_idx"),
        ]

    post = models.ForeignKey(
        "creator.CreatorPost",
        on_delete=models.CASCADE,
        related_name="outfit_tags",
        verbose_name=_("post"),
    )
    outfit = models.ForeignKey(
        "closet.Outfit",
        on_delete=models.PROTECT,
        related_name="creator_post_tags",
        verbose_name=_("outfit"),
    )
    display_order = models.PositiveIntegerField(
        _("display order"),
        default=0,
        help_text=_("Lower numbers appear first on the outfit card."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.outfit.name} tagged on post {self.post_id}"