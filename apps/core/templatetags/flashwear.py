"""Shared template tags."""

from __future__ import annotations

import re

from django import template
from django.urls import NoReverseMatch, reverse

register = template.Library()

_SLUGIFY_RE = re.compile(r"[^a-z0-9\-\s]+", re.IGNORECASE)


@register.filter
def space_to_dash(value: str) -> str:
    """Convert spaces to dashes (useful for HTML IDs and utility classes)."""
    if not value:
        return ""
    value = str(value).strip().lower()
    value = _SLUGIFY_RE.sub("", value)
    return re.sub(r"\s+", "-", value)


@register.simple_tag
def active_url(request, view_name: str, *args, **kwargs) -> str:
    """Return ``active`` if the current request URL resolves to the given view."""
    if not request:
        return ""
    try:
        target = reverse(view_name, args=args, kwargs=kwargs)
    except NoReverseMatch:
        return ""
    if request.path_info.startswith(target) and target != "/":
        return "active"
    if request.path_info == target:
        return "active"
    return ""


@register.filter
def currency(value, currency_code: str = "USD") -> str:
    """Simple currency formatter for templates (Phase 1 placeholder)."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return f"{currency_code} 0.00"
    return f"{currency_code} {amount:.2f}"


@register.simple_tag
def flashwear_version() -> str:
    """Expose the FLASHWEAR build version to templates (Phase 1 constant)."""
    return "0.1.0"
