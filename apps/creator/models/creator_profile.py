"""Creator profile and application models (Phase 12)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CreatorStatus(models.TextChoices):
    """Creator account states."""

    PENDING = "pending", _("Pending application")
    APPROVED = "approved", _("Approved creator")
    SUSPENDED = "suspended", _("Suspended")
    REJECTED = "rejected", _("Rejected application")


class CreatorProfile(models.Model):
    """Creator profile associated with a User.

    A normal customer is NOT automatically a creator.
    One profile per user; creation is controlled via application workflow.
    """

    user = models.OneToOneField(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="creator_profile",
        verbose_name=_("user"),
    )
    display_name = models.CharField(
        _("display name"),
        max_length=200,
        blank=True,
        help_text=_("Public-facing name. Blank uses the user's name."),
    )
    slug = models.SlugField(
        _("slug"),
        max_length=200,
        unique=True,
        help_text=_("URL-safe identifier for the creator profile page."),
    )
    bio = models.TextField(
        _("biography"),
        blank=True,
        help_text=_("Public biography displayed on profile."),
    )
    avatar = models.ImageField(
        _("avatar"),
        upload_to="creators/avatars/",
        blank=True,
        null=True,
        help_text=_("Profile image. Recommended: 400x400px."),
    )
    cover_image = models.ImageField(
        _("cover image"),
        upload_to="creators/covers/",
        blank=True,
        null=True,
        help_text=_("Header image. Recommended: 1200x400px."),
    )
    website = models.URLField(
        _("website"),
        blank=True,
        help_text=_("Personal or brand website URL."),
    )
    instagram_handle = models.CharField(
        _("Instagram handle"),
        max_length=150,
        blank=True,
    )
    tiktok_handle = models.CharField(
        _("TikTok handle"),
        max_length=150,
        blank=True,
    )
    location = models.CharField(
        _("location"),
        max_length=200,
        blank=True,
        help_text=_("Display location only. Not stored publicly beyond this."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=CreatorStatus.choices,
        default=CreatorStatus.PENDING,
        help_text=_("Derived from application workflow."),
    )
    is_featured = models.BooleanField(
        _("featured"),
        default=False,
        help_text=_("Staff-only flag for featured creators."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("creator profile")
        verbose_name_plural = _("creator profiles")
        indexes = [
            models.Index(fields=["user"], name="creator_user_idx"),
            models.Index(fields=["status"], name="creator_status_idx"),
        ]

    def __str__(self) -> str:
        return self.display_name or self.user.email

    def get_absolute_url(self) -> str:
        return f"/creators/{self.slug}/"

    @property
    def is_approved(self) -> bool:
        return self.status == CreatorStatus.APPROVED

    @property
    def is_pending(self) -> bool:
        return self.status == CreatorStatus.PENDING

    @property
    def is_rejected(self) -> bool:
        return self.status == CreatorStatus.REJECTED

    @property
    def is_suspended(self) -> bool:
        return self.status == CreatorStatus.SUSPENDED


