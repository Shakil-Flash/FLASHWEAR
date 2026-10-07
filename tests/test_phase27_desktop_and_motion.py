
import pytest
from django.conf import settings
from django.urls import reverse

from apps.catalog.models import ProductImage


@pytest.mark.django_db
class TestDesktopHeroRendering:
    def test_hero_renders_with_real_product_image_when_available(self, client, make_product):
        # Create a featured product with an image
        product = make_product(name="Architectural Wool Coat", is_featured=True)
        ProductImage.objects.create(
            product=product,
            image="catalog/tests/coat.png",
            alt_text="Wool coat front",
            position=0,
        )

        response = client.get(reverse("core:home"))
        assert response.status_code == 200
        content = response.content.decode()

        # Hero content is visible and animated
        assert "Wear what feels like you." in content
        assert "Explore the Drop" in content
        assert "View Collections" in content
        assert "hero-anim-title" in content
        assert "hero-anim-visual" in content
        assert "hero-anim-cta" in content
        assert "Architectural Wool Coat" in content
        assert "catalog/tests/coat.png" in content

    def test_hero_renders_graceful_signature_fallback_when_empty(self, client, db):
        response = client.get(reverse("core:home"))
        assert response.status_code == 200
        content = response.content.decode()

        assert "Wear what feels like you." in content
        assert "Signature Series" in content
        assert "Fashion Beyond Ordinary" in content


@pytest.mark.django_db
class TestDesktopNavigationHierarchy:
    def test_desktop_navigation_prioritizes_key_destinations(self, client):
        response = client.get(reverse("core:home"))
        assert response.status_code == 200
        content = response.content.decode()

        # Primary desktop navigation items
        assert "Home" in content
        assert "Shop" in content
        assert "Collections" in content
        assert "Styling" in content
        assert "Discover" in content

        # Secondary items inside Discover menu
        assert "Categories" in content
        assert "Loop Resale" in content
        assert "Search by Image" in content

        # Utility actions
        assert "Bag" in content
        assert "Wishlist" in content
        assert "Search" in content


class TestFailSafeMotionDesign:
    def test_css_fail_safe_defaults_reveal_to_visible(self):
        css_path = settings.BASE_DIR / "frontend" / "css" / "tailwind.css"
        css = css_path.read_text(encoding="utf-8")

        # By default, .reveal must have opacity: 1 so JS failures never leave pages blank
        assert ".reveal {\n        opacity: 1;" in css or ".reveal {\n    opacity: 1;" in css

        # Only armed when js-reveal-active is added to html
        assert "html.js-reveal-active .reveal" in css

        # Coordinated hero load animation keyframes exist
        assert "@keyframes heroFadeSlideUp" in css
        assert "@keyframes heroScaleFadeIn" in css
        assert ".hero-anim-title" in css
        assert ".hero-anim-visual" in css

    def test_compiled_css_includes_hero_and_reveal_tokens(self):
        css_file = settings.BASE_DIR / "static" / "css" / "app.css"
        compiled_css = css_file.read_text(encoding="utf-8")
        assert ".hero-anim-title" in compiled_css
        assert ".hero-anim-visual" in compiled_css
        assert ".reveal" in compiled_css
        assert ".hover-lift" in compiled_css

    def test_reduced_motion_disables_hero_and_scroll_animations(self):
        css_path = settings.BASE_DIR / "frontend" / "css" / "tailwind.css"
        css = css_path.read_text(encoding="utf-8")

        assert "prefers-reduced-motion: reduce" in css
        assert ".hero-anim-title" in css
        assert "animation: none !important" in css


@pytest.mark.django_db
class TestHorizontalOverflowPrevention:
    def test_body_has_overflow_x_hidden(self, client):
        response = client.get(reverse("core:home"))
        assert response.status_code == 200
        content = response.content.decode()

        assert 'overflow-x-hidden' in content

    def test_new_arrivals_container_has_responsive_scroll_safety(self, client, make_product):
        make_product(name="Arrival Alpha", is_new=True)
        make_product(name="Arrival Beta", is_new=True)

        response = client.get(reverse("core:home"))
        assert response.status_code == 200
        content = response.content.decode()

        # Contains responsive snap rail on mobile and grid on desktop
        assert "overflow-x-auto" in content
        assert "sm:grid" in content
        assert "sm:overflow-visible" in content
