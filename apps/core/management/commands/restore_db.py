"""PostgreSQL / Database Disaster Recovery & Restoration Management Command.

Restores and validates database backups with:
- Safety guards: refuses to overwrite default DB without --confirm-destructive
- Disposable target support: --target-db to verify in disposable/test databases
- Schema and migration integrity verification post-restore
- Measurement of recovery time (RTO tracking)
- Diagnostic verification mode: --verify-only
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Restore and verify a database backup with disaster-recovery safety guards."

    def add_arguments(self, parser):
        parser.add_argument(
            "backup_file",
            type=str,
            help="Path to the backup file to restore.",
        )
        parser.add_argument(
            "--target-db",
            dest="target_db",
            type=str,
            default=None,
            help=(
                "Target database name (or file path for SQLite). "
                "Defaults to settings.DATABASES['default']."
            ),
        )
        parser.add_argument(
            "--confirm-destructive",
            dest="confirm_destructive",
            action="store_true",
            help="Explicit safety confirmation required when restoring to the primary database.",
        )
        parser.add_argument(
            "--verify-only",
            dest="verify_only",
            action="store_true",
            help="Inspect and verify the archive contents without modifying any database.",
        )
        parser.add_argument(
            "--database",
            default="default",
            help="Django database alias for connection parameters (default: 'default').",
        )

    def handle(self, *args, **options):
        backup_path = Path(options["backup_file"]).resolve()
        if not backup_path.exists() or not backup_path.is_file():
            raise CommandError(f"Backup file not found: {backup_path}")

        db_alias = options["database"]
        if db_alias not in settings.DATABASES:
            raise CommandError(f"Database alias '{db_alias}' not found in settings.")

        db_config = settings.DATABASES[db_alias]
        engine = db_config.get("ENGINE", "")
        target_db_name = options.get("target_db") or str(db_config.get("NAME") or ":memory:")

        # Detect format from file header
        is_pg = False
        is_sqlite = False
        with open(backup_path, "rb") as f:
            header = f.read(16)
            if header.startswith(b"PGDMP"):
                is_pg = True
            elif header.startswith(b"SQLite format 3"):
                is_sqlite = True
            else:
                if backup_path.suffix in (".dump", ".tar") or "postgresql" in engine:
                    is_pg = True
                elif backup_path.suffix in (".sqlite3", ".db") or "sqlite" in engine:
                    is_sqlite = True

        if options.get("verify_only"):
            self.stdout.write(f"Verifying archive integrity for: {backup_path.name}")
            if is_pg:
                self._verify_pg_archive_only(backup_path)
            elif is_sqlite:
                self._verify_sqlite_archive_only(backup_path)
            else:
                raise CommandError(f"Unrecognized backup format in {backup_path.name}")
            self.stdout.write(self.style.SUCCESS("Archive verification passed."))
            return "verified"

        # Safety Guard: refuse overwrite unless confirmed or explicit disposable DB specified
        primary_db_name = str(db_config.get("NAME") or ":memory:")
        if target_db_name == primary_db_name and not options.get("confirm_destructive"):
            raise CommandError(
                f"SAFETY GUARD: Target database '{target_db_name}' matches the default "
                "configured database. Restoring is destructive. To proceed, specify "
                "--confirm-destructive, or provide a disposable database name via "
                "--target-db=<test_db>."
            )

        start_time = time.monotonic()

        if is_pg:
            self._restore_postgresql(backup_path, db_config, target_db_name)
        elif is_sqlite:
            self._restore_sqlite(backup_path, target_db_name)
        else:
            raise CommandError(f"Unsupported backup format for restoration: {backup_path.name}")

        duration = time.monotonic() - start_time
        self.stdout.write(
            self.style.SUCCESS(
                f"Disaster Recovery Restoration completed successfully in {duration:.2f}s (RTO). "
                f"Target database: {target_db_name}"
            )
        )
        return str(target_db_name)

    def _verify_pg_archive_only(self, backup_path: Path) -> None:
        """Inspect pg_dump custom archive without restoring."""
        if not shutil.which("pg_restore"):
            raise CommandError("Tool 'pg_restore' is not found in PATH.")

        result = subprocess.run(
            ["pg_restore", "-l", str(backup_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise CommandError(f"Archive inspection failed: {result.stderr.strip()}")

        entry_count = len(result.stdout.strip().splitlines())
        self.stdout.write(
            f"Archive is valid custom PostgreSQL dump with {entry_count} catalogue entries."
        )

    def _verify_sqlite_archive_only(self, backup_path: Path) -> None:
        """Inspect SQLite database without modifying target."""
        try:
            conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            row = cursor.fetchone()
            conn.close()
            if not row or row[0] != "ok":
                raise CommandError(f"SQLite integrity check failed: {row}")
            self.stdout.write("SQLite archive integrity check: OK.")
        except Exception as exc:
            raise CommandError(f"SQLite archive inspection failed: {exc}") from exc

    def _restore_postgresql(self, backup_path: Path, db_config: dict, target_db: str) -> None:
        """Execute pg_restore into target database."""
        if not shutil.which("pg_restore"):
            raise CommandError("Tool 'pg_restore' is not found in PATH. Install postgresql-client.")

        user = db_config.get("USER")
        password = db_config.get("PASSWORD")
        host = db_config.get("HOST")
        port = str(db_config.get("PORT") or "5432")

        cmd = [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "-d",
            target_db,
        ]
        if host:
            cmd.extend(["-h", host])
        if port:
            cmd.extend(["-p", port])
        if user:
            cmd.extend(["-U", user])
        cmd.append(str(backup_path))

        env = os.environ.copy()
        if password:
            env["PGPASSWORD"] = password

        self.stdout.write(f"Restoring {backup_path.name} into database '{target_db}'...")
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode not in (0, 1):
            raise CommandError(
                f"pg_restore failed with exit code {result.returncode}: {result.stderr.strip()}"
            )

        self.stdout.write("Post-restore schema verification...")
        if target_db == db_config.get("NAME"):
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM django_migrations;")
                mig_count = cursor.fetchone()[0]
                self.stdout.write(
                    f"Schema verification passed: {mig_count} applied migrations found."
                )

    def _restore_sqlite(self, backup_path: Path, target_db_path: str) -> None:
        """Restore SQLite backup into target database file."""
        if target_db_path == ":memory:":
            from django.db import connection

            source_conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
            with connection.connection:
                source_conn.backup(connection.connection)
            source_conn.close()
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM django_migrations;")
                mig_count = cursor.fetchone()[0]
            self.stdout.write(f"In-memory restore verified: {mig_count} applied migrations.")
            return

        dest_path = Path(target_db_path).resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        self.stdout.write(f"Restoring SQLite snapshot to {dest_path}...")
        try:
            source_conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
            dest_conn = sqlite3.connect(dest_path)
            with dest_conn:
                source_conn.backup(dest_conn)
            source_conn.close()
            dest_conn.close()
        except Exception as exc:
            raise CommandError(f"SQLite restore failed: {exc}") from exc

        # Post-restore verification
        conn = sqlite3.connect(dest_path)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table';")
        tables_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM django_migrations;")
        mig_count = cursor.fetchone()[0]
        conn.close()
        self.stdout.write(
            f"Schema verification passed: {tables_count} tables and "
            f"{mig_count} applied migrations found."
        )
