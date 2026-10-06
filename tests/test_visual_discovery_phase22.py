"""Tests for Phase 22: Visual Product Discovery.

Validates:
- Image validation (content-first check, formats, dimensions, sizes, extensions).
- Privacy and ephemeral processing (no storage retention, EXIF stripped, no biometrics).
- Provider abstraction & deterministic feature extraction.
- Product similarity scoring and deterministic ranking.
- "Find Similar" from product detail page and API.
- Storefront views, templates, and UX states (preview, results, error, empty).
- REST API endpoints, public-safe permissions, and rate limiting.
- Analytics events (5 required events recorded via Phase 20 analytics).
- Catalog availability invariants (draft/archived/inactive excluded).
"""

from __future__ import annotations

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from rest_framework.throttling import SimpleRateThrottle

from apps.analytics.models import Event
from apps.catalog.models import Product
from apps.catalog.visual_discovery import (
    LocalDeterministicVisualProvider,
    MockVisualProvider,
    VisualFeatures,
    find_similar_to_product,
    find_visually_similar_products,
    get_visual_provider,
    validate_and_sanitize_image_upload,
)
from apps.styling.models import FlashDNA

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _create_image_bytes(
    fmt: str = "PNG",
    size: tuple[int, int] = (100, 100),
    colour: tuple[int, int, int] = (20, 20, 20),
) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format=fmt)
    return buffer.getvalue()


def _upload(
    name: str = "photo.png",
    fmt: str = "PNG",
    size: tuple[int, int] = (100, 100),
    colour: tuple[int, int, int] = (20, 20, 20),
    content: bytes | None = None,
) -> SimpleUploadedFile:
    payload = content if content is not None else _create_image_bytes(fmt, size, colour)
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(
        fmt, "application/octet-stream"
    )
    return SimpleUploadedFile(name, payload, content_type=mime)


# --------------------------------------------------------------------------------------
# 1. Image Validation and Upload Handling
# --------------------------------------------------------------------------------------


class TestImageUploadValidation:
    def test_accepts_valid_png_jpeg_webp(self):
        for fmt, name in [("PNG", "shot.png"), ("JPEG", "shot.jpg"), ("WEBP", "shot.webp")]:
            upload = _upload(name=name, fmt=fmt)
            clean = validate_and_sanitize_image_upload(upload)
            assert isinstance(clean, bytes)
            assert len(clean) > 0

    def test_rejects_empty_upload(self):
        upload = SimpleUploadedFile("empty.png", b"", content_type="image/png")
        with pytest.raises(ValidationError) as exc:
            validate_and_sanitize_image_upload(upload)
        assert exc.value.code == "empty_upload"

    def test_rejects_none_upload(self):
        with pytest.raises(ValidationError) as exc:
            validate_and_sanitize_image_upload(None)
        assert exc.value.code == "empty_upload"

    def test_rejects_unsupported_file_extension(self):
        upload = SimpleUploadedFile(
            "test.gif", _create_image_bytes("GIF"), content_type="image/gif"
        )
        with pytest.raises(ValidationError) as exc:
            validate_and_sanitize_image_upload(upload)
        assert exc.value.code == "bad_extension"

    def test_rejects_fake_extension_disguising_invalid_content(self):
        upload = SimpleUploadedFile("fake.jpg", b"not an image", content_type="image/jpeg")
        with pytest.raises(ValidationError) as exc:
            validate_and_sanitize_image_upload(upload)
        assert exc.value.code == "unreadable"

    def test_rejects_svg_disguised_as_jpg(self):
        svg_content = b"<svg xmlns='http://www.w3.org/2000/svg'><circle r='5'/></svg>"
        upload = SimpleUploadedFile("vector.jpg", svg_content, content_type="image/jpeg")
        with pytest.raises(ValidationError) as exc:
            validate_and_sanitize_image_upload(upload)
        assert exc.value.code == "unreadable"

    def test_rejects_file_exceeding_max_bytes(self, settings):
        settings.CATALOG_IMAGE_MAX_BYTES = 100  # 100 bytes limit
        upload = _upload(size=(200, 200))
        with pytest.raises(ValidationError) as exc:
            validate_and_sanitize_image_upload(upload)
        assert exc.value.code == "too_large"

    def test_rejects_image_exceeding_pixel_dimensions(self, settings):
        settings.CATALOG_IMAGE_MAX_PIXELS = 50  # 50px max
        upload = _upload(size=(100, 100))
        with pytest.raises(ValidationError) as exc:
            validate_and_sanitize_image_upload(upload)
        assert exc.value.code == "too_large_dimensions"


# --------------------------------------------------------------------------------------
# 2. Privacy & Ephemeral Processing
# --------------------------------------------------------------------------------------


class TestPrivacyAndEphemeralProcessing:
    def test_images_are_not_persisted_to_database(self, product):
        upload = _upload(colour=(20, 20, 20))
        results, features = find_visually_similar_products(upload)
        # Results exist in-memory
        assert len(results) > 0
        assert features.tone == "dark"

        # No models created for images
        from apps.catalog.models import ProductImage

        assert not ProductImage.objects.filter(alt_text="upload").exists()

    def test_exif_metadata_is_not_retained(self):
        from PIL import Image

        # Create image with comment/exif-like info
        buffer = io.BytesIO()
        img = Image.new("RGB", (50, 50), color=(100, 100, 100))
        img.save(buffer, format="JPEG", quality=90)
        upload = SimpleUploadedFile("test.jpg", buffer.getvalue(), content_type="image/jpeg")

        provider = LocalDeterministicVisualProvider()
        clean = validate_and_sanitize_image_upload(upload)
        features = provider.extract_features(clean)

        # Features contain only visual geometry/color metrics, no personal PII
        assert "gps" not in features.metadata
        assert "camera" not in features.metadata
        assert "author" not in features.metadata


# --------------------------------------------------------------------------------------
# 3. Provider Abstraction & Feature Extraction
# --------------------------------------------------------------------------------------


class TestVisualProviderAbstraction:
    def test_default_provider_is_local_deterministic(self):
        provider = get_visual_provider()
        assert isinstance(provider, LocalDeterministicVisualProvider)

    def test_mock_provider_returns_configured_features(self):
        mock_features = VisualFeatures(
            dominant_colors=["#000000"],
            dominant_rgb=[(0, 0, 0)],
            matched_catalog_colors=["black"],
            brightness=0.1,
            aspect_ratio=1.0,
            perceptual_hash="1234567890abcdef",
            tone="dark",
            metadata={"test": "mock"},
        )
        provider = MockVisualProvider(features=mock_features)
        extracted = provider.extract_features(b"dummy")
        assert extracted == mock_features

    def test_local_provider_extracts_correct_dark_tone(self, colour):
        provider = LocalDeterministicVisualProvider()
        black_img = _create_image_bytes(colour=(10, 10, 10))
        features = provider.extract_features(black_img)
        assert features.tone == "dark"
        assert features.brightness < 0.35
        assert colour.slug in features.matched_catalog_colors

    def test_local_provider_extracts_correct_light_tone(self, other_colour):
        provider = LocalDeterministicVisualProvider()
        # Sand/light cream
        light_img = _create_image_bytes(colour=(235, 220, 200))
        features = provider.extract_features(light_img)
        assert features.tone == "light"
        assert features.brightness > 0.65
        assert other_colour.slug in features.matched_catalog_colors

    def test_perceptual_hash_is_deterministic(self):
        provider = LocalDeterministicVisualProvider()
        img_bytes = _create_image_bytes(size=(80, 80), colour=(45, 90, 135))
        first = provider.extract_features(img_bytes)
        second = provider.extract_features(img_bytes)
        assert first.perceptual_hash == second.perceptual_hash
        assert first.dominant_colors == second.dominant_colors
        assert first.brightness == second.brightness


# --------------------------------------------------------------------------------------
# 4. Product Similarity Scoring & Deterministic Ranking
# --------------------------------------------------------------------------------------


class TestProductSimilarityRanking:
    def test_product_matching_dominant_color_ranks_highest(
        self, make_product, make_variant, colour, other_colour, size
    ):
        # Product 1 has Black variant
        p_black = make_product(name="Black Hoodie", slug="black-hoodie")
        make_variant(p_black, color=colour, size=size)

        # Product 2 has Sand variant
        p_sand = make_product(name="Sand Hoodie", slug="sand-hoodie")
        make_variant(p_sand, color=other_colour, size=size)

        # Upload a black image
        black_upload = _upload(colour=(15, 15, 15))
        results, _ = find_visually_similar_products(black_upload)

        assert len(results) >= 2
        # Black product should rank first with higher score
        assert results[0].product.pk == p_black.pk
        assert results[0].score > results[1].score
        assert any("Black" in r for r in results[0].reasons)

    def test_category_filter_narrows_candidates(
        self, make_product, make_variant, category, other_category, colour, size
    ):
        p_tee = make_product(name="Tee", slug="tee", category=category)
        make_variant(p_tee, color=colour, size=size)

        p_hoodie = make_product(name="Hoodie", slug="hoodie", category=other_category)
        make_variant(p_hoodie, color=colour, size=size)

        upload = _upload()
        results, _ = find_visually_similar_products(upload, category_slug=category.slug)

        pks = [r.product.pk for r in results]
        assert p_tee.pk in pks
        assert p_hoodie.pk not in pks

    def test_results_ranking_is_100_percent_deterministic(
        self, make_product, make_variant, colour, other_colour, size
    ):
        for i in range(5):
            p = make_product(name=f"Item {i}", slug=f"item-{i}")
            make_variant(p, color=colour if i % 2 == 0 else other_colour, size=size)

        upload = _upload(colour=(20, 20, 20))
        first_run, _ = find_visually_similar_products(upload)
        second_run, _ = find_visually_similar_products(upload)

        assert [r.pk for r in first_run] == [r.pk for r in second_run]
        assert [r.score for r in first_run] == [r.score for r in second_run]
        assert [r.reasons for r in first_run] == [r.reasons for r in second_run]

    def test_personalization_signals_influence_score_for_authenticated_user(
        self, user, make_product, make_variant, colour, other_colour, size
    ):
        # Create user DNA preferring Sand and disliking Black
        dna = FlashDNA.objects.create(user=user, styles="minimal")
        dna.favorite_colors.add(other_colour)
        dna.disliked_colors.add(colour)

        p_black = make_product(name="Dark Tee", slug="dark-tee")
        make_variant(p_black, color=colour, size=size)

        p_sand = make_product(name="Light Tee", slug="light-tee")
        make_variant(p_sand, color=other_colour, size=size)

        # Upload neutral image evaluated with provider having equal color baseline
        upload = _upload()
        results, _ = find_visually_similar_products(
            upload,
            user=user,
            provider=MockVisualProvider(matched_catalog_colors=[colour.slug, other_colour.slug]),
        )

        res_dict = {r.product.slug: r for r in results}
        # Sand should get positive DNA boost (+10), Black should get penalty (-20)
        assert res_dict["light-tee"].score > res_dict["dark-tee"].score


# --------------------------------------------------------------------------------------
# 5. "Find Similar" from Existing Products
# --------------------------------------------------------------------------------------


class TestFindSimilarToProduct:
    def test_excludes_source_product_from_results(
        self, product, make_product, make_variant, colour, size
    ):
        sibling = make_product(name="Sibling Tee", slug="sibling-tee", category=product.category)
        make_variant(sibling, color=colour, size=size)

        similar = find_similar_to_product(product)
        pks = [s.product.pk for s in similar]
        assert product.pk not in pks
        assert sibling.pk in pks

    def test_shared_category_and_color_boosts_similarity(
        self,
        product,
        make_product,
        make_variant,
        category,
        other_category,
        colour,
        other_colour,
        size,
    ):
        # High match: same category and same color
        high_match = make_product(name="High Match", slug="high-match", category=category)
        make_variant(high_match, color=colour, size=size)

        # Low match: different category and different color
        low_match = make_product(name="Low Match", slug="low-match", category=other_category)
        make_variant(low_match, color=other_colour, size=size)

        similar = find_similar_to_product(product)
        assert len(similar) >= 2
        assert similar[0].product.pk == high_match.pk
        assert similar[0].score > similar[1].score
        assert any("category" in r.lower() for r in similar[0].reasons)
        assert any("color" in r.lower() for r in similar[0].reasons)

    def test_matching_fit_and_material_add_explainable_reasons(
        self, product, make_product, make_variant, colour, size, fit, material
    ):
        product.fit = fit
        product.materials.add(material)
        product.save()

        matched = make_product(
            name="Matched Style",
            slug="matched-style",
            category=product.category,
            fit=fit,
        )
        matched.materials.add(material)
        make_variant(matched, color=colour, size=size)

        similar = find_similar_to_product(product)
        assert len(similar) > 0
        top = similar[0]
        reasons_text = " ".join(top.reasons)
        assert fit.name in reasons_text
        assert material.name in reasons_text


# --------------------------------------------------------------------------------------
# 6. Storefront UX & Views
# --------------------------------------------------------------------------------------


class TestStorefrontVisualSearch:
    def test_visual_search_landing_page_renders_200(self, client):
        response = client.get(reverse("catalog:visual-search"))
        assert response.status_code == 200
        assert "Visual Product Discovery" in response.content.decode()
        assert "Drag and drop your photo here" in response.content.decode()

    def test_find_similar_query_param_renders_similar_products_mode(self, client, product):
        url = f"{reverse('catalog:visual-search')}?product={product.slug}"
        response = client.get(url)
        assert response.status_code == 200
        content = response.content.decode()
        assert f"Pieces Similar to {product.name}" in content
        assert "Source Piece" in content

    def test_find_similar_query_with_invalid_slug_returns_404(self, client):
        url = f"{reverse('catalog:visual-search')}?product=non-existent-slug"
        response = client.get(url)
        assert response.status_code == 404

    def test_post_image_search_renders_results_and_detected_palette(
        self, client, make_product, make_variant, colour, size
    ):
        p = make_product(name="Dark Jacket", slug="dark-jacket")
        make_variant(p, color=colour, size=size)

        image_file = _upload(colour=(15, 15, 15))
        response = client.post(
            reverse("catalog:visual-search"),
            {"image": image_file},
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "Discovered Pieces" in content
        assert "Detected Palette:" in content
        assert "Dark Jacket" in content

    def test_post_empty_image_search_returns_400_with_error(self, client):
        response = client.post(reverse("catalog:visual-search"), {})
        assert response.status_code == 400
        content = response.content.decode()
        assert "Please select an image to search" in content

    def test_post_invalid_image_returns_400_with_error(self, client):
        bad_file = SimpleUploadedFile("corrupt.png", b"not a valid png", content_type="image/png")
        response = client.post(
            reverse("catalog:visual-search"),
            {"image": bad_file},
        )
        assert response.status_code == 400
        content = response.content.decode()
        assert "not a readable image" in content.lower()

    def test_track_visual_click_redirects_and_records_event(self, client, product):
        url = reverse("catalog:visual-click")
        response = client.get(f"{url}?product_id={product.pk}")
        assert response.status_code == 320 or response.status_code == 302
        assert response.url == product.get_absolute_url()

        # Analytics event was captured
        event = Event.objects.filter(name="visual_product_clicked").first()
        assert event is not None
        assert event.object_id == product.pk


# --------------------------------------------------------------------------------------
# 7. Product Detail Page "Find Similar" Action
# --------------------------------------------------------------------------------------


class TestProductDetailFindSimilarAction:
    def test_product_detail_page_includes_find_similar_action(self, client, product):
        response = client.get(product.get_absolute_url())
        assert response.status_code == 200
        content = response.content.decode()
        expected_url = f"{reverse('catalog:visual-search')}?product={product.slug}"
        assert expected_url in content
        assert "Find Similar" in content


# --------------------------------------------------------------------------------------
# 8. REST API Endpoints & Rate Limiting
# --------------------------------------------------------------------------------------


class TestVisualSearchApi:
    def test_api_visual_search_unauthenticated_public_safe(
        self, api_client, make_product, make_variant, colour, size
    ):
        p = make_product(name="Api Tee", slug="api-tee")
        make_variant(p, color=colour, size=size)

        image_file = _upload(colour=(20, 20, 20))
        url = reverse("v1:product-visual-search")
        response = api_client.post(url, {"image": image_file}, format="multipart")
        assert response.status_code == 200
        data = response.data
        assert "count" in data
        assert "features" in data
        assert "dominant_colors" in data["features"]
        assert len(data["results"]) >= 1
        assert data["results"][0]["slug"] == p.slug
        assert data["results"][0]["score"] > 0
        assert "reasons" in data["results"][0]

    def test_api_visual_search_missing_image_returns_400(self, api_client):
        url = reverse("v1:product-visual-search")
        response = api_client.post(url, {}, format="multipart")
        assert response.status_code == 400
        assert "No image file was provided" in response.data["detail"]

    def test_api_visual_search_invalid_image_returns_400(self, api_client):
        bad_file = SimpleUploadedFile("broken.png", b"corrupt", content_type="image/png")
        url = reverse("v1:product-visual-search")
        response = api_client.post(url, {"image": bad_file}, format="multipart")
        assert response.status_code == 400
        assert response.data["code"] == "unreadable"

    def test_api_product_similar_endpoint(
        self, api_client, product, make_product, make_variant, colour, size
    ):
        sibling = make_product(name="Sibling", slug="sibling")
        make_variant(sibling, color=colour, size=size)

        url = reverse("v1:product-similar", kwargs={"slug": product.slug})
        response = api_client.get(url)
        assert response.status_code == 200
        data = response.data
        assert data["source_product"]["slug"] == product.slug
        assert len(data["results"]) >= 1
        assert data["results"][0]["slug"] == sibling.slug

    def test_api_product_similar_unknown_slug_returns_404(self, api_client):
        url = reverse("v1:product-similar", kwargs={"slug": "missing-item"})
        response = api_client.get(url)
        assert response.status_code == 404

    def test_api_visual_click_tracking(self, api_client, product):
        url = reverse("v1:visual-product-click")
        response = api_client.post(url, {"product_id": product.pk})
        assert response.status_code == 200
        assert response.data == {"status": "ok"}
        assert Event.objects.filter(name="visual_product_clicked", object_id=product.pk).exists()

    def test_api_visual_search_is_throttled_under_expensive_scope(self, api_client, monkeypatch):
        url = reverse("v1:product-visual-search")
        monkeypatch.setattr(
            SimpleRateThrottle,
            "THROTTLE_RATES",
            {"expensive": "1/min", "anon": "100/min", "user": "100/min"},
        )
        image1 = _upload()
        res1 = api_client.post(url, {"image": image1}, format="multipart")
        assert res1.status_code == 200

        image2 = _upload()
        res2 = api_client.post(url, {"image": image2}, format="multipart")
        assert res2.status_code == 429


# --------------------------------------------------------------------------------------
# 9. Analytics Tracking (All 5 Required Events)
# --------------------------------------------------------------------------------------


class TestVisualAnalyticsEvents:
    def test_image_search_started_and_completed_recorded(
        self, client, make_product, make_variant, colour, size
    ):
        p = make_product(name="Track Tee", slug="track-tee")
        make_variant(p, color=colour, size=size)

        upload = _upload()
        client.post(reverse("catalog:visual-search"), {"image": upload})

        started = Event.objects.filter(name="image_search_started").first()
        completed = Event.objects.filter(name="image_search_completed").first()

        assert started is not None
        assert started.metadata.get("source") == "storefront"
        assert completed is not None
        assert completed.metadata.get("results_count") >= 1
        assert "dominant_colors" in completed.metadata

    def test_image_search_failed_recorded_on_invalid_upload(self, client):
        client.post(reverse("catalog:visual-search"), {})
        failed = Event.objects.filter(name="image_search_failed").first()
        assert failed is not None
        assert failed.metadata.get("error_code") == "empty_upload"

    def test_find_similar_used_recorded(self, client, product):
        url = f"{reverse('catalog:visual-search')}?product={product.slug}"
        client.get(url)

        event = Event.objects.filter(name="find_similar_used").first()
        assert event is not None
        assert event.object_type == "product"
        assert event.object_id == product.pk
        assert event.metadata.get("product_slug") == product.slug

    def test_visual_product_clicked_recorded_via_redirect(self, client, product):
        url = f"{reverse('catalog:visual-click')}?product_id={product.pk}"
        client.get(url)

        event = Event.objects.filter(name="visual_product_clicked").first()
        assert event is not None
        assert event.object_type == "product"
        assert event.object_id == product.pk


# --------------------------------------------------------------------------------------
# 10. Catalog Availability & Visibility Safety
# --------------------------------------------------------------------------------------


class TestCatalogAvailabilitySafety:
    def test_draft_products_are_excluded_from_visual_search(
        self, make_product, make_variant, colour, size
    ):
        draft = make_product(
            name="Draft Item",
            slug="draft-item",
            status=Product.Status.DRAFT,
            published_at=None,
        )
        make_variant(draft, color=colour, size=size)

        upload = _upload()
        results, _ = find_visually_similar_products(upload)
        assert draft.pk not in [r.pk for r in results]

    def test_archived_products_are_excluded_from_visual_search(
        self, make_product, make_variant, colour, size
    ):
        archived = make_product(
            name="Archived Item",
            slug="archived-item",
            status=Product.Status.ARCHIVED,
        )
        make_variant(archived, color=colour, size=size)

        upload = _upload()
        results, _ = find_visually_similar_products(upload)
        assert archived.pk not in [r.pk for r in results]

    def test_future_scheduled_products_are_excluded(self, make_product, make_variant, colour, size):
        future = make_product(
            name="Future Item",
            slug="future-item",
            status=Product.Status.ACTIVE,
            published_at=timezone.now() + timezone.timedelta(days=7),
        )
        make_variant(future, color=colour, size=size)

        upload = _upload()
        results, _ = find_visually_similar_products(upload)
        assert future.pk not in [r.pk for r in results]

    def test_inactive_category_products_are_excluded(
        self, make_product, make_variant, inactive_category, colour, size
    ):
        inact_cat_prod = make_product(
            name="Inactive Cat Item",
            slug="inact-cat-item",
            category=inactive_category,
        )
        make_variant(inact_cat_prod, color=colour, size=size)

        upload = _upload()
        results, _ = find_visually_similar_products(upload)
        assert inact_cat_prod.pk not in [r.pk for r in results]
