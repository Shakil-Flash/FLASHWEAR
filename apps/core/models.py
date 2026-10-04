"""Site-wide configuration.

A single, editable row that drives global branding, SEO defaults and the announcement
bar. Kept in the database (not settings) so operations can change copy without a deploy.

Future phase note: feature flags, maintenance windows and experiment toggles can be added
here as additional boolean columns rather than introducing a second config store.
"""

from __future__ import annotations

from django.core.cache import cache
from django.db import models
from django.utils.translation import gettext_lazy as _


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
