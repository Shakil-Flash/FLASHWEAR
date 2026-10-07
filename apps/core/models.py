"""Site-wide configuration.

A single, editable row that drives global branding, SEO defaults and the announcement
bar. Kept in the database (not settings) so operations can change copy without a deploy.

Future phase note: feature flags, maintenance windows and experiment toggles can be added
here as additional boolean columns rather than introducing a second config store.
"""

from __future__ import annotations

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.validators import validate_catalog_image
from apps.core.services.content import invalidate_homepage_cache, validate_cta_destination


class SiteConfiguration(models.Model):
    """Singleton row holding global, non-secret site configuration."""

    SINGLETON_PK = 1
    CACHE_KEY = "core:site-configuration"
    CACHE_TTL = 300

    site_name = models.CharField(
        _("site name"),
        max_length=80,
        default="FLASHWEAR",
    )
    tagline = models.CharField(
        _("tagline"),
        max_length=140,
        default="Fashion Beyond Ordinary.",
    )
    announcement = models.CharField(
        _("announcement bar text"),
        max_length=200,
        blank=True,
        help_text=_("Leave empty to hide the announcement bar."),
    )
    maintenance_mode = models.BooleanField(
        _("maintenance mode"),
        default=False,
        help_text=_("When enabled the storefront shows a maintenance notice."),
    )
    default_seo_title = models.CharField(
        _("default SEO title"),
        max_length=70,
        default="FLASHWEAR | Fashion Beyond Ordinary",
    )
    default_seo_description = models.CharField(
        _("default SEO description"),
        max_length=160,
        default=(
            "FLASHWEAR is a fashion-tech storefront. Shop the drop, build your outfit "
            "and get styled by AI."
        ),
    )
    support_email = models.EmailField(
        _("support email"),
        max_length=254,
        default="support@flashwear.local",
    )
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("site configuration")
        verbose_name_plural = _("site configuration")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(pk=1),
                name="core_siteconfiguration_singleton",
            )
        ]

    def __str__(self) -> str:
        return self.site_name

    def save(self, *args, **kwargs) -> None:
        """Force the singleton primary key and drop any cached copy."""
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    def delete(self, *args, **kwargs):
        """Refuse deletion so the storefront always has configuration available."""
        raise ValueError("The site configuration row cannot be deleted.")

    @classmethod
    def load(cls) -> SiteConfiguration | None:
        """Return the cached singleton, or ``None`` before the first migration."""
        cached = cache.get(cls.CACHE_KEY)
        if cached is not None:
            return cached
        try:
            instance = cls.objects.get(pk=cls.SINGLETON_PK)
        except cls.DoesNotExist:
            return None
        cache.set(cls.CACHE_KEY, instance, cls.CACHE_TTL)
        return instance

    @classmethod
    def ensure(cls) -> SiteConfiguration:
        """Return the singleton, creating it with defaults when missing."""
        instance = cls.objects.filter(pk=cls.SINGLETON_PK).first()
        if instance is None:
            instance = cls.objects.create()
        return instance


class Campaign(models.Model):
    """Seasonal marketing campaign (e.g., Autumn Edit, Neon Cyber Drop).

    Controls the scheduled visibility of homepage sections and editorial stories.
    """

    title = models.CharField(_("title"), max_length=120)
    slug = models.SlugField(_("slug"), max_length=120, unique=True)
    description = models.TextField(_("description"), blank=True)
    starts_at = models.DateTimeField(_("starts at"))
    ends_at = models.DateTimeField(_("ends at"))
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_("Campaign must be active and within the date window to display."),
    )
    banner_image = models.ImageField(
        _("banner image"),
        upload_to="content/campaigns/",
        blank=True,
        validators=[validate_catalog_image],
    )
    banner_image_url = models.URLField(_("banner image URL"), blank=True)
    banner_alt = models.CharField(_("banner alt text"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("campaign")
        verbose_name_plural = _("campaigns")
        ordering = ["-starts_at", "title"]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs) -> None:
        super().save(*args, **kwargs)
        invalidate_homepage_cache()

    def delete(self, *args, **kwargs):
        res = super().delete(*args, **kwargs)
        invalidate_homepage_cache()
        return res

    def clean(self) -> None:
        super().clean()
        if self.starts_at and self.ends_at and self.starts_at > self.ends_at:
            raise ValidationError({"ends_at": _("End date must be after start date.")})

    def is_running(self, now=None) -> bool:
        """Return True if campaign is active and currently within scheduled dates."""
        current = now or timezone.now()
        return self.is_active and self.starts_at <= current <= self.ends_at

    def get_banner_url(self) -> str:
        if self.banner_image:
            return self.banner_image.url
        return self.banner_image_url or ""



class HomepageSection(models.Model):
    """Dynamic visual section rendered on the FLASHWEAR storefront homepage."""

    class SectionType(models.TextChoices):
        HERO = "hero", _("Hero")
        FEATURED_COLLECTION = "featured_collection", _("Featured Collection")
        NEW_ARRIVALS = "new_arrivals", _("New Arrivals")
        EDITORIAL = "editorial", _("Editorial Story")
        SHOP_BY_MOOD = "shop_by_mood", _("Shop by Mood")
        FEATURED_PRODUCTS = "featured_products", _("Featured Products")
        CREATOR = "creator", _("Creator / Community")
        COMPLETE_LOOK = "complete_look", _("Complete the Look")
        DROPS = "drops", _("Drops")
        BANNER = "banner", _("Promotional Banner")

    class OverlayPosition(models.TextChoices):
        CENTER = "center", _("Center")
        LEFT = "left", _("Left")
        RIGHT = "right", _("Right")
        BOTTOM = "bottom", _("Bottom")

    section_type = models.CharField(
        _("section type"),
        max_length=40,
        choices=SectionType.choices,
        default=SectionType.FEATURED_PRODUCTS,
    )
    title = models.CharField(_("title"), max_length=150)
    subtitle = models.CharField(_("subtitle / short description"), max_length=255, blank=True)
    is_enabled = models.BooleanField(
        _("enabled"),
        default=True,
        help_text=_("Only enabled sections are rendered on the storefront."),
    )
    display_order = models.PositiveIntegerField(
        _("display order"),
        default=0,
        help_text=_("Lower numbers appear higher on the page."),
    )
    starts_at = models.DateTimeField(
        _("starts at"),
        null=True,
        blank=True,
        help_text=_("Optional scheduled start visibility."),
    )
    ends_at = models.DateTimeField(
        _("ends at"),
        null=True,
        blank=True,
        help_text=_("Optional scheduled end visibility."),
    )
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sections",
        verbose_name=_("seasonal campaign"),
        help_text=_("If set, this section is only visible when the campaign is running."),
    )

    # Imagery / Hero fields
    desktop_image = models.ImageField(
        _("desktop image"),
        upload_to="content/sections/",
        blank=True,
        validators=[validate_catalog_image],
    )
    mobile_image = models.ImageField(
        _("mobile image"),
        upload_to="content/sections/",
        blank=True,
        validators=[validate_catalog_image],
    )
    desktop_image_url = models.URLField(_("desktop image URL"), blank=True)
    mobile_image_url = models.URLField(_("mobile image URL"), blank=True)
    image_alt = models.CharField(_("image alt text"), max_length=200, blank=True)
    overlay_position = models.CharField(
        _("overlay position"),
        max_length=20,
        choices=OverlayPosition.choices,
        default=OverlayPosition.CENTER,
    )

    # CTAs
    primary_cta_text = models.CharField(_("primary CTA text"), max_length=50, blank=True)
    primary_cta_url = models.CharField(
        _("primary CTA destination"),
        max_length=255,
        blank=True,
        help_text=_("Relative path (e.g. /catalog/) or full URL."),
    )
    secondary_cta_text = models.CharField(_("secondary CTA text"), max_length=50, blank=True)
    secondary_cta_url = models.CharField(
        _("secondary CTA destination"),
        max_length=255,
        blank=True,
        help_text=_("Relative path (e.g. /drops/) or full URL."),
    )

    # Linked entities for specific section types
    linked_collection = models.ForeignKey(
        "catalog.Collection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="homepage_sections",
        verbose_name=_("linked collection"),
    )
    linked_category = models.ForeignKey(
        "catalog.Category",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="homepage_sections",
        verbose_name=_("linked category"),
    )
    linked_drop = models.ForeignKey(
        "drops.FlashDrop",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="homepage_sections",
        verbose_name=_("linked drop"),
    )

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("homepage section")
        verbose_name_plural = _("homepage sections")
        ordering = ["display_order", "id"]

    def __str__(self) -> str:
        return f"{self.get_section_type_display()}: {self.title}"

    def save(self, *args, **kwargs) -> None:
        super().save(*args, **kwargs)
        invalidate_homepage_cache()

    def delete(self, *args, **kwargs):
        res = super().delete(*args, **kwargs)
        invalidate_homepage_cache()
        return res

    def clean(self) -> None:
        super().clean()
        if self.starts_at and self.ends_at and self.starts_at > self.ends_at:
            raise ValidationError({"ends_at": _("End date must be after start date.")})
        if self.primary_cta_url:
            validate_cta_destination(self.primary_cta_url)
        if self.secondary_cta_url:
            validate_cta_destination(self.secondary_cta_url)

    def is_visible(self, now=None) -> bool:
        """Evaluate if section is currently active, enabled and in-schedule."""
        if not self.is_enabled:
            return False
        current = now or timezone.now()
        if self.starts_at and current < self.starts_at:
            return False
        if self.ends_at and current > self.ends_at:
            return False
        if self.campaign and not self.campaign.is_running(current):
            return False
        return True

    def get_desktop_image_url(self) -> str:
        if self.desktop_image:
            return self.desktop_image.url
        return self.desktop_image_url or ""

    def get_mobile_image_url(self) -> str:
        if self.mobile_image:
            return self.mobile_image.url
        if self.mobile_image_url:
            return self.mobile_image_url
        return self.get_desktop_image_url()

    def get_active_products(self, now=None):
        """Retrieve pre-merchandised products ordered by display_order."""
        current = now or timezone.now()
        items = [sp for sp in self.section_products.all() if sp.is_visible(current)]
        return [
            sp.product
            for sp in items
            if getattr(sp.product, "is_available_for_purchase", True)
        ]



class SectionProduct(models.Model):
    """Merchandised product pinned to a homepage section."""

    section = models.ForeignKey(
        HomepageSection,
        on_delete=models.CASCADE,
        related_name="section_products",
        verbose_name=_("homepage section"),
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.CASCADE,
        related_name="merchandised_sections",
        verbose_name=_("product"),
    )
    display_order = models.PositiveIntegerField(_("display order"), default=0)
    starts_at = models.DateTimeField(_("starts at"), null=True, blank=True)
    ends_at = models.DateTimeField(_("ends at"), null=True, blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("section product")
        verbose_name_plural = _("section products")
        unique_together = ("section", "product")
        ordering = ["display_order", "id"]

    def __str__(self) -> str:
        return f"{self.section.title} -> {self.product.name}"

    def save(self, *args, **kwargs) -> None:
        super().save(*args, **kwargs)
        invalidate_homepage_cache()

    def delete(self, *args, **kwargs):
        res = super().delete(*args, **kwargs)
        invalidate_homepage_cache()
        return res

    def clean(self) -> None:
        super().clean()
        if self.starts_at and self.ends_at and self.starts_at > self.ends_at:
            raise ValidationError({"ends_at": _("End date must be after start date.")})

    def is_visible(self, now=None) -> bool:
        if not self.is_active:
            return False
        current = now or timezone.now()
        if self.starts_at and current < self.starts_at:
            return False
        if self.ends_at and current > self.ends_at:
            return False
        return True



class EditorialStory(models.Model):
    """Lightweight fashion story content (e.g., After Dark)."""

    title = models.CharField(_("title"), max_length=150)
    slug = models.SlugField(_("slug"), max_length=150, unique=True)
    subtitle = models.CharField(_("subtitle"), max_length=255, blank=True)
    story = models.TextField(
        _("story content"),
        blank=True,
        help_text=_("Short fashion story narrative."),
    )
    image = models.ImageField(
        _("editorial image"),
        upload_to="content/editorial/",
        blank=True,
        validators=[validate_catalog_image],
    )
    image_url = models.URLField(_("editorial image URL"), blank=True)
    image_alt = models.CharField(_("image alt text"), max_length=200, blank=True)
    primary_cta_text = models.CharField(_("CTA text"), max_length=50, blank=True)
    primary_cta_url = models.CharField(
        _("CTA destination"),
        max_length=255,
        blank=True,
        help_text=_("Relative path (e.g. /catalog/) or full URL."),
    )

    linked_collection = models.ForeignKey(
        "catalog.Collection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="editorial_stories",
        verbose_name=_("linked collection"),
    )
    linked_category = models.ForeignKey(
        "catalog.Category",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="editorial_stories",
        verbose_name=_("linked category"),
    )
    linked_products = models.ManyToManyField(
        "catalog.Product",
        blank=True,
        related_name="editorial_stories",
        verbose_name=_("linked products"),
    )
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="editorial_stories",
        verbose_name=_("campaign"),
    )

    display_order = models.PositiveIntegerField(_("display order"), default=0)
    is_published = models.BooleanField(_("published"), default=True)
    starts_at = models.DateTimeField(_("starts at"), null=True, blank=True)
    ends_at = models.DateTimeField(_("ends at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("editorial story")
        verbose_name_plural = _("editorial stories")
        ordering = ["display_order", "-created_at"]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs) -> None:
        super().save(*args, **kwargs)
        invalidate_homepage_cache()

    def delete(self, *args, **kwargs):
        res = super().delete(*args, **kwargs)
        invalidate_homepage_cache()
        return res

    def clean(self) -> None:
        super().clean()
        if self.starts_at and self.ends_at and self.starts_at > self.ends_at:
            raise ValidationError({"ends_at": _("End date must be after start date.")})
        if self.primary_cta_url:
            validate_cta_destination(self.primary_cta_url)

    def is_visible(self, now=None) -> bool:
        if not self.is_published:
            return False
        current = now or timezone.now()
        if self.starts_at and current < self.starts_at:
            return False
        if self.ends_at and current > self.ends_at:
            return False
        if self.campaign and not self.campaign.is_running(current):
            return False
        return True

    def get_image_url(self) -> str:
        if self.image:
            return self.image.url
        return self.image_url or ""


