"""Catalogue API views (``/api/v1/products/`` and friends).

Public, read-only and deliberately stateless:

* ``permission_classes = [AllowAny]`` is written out on every view rather than inherited, so
  "is the catalogue public?" never depends on a project default someone might change;
* ``authentication_classes = []`` because there is nothing to authenticate. Reading a public
  catalogue should not cost a session lookup per request;
* responses are ``never_cache``: prices, sale flags and collection windows change without a deploy.

The same rule as the storefront applies to *visibility*: everything resolves through
``Product.objects.published()``, so a draft or archived product is a 404 here exactly as it is on
the website. A half-public catalogue -- visible in the API but not on the website -- is the kind of
mistake nobody notices until it ships.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.services import record_event
from apps.catalog import selectors
from apps.catalog.models import Brand, Category, Product
from apps.catalog.serializers import (
    BrandSerializer,
    CategorySerializer,
    CollectionSerializer,
    ProductDetailSerializer,
    ProductListSerializer,
    VisualSearchResultSerializer,
)
from apps.catalog.visual_discovery import (
    find_similar_to_product,
    find_visually_similar_products,
)

# Reused everywhere a client might want to narrow the list. Slugs, not ids: a client that stores a
# product's category should not break when the taxonomy is re-imported with new primary keys.
FILTER_PARAMETERS = ("category", "brand", "collection")


class CatalogPagination(PageNumberPagination):
    """Page-number pagination with a bounded, client-controllable page size.

    ``page_size`` is accepted from the query string but clamped to
    ``CATALOG_API_MAX_PAGE_SIZE``: without the ceiling, ``?page_size=100000`` turns a catalogue
    endpoint into a full-table dump.
    """

    page_size_query_param = "page_size"
    max_page_size = settings.CATALOG_API_MAX_PAGE_SIZE
    page_size = settings.CATALOG_API_PAGE_SIZE


def _slug(request, name: str) -> str | None:
    """Read a slug filter from the query string, normalised to lowercase and empty-safe."""
    value = (request.query_params.get(name) or "").strip().lower()
    return value or None


def _sort(request) -> str:
    """Resolve ``?sort=`` against the same allow-list the storefront uses."""
    return selectors.resolve_sort(request.query_params.get("sort"))


def _published_filter(prefix: str = "") -> Q:
    """A ``Q`` object matching *published* rows through a relation prefix.

    Used for counts: a category with four draft products must report zero, not four.
    """
    join = f"{prefix}__" if prefix else ""
    return Q(**{f"{join}status": Product.Status.ACTIVE, f"{join}published_at__lte": timezone.now()})


def annotate_published_counts(queryset, *, relation: str):
    """Annotate ``published_product_count`` using the relation name a model exposes."""
    return queryset.annotate(
        published_product_count=Count(relation, filter=_published_filter(relation), distinct=True)
    )


class ProductListView(ListAPIView):
    """``GET /api/v1/products/`` -- the published catalogue.

    Supports ``?category=<slug>``, ``?brand=<slug>``, ``?collection=<slug>``, ``?sort=`` and
    ``?page_size=``. Unknown filter values simply return nothing rather than 400: a stale bookmark
    should show an empty shelf, not an error page.

    Phase 4 additions:
    - ``?q=`` -- full-text search
    - ``?color=`` -- color slug(s), comma-separated
    - ``?size=`` -- size code(s), comma-separated
    - ``?material=`` -- material slug(s), comma-separated
    - ``?fit=`` -- fit slug(s), comma-separated
    - ``?tag=`` -- tag slug(s), comma-separated
    - ``?min_price=`` / ``?max_price=`` -- price range
    - ``?sort=name_asc|name_desc`` -- additional sort options
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = ProductListSerializer
    pagination_class = CatalogPagination

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        # ``with_price_range`` is applied unconditionally: the serializer reads ``price_min`` /
        # ``price_max`` as ``DecimalField``, so the annotations have to be there on every request,
        # not only on a price sort.
        queryset = selectors.with_price_range(
            selectors.storefront_products(sort=_sort(self.request))
        )
        if category := _slug(self.request, "category"):
            queryset = queryset.filter(category__slug=category)
        if brand := _slug(self.request, "brand"):
            queryset = queryset.filter(brand__slug=brand)
        if collection := _slug(self.request, "collection"):
            queryset = queryset.filter(collections__slug=collection).distinct()

        # Phase 4: search and filters
        q = (self.request.query_params.get("q") or "").strip()
        if q:
            queryset = selectors.apply_search(queryset, q)

        # Filters (comma-separated for multi-value)
        if color := (self.request.query_params.get("color") or "").strip():
            slugs = [s.strip().lower() for s in color.split(",") if s.strip()]
            color_q = Q()
            for slug in slugs:
                color_q |= Q(variants__color__slug=slug, variants__is_active=True)
            queryset = queryset.filter(color_q)

        if size := (self.request.query_params.get("size") or "").strip():
            codes = [s.strip().upper() for s in size.split(",") if s.strip()]
            size_q = Q()
            for code in codes:
                size_q |= Q(variants__size__code__iexact=code, variants__is_active=True)
            queryset = queryset.filter(size_q)

        if material := (self.request.query_params.get("material") or "").strip():
            slugs = [s.strip().lower() for s in material.split(",") if s.strip()]
            mat_q = Q()
            for slug in slugs:
                mat_q |= Q(materials__slug=slug, materials__is_active=True)
            queryset = queryset.filter(mat_q)

        if fit := (self.request.query_params.get("fit") or "").strip():
            slugs = [s.strip().lower() for s in fit.split(",") if s.strip()]
            fit_q = Q()
            for slug in slugs:
                fit_q |= Q(fit__slug=slug, fit__is_active=True)
            queryset = queryset.filter(fit_q)

        if tag := (self.request.query_params.get("tag") or "").strip():
            slugs = [s.strip().lower() for s in tag.split(",") if s.strip()]
            tag_q = Q()
            for slug in slugs:
                tag_q |= Q(tags__slug=slug, tags__is_active=True)
            queryset = queryset.filter(tag_q)

        # Price range
        min_price = (self.request.query_params.get("min_price") or "").strip()
        max_price = (self.request.query_params.get("max_price") or "").strip()
        if min_price or max_price:
            queryset = selectors.with_price_range(queryset)
            if min_price:
                try:
                    queryset = queryset.filter(price_max__gte=Decimal(min_price.strip()))
                except Exception:
                    pass
            if max_price:
                try:
                    queryset = queryset.filter(price_min__lte=Decimal(max_price.strip()))
                except Exception:
                    pass

        # Sort
        sort = _sort(self.request)
        queryset = selectors.apply_sorting(queryset, sort)

        return queryset


class ProductDetailView(RetrieveAPIView):
    """``GET /api/v1/products/<slug>/`` -- one product with every purchasable variant.

    The eager loading comes entirely from :func:`apps.catalog.selectors.product_detail_queryset`.
    Re-declaring ``prefetch_related("variants")`` here would look harmless and is not: Django raises
    ``ValueError`` on a conflicting prefetch, and DRF's ``get_object_or_404`` converts a ValueError
    into a bare ``404 Not found`` -- which reads exactly like a missing product and sends you
    hunting for a data problem that does not exist.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = ProductDetailSerializer
    lookup_field = "slug"

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return selectors.with_price_range(selectors.product_detail_queryset().published())


class CategoryListView(ListAPIView):
    """``GET /api/v1/categories/`` -- the taxonomy, top level first, children attached.

    Only active categories: an inactive department is a merchandising decision, not public
    information. ``product_count`` is a subtree count, computed for the whole taxonomy in two
    queries rather than one query per row.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = CategorySerializer
    pagination_class = CatalogPagination

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return selectors.with_category_counts(
            Category.objects.filter(is_active=True, parent__isnull=True).prefetch_related(
                Prefetch("children", queryset=_active_children())
            )
        )


def _active_children():
    """Active direct children of a category, in the order the storefront shows them."""
    return Category.objects.filter(is_active=True).order_by("display_order", "name")


class CategoryDetailView(RetrieveAPIView):
    """``GET /api/v1/categories/<slug>/`` -- one category with its counts and children.

    ``get()`` is written out rather than inherited because the counts come from
    :func:`apps.catalog.selectors.with_category_counts`, which returns a list of annotated
    instances (that is the whole point -- two queries for the taxonomy instead of one per row) and a
    list is not something ``RetrieveAPIView`` can ``get()`` from.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = CategorySerializer
    lookup_field = "slug"

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        category = get_object_or_404(
            Category.objects.filter(is_active=True).prefetch_related(
                Prefetch("children", queryset=_active_children())
            ),
            slug=kwargs.get(self.lookup_field),
        )
        [category] = selectors.with_category_counts(
            Category.objects.filter(pk=category.pk).prefetch_related(
                Prefetch("children", queryset=_active_children())
            )
        )
        return Response(CategorySerializer(category).data)


class BrandListView(ListAPIView):
    """``GET /api/v1/brands/`` -- brands that have at least one published product."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = BrandSerializer
    pagination_class = CatalogPagination

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        # Only brands with something to show. An active brand whose products are all drafts would
        # otherwise appear in the list and 404 on its detail page, which is a worse experience than
        # it being absent.
        return annotate_published_counts(
            Brand.objects.filter(is_active=True).order_by("display_order", "name"),
            relation="products",
        ).filter(published_product_count__gt=0)


class BrandDetailView(RetrieveAPIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = BrandSerializer
    lookup_field = "slug"

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return annotate_published_counts(Brand.objects.filter(is_active=True), relation="products")


class CollectionListView(ListAPIView):
    """``GET /api/v1/collections/`` -- collections that are live right now."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = CollectionSerializer
    pagination_class = CatalogPagination

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return annotate_published_counts(selectors.live_collections(), relation="products")


class CollectionDetailView(RetrieveAPIView):
    """``GET /api/v1/collections/<slug>/`` -- one collection.

    Returns 404 for an inactive collection or one outside its window, mirroring the storefront.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = CollectionSerializer
    lookup_field = "slug"

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        # "Live" is part of the lookup, not a filter applied afterwards: an out-of-window collection
        # must be indistinguishable from one that does not exist.
        now = timezone.now()
        return (
            annotate_published_counts(selectors.live_collections(), relation="products")
            .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=now))
        )


class ProductSearchSuggestionsView(ListAPIView):
    """``GET /api/v1/products/suggestions/?q=<query>`` -- search suggestions for autocomplete.

    Returns a list of matching products, categories, brands, and collections.
    """

    permission_classes = [AllowAny]
    throttle_scope = "expensive"
    authentication_classes: list = []
    serializer_class = None  # We return raw data
    pagination_class = None

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        q = (request.query_params.get("q") or "").strip()
        suggestions = selectors.get_search_suggestions(q, limit=10)
        return Response(suggestions)


class ProductSearchView(ListAPIView):
    """``GET /api/v1/products/search/?q=<query>&...`` -- full search with filters.

    This is the programmatic equivalent of the storefront search page.
    """

    permission_classes = [AllowAny]
    throttle_scope = "expensive"
    authentication_classes: list = []
    serializer_class = ProductListSerializer
    pagination_class = CatalogPagination

    @method_decorator(never_cache)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        # Reuse ProductListView's queryset logic
        view = ProductListView()
        view.request = self.request
        view.format_kwarg = self.format_kwarg
        return view.get_queryset()


class ProductVisualSearchView(APIView):
    """``POST /api/v1/products/visual-search/`` -- search catalog by uploaded image."""

    permission_classes = [AllowAny]
    throttle_scope = "expensive"
    parser_classes = [MultiPartParser, FormParser]

    @method_decorator(never_cache)
    def post(self, request, *args, **kwargs):
        record_event(
            "image_search_started",
            request=request,
            metadata={"source": "api"},
        )
        image_file = request.FILES.get("image")
        if not image_file:
            record_event(
                "image_search_failed",
                request=request,
                metadata={"error_code": "empty_upload", "reason": "No image file provided"},
            )
            return Response(
                {"detail": "No image file was provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        category_slug = (request.data.get("category") or "").strip() or None
        limit_param = request.data.get("limit")
        limit = None
        if limit_param:
            try:
                limit = min(max(1, int(limit_param)), settings.CATALOG_API_MAX_PAGE_SIZE)
            except (ValueError, TypeError):
                pass

        try:
            results, features = find_visually_similar_products(
                image_file,
                category_slug=category_slug,
                user=request.user,
                limit=limit,
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
            return Response(
                {
                    "count": len(results),
                    "features": {
                        "dominant_colors": features.dominant_colors,
                        "matched_catalog_colors": features.matched_catalog_colors,
                        "brightness": features.brightness,
                        "tone": features.tone,
                        "aspect_ratio": features.aspect_ratio,
                    },
                    "results": VisualSearchResultSerializer(results, many=True).data,
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as error:
            msg = getattr(error, "message", str(error))
            code = getattr(error, "code", "invalid_image")
            record_event(
                "image_search_failed",
                request=request,
                metadata={"error_code": code, "reason": msg},
            )
            return Response(
                {"detail": msg, "code": code},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            record_event(
                "image_search_failed",
                request=request,
                metadata={"error_code": "server_error", "reason": "Processing failure"},
            )
            return Response(
                {"detail": "Could not analyze image.", "code": "server_error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ProductSimilarView(APIView):
    """``GET /api/v1/products/<slug>/similar/`` -- find similar products by product slug."""

    permission_classes = [AllowAny]
    throttle_scope = "expensive"

    @method_decorator(never_cache)
    def get(self, request, slug: str, *args, **kwargs):
        product = get_object_or_404(Product.objects.published(), slug=slug)
        record_event(
            "find_similar_used",
            request=request,
            object_type="product",
            object_id=product.pk,
            metadata={"product_slug": product.slug},
        )
        limit_param = request.query_params.get("limit")
        limit = 8
        if limit_param:
            try:
                limit = min(max(1, int(limit_param)), settings.CATALOG_API_MAX_PAGE_SIZE)
            except (ValueError, TypeError):
                pass

        results = find_similar_to_product(product, user=request.user, limit=limit)
        return Response(
            {
                "source_product": {
                    "id": product.pk,
                    "name": product.name,
                    "slug": product.slug,
                    "category": product.category.name,
                },
                "count": len(results),
                "results": VisualSearchResultSerializer(results, many=True).data,
            },
            status=status.HTTP_200_OK,
        )


class VisualProductClickView(APIView):
    """``POST /api/v1/products/visual-search/click/`` -- record visual product click."""

    permission_classes = [AllowAny]
    throttle_scope = "expensive"

    def post(self, request, *args, **kwargs):
        product_id = request.data.get("product_id") or request.query_params.get("product_id")
        if not product_id:
            return Response(
                {"detail": "product_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            prod_pk = int(product_id)
        except (ValueError, TypeError):
            return Response(
                {"detail": "Invalid product_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        record_event(
            "visual_product_clicked",
            request=request,
            object_type="product",
            object_id=prod_pk,
        )
        return Response({"status": "ok"}, status=status.HTTP_200_OK)
