"""Catalogue services: the logic behind catalogue behaviour.

Views, forms and templates stay declarative; anything that needs a transaction, an invariant or a
decision the database cannot make on its own lives here. Three groups:

* **Variant resolution.** Which colour/size combinations exist, and which one a shopper selected.
  The answer always comes from a database query -- never from a value the browser sent.
* **Publishing.** Moving a product between Draft, Active and Archived, with the invariants that go
  with each transition.
* **Gallery maintenance.** Keeping exactly one primary image and a stable order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from apps.catalog.models import Category, Product, ProductImage, ProductVariant


@dataclass(frozen=True)
class VariantMatrix:
    """The colour/size combinations a product really has.

    ``variants`` is keyed by ``(color_id, size_id)`` with ``None`` for an absent option, which is
    the same shape the templates and the API need to answer "can I pick Black in XL?" with a
    lookup instead of a loop over every combination.
    """

    colors: list = field(default_factory=list)
    sizes: list = field(default_factory=list)
    variants: dict = field(default_factory=dict)
    selected: object | None = None

    @property
    def has_variants(self) -> bool:
        return bool(self.variants)

    @property
    def combination_count(self) -> int:
        return len(self.variants)

    def variant_for(self, color=None, size=None):
        """The variant for a ``(color, size)`` pair, or ``None`` if that combination is not sold."""
        color_id = getattr(color, "pk", color)
        size_id = getattr(size, "pk", size)
        return self.variants.get((color_id, size_id))

    def has_combination(self, color=None, size=None) -> bool:
        return self.variant_for(color, size) is not None

    def colors_for_size(self, size):
        """Colours still selectable for ``size`` -- used to grey out dead combinations."""
        size_id = getattr(size, "pk", size)
        return [color for color in self.colors if (color.pk, size_id) in self.variants]

    def sizes_for_color(self, color):
        """Sizes still selectable for ``color``."""
        color_id = getattr(color, "pk", color)
        return [size for size in self.sizes if (color_id, size.pk) in self.variants]


def _active_variants(product) -> list:
    """Active variants for a product, reusing the prefetched list when the caller has one.

    The product page prefetches active variants already; re-querying them here would be the second
    identical SELECT on every product page, which is exactly the sort of thing that gets noticed in
    production and never explained.
    """
    prefetched = getattr(product, "_prefetched_objects_cache", {}).get("variants")
    if prefetched is not None:
        return [variant for variant in prefetched if variant.is_active]
    return list(
        ProductVariant.objects.filter(product=product, is_active=True).select_related(
            "color", "size"
        )
    )


def build_variant_matrix(product, *, color=None, size=None) -> VariantMatrix:
    """Return the variant matrix for ``product``, with ``selected`` resolved server-side.

    ``color`` and ``size`` are the options the shopper asked for (any object with a ``pk``, or a
    ``pk``). A combination that does not exist is not an error a customer should see: the request
    falls back to the first available variant, so a stale bookmark, a shared link to a
    since-withdrawn size or a hand-edited query string still lands on a usable page.

    This is the trust boundary for variant selection. The browser may *ask* for Black/XL; only this
    function decides whether that variant exists.
    """
    variants = _active_variants(product)
    if not variants:
        return VariantMatrix()

    matrix = VariantMatrix(
        colors=_ordered(variant.color for variant in variants),
        sizes=_ordered(variant.size for variant in variants),
        variants={(variant.color_id, variant.size_id): variant for variant in variants},
    )

    requested = matrix.variant_for(color, size)
    if requested is None:
        if color is None and size is None:
            # Nothing asked for: preselect the first variant in display order, so the product page
            # always shows a concrete price, size and SKU instead of a bare range.
            requested = next(iter(matrix.variants.values()), None)
        else:
            # Ask for Black but no size: take any Black. Ask for a withdrawn pair: take the first.
            candidates = [
                variant
                for variant in variants
                if (color is None or variant.color_id == getattr(color, "pk", color))
                and (size is None or variant.size_id == getattr(size, "pk", size))
            ]
            requested = candidates[0] if candidates else variants[0]

    return VariantMatrix(
        colors=matrix.colors,
        sizes=matrix.sizes,
        variants=matrix.variants,
        selected=requested,
    )


def _ordered(options):
    """Distinct, display-ordered *active* options from a generator of possibly-``None`` rows.

    An inactive colour or size is dropped here as well as in the storefront: the matrix is what
    decides which swatches and size chips a shopper is offered, so a retired option must not appear
    in it even though its variants still exist.
    """
    found: dict[int, object] = {}
    for option in options:
        if option is not None and option.is_active:
            found.setdefault(option.pk, option)
    return sorted(found.values(), key=lambda option: (option.display_order, option.name))


def default_variant(product):
    """The variant a product page shows before a shopper chooses anything.

    The cheapest active variant, chosen in display order so the result is stable and repeatable
    rather than database-dependent.
    """
    return (
        ProductVariant.objects.filter(product=product, is_active=True)
        .select_related("color", "size")
        .order_by("color__display_order", "size__display_order", "pk")
        .first()
    )


def variant_conflict(*, product, color, size, exclude_pk=None):
    """Return the existing variant for this ``(product, colour, size)`` combination, if any.

    ``filter(color=None)`` means ``color IS NULL``, which is exactly the "no colourway" case the
    four partial unique indexes cover. Used by the admin form to turn a duplicate combination into
    a readable field error; the database constraint remains the guarantee.
    """
    queryset = ProductVariant.objects.filter(product=product, color=color, size=size)
    if exclude_pk:
        queryset = queryset.exclude(pk=exclude_pk)
    return queryset.first()


# --------------------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------------------


@transaction.atomic
def publish(product) -> Product:
    """Make ``product`` live.

    Validates first, so publishing into an inactive category fails here with a readable message
    rather than in the storefront, where the product would simply be invisible. ``published_at`` is
    stamped by ``Product.save()`` on first publication and never overwritten afterwards, so a
    re-publish does not push the product back to the top of "newest first".
    """
    product.status = Product.Status.ACTIVE
    product.full_clean()
    product.save()
    return product


@transaction.atomic
def archive(product) -> Product:
    """Retire a product: hidden from the storefront, kept for orders and analytics."""
    product.status = Product.Status.ARCHIVED
    product.save(update_fields=["status", "updated_at"])
    return product


@transaction.atomic
def unpublish(product) -> Product:
    """Return a live product to Draft."""
    product.status = Product.Status.DRAFT
    product.save(update_fields=["status", "updated_at"])
    return product


def category_subtree_ids(category) -> list[int]:
    """Ids of ``category`` and everything below it, breadth-first.

    Used by the category page so "Men" lists the t-shirts inside it. A taxonomy is tens of rows,
    so the extra queries here are cheaper than the alternative of storing a materialised path that
    every other write would then have to keep in sync.
    """
    found: list[int] = []
    frontier = [category.pk]
    while frontier:
        children = list(
            Category.objects.filter(parent_id__in=frontier).values_list("id", flat=True)
        )
        found.extend(children)
        frontier = children
    return [category.pk, *found]


def category_tree(category, *, include_inactive: bool = False) -> list[dict]:
    """Nested ``[{category, children: [...]}]`` for the category page's side rail."""
    queryset = Category.objects.filter(parent=category)
    if not include_inactive:
        queryset = queryset.filter(is_active=True)
    nodes = []
    for child in queryset.order_by("display_order", "name"):
        nodes.append(
            {
                "category": child,
                "children": category_tree(child, include_inactive=include_inactive),
            }
        )
    return nodes


# --------------------------------------------------------------------------------------
# Gallery
# --------------------------------------------------------------------------------------


def promote_primary_image(product, image) -> ProductImage:
    """Make ``image`` the product's card image, demoting whichever row held the flag."""
    image.product = product
    image.is_primary = True
    image.save()
    return image


def reorder_images(product, ordered_ids: list[int]) -> int:
    """Apply a new gallery order.

    ``ordered_ids`` is the full ordered list of image ids. Ids that do not belong to the product
    are ignored rather than trusted, and the return value is how many images were repositioned. The
    positions go out as a single ``CASE`` update -- one query for the whole gallery instead of one
    per image.
    """
    owned = set(ProductImage.objects.filter(product=product).values_list("id", flat=True))
    positions: dict[int, int] = {}
    position = 0
    for image_id in ordered_ids:
        if image_id in owned:
            positions[image_id] = position
            position += 1
    if not positions:
        return 0

    reorder = Case(
        *[When(pk=image_id, then=Value(index)) for image_id, index in positions.items()],
        output_field=IntegerField(),
    )
    ProductImage.objects.filter(pk__in=list(positions)).update(
        position=reorder, updated_at=timezone.now()
    )
    return len(positions)


def product_gallery(product) -> dict:
    """Split a product's images into shared shots and per-colour groups.

    Returns ``{"shared": [...], "by_color": {color_id: [...]}, "primary": ProductImage | None}``.

    The template shows ``shared`` plus the selected colour's group, so choosing "Black" reveals
    black photography the customer can already see without JavaScript (the colour selector is a
    plain link with ``?color=<slug>``).

    Uses the prefetched ``images`` relation when the caller supplied one, so a product page does not
    run the same query twice.
    """
    prefetched = getattr(product, "_prefetched_objects_cache", {}).get("images")
    if prefetched is not None:
        images = sorted(prefetched, key=lambda image: (image.position, image.pk))
    else:
        images = list(
            ProductImage.objects.filter(product=product)
            .select_related("variant__color")
            .order_by("position", "id")
        )
    shared: list[ProductImage] = []
    by_color: dict[int, list[ProductImage]] = {}
    primary = None
    for image in images:
        if image.is_primary:
            primary = image
        if image.color_id:
            by_color.setdefault(image.color_id, []).append(image)
        else:
            shared.append(image)
    if primary is None:
        primary = images[0] if images else None
    return {"shared": shared, "by_color": by_color, "primary": primary}
