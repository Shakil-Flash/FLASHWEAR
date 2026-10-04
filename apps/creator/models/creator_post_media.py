"""Creator post media model (Phase 12)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CreatorPostMedia(models.Model):
    """Media attached to a creator post.

    Separate model for deterministic ordering and primary/cover handling.
    """

    post = models.ForeignKey(
        "creator.CreatorPost",
        on_delete=models.CASCADE,
        related_name="media",
        verbose_name=_("post"),
    )
    image = models.ImageField(
        _("image"),
        upload_to="creator_posts/images/",
        help_text=_("Post image. Maximum 10MB. Validated by storage layer."),
    )
    alt_text = models.CharField(
        _("alt text"),
        max_length=250,
        blank=True,
        help_text=_("Accessibility alternative text for the image."),
    )
    sort_order = models.PositiveIntegerField(
        _("display order"),
        default=0,
        help_text=_("Lower numbers appear first. Determines primary image position."),
    )
    is_primary = models.BooleanField(
        _("is primary"),
        default=False,
        help_text=_("Mark the cover/primary image for the post."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("creator post media")
        verbose_name_plural = _("creator post media")
        ordering = ("sort_order", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["post", "sort_order"],
                name="unique_post_media_order",
            ),
        ]
        indexes = [
            models.Index(fields=["post", "sort_order"], name="post_media_order_idx"),
        ]

    def __str__(self) -> str:
        return f"Media {self.sort_order} for post {self.post_id}"
