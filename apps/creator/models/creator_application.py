"""Creator application model (Phase 12)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CreatorStatus(models.TextChoices):
    """Creator account states."""

    PENDING = "pending", _("Pending application")
    APPROVED = "approved", _("Approved creator")
    SUSPENDED = "suspended", _("Suspended")
    REJECTED = "rejected", _("Rejected application")


class CreatorApplication(models.Model):
    """Application to become a creator.

    Rules:
    - authenticated users only
    - one active application per user
    - suspended creators cannot create a new application unless explicitly allowed
    - approval creates/enables the CreatorProfile
    - rejection does not expose internal reviewer notes to the applicant
    """

    applicant = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="creator_applications",
        verbose_name=_("applicant"),
    )
    application_bio = models.TextField(
        _("application biography"),
        help_text=_("Free-form text from the applicant about their creator vision."),
    )
    requested_display_name = models.CharField(
        _("requested display name"),
        max_length=200,
        help_text=_("Display name requested by the applicant."),
    )
    social_links = models.JSONField(
        _("social links"),
        default=dict,
        blank=True,
        help_text=_("JSON dict of social platform handles/URLs."),
    )
    portfolio_url = models.URLField(
        _("portfolio URL"),
        blank=True,
        verify_exists=False,
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=CreatorStatus.choices,
        default=CreatorStatus.PENDING,
    )
    reviewer = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="creator_application_reviews",
        verbose_name=_("reviewer"),
        help_text=_("Staff user who performed the review."),
    )
    reviewer_note = models.TextField(
        _("reviewer note"),
        blank=True,
        help_text=_("Internal moderation note. Not shown to applicant on rejection."),
    )
    submitted_at = models.DateTimeField(_("submitted at"), auto_now_add=True)
    reviewed_at = models.DateTimeField(
        _("reviewed at"),
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("creator application")
        verbose_name_plural = _("creator applications")
        indexes = [
            models.Index(fields=["applicant"], name="creator_app_user_idx"),
            models.Index(fields=["status"], name="creator_app_status_idx"),
            models.Index(fields=["reviewer"], name="creator_app_reviewer_idx"),
        ]

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    def __str__(self) -> str:
        return f"Application by {self.applicant.email} — {self.status}"

    def save(self, *args, **kwargs):
        """Ensure status consistency."""
        super().save(*args, **kwargs)

    def clean(self):
        """Validate application state."""
        if self.status == CreatorStatus.REJECTED and not self.reviewer:
            raise models.ValidationError(
                {"reviewer": "A reviewer must be set for rejected applications."}
            )
