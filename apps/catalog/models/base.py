"""Abstract building blocks shared by the catalogue models.

Three small abstract models rather than one large base class:

* :class:`TimestampedModel` -- the ``created_at`` / ``updated_at`` pair every row carries.
* :class:`SEOMixin` -- optional per-row search-engine metadata. Empty means "derive it", which is
  what :attr:`apps.catalog.models.Product.seo_title` and its siblings do.
* :class:`SluggedModel` -- a name plus a unique slug, an active flag and a display order.

They deliberately stay in the catalogue app. Nothing else in the project needs them yet, and
promoting one to ``apps.core`` later is a one-line import change, not a migration: abstract bases
contribute their fields to each concrete model independently.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class TimestampedModel(models.Model):
    """``created_at`` / ``updated_at``, declared once."""

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        abstract = True


class SEOMixin(models.Model):
    """Per-row ``<title>`` / meta description overrides.

    Both fields are optional on purpose: hand-written SEO copy is a merchandising task that most
    products never get round to, and a blank default that resolves to something sensible is better
    than an empty tag. :mod:`apps.catalog.seo` is what fills the gaps.
    """

    seo_title = models.CharField(
        _("SEO title"),
        max_length=70,
        blank=True,
        help_text=_("Falls back to the product/page name. Aim for under 70 characters."),
    )
    seo_description = models.CharField(
        _("SEO description"),
        max_length=160,
        blank=True,
        help_text=_("Falls back to the short description. Aim for under 160 characters."),
    )

    class Meta:
        abstract = True


class SluggedModel(TimestampedModel, SEOMixin):
    """A named, slugsed, switchable catalogue entry.

    ``display_order`` exists because fashion taxonomy is merchandised by hand: an admin drags
    "T-Shirts" above "Shirts" and the storefront follows. It is presentation only -- nothing
    filters on it.

    A **blank** slug is derived from the name and disambiguated ("relaxed", "relaxed-2"). A slug the
    operator typed is never rewritten: these are public URLs, and silently changing one breaks every
    inbound link to it.
    """

    name = models.CharField(_("name"), max_length=120)
    slug = models.SlugField(_("slug"), max_length=140, unique=True)
    description = models.TextField(_("description"), blank=True)
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_("Inactive rows stay in the database but disappear from the storefront."),
    )
    display_order = models.PositiveIntegerField(
        _("display order"),
        default=0,
        help_text=_("Lower numbers appear first."),
    )

    class Meta:
        abstract = True
        ordering = ("display_order", "name")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        # Imported here because ``apps.catalog.slugs`` imports models for type hints; a module-level
        # import would be circular.
        if not self.slug:
            from apps.catalog.slugs import resolve_slug

            self.slug = resolve_slug(
                type(self),
                self,
                max_length=self._meta.get_field("slug").max_length,
            )
        super().save(*args, **kwargs)
