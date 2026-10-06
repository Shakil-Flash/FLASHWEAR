"""FLASHWEAR project package.

Importing the Celery application here guarantees that it is always available when
Django starts, whether the process is a web server, a worker or a management command.
"""

try:
    from config.celery import app as celery_app
except ImportError:
    celery_app = None  # type: ignore[assignment]

__all__ = ("celery_app",)
