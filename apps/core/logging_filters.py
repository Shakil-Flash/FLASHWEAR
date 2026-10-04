"""Sensitive-data redaction for application logs.

Never log raw credentials, tokens or customer contact details. Redaction is implemented
as a *formatter* rather than a filter: filters must mutate ``record.msg``/``record.args``
in place, which breaks libraries that log with mapping-style args (Celery does this).

Usage::

    "formatters": {
        "redacted": {"()": "apps.core.logging_filters.RedactingFormatter"},
    }
"""

from __future__ import annotations

import logging
import re

# Ordered, non-overlapping patterns. Each pattern must consume the sensitive value so
# nothing leaks into the output. The last capture group is always the secret itself.
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # key=value / key: value pairs. ``[_-]?`` prefixed keys matter: an underscore is a
    # word character, so ``payment_secret`` must be matched as one token, not ``secret``.
    re.compile(
        r"(?i)\b([\w.-]*"
        r"(?:password|passwd|pwd|token|secret|api[_-]?key|private[_-]?key"
        r"|credential|signature|salt|otp|pin)"
        r")(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
    ),
    # HTTP header style: Authorization: Bearer <token> (keep the scheme, drop the value).
    re.compile(r"(?i)((?:proxy-)?authorization(\s*:\s*)(?:bearer|basic|token|digest)\s+)(\S+)"),
    # Cookies carrying session/auth state
    re.compile(r"(?i)\b(set-cookie|cookie)(\s*:\s*)((?:sessionid|csrftoken|auth|jwt)[^;\r\n]*)"),
    # Email addresses (customer PII)
    re.compile(r"(?i)\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # Provider-style prefixed credentials (Stripe, Paystack, OpenAI, ...)
    re.compile(r"\b(?:sk|pk|rk|ps)_(?:live|test)_[A-Za-z0-9]{6,}\b"),
    # Long opaque blobs that are almost certainly tokens
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
)


def redact(message: str) -> str:
    """Replace credential-like substrings with ``[REDACTED]``."""
    if not message:
        return message

    redacted = message
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(_redaction_replacement, redacted)
    return redacted


def _redaction_replacement(match: re.Match[str]) -> str:
    """Rebuild the match, keeping non-sensitive context intact."""
    groups = match.groups()
    # The final group is always the sensitive value.
    prefix = "".join(group or "" for group in groups[:-1])
    return f"{prefix}[REDACTED]"


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive values after interpolation.

    Interpolating first (via the parent class) means we redact the final rendered
    string, which is correct for every logging style: positional args, mapping args
    and ``extra=`` context alike.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
