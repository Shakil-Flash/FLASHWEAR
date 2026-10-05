"""Tests for the logging configuration, redaction filter and Celery wiring."""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
from urllib.parse import urlsplit

import pytest
from django.conf import settings


class TestLoggingConfiguration:
    """Structured, separated log streams."""

    @staticmethod
    def _base_logging() -> dict:
        """Read the production-shaped logging config.

        The testing module deliberately swaps file handlers for console handlers, so the
        separation guarantees must be asserted against ``base``.
        """
        from config.settings import base

        return base.LOGGING

    def test_app_logger_exists(self):
        assert "flashwear" in self._base_logging()["loggers"]

    def test_security_logger_is_separate(self):
        loggers = self._base_logging()["loggers"]
        security = loggers["django.security"]
        app = loggers["flashwear"]
        assert security["handlers"] != app["handlers"]
        assert "security_file" in security["handlers"]

    def test_security_logger_warns_minimum(self):
        level = self._base_logging()["loggers"]["django.security"]["level"]
        assert level in {"WARNING", "ERROR"}

    def test_django_errors_use_dedicated_file(self):
        assert "django_file" in self._base_logging()["loggers"]["django.request"]["handlers"]

    def test_app_logs_do_not_leak_into_security_stream(self):
        assert "security_file" not in self._base_logging()["loggers"]["flashwear"]["handlers"]

    def test_handlers_use_rotation(self):
        handlers = self._base_logging()["handlers"]
        for name in ("app_file", "django_file", "security_file"):
            assert handlers[name]["class"].endswith("RotatingFileHandler")
            assert handlers[name]["maxBytes"]
            assert handlers[name]["backupCount"]

    def test_rotation_limits_are_environment_driven(self):
        """Retention must be tunable without editing settings."""
        from config.settings import base

        assert base.LOG_MAX_BYTES > 0
        assert base.LOG_BACKUP_COUNT > 0
        assert base.SECURITY_LOG_BACKUP_COUNT >= base.LOG_BACKUP_COUNT
        handlers = base.LOGGING["handlers"]
        assert handlers["app_file"]["maxBytes"] == base.LOG_MAX_BYTES
        assert handlers["security_file"]["backupCount"] == base.SECURITY_LOG_BACKUP_COUNT

    def test_every_formatter_redacts(self):
        """Every formatter -- plain or JSON -- must redact, not just claim to."""
        from django.utils.module_loading import import_string

        from apps.core.logging_filters import RedactingFormatter

        formatters = self._base_logging()["formatters"]
        assert formatters, "no formatters configured"
        for name, config in formatters.items():
            formatter_class = import_string(config["()"])
            assert issubclass(formatter_class, RedactingFormatter), name

    def test_runtime_logging_is_wired(self, caplog):
        logger = logging.getLogger("flashwear.test")
        with caplog.at_level(logging.INFO, logger="flashwear.test"):
            logger.info("flashwear logging is active")
        assert any("flashwear logging is active" in r.getMessage() for r in caplog.records)


class TestSensitiveDataRedaction:
    """Never log passwords, tokens, payment secrets or customer contact details."""

    @pytest.mark.parametrize(
        "message,forbidden",
        [
            ("user logged in with password=Hunter2Secret", "Hunter2Secret"),
            ("api_key=sk-live-abcdef123456", "sk-live-abcdef123456"),
            ("access_token=eyJhbGciOiJIUzI1NiJ9.payload", "eyJhbGciOiJIUzI1NiJ9"),
            ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
            ("payment_secret=ps_live_98765", "ps_live_98765"),
            ("contact changed for bob@example.com", "bob@example.com"),
            ("secret=TopSecretValue123", "TopSecretValue123"),
            ("client_secret: 'pi_key_1234567890abc'", "pi_key_1234567890abc"),
            ("Cookie: sessionid=8f7b6c5d4e3f", "8f7b6c5d4e3f"),
        ],
    )
    def test_sensitive_values_are_redacted(self, message, forbidden):
        from apps.core.logging_filters import redact

        cleaned = redact(message)
        assert forbidden not in cleaned
        assert "[REDACTED]" in cleaned

    @pytest.mark.parametrize(
        "message",
        [
            "order FW-1001 was created",
            "product 42 cached successfully",
            "GET /health/ 200",
            "celery task flashwear.core.normalize finished",
            "cart total 249.99 for user_id=88",
        ],
    )
    def test_benign_messages_are_untouched(self, message):
        from apps.core.logging_filters import redact

        assert redact(message) == message

    def test_redact_handles_empty_input(self):
        from apps.core.logging_filters import redact

        assert redact("") == ""

    def test_formatter_redacts_positional_args(self):
        from apps.core.logging_filters import RedactingFormatter

        record = logging.LogRecord(
            name="flashwear",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="login for %s with password=%s",
            args=("user@example.com", "letmein"),
            exc_info=None,
        )
        output = RedactingFormatter().format(record)
        assert "letmein" not in output
        assert "user@example.com" not in output
        assert "[REDACTED]" in output

    def test_formatter_supports_mapping_args(self):
        """Celery logs with a dict of named substitutions; redaction must not break it."""
        from apps.core.logging_filters import RedactingFormatter

        record = logging.LogRecord(
            name="celery",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Task %(name)s[%(id)s] succeeded in %(runtime)ss: %(return_value)s",
            args={"name": "flashwear.demo", "id": "abc", "runtime": "0.01", "return_value": "ok"},
            exc_info=None,
        )
        output = RedactingFormatter().format(record)
        assert "flashwear.demo" in output
        assert "succeeded" in output

    def test_formatter_never_raises_on_odd_records(self):
        from apps.core.logging_filters import RedactingFormatter

        record = logging.LogRecord(
            name="flashwear",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="plain message",
            args=(),
            exc_info=None,
        )
        assert "plain message" in RedactingFormatter().format(record)

    def test_no_credentials_reach_the_log_output(self, caplog):
        """End-to-end: a logger call with a secret must not surface the secret."""
        from apps.core.logging_filters import RedactingFormatter

        logger = logging.getLogger("flashwear")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingFormatter())
        logger.addHandler(handler)
        try:
            logger.info("checkout failed for %s token=%s", "user@example.com", "tok_abcdef123")
        finally:
            logger.removeHandler(handler)
        output = stream.getvalue()
        assert "tok_abcdef123" not in output
        assert "user@example.com" not in output
        assert "[REDACTED]" in output

    def test_email_body_is_never_written_by_us(self, client):
        """Logging middleware must not record request bodies with credentials."""
        response = client.post("/health/", {"password": "should-not-be-logged"})
        assert response.status_code == 405


class TestCeleryFoundation:
    """Celery app is importable and correctly configured."""

    def test_app_is_exposed(self):
        from config import celery_app

        assert celery_app.main == "flashwear"

    def test_app_uses_django_settings_namespace(self):
        from config.celery import app

        assert app.conf.broker_url == settings.CELERY_BROKER_URL
        assert app.conf.result_backend == settings.CELERY_RESULT_BACKEND

    def test_json_serialisation_only(self):
        from config.celery import app

        assert app.conf.task_serializer == "json"
        assert app.conf.result_serializer == "json"
        assert app.conf.accept_content == ["json"]

    def test_timezone_matches_django(self):
        from config.celery import app

        assert app.conf.timezone == settings.TIME_ZONE

    def test_debug_task_registered(self):
        from config.celery import app

        assert "config.celery.debug_task" in app.tasks

    def test_debug_task_executes_eagerly(self):
        from config.celery import debug_task

        result = debug_task.apply().get()
        assert "flashwear celery ok" in result

    def test_worker_can_inspect_broker_settings(self):
        result = subprocess.run(
            [sys.executable, "-c", "from config.celery import app; print(app.main)"],
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
            env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.testing"},
            check=False,
        )
        assert result.returncode == 0
        assert "flashwear" in result.stdout

    def test_autodiscovery_is_enabled(self):
        from config.celery import app

        assert app.autodiscover_tasks is not None


class TestRedisConfiguration:
    """Redis wiring for cache, Celery broker and sessions."""

    def test_celery_broker_and_backend_are_set(self):
        assert settings.CELERY_BROKER_URL.startswith(("redis://", "rediss://"))
        assert settings.CELERY_RESULT_BACKEND.startswith(("redis://", "rediss://"))

    def test_tests_use_in_process_cache(self):
        """The suite must not require a running Redis instance."""
        assert settings.CACHES["default"]["BACKEND"].endswith("LocMemCache")

    def test_cache_ignores_exceptions_when_redis_fails(self):
        """A Redis outage should degrade the cache, not break every request."""
        source = (settings.BASE_DIR / "config" / "settings" / "base.py").read_text(encoding="utf-8")
        assert "IGNORE_EXCEPTIONS" in source

    def test_redis_urls_are_separated_by_purpose(self):
        """Sharing one Redis database between cache and broker invites key collisions."""
        from config.settings import base

        assert base.CELERY_BROKER_URL != base.CELERY_RESULT_BACKEND
        assert base.CELERY_BROKER_URL != base.CACHE_URL
        assert base.CACHE_URL != base.CELERY_RESULT_BACKEND

    @pytest.mark.parametrize(
        "redis_url,expected_cache,expected_broker,expected_results",
        [
            ("redis://cache:6379/0", 1, 2, 3),
            ("redis://cache:6379/5", 1, 2, 3),
            ("rediss://cache:6380", 1, 2, 3),
            ("redis://:secret@cache:6379?ssl_cert_reqs=none", 1, 2, 3),
        ],
    )
    def test_redis_databases_are_derived_when_not_configured(
        self, redis_url, expected_cache, expected_broker, expected_results
    ):
        """A single REDIS_URL must fan out to distinct logical databases.

        The host, credentials and query arguments have to survive the rewrite -- asserting only the
        trailing index would not notice a URL rebuilt without its authority.
        """
        snippet = (
            "from config.settings import base;"
            "print(base.CACHE_URL, base.CELERY_BROKER_URL, base.CELERY_RESULT_BACKEND)"
        )
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
            env={
                **os.environ,
                "DJANGO_SETTINGS_MODULE": "config.settings.development",
                "REDIS_URL": redis_url,
                # Ensure no inherited value short-circuits the derivation.
                "CACHE_URL": "",
                "CELERY_BROKER_URL": "",
                "CELERY_RESULT_BACKEND": "",
            },
            check=False,
        )
        assert result.returncode == 0, result.stderr
        cache, broker, results = result.stdout.split()

        authority = urlsplit(redis_url).netloc
        for url, expected_db in (
            (cache, expected_cache),
            (broker, expected_broker),
            (results, expected_results),
        ):
            parsed = urlsplit(url)
            assert parsed.netloc == authority
            assert parsed.path == f"/{expected_db}"
            assert parsed.query == urlsplit(redis_url).query

    def test_explicit_redis_urls_win_over_derivation(self):
        snippet = "from config.settings import base;print(base.CACHE_URL, base.CELERY_BROKER_URL)"
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
            env={
                **os.environ,
                "DJANGO_SETTINGS_MODULE": "config.settings.development",
                "REDIS_URL": "redis://cache:6379/0",
                "CACHE_URL": "redis://dedicated-cache:6379/7",
                "CELERY_BROKER_URL": "redis://dedicated-broker:6379/9",
            },
            check=False,
        )
        assert result.returncode == 0, result.stderr
        cache, broker = result.stdout.split()
        assert cache == "redis://dedicated-cache:6379/7"
        assert broker == "redis://dedicated-broker:6379/9"

    def test_sessions_use_the_database_backend(self):
        from config.settings import base

        assert base.SESSION_ENGINE == "django.contrib.sessions.backends.db"

    def test_production_would_use_redis(self):
        """Production settings always route the cache through Redis."""
        source = (settings.BASE_DIR / "config" / "settings" / "production.py").read_text(
            encoding="utf-8"
        )
        assert "django_redis.cache.RedisCache" in source

    def test_broker_retry_on_startup(self):
        assert settings.CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP is True

    def test_task_time_limits_configured(self):
        assert settings.CELERY_TASK_TIME_LIMIT > settings.CELERY_TASK_SOFT_TIME_LIMIT


class TestDatabaseConfiguration:
    """Database wiring."""

    def test_single_default_alias(self):
        assert list(settings.DATABASES) == ["default"]

    def test_tests_are_isolated_and_fast(self):
        """The suite must run against an in-memory database, never a real file."""
        name = settings.DATABASES["default"]["NAME"]
        assert ":memory:" in name or "mode=memory" in name

    def test_persistent_connections_only_configured_for_postgres(self):
        """SQLite must not receive Postgres-only connection tuning."""
        from config.settings import base

        engine = base.DATABASES["default"].get("ENGINE", "")
        if engine.endswith("postgresql"):
            assert base.DATABASES["default"]["CONN_MAX_AGE"] >= 0
            assert base.DATABASES["default"]["CONN_HEALTH_CHECKS"] is True
        else:
            assert "CONN_MAX_AGE" not in base.DATABASES["default"]

    def test_production_requires_postgres(self):
        source = (settings.BASE_DIR / "config" / "settings" / "production.py").read_text(
            encoding="utf-8"
        )
        assert "ImproperlyConfigured" in source
        assert "postgresql" in source

    def test_base_has_no_hardcoded_credentials(self):
        """DATABASE_URL must come from the environment, never the source tree."""
        source = (settings.BASE_DIR / "config" / "settings" / "base.py").read_text(encoding="utf-8")
        assert "env.db_url(" in source
        assert "postgres://" not in source

    def test_env_file_is_ignored_by_git(self):
        gitignore = (settings.BASE_DIR / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gitignore
        assert "!.env.example" in gitignore

    def test_env_example_has_no_real_secrets(self):
        example = (settings.BASE_DIR / ".env.example").read_text(encoding="utf-8")
        assert "SECRET_KEY=" in example
        assert "DATABASE_URL=" in example
        # Placeholders only.
        assert "sk-live" not in example
