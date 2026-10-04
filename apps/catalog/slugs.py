"""Slug resolution.

Public catalogue URLs are slugs, so a slug has to stay unique and predictable. Names are not
unique ("Premium Hoodie" may legitimately exist twice), which means the name -> slug step needs a
documented conflict policy.

The policy is a numeric suffix in creation order: the first product to claim a name gets the bare
slug, the next gets ``-2``, and so on. That is deterministic (the same database state always
resolves to the same slug), greppable, and it never hides a data-entry mistake behind a hash --
unlike an id or random suffix, which produce URLs nobody can predict or type.

The unique index on ``slug`` is the real guarantee. This helper exists so that saving a product
does not blow up with ``IntegrityError`` and so that an explicit slug is always honoured.
"""

from __future__ import annotations

from django.db import models
from django.utils.text import slugify

# Slugs longer than this get truncated before the suffix is appended, so the result still fits the
# column and the ``-2`` marker stays visible.
DEFAULT_MAX_LENGTH = 120


def slug_candidate(value: str, *, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Return the slug for ``value``, without touching the database."""
    base = slugify(value or "")[:max_length].strip("-")
    return base or "item"


def unique_slug(
    model: type[models.Model],
    value: str,
    *,
    instance: models.Model | None = None,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> str:
    """Return a slug for ``value`` that no *other* row of ``model`` uses.

    ``instance`` (when it is already saved) is excluded from the check, so re-saving a product
    without renaming it keeps its URL instead of appending ``-2`` to itself.
    """
    base = slug_candidate(value, max_length=max_length)
    queryset = model._default_manager.all()
    if instance is not None and instance.pk:
        queryset = queryset.exclude(pk=instance.pk)

    candidate = base
    counter = 2
    while queryset.filter(slug=candidate).exists():
        suffix = f"-{counter}"
        candidate = f"{base[: max_length - len(suffix)]}{suffix}"
        counter += 1
    return candidate


def resolve_slug(
    model: type[models.Model],
    instance: models.Model,
    *,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> str:
    """Fill in a blank slug from ``instance.name``, or return the existing one untouched.

    An operator who typed a slug has decided the public URL; never second-guess them.
    """
    if instance.slug:
        return instance.slug
    return unique_slug(model, instance.name, instance=instance, max_length=max_length)
