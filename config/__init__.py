"""FLASHWEAR project package.

Importing the Celery application here guarantees that it is always available when
Django starts, whether the process is a web server, a worker or a management command.
"""

from config.celery import app as celery_app

__all__ = ("celery_app",)
