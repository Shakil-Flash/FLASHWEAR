"""Custom compilemessages management command.

Overrides Django's compilemessages command to fall back to a pure-Python MO compiler
when GNU gettext `msgfmt` is not available (e.g. on Windows developer machines).
"""

from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


def parse_po_file(filepath: str | Path) -> dict[str, str]:
    """Parse a .po file into a dictionary of msgid -> msgstr."""
    messages: dict[str, str] = {}
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    msgid: str | None = None
    msgstr: str | None = None
    section: str | None = None

    def parse_str(line_val: str) -> str:
        start = line_val.find('"')
        end = line_val.rfind('"')
        if start != -1 and end != -1 and start < end:
            inner = line_val[start + 1 : end]
            return (
                inner.replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace("\\\\", "\\")
            )
        return ""

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if msgid is not None and msgstr is not None:
                messages[msgid] = msgstr
                msgid = None
                msgstr = None
                section = None
            continue

        if stripped.startswith("msgid "):
            if msgid is not None and msgstr is not None:
                messages[msgid] = msgstr
                msgid = None
                msgstr = None
            msgid = parse_str(stripped[6:])
            section = "msgid"
        elif stripped.startswith("msgstr "):
            msgstr = parse_str(stripped[7:])
            section = "msgstr"
        elif stripped.startswith('"') and stripped.endswith('"'):
            val = parse_str(stripped)
            if section == "msgid" and msgid is not None:
                msgid += val
            elif section == "msgstr" and msgstr is not None:
                msgstr += val

    if msgid is not None and msgstr is not None:
        messages[msgid] = msgstr

    return messages


def compile_po_to_mo(po_path: str | Path, mo_path: str | Path) -> int:
    """Compile a .po file into GNU gettext .mo binary format."""
    raw_messages = parse_po_file(po_path)
    messages = {k: v for k, v in raw_messages.items() if k == "" or v.strip() != ""}
    if "" not in messages:
        messages[""] = (
            "Project-Id-Version: FLASHWEAR 1.0\n"
            "Content-Type: text/plain; charset=UTF-8\n"
            "Content-Transfer-Encoding: 8bit\n"
        )

    keys = sorted(messages.keys())
    ids = b"".join([k.encode("utf-8") + b"\x00" for k in keys])
    strs = b"".join([messages[k].encode("utf-8") + b"\x00" for k in keys])

    keystart = 7 * 4 + len(keys) * 8 * 2
    valuestart = keystart + len(ids)

    koffsets: list[int] = []
    o = keystart
    for k in keys:
        k_len = len(k.encode("utf-8"))
        koffsets.extend([k_len, o])
        o += k_len + 1

    voffsets: list[int] = []
    o = valuestart
    for k in keys:
        v_len = len(messages[k].encode("utf-8"))
        voffsets.extend([v_len, o])
        o += v_len + 1

    header = struct.pack(
        "Iiiiiii",
        0x950412DE,  # Magic
        0,  # Version
        len(keys),  # Count of entries
        7 * 4,  # Offset of original table
        7 * 4 + len(keys) * 8,  # Offset of translated table
        0,  # Size of hash table
        0,  # Offset of hash table
    )

    mo_bytes = (
        header
        + struct.pack(f"{len(koffsets)}i", *koffsets)
        + struct.pack(f"{len(voffsets)}i", *voffsets)
        + ids
        + strs
    )

    os.makedirs(os.path.dirname(mo_path), exist_ok=True)
    with open(mo_path, "wb") as f:
        f.write(mo_bytes)

    return len(keys)


class Command(BaseCommand):
    help = "Compiles .po files to .mo files for use with builtin gettext support."

    def add_arguments(self, parser):
        parser.add_argument(
            "--locale",
            "-l",
            action="append",
            default=[],
            help="Locale(s) to process (e.g. bn). Default is to process all.",
        )
        parser.add_argument(
            "--exclude",
            "-x",
            action="append",
            default=[],
            help="Locales to exclude. Default is none.",
        )
        parser.add_argument(
            "--use-fuzzy",
            "-f",
            action="store_true",
            help="Use fuzzy translations (ignored in fallback compiler).",
        )
        parser.add_argument(
            "--ignore",
            "-i",
            action="append",
            default=[],
            help="Ignore directories matching this pattern.",
        )

    def handle(self, *args, **options):
        # If msgfmt binary is available, prefer Django's native command
        if shutil.which("msgfmt"):
            from django.core.management.commands.compilemessages import Command as DjangoCommand

            return DjangoCommand().handle(*args, **options)

        locales = options.get("locale") or []
        excludes = options.get("exclude") or []

        base_dir = Path(settings.BASE_DIR).resolve()
        locale_dirs: list[Path] = []
        for path in getattr(settings, "LOCALE_PATHS", []):
            p = Path(path).resolve()
            if base_dir in p.parents or p == base_dir:
                locale_dirs.append(p)

        default_locale = base_dir / "locale"
        if default_locale not in locale_dirs:
            locale_dirs.append(default_locale)

        # Also search app directories within BASE_DIR
        for app_config in settings.INSTALLED_APPS:
            if not app_config.startswith("apps."):
                continue
            try:
                mod = __import__(app_config, fromlist=[""])
                if hasattr(mod, "__file__") and mod.__file__:
                    app_locale = Path(mod.__file__).resolve().parent / "locale"
                    if app_locale.is_dir():
                        locale_dirs.append(app_locale)
            except Exception:
                pass

        processed_count = 0
        seen_dirs: set[Path] = set()

        for l_dir in locale_dirs:
            l_dir = l_dir.resolve()
            if l_dir in seen_dirs or not l_dir.is_dir():
                continue
            seen_dirs.add(l_dir)

            for po_file in l_dir.glob("*/LC_MESSAGES/*.po"):
                loc = po_file.parent.parent.name
                if locales and loc not in locales:
                    continue
                if loc in excludes:
                    continue

                mo_file = po_file.with_suffix(".mo")
                count = compile_po_to_mo(po_file, mo_file)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"processing file {po_file.name} in {po_file.parent} "
                        f"({count} messages compiled to {mo_file.name})"
                    )
                )
                processed_count += 1

        if processed_count == 0:
            self.stdout.write("No .po files found to compile.")
