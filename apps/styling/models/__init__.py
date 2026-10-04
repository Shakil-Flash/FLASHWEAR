"""Styling models package.

Importing the concrete module here is what registers :class:`FlashDNA` with the app
registry during startup: without this file, ``apps.styling.models`` is a namespace package
with no attributes, the model is only registered whenever somebody happens to import the
submodule later, and the table is therefore never created by ``migrate --run-syncdb``.
"""

from __future__ import annotations

from apps.styling.models.flash_dna import FlashDNA

__all__ = ["FlashDNA"]
