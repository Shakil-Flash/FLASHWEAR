"""Seed the demo catalogue.

Why this exists: a storefront with an empty database is impossible to review. This command builds a
small, coherent FLASHWEAR catalogue -- two departments, real garments, real colour/size variants --
so the pages, the API and the admin all have something true to show.

Design decisions, because they shape everything below:

* **Idempotent by natural key.** Run it twice and you get the same catalogue, not duplicates. Every
  object is looked up by slug (taxonomy) or by ``(product, color, size)`` (variants), and
  the command updates in place. There is no ``--flush``, and it will not delete anything you have
  added.
* **Deterministic prices.** Derived from a base price and a per-variant multiplier, so re-running
  does not shuffle the price sort order.
* **Realistic, not exhaustive.** Twelve products is enough to exercise pagination, sorting, colour
  selection, sale pricing and category trees. It is not a production catalogue and does not pretend
  to be.
* **Images are optional.** Product images point at nothing by default; pass ``--with-images`` to
  generate simple solid-colour placeholders with Pillow, which is enough to verify the gallery,
  Open Graph tags and the responsive ``<img>`` markup without shipping binaries.

Usage::

    python manage.py seed_catalog
    python manage.py seed_catalog --with-images
    python manage.py seed_catalog --quiet
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.catalog import services
from apps.catalog.models import (
    Brand,
    Category,
    Collection,
    Color,
    Fit,
    Material,
    Product,
    ProductTag,
    ProductVariant,
    Size,
)
from apps.catalog.skus import build_sku

# --------------------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------------------

SIZES = [
    # (code, name, size_type, display_order). ``size_type`` must be one of
    # ``Size.SizeType``: the database has a check constraint on it, so a typo here fails loudly
    # rather than quietly producing a size that appears in no filter.
    ("XS", "Extra small", "clothing", 10),
    ("S", "Small", "clothing", 20),
    ("M", "Medium", "clothing", 30),
    ("L", "Large", "clothing", 40),
    ("XL", "Extra large", "clothing", 50),
    ("XXL", "Double extra large", "clothing", 60),
    ("28", "W28", "numeric", 10),
    ("30", "W30", "numeric", 20),
    ("32", "W32", "numeric", 30),
    ("34", "W34", "numeric", 40),
    ("36", "W36", "numeric", 50),
]

COLORS = [
    # (name, slug, hex_code, display_order)
    ("Black", "black", "#111827", 10),
    ("White", "white", "#f8fafc", 20),
    ("Sand", "sand", "#e7d8c9", 30),
    ("Olive", "olive", "#4d5d43", 40),
    ("Navy", "navy", "#1e3a5f", 50),
    ("Slate", "slate", "#64748b", 60),
    ("Clay", "clay", "#b4553f", 70),
]

FITS = [
    ("Regular", "A straight, everyday fit.", 10),
    ("Relaxed", "Roomy through the body and the sleeve.", 20),
    ("Oversized", "Deliberately roomy; size down for a neater line.", 30),
    ("Slim", "Cut close through the body.", 40),
    ("Tapered", "Full at the thigh and narrowing to the hem.", 50),
]

MATERIALS = [
    # (name, description, display_order). Materials carry a description only -- care instructions
    # are Phase 4 content that belongs with the garment, not the fibre.
    ("Organic cotton", "Grown without synthetic pesticides, knitted to a soft hand.", 10),
    ("Heavyweight cotton", "Dense 240gsm jersey that holds its shape after washing.", 20),
    ("Cotton-poly fleece", "Brushed-back fleece with a cotton face for warmth without weight.", 30),
    ("Recycled polyester", "Post-consumer plastic, spun for durability and quick drying.", 40),
    ("Cotton twill", "A hard-wearing diagonal weave for trousers and overshirts.", 50),
    ("Ripstop nylon", "Grid-stop nylon that resists tearing and shrugs off light rain.", 60),
]

TAGS = ["new-in", "core", "limited", "restock", "staff-pick"]

CATEGORIES = [
    # (name, slug, parent_slug, description, display_order)
    ("Men", "men", None, "Everyday essentials and outerwear, cut for the FLASHWEAR fit.", 10),
    ("Women", "women", None, "Relaxed shapes, soft fabrics and easy layers.", 20),
    ("T-Shirts", "t-shirts", "men", "Heavyweight cotton tees, the backbone of any wardrobe.", 10),
    ("Hoodies & Sweats", "hoodies-sweats", "men", "Fleece hoodies, crews and sweatpants.", 20),
    ("Shirts", "shirts", "men", "Oversized shirts and button-downs.", 30),
    ("Bottoms", "bottoms", "men", "Cargo pants, denim and everyday trousers.", 40),
    ("Tops", "tops", "women", "Tees, tanks and long sleeves.", 10),
    ("Dresses", "dresses", "women", "Day dresses and easy shapes.", 20),
    ("Outerwear", "outerwear", "men", "Jackets and shells for the in-between weeks.", 30),
]

BRANDS = [
    # (name, slug, description, display_order)
    (
        "FLASHWEAR",
        "flashwear",
        "The in-house label: the garments this platform was built to sell.",
        10,
    ),
    ("Northline", "northline", "Outerwear and workwear from a Dhaka workshop since 2014.", 20),
    ("Studio Kaar", "studio-kaar", "Small-batch knits and heavyweight tees.", 30),
]

COLLECTIONS = [
    # (name, slug, description, is_featured, starts_in_days, ends_in_days)
    (
        "Core Essentials",
        "core-essentials",
        "The pieces we never stop making, restocked and refined.",
        True,
        None,
        None,
    ),
    (
        "Midnight Run",
        "midnight-run",
        "Dark technical layers built for the last train home.",
        False,
        -14,
        45,
    ),
    (
        "Sand Season",
        "sand-season",
        "Warm neutrals and loose weaves for the long months.",
        True,
        None,
        120,
    ),
]

# --------------------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------------------
#
# Each entry: name, category_slug, brand_slug, fit_name, materials, colors, sizes, base price,
# compare-at price (None unless on sale), flags and description. ``price_multiplier`` on a colour
# nudges a price so a price sort has something to do.

PRODUCTS = [
    {
        "name": "Heavyweight Boxy Tee",
        "category": "t-shirts",
        "brand": "flashwear",
        "fit": "oversized",
        "materials": ["heavyweight-cotton"],
        "colors": ["black", "white", "sand"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "1290",
        "compare_at": "1590",
        "tags": ["core", "staff-pick"],
        "featured": True,
        "new": True,
        "short_description": (
            "240gsm cotton, boxy through the body, with a collar that keeps its shape."
        ),
        "description": (
            "The tee we could not stop improving. Knitted from 240gsm organic cotton, it holds its "
            "shape through the wash, sits square on the shoulder and drops straight at the hem.\n\n"
            "Pre-shrunk and garment-dyed, so the colour settles after the first wash instead of "
            "moving afterwards."
        ),
    },
    {
        "name": "Everyday Pocket Tee",
        "category": "t-shirts",
        "brand": "flashwear",
        "fit": "regular",
        "materials": ["organic-cotton"],
        "colors": ["white", "slate", "olive"],
        "sizes": ["S", "M", "L", "XL", "XXL"],
        "price": "890",
        "compare_at": None,
        "tags": ["core"],
        "featured": False,
        "new": True,
        "short_description": "Soft 180gsm cotton with a reinforced chest pocket.",
        "description": (
            "A lighter everyday tee: 180gsm organic cotton, straight body, and a chest "
            "pocket with a bar-tacked corner that survives being filled with a phone all day."
        ),
    },
    {
        "name": "Studio Rib Tee",
        "category": "t-shirts",
        "brand": "studio-kaar",
        "fit": "regular",
        "materials": ["organic-cotton"],
        "colors": ["black", "clay", "sand"],
        "sizes": ["XS", "S", "M", "L"],
        "price": "1090",
        "compare_at": None,
        "tags": ["limited"],
        "featured": False,
        "new": False,
        "short_description": "A 2x1 rib with real recovery, in a small-batch dye run.",
        "description": (
            "Knitted on a 2x1 rib so it springs back after a full day. Produced in small dye lots, "
            "which means the shade may shift slightly between runs."
        ),
    },
    {
        "name": "Fleece Layer Hoodie",
        "category": "hoodies-sweats",
        "brand": "flashwear",
        "fit": "relaxed",
        "materials": ["cotton-poly-fleece"],
        "colors": ["black", "olive", "navy"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "2490",
        "compare_at": "2990",
        "tags": ["core", "staff-pick"],
        "featured": True,
        "new": False,
        "short_description": "Brushed-back fleece with a lined hood and a kangaroo pocket.",
        "description": (
            "The layer for the in-between months. Brushed-back fleece, a two-panel hood lined in "
            "twill, and ribbed cuffs that stay put after a season of pulling them on and off."
        ),
    },
    {
        "name": "Loopback Crew Sweatshirt",
        "category": "hoodies-sweats",
        "brand": "flashwear",
        "fit": "relaxed",
        "materials": ["cotton-poly-fleece"],
        "colors": ["slate", "sand"],
        "sizes": ["S", "M", "L", "XL", "XXL"],
        "price": "1990",
        "compare_at": None,
        "tags": ["core"],
        "featured": False,
        "new": False,
        "short_description": "Loopback cotton, set-in sleeve, no rib at the waist.",
        "description": (
            "Loopback cotton sweatshirt with a clean set-in sleeve and a straight hem. The kind of "
            "thing you wear on the days you do not want to think about clothes."
        ),
    },
    {
        "name": "Oversized Button-Down",
        "category": "shirts",
        "brand": "flashwear",
        "fit": "oversized",
        "materials": ["cotton-twill"],
        "colors": ["white", "navy", "sand"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "2190",
        "compare_at": None,
        "tags": ["new-in"],
        "featured": False,
        "new": True,
        "short_description": "Camp collar, dropped shoulder, cut long enough to tie.",
        "description": (
            "A shirt you can wear open over a tee. Camp collar, dropped shoulder and a body long "
            "enough to knot at the waist without riding up."
        ),
    },
    {
        "name": "Utility Cargo Pant",
        "category": "bottoms",
        "brand": "northline",
        "fit": "relaxed",
        "materials": ["cotton-twill"],
        "colors": ["olive", "black", "sand"],
        "sizes": ["28", "30", "32", "34", "36"],
        "price": "2890",
        "compare_at": "3290",
        "tags": ["core"],
        "featured": True,
        "new": False,
        "short_description": "Eight pockets, articulated knee, and a taper that clears a boot.",
        "description": (
            "Built for work and borrowed for everything else. Eight pockets including two hidden "
            "cargo, an articulated knee panel, and a taper narrow enough to wear with a boot."
        ),
    },
    {
        "name": "Straight-Leg Denim",
        "category": "bottoms",
        "brand": "northline",
        "fit": "regular",
        "materials": ["cotton-twill"],
        "colors": ["navy", "black"],
        "sizes": ["28", "30", "32", "34", "36"],
        "price": "3190",
        "compare_at": None,
        "tags": ["core"],
        "featured": False,
        "new": False,
        "short_description": "13oz denim with a straight leg and a clean, unbranded back pocket.",
        "description": (
            "13oz cotton denim, straight from hip to hem, with a clean unbranded back pocket and a "
            "hidden rivet at the waistband so the front stays flat."
        ),
    },
    {
        "name": "Ribbed Tank",
        "category": "tops",
        "brand": "studio-kaar",
        "fit": "regular",
        "materials": ["organic-cotton"],
        "colors": ["white", "black", "clay"],
        "sizes": ["XS", "S", "M", "L"],
        "price": "790",
        "compare_at": None,
        "tags": ["core"],
        "featured": False,
        "new": False,
        "short_description": "A close, ribbed tank with a bound neckline.",
        "description": (
            "Ribbed cotton tank with a bound neckline that will not stretch out, cut close without "
            "being clingy."
        ),
    },
    {
        "name": "Poplin Shirt Dress",
        "category": "dresses",
        "brand": "flashwear",
        "fit": "regular",
        "materials": ["cotton-twill"],
        "colors": ["white", "navy", "clay"],
        "sizes": ["XS", "S", "M", "L", "XL"],
        "price": "3290",
        "compare_at": None,
        "tags": ["new-in"],
        "featured": True,
        "new": True,
        "short_description": "A shirt that kept going: button front, removable belt, deep pockets.",
        "description": (
            "A poplin shirt with the patience to be a dress. Button front, removable belt and "
            "pockets deep enough to be useful rather than decorative."
        ),
    },
    {
        "name": "Packable Shell Jacket",
        "category": "outerwear",
        "brand": "northline",
        "fit": "relaxed",
        "materials": ["ripstop-nylon"],
        "colors": ["black", "olive"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "5490",
        "compare_at": "6490",
        "tags": ["limited", "staff-pick"],
        "featured": False,
        "new": True,
        "short_description": "Water-resistant ripstop that packs into its own pocket.",
        "description": (
            "A shell for weather that is not quite rain yet. Water-resistant ripstop, taped seams "
            "at the shoulder, and a chest pocket sized so the whole jacket folds into it."
        ),
    },
    {
        "name": "Fleece-Lined Zip Vest",
        "category": "outerwear",
        "brand": "northline",
        "fit": "regular",
        "materials": ["recycled-polyester"],
        "colors": ["slate", "olive", "black"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "2790",
        "compare_at": None,
        "tags": ["new-in"],
        "featured": False,
        "new": False,
        "short_description": "A vest with the warmth of the hoodie and the freedom of a jacket.",
        "description": (
            "Fleece-lined front, wind-resistant back, and armholes cut so the shoulders stay put "
            "when you reach for something."
        ),
    },
]

# Colour-specific price nudges, so a price sort is not a flat block of identical numbers.
COLOR_PRICE_FACTOR = {"clay": "1.05", "navy": "1.02"}
# XXL costs slightly more to make, and is priced that way.
SIZE_PRICE_FACTOR = {"XXL": "1.10", "XXL_PLACEHOLDER": "1.00"}


class Command(BaseCommand):
    """Create (or update) a small demo catalogue."""

    help = "Seed taxonomy, products and variants for local development. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--with-images",
            action="store_true",
            help="Also generate solid-colour placeholder images with Pillow.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Only report the final summary line.",
        )

    def handle(self, *args, **options):
        quiet = options["quiet"]
        with transaction.atomic():
            sizes = self._seed_sizes(quiet)
            colors = self._seed_colors(quiet)
            self._seed_materials(quiet)
            self._seed_fits(quiet)
            self._seed_tags(quiet)
            categories = self._seed_categories(quiet)
            brands = self._seed_brands(quiet)
            collections = self._seed_collections(quiet)
            products = self._seed_products(quiet, categories, brands, collections)

            variant_total = sum(len(spec["sizes"]) * len(spec["colors"]) for spec in PRODUCTS)
            if options["with_images"]:
                self._attach_placeholder_images(quiet, products)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(categories)} categories, {len(brands)} brands, "
                f"{len(collections)} collections, {len(products)} products, "
                f"{variant_total} variants, {len(sizes)} sizes, {len(colors)} colours."
            )
        )

    # -- helpers --------------------------------------------------------------------------

    def _say(self, quiet: bool, message: str) -> None:
        if not quiet:
            self.stdout.write(message)

    def _seed_sizes(self, quiet: bool) -> dict[str, Size]:
        """Sizes, keyed by the code a shopper reads.

        ``slug`` is set from the code because the model requires one and derives none: every row
        left with the default empty string would collide on the unique index the second time round.
        """
        sizes: dict[str, Size] = {}
        for code, name, size_type, order in SIZES:
            size, _created = Size.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "slug": apps_slug(code),
                    "size_type": size_type,
                    "display_order": order,
                    "is_active": True,
                },
            )
            sizes[code] = size
        self._say(quiet, f"  sizes: {len(sizes)}")
        return sizes

    def _seed_colors(self, quiet: bool) -> dict[str, Color]:
        colors: dict[str, Color] = {}
        for name, slug, hex_code, order in COLORS:
            color, _created = Color.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "hex_code": hex_code,
                    "display_order": order,
                    "is_active": True,
                },
            )
            colors[slug] = color
        self._say(quiet, f"  colours: {len(colors)}")
        return colors

    def _seed_materials(self, quiet: bool) -> dict[str, Material]:
        """Materials, keyed by the slug the product specs refer to."""
        materials: dict[str, Material] = {}
        for name, description, order in MATERIALS:
            slug = apps_slug(name)
            material, _created = Material.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "description": description,
                    "display_order": order,
                    "is_active": True,
                },
            )
            materials[slug] = material
        self._say(quiet, f"  materials: {len(materials)}")
        return materials

    def _seed_fits(self, quiet: bool) -> dict[str, Fit]:
        """Fits, keyed by slug so product specs can refer to them like every other reference."""
        fits: dict[str, Fit] = {}
        for name, description, order in FITS:
            slug = apps_slug(name)
            fit, _created = Fit.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "description": description,
                    "display_order": order,
                    "is_active": True,
                },
            )
            fits[slug] = fit
        self._say(quiet, f"  fits: {len(fits)}")
        return fits

    def _seed_tags(self, quiet: bool) -> dict[str, ProductTag]:
        tags: dict[str, ProductTag] = {}
        for index, name in enumerate(TAGS, start=1):
            tag, _created = ProductTag.objects.update_or_create(
                slug=name,
                defaults={"name": name, "display_order": index * 10, "is_active": True},
            )
            tags[name] = tag
        self._say(quiet, f"  tags: {len(tags)}")
        return tags

    def _seed_categories(self, quiet: bool) -> dict[str, Category]:
        categories: dict[str, Category] = {}
        for name, slug, parent_slug, description, order in CATEGORIES:
            category, _created = Category.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "parent": categories.get(parent_slug) if parent_slug else None,
                    "description": description,
                    "display_order": order,
                    "is_active": True,
                },
            )
            categories[slug] = category
        self._say(quiet, f"  categories: {len(categories)}")
        return categories

    def _seed_brands(self, quiet: bool) -> dict[str, Brand]:
        brands: dict[str, Brand] = {}
        for name, slug, description, order in BRANDS:
            brand, _created = Brand.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "description": description,
                    "display_order": order,
                    "is_active": True,
                },
            )
            brands[slug] = brand
        self._say(quiet, f"  brands: {len(brands)}")
        return brands

    def _seed_collections(self, quiet: bool) -> dict[str, Collection]:
        now = timezone.now()
        collections: dict[str, Collection] = {}
        for name, slug, description, featured, starts_in, ends_in in COLLECTIONS:
            collection, _created = Collection.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "description": description,
                    "is_featured": featured,
                    "starts_at": now + timezone.timedelta(days=starts_in) if starts_in else None,
                    "ends_at": now + timezone.timedelta(days=ends_in) if ends_in else None,
                    "is_active": True,
                },
            )
            collections[slug] = collection
        self._say(quiet, f"  collections: {len(collections)}")
        return collections

    def _seed_products(
        self,
        quiet: bool,
        categories: dict[str, Category],
        brands: dict[str, Brand],
        collections: dict[str, Collection],
    ) -> list[Product]:
        """Create each product, then its variants, then publish.

        Products are published through :func:`apps.catalog.services.publish`, the same path an admin
        would use -- so a seeded catalogue exercises the publishing invariants rather than bypassing
        them with a direct ``status`` write.
        """
        materials = {material.slug: material for material in Material.objects.all()}
        fits = {fit.slug: fit for fit in Fit.objects.all()}
        tags = {tag.name: tag for tag in ProductTag.objects.all()}
        now = timezone.now()

        products: list[Product] = []
        for index, spec in enumerate(PRODUCTS):
            product, _created = Product.objects.update_or_create(
                slug=apps_slug(spec["name"]),
                defaults={
                    "name": spec["name"],
                    "short_description": spec["short_description"],
                    "description": spec["description"],
                    "category": categories[spec["category"]],
                    "brand": brands[spec["brand"]],
                    "fit": fits[spec["fit"]],
                    "is_featured": spec["featured"],
                    "is_new": spec["new"],
                    # A different offset per product keeps the "newest first" ordering stable and
                    # meaningful across runs, instead of one shared timestamp broken by id.
                    "published_at": now - timezone.timedelta(days=len(PRODUCTS) - index),
                },
            )
            product.materials.set(
                [materials[slug] for slug in spec["materials"] if slug in materials]
            )
            product.tags.set([tags[name] for name in spec["tags"] if name in tags])
            product.collections.set(
                [
                    collection
                    for slug, collection in collections.items()
                    if self._in_collection(spec, slug)
                ]
            )

            for color_slug in spec["colors"]:
                color = Color.objects.get(slug=color_slug)
                for size_code in spec["sizes"]:
                    self._create_variant(
                        product,
                        spec,
                        color,
                        Size.objects.get(code=size_code),
                    )

            services.publish(product)
            products.append(product)

        self._say(quiet, f"  products: {len(products)} (published)")
        return products

    @staticmethod
    def _in_collection(spec: dict, collection_slug: str) -> bool:
        """Which products belong to which story.

        Explicit membership where it matters, plus one deliberate catch-all so "Core Essentials"
        has a shelf: anything tagged ``core``.
        """
        if collection_slug == "core-essentials":
            return "core" in spec["tags"]
        if collection_slug == "midnight-run":
            return spec["brand"] == "northline" or "black" in spec["colors"]
        if collection_slug == "sand-season":
            return "sand" in spec["colors"]
        return False

    @staticmethod
    def _create_variant(product: Product, spec: dict, color: Color, size: Size) -> None:
        """Create one variant with its deterministic price.

        Uses ``update_or_create`` on ``(product, color, size)``, which is exactly the identity the
        database's partial unique indexes enforce, so re-running cannot collide.
        """
        base = Decimal(spec["price"])
        price = (
            base
            * Decimal(COLOR_PRICE_FACTOR.get(color.slug, "1"))
            * Decimal(SIZE_PRICE_FACTOR.get(size.code, "1"))
        ).quantize(Decimal("0.01"))

        compare_at = None
        if spec["compare_at"]:
            compare_at = (
                Decimal(spec["compare_at"])
                * Decimal(COLOR_PRICE_FACTOR.get(color.slug, "1"))
                * Decimal(SIZE_PRICE_FACTOR.get(size.code, "1"))
            ).quantize(Decimal("0.01"))

        sku = build_sku(product=product, color=color, size=size)
        ProductVariant.objects.update_or_create(
            product=product,
            color=color,
            size=size,
            defaults={
                "sku": sku,
                "price": price,
                "compare_at_price": compare_at,
                "is_active": True,
            },
        )

    def _attach_placeholder_images(self, quiet: bool, products: list[Product]) -> None:
        """Generate one flat placeholder image per product.

        Pillow writes a solid rectangle in the product's first colour: enough to prove the gallery,
        the lazy ``<img>`` markup and the Open Graph tags work end to end, without committing
        binaries to the repository.
        """
        from django.core.files.base import ContentFile

        from apps.catalog.models import ProductImage

        created = 0
        for product in products:
            if product.images.exists():
                continue
            color = Color.objects.filter(
                pk__in=product.variants.values_list("color_id", flat=True)
            ).first()
            buffer = _solid_png(hex_code=color.hex_code if color else "#e2e8f0")
            ProductImage.objects.create(
                product=product,
                image=ContentFile(buffer, name=f"{product.slug}.png"),
                alt_text=f"{product.name} in {color.name if color else 'studio'}",
                position=0,
                is_primary=True,
            )
            created += 1
        self._say(quiet, f"  images: {created} placeholder(s)")


def apps_slug(value: str) -> str:
    """Slug for seed data, delegating to the app's own slug policy so both agree."""
    from apps.catalog.slugs import slug_candidate

    return slug_candidate(value)


def _solid_png(hex_code: str, width: int = 1200, height: int = 1500) -> bytes:
    """A solid-colour PNG, used as a development placeholder only."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), hex_code)
    draw = ImageDraw.Draw(image)
    # A faint diagonal so a placeholder is visibly a placeholder in a gallery.
    for offset in range(-height, width, 40):
        draw.line([(offset, height), (offset + height, 0)], fill="#ffffff", width=1)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
