"""Backoffice template tags and status badge presentation."""

from __future__ import annotations

from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def status_badge(status, label: str | None = None) -> str:
    """Render a unified, accessible status badge for backoffice tables and screens.

    Styles are mapped to consistent semantics:
    * Emerald: Active, Published, Completed, In Stock, Paid, Approved, Succeeded
    * Amber: Pending, Processing, Low Stock, Waiting, Under Review, Requested
    * Rose: Failed, Cancelled, Archived, Out of Stock, Rejected, Urgent, Suspended
    * Sky: Shipped, Dispatched, In Transit
    * Slate: Draft, Inactive, Closed, Default
    """
    if not status and not label:
        return ""

    display_text = label if label is not None else str(status)
    s = str(status).lower().strip().replace("-", "_")

    if s in (
        "active",
        "published",
        "delivered",
        "completed",
        "in_stock",
        "instock",
        "paid",
        "approved",
        "succeeded",
        "success",
        "resolved",
    ):
        badge_cls = (
            "border-emerald-200/80 bg-emerald-50 text-emerald-700 "
            "dark:border-emerald-800/60 dark:bg-emerald-950/40 dark:text-emerald-300"
        )
        dot_cls = "bg-emerald-500"
    elif s in (
        "pending",
        "processing",
        "low_stock",
        "lowstock",
        "waiting",
        "under_review",
        "open",
        "requested",
        "partial",
        "warning",
    ):
        badge_cls = (
            "border-amber-200/80 bg-amber-50 text-amber-700 "
            "dark:border-amber-800/60 dark:bg-amber-950/40 dark:text-amber-300"
        )
        dot_cls = "bg-amber-500"
    elif s in (
        "failed",
        "cancelled",
        "canceled",
        "archived",
        "out_of_stock",
        "outofstock",
        "out",
        "rejected",
        "urgent",
        "suspended",
        "expired",
        "danger",
        "error",
    ):
        badge_cls = (
            "border-rose-200/80 bg-rose-50 text-rose-700 "
            "dark:border-rose-800/60 dark:bg-rose-950/40 dark:text-rose-300"
        )
        dot_cls = "bg-rose-500"
    elif s in ("shipped", "dispatched", "in_transit", "transit", "info"):
        badge_cls = (
            "border-sky-200/80 bg-sky-50 text-sky-700 "
            "dark:border-sky-800/60 dark:bg-sky-950/40 dark:text-sky-300"
        )
        dot_cls = "bg-sky-500"
    else:
        badge_cls = (
            "border-slate-200 bg-slate-100 text-slate-700 "
            "dark:border-slate-700 dark:bg-slate-800/80 dark:text-slate-300"
        )
        dot_cls = "bg-slate-400 dark:bg-slate-500"

    return format_html(
        '<span class="inline-flex items-center gap-1.5 rounded-full border '
        'px-2.5 py-0.5 text-xs font-medium {}">'
        '<span class="h-1.5 w-1.5 rounded-full {}" aria-hidden="true"></span>'
        "{}"
        "</span>",
        badge_cls,
        dot_cls,
        display_text,
    )
