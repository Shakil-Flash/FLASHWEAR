"""The account area's navigation model.

One list, defined once, so the rail on the left and the ``AccountDashboardView`` context can never
drift apart. Sections belonging to a later phase are marked ``soon`` rather than omitted: an
account area that silently lacks its own sections reads as broken, and a link to an unimplemented
page is worse.
"""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

# ``(label, url_name, live)``. ``None`` means "planned, not yet a URL".
ACCOUNT_SECTIONS: list[tuple[str, str | None, bool]] = [
    (_("Overview"), "account:dashboard", True),
    (_("Profile"), "account:profile", True),
    (_("Orders"), "account:orders", True),
    (_("Wishlist"), None, False),
    (_("Addresses"), "account:addresses", True),
    (_("Reviews"), None, False),
    (_("FLASH DNA"), None, False),
    (_("FLASH Closet"), None, False),
    (_("Loyalty"), None, False),
    (_("Notifications"), None, False),
    (_("Security"), "account:security", True),
]


def account_sections(request=None) -> list[dict]:
    """Resolve the section list for the navigation rail.

    Each entry carries its resolved ``url`` and, when a request is available, an ``active`` flag.
    The dashboard matches exactly: it lives at the ``/account/`` prefix, so a prefix match would
    light it up on every page in the area.
    """
    sections = []
    for label, url_name, live in ACCOUNT_SECTIONS:
        url = reverse(url_name) if live and url_name else None
        active = False
        if request is not None and url:
            path = request.path
            # Everything else is a leaf page, so a child path ("/account/addresses/3/") still
            # counts as being inside its section.
            active = path == url or (path.startswith(f"{url}/") and url_name != "account:dashboard")
        sections.append({"label": label, "url": url, "live": live, "active": active})
    return sections
