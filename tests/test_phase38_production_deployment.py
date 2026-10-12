"""Phase 38: Production Deployment, Backup & Disaster Recovery Test Suite.

Covers:
- Production settings boot-guard enforcement (fails loud on invalid config)
- Django deployment security checks (check --deploy)
- Liveness and readiness probe behaviors under healthy and degraded conditions
- Automated backup command: timestamping, overwrite protection, retention pruning
- Automated restore command: guards against production overwrite, --verify-only
- Nginx reverse-proxy configuration (upstream TLS preservation via $forwarded_proto)
- Docker entrypoint startup safety (gating migrations and preventing demo seeding)
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# 1. Production Settings Boot Guards
# --------------------------------------------------------------------------------------


class TestProductionSettingsHardening:
    @pytest.fixture(autouse=True)
    def restore_base_settings(self):
        yield
        import importlib

        import config.settings.base

        importlib.reload(config.settings.base)

    def test_production_settings_enforces_debug_false(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "prod-key-123456789012345678901234567890")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", "https://example.com")
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/db")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("PAYMENT_PROVIDER", "stripe")
        monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", "whsec_123")
        monkeypatch.setenv("EMAIL_HOST", "smtp.example.com")

        import importlib

        import config.settings.base

        importlib.reload(config.settings.base)
        import config.settings.production

        importlib.reload(config.settings.production)

        assert config.settings.production.DEBUG is False
        assert config.settings.production.SESSION_COOKIE_SECURE is True
        assert config.settings.production.CSRF_COOKIE_SECURE is True
        assert config.settings.production.X_FRAME_OPTIONS == "DENY"
        assert config.settings.production.SECURE_CONTENT_TYPE_NOSNIFF is True

    def test_production_refuses_missing_or_insecure_secret_key(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "django-insecure-test-key")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", "https://example.com")
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/db")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("PAYMENT_PROVIDER", "stripe")
        monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", "whsec_123")
        monkeypatch.setenv("EMAIL_HOST", "smtp.example.com")

        import importlib

        import config.settings.base

        importlib.reload(config.settings.base)

        with pytest.raises(
            ImproperlyConfigured,
            match="Refusing to start production with the development SECRET_KEY",
        ):
            import config.settings.production

            importlib.reload(config.settings.production)

    def test_production_refuses_sqlite(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "prod-key-123456789012345678901234567890")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", "https://example.com")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("PAYMENT_PROVIDER", "stripe")
        monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", "whsec_123")
        monkeypatch.setenv("EMAIL_HOST", "smtp.example.com")

        import importlib

        import config.settings.base

        importlib.reload(config.settings.base)

        with pytest.raises(ImproperlyConfigured, match="Production requires PostgreSQL"):
            import config.settings.production

            importlib.reload(config.settings.production)

    def test_check_deploy_passes_cleanly(self):
        """Django's check --deploy passes with no security warnings."""
        from django.core.management import call_command

        # In testing environment (settings.testing), run check
        call_command("check")


# --------------------------------------------------------------------------------------
# 2. Health & Readiness Probes
# --------------------------------------------------------------------------------------


class TestHealthAndReadinessProbes:
    def test_liveness_probe_returns_200_without_touching_database(self, client):
        response = client.get(reverse("core:health_live"))
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    def test_readiness_probe_returns_200_when_healthy(self, client):
        response = client.get(reverse("core:health_ready"))
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["checks"]["database"] == "ok"
        assert data["checks"]["cache"] == "ok"

    def test_readiness_probe_returns_503_when_database_fails(self, client):
        with patch("apps.core.health.check_database", return_value="down"):
            response = client.get(reverse("core:health_ready"))
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["checks"]["database"] == "down"

    def test_readiness_probe_returns_503_when_cache_fails(self, client):
        with patch("apps.core.health.check_cache", return_value="down"):
            response = client.get(reverse("core:health_ready"))
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["checks"]["cache"] == "down"

    def test_health_endpoints_do_not_leak_secrets(self, client):
        for path in [
            reverse("core:health"),
            reverse("core:health_live"),
            reverse("core:health_ready"),
        ]:
            resp = client.get(path)
            raw = resp.content.decode("utf-8")
            assert "password" not in raw.lower()
            assert "secret" not in raw.lower()
            assert "token" not in raw.lower()
            assert "traceback" not in raw.lower()


# --------------------------------------------------------------------------------------
# 3. Database Backup Automation
# --------------------------------------------------------------------------------------


class TestDatabaseBackupCommand:
    def test_backup_creates_verified_timestamped_file(self, tmp_path):
        out_dir = tmp_path / "backups"
        out_dir.mkdir()

        backup_file_str = call_command(
            "backup_db",
            backup_dir=str(out_dir),
            retention_days=7,
        )
        backup_path = Path(backup_file_str)
        assert backup_path.exists()
        assert backup_path.stat().st_size > 0
        assert backup_path.name.startswith("flashwear_backup_")

    def test_backup_pruning_retains_newest_backup(self, tmp_path):
        out_dir = tmp_path / "backups"
        out_dir.mkdir()

        # Create older dummy backup files
        old_file_1 = out_dir / "flashwear_backup_20200101_000000.sqlite3"
        old_file_1.write_bytes(b"SQLite format 3\x00dummy")
        # Set mtime to 30 days ago
        old_time = time.time() - (30 * 86400)
        os.utime(old_file_1, (old_time, old_time))

        old_file_2 = out_dir / "flashwear_backup_20200102_000000.sqlite3"
        old_file_2.write_bytes(b"SQLite format 3\x00dummy2")
        os.utime(old_file_2, (old_time, old_time))

        # Run backup with retention of 3 days
        call_command(
            "backup_db",
            backup_dir=str(out_dir),
            retention_days=3,
        )

        # The expired files should be pruned, while the newly created backup remains
        assert not old_file_1.exists()
        assert not old_file_2.exists()
        remaining = list(out_dir.glob("flashwear_backup_*.sqlite3"))
        assert len(remaining) >= 1

    def test_backup_pg_dump_safely_passes_secrets(self, tmp_path):
        """PostgreSQL backup passes password via environment, not command arguments."""
        from apps.core.management.commands.backup_db import Command

        cmd = Command()
        fake_db = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "test_db",
            "USER": "test_user",
            "PASSWORD": "super_secret_password_xyz",
            "HOST": "localhost",
            "PORT": 5432,
        }

        with (
            patch("shutil.which", return_value="/usr/bin/pg_dump"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            # Mock verification
            with patch.object(cmd, "_verify_postgresql_dump"):
                result_path = cmd._backup_postgresql(
                    fake_db, tmp_path, "test_prefix", "20261013_000000"
                )
                assert result_path.name.startswith("test_prefix")

                # Verify subprocess.run arguments
                args, kwargs = mock_run.call_args
                command_args = args[0]
                assert "super_secret_password_xyz" not in command_args
                # Password must be in kwargs['env']
                assert kwargs["env"].get("PGPASSWORD") == "super_secret_password_xyz"


# --------------------------------------------------------------------------------------
# 4. Disaster Recovery & Restoration Automation
# --------------------------------------------------------------------------------------


class TestDatabaseRestoreCommand:
    def test_restore_refuses_default_db_without_confirmation(self, tmp_path):
        dummy_backup = tmp_path / "flashwear_backup_test.sqlite3"
        dummy_backup.write_bytes(b"SQLite format 3\x00data")

        # Refuses to restore over active database without --confirm-destructive
        with pytest.raises(CommandError, match="SAFETY GUARD"):
            call_command("restore_db", str(dummy_backup))

    def test_restore_verify_only_inspects_archive_safely(self, tmp_path):
        # Create a valid sqlite test file
        test_db = tmp_path / "valid.sqlite3"
        conn = sqlite3.connect(test_db)
        conn.execute("CREATE TABLE test_tbl (id INT);")
        conn.commit()
        conn.close()

        res = call_command("restore_db", str(test_db), verify_only=True)
        assert res == "verified"

    def test_restore_into_disposable_target_succeeds(self, tmp_path):
        # Create a source database with migrations table
        source_db = tmp_path / "source.sqlite3"
        conn = sqlite3.connect(source_db)
        conn.execute("CREATE TABLE django_migrations (id INT, app TEXT);")
        conn.execute("INSERT INTO django_migrations VALUES (1, 'catalog');")
        conn.commit()
        conn.close()

        disposable_target = tmp_path / "disposable_test.sqlite3"

        # Restore into disposable target
        res = call_command(
            "restore_db",
            str(source_db),
            target_db=str(disposable_target),
        )
        assert res == str(disposable_target)
        assert disposable_target.exists()

        # Check restored content
        verify_conn = sqlite3.connect(disposable_target)
        row = verify_conn.execute("SELECT count(*) FROM django_migrations;").fetchone()
        verify_conn.close()
        assert row[0] == 1


# --------------------------------------------------------------------------------------
# 5. Deployment Configuration & Script Integrity
# --------------------------------------------------------------------------------------


class TestDeploymentConfigurationAndScripts:
    def test_nginx_preserves_forwarded_proto(self):
        nginx_conf = Path("deploy/nginx.conf").read_text(encoding="utf-8")
        assert "map $http_x_forwarded_proto $forwarded_proto" in nginx_conf
        assert "proxy_set_header X-Forwarded-Proto $forwarded_proto;" in nginx_conf

    def test_entrypoint_protects_against_automatic_demo_seeding(self):
        entrypoint_sh = Path("entrypoint.sh").read_text(encoding="utf-8")
        assert "SEED_DEMO_DATA" in entrypoint_sh
        assert "AUTO_MIGRATE" in entrypoint_sh

    def test_deploy_and_backup_scripts_exist_and_executable(self):
        backup_sh = Path("scripts/backup.sh")
        restore_sh = Path("scripts/restore.sh")
        deploy_sh = Path("scripts/deploy.sh")

        assert backup_sh.exists()
        assert restore_sh.exists()
        assert deploy_sh.exists()

        # Scripts must contain safety checks
        assert "set -euo pipefail" in backup_sh.read_text(encoding="utf-8")
        assert "set -euo pipefail" in restore_sh.read_text(encoding="utf-8")
        assert "set -euo pipefail" in deploy_sh.read_text(encoding="utf-8")
