"""Content and merchandising studio service for homepage visual presentation.

Handles:
- Section retrieval with scheduling, campaign awareness, and caching.
- Efficient prefetching of linked products, collections, categories, and drops.
- Cache invalidation upon content mutations.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger("flashwear.content")

HOMEPAGE_SECTIONS_CACHE_KEY = "core:homepage_sections:v1"
HOMEPAGE_EDITORIAL_CACHE_KEY = "core:homepage_editorial:v1"
HOMEPAGE_CAMPAIGNS_CACHE_KEY = "core:homepage_campaigns:v1"
CACHE_TTL = 300  # 5 minutes

DISALLOWED_URL_SCHEMES = frozenset({"javascript", "data", "vbscript", "file"})


def validate_cta_destination(url: str | None) -> None:
    """Validate CTA destination URL to prevent open redirects and script injection.

    Allows safe relative paths (/catalog/, /drops/) or full http/https URLs.
    Rejects javascript:, data:, vbscript:, protocol-relative (//example.com), and malformed URLs.
    """
    if not url:
        return

    cleaned = url.strip()
    if not cleaned:
        return

    if any(ch in cleaned for ch in ("\r", "\n", "\t", " ")):
        raise ValidationError(_("CTA destination URL cannot contain whitespace or newlines."))

    # Reject protocol-relative URLs (e.g. //evil.com)
    if cleaned.startswith("//"):
        raise ValidationError(_("Protocol-relative URLs starting with '//' are not allowed."))

    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()

    if scheme in DISALLOWED_URL_SCHEMES:
        raise ValidationError(
            _("The URL scheme '%(scheme)s' is not allowed."),
            params={"scheme": scheme},
        )

    if scheme and scheme not in ("http", "https"):
        raise ValidationError(
            _("Only HTTP, HTTPS, or relative site paths starting with '/' are permitted.")
        )

    # If no scheme, it must be a relative path starting with /
    if not scheme and not cleaned.startswith("/"):
        raise ValidationError(_("Relative destinations must start with '/' (e.g. '/catalog/')."))




def invalidate_homepage_cache() -> None:
    """Clear all cached storefront homepage configuration and section data."""
    try:
        cache.delete(HOMEPAGE_SECTIONS_CACHE_KEY)
        cache.delete(HOMEPAGE_EDITORIAL_CACHE_KEY)
        cache.delete(HOMEPAGE_CAMPAIGNS_CACHE_KEY)
    except Exception:
        logger.exception("Failed to invalidate homepage cache.")


def get_active_homepage_sections(
    now=None,
    *,
    include_unpublished: bool = False,
) -> list[Any]:
    """Retrieve ordered homepage sections for rendering or preview.

    When include_unpublished is False, only live, enabled, and scheduled sections
    are returned, using cache to keep homepage loading fast and query-budget clean.
    """
    current_time = now or timezone.now()

    if not include_unpublished:
        cached = cache.get(HOMEPAGE_SECTIONS_CACHE_KEY)
        if cached is not None:
            return cached

    from apps.core.models import HomepageSection

    queryset = (
        HomepageSection.objects.all()
        .select_related("campaign", "linked_collection", "linked_category", "linked_drop")
        .prefetch_related(
            "section_products",
            "section_products__product",
            "section_products__product__brand",
            "section_products__product__images",
            "section_products__product__variants",
        )
        .order_by("display_order", "id")
    )

    if not include_unpublished:
        queryset = queryset.filter(is_enabled=True)

    sections: list[HomepageSection] = []
    for section in queryset:
        if include_unpublished or section.is_visible(current_time):
            sections.append(section)

    if not include_unpublished:
        cache.set(HOMEPAGE_SECTIONS_CACHE_KEY, sections, CACHE_TTL)

    return sections


def get_active_editorial_stories(
    limit: int = 6,
    now=None,
    *,
    include_unpublished: bool = False,
) -> list[Any]:
    """Retrieve published editorial stories."""
    current_time = now or timezone.now()

    from apps.core.models import EditorialStory

    queryset = (
        EditorialStory.objects.all()
        .select_related("linked_collection", "linked_category", "campaign")
        .prefetch_related(
            "linked_products",
            "linked_products__brand",
            "linked_products__images",
            "linked_products__variants",
        )
        .order_by("display_order", "-created_at")
    )

    if not include_unpublished:
        queryset = queryset.filter(is_published=True)

    stories = [s for s in queryset if include_unpublished or s.is_visible(current_time)]
    return stories[:limit]


def get_active_campaigns(now=None) -> list[Any]:
    """Retrieve currently running seasonal marketing campaigns."""
    current_time = now or timezone.now()

    from apps.core.models import Campaign

    return list(
        Campaign.objects.filter(
            is_active=True,
            starts_at__lte=current_time,
            ends_at__gte=current_time,
        ).order_by("-starts_at")
    )
