"""Payment endpoints: the webhook every provider talks to, and local controls.

Two surfaces:

* ``/payments/webhook/<provider>/`` -- the only place an outside event enters the
  system. Signature first, parse second, apply once, answer 4xx on anything that
  will never apply (so the provider stops retrying).
* ``/payments/<id>/simulate|cancel`` -- development-only buttons on the payment
  page. ``simulate`` builds the same event object a webhook would deliver and
  feeds it through the same service, so the local path and the remote path are
  one code path.
"""

from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.payments.models import Payment
from apps.payments.providers import InvalidSignature, WebhookError, get_provider, provider_name
from apps.payments.providers.development import make_provider_event
from apps.payments.services import cancel_payment, handle_provider_event, start_payment

logger = logging.getLogger(__name__)

__all__ = ["cancel", "simulate", "webhook"]

_SIMULATABLE = {Payment.Status.SUCCEEDED, Payment.Status.FAILED, Payment.Status.PENDING}


@csrf_exempt
@require_POST
def webhook(request, provider_key: str):
    """Authenticate and apply one provider event."""
    if provider_key != provider_name():
        return JsonResponse({"detail": "Unknown provider."}, status=404)

    provider = get_provider()
    try:
        event = provider.verify_webhook(request)
    except InvalidSignature as err:
        logger.warning("Webhook signature rejected: %s", err)
        return JsonResponse({"detail": str(err)}, status=401)
    except WebhookError as err:
        return JsonResponse({"detail": str(err)}, status=400)

    try:
        record = handle_provider_event(provider_key, event)
    except WebhookError as err:
        # Unknown reference, amount mismatch or an impossible transition: 409 so
        # the provider knows the retry will fail identically.
        logger.warning("Webhook rejected: %s", err)
        return JsonResponse({"detail": str(err)}, status=409)
    return JsonResponse({"received": True, "event": record.event_id}, status=200)


@login_required
@require_POST
def simulate(request, pk: int):
    """Development only: fire a provider event for the customer's own payment."""
    if provider_name() != "development":
        raise Http404
    payment = get_object_or_404(Payment, pk=pk, order__user=request.user)
    outcome = request.POST.get("outcome", "")

    if outcome not in _SIMULATABLE:
        return JsonResponse({"detail": _("Unknown outcome.")}, status=400)
    if payment.status == Payment.Status.CREATED:
        payment = start_payment(payment.order)
    if payment.is_settled:
        # Already decided (double click / back button): show the result instead.
        return redirect("shop:checkout-done", number=payment.order.number)

    handle_provider_event(
        provider_name(),
        make_provider_event(payment, outcome, reason=request.POST.get("reason", "")),
    )
    return redirect("shop:checkout-done", number=payment.order.number)


@login_required
@require_POST
def cancel(request, pk: int):
    """Development-facing cancel button: provider cancel + order cancellation."""
    if provider_name() != "development":
        raise Http404
    payment = get_object_or_404(Payment, pk=pk, order__user=request.user)
    cancel_payment(payment, actor=request.user, reason="Cancelled at the payment page.")
    return redirect("shop:checkout-done", number=payment.order.number)
