"""Shared plumbing for every back-office screen (Phase 16).

One render helper so navigation, capabilities and the active section are computed in a
single place rather than re-assembled by twenty views -- and one request-range parser so
``?range=`` means the same thing on every page.
"""

from __future__ import annotations

from django.shortcuts import render

from apps.backoffice.permissions import capabilities_for, navigation_for
from apps.backoffice.selectors.common import RANGES, resolve_range

__all__ = ["range_from_request", "render_bo"]


def render_bo(request, template_name: str, *, active: str = "", **context):
    """Render a back-office template with the sidebar and capability set attached."""
    context.setdefault("nav", navigation_for(request.user))
    context.setdefault("capabilities", capabilities_for(request.user))
    context.setdefault("active_key", active)
    context.setdefault("range_choices", RANGES)
    return render(request, template_name, context)


def range_from_request(request):
    """The requested date window (defaults to the last seven days)."""
    return resolve_range(request.GET.get("range"))
