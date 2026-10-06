"""The one place an analytics event is written.

Every caller goes through :func:`record_event`, which enforces the contracts the rest of the
system relies on:

* **allowlist** -- an unknown name is rejected before it reaches the table, so a typo in a
  call site is a loud log line, not a silently dead metric;
* **identity** -- user (authenticated), session key and the middleware's visitor id are
  resolved from the request in one place;
* **idempotency** -- callers pass a key for events that a retry could duplicate (one per
  order, checkout, ticket, quest completion); ``get_or_create`` makes the second call free;
* **privacy** -- metadata keys that look credential-shaped are scrubbed before the row is
  written, so a call site cannot leak a password or a card number into analytics even by
  accident;
* **never raises** -- analytics failing must never fail a shopper action or a payment.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import IntegrityError

from apps.analytics.middleware import attribution_from_request
from apps.analytics.models import Event, VisitorAttribution

logger = logging.getLogger("flashwear.analytics")

#: Metadata keys (lower-case, matched as substrings) that never belong in an event row.
_FORBIDDEN_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "card",
    "cvv",
    "csc",
    "pan",
    "credential",
    "cookie",
)


def _scrub(metadata: dict | None) -> dict:
    """Drop credential-shaped keys and coerce the rest to JSON-safe scalars."""
    if not metadata:
        return {}
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in _FORBIDDEN_KEY_MARKERS):
            logger.warning("analytics: dropped credential-shaped metadata key %r", key)
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)] = value
        elif isinstance(value, (list, tuple)):
            safe[str(key)] = [
                item for item in value if isinstance(item, (str, int, float, bool, type(None)))
            ]
        else:
            # Objects (model instances, Decimals, datetimes) are not JSON-stable and are
            # not worth a serializer here: the string form is enough for a metric.
            safe[str(key)] = str(value)
    return safe


def _identity(request, user=None) -> tuple:
    """``(user, visitor_id, session_key)`` resolved from the request once."""
    resolved_user = user
    visitor_id = ""
    session_key = ""
    if request is not None:
        if resolved_user is None:
            candidate = getattr(request, "user", None)
            if candidate is not None and getattr(candidate, "is_authenticated", False):
                resolved_user = candidate
        visitor_id = getattr(request, "analytics_visitor_id", "") or ""
        session = getattr(request, "session", None)
        if session is not None:
            session_key = session.session_key or ""
    return resolved_user, visitor_id, session_key


def touch_attribution(request, *, user=None) -> None:
    """Create the visitor's attribution row if it does not exist yet (first tracked event).

    Landing page and referrer come from the request the middleware already parsed. The
    middleware itself persists attribution for HTML GETs; this covers the remaining cases
    (POSTs, skipped paths) so an event can always be joined to a first touch. Best effort:
    never raises.
    """
    if request is None:
        return
    visitor_id = getattr(request, "analytics_visitor_id", "") or ""
    if not visitor_id:
        return
    if getattr(request, "_fw_attribution_ready", False):
        return
    try:
        defaults = attribution_from_request(request)
        defaults.setdefault("landing_page", request.path)
        resolved_user, _, _ = _identity(request, user)
        row = VisitorAttribution.objects.filter(visitor_id=visitor_id).first()
        if row is None:
            try:
                row = VisitorAttribution.objects.create(
                    visitor_id=visitor_id, user=resolved_user, **defaults
                )
            except IntegrityError:
                # Lost a harmless race with a parallel request; that row is equally good.
                request._fw_attribution_ready = True
                return
        elif resolved_user is not None and row.user_id is None:
            # Conditional UPDATE: first authenticated event claims the visitor, without
            # a race window and without a second SELECT.
            VisitorAttribution.objects.filter(pk=row.pk, user__isnull=True).update(
                user=resolved_user
            )
        request._fw_attribution_ready = True
    except Exception:  # pragma: no cover - defensive by contract
        logger.exception("analytics: attribution touch failed")


def record_event(
    name: str,
    *,
    request=None,
    user=None,
    object_type: str = "",
    object_id: int | None = None,
    metadata: dict | None = None,
    idempotency_key: str = "",
) -> Event | None:
    """Record one business event. Returns the row, or ``None`` when anything went wrong.

    ``idempotency_key`` is for events a retry could duplicate (checkout conversions,
    ticket creation, quest completions): the unique constraint makes the second write a
    no-op read instead of a second row.
    """
    if name not in Event.Name.values:
        logger.error("analytics: refused unknown event name %r", name)
        return None
    try:
        resolved_user, visitor_id, session_key = _identity(request, user)
        touch_attribution(request, user=resolved_user)
        payload = {
            "name": name,
            "user": resolved_user,
            "visitor_id": visitor_id,
            "session_key": session_key,
            "object_type": object_type or "",
            "object_id": object_id,
            "metadata": _scrub(metadata),
        }
        if idempotency_key:
            event, _ = Event.objects.get_or_create(
                idempotency_key=str(idempotency_key)[:200], defaults=payload
            )
            return event
        return Event.objects.create(**payload)
    except Exception:  # pragma: no cover - defensive by contract
        logger.exception("analytics: failed to record %s", name)
        return None
