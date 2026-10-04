"""Creator post model (Phase 12)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CreatorPostStatus(models.TextChoices):
    """Creator post publication states."""

    DRAFT = "draft", _("Draft")
    PENDING_REVIEW = "pending_review", _("Pending review")
    PUBLISHED = "published", _("Published")
    REJECTED = "rejected", _("Rejected")
    ARCHIVED = "archived", _("Archived")


class CreatorPost(models.Model):
    """A creator's fashion content post.

    A post contains text, media, and optional product/outfit tagging.
    The creator owns the post; only the creator or staff can modify it.
    """

    creator = models.ForeignKey(
        "creator.CreatorProfile",
        on_delete=models.CASCADE,
        related_name="posts",
        verbose_name=_("creator"),
    )
    title = models.CharField(
        _("title"),
        max_length=300,
        help_text=_("Post headline. Shown on profile and discovery pages."),
    )
    slug = models.SlugField(
        _("slug"),
        max_length=300,
        unique=True,
        help_text=_("URL-safe identifier for the post page."),
    )
    caption = models.TextField(
        _("caption"),
        blank=True,
        help_text=_("Post body text. Supports basic formatting."),
    )
    cover_image = models.ImageField(
        _("cover image"),
        upload_to="creator_posts/covers/",
        blank=True,
        null=True,
        help_text=_("Primary image for the post. Recommended: 1200x628px."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=CreatorPostStatus.choices,
        default=CreatorPostStatus.DRAFT,
        help_text=_("Derived from publication + moderation workflow."),
    )
    published_at = models.DateTimeField(
        _("published at"),
        null=True,
        blank=True,
        help_text=_("When this post was first published."),
    )
    scheduled_at = models.DateTimeField(
        _("scheduled at"),
        null=True,
        blank=True,
        help_text=_("When this post is scheduled to go live."),
    )
    view_count = models.PositiveIntegerField(
        _("view count"),
        default=0,
        help_text=_("Analytics-ready counter. Incremented via service."),
    )
    like_count = models.PositiveIntegerField(
        _("like count"),
        default=0,
        help_text=_("Analytics-ready counter. Managed via like service."),
    )
    save_count = models.PositiveIntegerField(
        _("save count"),
        default=0,
        help_text=_("Analytics-ready counter. Managed via save service."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("creator post")
        verbose_name_plural = _("creator posts")
        ordering = ("-published_at", "-created_at")
        indexes = [
            models.Index(fields=["creator"], name="creator_post_creator_idx"),
            models.Index(fields=["status"], name="creator_post_status_idx"),
            models.Index(fields=["published_at"], name="creator_post_published_idx"),
            models.Index(fields=["creator", "status"], name="creator_post_creat_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} by {self.creator.display_name}"

    def get_absolute_url(self) -> str:
        return f"/inspiration/{self.slug}/"

    @property
    def is_draft(self) -> bool:
        return self.status == CreatorPostStatus.DRAFT

    @property
    def is_pending_review(self) -> bool:
        return self.status == CreatorPostStatus.PENDING_REVIEW

    @property
    def is_published(self) -> bool:
        return self.status == CreatorPostStatus.PUBLISHED

    @property
    def is_rejected(self) -> bool:
        return self.status == CreatorPostStatus.REJECTED

    @property
    def is_archived(self) -> bool:
        return self.status == CreatorPostStatus.ARCHIVED

    @property
    def can_be_edited_by(self, user) -> bool:
        """Check if this user can edit this post."""
        if not user or not user.is_authenticated:
            return False
        return self.creator.user == user or user.is_staff

    @property
    def can_be_published(self) -> bool:
        """Check if this post can enter the publication workflow."""
        return self.status in [CreatorPostStatus.DRAFT, CreatorPostStatus.PENDING_REVIEW]

    @property
    def can_be_rejected(self) -> bool:
        """Check if this post can be rejected."""
        return self.status in [
            CreatorPostStatus.DRAFT,
            CreatorPostStatus.PENDING_REVIEW,
        ]
