"""SEO metadata for catalogue pages.

Kept out of the views so the fallback rules live in exactly one place and can be tested without
rendering a template.

The rule everywhere: **an explicit field wins, otherwise derive something sensible**. Hand-written
SEO copy is optional for almost every product, and a blank ``<title>`` is worse than a derived one.

Currency note: Phase 3 prices are plain ``Decimal`` amounts with no currency column (see the README
decision log). ``CATALOG_CURRENCY_CODE`` names the storefront's display currency; a customer can
still express a preference in their profile from Phase 2.
"""

from __future__ import annotations

from django.conf import settings
from django.urls import reverse

from apps.catalog.models import Product
from apps.core.context_processors import get_site_configuration

ELLIPSIS = "…"


def _absolute(request, path: str) -> str:
    return request.build_absolute_uri(path) if request is not None else path


def _site_name() -> str:
    """The configured site name, degrading to the model default if the row is missing."""
    return get_site_configuration().site_name


def _page_title(value: str) -> str:
    return f"{value} | {_site_name()}"


def _image_of(obj):
    """The most useful image on a taxonomy object, whichever field it happens to use."""
    for field in ("hero_image", "banner_image", "image", "logo"):
        image = getattr(obj, field, None)
        if image:
            return image
    return None


def product_metadata(product: Product, *, request=None) -> dict:
    """Title, description, canonical URL and Open Graph fields for a product page."""
    title = product.seo_title or _page_title(product.name)
    # Each fallback is truncated to the length it will actually be rendered at, because
    # ``_truncate`` needs the limit: a product with no short description would otherwise raise
    # TypeError inside the template.
    description = (
        product.seo_description
        or product.short_description
        or _truncate(product.description, 160)
        or f"Shop {product.name} from the FLASHWEAR catalogue."
    )
    image = product.primary_image
    canonical = _absolute(request, product.get_absolute_url())

    return {
        "seo_title": _truncate(title, 70),
        "seo_description": _truncate(description, 160),
        "canonical_url": canonical,
        "og_title": _truncate(product.seo_title or product.name, 95),
        "og_description": _truncate(description, 200),
        "og_type": "product",
        "og_image": _absolute(request, image.image.url) if image else "",
        "og_url": canonical,
        "seo_robots": "index, follow",
    }


def taxonomy_metadata(
    obj, *, request=None, kind: str = "website", fallback_description: str = ""
) -> dict:
    """Metadata for a category, collection or brand page.

    ``fallback_description`` lets a view supply a generated sentence (a product count, say) instead
    of the generic one, which is what makes an unconfigured category page still worth indexing.
    """
    title = obj.seo_title or _page_title(obj.name)
    description = (
        obj.seo_description
        or _truncate(obj.description, 160)
        or fallback_description
        or f"Browse {obj.name} at {_site_name()}."
    )
    image = _image_of(obj)
    canonical = _absolute(request, obj.get_absolute_url())

    return {
        "seo_title": _truncate(title, 70),
        "seo_description": _truncate(description, 160),
        "canonical_url": canonical,
        "og_title": _truncate(obj.name, 95),
        "og_description": _truncate(description, 200),
        "og_type": kind,
        "og_image": _absolute(request, image.url) if image else "",
        "og_url": canonical,
        "seo_robots": "index, follow",
    }


def listing_metadata(
    title: str, description: str, *, request=None, path: str | None = None
) -> dict:
    """Metadata for the paginated listing pages."""
    canonical = _absolute(request, path or reverse("catalog:product-list"))
    return {
        "seo_title": _truncate(title, 70),
        "seo_description": _truncate(description, 160),
        "canonical_url": canonical,
        "og_title": _truncate(title, 95),
        "og_description": _truncate(description, 200),
        "og_type": "website",
        "og_image": "",
        "og_url": canonical,
        # Paginated listings stay indexed, but the query permutations (?sort=price_asc, ?page=3)
        # are canonicalised onto the clean path: same products, different order, and indexing both
        # would have them compete with page one.
        "seo_robots": "index, follow",
    }


def product_schema(product: Product, *, request=None, aggregate: dict | None = None) -> dict:
    """schema.org ``Product`` JSON-LD for a product page.

    Kept to facts the catalogue actually holds. ``offers`` is only emitted when a purchasable
    variant with a price exists, and reports the variant price range as the specification allows:
    advertising one price the customer cannot buy at would be a lie in structured data.

    ``aggregate`` (Phase 7) is the *published* review summary from
    ``apps.engagement.services.reviews.aggregate_for``. ``aggregateRating`` is only emitted when
    at least one published review exists -- claiming a rating of zero reviews is worse than
    saying nothing.

    Availability is deliberately absent from ``offers``: stock comes and goes, and this payload
    must not promise a quantity the catalogue cannot keep.
    """
    url = _absolute(request, product.get_absolute_url())
    variants = product.purchasable_variants
    low, high = product.price_range

    payload: dict = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product.name,
        "url": url,
    }
    if product.short_description:
        payload["description"] = product.short_description
    elif product.description:
        payload["description"] = _truncate(product.description, 500)
    if product.brand_id:
        payload["brand"] = {"@type": "Brand", "name": product.brand.name}
    if product.category_id:
        payload["category"] = product.category.name
    colors = product.available_colors
    if colors:
        payload["color"] = ", ".join(color.name for color in colors)

    image = product.primary_image
    if image:
        payload["image"] = [_absolute(request, image.image.url)]

    if low is not None:
        payload["offers"] = {
            "@type": "AggregateOffer",
            "priceCurrency": settings.CATALOG_CURRENCY_CODE,
            "lowPrice": f"{low:.2f}",
            "highPrice": f"{(high if high is not None else low):.2f}",
            "offerCount": len(variants),
            "url": url,
        }

    if aggregate and aggregate.get("count"):
        payload["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": str(aggregate["average"]),
            "reviewCount": str(aggregate["count"]),
            "bestRating": "5",
            "worstRating": "1",
        }

    return payload


def breadcrumb_schema(trail: list[dict]) -> dict:
    """schema.org ``BreadcrumbList`` built from ``[{"name": ..., "url": ...}]`` entries."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": index,
                "name": item["name"],
                "item": item["url"],
            }
            for index, item in enumerate(trail, start=1)
        ],
    }


def _truncate(value: str, limit: int) -> str:
    """Trim to ``limit`` characters on a word boundary.

    Search engines truncate anyway; doing it here means the value in the tag is the value a crawler
    sees.
    """
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    clipped = text[: max(1, limit - 1)]
    if " " in clipped:
        clipped = clipped[: clipped.rfind(" ")]
    return f"{clipped.rstrip(' ,.;:-')}{ELLIPSIS}"
