"""Phase 37: Visual Quality, Image Optimization & Fashion Brand Polish Regression Test Suite.

Covers:
- Safe image property fallbacks on ProductImage and Product (no ValueError on unassigned files)
- Product detail view gallery serialization resilience
- Product detail hero image direct DOM rendering, fetchpriority="high", decoding="async"
- Alpine gallery bindings and active thumbnail ring indicators
- Product gallery thumbnail active state ring indicators and SVG fallback
- Product card responsive aspect-[4/5], loading="lazy", decoding="async", and onerror fallback
- Wishlist aspect ratio utility correction (aspect-[3/4])
- Admin media preview onerror indicators and Media asset registration on ProductImageInline
- Admin preview view context injection of gallery_data
- SSRF security preservation for external image URLs
"""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import get_template
from django.test import RequestFactory
from django.urls import reverse
from PIL import Image

from apps.catalog.admin import ProductAdmin, ProductImageAdmin, ProductImageInline
from apps.catalog.image_fetcher import validate_url_for_ssrf
from apps.catalog.models import (
    Brand,
    Category,
    Color,
    Fit,
    Product,
    ProductImage,
    ProductVariant,
    Size,
)

pytestmark = pytest.mark.django_db
User = get_user_model()


def _dummy_image_file(
    name: str = "test.png", size: tuple[int, int] = (600, 800)
) -> SimpleUploadedFile:
    buf = BytesIO()
    Image.new("RGB", size, (20, 40, 80)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


@pytest.fixture
def sample_product(db):
    brand = Brand.objects.create(name="Studio Flash", slug="studio-flash", is_active=True)
    category = Category.objects.create(name="Tops", slug="tops", is_active=True)
    fit = Fit.objects.create(name="Relaxed", slug="relaxed", is_active=True)
    return Product.objects.create(
        name="Tailored Silk Overshirt",
        slug="tailored-silk-overshirt",
        brand=brand,
        category=category,
        fit=fit,
        short_description="Premium heavyweight silk overshirt.",
        description="Premium heavyweight silk overshirt with relaxed tailoring.",
        status=Product.Status.ACTIVE,
    )


@pytest.fixture
def auth_user(db):
    return User.objects.create_user(
        email="shopper@flashwear.test",
        password="TestPassword123!",
        first_name="Jordan",
        last_name="Lee",
    )


@pytest.fixture
def staff_user(db):
    return User.objects.create_superuser(
        email="admin@flashwear.test",
        password="AdminPassword123!",
        first_name="Admin",
        last_name="Staff",
    )


# --------------------------------------------------------------------------------------
# 1. Image Reliability & Safe Property Access
# --------------------------------------------------------------------------------------


class TestImageReliability:
    def test_product_image_url_with_valid_file(self, sample_product):
        uploaded = _dummy_image_file()
        img = ProductImage.objects.create(
            product=sample_product,
            image=uploaded,
            alt_text="Front view",
            position=0,
        )
        assert img.image_url != ""
        assert sample_product.slug in img.image_url or "catalog/products" in img.image_url

    def test_product_image_url_unassigned_returns_empty_string_safely(self, sample_product):
        # Image instance created without a file associated
        img = ProductImage(product=sample_product, alt_text="Placeholder")
        # Direct access to img.image.url would raise ValueError, but image_url property must not:
        assert img.image_url == ""

    def test_product_primary_image_url_returns_correctly(self, sample_product):
        # When product has no images
        assert sample_product.primary_image_url == ""

        # Add an image
        uploaded = _dummy_image_file()
        ProductImage.objects.create(
            product=sample_product,
            image=uploaded,
            alt_text="Primary shot",
            is_primary=True,
            position=0,
        )
        sample_product.refresh_from_db()
        assert sample_product.primary_image_url != ""

    def test_gallery_serialization_survives_missing_file_in_view(self, sample_product, client):
        # Product with valid variant to be fully viewable
        color = Color.objects.create(name="Obsidian", hex_code="#000000")
        size = Size.objects.create(name="Medium", code="M", is_active=True, display_order=1)
        ProductVariant.objects.create(
            product=sample_product,
            color=color,
            size=size,
            sku="SLK-OBS-M",
            price=Decimal("180.00"),
            is_active=True,
        )

        # Create one image with valid file, and one without file saved
        valid_img = _dummy_image_file("valid.png")
        ProductImage.objects.create(
            product=sample_product,
            image=valid_img,
            alt_text="Valid front",
            position=0,
            is_primary=True,
        )
        unassigned_img = ProductImage(product=sample_product, alt_text="Unassigned", position=1)
        unassigned_img.image = None
        unassigned_img.save()

        response = client.get(sample_product.get_absolute_url())
        assert response.status_code == 200
        gallery_data = response.context.get("gallery_data", [])
        assert len(gallery_data) >= 1
        for item in gallery_data:
            assert item["url"] != ""


# --------------------------------------------------------------------------------------
# 2. Product Detail Gallery Polish
# --------------------------------------------------------------------------------------


class TestProductDetailGalleryPolish:
    def test_hero_image_direct_dom_rendering_and_priority(self, sample_product, client):
        uploaded = _dummy_image_file()
        ProductImage.objects.create(
            product=sample_product,
            image=uploaded,
            alt_text="Silk front",
            is_primary=True,
            position=0,
        )
        color = Color.objects.create(name="Noir", hex_code="#111111")
        size = Size.objects.create(name="Large", code="L", is_active=True, display_order=2)
        ProductVariant.objects.create(
            product=sample_product,
            color=color,
            size=size,
            sku="SLK-NOIR-L",
            price=Decimal("210.00"),
            is_active=True,
        )

        response = client.get(sample_product.get_absolute_url())
        assert response.status_code == 200
        content = response.content.decode("utf-8")

        # Must have fetchpriority="high" and decoding="async" for LCP optimization
        assert 'fetchpriority="high"' in content
        assert 'decoding="async"' in content
        # Must have Alpine bindings for interactive gallery switching
        assert ":src=" in content
        assert ":alt=" in content
        # Must have ring-offset-2 for active thumbnail visual indicator
        assert "ring-offset-2" in content
        # Must have graceful SVG fallback markup for error handling
        assert "image-fallback-placeholder" in content

    def test_gallery_thumbnail_keyboard_and_accessibility(self, sample_product, client):
        for i in range(3):
            ProductImage.objects.create(
                product=sample_product,
                image=_dummy_image_file(f"view_{i}.png"),
                alt_text=f"View angle {i}",
                is_primary=(i == 0),
                position=i,
            )

        color = Color.objects.create(name="Navy", hex_code="#001F3F")
        size = Size.objects.create(name="Small", code="S", is_active=True, display_order=0)
        ProductVariant.objects.create(
            product=sample_product,
            color=color,
            size=size,
            sku="SLK-NVY-S",
            price=Decimal("195.00"),
            is_active=True,
        )

        response = client.get(sample_product.get_absolute_url())
        content = response.content.decode("utf-8")

        # Verify thumbnail buttons have type="button" and click handler
        assert 'type="button"' in content
        assert "@click=" in content


# --------------------------------------------------------------------------------------
# 3. Storefront Fashion Polish & Aspect Ratios
# --------------------------------------------------------------------------------------


class TestStorefrontFashionPolish:
    def test_product_card_template_resilience_and_aspect_ratio(self, sample_product, client):
        ProductImage.objects.create(
            product=sample_product,
            image=_dummy_image_file(),
            alt_text="Front card",
            is_primary=True,
            position=0,
        )
        color = Color.objects.create(name="White", hex_code="#ffffff")
        size = Size.objects.create(name="One Size", code="OS", is_active=True, display_order=0)
        ProductVariant.objects.create(
            product=sample_product,
            color=color,
            size=size,
            sku="SLK-WHT-OS",
            price=Decimal("150.00"),
            is_active=True,
        )
        response = client.get(reverse("catalog:product-list"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")

        # Standardized editorial fashion ratio
        assert "aspect-[4/5]" in content
        # Lazy loading & async decoding for below-fold cards
        assert 'loading="lazy"' in content
        assert 'decoding="async"' in content
        # Graceful SVG placeholder presence
        assert "FLASHWEAR" in content

    def test_wishlist_template_has_correct_aspect_ratio_utility(self):
        template = get_template("shop/wishlist.html")
        source = template.template.source

        # Ensure the invalid typo 'aspect-3/4' is not present
        assert "aspect-3/4" not in source
        # Ensure modern arbitrary ratio 'aspect-[3/4]' is used
        assert "aspect-[3/4]" in source

    def test_cart_template_has_image_dimensions_and_resilience(self):
        # Inspect cart template directly to confirm attributes and CSP-safe resilience
        template = get_template("shop/cart.html")
        source = template.template.source
        assert 'decoding="async"' in source
        assert "image-fallback-placeholder" in source
        # Must stay CSP-clean without inline onerror
        assert "onerror=" not in source


# --------------------------------------------------------------------------------------
# 4. Admin Media Experience
# --------------------------------------------------------------------------------------


class TestAdminMediaExperience:
    def test_product_image_inline_includes_media_assets(self):
        inline = ProductImageInline(Product, AdminSite())
        media = inline.media
        # Verify css and js for image URL management are registered
        css_rendered = str(media["css"])
        js_rendered = str(media["js"])
        assert "image_url_manager.css" in css_rendered
        assert "image_url_manager.js" in js_rendered

    def test_product_image_admin_preview_thumb_includes_onerror(self, sample_product):
        admin = ProductImageAdmin(ProductImage, AdminSite())
        img = ProductImage.objects.create(
            product=sample_product,
            image=_dummy_image_file(),
            alt_text="Admin test",
            position=0,
        )
        preview_html = str(admin.preview_thumb(img))
        assert "onerror=" in preview_html

    def test_product_admin_primary_image_thumb_includes_onerror(self, sample_product):
        admin = ProductAdmin(Product, AdminSite())
        ProductImage.objects.create(
            product=sample_product,
            image=_dummy_image_file(),
            alt_text="Primary thumb",
            is_primary=True,
            position=0,
        )
        thumb_html = str(admin.primary_image_thumb(sample_product))
        assert "onerror=" in thumb_html
        assert "object-fit:cover" in thumb_html

    def test_product_admin_preview_view_injects_gallery_data(self, sample_product, staff_user):
        admin = ProductAdmin(Product, AdminSite())
        ProductImage.objects.create(
            product=sample_product,
            image=_dummy_image_file(),
            alt_text="Preview gallery",
            is_primary=True,
            position=0,
        )
        color = Color.objects.create(name="Charcoal", hex_code="#333333")
        size = Size.objects.create(name="Medium", code="M", is_active=True, display_order=1)
        ProductVariant.objects.create(
            product=sample_product,
            color=color,
            size=size,
            sku="SLK-CHR-M",
            price=Decimal("190.00"),
            is_active=True,
        )

        factory = RequestFactory()
        req = factory.get(f"/admin/catalog/product/{sample_product.pk}/preview/")
        req.user = staff_user

        response = admin.preview_view(req, str(sample_product.pk))
        assert response.status_code == 200
        rendered_content = response.content.decode("utf-8")
        assert "gallery-data" in rendered_content


# --------------------------------------------------------------------------------------
# 5. External URL Security
# --------------------------------------------------------------------------------------


class TestExternalURLSecurity:
    def test_ssrf_rejects_loopback_and_metadata(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            validate_url_for_ssrf("http://127.0.0.1/malicious.png")

        with pytest.raises(ValidationError):
            validate_url_for_ssrf("http://localhost/malicious.png")

        with pytest.raises(ValidationError):
            validate_url_for_ssrf("http://169.254.169.254/latest/meta-data/")

        with pytest.raises(ValidationError):
            validate_url_for_ssrf("http://10.0.0.1/private.png")

    def test_ssrf_accepts_valid_public_https_domain(self):
        # A valid public domain with safe scheme
        url = "https://images.unsplash.com/photo-12345678"
        scheme, hostname, port, _ip = validate_url_for_ssrf(url)
        assert scheme == "https"
        assert hostname == "images.unsplash.com"
        assert port == 443
