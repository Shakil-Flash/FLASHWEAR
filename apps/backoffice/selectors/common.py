"""Time ranges, allowlisted sorting and pagination -- shared by every back-office list.

The three things every internal screen needs, in one place so they behave identically:

* :func:`resolve_range` turns ``?range=7d`` into timezone-aware boundaries built in the
  *current* timezone (``TIME_ZONE``), never naive datetimes and never UTC-shifted dates;
* :func:`sort_queryset` accepts only keys from an allowlist the caller supplies, because
  ordering by a user-supplied expression is how an ORM becomes an injection surface;
* :func:`paginate` applies the single configured page size and clamps the page number, so
  a list can never ask for the whole table.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import NamedTuple

from django.core.paginator import Paginator
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

__all__ = ["RANGES", "DateRange", "paginate", "resolve_range", "sort_queryset"]


class DateRange(NamedTuple):
    """An inclusive start and an **exclusive** end, both aware (or ``None`` for all time)."""

    key: str
    label: str
    start: datetime | None
    end: datetime | None

    @property
    def is_all_time(self) -> bool:
        return self.start is None and self.end is None

    @property
    def as_query(self) -> dict:
        """The ``created_at__range`` kwargs for this window (empty for all time)."""
        return self.window("created_at")

    def window(self, field: str) -> dict:
        """``{field}__gte, {field}__lt}`` kwargs for this window (empty for all time)."""
        if self.is_all_time:
            return {}
        query: dict = {}
        if self.start is not None:
            query[f"{field}__gte"] = self.start
        if self.end is not None:
            query[f"{field}__lt"] = self.end
        return query


#: Offered by every date filter, in menu order.
RANGES = (
    ("today", _("Today")),
    ("yesterday", _("Yesterday")),
    ("7d", _("Last 7 days")),
    ("30d", _("Last 30 days")),
    ("this_month", _("This month")),
    ("prev_month", _("Previous month")),
    ("all", _("All time")),
)

_RANGE_KEYS = {key for key, _label in RANGES} | {"custom"}


def _aware(day: date, at: time) -> datetime:
    """A naive *local* wall clock time, made aware in the current timezone."""
    return timezone.make_aware(datetime.combine(day, at))


def _parse_date(value: str | date | None) -> date | None:
    """Safely parse a date instance or ISO YYYY-MM-DD string."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except (ValueError, AttributeError):
            return None
    return None


def resolve_range(
    key: str | None,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> DateRange:
    """Map a range key to its boundaries. Unknown keys fall back to the default (7 days).

    Days are local calendar days: "today" is today where the shop operates, which is what
    an operator asking "orders today" means. ``end`` is exclusive so consecutive windows
    do not double-count a midnight row.

    When ``start_date`` and ``end_date`` are provided (or ``key == 'custom'``), boundaries
    span from start 00:00:00 to end + 1 day 00:00:00 (exclusive).
    """
    labels = dict(RANGES)

    parsed_start = _parse_date(start_date)
    parsed_end = _parse_date(end_date)
    if parsed_start and parsed_end:
        if parsed_start > parsed_end:
            parsed_start, parsed_end = parsed_end, parsed_start
        s_dt = _aware(parsed_start, time.min)
        e_dt = _aware(parsed_end + timedelta(days=1), time.min)
        return DateRange(
            "custom", f"{parsed_start.isoformat()} to {parsed_end.isoformat()}", s_dt, e_dt
        )

    chosen = key if key in _RANGE_KEYS and key != "custom" else "7d"
    today = timezone.localtime().date()

    if chosen == "all":
        return DateRange("all", labels["all"], None, None)
    if chosen == "today":
        start, end = today, today + timedelta(days=1)
    elif chosen == "yesterday":
        start, end = today - timedelta(days=1), today
    elif chosen == "7d":
        start, end = today - timedelta(days=6), today + timedelta(days=1)
    elif chosen == "30d":
        start, end = today - timedelta(days=29), today + timedelta(days=1)
    elif chosen == "this_month":
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    else:  # prev_month
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)

    return DateRange(chosen, labels[chosen], _aware(start, time.min), _aware(end, time.min))


def paginate(request: HttpRequest, queryset: QuerySet, *, per_page: int | None = None):
    """One page of ``queryset`` with the configured (and clamped) page size."""
    from django.conf import settings

    size = per_page or settings.BACKOFFICE_PAGE_SIZE
    page_number = request.GET.get("page", "1") if request is not None else "1"
    return Paginator(queryset, size).get_page(page_number)


def sort_queryset(
    queryset: QuerySet,
    requested: str | None,
    allowed: dict[str, list[str]],
    default: str,
) -> tuple[QuerySet, str]:
    """Order ``queryset`` by an allowlisted key.

    ``allowed`` maps a URL-safe key (``-created_at``) to the ORM ordering it expands to.
    Anything else -- including an empty string -- resolves to ``default``, so a crafted
    ``?sort=...`` can neither inject an expression nor produce an unordered page.
    """
    key = requested or ""
    if key not in allowed:
        key = default
    return queryset.order_by(*allowed[key]), key
