"""Visitor identity + attribution capture.

Two jobs, both cheap and both read-only for the vast majority of requests:

1. **Visitor id** -- a random first-party UUID cookie (``fw_visitor``, HttpOnly, one year).
   Anonymous shoppers who never open a cart still get an identity the funnel can count;
   nothing about a person is stored, only an opaque id. When the cookie already exists the
   middleware does nothing at all.
2. **Attribution** -- UTM parameters, an *external* referrer and the landing path are parsed
   into ``request.analytics_attribution`` on every request (no I/O). They are persisted only
   when there is something worth writing: a campaign landing, or a visitor we have not seen
   before (first touch). The read happens here, once per request, so the event service can
   reuse it.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

from django.db import IntegrityError

from apps.analytics.models import VisitorAttribution

logger = logging.getLogger("flashwear.analytics")

COOKIE_NAME = "fw_visitor"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # one year

UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")

#: Attribution is meaningless on machine surfaces -- never write rows for them.
_SKIP_PREFIXES = ("/api/", "/static/", "/media/", "/operations/", "/admin/", "/payments/")


def parse_attribution(request) -> dict:
    """UTM values, external referrer and landing path for this request (no I/O)."""
    data = {field: (request.GET.get(field) or "")[:120] for field in UTM_FIELDS}
    referrer = request.META.get("HTTP_REFERER", "") or ""
    # A referrer from our own host is navigation, not attribution.
    if referrer and urlparse(referrer).netloc == request.get_host():
        referrer = ""
    data["referrer"] = referrer[:500]
    data["landing_page"] = request.get_full_path()[:500]
    return data


def attribution_from_request(request) -> dict:
    """The defaults dict for :class:`~apps.analytics.models.VisitorAttribution`."""
    parsed = getattr(request, "analytics_attribution", None)
    if parsed is None:
        parsed = parse_attribution(request)
    return parsed


def _has_campaign(data: dict) -> bool:
    return any(data.get(field) for field in UTM_FIELDS)


def _persist(request) -> bool:
    """Persist a campaign touch; ``True`` when we know the attribution row exists.

    Organic visitors are deliberately *not* written here -- their row appears with their
    first tracked event (``services.touch_attribution``), so a plain page view costs zero
    queries. A campaign landing is worth two queries because UTMs decay: written on any
    later touch, they are gone.

    No ``get_or_create`` here on purpose: its atomic retry block costs two savepoint
    statements per first visit, and this sits on every storefront GET.
    """
    visitor_id = getattr(request, "analytics_visitor_id", "") or ""
    if not visitor_id:
        return False
    data = attribution_from_request(request)
    if not _has_campaign(data):
        return False
    row = VisitorAttribution.objects.filter(visitor_id=visitor_id).first()
    if row is None:
        try:
            VisitorAttribution.objects.create(visitor_id=visitor_id, **data)
        except IntegrityError:
            # Lost a harmless race with a parallel request; that row is equally good.
            return False
        return True
    for field in UTM_FIELDS:
        setattr(row, field, data[field])
    row.save(update_fields=[*UTM_FIELDS, "last_touch_at"])
    return True


def _should_persist(request) -> bool:
    if request.method != "GET":
        return False
    path = request.path
    return not any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


class AnalyticsMiddleware:
    """Attach ``request.analytics_visitor_id`` and persist attribution when worth it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        visitor_id = request.COOKIES.get(COOKIE_NAME, "")
        minted = False
        if not visitor_id or len(visitor_id) > 64:
            visitor_id = uuid.uuid4().hex
            minted = True
        request.analytics_visitor_id = visitor_id
        request.analytics_attribution = parse_attribution(request)

        if _should_persist(request):
            try:
                if _persist(request):
                    # The row is proven to exist; the event service can skip its read.
                    request._fw_attribution_ready = True
            except Exception:  # pragma: no cover - analytics must never break a page
                logger.exception("analytics: attribution persistence failed")

        response = self.get_response(request)

        if minted:
            response.set_cookie(
                COOKIE_NAME,
                visitor_id,
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                samesite="Lax",
                secure=bool(getattr(request, "is_secure", lambda: False)()),
            )
        return response
