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
    def _run_production_check(settings_module: str = "config.settings.production", **env_overrides):
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings_module}
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
                "EMAIL_HOST": "smtp.example",
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
            "EMAIL_HOST": "smtp.example",
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

    # Phase 18: environment validation

    def test_production_refuses_missing_email_host_with_smtp(self):
        """SMTP without a host would fail silently at the first order confirmation."""
        result = self._run_production_check(EMAIL_HOST="")
        assert result.returncode != 0
        assert "EMAIL_HOST" in result.stderr

    def test_production_accepts_alternative_email_backend_without_smtp_host(self):
        """A transactional-mail provider over HTTP needs no EMAIL_HOST."""
        result = self._run_production_check(
            EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
            EMAIL_HOST="",
        )
        assert result.returncode == 0, result.stderr


STAGING_SNIPPET = (
    "from django.conf import settings;"
    "import django;"
    "django.setup();"
    "print('OK', settings.CSP_REPORT_ONLY, settings.SECURE_HSTS_PRELOAD,"
    " settings.SECURE_HSTS_SECONDS, settings.CACHES['default']['KEY_PREFIX'],"
    " settings.SECURE_HSTS_INCLUDE_SUBDOMAINS)"
)


class TestStagingSettings:
    """Staging must load like production, with only the documented relaxations."""

    def test_staging_loads_with_production_environment(self):
        result = TestProductionSettingsGuards._run_production_check(
            settings_module="config.settings.staging"
        )
        assert result.returncode == 0, result.stderr

    def test_staging_inherits_production_guards(self):
        """A missing webhook secret must break staging too: it rehearses production."""
        result = TestProductionSettingsGuards._run_production_check(
            settings_module="config.settings.staging",
            PAYMENT_WEBHOOK_SECRET="",
        )
        assert result.returncode != 0
        assert "PAYMENT_WEBHOOK_SECRET" in result.stderr

    @staticmethod
    def _staging_values(**env_overrides) -> list[str]:
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.staging",
            "SECRET_KEY": "a-strong-non-insecure-production-secret-key",
            "ALLOWED_HOSTS": "staging.example",
            "CSRF_TRUSTED_ORIGINS": "https://staging.example",
            "DATABASE_URL": "postgres://flashwear:flashwear@db:5432/flashwear",
            "REDIS_URL": "redis://redis:6379/0",
            "PAYMENT_PROVIDER": "stripe",
            "PAYMENT_WEBHOOK_SECRET": "a-real-provider-webhook-secret",
            "EMAIL_HOST": "smtp.example",
        }
        env.update(env_overrides)
        result = subprocess.run(
            [sys.executable, "-c", STAGING_SNIPPET],
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.split()

    def test_staging_defaults_to_report_only_csp_without_hsts_preload(self):
        values = self._staging_values()
        # print order: OK, CSP_REPORT_ONLY, HSTS_PRELOAD, HSTS_SECONDS, prefix, subdomains.
        assert values[1] == "True"  # CSP reports instead of blocking
        assert values[2] == "False"  # never preload on staging
        assert values[3] == "604800"  # 7 days, not 365
        assert values[5] == "False"  # do not extend HSTS to subdomains

    def test_staging_uses_distinct_cache_key_prefix(self):
        values = self._staging_values()
        assert "flashwear-staging" in values

    def test_staging_can_be_forced_to_production_behaviour(self):
        values = self._staging_values(
            CSP_REPORT_ONLY="False",
            SECURE_HSTS_PRELOAD="True",
            CACHE_KEY_PREFIX="flashwear-prod",
        )
        assert values[1] == "False"
        assert values[2] == "True"
        assert "flashwear-prod" in values


class TestCeleryConfiguration:
    """Beat schedule completeness (Phase 18) and the retry/result policy."""

    # Every *housekeeping* task the project defines. Event-driven tasks (send_email,
    # broadcast_batch) are deliberately absent: they fire from transaction.on_commit.
    # A new housekeeping task without a beat entry is an orphan nobody notices, so the
    # set is asserted exactly rather than by membership.
    HOUSEKEEPING_TASKS = {
        "inventory.sweep_expired_reservations",
        "engagement.sweep_loyalty",
        "quests.sweep_progress",
        "notifications.sweep_queue",
        "notifications.sweep_retention",
        "notifications.sweep_drop_events",
        "notifications.sweep_points_expiring",
        "support.close_abandoned_tickets",
        "support.remind_pending_tickets",
        "loop.expire_stale_listings",
        "loop.expire_loop_credits",
    }

    def test_every_housekeeping_task_is_scheduled(self):
        scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        assert scheduled == self.HOUSEKEEPING_TASKS

    def test_every_schedule_entry_has_a_positive_interval(self):
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            assert entry["schedule"] > 0, name

    def test_retry_policy_is_bounded_exponential(self):
        assert settings.CELERY_TASK_MAX_RETRIES >= 1
        assert settings.CELERY_TASK_RETRY_BACKOFF >= 1
        assert settings.CELERY_TASK_RETRY_BACKOFF_MAX >= settings.CELERY_TASK_DEFAULT_RETRY_DELAY

    def test_results_expire_so_the_backend_cannot_grow_without_bound(self):
        assert settings.CELERY_RESULT_EXPIRES > 0
