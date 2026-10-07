import pytest
from django.test import Client

from apps.catalog.models import Category, Collection, Product, ProductImage, ProductVariant
from apps.inventory.models import Stock

SEED_SLUGS = [
    "heavyweight-boxy-tee",
    "everyday-pocket-tee",
    "studio-rib-tee",
    "fleece-layer-hoodie",
    "loopback-crew-sweatshirt",
    "oversized-button-down",
    "utility-cargo-pant",
    "straight-leg-denim",
    "ribbed-tank",
    "poplin-shirt-dress",
    "packable-shell-jacket",
    "fleece-lined-zip-vest",
    "chunky-cable-knit-sweater",
    "pleated-wide-leg-trouser",
    "relaxed-camp-collar-linen-shirt",
    "acid-wash-oversized-hoodie",
    "heavyweight-carpenter-denim-shorts",
    "minimalist-wool-blend-overcoat",
    "structured-twill-midi-skirt",
    "waffle-knit-long-sleeve-tee",
]


@pytest.mark.django_db
class TestTwentyProductsCatalog:
    @pytest.fixture(autouse=True)
    def seed_data(self, runner):
        runner("seed_catalog", "--with-images", verbosity=0)

    def test_exactly_twenty_products_exist_and_active(self):
        products = Product.objects.filter(slug__in=SEED_SLUGS)
        assert products.count() == 20
        assert all(p.status == Product.Status.ACTIVE for p in products)
        assert all(p.published_at is not None for p in products)

    def test_products_have_realistic_fashion_metadata(self):
        products = Product.objects.filter(slug__in=SEED_SLUGS)
        for p in products:
            assert len(p.name) > 3
            assert len(p.description) > 30
            assert len(p.short_description) > 15
            assert p.category is not None
            assert p.category.is_active is True
            assert p.materials.exists()
            assert p.tags.exists()
            assert p.fit is not None
            assert p.seo_title != ""
            assert p.seo_description != ""

    def test_all_variants_have_unique_skus_and_prices(self):
        products = Product.objects.filter(slug__in=SEED_SLUGS)
        skus = set()
        total_variants = 0
        for p in products:
            variants = p.variants.filter(is_active=True)
            assert variants.exists()
            for v in variants:
                total_variants += 1
                assert v.sku not in skus, f"Duplicate SKU: {v.sku}"
                skus.add(v.sku)
                assert v.price > 0
                if v.compare_at_price:
                    assert v.compare_at_price > v.price
        assert total_variants >= 200

    def test_inventory_has_healthy_low_and_out_of_stock_mixture(self):
        products = Product.objects.filter(slug__in=SEED_SLUGS)
        healthy = 0
        low = 0
        out_of_stock = 0

        for p in products:
            for v in p.variants.all():
                stock = getattr(v, "stock", None)
                assert stock is not None, f"Variant {v.sku} missing stock"
                if stock.available == 0:
                    out_of_stock += 1
                elif stock.available <= 5:
                    low += 1
                else:
                    healthy += 1

        assert healthy > 0, "Expected variants with healthy stock"
        assert low > 0, "Expected variants with low stock"
        assert out_of_stock > 0, "Expected variants out of stock"

    def test_all_products_have_primary_image_and_gallery(self):
        products = Product.objects.filter(slug__in=SEED_SLUGS)
        for p in products:
            primary = p.primary_image
            assert primary is not None, f"{p.name} missing primary image"
            assert p.images.filter(is_primary=True).count() == 1

    def test_storefront_rendering(self, client: Client):
        # Homepage
        r_home = client.get("/")
        assert r_home.status_code == 200

        # All products list (page 1 and page 2)
        r_products = client.get("/products/")
        assert r_products.status_code == 200
        assert b"Chunky Cable Knit Sweater" in r_products.content
        r_page2 = client.get("/products/?page=2")
        assert r_page2.status_code == 200
        assert b"Heavyweight Boxy Tee" in r_page2.content

        # Category pages
        for cat in Category.objects.filter(is_active=True, parent__isnull=False)[:4]:
            r_cat = client.get(cat.get_absolute_url())
            assert r_cat.status_code == 200

        # Collection pages
        for col in Collection.objects.filter(is_active=True)[:3]:
            r_col = client.get(col.get_absolute_url())
            assert r_col.status_code == 200

        # Product Detail Pages
        for slug in ["heavyweight-boxy-tee", "fleece-layer-hoodie", "chunky-cable-knit-sweater"]:
            r_pdp = client.get(f"/products/{slug}/")
            assert r_pdp.status_code == 200

        # Search
        r_search = client.get("/search/?q=hoodie")
        assert r_search.status_code == 200

    def test_seed_command_idempotency(self, runner):
        counts_before = (
            Product.objects.count(),
            ProductVariant.objects.count(),
            ProductImage.objects.count(),
            Stock.objects.count(),
        )
        runner("seed_catalog", "--with-images", verbosity=0)
        counts_after = (
            Product.objects.count(),
            ProductVariant.objects.count(),
            ProductImage.objects.count(),
            Stock.objects.count(),
        )
        assert counts_after == counts_before
