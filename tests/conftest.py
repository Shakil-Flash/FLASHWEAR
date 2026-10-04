import os

import pytest
from django.core.management import call_command


@pytest.fixture(autouse=True)
def _clear_cache():
    """Prevent cache state leaking between tests (the singleton config is cached)."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def site_configuration(db):
    """Return the singleton SiteConfiguration, created on demand."""
    from apps.core.models import SiteConfiguration

    return SiteConfiguration.ensure()


@pytest.fixture
def user(db):
    """Create a plain active user."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email="shopper@flashwear.test",
        password="Str0ng-Passw0rd!",
    )


@pytest.fixture
def verified_user(db):
    """Create a user whose address is already proven."""
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    return get_user_model().objects.create_user(
        email="verified@flashwear.test",
        password="Str0ng-Passw0rd!",
        email_verified_at=timezone.now(),
    )


@pytest.fixture
def other_user(db):
    """A second customer, for the tests that must prove ownership is enforced."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email="someone-else@flashwear.test",
        password="Str0ng-Passw0rd!",
    )


@pytest.fixture
def address(db, user):
    """A saved address belonging to ``user``."""
    from apps.accounts.models import Address

    return Address.objects.create(
        user=user,
        full_name="Ada Lovelace",
        phone="+44 7700 900000",
        line1="1 Example Way",
        line2="Apartment 3",
        city="London",
        region="Greater London",
        postal_code="SW1A 1AA",
        country="GB",
        address_type="both",
        is_default_shipping=True,
    )


@pytest.fixture
def admin_user(db):
    """Create a superuser for admin-facing tests."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_superuser(
        email="root@flashwear.test",
        password="Sup3r-S3cret!",
    )


@pytest.fixture
def api_client():
    """Return a DRF API client."""
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def runner():
    """Expose the management command runner to tests that need it."""

    def _run(command: str, *args: str, **kwargs):
        return call_command(command, *args, **kwargs)

    return _run


def pytest_report_header(config):
    """Show which settings module pytest is exercising."""
    return f"DJANGO_SETTINGS_MODULE: {os.environ.get('DJANGO_SETTINGS_MODULE', 'unset')}"
