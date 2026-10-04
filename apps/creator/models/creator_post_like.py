"""Creator post like model (Phase 12)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CreatorPostLike(models.Model):
    """Like on a creator post.

    Rules:
    - authenticated users only
    - one like per user/post
    - unique database constraint
    - unlike supported
    - cannot like unpublished content
    """

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="creator_post_likes",
        verbose_name=_("user"),
    )
    post = models.ForeignKey(
        "creator.CreatorPost",
        on_delete=models.CASCADE,
        related_name="likes",
        verbose_name=_("post"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("creator post like")
        verbose_name_plural = _("creator post likes")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "post"],
                name="one_like_per_user_post",
            ),
        ]
        indexes = [
            models.Index(fields=["user"], name="like_user_idx"),
            models.Index(fields=["post"], name="like_post_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} liked post {self.post_id}"
