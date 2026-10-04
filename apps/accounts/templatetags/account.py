"""Template tags for the account area."""

from django import template

from apps.accounts.navigation import account_sections

register = template.Library()

BASE = "inline-flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium"
IDLE = f"{BASE} text-slate-600 transition hover:bg-slate-50 hover:text-slate-900"
ACTIVE = f"{BASE} bg-flash-50 text-flash-800"
DISABLED = f"{BASE} cursor-not-allowed text-slate-400"


@register.simple_tag(takes_context=True)
def account_nav(context, css_classes: str = ""):
    """Render the account section rail.

    Kept in Python rather than a template loop because each entry needs ``reverse()`` and an
    active-state calculation; doing that in the template would mean recomputing it per screen.

    ``css_classes`` overrides the shared layout classes for the sidebar case; the active, idle and
    unavailable states stay consistent either way.
    """
    request = context.get("request")
    items = []
    for section in account_sections(request):
        label, url = section["label"], section["url"]
        if not section["live"]:
            css = css_classes or DISABLED
        elif section["active"]:
            css = css_classes or ACTIVE
        else:
            css = css_classes or IDLE
        items.append({"label": label, "url": url, "css": css, "soon": not section["live"]})
    return items
