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
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.analytics.services import record_event
from apps.catalog import merchandising, selectors, services
from apps.catalog.models import Brand, Category, Collection, Color, Product, Size
from apps.catalog.seo import (
    breadcrumb_schema,
    listing_metadata,
    product_metadata,
    product_schema,
    taxonomy_metadata,
)
from apps.core.utils import storefront_open
from apps.engagement.forms import ReviewForm
from apps.engagement.services import reviews as review_services

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
    queryset = selectors.storefront_products(sort=sort)

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

    if filters["query"]:
        record_event(
            "product_search",
            request=request,
            metadata={"query": filters["query"][:100], "result_count": paginator.count},
        )
    applied_filters = [key for key, value in filters.items() if key != "query" and value]
    if applied_filters:
        record_event("filter_used", request=request, metadata={"filters": applied_filters})

    context = {
        "page_obj": page,
        "paginator": paginator,
        "products": page.object_list,
        "sort": sort,
        "sort_choices": Product.Sort.choices,
        "total_count": paginator.count,
        "category_rail": selectors.storefront_categories(
            limit=NAV_CATEGORY_LIMIT, with_children=False
        ),
        "filters": filters,
        **listing_metadata(
            f"{_('All products')} | FLASHWEAR",
            _(
                "Browse every FLASHWEAR product: t-shirts, hoodies, shirts, "
                "cargo pants, denim and accessories."
            ),
            request=request,
            page=page.number,
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

    # Only look up the option rows when the query string actually asked for one: an empty
    # slug never matches anything, and two guaranteed-miss SELECTs on every product page
    # is two more than needed.
    color_slug = request.GET.get("color", "")
    size_code = request.GET.get("size", "").upper()
    matrix = services.build_variant_matrix(
        product,
        color=Color.objects.filter(slug=color_slug, is_active=True).first() if color_slug else None,
        size=Size.objects.filter(code=size_code, is_active=True).first() if size_code else None,
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

    # Phase 7: reviews. The aggregate is one query and feeds both the visible summary and the
    # JSON-LD block; the list is paginated with an allowlisted sort (never a raw order_by).
    review_summary = review_services.aggregate_for(product)
    review_sort = request.GET.get("reviews_sort", review_services.DEFAULT_SORT)
    review_page = Paginator(
        review_services.public_queryset(product, sort=review_sort),
        settings.REVIEWS_PER_PAGE,
    ).get_page(request.GET.get("reviews_page"))

    own_review = None
    can_review, review_reason = False, "anonymous"
    if request.user.is_authenticated:
        own_review = review_services.own_review(request.user, product)
        can_review, review_reason = review_services.eligibility(request.user, product)

    merchandising.record_recently_viewed(request, product)
    signals = merchandising.get_product_signals(product, selected, user=request.user)
    related_products = selectors.related_products(product)

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
        "product_schema": product_schema(product, request=request, aggregate=review_summary),
        "related_products": related_products,
        "you_may_also_like": related_products,
        "more_like_this": [],
        "recently_viewed": [],
        "signals": signals,
        "in_wishlist": in_wishlist,
        "review_summary": review_summary,
        "review_page": review_page,
        "review_sort": review_sort,
        "review_form": ReviewForm(),
        "own_review": own_review,
        "can_review": can_review,
        "review_reason": review_reason,
        "review_reason_message": review_services.ineligible_message(review_reason)
        if not can_review
        else "",
        **product_metadata(product, request=request),
    }
    record_event(
        "product_view",
        request=request,
        object_type="product",
        object_id=product.pk,
        metadata={"slug": product.slug, "category": product.category.slug},
    )
    response = render(request, "catalog/product_detail.html", context)
    cookie_val = getattr(request, "_pending_recently_viewed_cookie", None)
    if cookie_val:
        response.set_cookie(
            "fw_recent_views",
            cookie_val,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="Lax",
        )
    return response


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
        "breadcrumb_schema": _trail_schema(
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
        **taxonomy_metadata(
            category, request=request, fallback_description=str(summary), page=page.number
        ),
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
        **taxonomy_metadata(collection, request=request, kind="website", page=page.number),
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
        **taxonomy_metadata(brand, request=request, kind="website", page=page.number),
    }
    return render(request, "catalog/brand_detail.html", context)


@storefront_open
@never_cache
def visual_search(request):
    """Customer-facing visual product discovery (Phase 22).

    Supports:
    1. 'Search by Image' via file upload (POST).
    2. 'Find Similar' to a specified product via `?product=<slug>` (GET).
    3. Standalone upload landing state (GET).
    """
    from apps.catalog.visual_discovery import (
        find_similar_to_product,
        find_visually_similar_products,
    )

    # Mode 1: "Find Similar" from an existing product
    product_slug = request.GET.get("product") or request.GET.get("similar_to")
    if product_slug and request.method == "GET":
        source_product = get_object_or_404(Product.objects.published(), slug=product_slug)
        results = find_similar_to_product(source_product, user=request.user)

        record_event(
            "find_similar_used",
            request=request,
            object_type="product",
            object_id=source_product.pk,
            metadata={"product_slug": source_product.slug},
        )

        categories = Category.objects.filter(is_active=True, parent__isnull=True).order_by(
            "display_order", "name"
        )

        context = {
            "mode": "similar_product",
            "source_product": source_product,
            "results": results,
            "result_count": len(results),
            "categories": categories,
        }
        return render(request, "catalog/visual_search.html", context)

    # Mode 2: "Search by Image" upload
    if request.method == "POST":
        image_file = request.FILES.get("image")
        category_slug = (request.POST.get("category") or "").strip() or None

        record_event(
            "image_search_started",
            request=request,
            metadata={"source": "storefront"},
        )

        if not image_file:
            error_msg = _("Please select an image to search.")
            record_event(
                "image_search_failed",
                request=request,
                metadata={"error_code": "empty_upload", "reason": "No image selected"},
            )
            categories = Category.objects.filter(is_active=True, parent__isnull=True).order_by(
                "display_order", "name"
            )
            return render(
                request,
                "catalog/visual_search.html",
                {
                    "mode": "error",
                    "error": error_msg,
                    "categories": categories,
                },
                status=400,
            )

        try:
            results, features = find_visually_similar_products(
                image_file,
                category_slug=category_slug,
                user=request.user,
            )

            record_event(
                "image_search_completed",
                request=request,
                metadata={
                    "results_count": len(results),
                    "dominant_colors": features.dominant_colors,
                    "matched_colors": features.matched_catalog_colors,
                    "tone": features.tone,
                },
            )

            categories = Category.objects.filter(is_active=True, parent__isnull=True).order_by(
                "display_order", "name"
            )

            context = {
                "mode": "image_search",
                "features": features,
                "results": results,
                "result_count": len(results),
                "selected_category": category_slug,
                "categories": categories,
            }
            return render(request, "catalog/visual_search.html", context)

        except ValidationError as error:
            msg = getattr(error, "message", str(error))
            code = getattr(error, "code", "invalid_image")
            record_event(
                "image_search_failed",
                request=request,
                metadata={"error_code": code, "reason": msg},
            )
            categories = Category.objects.filter(is_active=True, parent__isnull=True).order_by(
                "display_order", "name"
            )
            return render(
                request,
                "catalog/visual_search.html",
                {
                    "mode": "error",
                    "error": msg,
                    "categories": categories,
                },
                status=400,
            )
        except Exception:
            record_event(
                "image_search_failed",
                request=request,
                metadata={"error_code": "server_error", "reason": "Processing failure"},
            )
            categories = Category.objects.filter(is_active=True, parent__isnull=True).order_by(
                "display_order", "name"
            )
            return render(
                request,
                "catalog/visual_search.html",
                {
                    "mode": "error",
                    "error": _("We could not analyze that image. Please try another photo."),
                    "categories": categories,
                },
                status=400,
            )

    # Mode 3: Initial upload landing page
    categories = Category.objects.filter(is_active=True, parent__isnull=True).order_by(
        "display_order", "name"
    )
    return render(
        request,
        "catalog/visual_search.html",
        {
            "mode": "upload",
            "categories": categories,
        },
    )


@storefront_open
@never_cache
def track_visual_click(request):
    """Track outbound clicks on visual search results and redirect."""
    product_id = request.GET.get("product_id") or request.POST.get("product_id")
    target_url = request.GET.get("next") or ""

    if product_id:
        try:
            prod_pk = int(product_id)
            record_event(
                "visual_product_clicked",
                request=request,
                object_type="product",
                object_id=prod_pk,
            )
            if not target_url:
                prod = Product.objects.filter(pk=prod_pk).first()
                if prod:
                    target_url = prod.get_absolute_url()
        except (ValueError, TypeError):
            pass

    if target_url and url_has_allowed_host_and_scheme(
        target_url, allowed_hosts={request.get_host()}
    ):
        return redirect(target_url)
    return redirect("catalog:product-list")


@storefront_open
@never_cache
@require_GET
def quick_view(request, slug: str):
    """Return a lightweight HTML modal fragment for quick product preview and shopping."""
    product = get_object_or_404(selectors.product_detail_queryset().published(), slug=slug)

    color_slug = request.GET.get("color", "")
    size_code = request.GET.get("size", "").upper()
    matrix = services.build_variant_matrix(
        product,
        color=Color.objects.filter(slug=color_slug, is_active=True).first() if color_slug else None,
        size=Size.objects.filter(code=size_code, is_active=True).first() if size_code else None,
    )
    selected = matrix.selected
    gallery = services.product_gallery(product)

    color_images = gallery["by_color"].get(selected.color_id, []) if selected else []
    gallery_images = [*color_images, *gallery["shared"]]

    in_wishlist = False
    if request.user.is_authenticated:
        wishlist = getattr(request.user, "wishlist", None)
        in_wishlist = wishlist is not None and wishlist.has_product(product)

    signals = merchandising.get_product_signals(product, selected, user=request.user)

    record_event(
        "quick_view_opened",
        request=request,
        object_type="product",
        object_id=product.pk,
        metadata={"slug": product.slug},
    )

    context = {
        "product": product,
        "matrix": matrix,
        "selected_variant": selected,
        "selected_color": selected.color if selected else None,
        "selected_size": selected.size if selected else None,
        "gallery": gallery,
        "gallery_images": gallery_images,
        "hero_image": gallery_images[0] if gallery_images else gallery["primary"],
        "in_wishlist": in_wishlist,
        "signals": signals,
    }
    return render(request, "catalog/_quick_view_modal.html", context)


@storefront_open
@never_cache
def merchandising_click(request):
    """Track outbound clicks on merchandising/discovery widgets and redirect safely."""
    event_type = request.GET.get("type", "recommendation_click")
    allowed_types = {
        "recently_viewed_click",
        "recommendation_click",
        "related_product_click",
        "continue_shopping_click",
    }
    if event_type not in allowed_types:
        event_type = "recommendation_click"

    product_id = request.GET.get("product_id")
    target_url = request.GET.get("next") or ""
    prod_pk = None

    if product_id:
        try:
            prod_pk = int(product_id)
            if not target_url:
                prod = Product.objects.filter(pk=prod_pk).first()
                if prod:
                    target_url = prod.get_absolute_url()
        except (ValueError, TypeError):
            pass

    record_event(
        event_type,
        request=request,
        object_type="product",
        object_id=prod_pk,
        metadata={
            "next": target_url or "/",
            "src": (request.GET.get("src") or "")[:40],
        },
    )

    if target_url and (
        target_url.startswith("/")
        and not target_url.startswith("//")
        and url_has_allowed_host_and_scheme(
            target_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return redirect(target_url)
    return redirect("catalog:product-list")


@storefront_open
@never_cache
@require_GET
def pdp_discovery_extra(request, slug: str):
    """Load below-the-fold More Like This and Recently Viewed products asynchronously."""
    product = get_object_or_404(selectors.product_detail_queryset().published(), slug=slug)
    discovery = merchandising.get_pdp_discovery_sections(product, request=request)
    context = {
        "product": product,
        "materials": product.materials.all(),
        "more_like_this": discovery["more_like_this"],
        "recently_viewed": discovery["recently_viewed"],
    }
    return render(request, "catalog/_pdp_discovery_extra.html", context)
