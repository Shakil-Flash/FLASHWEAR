"""The analytics funnel screen (Phase 20).

Aggregates only: the selector returns counts, rates and top lists -- never a visitor id,
session key, IP or any other per-person row -- so the capability ``analytics.view`` is all
a staff member needs and nothing here can be used to read an individual customer's path.
"""

from __future__ import annotations

from apps.backoffice.permissions import ANALYTICS_VIEW, backoffice_access
from apps.backoffice.selectors.analytics import funnel_summary
from apps.backoffice.views.base import range_from_request, render_bo

__all__ = ["analytics"]


@backoffice_access(ANALYTICS_VIEW)
def analytics(request):
    """``/operations/analytics/`` -- visitors, funnel stages, top products and searches."""
    date_range = range_from_request(request)
    return render_bo(
        request,
        "backoffice/analytics.html",
        active="analytics",
        summary=funnel_summary(date_range=date_range),
        current_range=date_range.key,
    )
