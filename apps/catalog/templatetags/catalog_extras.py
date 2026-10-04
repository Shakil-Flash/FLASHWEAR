"""Catalogue template tags and filters.

Deliberately small: the two things templates need that Python cannot express inline are money
formatting and query-string-preserving links. Everything else the catalogue needs is a model
property, so a template never has to loop to work something out.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import template
from django.conf import settings
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def money(value) -> str:
    """Format a catalogue amount with the storefront currency symbol.

    ``{{ product.price_min|money }}`` -> ``৳1,490`` / ``$49.00``.

    Uses the configured symbol and always shows two decimals when the amount has them, because a
    price of ``49`` and a price of ``49.00`` should look identical to a shopper. Not
    ``intlformat``: the storefront's currency is a setting, not a browser locale, and a customer in
    Dhaka browsing a USD storefront still needs to see the store's currency.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        # JSON-sourced amounts (checkout snapshots) arrive as decimal strings.
        if not value:
            return ""
        try:
            value = Decimal(value)
        except InvalidOperation:
            return value
    symbol = settings.CATALOG_CURRENCY_SYMBOL
    # ``as_tuple().exponent`` distinguishes a price stored as 49.00 from a bare 49, so both render
    # the same way to a shopper.
    if value.as_tuple().exponent == -2:
        return f"{symbol}{value:,.2f}"
    return f"{symbol}{value:,.0f}"


@register.filter
def was_price(value) -> str:
    """The struck-through "was" price. Empty when there is no compare-at price."""
    return money(value) if value else ""


@register.simple_tag(takes_context=True)
def query_transform(context, **kwargs):
    """Rebuild the current query string with some parameters replaced.

    Used by the sort controls and the pagination links so that ``?sort=price_asc`` survives page
    three, and so a page link never accidentally drops the shopper's sort. ``None`` values remove
    the key entirely, which is how ``page=1`` gets dropped on page one.
    """
    request = context["request"]
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    # Any list page change resets pagination; keeping page=3 with a new sort shows page three of a
    # different ordering, which reads as "the filter did nothing".
    if "page" not in kwargs:
        params.pop("page", None)
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else ""


@register.simple_tag(takes_context=True)
def sort_link(context, value, label=None) -> str:
    """A sort control that marks itself with ``aria-current`` when active.

    The active attribute is marked safe before it reaches ``format_html``. Passing it as a plain
    argument escapes the quotes -- ``aria-current=&quot;true&quot;`` -- which is not an
    attribute any browser recognises: the control would look right in a snapshot test and be
    wrong for a screen reader.
    """
    request = context["request"]
    is_active = request.GET.get("sort", "") == value
    href = f"{request.path}{query_transform(context, sort=value)}"
    css = (
        "rounded-full bg-slate-900 px-4 py-2 text-sm font-semibold text-white"
        if is_active
        else (
            "rounded-full border border-slate-300 bg-white px-4 py-2 text-sm "
            "text-slate-700 hover:border-slate-900"
        )
    )
    current_attr = mark_safe(' aria-current="true"') if is_active else ""
    return format_html(
        '<a href="{}" class="{}"{}>{}</a>', href, mark_safe(css), current_attr, label or value
    )


@register.filter
def swatch_style(color) -> str:
    """An inline background style for a colour swatch, when the colour defines one.

    Colours are seeded with plain names ("Black", "Sand") and some carry a hex for the swatch dot.
    Anything unrecognised renders as a neutral ring rather than an invisible dot, so the option is
    still selectable and still legible.
    """
    hex_code = getattr(color, "hex_code", "") or ""
    if not hex_code:
        return "background-color: rgb(226 232 240);"
    return f"background-color: {hex_code};"


@register.filter
def get_item(mapping, key):
    """``{{ matrix.variants|get_item:key }}`` -- dict lookup by variable key.

    The variant matrix is keyed by ``(color_id, size_id)`` so templates can ask "does this
    combination exist?" with one lookup instead of a loop over every variant.
    """
    try:
        return mapping.get(key)
    except AttributeError:
        return None
