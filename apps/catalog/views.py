"""Catalogue views.

All read-only, all public, all ``GET``. There is no customer-facing write path in Phase 3: uploads
and price changes are admin work (Django admin), and "add to bag" arrives with the cart phase.

Three things are worth reading before changing these:

* **Only published products are reachable.** ``product_detail`` resolves through
  ``Product.objects.published()``, so a draft or archived product answers 404 rather than 200 with
  a warning nobody reads. Nothing here relies on a template check to hide it.
* **Variant selection is server-resolved.** ``?color=`` and ``?size=`` are requests, not decisions:
  :func:`apps.catalog.services.build_variant_matrix` looks the combination up, and an impossible
  pair falls back to a real variant instead of erroring.
* **Sorting is a fixed allow-list** (``Product.Sort``). ``?sort=`` never reaches the ORM.

Pages are wrapped in ``never_cache``: prices, sale state and collection windows change without a
new deploy, and a cached product page that quotes yesterday's price is worse than one extra
database read.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.catalog import selectors, services
from apps.catalog.models import Brand, Category, Collection, Color, Product, Size
from apps.catalog.seo import (
    breadcrumb_schema,
    listing_metadata,
    product_metadata,
    product_schema,
    taxonomy_metadata,
)
from apps.core.utils import storefront_open

# How many departments to surface as filter chips above a listing. A shopper scans a shelf, not a
# sitemap: past a dozen links the row wraps into a wall and stops being a filter.
NAV_CATEGORY_LIMIT = 12


def _split_param(value: str | None, *, upper: bool = False) -> list[str]:
    """Split a comma-separated query param into a clean list of values."""
    parts = (value or "").split(",")
    cleaned = [p.strip() for p in parts if p.strip()]
    return [p.upper() for p in cleaned] if upper else [p.lower() for p in cleaned]


def _parse_filters(request) -> dict:
    """Parse and normalize filter parameters from the request."""
    return {
        "query": (request.GET.get("q") or "").strip(),
        "category_slug": selectors._clean_slug(request.GET.get("category")),
        "brand_slug": selectors._clean_slug(request.GET.get("brand")),
        "collection_slug": selectors._clean_slug(request.GET.get("collection")),
        "color_slugs": _split_param(request.GET.get("color")),
        "size_codes": _split_param(request.GET.get("size"), upper=True),
        "material_slugs": _split_param(request.GET.get("material")),
        "fit_slugs": _split_param(request.GET.get("fit")),
        "tag_slugs": _split_param(request.GET.get("tag")),
        "min_price": _parse_decimal(request.GET.get("min_price")),
        "max_price": _parse_decimal(request.GET.get("max_price")),
    }


def _parse_decimal(value: str | None) -> Decimal | None:
    """Parse a decimal string, returning None on failure."""
    if not value:
        return None
    try:
        return Decimal(value.strip())
    except Exception:
        return None


def _paginate(request, queryset):
    """Paginate any product queryset with the storefront page size."""
    paginator = Paginator(queryset, settings.CATALOG_PRODUCTS_PER_PAGE)
    return paginator, paginator.get_page(request.GET.get("page"))


def _trail_schema(request, nodes: list[dict]) -> dict:
    return breadcrumb_schema([{"name": "Home", "url": request.build_absolute_uri("/")}, *nodes])


@storefront_open
@never_cache
@require_GET
def product_list(request):
    """``/products/`` -- the whole catalogue, newest first unless asked otherwise."""
    sort = selectors.resolve_sort(request.GET.get("sort"))
    filters = _parse_filters(request)

    # Build the queryset using the new discovery selectors
    queryset = selectors.storefront_products(sort=selectors.resolve_sort(request.GET.get("sort")))

    # Apply search
    if filters["query"]:
        from apps.catalog.selectors import apply_search

        queryset = apply_search(queryset, filters["query"])

    # Apply price annotations (needed for price filtering/sorting)
    queryset = selectors.with_price_range(queryset)

    # Apply filters
    queryset = selectors._apply_filters(queryset, filters)

    # Apply sorting
    queryset = selectors.apply_sorting(queryset, sort)

    paginator, page = _paginate(request, queryset)

    context = {
        "page_obj": page,
        "paginator": paginator,
        "products": page.object_list,
        "sort": sort,
        "sort_choices": Product.Sort.choices,
        "total_count": paginator.count,
        "category_rail": selectors.storefront_categories(limit=NAV_CATEGORY_LIMIT),
        "filters": filters,
        "facet_counts": selectors.get_facet_counts(queryset, {}),
        **listing_metadata(
            f"{_('All products')} | FLASHWEAR",
            _(
                "Browse every FLASHWEAR product: t-shirts, hoodies, shirts, "
                "cargo pants, denim and accessories."
            ),
            request=request,
        ),
    }
    return render(request, "catalog/product_list.html", context)


@storefront_open
@never_cache
@require_GET
def product_search(request):
    """``/search/`` -- the search results page.

    This is an alias for ``/products/?q=...`` that provides a dedicated search landing page.
    """
    return product_list(request)


@storefront_open
@never_cache
@require_GET
def product_detail(request, slug: str):
    """``/products/<slug>/`` -- one garment, with its variants and gallery."""
    product = get_object_or_404(selectors.product_detail_queryset().published(), slug=slug)

    matrix = services.build_variant_matrix(
        product,
        color=Color.objects.filter(slug=request.GET.get("color", ""), is_active=True).first(),
        size=Size.objects.filter(code=request.GET.get("size", "").upper(), is_active=True).first(),
    )
    selected = matrix.selected
    gallery = services.product_gallery(product)

    # Flatten the gallery for the template: the selected colour's own shots first (choosing "Black"
    # should reveal black photography), then the shared ones. ``hero`` is what fills the large
    # frame. Doing this here keeps the template free of dictionary lookups it cannot express well.
    color_images = gallery["by_color"].get(selected.color_id, []) if selected else []
    gallery_images = [*color_images, *gallery["shared"]]

    breadcrumbs = _trail_schema(
        request,
        [
            {
                "name": product.category.name,
                "url": request.build_absolute_uri(product.category.get_absolute_url()),
            },
            {
                "name": product.name,
                "url": request.build_absolute_uri(product.get_absolute_url()),
            },
        ],
    )

    # Wishlist state for the heart toggle. Anonymous shoppers get the plain heart, which
    # routes to the login prompt and returns them here afterwards.
    in_wishlist = False
    if request.user.is_authenticated:
        wishlist = getattr(request.user, "wishlist", None)
        in_wishlist = wishlist is not None and wishlist.has_product(product)

    context = {
        "product": product,
        "matrix": matrix,
        "selected_variant": selected,
        "selected_color": selected.color if selected else None,
        "selected_size": selected.size if selected else None,
        "gallery": gallery,
        "gallery_images": gallery_images,
        "hero_image": gallery_images[0] if gallery_images else gallery["primary"],
        "materials": product.materials.all(),
        "collections": product.collections.all(),
        "category_trail": product.category.breadcrumb_trail(),
        "breadcrumbs": breadcrumbs["itemListElement"],
        "breadcrumb_schema": breadcrumbs,
        "product_schema": product_schema(product, request=request),
        "related_products": selectors.related_products(product),
        "in_wishlist": in_wishlist,
        **product_metadata(product, request=request),
    }
    return render(request, "catalog/product_detail.html", context)


@storefront_open
@never_cache
@require_GET
def category_list(request):
    """``/categories/`` -- the department index.

    Deliberately a page of departments rather than every category: the taxonomy is three levels
    deep, and a flat list of all nine nodes would be a worse way in than the two levels a shopper
    actually navigates by.
    """
    categories = selectors.storefront_categories()
    context = {
        "categories": categories,
        "breadcrumbs": _trail_schema(
            request,
            [{"name": _("Categories"), "url": request.build_absolute_uri("/categories/")}],
        ),
        **listing_metadata(
            f"{_('Categories')} | FLASHWEAR",
            _("Shop by department: t-shirts, hoodies, shirts, cargo pants, denim and more."),
            request=request,
            path=reverse("catalog:category-list"),
        ),
    }
    return render(request, "catalog/category_list.html", context)


@storefront_open
@never_cache
@require_GET
def category_detail(request, slug: str):
    """``/categories/<slug>/`` -- a department and everything filed beneath it."""
    category = get_object_or_404(Category.objects.filter(is_active=True), slug=slug)
    sort = selectors.resolve_sort(request.GET.get("sort"))
    paginator, page = _paginate(request, selectors.products_in_category(category, sort=sort))

    trail = category.breadcrumb_trail()
    trail_nodes = [
        {"name": node.name, "url": request.build_absolute_uri(node.get_absolute_url())}
        for node in trail
    ]
    summary = _(
        "%(count)d product in %(category)s."
        if paginator.count == 1
        else "%(count)d products in %(category)s.",
    ) % {"count": paginator.count, "category": category.name}

    context = {
        "category": category,
        "child_categories": Category.objects.filter(parent=category, is_active=True).order_by(
            "display_order", "name"
        ),
        "parent_category": category.parent,
        "category_trail": trail,
        "products": page.object_list,
        "page_obj": page,
        "paginator": paginator,
        "total_count": paginator.count,
        "sort": sort,
        "sort_choices": Product.Sort.choices,
        "breadcrumb_schema": _trail_schema(request, trail_nodes),
        **taxonomy_metadata(category, request=request, fallback_description=str(summary)),
    }
    return render(request, "catalog/category_detail.html", context)


@storefront_open
@never_cache
@require_GET
def collection_list(request):
    """``/collections/`` -- every collection that is live right now."""
    context = {
        "collections": list(selectors.live_collections()),
        **listing_metadata(
            f"{_('Collections')} | FLASHWEAR",
            _("Shop FLASHWEAR collections: seasonal drops, capsules and limited editions."),
            request=request,
            path=reverse("catalog:collection-list"),
        ),
    }
    return render(request, "catalog/collection_list.html", context)


@storefront_open
@never_cache
@require_GET
def collection_detail(request, slug: str):
    """``/collections/<slug>/`` -- one collection, if it is live.

    A collection that is inactive or outside its date window answers 404, not "coming soon": the
    page exists only while the story does.
    """
    collection = get_object_or_404(Collection.objects.filter(is_active=True), slug=slug)
    if not collection.is_current:
        raise Http404(_("This collection is not running."))

    sort = selectors.resolve_sort(request.GET.get("sort"))
    paginator, page = _paginate(request, selectors.products_in_collection(collection, sort=sort))

    context = {
        "collection": collection,
        "products": page.object_list,
        "page_obj": page,
        "paginator": paginator,
        "total_count": paginator.count,
        "sort": sort,
        "sort_choices": Product.Sort.choices,
        "breadcrumb_schema": _trail_schema(
            request,
            [
                {"name": _("Collections"), "url": request.build_absolute_uri("/collections/")},
                {
                    "name": collection.name,
                    "url": request.build_absolute_uri(collection.get_absolute_url()),
                },
            ],
        ),
        **taxonomy_metadata(collection, request=request, kind="website"),
    }
    return render(request, "catalog/collection_detail.html", context)


@storefront_open
@never_cache
@require_GET
def brand_detail(request, slug: str):
    """``/brands/<slug>/`` -- everything by one label."""
    brand = get_object_or_404(Brand.objects.filter(is_active=True), slug=slug)
    sort = selectors.resolve_sort(request.GET.get("sort"))
    paginator, page = _paginate(request, selectors.products_for_brand(brand, sort=sort))

    context = {
        "brand": brand,
        "products": page.object_list,
        "page_obj": page,
        "paginator": paginator,
        "total_count": paginator.count,
        "sort": sort,
        "sort_choices": Product.Sort.choices,
        "breadcrumb_schema": _trail_schema(
            request,
            [{"name": brand.name, "url": request.build_absolute_uri(brand.get_absolute_url())}],
        ),
        **taxonomy_metadata(brand, request=request, kind="website"),
    }
    return render(request, "catalog/brand_detail.html", context)
