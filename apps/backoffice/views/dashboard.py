"""Dashboard and alerts (Phase 16).

The dashboard is a read of the whole shop: every tile links to a screen that shows the
rows behind the number, so nothing on it is a figure an operator cannot trace. The alerts
page runs the same deterministic rules and adds nothing of its own -- one rule set, two
places to read it.
"""

from __future__ import annotations

from apps.backoffice.permissions import OPS_VIEW, backoffice_access
from apps.backoffice.selectors.dashboard import dashboard_summary
from apps.backoffice.services.alerts import SEVERITY_LABELS, alerts_for
from apps.backoffice.views.base import range_from_request, render_bo

__all__ = ["alerts", "dashboard"]


@backoffice_access(OPS_VIEW)
def dashboard(request):
    """``/operations/`` -- the shop's numbers for the chosen window."""
    from apps.orders.models import Order

    date_range = range_from_request(request)
    summary = dashboard_summary(date_range=date_range)
    # Labels are resolved here, not in the template: a template cannot look a dict up by
    # a variable key without a bespoke filter, and a bespoke filter is where a typo hides.
    pipeline = [
        {"code": code, "label": label, "count": summary["pipeline"].get(code, 0)}
        for code, label in Order.Status.choices
    ]
    return render_bo(
        request,
        "backoffice/dashboard.html",
        active="dashboard",
        summary=summary,
        alerts=alerts_for(request.user),
        current_range=date_range.key,
        pipeline=pipeline,
    )


@backoffice_access(OPS_VIEW)
def alerts(request):
    """``/operations/alerts/`` -- every rule that fired, filtered by severity if asked."""
    requested = request.GET.get("severity", "")
    severity = requested if requested in SEVERITY_LABELS else ""
    fired = alerts_for(request.user)
    shown = [alert for alert in fired if alert.severity == severity] if severity else fired
    return render_bo(
        request,
        "backoffice/alerts.html",
        active="alerts",
        alerts=shown,
        severity=severity,
        severity_labels=SEVERITY_LABELS,
        # The dashboard renders the same list; this page adds a count and a filter.
        total=len(fired),
    )
