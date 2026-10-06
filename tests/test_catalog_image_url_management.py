"""Comprehensive tests for FLASHWEAR Admin Image URL Management.

Validates:
- Direct image URL validation, download, and storage
- Webpage URL handling, candidate extraction, and exact fallback error
- SSRF security controls: localhost, 127.0.0.1, private IP ranges, cloud metadata, schemes
- Redirect handling and redirect SSRF prevention
- Timeout, size ceiling, and format checks
- Attribution, licensing, and image source indicator
- Product image form and catalog admin integration
- Staff-only API permissions and IDOR prevention
"""

from __future__ import annotations

import ipaddress
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from apps.catalog.admin import ProductImageInline
from apps.catalog.forms import CollectionAdminForm, ProductImageForm
from apps.catalog.image_fetcher import (
    FetchResult,
    extract_images_from_webpage,
    fetch_and_validate_image_file,
    fetch_url_content,
    inspect_image_url,
    is_safe_ip,
    validate_url_for_ssrf,
)
from apps.catalog.models import Product, ProductImage

pytestmark = pytest.mark.django_db
User = get_user_model()


def _create_test_image_bytes(
    fmt: str = "PNG", size: tuple[int, int] = (400, 400), colour=(45, 95, 185)
) -> bytes:
    """Generate in-memory valid image bytes."""
    buf = BytesIO()
    Image.new("RGB", size, colour).save(buf, format=fmt)
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# 1. SSRF & Security Tests
# --------------------------------------------------------------------------------------


class TestSSRFProtection:
    """Ensure no requests reach loopback, private ranges, metadata or invalid schemes."""

    def test_safe_ip_recognizes_public_ips(self):
        assert is_safe_ip(ipaddress.ip_address("8.8.8.8")) is True
        assert is_safe_ip(ipaddress.ip_address("1.1.1.1")) is True
        assert is_safe_ip(ipaddress.ip_address("2606:4700:4700::1111")) is True

    @pytest.mark.parametrize(
        "ip_str",
        [
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.255",
            "::1",
            "10.0.0.1",
            "10.254.0.1",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.1.254",
            "169.254.169.254",  # AWS/GCP/Azure IMDS
            "169.254.1.1",      # link-local
            "100.64.0.1",       # CGNAT
            "100.100.100.200",  # Alibaba metadata
            "0.0.0.0",
            "224.0.0.1",        # multicast
            "240.0.0.1",        # reserved
        ],
    )
    def test_safe_ip_rejects_dangerous_and_private_ips(self, ip_str):
        assert is_safe_ip(ipaddress.ip_address(ip_str)) is False

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/photo.jpg",
            "https://localhost:8000/photo.jpg",
            "http://app.localhost/photo.jpg",
            "http://service.local/photo.jpg",
            "http://db.internal/photo.jpg",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://127.0.0.1/photo.jpg",
            "http://127.0.0.1:8080/photo.jpg",
            "http://[::1]/photo.jpg",
            "http://10.0.0.5/photo.jpg",
            "http://172.16.5.1/photo.jpg",
            "http://192.168.1.100/photo.jpg",
            "http://169.254.169.254/latest/meta-data/",
        ],
    )
    def test_validate_url_blocks_internal_and_private_hosts(self, url):
        with pytest.raises(ValidationError):
            validate_url_for_ssrf(url)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://ftp.example.com/photo.jpg",
            "gopher://example.com/",
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA",
            "javascript:alert(1)",
            "ws://example.com/socket",
        ],
    )
    def test_validate_url_blocks_unsupported_schemes(self, url):
        with pytest.raises(ValidationError) as err:
            validate_url_for_ssrf(url)
        assert err.value.code == "bad_scheme"

    def test_validate_url_blocks_embedded_userinfo(self):
        with pytest.raises(ValidationError) as err:
            validate_url_for_ssrf("http://admin:secret@example.com/photo.jpg")
        assert err.value.code == "disallowed_userinfo"

    def test_validate_url_accepts_valid_public_https_url(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", ("93.184.216.34", 443))
            ]
            scheme, host, port, safe_ip = validate_url_for_ssrf("https://example.com/image.jpg")
            assert scheme == "https"
            assert host == "example.com"
            assert port == 443
            assert safe_ip == "93.184.216.34"

    def test_blocks_redirect_to_private_ip(self):
        """A redirect to an internal IP must be blocked at the redirect step."""
        with patch("apps.catalog.image_fetcher.validate_url_for_ssrf") as mock_validate:
            # First URL is ok
            mock_validate.side_effect = [
                ("https", "example.com", 443, "93.184.216.34"),
                ValidationError("SSRF blocked", code="ssrf_blocked"),
            ]
            with patch("apps.catalog.image_fetcher._SSRFSafeHTTPSConnection") as mock_conn:
                instance = MagicMock()
                instance.getresponse.return_value.status = 302
                instance.getresponse.return_value.getheader.return_value = "http://127.0.0.1/private.png"
                instance.getresponse.return_value.getheaders.return_value = [("location", "http://127.0.0.1/private.png")]
                mock_conn.return_value = instance

                with pytest.raises(ValidationError) as err:
                    fetch_url_content("https://example.com/redirect", max_bytes=1000)
                assert err.value.code == "ssrf_blocked"


# --------------------------------------------------------------------------------------
# 2. Direct Image URL Fetching Tests
# --------------------------------------------------------------------------------------


class TestDirectImageFetching:
    """Test downloading and validating direct raster image URLs."""

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_downloads_and_validates_png_url(self, mock_fetch):
        png_bytes = _create_test_image_bytes("PNG", size=(500, 500))
        mock_fetch.return_value = FetchResult(
            content=png_bytes,
            content_type="image/png",
            final_url="https://images.example.com/product/front.png",
            status_code=200,
            headers={"content-type": "image/png"},
        )

        result = fetch_and_validate_image_file("https://images.example.com/product/front.png")
        assert result.format == "PNG"
        assert result.width == 500
        assert result.height == 500
        assert result.size == len(png_bytes)
        assert result.file.name.endswith(".png")
        assert result.source_url == "https://images.example.com/product/front.png"

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_downloads_and_validates_jpeg_url(self, mock_fetch):
        jpeg_bytes = _create_test_image_bytes("JPEG", size=(300, 300))
        mock_fetch.return_value = FetchResult(
            content=jpeg_bytes,
            content_type="image/jpeg",
            final_url="https://images.example.com/photo.jpg",
            status_code=200,
            headers={"content-type": "image/jpeg"},
        )

        result = fetch_and_validate_image_file("https://images.example.com/photo.jpg")
        assert result.format == "JPEG"
        assert result.width == 300
        assert result.height == 300
        assert result.file.name.endswith(".jpg")

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_rejects_unsupported_image_format_gif(self, mock_fetch):
        gif_bytes = _create_test_image_bytes("GIF", size=(200, 200))
        mock_fetch.return_value = FetchResult(
            content=gif_bytes,
            content_type="image/gif",
            final_url="https://images.example.com/anim.gif",
            status_code=200,
            headers={"content-type": "image/gif"},
        )

        with pytest.raises(ValidationError) as err:
            fetch_and_validate_image_file("https://images.example.com/anim.gif")
        assert err.value.code == "bad_format"

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_rejects_svg_image(self, mock_fetch):
        svg_content = b"<svg xmlns='http://www.w3.org/2000/svg'><circle r='10'/></svg>"
        mock_fetch.return_value = FetchResult(
            content=svg_content,
            content_type="image/svg+xml",
            final_url="https://images.example.com/vector.svg",
            status_code=200,
            headers={"content-type": "image/svg+xml"},
        )

        with pytest.raises(ValidationError) as err:
            fetch_and_validate_image_file("https://images.example.com/vector.svg")
        assert err.value.code in ("unreadable", "bad_format")

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_rejects_oversized_image_exceeding_max_bytes(self, mock_fetch):
        # Trigger ValidationError from size check
        mock_fetch.side_effect = ValidationError("Resource exceeded size limit", code="too_large")
        with pytest.raises(ValidationError) as err:
            fetch_and_validate_image_file("https://images.example.com/huge.png")
        assert err.value.code == "too_large"

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_rejects_decompression_bomb_dimensions(self, mock_fetch):
        """Reject dimensions exceeding CATALOG_IMAGE_MAX_PIXELS."""
        png_bytes = _create_test_image_bytes("PNG", size=(6500, 500))
        mock_fetch.return_value = FetchResult(
            content=png_bytes,
            content_type="image/png",
            final_url="https://images.example.com/wide.png",
            status_code=200,
            headers={"content-type": "image/png"},
        )

        with override_settings(CATALOG_IMAGE_MAX_PIXELS=6000):
            with pytest.raises(ValidationError) as err:
                fetch_and_validate_image_file("https://images.example.com/wide.png")
            assert err.value.code == "too_large_dimensions"


# --------------------------------------------------------------------------------------
# 3. Webpage URL & Extraction Tests
# --------------------------------------------------------------------------------------


class TestWebpageExtraction:
    """Test detecting webpage URLs, parsing images, and falling back safely."""

    def test_extracts_og_image_and_author_from_html(self):
        html = b"""<!DOCTYPE html>
        <html>
        <head>
            <title>Silk Minimalist Blouse - FLASHWEAR Inspiration</title>
            <meta property="og:title" content="Silk Minimalist Blouse" />
            <meta property="og:image" content="https://cdn.example.com/photos/blouse_main.jpg" />
            <meta name="author" content="Elena Rostova" />
        </head>
        <body>
            <h1>Silk Minimalist Blouse</h1>
            <img src="/assets/thumb.jpg" alt="Thumbnail" />
            <img src="https://tracker.com/1x1.gif" alt="Tracker" />
        </body>
        </html>
        """
        result = extract_images_from_webpage(html, "https://example.com/blouse")
        assert result.title == "Silk Minimalist Blouse"
        assert result.author == "Elena Rostova"
        assert len(result.candidates) >= 1
        assert result.candidates[0].url == "https://cdn.example.com/photos/blouse_main.jpg"
        assert result.candidates[0].source == "og:image"
        # Tracker 1x1 gif must be excluded
        assert not any("1x1.gif" in c.url for c in result.candidates)

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_inspect_recognizes_direct_image(self, mock_fetch):
        png_bytes = _create_test_image_bytes("PNG", size=(400, 400))
        mock_fetch.return_value = FetchResult(
            content=png_bytes,
            content_type="image/png",
            final_url="https://cdn.example.com/photo.png",
            status_code=200,
            headers={"content-type": "image/png"},
        )

        info = inspect_image_url("https://cdn.example.com/photo.png")
        assert info["type"] == "image"
        assert info["format"] == "PNG"
        assert info["width"] == 400
        assert info["height"] == 400

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_inspect_recognizes_webpage_with_candidates(self, mock_fetch):
        html = b"""<!DOCTYPE html>
        <html>
        <head>
            <title>Stock Photo Model</title>
            <meta property="og:image" content="https://cdn.example.com/stock_photo.jpg" />
        </head>
        <body><p>Sample</p></body>
        </html>"""
        mock_fetch.return_value = FetchResult(
            content=html,
            content_type="text/html; charset=utf-8",
            final_url="https://stock.example.com/photo/123",
            status_code=200,
            headers={"content-type": "text/html"},
        )

        info = inspect_image_url("https://stock.example.com/photo/123")
        assert info["type"] == "webpage"
        assert len(info["candidates"]) == 1
        assert info["candidates"][0]["url"] == "https://cdn.example.com/stock_photo.jpg"

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_webpage_without_extractable_images_returns_clear_error(self, mock_fetch):
        """When extraction is not possible, show the exact required error."""
        html = b"""<!DOCTYPE html>
        <html><head><title>No Images Here</title></head><body><p>Text only</p></body></html>"""
        mock_fetch.return_value = FetchResult(
            content=html,
            content_type="text/html",
            final_url="https://example.com/article",
            status_code=200,
            headers={"content-type": "text/html"},
        )

        with pytest.raises(ValidationError) as err:
            fetch_and_validate_image_file("https://example.com/article")
        assert "This URL is a webpage, not a directly accessible image" in str(err.value)

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_webpage_auto_imports_top_candidate(self, mock_fetch):
        html = b"""<!DOCTYPE html>
        <html><head>
            <title>Fashion Shoot</title>
            <meta property="og:image" content="https://cdn.example.com/hero.png" />
            <meta name="author" content="Studio Milan" />
        </head><body></body></html>"""
        png_bytes = _create_test_image_bytes("PNG")

        # 1st call fetches HTML, 2nd call fetches the candidate hero.png
        mock_fetch.side_effect = [
            FetchResult(
                content=html,
                content_type="text/html",
                final_url="https://example.com/gallery",
                status_code=200,
                headers={"content-type": "text/html"},
            ),
            FetchResult(
                content=png_bytes,
                content_type="image/png",
                final_url="https://cdn.example.com/hero.png",
                status_code=200,
                headers={"content-type": "image/png"},
            ),
        ]

        result = fetch_and_validate_image_file("https://example.com/gallery")
        assert result.format == "PNG"
        assert result.source_url == "https://example.com/gallery"
        assert result.suggested_photographer == "Studio Milan"


# --------------------------------------------------------------------------------------
# 4. ProductImageForm & Admin Integration Tests
# --------------------------------------------------------------------------------------


class TestProductImageFormAndAdmin:
    """Validate admin form behaviour and admin presentation."""

    def test_form_requires_either_file_or_source_url(self, product):
        form = ProductImageForm(
            data={"alt_text": "Hoodie shot", "position": 0},
            product=product,
        )
        assert not form.is_valid()
        assert "Provide an image file or an external URL" in str(form.errors)

    def test_form_accepts_file_upload_without_url(self, product):
        png_bytes = _create_test_image_bytes("PNG")
        upload = SimpleUploadedFile("local.png", png_bytes, content_type="image/png")
        form = ProductImageForm(
            data={"alt_text": "Local hoodie shot", "position": 0},
            files={"image": upload},
            product=product,
        )
        assert form.is_valid(), form.errors
        saved = form.save(commit=False)
        saved.product = product
        saved.save()
        assert saved.image_source == "Uploaded File"
        assert not saved.source_url

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_form_accepts_valid_source_url_without_file(self, mock_fetch, product):
        png_bytes = _create_test_image_bytes("PNG")
        mock_fetch.return_value = FetchResult(
            content=png_bytes,
            content_type="image/png",
            final_url="https://cdn.example.com/jacket.png",
            status_code=200,
            headers={"content-type": "image/png"},
        )

        form = ProductImageForm(
            data={
                "source_url": "https://cdn.example.com/jacket.png",
                "alt_text": "Imported jacket",
                "photographer": "Alex Carter",
                "license": "Unsplash Commercial",
                "position": 1,
            },
            product=product,
        )
        assert form.is_valid(), form.errors
        saved = form.save(commit=False)
        saved.product = product
        saved.save()

        assert saved.image_source == "External URL"
        assert saved.source_url == "https://cdn.example.com/jacket.png"
        assert saved.photographer == "Alex Carter"
        assert saved.license == "Unsplash Commercial"
        assert saved.image.name.endswith(".png")

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_form_shows_clear_error_for_unextractable_webpage(self, mock_fetch, product):
        html = b"<html><body>No images</body></html>"
        mock_fetch.return_value = FetchResult(
            content=html,
            content_type="text/html",
            final_url="https://example.com/empty",
            status_code=200,
            headers={"content-type": "text/html"},
        )

        form = ProductImageForm(
            data={
                "source_url": "https://example.com/empty",
                "alt_text": "Empty page",
            },
            product=product,
        )
        assert not form.is_valid()
        assert "source_url" in form.errors
        err_msg = str(form.errors["source_url"])
        assert "This URL is a webpage, not a directly accessible image" in err_msg

    def test_admin_displays_correct_source_badge(self, product):
        site = AdminSite()
        inline = ProductImageInline(parent_model=Product, admin_site=site)

        # Uploaded file instance
        file_payload = _create_test_image_bytes()
        uploaded_img = ProductImage.objects.create(
            product=product,
            image=SimpleUploadedFile("shot.png", file_payload, content_type="image/png"),
            alt_text="Uploaded",
            source_url="",
        )
        badge_html = inline.source_badge(uploaded_img)
        assert "Uploaded File" in badge_html

        # External URL instance
        external_img = ProductImage.objects.create(
            product=product,
            image=SimpleUploadedFile("ext.png", file_payload, content_type="image/png"),
            alt_text="External",
            source_url="https://images.unsplash.com/photo-12345",
        )
        badge_ext = inline.source_badge(external_img)
        assert "External" in badge_ext
        assert "images.unsplash.com" in badge_ext

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_collection_admin_form_with_hero_image_url(self, mock_fetch):
        png_bytes = _create_test_image_bytes("PNG")
        mock_fetch.return_value = FetchResult(
            content=png_bytes,
            content_type="image/png",
            final_url="https://cdn.example.com/hero.png",
            status_code=200,
            headers={"content-type": "image/png"},
        )

        form = CollectionAdminForm(
            data={
                "name": "Winter 2026",
                "slug": "winter-2026",
                "hero_image_url": "https://cdn.example.com/hero.png",
                "display_order": 0,
            }
        )
        assert form.is_valid(), form.errors
        collection = form.save()
        assert collection.hero_image.name.endswith(".png")


# --------------------------------------------------------------------------------------
# 5. API Endpoints & Permissions Tests
# --------------------------------------------------------------------------------------


class TestStaffImageAPI:
    """Ensure staff endpoints enforce authentication, staff checks, and SSRF rules."""

    def test_inspect_endpoint_requires_staff_authentication(self, client):
        resp = client.get("/api/v1/catalog/admin/images/inspect-url/?url=https://example.com/img.jpg")
        assert resp.status_code in (401, 403)

    def test_inspect_endpoint_rejects_regular_non_staff_customer(self, client):
        customer = User.objects.create_user(
            email="customer@example.com",
            password="testpassword123",
            is_staff=False,
        )
        client.force_login(customer)
        resp = client.get("/api/v1/catalog/admin/images/inspect-url/?url=https://example.com/img.jpg")
        assert resp.status_code == 403

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_inspect_endpoint_returns_image_metadata_to_staff(self, mock_fetch, client):
        staff = User.objects.create_user(
            email="staff@example.com",
            password="testpassword123",
            is_staff=True,
        )
        client.force_login(staff)

        png_bytes = _create_test_image_bytes("PNG", size=(600, 400))
        mock_fetch.return_value = FetchResult(
            content=png_bytes,
            content_type="image/png",
            final_url="https://images.example.com/jacket.png",
            status_code=200,
            headers={"content-type": "image/png"},
        )

        resp = client.get("/api/v1/catalog/admin/images/inspect-url/?url=https://images.example.com/jacket.png")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "image"
        assert data["format"] == "PNG"
        assert data["width"] == 600
        assert data["height"] == 400

    def test_inspect_endpoint_blocks_ssrf_attempt_by_staff(self, client):
        staff = User.objects.create_user(
            email="staff_security@example.com",
            password="testpassword123",
            is_staff=True,
        )
        client.force_login(staff)

        resp = client.get("/api/v1/catalog/admin/images/inspect-url/?url=http://127.0.0.1/admin.png")
        assert resp.status_code == 400
        assert "error" in resp.json()

    @patch("apps.catalog.image_fetcher.fetch_url_content")
    def test_product_image_create_api_adds_external_image(self, mock_fetch, client, product):
        staff = User.objects.create_user(
            email="staff_uploader@example.com",
            password="testpassword123",
            is_staff=True,
        )
        client.force_login(staff)

        png_bytes = _create_test_image_bytes("PNG")
        mock_fetch.return_value = FetchResult(
            content=png_bytes,
            content_type="image/png",
            final_url="https://cdn.example.com/jacket.png",
            status_code=200,
            headers={"content-type": "image/png"},
        )

        resp = client.post(
            f"/api/v1/catalog/admin/products/{product.pk}/images/",
            {
                "source_url": "https://cdn.example.com/jacket.png",
                "alt_text": "API imported jacket",
                "photographer": "Photo Studio",
                "license": "Commercial",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["alt_text"] == "API imported jacket"
        assert data["source_url"] == "https://cdn.example.com/jacket.png"
        assert data["image_source"] == "External URL"

    def test_product_image_create_api_prevents_idor(self, client):
        staff = User.objects.create_user(
            email="staff_idor@example.com",
            password="testpassword123",
            is_staff=True,
        )
        client.force_login(staff)

        # Product 999999 does not exist
        resp = client.post("/api/v1/catalog/admin/products/999999/images/", {"alt_text": "Missing"})
        assert resp.status_code == 404
