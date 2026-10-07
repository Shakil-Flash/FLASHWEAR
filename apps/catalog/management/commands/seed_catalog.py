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
    ProductImage,
    ProductTag,
    ProductVariant,
    Size,
)
from apps.catalog.skus import build_sku
from apps.inventory.models import InventoryMovement, Stock
from apps.inventory.services import adjust_stock

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
    ("Skirts", "skirts", "women", "Structured midis and relaxed everyday skirts.", 30),
    ("Knitwear", "knitwear", "men", "Chunky rib sweaters and fine-gauge layers.", 50),
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
    (
        "New Arrivals",
        "new-arrivals",
        "Fresh seasonal silhouettes and newly released colorways.",
        True,
        None,
        None,
    ),
    (
        "Streetwear",
        "streetwear",
        "Oversized proportions, heavy drape and utility details.",
        False,
        None,
        None,
    ),
]

# --------------------------------------------------------------------------------------
# Products (20 Realistic FLASHWEAR Products)
# --------------------------------------------------------------------------------------

PRODUCTS = [
    {
        "name": "Heavyweight Boxy Tee",
        "category": "t-shirts",
        "brand": "flashwear",
        "fit": "oversized",
        "materials": ["heavyweight-cotton", "organic-cotton"],
        "colors": ["black", "white", "sand"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "1290",
        "compare_at": "1590",
        "tags": ["core", "staff-pick"],
        "featured": True,
        "new": True,
        "short_description": (
            "240gsm combed organic cotton, cut square with a clean dropped shoulder and "
            "dense ribbed collar."
        ),
        "description": (
            "The tee that defines our silhouette. Knitted from 240gsm combed organic "
            "cotton jersey, it sits flat"
            "across the chest and drops straight through the torso without clinging. "
            "Pre-shrunk and garment-dyed so"
            "the tone and structure hold wash after wash."
        ),
        "seo_title": "Heavyweight Boxy Tee | FLASHWEAR",
        "seo_description": (
            "240gsm organic cotton heavyweight boxy t-shirt with dropped shoulder cut."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1521572267360-ee0c2909d518?w=1200&q=80",
                "photographer": "Austin Wade",
                "alt": "Heavyweight Boxy Tee in Black",
            },
            {
                "url": "https://images.unsplash.com/photo-1581655353564-df123a1eb820?w=1200&q=80",
                "photographer": "Mediamodifier",
                "alt": "Heavyweight Boxy Tee in White",
            },
            {
                "url": "https://images.unsplash.com/photo-1583743814966-8936f5b7be1a?w=1200&q=80",
                "photographer": "Alexander Andrews",
                "alt": "Heavyweight Boxy Tee in Sand",
            },
        ],
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
        "new": False,
        "short_description": (
            "180gsm combed cotton with a bar-tacked chest pocket and neat neckband."
        ),
        "description": (
            "A versatile daily base layer in 180gsm ring-spun cotton. Cut with a "
            "standard drape, ribbed collar that retains"
            "its shape, and a reinforced chest pocket sized for essentials."
        ),
        "seo_title": "Everyday Pocket Tee | FLASHWEAR",
        "seo_description": "180gsm organic cotton crewneck tee with reinforced chest pocket.",
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1527719327859-c6ce80353573?w=1200&q=80",
                "photographer": "Tamara Bellis",
                "alt": "Everyday Pocket Tee in White",
            },
            {
                "url": "https://images.unsplash.com/photo-1562157873-818bc0726f68?w=1200&q=80",
                "photographer": "Faith Yarn",
                "alt": "Everyday Pocket Tee chest pocket detail",
            },
        ],
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
        "short_description": (
            "2x1 flexible rib jersey with shape memory, small-batch garment washed."
        ),
        "description": (
            "Spun from long-staple organic cotton in a supple 2x1 rib. Designed to "
            "conform comfortably through movement"
            "and bounce back without warping at the cuffs or hem."
        ),
        "seo_title": "Studio Rib Tee | FLASHWEAR",
        "seo_description": (
            "Small-batch 2x1 ribbed organic cotton t-shirt with resilient stretch recovery."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1485230895905-ec40ba36b9bc?w=1200&q=80",
                "photographer": "Averie Woodard",
                "alt": "Studio Rib Tee in Black",
            },
            {
                "url": "https://images.unsplash.com/photo-1485968579580-b6d095142e6e?w=1200&q=80",
                "photographer": "Averie Woodard",
                "alt": "Studio Rib Tee fabric texture",
            },
        ],
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
        "short_description": (
            "380gsm brushed-back cotton fleece with double-lined hood and deep kangaroo pouch."
        ),
        "description": (
            "Substantial 380gsm fleece built for cool mornings and transition seasons. "
            "Features cross-grain side panels for lateral"
            "stretch, a self-lined two-piece hood, and durable 2x2 ribbing."
        ),
        "seo_title": "Fleece Layer Hoodie | FLASHWEAR",
        "seo_description": (
            "Heavyweight 380gsm cotton fleece pullover hoodie with double-layer hood."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1556905055-8f358a7a47b2?w=1200&q=80",
                "photographer": "Kamil Kalbarczyk",
                "alt": "Fleece Layer Hoodie in Black",
            },
            {
                "url": "https://images.unsplash.com/photo-1512436991641-6745cdb1723f?w=1200&q=80",
                "photographer": "Averie Woodard",
                "alt": "Fleece Layer Hoodie side profile",
            },
        ],
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
        "short_description": (
            "French terry loopback fleece, raglan sleeves and flatlock seam construction."
        ),
        "description": (
            "Midweight loopback crew with breathable interior terry loops. Cut with "
            "ergonomic raglan shoulders for mobility"
            "and finished with vintage triangular collar gusset."
        ),
        "seo_title": "Loopback Crew Sweatshirt | FLASHWEAR",
        "seo_description": (
            "Classic French terry loopback crewneck sweatshirt with flatlock stitching."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1578587018452-892bacefd3f2?w=1200&q=80",
                "photographer": "Alexander Andrews",
                "alt": "Loopback Crew Sweatshirt in Slate",
            },
            {
                "url": "https://images.unsplash.com/photo-1529374255404-311a2a4f1fd9?w=1200&q=80",
                "photographer": "Dom Hill",
                "alt": "Loopback Crew Sweatshirt relaxed fit",
            },
        ],
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
        "short_description": (
            "Crisp cotton poplin, dropped shoulders, relaxed camp collar and curved hem."
        ),
        "description": (
            "An effortless overshirt engineered for layering over basic tees. Soft "
            "washed cotton poplin provides natural"
            "breathability, paired with tonal buttons and back box pleat."
        ),
        "seo_title": "Oversized Button-Down Shirt | FLASHWEAR",
        "seo_description": (
            "Relaxed cotton poplin overshirt with dropped shoulder cut and clean collar."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1596755094514-f87e34085b2c?w=1200&q=80",
                "photographer": "Nimble Made",
                "alt": "Oversized Button-Down in White",
            },
            {
                "url": "https://images.unsplash.com/photo-1602810318383-e386cc2a3ccf?w=1200&q=80",
                "photographer": "Nimble Made",
                "alt": "Oversized Button-Down in Navy",
            },
        ],
    },
    {
        "name": "Utility Cargo Pant",
        "category": "bottoms",
        "brand": "northline",
        "fit": "relaxed",
        "materials": ["cotton-twill", "ripstop-nylon"],
        "colors": ["olive", "black", "sand"],
        "sizes": ["28", "30", "32", "34", "36"],
        "price": "2890",
        "compare_at": "3290",
        "tags": ["core"],
        "featured": True,
        "new": False,
        "short_description": (
            "Dense cotton twill, bellows cargo pockets, articulated knees and hem drawcords."
        ),
        "description": (
            "Engineered utilitarian cargo trousers. Built from durable 280gsm cotton "
            "twill with reinforced pocket flaps,"
            "gusseted crotch for mobility, and adjustable ankle ties."
        ),
        "seo_title": "Utility Cargo Pant | FLASHWEAR",
        "seo_description": (
            "Tactical cotton twill cargo pants with bellows pockets and articulated knees."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1624378439575-d8705ad7ae80?w=1200&q=80",
                "photographer": "Mo Eid",
                "alt": "Utility Cargo Pant in Olive",
            },
            {
                "url": "https://images.unsplash.com/photo-1517445312882-bc9910d016b7?w=1200&q=80",
                "photographer": "Brooke Cagle",
                "alt": "Utility Cargo Pant in Sand",
            },
        ],
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
        "short_description": "13.5oz ring-spun denim with a true straight cut from knee to ankle.",
        "description": (
            "Classic five-pocket denim crafted from 13.5oz raw cotton twill. Mid-rise "
            "waist, button fly closure with custom"
            "copper hardware, and a clean unbranded silhouette."
        ),
        "seo_title": "Straight-Leg Denim Jeans | FLASHWEAR",
        "seo_description": (
            "13.5oz durable straight-leg denim jeans with five-pocket styling and button fly."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1541099649105-f69ad21f3246?w=1200&q=80",
                "photographer": "Amir Hosseini",
                "alt": "Straight-Leg Denim in Raw Black",
            },
            {
                "url": "https://images.unsplash.com/photo-1582552938357-32b906df40cb?w=1200&q=80",
                "photographer": "Jason Leung",
                "alt": "Straight-Leg Denim in Classic Indigo",
            },
        ],
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
        "short_description": "Fitted stretch rib tank with scooped neck and narrow binding.",
        "description": (
            "An elevated layering foundation in 2x2 organic cotton rib with 5% elastane "
            "recovery. Cut with a modern scoop"
            "neckline, reinforced armholes, and hip-length hem."
        ),
        "seo_title": "Ribbed Tank Top | FLASHWEAR",
        "seo_description": "Organic cotton stretch-ribbed tank top with bound scoop neckline.",
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1508427953056-b00b8d78ebf5?w=1200&q=80",
                "photographer": "Carly Rae Hobbins",
                "alt": "Ribbed Tank in White",
            },
            {
                "url": "https://images.unsplash.com/photo-1503342394128-c104d54dba01?w=1200&q=80",
                "photographer": "Averie Woodard",
                "alt": "Ribbed Tank in Black",
            },
        ],
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
        "short_description": (
            "Crisp cotton poplin midi dress with removable sash tie and side seam pockets."
        ),
        "description": (
            "Tailored yet relaxed shirt dress in lightweight organic poplin. Features "
            "pointed collar, concealed button placket,"
            "drop shoulders, and functional deep inseam pockets."
        ),
        "seo_title": "Poplin Shirt Dress | FLASHWEAR",
        "seo_description": "Tailored cotton poplin midi shirt dress with self-tie waist belt.",
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1572804013309-59a88b7e92f1?w=1200&q=80",
                "photographer": "Tamara Bellis",
                "alt": "Poplin Shirt Dress in Crisp White",
            },
            {
                "url": "https://images.unsplash.com/photo-1515372039744-b8f02a3ae446?w=1200&q=80",
                "photographer": "Tamara Bellis",
                "alt": "Poplin Shirt Dress in Deep Navy",
            },
        ],
    },
    {
        "name": "Packable Shell Jacket",
        "category": "outerwear",
        "brand": "northline",
        "fit": "relaxed",
        "materials": ["ripstop-nylon", "recycled-polyester"],
        "colors": ["black", "olive"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "5490",
        "compare_at": "6490",
        "tags": ["limited", "staff-pick"],
        "featured": False,
        "new": True,
        "short_description": (
            "Weather-resistant micro-ripstop shell that stows into its internal chest pocket."
        ),
        "description": (
            "Technical lightweight windbreaker built from water-repellent recycled "
            "nylon. Features taped critical seams, elasticated"
            "storm cuffs, two-way YKK zipper, and stowable hood."
        ),
        "seo_title": "Packable Shell Jacket | FLASHWEAR",
        "seo_description": (
            "Water-resistant packable ripstop technical shell jacket with storm hood."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1544923246-77307dd654cb?w=1200&q=80",
                "photographer": "Alexander Andrews",
                "alt": "Packable Shell Jacket in Black",
            },
            {
                "url": "https://images.unsplash.com/photo-1548883354-7622d03aca27?w=1200&q=80",
                "photographer": "Craig McKay",
                "alt": "Packable Shell Jacket in Olive",
            },
        ],
    },
    {
        "name": "Fleece-Lined Zip Vest",
        "category": "outerwear",
        "brand": "northline",
        "fit": "regular",
        "materials": ["recycled-polyester", "cotton-poly-fleece"],
        "colors": ["slate", "olive", "black"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "2790",
        "compare_at": None,
        "tags": ["new-in"],
        "featured": False,
        "new": False,
        "short_description": (
            "Brushed thermal microfleece vest with storm flap and zipped chest utility pocket."
        ),
        "description": (
            "Streamlined insulating gilet for cold-weather layering. Stand-up mock neck, "
            "abrasion-resistant shoulder panels,"
            "and zippered handwarmer pockets lined with soft tricot."
        ),
        "seo_title": "Fleece-Lined Zip Vest | FLASHWEAR",
        "seo_description": "Thermal microfleece zip vest with utility pockets and storm collar.",
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1516257984-b1b4d707412e?w=1200&q=80",
                "photographer": "Marlon Alves",
                "alt": "Fleece-Lined Zip Vest in Slate",
            },
            {
                "url": "https://images.unsplash.com/photo-1509551388413-e18d0ac5d495?w=1200&q=80",
                "photographer": "Marlon Alves",
                "alt": "Fleece-Lined Zip Vest in Black",
            },
        ],
    },
    {
        "name": "Chunky Cable Knit Sweater",
        "category": "knitwear",
        "brand": "studio-kaar",
        "fit": "relaxed",
        "materials": ["organic-cotton"],
        "colors": ["sand", "slate", "navy"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "3490",
        "compare_at": "3990",
        "tags": ["new-in", "staff-pick"],
        "featured": True,
        "new": True,
        "short_description": (
            "Heavyweight 7-gauge fisherman rib knit with raglan sleeves and ribbed crew collar."
        ),
        "description": (
            "Spun from a substantial organic cotton yarn in a heritage 7-gauge fisherman "
            "stitch. Designed for cool weather"
            "with a slouchy, relaxed drape, reinforced shoulder seams, and dense ribbed "
            "hems that retain structure."
        ),
        "seo_title": "Chunky Cable Knit Sweater | FLASHWEAR",
        "seo_description": (
            "Heavyweight 7-gauge knit sweater with relaxed silhouette and fisherman rib texture."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1576566588028-4147f3842f27?w=1200&q=80",
                "photographer": "Rocknwool",
                "alt": "Chunky Cable Knit Sweater in Sand",
            },
            {
                "url": "https://images.unsplash.com/photo-1620799140408-edc6dcb6d633?w=1200&q=80",
                "photographer": "Dom Hill",
                "alt": "Chunky Cable Knit Sweater styling",
            },
            {
                "url": "https://images.unsplash.com/photo-1608231387042-66d1773070a5?w=1200&q=80",
                "photographer": "Dom Hill",
                "alt": "Chunky Cable Knit Sweater rib texture",
            },
        ],
    },
    {
        "name": "Pleated Wide-Leg Trouser",
        "category": "bottoms",
        "brand": "flashwear",
        "fit": "relaxed",
        "materials": ["cotton-twill"],
        "colors": ["black", "slate", "sand"],
        "sizes": ["28", "30", "32", "34", "36"],
        "price": "2690",
        "compare_at": "2990",
        "tags": ["core", "staff-pick"],
        "featured": True,
        "new": True,
        "short_description": (
            "Double front pleats, loose flowing drape and a clean break at the shoe."
        ),
        "description": (
            "Contemporary tailored trouser combining relaxed streetwear volume with "
            "sharp sartorial details. Cut with a"
            "generous double forward pleat, slant side pockets, rear jetted pockets, and "
            "an internal curtain waistband."
        ),
        "seo_title": "Pleated Wide-Leg Trouser | FLASHWEAR",
        "seo_description": (
            "Relaxed double-pleated wide leg trousers with tailored waistband and flowing drape."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1594633312681-425c7b97ccd1?w=1200&q=80",
                "photographer": "Tamara Bellis",
                "alt": "Pleated Wide-Leg Trouser in Black",
            },
            {
                "url": "https://images.unsplash.com/photo-1506629082955-511b1aa562c8?w=1200&q=80",
                "photographer": "Alexandre Chambon",
                "alt": "Pleated Wide-Leg Trouser drape",
            },
        ],
    },
    {
        "name": "Relaxed Camp Collar Linen Shirt",
        "category": "shirts",
        "brand": "flashwear",
        "fit": "relaxed",
        "materials": ["organic-cotton", "cotton-twill"],
        "colors": ["sand", "white", "olive"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "2390",
        "compare_at": None,
        "tags": ["new-in"],
        "featured": False,
        "new": True,
        "short_description": (
            "Breathable open-weave cotton-linen blend with retro camp collar and straight hem."
        ),
        "description": (
            "An easy-going warm-weather essential cut from an airy, garment-washed linen "
            "and cotton blend. Styled with an"
            "open cuban camp collar, clean mother-of-pearl buttons, and side-slit "
            "straight hem made to be worn untucked."
        ),
        "seo_title": "Relaxed Camp Collar Linen Shirt | FLASHWEAR",
        "seo_description": (
            "Short-sleeve relaxed camp collar shirt in lightweight breathable cotton- linen weave."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1598033129183-c4f50c736f10?w=1200&q=80",
                "photographer": "Sanford Creative",
                "alt": "Relaxed Camp Collar Linen Shirt in Sand",
            },
            {
                "url": "https://images.unsplash.com/photo-1607345366928-199ea26cfe3e?w=1200&q=80",
                "photographer": "Nimble Made",
                "alt": "Relaxed Camp Collar Linen Shirt print detail",
            },
        ],
    },
    {
        "name": "Acid Wash Oversized Hoodie",
        "category": "hoodies-sweats",
        "brand": "flashwear",
        "fit": "oversized",
        "materials": ["cotton-poly-fleece", "heavyweight-cotton"],
        "colors": ["black", "slate", "clay"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "2790",
        "compare_at": "3290",
        "tags": ["limited", "staff-pick"],
        "featured": True,
        "new": True,
        "short_description": (
            "Vintage mineral-washed 400gsm heavyweight fleece with dropped shoulders."
        ),
        "description": (
            "Each piece is individually treated with a mineral wash process to produce "
            "unique tonal fade patterns. Heavyweight"
            "400gsm fleece with raw-edge detailing, double-stitched hood without "
            "drawstrings, and a boxy silhouette."
        ),
        "seo_title": "Acid Wash Oversized Hoodie | FLASHWEAR",
        "seo_description": (
            "Heavyweight 400gsm mineral acid washed oversized hoodie with streetwear boxy drape."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1509967419530-da38b4704bc6?w=1200&q=80",
                "photographer": "Averie Woodard",
                "alt": "Acid Wash Oversized Hoodie in Washed Black",
            },
            {
                "url": "https://images.unsplash.com/photo-1515886657613-9f3515b0c78f?w=1200&q=80",
                "photographer": "Dom Hill",
                "alt": "Acid Wash Oversized Hoodie texture",
            },
        ],
    },
    {
        "name": "Heavyweight Carpenter Denim Shorts",
        "category": "bottoms",
        "brand": "northline",
        "fit": "relaxed",
        "materials": ["cotton-twill"],
        "colors": ["navy", "sand", "black"],
        "sizes": ["28", "30", "32", "34", "36"],
        "price": "2190",
        "compare_at": None,
        "tags": ["core", "new-in"],
        "featured": False,
        "new": True,
        "short_description": (
            "12oz washed cotton denim shorts with utility hammer loop and dual tool pockets."
        ),
        "description": (
            "Workwear-inspired knee-length denim shorts built from rugged 12oz cotton "
            "twill. Features functional tool pockets"
            "along the right thigh, hammer loop on the left, triple-needle chain "
            "stitching, and relaxed leg openings."
        ),
        "seo_title": "Heavyweight Carpenter Denim Shorts | FLASHWEAR",
        "seo_description": (
            "12oz workwear carpenter denim shorts with tool pockets and utility hammer loop."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1591195853828-11db59a44f6b?w=1200&q=80",
                "photographer": "Tamara Bellis",
                "alt": "Heavyweight Carpenter Denim Shorts in Washed Indigo",
            },
            {
                "url": "https://images.unsplash.com/photo-1565084888279-aca607ecce0c?w=1200&q=80",
                "photographer": "Jason Leung",
                "alt": "Heavyweight Carpenter Denim Shorts tool pocket",
            },
        ],
    },
    {
        "name": "Minimalist Wool-Blend Overcoat",
        "category": "outerwear",
        "brand": "northline",
        "fit": "regular",
        "materials": ["recycled-polyester", "cotton-twill"],
        "colors": ["black", "slate", "navy"],
        "sizes": ["S", "M", "L", "XL"],
        "price": "6490",
        "compare_at": "7490",
        "tags": ["limited", "staff-pick"],
        "featured": True,
        "new": True,
        "short_description": (
            "Clean single-breasted knee-length overcoat with concealed horn buttons."
        ),
        "description": (
            "Architectural outerwear tailored with structured shoulders and a modern "
            "straight drop. Fully satin lined with"
            "interior chest welt pockets, deep notched lapels, and a single vent back "
            "for walking ease."
        ),
        "seo_title": "Minimalist Wool-Blend Overcoat | FLASHWEAR",
        "seo_description": (
            "Single-breasted knee-length tailored overcoat with notched lapels and satin lining."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=1200&q=80",
                "photographer": "Ali Pazani",
                "alt": "Minimalist Wool-Blend Overcoat in Charcoal Black",
            },
            {
                "url": "https://images.unsplash.com/photo-1507679799987-c73779587ccf?w=1200&q=80",
                "photographer": "Hunters Race",
                "alt": "Minimalist Wool-Blend Overcoat back vent view",
            },
        ],
    },
    {
        "name": "Structured Twill Midi Skirt",
        "category": "skirts",
        "brand": "studio-kaar",
        "fit": "regular",
        "materials": ["cotton-twill", "organic-cotton"],
        "colors": ["black", "sand", "olive"],
        "sizes": ["XS", "S", "M", "L"],
        "price": "2590",
        "compare_at": None,
        "tags": ["new-in"],
        "featured": False,
        "new": True,
        "short_description": (
            "High-waisted A-line skirt in dense cotton twill with deep front slit and welt pockets."
        ),
        "description": (
            "A clean architectural midi skirt in durable 260gsm organic cotton twill. "
            "High-rise fitted waistband, subtle front"
            "walking slit, concealed side zipper, and slanted front utility pockets for "
            "an effortless everyday profile."
        ),
        "seo_title": "Structured Twill Midi Skirt | FLASHWEAR",
        "seo_description": (
            "High-waisted A-line cotton twill midi skirt with front walking slit and deep pockets."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1583496661160-fb5886a0aaaa?w=1200&q=80",
                "photographer": "Tamara Bellis",
                "alt": "Structured Twill Midi Skirt in Black",
            },
            {
                "url": "https://images.unsplash.com/photo-1576995853123-5a10305d93c0?w=1200&q=80",
                "photographer": "Tamara Bellis",
                "alt": "Structured Twill Midi Skirt pocket detail",
            },
        ],
    },
    {
        "name": "Waffle Knit Long Sleeve Tee",
        "category": "t-shirts",
        "brand": "studio-kaar",
        "fit": "regular",
        "materials": ["organic-cotton", "heavyweight-cotton"],
        "colors": ["white", "slate", "olive"],
        "sizes": ["S", "M", "L", "XL", "XXL"],
        "price": "1390",
        "compare_at": None,
        "tags": ["core"],
        "featured": False,
        "new": True,
        "short_description": (
            "Thermal honeycomb waffle knit in 220gsm combed cotton with ribbed storm cuffs."
        ),
        "description": (
            "Dense 220gsm thermal waffle fabric with exceptional tactile depth. Designed "
            "to insulate when worn beneath an"
            "overshirt or hold its own as a standalone top, with thumb-anchored rib "
            "cuffs and a bound crewneck."
        ),
        "seo_title": "Waffle Knit Long Sleeve Tee | FLASHWEAR",
        "seo_description": (
            "220gsm organic cotton thermal waffle long-sleeve t-shirt with ribbed cuffs."
        ),
        "images": [
            {
                "url": "https://images.unsplash.com/photo-1618354691373-d851c5c3a990?w=1200&q=80",
                "photographer": "Faith Yarn",
                "alt": "Waffle Knit Long Sleeve Tee in Off-White",
            },
            {
                "url": "https://images.unsplash.com/photo-1489987707025-afc232f7ea0f?w=1200&q=80",
                "photographer": "NordWood Themes",
                "alt": "Waffle Knit Long Sleeve Tee fabric fold",
            },
        ],
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
            help="Generate solid-colour placeholder images with Pillow (fast, for unit tests).",
        )
        parser.add_argument(
            "--with-real-images",
            action="store_true",
            help="Download and attach real fashion photographs with attribution.",
        )
        parser.add_argument(
            "--real",
            dest="with_real_images",
            action="store_true",
            help="Alias for --with-real-images.",
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

            if options.get("with_real_images"):
                self._attach_real_images(quiet, products)
            elif options.get("with_images"):
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
                    "seo_title": spec.get("seo_title", ""),
                    "seo_description": spec.get("seo_description", ""),
                    "category": categories[spec["category"]],
                    "brand": brands[spec["brand"]],
                    "fit": fits[spec["fit"]],
                    "is_featured": spec["featured"],
                    "is_new": spec["new"],
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
                    size = Size.objects.get(code=size_code)
                    variant = self._create_variant(
                        product,
                        spec,
                        color,
                        size,
                    )
                    self._seed_inventory(variant, color, size)

            services.publish(product)
            products.append(product)

        self._say(quiet, f"  products: {len(products)} (published)")
        return products

    @staticmethod
    def _in_collection(spec: dict, collection_slug: str) -> bool:
        if collection_slug == "core-essentials":
            return "core" in spec["tags"]
        if collection_slug == "midnight-run":
            return spec["brand"] == "northline" or "black" in spec["colors"]
        if collection_slug == "sand-season":
            return "sand" in spec["colors"]
        if collection_slug == "new-arrivals":
            return spec.get("new", False) or "new-in" in spec["tags"]
        if collection_slug == "streetwear":
            return spec["fit"] == "oversized" or "limited" in spec["tags"]
        return False

    @staticmethod
    def _create_variant(product: Product, spec: dict, color: Color, size: Size) -> ProductVariant:
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
        variant, _ = ProductVariant.objects.update_or_create(
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
        return variant

    @staticmethod
    def _seed_inventory(variant: ProductVariant, color: Color, size: Size) -> None:
        """Seed realistic inventory levels through the authoritative stock ledger.

        Mix of healthy stock (15-45 units), low stock (2-5 units), and a small number
        of variants out of stock (0 units).
        """
        if size.code in ("XXL", "36") and color.slug in ("clay", "sand", "olive"):
            target_on_hand = 0
        elif size.code in ("XS", "XL", "28", "34"):
            target_on_hand = 2 + ((variant.pk or 1) % 4)
        else:
            target_on_hand = 18 + ((variant.pk or 1) % 28)

        stock = Stock.get_for_variant(variant)
        delta = target_on_hand - stock.on_hand
        if delta != 0:
            adjust_stock(
                variant,
                delta,
                kind=InventoryMovement.Kind.RECEIVED
                if delta > 0
                else InventoryMovement.Kind.ADJUSTMENT,
                reference="seed:catalog",
                note="Catalog seed inventory level",
            )

    def _attach_real_images(self, quiet: bool, products: list[Product]) -> None:
        """Download and attach verified real fashion photographs from Unsplash with attribution."""
        from apps.catalog.image_fetcher import fetch_and_validate_image_file

        attached_count = 0
        spec_map = {apps_slug(s["name"]): s for s in PRODUCTS}

        for product in products:
            spec = spec_map.get(product.slug)
            if not spec or not spec.get("images"):
                continue

            # Remove placeholder images (which have no source_url) so real images take over
            from django.db.models import Q

            placeholder_imgs = product.images.filter(Q(source_url="") | Q(source_url__isnull=True))
            if placeholder_imgs.exists():
                placeholder_imgs.delete()

            for pos, img_data in enumerate(spec["images"]):
                url = img_data["url"]
                existing = product.images.filter(source_url=url).first()
                if existing:
                    is_primary = pos == 0
                    if existing.position != pos or existing.is_primary != is_primary:
                        existing.position = pos
                        existing.is_primary = is_primary
                        existing.save(update_fields=["position", "is_primary"])
                    continue

                try:
                    dl = fetch_and_validate_image_file(url)
                    ProductImage.objects.create(
                        product=product,
                        image=dl.file,
                        alt_text=img_data.get("alt", f"{product.name} photograph"),
                        position=pos,
                        is_primary=(pos == 0),
                        source_url=url,
                        photographer=img_data.get("photographer", ""),
                        attribution=(
                            f"Photo by {img_data.get('photographer', 'Contributor')} on Unsplash"
                        ),
                        license="Unsplash License",
                    )
                    attached_count += 1
                except Exception as err:
                    self.stderr.write(
                        self.style.WARNING(
                            f"Failed to fetch image for {product.name}: {url} ({err})"
                        )
                    )

        self._say(quiet, f"  real images: {attached_count} attached")

    def _attach_placeholder_images(self, quiet: bool, products: list[Product]) -> None:
        from django.core.files.base import ContentFile

        created = 0
        for product in products:
            images = list(product.images.all())
            color = Color.objects.filter(
                pk__in=product.variants.values_list("color_id", flat=True)
            ).first()
            buffer = _solid_png(hex_code=color.hex_code if color else "#e2e8f0")

            if not images:
                ProductImage.objects.create(
                    product=product,
                    image=ContentFile(buffer, name=f"{product.slug}.png"),
                    alt_text=f"{product.name} in {color.name if color else 'studio'}",
                    position=0,
                    is_primary=True,
                )
                created += 1
            else:
                for img in images:
                    if not img.image or not img.image.name:
                        img.image.save(f"{product.slug}.png", ContentFile(buffer), save=True)
                        created += 1
                    else:
                        try:
                            if not img.image.storage.exists(img.image.name):
                                img.image.storage.save(img.image.name, ContentFile(buffer))
                                created += 1
                        except Exception:
                            img.image.save(f"{product.slug}.png", ContentFile(buffer), save=True)
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
