"""Products, variants and images -- the purchasable core of the catalogue.

The one decision everything else follows from: **a Product is a garment, a
ProductVariant is a SKU.**

A black oversized tee in five sizes is one Product and five ProductVariants, not five Products.
That split is what makes colour/size selection, price differences between sizes, per-SKU inventory
and per-SKU analytics possible later, without a migration to unpick a flat catalogue.

Money is ``DecimalField`` everywhere and always will be. ``FloatField`` cannot represent 1490.10
exactly, and a rounding error that small is still a rounding error that lands in an order total.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Prefetch, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.attributes import Color, Fit, Material, ProductTag, Size
from apps.catalog.models.base import SEOMixin, TimestampedModel
from apps.catalog.models.taxonomy import Brand, Category, Collection
from apps.catalog.validators import validate_catalog_image

ZERO = Decimal("0.00")

# Extensions the storage layer is willing to write. Anything else is stored with no extension at
# all: an SVG is executable markup and must never end up under a path a browser will run.
_SAFE_IMAGE_EXTENSIONS = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".webp": ".webp"}


def catalog_image_upload_to(instance: ProductImage, filename: str) -> str:
    """Return a safe, collision-proof storage path for an uploaded product image.

    Two deliberate choices:

    * the name is a random UUID, never the client's filename -- a name is attacker-controlled, and
      even after sanitising it would put whatever the uploader typed into a public URL;
    * the extension comes from a fixed allow-list keyed on the declared extension, so an
      unrecognised one yields a file with **no** extension rather than ``.svg``. The real format
      was verified by content in :mod:`apps.catalog.validators` before this runs; the extension
      here is only what browsers and CDNs key off.
    """
    from uuid import uuid4

    _, _, declared = (filename or "").rpartition(".")
    suffix = _SAFE_IMAGE_EXTENSIONS.get(f".{declared.lower()}", "")
    return f"catalog/products/{instance.product_id or 'unassigned'}/{uuid4().hex}{suffix}"


class ProductQuerySet(models.QuerySet):
    """Read-side helpers that encode what "publicly visible" means.

    Kept on the queryset (rather than in a selector module) so every call site inherits the same
    definition -- a view, the API and a management command cannot each invent their own idea of
    "published".
    """

    def published(self) -> ProductQuerySet:
        """Products a customer is allowed to see.

        Four conditions, all of them load-bearing:

        * ``status = ACTIVE`` -- the editorial decision;
        * ``published_at <= now`` -- a scheduled launch;
        * the category is active -- retiring a department must not leave its products on sale;
        * the brand is absent or active -- a suspended brand disappears with it.

        Deliberately *not* filtered on stock. Availability is inventory's business (Phase 4), and a
        product that is momentarily sold out is still the right page to show.
        """
        return self.filter(
            status=Product.Status.ACTIVE,
            published_at__lte=timezone.now(),
            category__is_active=True,
        ).filter(Q(brand__isnull=True) | Q(brand__is_active=True))

    def with_storefront_data(self) -> ProductQuerySet:
        """Eager-load everything a product card or detail page touches.

        A card reads product + brand + category + price + primary image + colour swatches. Without
        this that is six queries per card; with it the whole grid costs a fixed handful. Images
        pull their variant (and therefore their colour) along, because the gallery groups by
        colourway.
        """
        return self.select_related("brand", "category").prefetch_related(
            Prefetch("images", queryset=ProductImage.objects.select_related("variant__color")),
            "collections",
            Prefetch(
                "variants",
                queryset=ProductVariant.objects.filter(is_active=True).select_related(
                    "color", "size"
                ),
            ),
        )


class ProductManager(models.Manager.from_queryset(ProductQuerySet)):
    """Default manager, so ``Product.objects.published()`` is always available."""


class Product(TimestampedModel, SEOMixin):
    """A garment: the thing a shopper looks at and the thing a product page is about.

    Fields that belong to *selling* it rather than *describing* it (price, stock, SKU) live on
    :class:`ProductVariant` instead. A Product here has no price column, no stock column and no
    colour -- only the attributes common to every variant of the garment plus merchandising and
    SEO.
    """

    class Status(models.TextChoices):
        """Editorial lifecycle.

        There is no ``OUT_OF_STOCK`` state, and that is a real decision rather than an omission.
        Availability is *derived* from inventory: a product whose variants are all sold out stays
        ``ACTIVE`` with a "sold out" badge, so restocking is a stock edit rather than a
        re-publish, and so ``ACTIVE`` keeps meaning one thing -- "this is a live product".

        ``ARCHIVED`` is for a product that is retired: hidden from the storefront and from search,
        kept for its orders and analytics. ``DRAFT`` is work in progress.
        """

        DRAFT = "draft", _("Draft")
        ACTIVE = "active", _("Active")
        ARCHIVED = "archived", _("Archived")

    class Sort(models.TextChoices):
        """The storefront's fixed ordering choices (search itself is Phase 4)."""

        NEWEST = "newest", _("Newest first")
        PRICE_ASC = "price_asc", _("Price: low to high")
        PRICE_DESC = "price_desc", _("Price: high to low")
        FEATURED = "featured", _("Featured")
        NAME_ASC = "name_asc", _("Name: A to Z")
        NAME_DESC = "name_desc", _("Name: Z to A")

    name = models.CharField(_("name"), max_length=180)
    slug = models.SlugField(_("slug"), max_length=200, unique=True)
    short_description = models.CharField(_("short description"), max_length=300, blank=True)
    description = models.TextField(
        _("description"),
        blank=True,
        help_text=_("Fabric, fit notes, care. Plain text for now; rich text is a later phase."),
    )

    brand = models.ForeignKey(
        Brand,
        on_delete=models.SET_NULL,
        related_name="products",
        null=True,
        blank=True,
        verbose_name=_("brand"),
        help_text=_("Leave empty for an in-house FLASHWEAR product with no external label."),
    )
    # One category, pointing at the *most specific* node ("Men > T-Shirts"). The tree above it is
    # the department, reachable through ``category.ancestors()``, so a separate ``subcategory``
    # column would be a second source of truth that can contradict the tree.
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="products",
        verbose_name=_("category"),
        help_text=_("The most specific category. Its parents become the breadcrumb."),
    )
    fit = models.ForeignKey(
        Fit,
        on_delete=models.SET_NULL,
        related_name="products",
        null=True,
        blank=True,
        verbose_name=_("fit"),
    )
    collections = models.ManyToManyField(
        Collection,
        related_name="products",
        blank=True,
        verbose_name=_("collections"),
        help_text=_("A product can be in as many collections as merchandising needs."),
    )
    materials = models.ManyToManyField(
        Material,
        related_name="products",
        blank=True,
        verbose_name=_("materials"),
    )
    tags = models.ManyToManyField(
        ProductTag, related_name="products", blank=True, verbose_name=_("tags")
    )

    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        help_text=_("Only Active products are visible in the storefront."),
    )
    is_featured = models.BooleanField(
        _("featured"),
        default=False,
        help_text=_("Homepage and merchandising slots. A business-critical flag, not a tag."),
    )
    is_new = models.BooleanField(
        _("new"),
        default=False,
        help_text=_("Shows the New badge and the 'newest' ordering."),
    )
    published_at = models.DateTimeField(
        _("published at"),
        null=True,
        blank=True,
        help_text=_("Stamped the first time the product goes live. Never cleared."),
    )

    objects = ProductManager()

    class Meta:
        verbose_name = _("product")
        verbose_name_plural = _("products")
        ordering = ("-created_at", "-id")
        constraints = [
            # A *live* product is always dated, so "newest first" and scheduled launches have
            # something to sort on. Product.save() stamps it; this is the backstop.
            #
            # Draft and archived are both allowed to be undated, and both have to be: a draft has
            # never been published, and archiving a draft is a normal way to abandon one. Writing
            # this as "not (draft or archived)" instead -- which the first version did -- required a
            # date on exactly the two states that cannot have one.
            models.CheckConstraint(
                condition=Q(published_at__isnull=False) | Q(status__in=["draft", "archived"]),
                name="catalog_product_live_has_published_at",
            ),
        ]
        indexes = [
            # The listing query: published products, newest first.
            models.Index(fields=["status", "-published_at"], name="cat_prod_status_pub_idx"),
            models.Index(fields=["category", "status"], name="cat_prod_category_status_idx"),
            models.Index(fields=["brand", "status"], name="cat_prod_brand_status_idx"),
            models.Index(fields=["is_new", "-created_at"], name="cat_prod_new_created_idx"),
            models.Index(fields=["-created_at"], name="cat_prod_created_idx"),
            # The homepage's featured shelf: equality on is_featured, published newest
            # first. Without it the featured filter was a scan of the listing index.
            models.Index(fields=["is_featured", "-published_at"], name="cat_prod_featured_idx"),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("catalog:product-detail", kwargs={"slug": self.slug})

    # -- lifecycle ----------------------------------------------------------------------

    @property
    def is_published(self) -> bool:
        """Whether this product is live right now.

        Separate from ``status == ACTIVE`` on purpose: a product scheduled for next week is ACTIVE
        in the admin and not yet on the storefront.
        """
        return (
            self.status == self.Status.ACTIVE
            and self.published_at is not None
            and self.published_at <= timezone.now()
        )

    @property
    def purchasable_variants(self) -> list[ProductVariant]:
        """Variants a customer may currently choose, as a list.

        Availability proper (quantity on hand) belongs to the Phase 4 inventory app. Until then
        "purchasable" means exactly what it says: an active variant of a live product.

        Returns the prefetched list when the caller prefetched it -- which
        :meth:`ProductQuerySet.with_storefront_data` does -- so a grid of twelve cards costs one
        variant query in total instead of twelve.
        """
        if "variants" in getattr(self, "_prefetched_objects_cache", {}):
            return [variant for variant in self.variants.all() if variant.is_active]
        # ``select_related`` matters here: ``available_colors`` and the gallery reach for
        # ``variant.color`` on every variant, which would be one query each without it.
        return list(self.variants.filter(is_active=True).select_related("color", "size"))

    @property
    def available_colors(self) -> list[Color]:
        """Distinct active colours across purchasable variants, in swatch order.

        Inactive colours are skipped: deactivating a colourway retires it from the storefront, and
        a swatch a shopper can click but not buy is worse than no swatch at all.
        """
        found: dict[int, Color] = {}
        for variant in self.purchasable_variants:
            if variant.color_id:
                colour = variant.color
                if colour is not None and colour.is_active:
                    found.setdefault(variant.color_id, colour)
        return sorted(found.values(), key=lambda color: (color.display_order, color.name))

    @property
    def available_sizes(self) -> list[Size]:
        """Distinct active sizes across purchasable variants, in display order."""
        found: dict[int, Size] = {}
        for variant in self.purchasable_variants:
            if variant.size_id:
                option = variant.size
                if option is not None and option.is_active:
                    found.setdefault(variant.size_id, option)
        return sorted(found.values(), key=lambda size: (size.display_order, size.code))

    @property
    def price_range(self) -> tuple[Decimal | None, Decimal | None]:
        """``(lowest, highest)`` price across purchasable variants.

        Three sources, cheapest first:

        1. the ``price_min`` / ``price_max`` annotations when the queryset applied them, so a
           price-sorted grid stays a database sort;
        2. the prefetched variant list, so an ordinary grid costs no extra query at all;
        3. one aggregate query, for a product fetched on its own.

        A product with no purchasable variant returns ``(None, None)`` rather than a misleading
        zero, and ``(low, low)`` when every variant costs the same -- which is what lets a card
        print a single price instead of a range nobody needs.
        """
        if hasattr(self, "price_min"):
            return self.price_min, self.price_max

        if "variants" in getattr(self, "_prefetched_objects_cache", {}):
            prices = [variant.price for variant in self.purchasable_variants]
            if not prices:
                return None, None
            return min(prices), max(prices)

        prices = self.variants.filter(is_active=True).aggregate(
            low=models.Min("price"), high=models.Max("price")
        )
        return prices["low"], prices["high"]

    @property
    def is_on_sale(self) -> bool:
        """Whether any purchasable variant carries a compare-at price above its price."""
        return any(variant.is_discounted for variant in self.purchasable_variants)

    @property
    def primary_image(self) -> ProductImage | None:
        """The card image: the flagged primary, else the first by position.

        Works off the prefetched ``images`` relation when the caller prefetched it, so a grid of
        twelve cards costs one extra query rather than twelve.
        """
        images = list(self.images.all())
        for image in images:
            if image.is_primary:
                return image
        return images[0] if images else None

    # -- validation ---------------------------------------------------------------------

    def clean(self):
        """Cross-field rules a form can act on.

        The hard invariants (unique slug, live products are dated) are database constraints; this
        is the layer that turns them into an inline error an operator can fix without reading a
        traceback.
        """
        super().clean()
        if self.status == self.Status.ACTIVE and self.category_id and not self.category.is_active:
            raise ValidationError(
                {
                    "category": _(
                        "This category is inactive. Reactivate it, or move the product, "
                        "before publishing."
                    )
                },
                code="inactive_category",
            )
        if self.brand_id and self.brand.is_active is False and self.status == self.Status.ACTIVE:
            raise ValidationError(
                {"brand": _("This brand is inactive. Reactivate it before publishing.")},
                code="inactive_brand",
            )

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        if not self.slug:
            from apps.catalog.slugs import unique_slug

            self.slug = unique_slug(Product, self.name, instance=self, max_length=200)
        if self.status == self.Status.ACTIVE and self.published_at is None:
            # First publication is stamped once and then left alone, so a re-publish does not push
            # the product back to the top of "newest first".
            self.published_at = timezone.now()
        super().save(*args, **kwargs)


class ProductVariant(TimestampedModel):
    """One purchasable configuration of a product: a SKU.

    A variant owns price, compare-at price, internal cost, SKU, barcode and the colour/size pair
    that identifies it. Stock is deliberately absent -- the Phase 4 inventory app will own
    quantities, and a column here would create two truths that disagree.

    ``color`` and ``size`` are nullable so accessories with no colourway ("Gift card") or
    one-size items (a belt) are representable. Partial unique indexes keep (product, colour, size)
    unique across all four null combinations, which a single ``UNIQUE`` constraint could not do:
    SQL treats NULLs as distinct from each other.
    """

    sku = models.CharField(_("SKU"), max_length=64, unique=True)
    barcode = models.CharField(
        _("barcode"),
        max_length=64,
        blank=True,
        help_text=_("EAN/UPC. Unique when set."),
    )
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="variants", verbose_name=_("product")
    )
    color = models.ForeignKey(
        Color,
        on_delete=models.PROTECT,
        related_name="variants",
        null=True,
        blank=True,
        verbose_name=_("color"),
    )
    size = models.ForeignKey(
        Size,
        on_delete=models.PROTECT,
        related_name="variants",
        null=True,
        blank=True,
        verbose_name=_("size"),
    )
    price = models.DecimalField(
        _("price"),
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(ZERO)],
    )
    compare_at_price = models.DecimalField(
        _("compare at price"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("The 'was' price. Must be higher than the price when set."),
    )
    cost_price = models.DecimalField(
        _("cost price"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Internal only. Never shown in the storefront or the public API."),
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_("Inactive variants are hidden from the storefront but keep their SKU."),
    )

    class Meta:
        verbose_name = _("product variant")
        verbose_name_plural = _("product variants")
        ordering = ("product__name", "color__display_order", "size__display_order", "sku")
        constraints = [
            models.CheckConstraint(
                condition=Q(price__gte=0), name="catalog_variant_price_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(compare_at_price__isnull=True) | Q(compare_at_price__gte=F("price")),
                name="catalog_variant_compare_at_above_price",
            ),
            models.CheckConstraint(
                condition=Q(cost_price__isnull=True) | Q(cost_price__gte=0),
                name="catalog_variant_cost_non_negative",
            ),
            models.UniqueConstraint(
                fields=["barcode"],
                condition=~Q(barcode=""),
                name="catalog_variant_unique_barcode",
            ),
            # One variant per colour/size combination, per product. Four partial indexes because a
            # single UNIQUE(product, color, size) would not constrain rows where either column is
            # NULL -- every NULL compares as distinct, so duplicates would slip through.
            models.UniqueConstraint(
                fields=["product", "color", "size"],
                condition=Q(color__isnull=False, size__isnull=False),
                name="catalog_variant_unique_color_size",
            ),
            models.UniqueConstraint(
                fields=["product", "size"],
                condition=Q(color__isnull=True, size__isnull=False),
                name="catalog_variant_unique_size_only",
            ),
            models.UniqueConstraint(
                fields=["product", "color"],
                condition=Q(color__isnull=False, size__isnull=True),
                name="catalog_variant_unique_color_only",
            ),
            models.UniqueConstraint(
                fields=["product"],
                condition=Q(color__isnull=True, size__isnull=True),
                name="catalog_variant_unique_default_only",
            ),
        ]
        indexes = [
            models.Index(fields=["product", "is_active"], name="cat_var_product_active_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.sku} ({self.option_label})" if self.option_label else self.sku

    def get_absolute_url(self) -> str:
        """Link to the product page, focused on this variant."""
        return f"{self.product.get_absolute_url()}#variant-{self.pk}"

    # -- pricing ------------------------------------------------------------------------

    @property
    def is_discounted(self) -> bool:
        return self.compare_at_price is not None and self.compare_at_price > self.price

    @property
    def discount_percent(self) -> int:
        """Whole-percent saving, or ``0`` when not discounted.

        Rounded rather than truncated: "-33%" for 1490 against 1990 reads better than "-25%"
        would be a lie and "-26%" reads like a rounding bug.
        """
        if not self.is_discounted or not self.compare_at_price:
            return 0
        saving = (self.compare_at_price - self.price) / self.compare_at_price * 100
        return round(saving)

    @property
    def option_label(self) -> str:
        """Human label for the chosen options, e.g. ``Black / M``."""
        parts = [
            part
            for part in (
                self.color.name if self.color else "",
                self.size.name if self.size else "",
            )
            if part
        ]
        return " / ".join(parts)

    @property
    def margin_percent(self) -> int | None:
        """Gross margin against ``cost_price``; ``None`` when no cost is recorded.

        Admin-only. It lives here as a property so the number is derived, never stored twice.
        """
        if self.cost_price is None or self.cost_price <= ZERO:
            return None
        return round((self.price - self.cost_price) / self.price * 100)

    def save(self, *args, **kwargs):
        # SKUs are printed on labels, scanned by staff and typed into support tickets: one
        # canonical form, always upper case and trimmed.
        self.sku = (self.sku or "").strip().upper()
        self.barcode = (self.barcode or "").strip()
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors: dict[str, ValidationError] = {}
        if self.compare_at_price is not None and self.price is not None:
            if self.compare_at_price < self.price:
                errors["compare_at_price"] = ValidationError(
                    _("The compare-at price must be higher than the price."),
                    code="compare_at_below_price",
                )
        if self.cost_price is not None and self.price is not None and self.cost_price > self.price:
            # Not a database error (a loss-making line is legal) but always a mistake worth a
            # nudge in the admin.
            errors["cost_price"] = ValidationError(
                _("The cost price is higher than the selling price. Is that intended?"),
                code="cost_above_price",
            )
        if errors:
            raise ValidationError(errors)


class ProductImage(TimestampedModel):
    """One photograph of a product, optionally tied to a colour.

    Image ownership is the interesting decision, so it is spelled out here:

    * **Product-level images** (``variant`` empty) are shared by every colourway -- a flat-lay, a
      fabric close-up, a detail shot that does not change between colours. Stored once.
    * **Colour-level images** point at the variant of that colour, so selecting "Black" can show
      black photography without duplicating a file that is identical across colours.

    The link is to ``ProductVariant`` rather than ``Color`` on purpose: a brand may photograph a
    particular size differently (a size chart shot), and the variant is what the customer will
    eventually add to a bag. :attr:`color_id` reads through the variant, so "images for this
    colourway" is a single indexed lookup.
    """

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="images", verbose_name=_("product")
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.SET_NULL,
        related_name="images",
        null=True,
        blank=True,
        verbose_name=_("variant"),
        help_text=_("Set for colour-specific photography. Leave empty for shared images."),
    )
    image = models.ImageField(
        _("image"),
        upload_to=catalog_image_upload_to,
        # Model-level so every surface -- admin inlines, operations forms, imports --
        # runs the same content-first check rather than trusting its own form.
        validators=[validate_catalog_image],
    )
    alt_text = models.CharField(
        _("alt text"),
        max_length=200,
        help_text=_("Describe the image for screen readers and when the image fails to load."),
    )
    position = models.PositiveIntegerField(
        _("position"),
        default=0,
        help_text=_("Order within the gallery. Lower numbers come first."),
    )
    is_primary = models.BooleanField(
        _("primary image"),
        default=False,
        help_text=_("Used on product cards. Exactly one per product once images exist."),
    )
    source_url = models.URLField(
        _("source URL"),
        blank=True,
        default="",
        help_text=_("Original external image URL, if imported from the web."),
    )
    photographer = models.CharField(
        _("photographer"),
        max_length=200,
        blank=True,
        help_text=_("Credit for the image source."),
    )
    attribution = models.CharField(
        _("attribution"),
        max_length=255,
        blank=True,
        help_text=_("Attribution statement or copyright notice for external image."),
    )
    license = models.CharField(
        _("license"),
        max_length=200,
        blank=True,
        help_text=_("License or usage terms for the external image."),
    )

    class Meta:
        verbose_name = _("product image")
        verbose_name_plural = _("product images")
        ordering = ("position", "id")
        constraints = [
            # At most one primary. "At least one" is enforced on write: the first image for a
            # product becomes primary, and deleting the primary promotes its successor.
            models.UniqueConstraint(
                fields=["product"],
                condition=Q(is_primary=True),
                name="catalog_image_one_primary_per_product",
            ),
        ]
        indexes = [
            models.Index(fields=["product", "position"], name="cat_image_product_pos_idx"),
            models.Index(fields=["variant", "position"], name="cat_image_variant_pos_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.product.name} image {self.position}"

    @property
    def color(self) -> Color | None:
        """The colour this image belongs to, via its variant."""
        return self.variant.color if self.variant_id and self.variant else None

    @property
    def color_id(self) -> int | None:
        variant = self._state.fields_cache.get("variant")
        if variant is not None:
            return variant.color_id
        return self.variant.color_id if self.variant_id else None

    @property
    def image_source(self) -> str:
        """Indicator showing whether image was uploaded directly or from an external URL."""
        return "External URL" if bool(self.source_url) else "Uploaded File"

    def clean(self):
        super().clean()
        if self.variant_id and self.product_id and self.variant.product_id != self.product_id:
            raise ValidationError(
                {"variant": _("That variant belongs to a different product.")},
                code="variant_product_mismatch",
            )

    def save(self, *args, **kwargs):
        if self.variant_id and self.product_id:
            self._guard_variant_product()
        self._enforce_single_primary()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Delete the file, then promote a successor so a product never loses its card image."""
        was_primary = self.is_primary
        product_id = self.product_id
        super().delete(*args, **kwargs)
        if was_primary and product_id:
            successor = (
                ProductImage.objects.filter(product_id=product_id)
                .order_by("position", "id")
                .first()
            )
            if successor is not None:
                successor.is_primary = True
                successor.save(update_fields=["is_primary", "updated_at"])

    # -- internals ----------------------------------------------------------------------

    def _guard_variant_product(self) -> None:
        """A colour-specific image cannot point at another product's variant."""
        if ProductVariant.objects.filter(pk=self.variant_id, product_id=self.product_id).exists():
            return
        raise ValidationError(
            {"variant": _("That variant belongs to a different product.")},
            code="variant_product_mismatch",
        )

    def _enforce_single_primary(self) -> None:
        """Keep the "exactly one primary per product" rule true on every write.

        Two cases, both cheap:

        * promoting an image demotes whichever row held the flag, in SQL rather than by looping
          the relation, so the partial unique index never has to catch us;
        * the *first* image for a product is promoted automatically, which is why a freshly
          uploaded product always has a card image without anyone ticking a box.
        """
        others = ProductImage.objects.filter(product_id=self.product_id)
        if self.pk:
            others = others.exclude(pk=self.pk)

        if self.is_primary:
            # ``update()`` skips auto_now, so stamp it explicitly for the demoted rows.
            others.filter(is_primary=True).update(is_primary=False, updated_at=timezone.now())
        elif not others.exists():
            self.is_primary = True
