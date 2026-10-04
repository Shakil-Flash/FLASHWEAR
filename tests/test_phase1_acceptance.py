"""Smoke tests for the Phase 1 acceptance criteria.

These mirror the checklist that must stay green before any later phase starts.
They intentionally overlap the focused suites; redundancy here is the point.
"""

import pytest
from django.conf import settings
from django.urls import reverse

# The homepage renders the site-configuration context processor, which reads the DB.
pytestmark = pytest.mark.django_db


class TestDjangoConfigurationLoads:
    """1. Django configuration loads."""

    def test_settings_are_loaded(self):
        assert settings.configured is True
        assert settings.ROOT_URLCONF == "config.urls"

    def test_debug_off_in_test_run(self):
        assert settings.DEBUG is False

    def test_auth_user_model_configured_before_dependent_models(self):
        assert settings.AUTH_USER_MODEL == "accounts.User"

    def test_custom_user_model_is_swapped_in(self):
        from django.contrib.auth import get_user_model

        assert get_user_model()._meta.label_lower == "accounts.user"


class TestCustomUserModelWorks:
    """2. Custom User model works."""

    def test_create_and_authenticate_user(self, user):
        from django.contrib.auth import authenticate

        assert user.pk is not None
        assert authenticate(username=user.email, password="Str0ng-Passw0rd!") == user


class TestEmailIsUnique:
    """3. Email is unique."""

    def test_duplicate_email_rejected(self, db):
        import pytest
        from django.contrib.auth import get_user_model
        from django.db import IntegrityError

        User = get_user_model()
        User.objects.create_user(email="uniq@flashwear.test", password="Str0ng-Passw0rd!")
        with pytest.raises(IntegrityError):
            User.objects.create_user(email="uniq@flashwear.test", password="Other-Passw0rd!")

    def test_email_field_declared_unique(self):
        from django.contrib.auth import get_user_model

        field = get_user_model()._meta.get_field("email")
        assert field.unique is True


class TestHealthEndpointReturnsSuccess:
    """4. Health endpoint returns success."""

    def test_health_ok(self, client):
        response = client.get(reverse("core:health"))
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestApiHealthEndpointReturnsJson:
    """5. API health endpoint returns JSON success."""

    def test_api_health_ok(self, api_client):
        response = api_client.get(reverse("v1:health"))
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert response["Content-Type"].startswith("application/json")


class TestHomepageLoadsSuccessfully:
    """6. Homepage loads successfully."""

    def test_homepage_ok(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert b"FLASHWEAR" in response.content
        assert b"Fashion Beyond Ordinary" in response.content
