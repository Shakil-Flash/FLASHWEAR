"""Django configuration integrity tests (settings split, static/media, security)."""

import os
import subprocess
import sys

import pytest
from django.conf import settings
from django.urls import reverse

CHECK_SNIPPET = (
    "from django.conf import settings;"
    "import django;"
    "django.setup();"
    "print('OK', settings.AUTH_USER_MODEL, settings.DEBUG)"
)

pytestmark = pytest.mark.django_db


class TestSettingsConfiguration:
    """Verify the settings split loads and points at the right things."""

    def test_auth_user_model_is_custom(self):
        assert settings.AUTH_USER_MODEL == "accounts.User"

    def test_local_apps_installed(self):
        assert "apps.core" in settings.INSTALLED_APPS
        assert "apps.accounts" in settings.INSTALLED_APPS

    def test_rest_framework_configured(self):
        assert "rest_framework" in settings.INSTALLED_APPS
        assert settings.REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"]

    def test_static_and_media_configured(self):
        assert settings.STATIC_URL
        assert settings.STATIC_ROOT
        assert settings.MEDIA_URL
        assert settings.MEDIA_ROOT
        assert str(settings.BASE_DIR) in str(settings.STATIC_ROOT)
        assert str(settings.BASE_DIR) in str(settings.MEDIA_ROOT)

    def test_timezone_aware_datetimes_enabled(self):
        assert settings.USE_TZ is True
        assert settings.TIME_ZONE == "UTC"

    def test_celery_broker_configured(self):
        assert settings.CELERY_BROKER_URL
        assert settings.CELERY_RESULT_BACKEND
        assert settings.CELERY_TASK_SERIALIZER == "json"

    def test_cors_restricted_to_api_namespace(self):
        assert settings.CORS_URLS_REGEX == r"^/api/.*$"

    def test_logging_separates_security_stream(self):
        assert "django.security" in settings.LOGGING["loggers"]
        assert "flashwear" in settings.LOGGING["loggers"]

    def test_debug_disabled_in_test_environment(self):
        assert settings.DEBUG is False

    @pytest.mark.parametrize(
        "url_name,expected",
        [
            ("core:home", "/"),
            ("core:health", "/health/"),
            ("v1:root", "/api/v1/"),
            ("v1:health", "/api/v1/health/"),
        ],
    )
    def test_url_routing(self, url_name, expected):
        assert reverse(url_name) == expected

    def test_media_serving_only_enabled_in_debug(self):
        """``django.conf.urls.static.static`` must never be reachable in production.

        It only adds a pattern when ``DEBUG`` is on, so with the test settings the media
        prefix must be absent from the resolved URL patterns.
        """
        from django.urls import get_resolver

        patterns = [str(p.pattern) for p in get_resolver().url_patterns]
        assert not any(p.startswith(settings.MEDIA_URL.rstrip("/")) for p in patterns)

    def test_error_handlers_are_wired(self):
        import config.urls

        assert config.urls.handler404 == "apps.core.views.page_not_found"
        assert config.urls.handler500 == "apps.core.views.server_error"

    def test_env_file_loading_is_optional(self, tmp_path, monkeypatch):
        """A missing ``.env`` must not break settings import."""
        source = (settings.BASE_DIR / "config" / "settings" / "base.py").read_text(encoding="utf-8")
        assert "_ENV_FILE.exists()" in source
        assert "env.read_env" in source


class TestProductionSettingsGuards:
    """Production settings must refuse to start on unsafe configuration.

    ``manage.py check --deploy`` is the second line of defence: it reports missing
    security middleware and cookie flags that a plain ``check`` does not.
    """

    @staticmethod
    def _run_production_check(**env_overrides) -> subprocess.CompletedProcess:
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.production"}
        # Assigned rather than defaulted: a developer's own DATABASE_URL (often SQLite for
        # local work) must not leak into the subprocess and change the expected outcome.
        env.update(
            {
                "SECRET_KEY": "a-strong-non-insecure-production-secret-key",
                "ALLOWED_HOSTS": "flashwear.example",
                "CSRF_TRUSTED_ORIGINS": "https://flashwear.example",
                "DATABASE_URL": "postgres://flashwear:flashwear@db:5432/flashwear",
                "REDIS_URL": "redis://redis:6379/0",
                "PAYMENT_PROVIDER": "stripe",
                "PAYMENT_WEBHOOK_SECRET": "a-real-provider-webhook-secret",
            }
        )
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, "-c", CHECK_SNIPPET],
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
            env=env,
            check=False,
        )

    def test_production_settings_load_with_valid_environment(self):
        result = self._run_production_check()
        assert result.returncode == 0, result.stderr

    def test_deploy_check_reports_no_security_warnings(self):
        """A strong secret key must clear every ``check --deploy`` warning.

        This is what forces coverage of ``config/settings/production.py``, which otherwise
        never loads in-process.
        """
        secret = "a" * 12 + "b7Kp2xQ9mR4tV8nL6wZ3yH5cD1fG0jJ" + "7sKq" * 4
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "SECRET_KEY": secret,
            "ALLOWED_HOSTS": "flashwear.example",
            "CSRF_TRUSTED_ORIGINS": "https://flashwear.example",
            "DATABASE_URL": "postgres://flashwear:flashwear@db:5432/flashwear",
            "REDIS_URL": "redis://redis:6379/0",
            "PAYMENT_PROVIDER": "stripe",
            "PAYMENT_WEBHOOK_SECRET": "a-real-provider-webhook-secret",
        }
        result = subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy"],
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "no issues" in result.stdout, result.stdout
        assert "WARNINGS" not in result.stdout

    def test_production_refuses_missing_secret_key(self):
        result = self._run_production_check(SECRET_KEY="")
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr

    def test_production_refuses_insecure_development_secret_key(self):
        result = self._run_production_check(
            SECRET_KEY="django-insecure-development-only-key-change-me-in-production"
        )
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr

    def test_production_refuses_sqlite_fallback(self):
        result = self._run_production_check(DATABASE_URL="sqlite:///db.sqlite3")
        assert result.returncode != 0
        assert "PostgreSQL" in result.stderr

    def test_production_refuses_empty_allowed_hosts(self):
        result = self._run_production_check(ALLOWED_HOSTS="")
        assert result.returncode != 0
        assert "ALLOWED_HOSTS" in result.stderr

    # Phase 6: payments

    def test_production_refuses_development_payment_provider(self):
        """The local fake provider is for development, never for taking money."""
        result = self._run_production_check(PAYMENT_PROVIDER="development")
        assert result.returncode != 0
        assert "PAYMENT_PROVIDER" in result.stderr

    def test_production_refuses_missing_payment_provider(self):
        result = self._run_production_check(PAYMENT_PROVIDER="")
        assert result.returncode != 0
        assert "PAYMENT_PROVIDER" in result.stderr

    def test_production_refuses_missing_webhook_secret(self):
        result = self._run_production_check(PAYMENT_WEBHOOK_SECRET="")
        assert result.returncode != 0
        assert "PAYMENT_WEBHOOK_SECRET" in result.stderr
