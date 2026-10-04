"""Creator post report model (Phase 12)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CreatorPostReport(models.Model):
    """Report inappropriate creator content.

    Rules:
    - authenticated users only
    - one active report per user/post/reason
    - creator cannot report their own content
    - report details must not become public
    - moderators can review reports
    """

    class Reason(models.TextChoices):
        SPAM = "spam", _("Spam")
        MISLEADING = "misleading", _("Misleading")
        INAPPROPRIATE = "inappropriate", _("Inappropriate")
        COPYRIGHT = "copyright", _("Copyright")
        HARASSMENT = "harassment", _("Harassment")
        OTHER = "other", _("Other")

    class Status(models.TextChoices):
        NEW = "new", _("New")
        UNDER_REVIEW = "under_review", _("Under review")
        RESOLVED = "resolved", _("Resolved")

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="creator_post_reports",
        verbose_name=_("reporter"),
    )
    post = models.ForeignKey(
        "creator.CreatorPost",
        on_delete=models.CASCADE,
        related_name="reports",
        verbose_name=_("post"),
    )
    reason = models.CharField(
        _("reason"),
        max_length=16,
        choices=Reason.choices,
    )
    details = models.TextField(
        _("details"),
        blank=True,
        help_text=_("Free-form reporter description. Not made public."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.NEW,
    )
    resolved_at = models.DateTimeField(
        _("resolved at"),
        null=True,
        blank=True,
    )
    resolved_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="creator_reports_resolved",
        verbose_name=_("resolved by"),
    )

    class Meta:
        verbose_name = _("creator post report")
        verbose_name_plural = _("creator post reports")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "post", "reason"],
                name="one_report_per_user_post_reason",
            ),
        ]
        indexes = [
            models.Index(fields=["post"], name="report_post_idx"),
            models.Index(fields=["status"], name="report_status_idx"),
        ]

    def __str__(self) -> str:
        return f"Report by {self.user.email} on post {self.post_id} — {self.reason}"
