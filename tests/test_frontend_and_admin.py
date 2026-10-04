"""Tests for the design-system components, template tags and admin wiring."""

import pytest
from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.text import slugify

pytestmark = pytest.mark.django_db


class TestTemplateTags:
    """Global template tag library."""

    def test_space_to_dash(self):
        from apps.core.templatetags.flashwear import space_to_dash

        assert space_to_dash("New In") == "new-in"
        assert space_to_dash("  FLASH  Drop  ") == "flash-drop"
        assert space_to_dash("") == ""
        assert space_to_dash(None) == ""

    def test_space_to_dash_strips_punctuation(self):
        from apps.core.templatetags.flashwear import space_to_dash

        assert space_to_dash("Drop #1: Limited!") == "drop-1-limited"

    def test_currency_formats_decimals(self):
        from apps.core.templatetags.flashwear import currency

        assert currency("129.5") == "USD 129.50"
        assert currency(99) == "USD 99.00"
        assert currency(None) == "USD 0.00"

    def test_currency_respects_code(self):
        from apps.core.templatetags.flashwear import currency

        assert currency("10", "EUR") == "EUR 10.00"

    def test_version_tag(self):
        from apps.core.templatetags.flashwear import flashwear_version

        assert flashwear_version() == "0.1.0"

    def test_active_url_matches_current_path(self, rf):
        from apps.core.templatetags.flashwear import active_url

        request = rf.get("/")
        assert active_url(request, "core:home") == "active"

    def test_active_url_is_empty_for_other_views(self, rf):
        from apps.core.templatetags.flashwear import active_url

        request = rf.get("/")
        assert active_url(request, "core:health") == ""

    def test_active_url_handles_unknown_view(self, rf):
        from apps.core.templatetags.flashwear import active_url

        assert active_url(rf.get("/"), "does.not:exist") == ""

    def test_active_url_is_empty_without_a_request(self):
        """Templates must not explode when rendering outside a request cycle."""
        from apps.core.templatetags.flashwear import active_url

        assert active_url(None, "core:home") == ""

    def test_currency_handles_non_numeric_input(self):
        from apps.core.templatetags.flashwear import currency

        assert currency("not-a-number") == "USD 0.00"
        assert currency("not-a-number", "GBP") == "GBP 0.00"
        assert currency([1, 2]) == "USD 0.00"


class TestContextProcessor:
    """Site configuration is exposed to every template."""

    def test_exposes_branding_keys(self, client):
        response = client.get(reverse("core:home"))
        assert response.context["site_name"] == "FLASHWEAR"
        assert response.context["seo_description"]
        assert response.context["support_email"]

    def test_reflects_admin_edits(self, client, site_configuration):
        site_configuration.announcement = "Free shipping this week"
        site_configuration.save()
        response = client.get(reverse("core:home"))
        assert b"Free shipping this week" in response.content

    def test_maintenance_mode_blocks_storefront(self, client, site_configuration):
        site_configuration.maintenance_mode = True
        site_configuration.save()
        response = client.get(reverse("core:home"))
        assert response.status_code == 503

    def test_staff_bypass_maintenance_mode(self, client, admin_user, site_configuration):
        site_configuration.maintenance_mode = True
        site_configuration.save()
        client.force_login(admin_user)
        assert client.get(reverse("core:home")).status_code == 200


class TestAdmin:
    """Django admin wiring for the Phase 1 models."""

    def test_user_registered(self):
        assert get_user_model() in django_admin.site._registry

    def test_site_configuration_registered(self):
        from apps.core.models import SiteConfiguration

        assert SiteConfiguration in django_admin.site._registry

    def test_site_configuration_admin_blocks_add_when_present(self, site_configuration):
        from apps.core.admin import SiteConfigurationAdmin
        from apps.core.models import SiteConfiguration

        admin_obj = SiteConfigurationAdmin(SiteConfiguration, django_admin.site)
        assert admin_obj.has_add_permission(None) is False

    def test_site_configuration_admin_blocks_delete(self, site_configuration):
        from apps.core.admin import SiteConfigurationAdmin
        from apps.core.models import SiteConfiguration

        admin_obj = SiteConfigurationAdmin(SiteConfiguration, django_admin.site)
        assert admin_obj.has_delete_permission(None) is False

    def test_admin_index_loads_for_superuser(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get(reverse("admin:index"))
        assert response.status_code == 200

    def test_admin_login_page_renders(self, client):
        response = client.get(reverse("admin:login"))
        assert response.status_code == 200

    def test_admin_requires_authentication(self, client):
        response = client.get(reverse("admin:index"))
        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_non_staff_cannot_enter_admin(self, client, user):
        client.force_login(user)
        response = client.get(reverse("admin:index"))
        assert response.status_code in {302, 403}


class TestStaticAndMedia:
    """Static/media configuration behaves as expected in the test environment."""

    def test_static_asset_is_findable(self):
        from django.contrib.staticfiles import finders

        assert finders.find("css/app.css") is not None

    def test_core_static_directory_exists(self):
        from django.conf import settings

        assert (settings.BASE_DIR / "static").is_dir()
        assert (settings.BASE_DIR / "media").is_dir()

    def test_media_in_memory_storage(self):
        from django.core.files.storage import default_storage

        assert default_storage.__class__.__name__ == "InMemoryStorage"


class TestDesignSystemComponents:
    """The reusable components referenced by the base layout."""

    @pytest.mark.parametrize(
        "template_name",
        [
            "base.html",
            "components/navbar.html",
            "components/footer.html",
            "components/messages.html",
            "pages/home.html",
            "pages/maintenance.html",
            "pages/errors/404.html",
            "pages/errors/500.html",
        ],
    )
    def test_template_exists_and_loads(self, template_name):
        from django.template.loader import get_template

        assert get_template(template_name) is not None

    def test_uses_container_width_convention(self, client):
        content = client.get(reverse("core:home")).content
        assert b"max-w-7xl" in content

    def test_uses_responsive_utilities(self, client):
        content = client.get(reverse("core:home")).content
        for breakpoint in (b"sm:", b"md:", b"lg:"):
            assert breakpoint in content

    def test_slugify_helper_matches_template_filter(self):
        assert slugify("New In") == "new-in"


class TestStaticAssetPipeline:
    """The Tailwind build inputs and outputs must stay in sync."""

    def test_every_template_utility_class_is_in_the_compiled_css(self):
        """A class used in markup but absent from app.css means a stale build.

        Checks content rather than timestamps: the Tailwind CLI skips rewriting an
        unchanged file, so mtimes are not a reliable signal. A representative sample of
        layout and design-token classes is enough to catch a missing rebuild.
        """
        css = (settings.BASE_DIR / "static" / "css" / "app.css").read_text(encoding="utf-8")
        for token in (
            ".bg-flash-600",
            ".shadow-soft",
            ".shadow-elevated",
            ".text-flash-700",
            ".max-w-7xl",
            ".skip-link",
        ):
            assert token in css, f"{token} is missing; run `npm run build:css`"

    def test_vendor_assets_are_minified_bundles(self):
        """Vendored libraries must be the minified builds, not the readable ones."""
        vendor = settings.BASE_DIR / "static" / "vendor"
        for name in ("htmx.min.js", "alpine.min.js"):
            content = (vendor / name).read_text(encoding="utf-8")
            assert len(content) > 10_000, name
            # Minified bundles are very long lines; unminified sources have many.
            lines = content.splitlines()
            assert len(lines) < 50, f"{name} has {len(lines)} lines: not minified"

    def test_node_modules_are_not_required_at_runtime(self):
        """The committed stylesheet means Django never shells out to npm."""
        assert (settings.BASE_DIR / "static" / "css" / "app.css").stat().st_size > 5_000


class TestAccessibilityBasics:
    """Lightweight a11y guarantees on the base layout."""

    def test_html_lang_attribute(self, client):
        content = client.get(reverse("core:home")).content
        assert b'<html lang="en"' in content

    def test_nav_has_aria_label(self, client):
        content = client.get(reverse("core:home")).content
        assert b'aria-label="Primary navigation"' in content

    def test_mobile_toggle_has_accessible_name(self, client):
        content = client.get(reverse("core:home")).content
        assert b"Toggle navigation" in content

    def test_decorative_icons_are_hidden_from_screen_readers(self, client):
        content = client.get(reverse("core:home")).content
        assert b"sr-only" in content


class TestErrorHandlers:
    """Custom error pages wired through ``handler404`` / ``handler500``."""

    def test_404_returns_branded_page(self, client):
        response = client.get("/no-such-page/")
        assert response.status_code == 404
        assert b"FLASHWEAR" in response.content

    def test_404_does_not_leak_debug(self, client):
        response = client.get("/no-such-page/")
        assert b"Traceback" not in response.content


class TestSelfHostedAssets:
    """Front-end assets are served from this origin; production CSP is ``'self'`` only."""

    THIRD_PARTY = (
        "cdn.tailwindcss.com",
        "unpkg.com",
        "cdn.jsdelivr.net",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "rsms.me",
    )

    def test_no_third_party_asset_hosts_in_any_template(self):
        for template in (settings.BASE_DIR / "templates").rglob("*.html"):
            source = template.read_text(encoding="utf-8")
            for host in self.THIRD_PARTY:
                assert host not in source, f"{template.name} references {host}"

    def test_rendered_page_has_no_third_party_asset_hosts(self, client):
        content = client.get(reverse("core:home")).content.decode()
        for host in self.THIRD_PARTY:
            assert host not in content

    def test_vendor_assets_exist_on_disk(self):
        vendor = settings.BASE_DIR / "static" / "vendor"
        assert (vendor / "htmx.min.js").is_file()
        assert (vendor / "alpine.min.js").is_file()

    def test_compiled_css_contains_design_tokens(self):
        css = (settings.BASE_DIR / "static" / "css" / "app.css").read_text(encoding="utf-8")
        for token in (".bg-flash-600", ".shadow-soft", ".text-flash-700", ".skip-link"):
            assert token in css, token

    def test_production_csp_needs_no_third_party_sources(self):
        source = (settings.BASE_DIR / "config" / "settings" / "production.py").read_text(
            encoding="utf-8"
        )
        for host in self.THIRD_PARTY:
            assert host not in source

    def test_skip_link_is_present(self, client):
        assert b"Skip to content" in client.get(reverse("core:home")).content

    def test_tailwind_build_inputs_are_tracked(self):
        assert (settings.BASE_DIR / "frontend" / "css" / "tailwind.css").is_file()
        assert (settings.BASE_DIR / "tailwind.config.js").is_file()
        assert (settings.BASE_DIR / "package.json").is_file()

    def test_static_dirs_are_inside_base_dir(self):
        """Every asset served to the browser must be a collectstatic output."""
        for path in settings.STATICFILES_DIRS:
            assert str(path).startswith(str(settings.BASE_DIR))

    def test_static_root_is_inside_base_dir(self):
        assert str(settings.STATIC_ROOT).startswith(str(settings.BASE_DIR))
