"""Tests for the REST API foundation (``/api/v1/``)."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestApiHealth:
    """``GET /api/v1/health/`` returns JSON."""

    def test_returns_ok(self, client):
        response = client.get("/api/v1/health/")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_content_type_is_json(self, client):
        response = client.get("/api/v1/health/")
        assert response["Content-Type"].startswith("application/json")

    def test_reverse(self):
        assert reverse("v1:health") == "/api/v1/health/"

    def test_does_not_leak_internals(self, client):
        payload = client.get("/api/v1/health/").json()
        assert set(payload) == {"status"}
        assert "SECRET_KEY" not in str(payload)
        assert "DATABASE" not in str(payload).upper()

    def test_rejects_non_get(self, api_client):
        assert api_client.post("/api/v1/health/").status_code == 405


class TestApiRoot:
    """``GET /api/v1/`` lists the available endpoints."""

    def test_returns_index(self, api_client):
        response = api_client.get("/api/v1/")
        assert response.status_code == 200
        assert response.data["service"] == "flashwear"
        assert response.data["version"] == "v1"

    def test_lists_health_endpoint(self, api_client):
        response = api_client.get("/api/v1/")
        assert "health" in response.data["endpoints"]

    def test_is_public(self, api_client):
        assert api_client.get("/api/v1/").status_code == 200

    def test_reverse(self):
        assert reverse("v1:root") == "/api/v1/"

    def test_unknown_endpoint_returns_404(self, api_client):
        """An endpoint that does not exist must 404 rather than resolve to something else.

        ``/api/v1/catalog/`` was chosen deliberately: ``products/`` became a real endpoint in
        Phase 3, so using it here would have turned this guard into a false failure.
        """
        assert api_client.get("/api/v1/catalog/").status_code == 404

    def test_catalog_endpoints_are_advertised(self, api_client):
        endpoints = api_client.get("/api/v1/").data["endpoints"]
        for name in ("products", "categories", "collections", "brands"):
            assert name in endpoints

    def test_health_url_is_absolute(self, api_client):
        response = api_client.get("/api/v1/")
        assert response.data["endpoints"]["health"].endswith("/api/v1/health/")

    def test_does_not_require_authentication(self, api_client):
        """Public endpoints must not challenge anonymous callers."""
        api_client.credentials()
        assert (
            api_client.get("/api/v1/health/", HTTP_AUTHORIZATION="Bearer invalid").status_code
            == 200
        )
        assert api_client.get("/api/v1/", HTTP_AUTHORIZATION="Bearer invalid").status_code == 200

    def test_api_root_is_not_cached(self, api_client):
        """Versioned indexes must not be served from a stale cache."""
        response = api_client.get("/api/v1/")
        assert "no-store" in response.headers.get("Cache-Control", "")

    def test_api_health_is_not_cached(self, api_client):
        """A cached probe would report healthy during an outage."""
        response = api_client.get("/api/v1/health/")
        assert "no-store" in response.headers.get("Cache-Control", "")


class TestApiVersioning:
    """The namespace is the versioning contract for API clients."""

    def test_root_list_only_declares_v1(self, api_client):
        payload = api_client.get("/api/v1/").data
        assert payload["version"] == "v1"
        assert payload["service"] == "flashwear"

    def test_unknown_version_returns_404(self, client):
        assert client.get("/api/v2/health/").status_code == 404

    def test_namespace_is_v1(self):
        from config.api.v1 import urls

        assert urls.app_name == "v1"
