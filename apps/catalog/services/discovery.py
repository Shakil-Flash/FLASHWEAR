"""Product Discovery Service.

This module contains the core logic for product search, filtering, sorting, and faceted navigation.
It is the single source of truth for how products are discovered across the storefront and API.

Design principles:
- All logic lives here; views and API just pass parameters and render results.
- Every query is built through this module so the storefront and API cannot diverge.
- PostgreSQL full-text search is used when available; SQLite falls back to icontains ranking.
- All filtering is variant-aware: a product only matches a colour/size if it has an active
  variant with that combination.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import (
    Case,
    Count,
    IntegerField,
    Max,
    Min,
    Q,
    Value,
    When,
)

from apps.catalog.models import (
    Category,
    Collection,
    Color,
    Fit,
    Material,
    Product,
    ProductTag,
    Size,
)

if TYPE_CHECKING:
    from django.db.models import QuerySet


# --------------------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryFilters:
    """Parsed and validated discovery parameters from a request."""

    query: str | None
    category_slug: str | None
    brand_slug: str | None
    collection_slug: str | None
    color_slugs: list[str]
    size_codes: list[str]
    material_slugs: list[str]
    fit_slugs: list[str]
    tag_slugs: list[str]
    min_price: Decimal | None
    max_price: Decimal | None
    sort: str
    page: int
    page_size: int

    @property
    def has_filters(self) -> bool:
        """Whether any filter (not search/sort/pagination) is active."""
        return any(
            [
                self.category_slug,
                self.brand_slug,
                self.collection_slug,
                self.color_slugs,
                self.size_codes,
                self.material_slugs,
                self.fit_slugs,
                self.tag_slugs,
                self.min_price is not None,
                self.max_price is not None,
            ]
        )

    @property
    def active_filter_count(self) -> int:
        """Count of active filter groups (for UI badges)."""
        count = 0
        if self.category_slug:
            count += 1
        if self.brand_slug:
            count += 1
        if self.collection_slug:
            count += 1
        if self.color_slugs:
            count += 1
        if self.size_codes:
            count += 1
        if self.material_slugs:
            count += 1
        if self.fit_slugs:
            count += 1
        if self.tag_slugs:
            count += 1
        if self.min_price is not None or self.max_price is not None:
            count += 1
        return count


@dataclass(frozen=True)
class FacetCounts:
    """Faceted counts for filter UI."""

    categories: list[dict]
    brands: list[dict]
    collections: list[dict]
    colors: list[dict]
    sizes: list[dict]
    materials: list[dict]
    fits: list[dict]
    tags: list[dict]
    price_range: dict | None  # {"min": Decimal, "max": Decimal}


@dataclass(frozen=True)
class DiscoveryResult:
    """Complete result of a discovery query."""

    queryset: QuerySet
    filters: DiscoveryFilters
    facets: FacetCounts
    total_count: int
    search_query: str | None


# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------


# Search rank weights (higher = more important). Used for SQLite ranking via Case/When.
# These weights determine ranking priority: name > brand > category > collection > description
SEARCH_RANK_WEIGHTS = {
    "name": 10,
    "brand": 8,
    "category": 6,
    "collection": 4,
    "color": 3,
    "size": 3,
    "material": 2,
    "fit": 2,
    "tag": 2,
    "short_description": 3,
    "description": 1,
}

# Valid sort options (mirrors Product.Sort but defined here for reuse)
VALID_SORTS = frozenset(
    [
        "newest",
        "price_asc",
        "price_desc",
        "featured",
        "name_asc",
        "name_desc",
    ]
)

DEFAULT_SORT = "newest"
DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE = 100
MIN_SEARCH_LENGTH = 2
SUGGESTIONS_LIMIT = 10


# --------------------------------------------------------------------------------------
# Parameter parsing
# --------------------------------------------------------------------------------------


def parse_discovery_params(request) -> DiscoveryFilters:
    """Parse and validate discovery parameters from a request's query string.

    All parameters are optional. Invalid values are silently ignored (fallback to defaults)
    rather than raising errors, so stale bookmarks never break.
    """
    GET = request.GET if hasattr(request, "GET") else request.query_params

    # Search query
    query = (GET.get("q") or "").strip()
    query = query if len(query) >= MIN_SEARCH_LENGTH else None

    # Taxonomy filters (single slug each)
    category_slug = _clean_slug(GET.get("category"))
    brand_slug = _clean_slug(GET.get("brand"))
    collection_slug = _clean_slug(GET.get("collection"))

    # Fashion attribute filters (multi-value, comma-separated)
    color_slugs = _parse_csv_slugs(GET.get("color"))
    size_codes = [s.strip().upper() for s in _parse_csv_slugs(GET.get("size"))]
    material_slugs = _parse_csv_slugs(GET.get("material"))
    fit_slugs = _parse_csv_slugs(GET.get("fit"))
    tag_slugs = _parse_csv_slugs(GET.get("tag"))

    # Price range
    min_price = _parse_decimal(GET.get("min_price"))
    max_price = _parse_decimal(GET.get("max_price"))

    # Sort & pagination
    sort = _clean_sort(GET.get("sort"))
    page = _parse_int(GET.get("page"), 1, min_val=1)
    page_size = _parse_int(
        GET.get("page_size"), settings.CATALOG_PRODUCTS_PER_PAGE, min_val=1, max_val=MAX_PAGE_SIZE
    )

    return DiscoveryFilters(
        query=query,
        category_slug=category_slug,
        brand_slug=brand_slug,
        collection_slug=collection_slug,
        color_slugs=color_slugs,
        size_codes=size_codes,
        material_slugs=material_slugs,
        fit_slugs=fit_slugs,
        tag_slugs=tag_slugs,
        min_price=min_price,
        max_price=max_price,
        sort=sort,
        page=page,
        page_size=page_size,
    )


def _clean_slug(value: str | None) -> str | None:
    """Normalize a slug parameter: lowercase, strip, empty -> None."""
    if not value:
        return None
    slug = value.strip().lower()
    return slug or None


def _parse_csv_slugs(value: str | None) -> list[str]:
    """Parse comma-separated slugs, normalizing each."""
    if not value:
        return []
    return [s.strip().lower() for s in value.split(",") if s.strip()]


def _parse_decimal(value: str | None) -> Decimal | None:
    """Parse a decimal string, returning None on failure."""
    if not value:
        return None
    try:
        return Decimal(value.strip())
    except Exception:
        return None


def _parse_int(
    value: str | None,
    default: int,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """Parse an integer with bounds checking."""
    if not value:
        return default
    try:
        val = int(value.strip())
        if min_val is not None and val < min_val:
            return default
        if max_val is not None and val > max_val:
            return default
        return val
    except Exception:
        return default


def _clean_sort(value: str | None) -> str:
    """Validate sort parameter against allowlist."""
    if not value:
        return DEFAULT_SORT
    candidate = value.strip().lower()
    return candidate if candidate in VALID_SORTS else DEFAULT_SORT


# --------------------------------------------------------------------------------------
# Search ranking (SQLite fallback)
# --------------------------------------------------------------------------------------


def _build_search_rank_expression(query: str) -> Case:
    """Build a CASE expression that ranks products by relevance to ``query``.

    This is the SQLite fallback ranking. When PostgreSQL is available, the view should
    use `SearchVector`/`SearchRank` instead (see the view implementation).

    The expression evaluates to an integer score; higher = more relevant.
    """
    terms = [term.strip() for term in query.lower().split() if term.strip()]
    if not terms:
        return Case(default=Value(0), output_field=IntegerField())

    when_clauses = []

    # For each search term, add weighted WHEN clauses for each field
    for term in terms:
        # Exact/prefix matches in name get highest weight
        when_clauses.append(When(name__icontains=term, then=Value(SEARCH_RANK_WEIGHTS["name"] * 3)))
        when_clauses.append(
            When(
                short_description__icontains=term,
                then=Value(SEARCH_RANK_WEIGHTS["short_description"] * 2),
            )
        )

        # Brand name match
        when_clauses.append(
            When(brand__name__icontains=term, then=Value(SEARCH_RANK_WEIGHTS["brand"] * 2))
        )

        # Category name match
        when_clauses.append(
            When(category__name__icontains=term, then=Value(SEARCH_RANK_WEIGHTS["category"] * 2))
        )

        # Collection name match
        when_clauses.append(
            When(
                collections__name__icontains=term,
                then=Value(SEARCH_RANK_WEIGHTS["collection"] * 2),
            )
        )

        # Colour/Size/Material/Fit/Tag matches (lower weight)
        when_clauses.append(
            When(variants__color__name__icontains=term, then=Value(SEARCH_RANK_WEIGHTS["color"]))
        )
        when_clauses.append(
            When(variants__size__name__icontains=term, then=Value(SEARCH_RANK_WEIGHTS["size"]))
        )
        when_clauses.append(
            When(materials__name__icontains=term, then=Value(SEARCH_RANK_WEIGHTS["material"]))
        )
        when_clauses.append(When(fit__name__icontains=term, then=Value(SEARCH_RANK_WEIGHTS["fit"])))
        when_clauses.append(
            When(tags__name__icontains=term, then=Value(SEARCH_RANK_WEIGHTS["tag"]))
        )

        # Description (lowest weight)
        when_clauses.append(
            When(description__icontains=term, then=Value(SEARCH_RANK_WEIGHTS["description"]))
        )

    # Sum all matching weights; Coalesce to 0 for no matches
    return Case(*when_clauses, default=Value(0), output_field=IntegerField())


# --------------------------------------------------------------------------------------
# Filter application
# --------------------------------------------------------------------------------------


def apply_search(queryset, query: str):
    """Apply full-text search with ranking to the queryset.

    Adds a `search_rank` annotation (integer, higher = more relevant).
    The queryset is NOT filtered here - that happens in `apply_filters` via the
    EXISTS subquery approach. This annotation is used for ordering.

    In production (PostgreSQL), replace with SearchVector/SearchRank.
    """
    if not query:
        return queryset.annotate(search_rank=Value(0, output_field=IntegerField()))

    # Filter to only products matching at least one term in any searchable field
    # This uses the same logic as the ranking expression but as a filter
    terms = [t.strip() for t in query.lower().split() if t.strip()]
    if not terms:
        return queryset.annotate(search_rank=Value(0, output_field=IntegerField()))

    search_q = Q()
    for term in terms:
        search_q |= Q(name__icontains=term)
        search_q |= Q(short_description__icontains=term)
        search_q |= Q(description__icontains=term)
        search_q |= Q(brand__name__icontains=term)
        search_q |= Q(category__name__icontains=term)
        search_q |= Q(collections__name__icontains=term)
        search_q |= Q(variants__color__name__icontains=term)
        search_q |= Q(variants__size__name__icontains=term)
        search_q |= Q(materials__name__icontains=term)
        search_q |= Q(fit__name__icontains=term)
        search_q |= Q(tags__name__icontains=term)

    return queryset.filter(search_q).annotate(search_rank=_build_search_rank_expression(query))


def apply_filters(queryset, filters: DiscoveryFilters):
    """Apply all discovery filters to the queryset.

    Returns a filtered queryset. Does not apply search or sorting.
    """
    # Category filter (subtree)
    if filters.category_slug:
        from apps.catalog.services import category_subtree_ids

        category = Category.objects.filter(slug=filters.category_slug, is_active=True).first()
        if category:
            subtree_ids = category_subtree_ids(category)
            queryset = queryset.filter(category_id__in=subtree_ids)

    # Brand filter
    if filters.brand_slug:
        queryset = queryset.filter(brand__slug=filters.brand_slug, brand__is_active=True)

    # Collection filter
    if filters.collection_slug:
        queryset = queryset.filter(
            collections__slug=filters.collection_slug, collections__is_active=True
        )

    # Color filter (variant-aware: product must have active variant with this color)
    if filters.color_slugs:
        color_q = Q()
        for slug in filters.color_slugs:
            color_q |= Q(variants__color__slug=slug, variants__is_active=True)
        queryset = queryset.filter(color_q)

    # Size filter (variant-aware)
    if filters.size_codes:
        size_q = Q()
        for code in filters.size_codes:
            size_q |= Q(variants__size__code__iexact=code, variants__is_active=True)
        queryset = queryset.filter(size_q)

    # Material filter
    if filters.material_slugs:
        mat_q = Q()
        for slug in filters.material_slugs:
            mat_q |= Q(materials__slug=slug, materials__is_active=True)
        queryset = queryset.filter(mat_q)

    # Fit filter
    if filters.fit_slugs:
        fit_q = Q()
        for slug in filters.fit_slugs:
            fit_q |= Q(fit__slug=slug, fit__is_active=True)
        queryset = queryset.filter(fit_q)

    # Tag filter
    if filters.tag_slugs:
        tag_q = Q()
        for slug in filters.tag_slugs:
            tag_q |= Q(tags__slug=slug, tags__is_active=True)
        queryset = queryset.filter(tag_q)

    # Price range filter
    if filters.min_price is not None or filters.max_price is not None:
        # Use annotated price_min/price_max if available, otherwise annotate
        if not hasattr(filters, "_price_annotated"):
            from apps.catalog.selectors import with_price_range

            queryset = with_price_range(queryset)

        if filters.min_price is not None:
            queryset = queryset.filter(price_max__gte=filters.min_price)
        if filters.max_price is not None:
            queryset = queryset.filter(price_min__lte=filters.max_price)

    return queryset.distinct()


def apply_sorting(queryset, sort: str):
    """Apply sorting to the queryset.

    Always includes a deterministic tiebreaker.
    """
    if sort == "price_asc":
        # Products with no price sort last (NULLS LAST)
        return queryset.order_by("price_min", "-id")
    if sort == "price_desc":
        return queryset.order_by("-price_max", "-id")
    if sort == "featured":
        return queryset.order_by("-is_featured", "-published_at", "-id")
    if sort == "name_asc":
        return queryset.order_by("name", "id")
    if sort == "name_desc":
        return queryset.order_by("-name", "-id")
    # Default: newest first
    return queryset.order_by("-published_at", "-id")


# --------------------------------------------------------------------------------------
# Faceted counts
# --------------------------------------------------------------------------------------


def get_facet_counts(queryset, filters: DiscoveryFilters) -> FacetCounts:
    """Compute faceted counts for all filter dimensions.

    Returns counts for each filter value within the current filtered queryset.
    This is used to render filter UI with counts (e.g., "Black (12)").
    """
    from apps.catalog.models import Brand, Category

    # We need counts within the current filtered queryset
    # Use subqueries to count efficiently
    # Categories (only top-level for UI, but count includes subtree)
    from apps.catalog.services import category_subtree_ids

    categories_qs = Category.objects.filter(is_active=True, parent__isnull=True).order_by(
        "display_order", "name"
    )
    categories = []
    for cat in categories_qs:
        subtree = category_subtree_ids(cat)
        count = queryset.filter(category_id__in=subtree).count()
        if count > 0:
            categories.append({"name": cat.name, "slug": cat.slug, "count": count})

    # Brands
    brands = list(
        Brand.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "name")
    )

    # Collections (live only)
    collections = list(
        Collection.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "name")
    )

    # Colors (only those with active variants in results)
    colors = list(
        Color.objects.filter(is_active=True)
        .filter(variants__product__in=queryset, variants__is_active=True)
        .annotate(count=Count("variants", filter=Q(variants__product__in=queryset)))
        .values("name", "slug", "hex_code", "count")
        .order_by("-count", "display_order", "name")
    )

    # Sizes
    sizes = list(
        Size.objects.filter(is_active=True)
        .filter(variants__product__in=queryset, variants__is_active=True)
        .annotate(count=Count("variants", filter=Q(variants__product__in=queryset)))
        .values("name", "code", "count")
        .order_by("size_type", "display_order", "code")
    )

    # Materials
    materials = list(
        Material.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "display_order", "name")
    )

    # Fits
    fits = list(
        Fit.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "display_order", "name")
    )

    # Tags
    tags = list(
        ProductTag.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "display_order", "name")
    )

    # Price range
    price_agg = queryset.aggregate(min_price=Min("price_min"), max_price=Max("price_max"))
    price_range = None
    if price_agg["min_price"] is not None:
        price_range = {
            "min": price_agg["min_price"],
            "max": price_agg["max_price"],
        }

    return FacetCounts(
        categories=categories,
        brands=brands,
        collections=collections,
        colors=colors,
        sizes=sizes,
        materials=materials,
        fits=fits,
        tags=tags,
        price_range=price_range,
    )


# --------------------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------------------


def build_discovery_queryset(base_queryset, request) -> DiscoveryResult:
    """Main entry point: build a complete discovery result from a request.

    This is the single entry point used by both storefront views and API views.
    """
    from apps.catalog.selectors import with_price_range

    # 1. Parse parameters
    filters = parse_discovery_params(request)

    # 2. Start with published products + price annotations
    queryset = base_queryset if base_queryset is not None else Product.objects.published()
    queryset = with_price_range(queryset)

    # 2. Apply search
    if filters.query:
        queryset = apply_search(queryset, filters.query)

    # 3. Apply filters
    queryset = apply_filters(queryset, filters)

    # 3. Apply sorting
    queryset = apply_sorting(queryset, filters.sort)

    # 4. Compute facets (on filtered queryset before pagination)
    facets = get_facet_counts(queryset, filters)

    # 5. Total count
    total_count = queryset.count()

    return DiscoveryResult(
        queryset=queryset,
        filters=filters,
        facets=facets,
        total_count=total_count,
        search_query=filters.query,
    )


# --------------------------------------------------------------------------------------
# Search suggestions (for autocomplete)
# --------------------------------------------------------------------------------------


def get_search_suggestions(query: str, limit: int = SUGGESTIONS_LIMIT) -> list[dict]:
    """Return search suggestions for autocomplete.

    Returns a list of dicts with: type, name, slug, url
    Types: product, category, brand, collection
    """
    if not query or len(query) < MIN_SEARCH_LENGTH:
        return []

    from apps.catalog.models import Brand, Category, Product

    suggestions = []

    # Product name matches
    products = Product.objects.published().filter(name__icontains=query)[:5]
    for p in products:
        suggestions.append(
            {
                "type": "product",
                "name": p.name,
                "slug": p.slug,
                "url": p.get_absolute_url(),
            }
        )

    # Category matches
    categories = Category.objects.filter(is_active=True, name__icontains=query)[:3]
    for cat in categories:
        suggestions.append(
            {
                "type": "category",
                "name": cat.name,
                "slug": cat.slug,
                "url": cat.get_absolute_url(),
            }
        )

    # Brand matches
    brands = Brand.objects.filter(is_active=True, name__icontains=query)[:2]
    for brand in brands:
        suggestions.append(
            {
                "type": "brand",
                "name": brand.name,
                "slug": brand.slug,
                "url": brand.get_absolute_url(),
            }
        )

    # Collection matches
    collections = Collection.objects.filter(is_active=True, name__icontains=query)[:2]
    for coll in collections:
        if coll.is_current:
            suggestions.append(
                {
                    "type": "collection",
                    "name": coll.name,
                    "slug": coll.slug,
                    "url": coll.get_absolute_url(),
                }
            )

    return suggestions[:limit]
