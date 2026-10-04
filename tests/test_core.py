"""Tests for storefront pages, health checks and error handlers."""

import pytest
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestHealthEndpoint:
    """``GET /health/`` must return a minimal JSON payload."""

    def test_returns_ok(self, client):
        response = client.get(reverse("core:health"))
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_content_type_is_json(self, client):
        response = client.get("/health/")
        assert response["Content-Type"].startswith("application/json")

    def test_url_path(self, client):
        assert reverse("core:health") == "/health/"

    def test_rejects_non_get(self, client):
        response = client.post("/health/")
        assert response.status_code == 405


class TestHomepage:
    """``GET /`` must render the FLASHWEAR landing page."""

    def test_loads_successfully(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_shows_branding(self, client):
        response = client.get("/")
        assert b"FLASHWEAR" in response.content

    def test_shows_tagline(self, client):
        response = client.get("/")
        assert b"Fashion Beyond Ordinary" in response.content

    def test_indicates_under_development(self, client):
        response = client.get("/")
        assert b"under development" in response.content.lower()

    def test_is_responsive(self, client):
        response = client.get("/")
        assert b'name="viewport"' in response.content
        assert b"width=device-width" in response.content

    def test_has_seo_metadata(self, client):
        response = client.get("/")
        assert b"<title>" in response.content
        assert b'name="description"' in response.content

    def test_includes_static_assets(self, client):
        response = client.get("/")
        assert b"/static/css/app.css" in response.content
        assert b"/static/js/app.js" in response.content

    def test_includes_layout_components(self, client):
        response = client.get("/")
        content = response.content
        assert b"<header" in content
        assert b"<footer" in content
        assert b"<main" in content

    def test_uses_site_configuration_copy(self, client, site_configuration):
        site_configuration.tagline = "Custom Tagline In Use."
        site_configuration.save()
        response = client.get("/")
        assert b"Custom Tagline In Use." in response.content


class TestErrorHandlers:
    """Custom error pages."""

    def test_404_page(self, client):
        response = client.get("/definitely-not-a-real-page/")
        assert response.status_code == 404
        assert b"404" in response.content

    def test_500_view_renders_branded_template(self, rf):
        from apps.core.views import server_error

        response = server_error(rf.get("/boom/"))
        assert response.status_code == 500
        assert b"500" in response.content


class TestSiteConfiguration:
    """The singleton configuration model."""

    def test_ensure_is_idempotent(self, db):
        from apps.core.models import SiteConfiguration

        first = SiteConfiguration.ensure()
        second = SiteConfiguration.ensure()
        assert first.pk == second.pk == SiteConfiguration.SINGLETON_PK
        assert SiteConfiguration.objects.count() == 1

    def test_save_forces_singleton_pk(self, site_configuration):
        site_configuration.site_name = "Renamed"
        site_configuration.save()
        from apps.core.models import SiteConfiguration

        assert SiteConfiguration.objects.get().site_name == "Renamed"

    def test_delete_is_blocked(self, site_configuration):
        with pytest.raises(ValueError, match="cannot be deleted"):
            site_configuration.delete()

    def test_load_uses_cache(self, site_configuration):
        from django.core.cache import cache

        from apps.core.models import SiteConfiguration

        assert cache.get(SiteConfiguration.CACHE_KEY) is None
        assert SiteConfiguration.load() == site_configuration
        assert cache.get(SiteConfiguration.CACHE_KEY) is not None

    def test_str_returns_site_name(self, site_configuration):
        site_configuration.site_name = "Branded Name"
        site_configuration.save()
        assert str(site_configuration) == "Branded Name"

    def test_save_invalidates_cache(self, site_configuration):
        from django.core.cache import cache

        from apps.core.models import SiteConfiguration

        SiteConfiguration.load()
        assert cache.get(SiteConfiguration.CACHE_KEY) is not None
        site_configuration.tagline = "Updated tagline."
        site_configuration.save()
        assert cache.get(SiteConfiguration.CACHE_KEY) is None
        assert SiteConfiguration.load().tagline == "Updated tagline."

    def test_singleton_constraint_is_declared(self):
        from apps.core.models import SiteConfiguration

        names = {c.name for c in SiteConfiguration._meta.constraints}
        assert "core_siteconfiguration_singleton" in names

    def test_seeded_row_exists_after_migrations(self, db):
        """Migration 0002 seeds the singleton so renders never need to write."""
        from apps.core.models import SiteConfiguration

        assert SiteConfiguration.load() is not None
        assert SiteConfiguration.load().pk == SiteConfiguration.SINGLETON_PK

    def test_load_returns_none_when_row_is_missing(self, db):
        from django.core.cache import cache

        from apps.core.models import SiteConfiguration

        SiteConfiguration.objects.all()._raw_delete(SiteConfiguration.objects.db)
        cache.delete(SiteConfiguration.CACHE_KEY)
        assert SiteConfiguration.load() is None

    def test_ensure_recreates_a_deleted_row(self, db):
        from apps.core.models import SiteConfiguration

        SiteConfiguration.objects.all()._raw_delete(SiteConfiguration.objects.db)
        restored = SiteConfiguration.ensure()
        assert restored.pk == SiteConfiguration.SINGLETON_PK

    def test_context_processor_survives_database_failure(self, db, monkeypatch):
        """A database outage must degrade to defaults, not turn every page into a 500."""
        from django.db import OperationalError

        from apps.core.context_processors import get_site_configuration

        def boom(*args, **kwargs):
            raise OperationalError("connection refused")

        monkeypatch.setattr("apps.core.models.SiteConfiguration.load", boom)

        fallback = get_site_configuration()
        assert fallback.pk is None
        assert fallback.site_name == "FLASHWEAR"

    def test_health_view_survives_database_failure(self, client, monkeypatch):
        """The probe must stay up even when the database is unreachable."""
        from django.db import OperationalError

        def boom(*args, **kwargs):
            raise OperationalError("connection refused")

        monkeypatch.setattr("apps.core.models.SiteConfiguration.load", boom)
        response = client.get("/health/")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_error_page_survives_database_failure(self, client, monkeypatch):
        """The 404 handler renders the same context processor, so it must degrade too."""
        from django.db import OperationalError

        def boom(*args, **kwargs):
            raise OperationalError("connection refused")

        monkeypatch.setattr("apps.core.models.SiteConfiguration.load", boom)
        response = client.get("/no-such-page/")
        assert response.status_code == 404
        assert b"FLASHWEAR" in response.content

    def test_context_processor_never_writes_when_row_is_missing(self, db):
        """A render must not need to write; the seeded row is the only source."""
        from django.core.cache import cache

        from apps.core.context_processors import site
        from apps.core.models import SiteConfiguration

        SiteConfiguration.objects.all()._raw_delete(SiteConfiguration.objects.db)
        cache.delete(SiteConfiguration.CACHE_KEY)

        with CaptureQueriesContext(connection) as queries:
            site(RequestFactory().get("/"))

        assert not [q for q in queries.captured_queries if q["sql"].startswith("INSERT")]
