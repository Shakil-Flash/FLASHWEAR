"""Error monitoring hook.

A provider-agnostic seam: when a monitoring SDK is installed and configured
(``SENTRY_DSN`` + ``sentry-sdk`` on the path) errors are forwarded to it; otherwise
every capture degrades to a structured log line on ``flashwear.monitoring``, which
the file handlers already persist. Either way the project has one call site --
``monitoring.capture_exception(exc)`` -- so wiring a real provider later is a
settings change, not a code change.

Hooked automatically in ``CoreConfig.ready()``:

* Django: ``got_request_exception`` -- every unhandled view/middleware exception.
* Celery: ``task_failure`` -- every failed task, with task name and retry state.

Both handlers also feed the operational counters (``apps.core.metrics``) that
back the Back Office health panel.
"""

from __future__ import annotations

import logging

from apps.core import metrics

logger = logging.getLogger("flashwear.monitoring")

__all__ = ["capture_exception", "capture_message", "configure", "configured_provider"]

_provider = "logging"
_configured = False


def configured_provider() -> str:
    """``"sentry"`` once an SDK is active, otherwise ``"logging"``."""
    return _provider


def configure() -> None:
    """Activate the SDK if the environment asks for one. Idempotent.

    Called from ``CoreConfig.ready()``; never raises -- a broken monitoring vendor
    must not stop the process from booting.
    """
    global _provider, _configured
    if _configured:
        return
    _configured = True
    from django.conf import settings

    dsn = getattr(settings, "SENTRY_DSN", "")
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            dsn=dsn,
            integrations=[DjangoIntegration(), CeleryIntegration()],
            traces_sample_rate=getattr(settings, "SENTRY_TRACES_SAMPLE_RATE", 0.0),
            send_default_pii=False,
        )
        _provider = "sentry"
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed; "
            "errors will be logged instead. pip install sentry-sdk."
        )
    except Exception:  # a monitoring vendor must never break boot
        logger.exception("monitoring provider failed to initialise; falling back to logs")


def capture_exception(exc: BaseException, **context) -> None:
    """Report an exception. Never raises."""
    try:
        if _provider == "sentry":
            import sentry_sdk

            with sentry_sdk.configure_scope() as scope:
                for key, value in context.items():
                    scope.set_tag(key, value)
            sentry_sdk.capture_exception(exc)
            return
        logger.error(
            "unhandled exception: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=exc,
            extra={"monitoring": True, **context},
        )
    except Exception:  # telemetry must never raise
        pass


def capture_message(message: str, level: str = "error", **context) -> None:
    """Report a non-exception event. Never raises."""
    try:
        if _provider == "sentry":
            import sentry_sdk

            sentry_sdk.capture_message(message, level=level)
            return
        log = logger.error if level in {"error", "fatal"} else logger.warning
        log(message, extra={"monitoring": True, **context})
    except Exception:  # telemetry must never raise
        pass


# --------------------------------------------------------------------------------------
# Signal wiring (connected from CoreConfig.ready)
# --------------------------------------------------------------------------------------


def _on_request_exception(sender, request=None, exception=None, **kwargs):
    if exception is not None:
        request_id = getattr(request, "request_id", "-") if request is not None else "-"
        capture_exception(exception, request_id=request_id)


def _on_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, **_):
    metrics.mark_task(task_name_of(sender), ok=False)
    if exception is not None:
        capture_exception(exception, task=str(task_name_of(sender)), task_id=task_id)


def task_name_of(sender) -> str:
    """A Celery signal's ``sender`` is the task class; fall back to something safe."""
    name = getattr(sender, "name", None) or getattr(sender, "__name__", None)
    return name or "unknown"


def connect_signals() -> None:
    """Idempotent wiring of the Django and Celery failure signals."""
    from celery.signals import task_failure, task_success
    from django.core.signals import got_request_exception

    got_request_exception.connect(_on_request_exception, dispatch_uid="flashwear.monitoring.http")
    task_failure.connect(_on_task_failure, dispatch_uid="flashwear.monitoring.task_failure")

    def _on_task_success(sender=None, **kwargs):
        metrics.mark_task(task_name_of(sender), ok=True)

    task_success.connect(_on_task_success, dispatch_uid="flashwear.monitoring.task_success")
