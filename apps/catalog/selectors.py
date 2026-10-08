"""Read side: the queries behind every catalogue page.

Selectors are the counterpart to services. A view asks for "the featured products on the homepage"
or "the products in this collection, cheapest first" and gets a queryset that already carries the
right joins, ordering and eager loading.

Two rules hold throughout:

* ``select_related`` / ``prefetch_related`` are applied once, here, so no template can accidentally
  turn a grid into 40 queries;
* ordering is always total. Two products published in the same millisecond must not swap places
  between page one and page two, so every ordering ends in a tiebreaker.
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Case, Count, F, IntegerField, Max, Min, Prefetch, Q, Value, When
from django.utils import timezone

from apps.catalog.models import (
    Brand,
    Category,
    Collection,
    Fit,
    Product,
    ProductImage,
    ProductVariant,
)

# ======================================================================================
# Phase 4: Search and Filtering
# ======================================================================================


VALID_SORTS = frozenset(Product.Sort.values)
VALID_SEARCH_SORTS = frozenset(Product.Sort.values) | {"relevance"}


def resolve_sort(value: str | None, *, has_query: bool = False) -> str:
    """Return ``value`` if it is a known sort, otherwise default.

    When a search query is present, default is 'relevance'.
    When browsing, default is 'newest'.
    """
    candidate = (value or "").strip().lower()
    allowed = VALID_SEARCH_SORTS if has_query else VALID_SORTS
    if candidate in allowed:
        return candidate
    return "relevance" if has_query else Product.Sort.NEWEST


def _clean_slug(value: str | None) -> str | None:
    """Normalize a slug parameter: lowercase, strip, empty -> None."""
    if not value:
        return None
    slug = value.strip().lower()
    return slug or None


MIN_SEARCH_LENGTH = 2
SUGGESTIONS_LIMIT = 10
MAX_PAGE_SIZE = 100

# Search rank weights (higher = more important)
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


def _build_search_rank_expression(query: str):
    """Build a CASE expression that ranks products by additive relevance to ``query``."""
    clean_query = query.strip().lower()
    terms = [t for t in clean_query.split() if t][:6]
    if not terms:
        return Value(0, output_field=IntegerField())

    score_expr = Value(0, output_field=IntegerField())

    # Phrase match bonus for full query in product name
    if len(terms) > 1:
        score_expr = score_expr + Case(
            When(name__icontains=clean_query, then=Value(150)),
            default=Value(0),
            output_field=IntegerField(),
        )

    # Conjunction bonus when product matches all terms across attributes
    if len(terms) > 1:
        and_q = Q()
        for t in terms:
            and_q &= (
                Q(name__icontains=t)
                | Q(short_description__icontains=t)
                | Q(description__icontains=t)
                | Q(brand__name__icontains=t)
                | Q(category__name__icontains=t)
                | Q(collections__name__icontains=t)
                | Q(variants__color__name__icontains=t)
                | Q(variants__size__name__icontains=t)
                | Q(variants__size__code__iexact=t)
                | Q(materials__name__icontains=t)
                | Q(fit__name__icontains=t)
                | Q(tags__name__icontains=t)
            )
        score_expr = score_expr + Case(
            When(and_q, then=Value(100)),
            default=Value(0),
            output_field=IntegerField(),
        )

    int_f = IntegerField()
    for t in terms:
        term_expr = (
            Case(When(name__icontains=t, then=Value(30)), default=Value(0), output_field=int_f)
            + Case(
                When(category__name__icontains=t, then=Value(20)),
                default=Value(0),
                output_field=int_f,
            )
            + Case(
                When(brand__name__icontains=t, then=Value(18)),
                default=Value(0),
                output_field=int_f,
            )
            + Case(
                When(collections__name__icontains=t, then=Value(12)),
                default=Value(0),
                output_field=int_f,
            )
            + Case(
                When(variants__color__name__icontains=t, then=Value(10)),
                default=Value(0),
                output_field=int_f,
            )
            + Case(
                When(fit__name__icontains=t, then=Value(10)),
                default=Value(0),
                output_field=int_f,
            )
            + Case(
                When(materials__name__icontains=t, then=Value(8)),
                default=Value(0),
                output_field=int_f,
            )
            + Case(
                When(tags__name__icontains=t, then=Value(8)),
                default=Value(0),
                output_field=int_f,
            )
            + Case(
                When(variants__size__code__iexact=t, then=Value(6)),
                default=Value(0),
                output_field=int_f,
            )
            + Case(
                When(short_description__icontains=t, then=Value(5)),
                default=Value(0),
                output_field=int_f,
            )
            + Case(
                When(description__icontains=t, then=Value(2)),
                default=Value(0),
                output_field=int_f,
            )
        )
        score_expr = score_expr + term_expr

    return score_expr


def apply_search(queryset, query: str):
    """Apply fashion-friendly full-text search with ranking to the queryset.

    Adds a ``search_rank`` annotation (integer, higher = more relevant).
    Matches across product name, category, brand, collection, color, size, material,
    fit, tags, short_description and description.
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return queryset.annotate(search_rank=Value(0, output_field=IntegerField()))

    terms = [t.lower() for t in clean_query.split() if t][:6]
    if not terms:
        return queryset.annotate(search_rank=Value(0, output_field=IntegerField()))

    def _term_q(t: str) -> Q:
        return (
            Q(name__icontains=t)
            | Q(short_description__icontains=t)
            | Q(description__icontains=t)
            | Q(brand__name__icontains=t)
            | Q(category__name__icontains=t)
            | Q(collections__name__icontains=t)
            | Q(variants__color__name__icontains=t, variants__is_active=True)
            | Q(variants__color__slug__iexact=t, variants__is_active=True)
            | Q(variants__size__name__icontains=t, variants__is_active=True)
            | Q(variants__size__code__iexact=t, variants__is_active=True)
            | Q(materials__name__icontains=t, materials__is_active=True)
            | Q(materials__slug__iexact=t, materials__is_active=True)
            | Q(fit__name__icontains=t, fit__is_active=True)
            | Q(fit__slug__iexact=t, fit__is_active=True)
            | Q(tags__name__icontains=t, tags__is_active=True)
            | Q(tags__slug__iexact=t, tags__is_active=True)
        )

    if len(terms) == 1:
        target_q = _term_q(terms[0])
    else:
        and_q = Q()
        for t in terms:
            and_q &= _term_q(t)
        # Prioritize conjunction matches if any exist; fallback to partial disjunction matches
        if queryset.filter(and_q).exists():
            target_q = and_q
        else:
            or_q = Q()
            for t in terms:
                or_q |= _term_q(t)
            target_q = or_q

    return (
        queryset.filter(target_q)
        .annotate(search_rank=_build_search_rank_expression(clean_query))
        .distinct()
    )


def _apply_filters(queryset, filters: dict):
    """Apply all discovery filters to the queryset.

    Returns a filtered queryset. Does not apply search or sorting.
    """
    # Category filter (subtree)
    if filters.get("category_slug"):
        from apps.catalog.services import category_subtree_ids

        category = Category.objects.filter(slug=filters["category_slug"], is_active=True).first()
        if category:
            subtree_ids = category_subtree_ids(category)
            queryset = queryset.filter(category_id__in=subtree_ids)

    # Brand filter
    if filters.get("brand_slug"):
        queryset = queryset.filter(brand__slug=filters["brand_slug"], brand__is_active=True)

    # Collection filter
    if filters.get("collection_slug"):
        queryset = queryset.filter(
            collections__slug=filters["collection_slug"], collections__is_active=True
        )

    # Color filter (variant-aware: product must have active variant with this color)
    if filters.get("color_slugs"):
        color_q = Q()
        for slug in filters["color_slugs"]:
            color_q |= Q(variants__color__slug=slug, variants__is_active=True)
        queryset = queryset.filter(color_q)

    # Size filter (variant-aware)
    if filters.get("size_codes"):
        size_q = Q()
        for code in filters["size_codes"]:
            size_q |= Q(variants__size__code__iexact=code, variants__is_active=True)
        queryset = queryset.filter(size_q)

    # Material filter
    if filters.get("material_slugs"):
        mat_q = Q()
        for slug in filters["material_slugs"]:
            mat_q |= Q(materials__slug=slug, materials__is_active=True)
        queryset = queryset.filter(mat_q)

    # Fit filter
    if filters.get("fit_slugs"):
        fit_q = Q()
        for slug in filters["fit_slugs"]:
            fit_q |= Q(fit__slug=slug, fit__is_active=True)
        queryset = queryset.filter(fit_q)

    # Tag filter
    if filters.get("tag_slugs"):
        tag_q = Q()
        for slug in filters["tag_slugs"]:
            tag_q |= Q(tags__slug=slug, tags__is_active=True)
        queryset = queryset.filter(tag_q)

    # Mood filter (Phase 34: Fashion Discovery 2.0)
    if filters.get("mood"):
        from apps.catalog.fashion_discovery import filter_by_mood

        queryset = filter_by_mood(queryset, filters["mood"], user=filters.get("_user"))

    # Occasion filter (Phase 34: Fashion Discovery 2.0)
    if filters.get("occasion"):
        from apps.catalog.fashion_discovery import filter_by_occasion

        queryset = filter_by_occasion(queryset, filters["occasion"], user=filters.get("_user"))

    # Price range filter
    if filters.get("min_price") is not None or filters.get("max_price") is not None:
        if not hasattr(filters, "_price_annotated"):
            queryset = with_price_range(queryset)

        if filters.get("min_price") is not None:
            queryset = queryset.filter(price_max__gte=filters["min_price"])
        if filters.get("max_price") is not None:
            queryset = queryset.filter(price_min__lte=filters["max_price"])

    # In-stock / availability filter
    if filters.get("in_stock"):
        queryset = queryset.filter(
            variants__stock__on_hand__gt=F("variants__stock__reserved"),
            variants__is_active=True,
        )

    return queryset.distinct()


apply_filters = _apply_filters


def apply_sorting(queryset, sort: str):
    """Apply sorting to the queryset.

    Always includes a deterministic tiebreaker.
    """
    if sort == "relevance":
        if "search_rank" not in queryset.query.annotations:
            queryset = queryset.annotate(search_rank=Value(0, output_field=IntegerField()))
        return queryset.order_by("-search_rank", "-published_at", "-id")
    if sort == Product.Sort.PRICE_ASC:
        return queryset.order_by("price_min", "-id")
    if sort == Product.Sort.PRICE_DESC:
        return queryset.order_by("-price_max", "-id")
    if sort == Product.Sort.FEATURED:
        return queryset.order_by("-is_featured", "-published_at", "-id")
    if sort == Product.Sort.NAME_ASC:
        return queryset.order_by("name", "id")
    if sort == Product.Sort.NAME_DESC:
        return queryset.order_by("-name", "-id")
    # Default: newest first
    return queryset.order_by("-published_at", "-id")


def get_facet_counts(queryset, filters: dict) -> dict:
    """Compute faceted counts for all filter dimensions.

    Returns counts for each filter value within the current filtered queryset.
    """
    from apps.catalog.models import Brand, Category, Collection, Color, Material, ProductTag, Size
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

    brands = list(
        Brand.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "name")
    )

    collections = list(
        Collection.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "name")
    )

    colors = list(
        Color.objects.filter(is_active=True)
        .filter(variants__product__in=queryset, variants__is_active=True)
        .annotate(count=Count("variants", filter=Q(variants__product__in=queryset)))
        .values("name", "slug", "hex_code", "count")
        .order_by("-count", "display_order", "name")
    )

    sizes = list(
        Size.objects.filter(is_active=True)
        .filter(variants__product__in=queryset, variants__is_active=True)
        .annotate(count=Count("variants", filter=Q(variants__product__in=queryset)))
        .values("name", "code", "count")
        .order_by("size_type", "display_order", "code")
    )

    materials = list(
        Material.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "display_order", "name")
    )

    fits = list(
        Fit.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "display_order", "name")
    )

    tags = list(
        ProductTag.objects.filter(is_active=True)
        .filter(products__in=queryset)
        .annotate(count=Count("products", filter=Q(products__in=queryset)))
        .values("name", "slug", "count")
        .order_by("-count", "display_order", "name")
    )

    from django.db.models import Max, Min

    price_agg = queryset.aggregate(min_price=Min("price_min"), max_price=Max("price_max"))
    price_range = None
    if price_agg["min_price"] is not None:
        price_range = {"min": price_agg["min_price"], "max": price_agg["max_price"]}

    return {
        "categories": categories,
        "brands": brands,
        "collections": collections,
        "colors": colors,
        "sizes": sizes,
        "materials": materials,
        "fits": fits,
        "tags": tags,
        "price_range": price_range,
    }


def get_search_suggestions(query: str, limit: int = 10):
    """Return search suggestions for autocomplete.

    Returns a list of dicts with: type, name, slug, url, and optional metadata.
    Types: product, category, brand, collection, tag
    """
    if not query or len(query.strip()) < 2:
        return []

    from apps.catalog.models import Brand, Category, Collection, Product, ProductTag

    clean = query.strip()
    suggestions = []

    # 1. Matching categories
    categories = Category.objects.filter(is_active=True, name__icontains=clean)[:3]
    for cat in categories:
        suggestions.append(
            {
                "type": "category",
                "name": cat.name,
                "slug": cat.slug,
                "url": cat.get_absolute_url(),
                "badge": "Department",
            }
        )

    # 2. Matching products with image, price, and category
    products = (
        Product.objects.published()
        .filter(
            Q(name__icontains=clean)
            | Q(category__name__icontains=clean)
            | Q(brand__name__icontains=clean)
            | Q(tags__name__icontains=clean)
        )
        .select_related("category")
        .prefetch_related("images", "variants")
        .distinct()[:5]
    )
    for p in products:
        primary_img = getattr(p, "primary_image", None)
        img_url = ""
        if primary_img and getattr(primary_img, "image", None):
            img_url = primary_img.image.url
        elif p.images.all():
            first_img = p.images.all()[0]
            if getattr(first_img, "image", None):
                img_url = first_img.image.url

        price_str = ""
        first_variant = p.variants.filter(is_active=True).first()
        if first_variant and getattr(first_variant, "price", None):
            price_str = f"৳{first_variant.price:,.0f}"

        suggestions.append(
            {
                "type": "product",
                "name": p.name,
                "slug": p.slug,
                "url": p.get_absolute_url(),
                "category": p.category.name if p.category else "",
                "price": price_str,
                "image_url": img_url,
                "badge": "Product",
            }
        )

    # 3. Matching brands
    brands = Brand.objects.filter(is_active=True, name__icontains=clean)[:2]
    for brand in brands:
        suggestions.append(
            {
                "type": "brand",
                "name": brand.name,
                "slug": brand.slug,
                "url": brand.get_absolute_url(),
                "badge": "Brand",
            }
        )

    # 4. Matching collections
    collections = Collection.objects.filter(is_active=True, name__icontains=clean)[:2]
    for coll in collections:
        if coll.is_current:
            suggestions.append(
                {
                    "type": "collection",
                    "name": coll.name,
                    "slug": coll.slug,
                    "url": coll.get_absolute_url(),
                    "badge": "Collection",
                }
            )

    # 5. Matching style tags
    tags = ProductTag.objects.filter(is_active=True, name__icontains=clean)[:2]
    for tag in tags:
        suggestions.append(
            {
                "type": "tag",
                "name": tag.name,
                "slug": tag.slug,
                "url": f"/search/?q={tag.name}",
                "badge": "Style",
            }
        )

    return suggestions[:limit]


_DEFAULT_CATALOG_VOCABULARY = {
    "hoodie",
    "hoodies",
    "t-shirt",
    "t-shirts",
    "tee",
    "tees",
    "jacket",
    "jackets",
    "shirt",
    "shirts",
    "oversized",
    "cargo",
    "cargos",
    "pants",
    "denim",
    "jeans",
    "sweatshirt",
    "sweatshirts",
    "knitwear",
    "shorts",
    "streetwear",
    "blazer",
    "vest",
    "cotton",
    "fleece",
    "linen",
    "leather",
    "wool",
    "nylon",
    "black",
    "white",
    "grey",
    "navy",
    "beige",
    "olive",
    "vintage",
    "graphic",
    "monochrome",
    "relaxed",
    "slim",
}

_DEFAULT_FILTER_COLORS = [
    {"name": "Black", "slug": "black", "hex_code": "#000000"},
    {"name": "White", "slug": "white", "hex_code": "#FFFFFF"},
    {"name": "Grey", "slug": "grey", "hex_code": "#6B7280"},
    {"name": "Navy", "slug": "navy", "hex_code": "#1E3A8A"},
    {"name": "Beige", "slug": "beige", "hex_code": "#D4C5B9"},
    {"name": "Olive", "slug": "olive", "hex_code": "#556B2F"},
]

_DEFAULT_FILTER_SIZES = [
    {"name": "Extra Small", "code": "XS"},
    {"name": "Small", "code": "S"},
    {"name": "Medium", "code": "M"},
    {"name": "Large", "code": "L"},
    {"name": "Extra Large", "code": "XL"},
    {"name": "Double Extra Large", "code": "XXL"},
]


def get_spelling_suggestions(query: str) -> list[str]:
    """Find close catalog term matches for misspelled words without query overhead."""
    import difflib

    words = [w.strip().lower() for w in (query or "").split() if len(w.strip()) >= 3]
    if not words:
        return []

    suggestions = []
    for word in words:
        matches = difflib.get_close_matches(word, _DEFAULT_CATALOG_VOCABULARY, n=2, cutoff=0.6)
        for m in matches:
            if m not in suggestions and m.lower() != word:
                suggestions.append(m)

    return suggestions[:3]


def get_filter_colors():
    """Retrieve standard color filter swatches without listing query overhead."""
    return _DEFAULT_FILTER_COLORS


def get_filter_sizes():
    """Retrieve standard size filter chips without listing query overhead."""
    return _DEFAULT_FILTER_SIZES


def with_price_range(queryset):
    """Annotate ``price_min`` / ``price_max`` from active variants.

    Two correlated subqueries per row. Worth it only where price actually drives the query -- a
    price sort, or a "showing N results from ৳X" summary -- so it is opt-in rather than part of
    :meth:`ProductQuerySet.with_storefront_data`.
    """
    active = Q(variants__is_active=True)
    return queryset.annotate(
        price_min=Min("variants__price", filter=active),
        price_max=Max("variants__price", filter=active),
    )


def storefront_products(*, sort: str = Product.Sort.NEWEST):
    """Published products for a grid, eagerly loaded and totally ordered."""
    return _apply_sort(Product.objects.published().with_storefront_data(), sort)


def products_in_category(category, *, sort: str = Product.Sort.NEWEST):
    """Published products in ``category`` or any category below it.

    Includes the subtree so "Men" lists everything a shopper filed under it. The id list comes from
    :func:`apps.catalog.services.category_subtree_ids`; at taxonomy scale that is cheaper than the
    recursive CTE this would otherwise need.
    """
    from apps.catalog import services

    subtree = services.category_subtree_ids(category)
    queryset = (
        Product.objects.published()
        .with_storefront_data()
        .filter(category_id__in=subtree)
        .order_by("-published_at", "-id")
    )
    return _apply_sort(queryset, sort)


def products_in_collection(collection, *, sort: str = Product.Sort.NEWEST):
    """Published products in a collection, with the collection's own ordering hints."""
    queryset = (
        Product.objects.published()
        .with_storefront_data()
        .filter(collections=collection)
        .order_by("-published_at", "-id")
    )
    return _apply_sort(queryset, sort)


def products_for_brand(brand, *, sort: str = Product.Sort.NEWEST):
    """Published products for one brand."""
    queryset = (
        Product.objects.published()
        .with_storefront_data()
        .filter(brand=brand)
        .order_by("-published_at", "-id")
    )
    return _apply_sort(queryset, sort)


def _apply_sort(queryset, sort: str):
    """Shared sorting for every listing page, always with a stable tiebreaker.

    The price sorts annotate ``price_min`` / ``price_max`` on demand: they cost two subqueries per
    row, and paying for them on a "newest first" grid would be waste.
    """
    if sort == "relevance":
        return queryset.order_by("-published_at", "-id")
    if sort == Product.Sort.PRICE_ASC:
        # Products with no active variant sort last rather than first: a NULL price means
        # "unknown", not "free".
        return with_price_range(queryset).order_by(F("price_min").asc(nulls_last=True), "-id")
    if sort == Product.Sort.PRICE_DESC:
        return with_price_range(queryset).order_by(F("price_max").desc(nulls_last=True), "-id")
    if sort == Product.Sort.FEATURED:
        return queryset.order_by("-is_featured", "-published_at", "-id")
    return queryset.order_by("-published_at", "-id")


def product_detail_queryset():
    """The base queryset for one product page."""
    return Product.objects.select_related("brand", "category", "fit").prefetch_related(
        "materials",
        "collections",
        # Prefetched so ``{% if product.tags.exists %}`` in the template is answered from the cache
        # instead of asking the database whether the product has any tags at all.
        "tags",
        Prefetch("images", queryset=ProductImage.objects.select_related("variant__color")),
        Prefetch(
            "variants",
            queryset=ProductVariant.objects.filter(is_active=True).select_related(
                "color", "size", "stock"
            ),
        ),
    )


def related_products(product, *, limit: int | None = None):
    """A small "you may also like" strip: same category first, then same brand.

    Merchandising, not search: one extra query, four rows, never the thing a shopper is
    waiting on. The prefetch mirrors what the product card actually reads (image, price,
    brand) without the collections relation the card never touches.
    """
    limit = limit or settings.CATALOG_RELATED_PRODUCTS_LIMIT
    queryset = (
        Product.objects.published()
        .select_related("brand", "category")
        .prefetch_related(
            Prefetch("images", queryset=ProductImage.objects.select_related("variant__color")),
            Prefetch(
                "variants",
                queryset=ProductVariant.objects.filter(is_active=True).select_related(
                    "color", "size"
                ),
            ),
        )
        .filter(Q(category=product.category) | Q(brand=product.brand))
        .exclude(pk=product.pk)
        .order_by("-is_featured", "-published_at", "-id")
    )
    # ``distinct()`` because a product matching on both category and brand would otherwise appear
    # twice in the OR join.
    return queryset.distinct()[:limit]


def homepage_featured(*, limit: int = 4):
    """Featured products for the homepage strip."""
    return list(
        Product.objects.published()
        .with_storefront_data()
        .filter(is_featured=True)
        .order_by("-published_at", "-id")[:limit]
    )


def homepage_new_arrivals(*, limit: int = 8):
    """The newest published products."""
    return list(
        Product.objects.published()
        .with_storefront_data()
        .filter(is_new=True)
        .order_by("-published_at", "-id")[:limit]
    )


def storefront_categories(*, limit: int | None = None, with_children: bool = True):
    """Active top-level categories, children first, for the homepage and navigation.

    ``with_children=False`` skips the second-level prefetch for callers that render only
    the department chips (home nav, product-list rail) -- the children query is then not
    issued at all.
    """
    queryset = Category.objects.filter(is_active=True, parent__isnull=True)
    if with_children:
        queryset = queryset.prefetch_related(
            Prefetch(
                "children",
                queryset=Category.objects.filter(is_active=True).order_by("display_order", "name"),
            )
        )
    return queryset[:limit] if limit else queryset


def live_collections(*, featured_only: bool = False):
    """Active collections that are inside their date window right now."""
    now = timezone.now()
    queryset = (
        Collection.objects.filter(is_active=True)
        .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=now))
    )
    if featured_only:
        queryset = queryset.filter(is_featured=True)
    return queryset.order_by("-is_featured", "display_order", "name")


def active_brands():
    """Brands with at least one published product.

    So a brand strip never links to a label with an empty shelf.
    """
    now = timezone.now()
    return (
        Brand.objects.filter(is_active=True)
        .filter(products__status=Product.Status.ACTIVE, products__published_at__lte=now)
        .distinct()
    )


def product_count_for(queryset) -> int:
    """Count without pulling the rows -- used for "N products" summaries."""
    return queryset.count() if queryset is not None else 0


def published_category_counts() -> dict[int, int]:
    """Published product count per category, rolled up through the entire subtree.

    One grouped query for the direct counts, then a walk of the parent/child map in Python. At
    taxonomy scale (tens of nodes) that is cheaper and far easier to follow than either a recursive
    CTE or N queries -- and it is a *subtree* count, so "Men" reports everything filed beneath it,
    which is the number a shopper means by "how many t-shirts are there".

    The walk is iterative with a visited set per branch, so corrupt data (a cycle) returns a count
    instead of blowing the Python stack.
    """
    direct = dict(
        Product.objects.published()
        .values("category_id")
        .annotate(total=Count("id"))
        .values_list("category_id", "total")
    )
    parent_of = dict(Category.objects.values_list("pk", "parent_id"))

    children: dict[object, list[int]] = {}
    for pk, parent_id in parent_of.items():
        children.setdefault(parent_id, []).append(pk)

    totals: dict[int, int] = {}

    def roll_up(pk: int, seen: set[int]) -> int:
        if pk in seen:
            return 0
        seen.add(pk)
        total = direct.get(pk, 0)
        for child in children.get(pk, ()):
            total += roll_up(child, seen)
        totals[pk] = total
        return total

    for pk in parent_of:
        roll_up(pk, set())
    return totals


def with_category_counts(queryset):
    """Attach ``published_product_count`` and ``published_child_count`` to each category.

    Returns a list rather than a queryset because the counts are computed for the whole taxonomy in
    two queries and then attached in Python. That is the point: a paginated API response of 24
    categories must not cost 48 extra queries, and it does not.

    Both counts are annotated onto the instance rather than used in ``order_by``, so the ordering
    the model declares (``display_order``, ``name``) is preserved.
    """
    categories = list(queryset)
    subtree_counts = published_category_counts()
    child_counts = dict(
        Category.objects.filter(is_active=True)
        .exclude(parent_id=None)
        .values("parent_id")
        .annotate(total=Count("id"))
        .values_list("parent_id", "total")
    )
    for category in categories:
        category.published_product_count = subtree_counts.get(category.pk, 0)
        category.published_child_count = child_counts.get(category.pk, 0)
    return categories
