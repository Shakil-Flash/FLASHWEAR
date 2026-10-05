"""Request correlation, access logging, metrics and security headers.

``RequestContextMiddleware`` is first in the stack so its correlation id is present
in every log line that follows, including exceptions raised by later middleware or
by a view. ``ContentSecurityPolicyMiddleware`` renders the policy declared in
settings, so no view can forget the header.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from apps.core import metrics
from apps.core.logging_filters import request_id_var

__all__ = ["ContentSecurityPolicyMiddleware", "RequestContextMiddleware"]

access_logger = logging.getLogger("flashwear.access")

# A correlation id we are willing to echo back: our own ids are hex UUIDs, but an
# upstream proxy or client may supply one, and adopting it correlates both sides.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestContextMiddleware:
    """Assign a request id, time the request, emit one access-log line.

    * adopt or mint ``X-Request-ID`` and publish it to the context variable the JSON
      formatter reads, so web logs and task logs can be joined on one id;
    * echo the id on the response for support ("quote the header on your ticket");
    * record duration/status counters for the Back Office health panel;
    * emit exactly one access-log line per response (ERROR for 5xx, WARNING for
      4xx, INFO otherwise).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.META.get("HTTP_X_REQUEST_ID", "")
        request.request_id = incoming if _REQUEST_ID_PATTERN.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request.request_id)
        started = time.monotonic()
        try:
            # Django converts view exceptions into 500 responses below this layer,
            # so ``response`` is the single outcome unless middleware itself is
            # broken -- in which case the counters simply miss one request.
            response = self.get_response(request)
            duration_ms = int((time.monotonic() - started) * 1000)
            response["X-Request-ID"] = request.request_id
            self._record(request, response, duration_ms)
            return response
        finally:
            request_id_var.reset(token)

    @staticmethod
    def _record(request: HttpRequest, response: HttpResponse, duration_ms: int) -> None:
        status = response.status_code
        metrics.mark_request(status, duration_ms)
        level = (
            logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        )
        user = getattr(request, "user", None)
        access_logger.log(
            level,
            "%s %s -> %s (%dms)",
            request.method,
            request.get_full_path(),
            status,
            duration_ms,
            extra={
                "request_id": request.request_id,
                "method": request.method,
                "path": request.path,
                "status": status,
                "duration_ms": duration_ms,
                "user_id": user.pk if user is not None and user.is_authenticated else None,
            },
        )


def render_csp(directives: dict[str, list[str]]) -> str:
    """Flatten ``{"script-src": ["'self'"], "upgrade-insecure-requests": []}``."""
    parts = []
    for name, values in directives.items():
        parts.append(name if not values else f"{name} {' '.join(values)}")
    return "; ".join(parts)


class ContentSecurityPolicyMiddleware:
    """Attach the Content-Security-Policy header declared in settings.

    Whether the policy enforces or only reports is a settings decision
    (``CSP_REPORT_ONLY``), so a deployment can watch violations before blocking.
    A response that already carries a policy header keeps its own.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if response.has_header("Content-Security-Policy"):
            return response
        spec = getattr(settings, "CONTENT_SECURITY_POLICY", None) or {}
        policy = render_csp(spec.get("DIRECTIVES", {}))
        if not policy:
            return response
        header = (
            "Content-Security-Policy-Report-Only"
            if getattr(settings, "CSP_REPORT_ONLY", False)
            else "Content-Security-Policy"
        )
        response[header] = policy
        return response
