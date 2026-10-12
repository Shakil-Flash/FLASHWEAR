"""PostgreSQL / Database Backup Management Command.

Automates database backups with:
- Timestamped filenames
- Format verification & integrity checks
- Secure secret handling (environment variables only, no passwords in argv)
- Protection against accidental overwrite
- Configurable retention policy (retention pruning)
- Useful exit codes and failure detection
"""

from __future__ import annotations

import datetime
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a verified, timestamped database backup with retention management."

    def add_arguments(self, parser):
        parser.add_argument(
            "--backup-dir",
            dest="backup_dir",
            type=str,
            default=os.environ.get("BACKUP_DIR", getattr(settings, "BACKUP_DIR", "backups")),
            help="Directory to store backup files (default: 'backups/' or $BACKUP_DIR).",
        )
        parser.add_argument(
            "--retention-days",
            dest="retention_days",
            type=int,
            default=int(
                os.environ.get(
                    "BACKUP_RETENTION_DAYS", getattr(settings, "BACKUP_RETENTION_DAYS", 7)
                )
            ),
            help="Number of days to retain backups before pruning (default: 7).",
        )
        parser.add_argument(
            "--no-cleanup",
            dest="no_cleanup",
            action="store_true",
            help="Skip pruning older backups exceeding retention days.",
        )
        parser.add_argument(
            "--database",
            default="default",
            help="Django database alias to back up (default: 'default').",
        )
        parser.add_argument(
            "--prefix",
            default="flashwear_backup",
            help="Prefix for backup filenames.",
        )

    def handle(self, *args, **options):
        db_alias = options["database"]
        if db_alias not in settings.DATABASES:
            raise CommandError(f"Database alias '{db_alias}' not found in settings.DATABASES.")

        db_config = settings.DATABASES[db_alias]
        engine = db_config.get("ENGINE", "")
        backup_dir = Path(options.get("backup_dir") or "backups").resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.datetime.now(datetime.UTC)
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")

        start_time = time.monotonic()

        if "postgresql" in engine:
            backup_file = self._backup_postgresql(
                db_config, backup_dir, options["prefix"], timestamp_str
            )
        elif "sqlite" in engine:
            backup_file = self._backup_sqlite(
                db_config, backup_dir, options["prefix"], timestamp_str
            )
        else:
            raise CommandError(f"Unsupported database engine for automated backup: {engine}")

        duration = time.monotonic() - start_time
        file_size = backup_file.stat().st_size

        self.stdout.write(
            self.style.SUCCESS(
                f"Backup completed successfully in {duration:.2f}s: {backup_file.name} "
                f"({file_size / (1024 * 1024):.2f} MB)"
            )
        )

        # Retention Cleanup
        if not options.get("no_cleanup"):
            self._prune_old_backups(
                backup_dir,
                options["prefix"],
                options.get("retention_days", 7),
                current_file=backup_file,
            )

        return str(backup_file)

    def _backup_postgresql(
        self, db_config: dict, backup_dir: Path, prefix: str, timestamp_str: str
    ) -> Path:
        """Execute pg_dump with custom archive format and verify integrity."""
        db_name = db_config.get("NAME")
        user = db_config.get("USER")
        password = db_config.get("PASSWORD")
        host = db_config.get("HOST")
        port = str(db_config.get("PORT") or "5432")

        if not db_name:
            raise CommandError("PostgreSQL database NAME is not configured.")

        filename = f"{prefix}_{timestamp_str}.dump"
        target_path = backup_dir / filename

        if target_path.exists():
            raise CommandError(
                f"Backup file already exists: {target_path}. Aborting to prevent overwrite."
            )

        cmd = ["pg_dump", "-Fc", "--no-owner", "--no-privileges", "-f", str(target_path)]
        if host:
            cmd.extend(["-h", host])
        if port:
            cmd.extend(["-p", port])
        if user:
            cmd.extend(["-U", user])
        cmd.append(db_name)

        env = os.environ.copy()
        if password:
            env["PGPASSWORD"] = password

        if not shutil.which("pg_dump"):
            raise CommandError(
                "PostgreSQL client tool 'pg_dump' not found in system PATH. "
                "Ensure postgresql-client is installed in your runtime environment."
            )

        self.stdout.write(f"Initiating pg_dump to {target_path.name}...")
        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:
            if target_path.exists():
                target_path.unlink()
            raise CommandError(f"Failed to execute pg_dump: {exc}") from exc

        if result.returncode != 0:
            if target_path.exists():
                target_path.unlink()
            err_msg = result.stderr.strip() or "Unknown error"
            raise CommandError(f"pg_dump failed with exit code {result.returncode}: {err_msg}")

        # Set restrictive permissions (0600)
        try:
            target_path.chmod(0o600)
        except (AttributeError, OSError):
            pass

        self._verify_postgresql_dump(target_path, env)
        return target_path

    def _verify_postgresql_dump(self, dump_path: Path, env: dict) -> None:
        """Verify the dumped archive is non-empty and structurally intact."""
        if dump_path.stat().st_size == 0:
            dump_path.unlink()
            raise CommandError("Backup validation failed: dump file is 0 bytes.")

        try:
            with open(dump_path, "rb") as f:
                header = f.read(5)
                if header != b"PGDMP":
                    dump_path.unlink()
                    raise CommandError(
                        "Backup validation failed: invalid archive header (expected PGDMP)."
                    )
        except OSError as exc:
            raise CommandError(f"Could not read backup file header: {exc}") from exc

        if shutil.which("pg_restore"):
            check_result = subprocess.run(
                ["pg_restore", "-l", str(dump_path)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if check_result.returncode != 0:
                dump_path.unlink()
                raise CommandError(
                    f"pg_restore integrity verification failed: {check_result.stderr.strip()}"
                )

        self.stdout.write(self.style.SUCCESS("Archive integrity verified successfully."))

    def _backup_sqlite(
        self, db_config: dict, backup_dir: Path, prefix: str, timestamp_str: str
    ) -> Path:
        """Execute SQLite backup using the safe online backup API."""
        source_name = str(db_config.get("NAME") or ":memory:")
        filename = f"{prefix}_{timestamp_str}.sqlite3"
        target_path = backup_dir / filename

        if target_path.exists():
            raise CommandError(
                f"Backup file already exists: {target_path}. Aborting to prevent overwrite."
            )

        self.stdout.write(f"Initiating SQLite snapshot to {target_path.name}...")
        try:
            if source_name == ":memory:" or not Path(source_name).exists():
                from django.db import connection

                source_conn = connection.connection
                dest_conn = sqlite3.connect(target_path)
                with dest_conn:
                    source_conn.backup(dest_conn)
                dest_conn.close()
            else:
                source_path = Path(source_name)
                source_conn = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
                dest_conn = sqlite3.connect(target_path)
                with dest_conn:
                    source_conn.backup(dest_conn)
                source_conn.close()
                dest_conn.close()
        except Exception as exc:
            if target_path.exists():
                target_path.unlink()
            raise CommandError(f"SQLite backup failed: {exc}") from exc

        try:
            target_path.chmod(0o600)
        except (AttributeError, OSError):
            pass

        if target_path.stat().st_size == 0:
            target_path.unlink()
            raise CommandError("SQLite backup validation failed: file is 0 bytes.")

        with open(target_path, "rb") as f:
            header = f.read(16)
            if not header.startswith(b"SQLite format 3"):
                target_path.unlink()
                raise CommandError(
                    "SQLite backup validation failed: missing valid SQLite magic header."
                )

        self.stdout.write(self.style.SUCCESS("SQLite snapshot verified successfully."))
        return target_path

    def _prune_old_backups(
        self, backup_dir: Path, prefix: str, retention_days: int, current_file: Path
    ) -> int:
        """Prune backup files older than retention_days, preserving at least 1 backup."""
        if retention_days <= 0:
            return 0

        now = datetime.datetime.now(datetime.UTC)
        cutoff = now - datetime.timedelta(days=retention_days)

        candidates: list[tuple[Path, datetime.datetime]] = []
        for p in backup_dir.iterdir():
            if not p.is_file() or not p.name.startswith(prefix):
                continue
            mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime, tz=datetime.UTC)
            candidates.append((p, mtime))

        if not candidates:
            return 0

        candidates.sort(key=lambda x: x[1], reverse=True)
        to_check = candidates[1:]
        deleted_count = 0

        for path, mtime in to_check:
            if mtime < cutoff and path != current_file:
                try:
                    path.unlink()
                    deleted_count += 1
                    self.stdout.write(
                        f"Pruned expired backup: {path.name} (age: {(now - mtime).days}d)"
                    )
                except OSError as exc:
                    self.stderr.write(
                        f"Warning: could not delete expired backup {path.name}: {exc}"
                    )

        return deleted_count
