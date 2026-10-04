"""Cross-cutting request helpers.

Only one thing lives here right now: honouring the site-wide maintenance flag. It is here rather
than in ``apps.core.views`` because "is the storefront open?" is a question every public page has to
ask, and Phase 1 only asked it on the homepage -- which meant flipping the switch kept the whole
catalogue reachable while the site showed a maintenance notice.
"""

from __future__ import annotations

from functools import wraps

from django.shortcuts import render

from apps.core.context_processors import get_site_configuration


def is_staff(request) -> bool:
    """Whether this request belongs to somebody who should see the site during maintenance.

    Staff are let through deliberately: the person turning the switch off must be able to see what
    they are turning it off for.
    """
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and user.is_staff)


def site_is_open(request) -> bool:
    """Whether the public storefront should render normally."""
    return not get_site_configuration().maintenance_mode


def maintenance_page(request):
    """Render the maintenance notice with the status a browser and a crawler both understand."""
    return render(request, "pages/maintenance.html", status=503)


def storefront_open(view):
    """Serve the maintenance page instead of ``view`` while the flag is on.

    Applied to read-only storefront views only. Anything that changes data (the account area, the
    admin, the API) is untouched: closing a shop for a deployment should not lock a customer out of
    their own address book, and should not break an API client's retry loop.
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if site_is_open(request) or is_staff(request):
            return view(request, *args, **kwargs)
        return maintenance_page(request)

    return wrapper
