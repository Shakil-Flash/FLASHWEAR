"""Email delivery seam (Phase 17 §18).

One module owns *how* an email leaves the building, so swapping Django's SMTP path for a
hosted provider (SES, Postmark, Mailgun) later touches this file and nothing above it --
the same trade the account mailer already makes, now with a provider object behind it so
provider-specific failure classes (rate limits, permanent bounces) map onto delivery
statuses in one place.

Contract:

* ``send()`` returning normally means the **provider accepted** the message. Nothing here
  claims the customer read it, and nothing ever writes ``DELIVERED`` -- a fabricated
  delivery state is a lie the support desk would eventually act on;
* ``EmailSendError`` is transient by default (retry with backoff);
  ``PermanentEmailError`` means retrying cannot help (bad address, unknown recipient) and
  the dispatcher fails the row immediately;
* rendering happens outside the provider (``services/templates.py``), so provider errors
  and template errors are distinguishable -- a missing template is a deploy bug, never a
  reason to retry for a day.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger("flashwear.notifications.email")

__all__ = [
    "DjangoEmailProvider",
    "EmailProvider",
    "EmailSendError",
    "EmailSendResult",
    "PermanentEmailError",
    "PermanentTemplateError",
    "get_email_provider",
    "retry_failed",
]


class EmailSendError(Exception):
    """Transient delivery failure -- the dispatcher will retry with backoff."""


class PermanentEmailError(Exception):
    """Non-retryable failure (e.g. the recipient address is unacceptable)."""


class PermanentTemplateError(Exception):
    """Rendering failed because a template is missing or broken: a deploy bug."""


@dataclass(frozen=True)
class EmailSendResult:
    """What the provider reported. ``accepted`` never means "delivered"."""

    accepted: int
    provider: str


class EmailProvider(ABC):
    """The swap point for hosted providers."""

    name = "abstract"

    @abstractmethod
    def send(
        self,
        *,
        subject: str,
        text_body: str,
        html_body: str,
        recipient: str,
        from_email: str | None = None,
    ) -> EmailSendResult: ...


class DjangoEmailProvider(EmailProvider):
    """``django.core.mail``: console in development, locmem in tests, SMTP in production.

    A thin, dependency-free default so Phase 17 needs no external service to run end to
    end; a hosted provider is a second ``EmailProvider`` subclass plus a settings switch.
    """

    name = "django"

    def send(
        self,
        *,
        subject: str,
        text_body: str,
        html_body: str,
        recipient: str,
        from_email: str | None = None,
    ) -> EmailSendResult:
        recipient = (recipient or "").strip()
        if "@" not in recipient:
            raise PermanentEmailError("Recipient address is not usable.")
        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=from_email or settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        message.attach_alternative(html_body, "text/html")
        try:
            accepted = message.send(fail_silently=False)
        except Exception as exc:  # SMTP hiccups, connection resets, provider rate limits
            raise EmailSendError(str(exc) or exc.__class__.__name__) from exc
        if not accepted:
            # The backend swallowed the message (rare, but a count of zero means nothing
            # was handed over) -- treat it as transient so the sweeper can try again.
            raise EmailSendError("Mail backend accepted no messages.")
        return EmailSendResult(accepted=accepted, provider=self.name)


def get_email_provider() -> EmailProvider:
    """Resolve the provider from ``NOTIFICATIONS_EMAIL_PROVIDER`` (default: django)."""
    name = getattr(settings, "NOTIFICATIONS_EMAIL_PROVIDER", "django") or "django"
    if name == "django":
        return DjangoEmailProvider()
    raise ValueError(f"Unknown email provider: {name!r}")


def retry_failed(notification_id: int) -> bool:
    """Put one failed email row back on the queue. Returns whether it moved.

    The Back Office retry (Phase 17 §22) is idempotent by construction: only a row that
    is ``failed`` right now can move, via a conditional UPDATE that loses any race with a
    second operator; ``attempts`` resets to zero so this authorized retry is a fresh
    bounded budget rather than one instant away from the cap; and the task is scheduled
    after commit, exactly like the dispatcher's own queue writes.
    """
    from django.db import transaction
    from django.utils import timezone

    from apps.notifications.models import Notification, NotificationStatus

    moved = Notification.objects.filter(
        pk=notification_id,
        channel="email",
        status=NotificationStatus.FAILED,
    ).update(
        status=NotificationStatus.QUEUED,
        attempts=0,
        failure_reason="",
        failed_at=None,
        updated_at=timezone.now(),
    )
    if not moved:
        return False
    from apps.notifications.tasks import send_email

    transaction.on_commit(lambda: send_email.delay(notification_id))
    return True
