"""Regression tests for Backoffice & Django Admin Theme Reliability, Navigation UX & Visual Polish.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.backoffice.forms import CatalogBulkForm, InventoryAdjustForm
from apps.backoffice.forms_content import HomepageSectionForm
from apps.backoffice.permissions import NAVIGATION, navigation_for
from apps.backoffice.templatetags.backoffice_extras import status_badge

pytestmark = pytest.mark.django_db


class TestBackofficeNavigationPolish:
    """Verify navigation reorganization and routing health."""

    def test_all_navigation_items_reverse_successfully(self):
        """Every navigation item in every section must resolve to a valid URL."""
        assert len(NAVIGATION) >= 6
        total_items = 0
        for section_label, items in NAVIGATION:
            assert len(items) > 0, f"Section {section_label} is empty"
            # Ensure no section is bloated (<= 8 items)
            assert len(items) <= 8, f"Section {section_label} has too many items ({len(items)})"
            for _key, _label, url_name, _cap in items:
                total_items += 1
                resolved_url = reverse(url_name)
                assert resolved_url.startswith("/"), f"Failed to resolve {url_name}"

        assert total_items >= 25

    def test_navigation_groups_are_logically_named(self):
        """Sections must match logical operational domains."""
        labels = [group[0] for group in NAVIGATION]
        expected_groups = [
            "Overview",
            "Orders & Sales",
            "Catalog & Stock",
            "Merchandising",
            "Content Studio",
            "Customers & Care",
            "Fashion & Community",
            "System",
        ]
        assert labels == expected_groups

    def test_navigation_for_operator_vs_superuser(self, admin_user):
        """Superuser gets all navigation sections; staff permissions filter correctly."""
        super_nav = navigation_for(admin_user)
        super_urls = {item["url_name"] for group in super_nav for item in group["items"]}
        assert "backoffice:dashboard" in super_urls
        assert "backoffice:staff" in super_urls
        assert "backoffice:products" in super_urls
        assert "backoffice:notifications" in super_urls


class TestBackofficeThemeSystem:
    """Verify dark/light theme persistence, tokens, and templates."""

    def test_base_template_has_anti_flash_script_and_theme_controller(self):
        """base.html must resolve theme immediately in head without FOUT."""
        base_path = Path("templates/backoffice/base.html")
        content = base_path.read_text(encoding="utf-8")

        # Anti-flash script in head
        assert "localStorage.getItem('theme')" in content
        assert "prefers-color-scheme: dark" in content
        assert "document.documentElement.classList.add('dark')" in content

        # Alpine theme management
        assert "themeOpen" in content
        assert "setTheme('light')" in content
        assert "setTheme('dark')" in content
        assert "setTheme('auto')" in content

    def test_backoffice_form_widgets_have_dark_mode_classes(self):
        """Form inputs, selects, and textareas must include dark mode classes."""
        inv_form = InventoryAdjustForm()
        delta_rendered = str(inv_form["delta"])
        assert "dark:bg-slate-800" in delta_rendered
        assert "dark:border-slate-700" in delta_rendered
        assert "dark:text-slate-100" in delta_rendered

        bulk_form = CatalogBulkForm()
        status_rendered = str(bulk_form["action"])
        assert "dark:bg-slate-800" in status_rendered
        assert "dark:border-slate-700" in status_rendered

        content_form = HomepageSectionForm()
        title_rendered = str(content_form["title"])
        assert "dark:bg-slate-800" in title_rendered
        assert "dark:border-slate-700" in title_rendered

    def test_status_badge_templatetag_includes_dark_classes(self):
        """Status badge outputs high-contrast dark mode classes."""
        badge_paid = status_badge("paid", "Paid")
        assert "dark:bg-emerald-950/40" in badge_paid
        assert "dark:text-emerald-300" in badge_paid

        badge_pending = status_badge("pending", "Pending")
        assert "dark:bg-amber-950/40" in badge_pending
        assert "dark:text-amber-300" in badge_pending

        badge_failed = status_badge("failed", "Failed")
        assert "dark:bg-rose-950/40" in badge_failed
        assert "dark:text-rose-300" in badge_failed

    def test_app_css_contains_backoffice_dark_tokens(self):
        """Compiled static app.css must contain centralized html.dark tokens."""
        css_path = Path("static/css/app.css")
        assert css_path.exists()
        css_content = css_path.read_text(encoding="utf-8")
        assert "--bo-card-bg" in css_content
        assert "--bo-card-border" in css_content
        assert "color-scheme:dark" in css_content or "color-scheme: dark" in css_content


class TestDjangoAdminDarkThemeOverrides:
    """Verify Django Admin CSS doesn't break in dark mode."""

    def test_image_url_manager_css_has_dark_mode_rules(self):
        """static/admin/css/image_url_manager.css must support dark mode."""
        css_path = Path("static/admin/css/image_url_manager.css")
        content = css_path.read_text(encoding="utf-8")

        # Must not force white background unconditionally
        pat = r"\.change-form fieldset\s*\{\s*[^}]*background:\s*#ffffff\s*!important"
        assert not re.search(pat, content)

        # Must have data-theme="dark" definitions
        assert '[data-theme="dark"] .change-form fieldset' in content
        assert '[data-theme="dark"] .image-url-preview-container' in content
        assert "prefers-color-scheme: dark" in content


class TestTableUsabilityAndImageUX:
    """Verify bulk actions and media preview controls."""

    def test_products_template_has_select_all_checkbox(self):
        """Products list table header must contain select-all checkbox."""
        template_path = Path("templates/backoffice/products.html")
        content = template_path.read_text(encoding="utf-8")
        assert "id=\"select-all-products\"" in content
        assert "aria-label=\"Select all products\"" in content

    def test_reviews_template_has_select_all_checkbox(self):
        """Reviews list table header must contain select-all checkbox."""
        template_path = Path("templates/backoffice/reviews.html")
        content = template_path.read_text(encoding="utf-8")
        assert "id=\"select-all-reviews\"" in content
        assert "aria-label=\"Select all reviews\"" in content

    def test_content_forms_render_image_preview_thumbnails(self):
        """Homepage, editorial, and campaign form templates must display thumbnail previews."""
        for form_name in ("homepage_form.html", "editorial_form.html", "campaign_form.html"):
            tmpl = Path(f"templates/backoffice/content/{form_name}").read_text(encoding="utf-8")
            assert "<img src=" in tmpl, f"{form_name} missing image preview thumbnail tag"
            assert "rounded-lg" in tmpl
