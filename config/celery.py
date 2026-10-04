"""Celery application for FLASHWEAR.

Business tasks are added from Phase 2 onwards in ``<app>/tasks.py``; they are
auto-discovered by Django's ``INSTALLED_APPS``.

Run a worker with::

    celery -A config worker --loglevel=info
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("flashwear")

# All Celery settings live in Django settings under the CELERY_ namespace.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Loads <app>/tasks.py for every entry in INSTALLED_APPS.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:
    """Trivial task used to verify that the worker is wired up correctly."""
    return f"flashwear celery ok (id={self.request.id})"
