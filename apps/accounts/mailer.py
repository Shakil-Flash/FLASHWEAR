"""Transactional email seam for account messages.

Every account email goes through :func:`send_account_email`, which renders a branded text +
HTML pair from ``templates/accounts/email/``. That gives two things the project needs:

* one place to swap in a hosted provider (SES, Postmark, Mailgun) later -- implement a delivery
  backend or replace this module, nothing above it changes;
* one place where the base URL is resolved, so no template hard-codes a domain.

Nothing here ever receives a password or a raw token except the token-bearing templates, which
receive an already-built verification URL.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.http import HttpRequest
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils.html import linebreaks, strip_tags

logger = logging.getLogger("flashwear.accounts.mailer")

EMAIL_TEMPLATE_DIR = "accounts/email"


def resolve_base_url(request: HttpRequest | None = None) -> str:
    """Return the absolute base URL to build account links from.

    An explicit ``ACCOUNT_EMAIL_BASE_URL`` always wins. Without one the request's host is used,
    which is correct for a single-host deployment and for the console backend, but wrong the
    moment mail is sent from a worker that has no request -- hence the setting.
    """
    configured = getattr(settings, "ACCOUNT_EMAIL_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    if request is not None:
        return request.build_absolute_uri("/").rstrip("/")
    site = getattr(settings, "SITE_URL", "")
    return str(site).rstrip("/")


def absolute_url(path: str, request: HttpRequest | None = None) -> str:
    """Turn a site-relative path into an absolute URL for use in an email."""
    base = resolve_base_url(request)
    if path.startswith(("http://", "https://")):
        return path
    return f"{base}/{path.lstrip('/')}" if base else f"/{path.lstrip('/')}"


def _render_template(name: str, context: dict[str, Any]) -> str:
    try:
        return render_to_string(name, context)
    except TemplateDoesNotExist:
        # A missing branded template is a deployment mistake, not a runtime condition to hide.
        logger.error("Account email template missing: %s", name)
        raise


def send_account_email(
    *,
    subject_template: str,
    text_template: str,
    html_template: str,
    context: dict[str, Any],
    recipient: str,
    request: HttpRequest | None = None,
    fail_silently: bool = False,
) -> int:
    """Render and send one account email.

    ``subject_template`` is a template too, so a subject can interpolate the recipient's name.
    The text part is the source of truth; the HTML part is derived from it when the branded
    template is absent, which keeps a missing template from silently dropping the message.
    """
    email_context = {**context, "base_url": resolve_base_url(request)}
    subject = _render_template(subject_template, email_context).strip()
    text_body = _render_template(text_template, email_context)

    try:
        html_body = _render_template(html_template, email_context)
    except TemplateDoesNotExist:
        html_body = f"<p>{linebreaks(strip_tags(text_body))}</p>"
        logger.warning("Falling back to a plain HTML body for %s", html_template)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
    )
    message.attach_alternative(html_body, "text/html")
    sent = message.send(fail_silently=fail_silently)
    logger.info("Account email %r queued to %s", subject_template, Path(text_template).stem)
    return sent
