"""Creator post save model (Phase 12)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CreatorPostSave(models.Model):
    """Save/bookmark a creator post.

    Rules:
    - authenticated users only
    - one save per user/post
    - unique constraint
    - toggle behavior
    - cannot save unpublished content
    - private save state must never be publicly visible
    """

    class Meta:
        verbose_name = _("creator post save")
        verbose_name_plural = _("creator post saves")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "post"],
                name="one_save_per_user_post",
            ),
        ]
        indexes = [
            models.Index(fields=["user"], name="save_user_idx"),
            models.Index(fields=["post"], name="save_post_idx"),
        ]

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="creator_post_saves",
        verbose_name=_("user"),
    )
    post = models.ForeignKey(
        "creator.CreatorPost",
        on_delete=models.CASCADE,
        related_name="saves",
        verbose_name=_("post"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.user.email} saved post {self.post_id}"