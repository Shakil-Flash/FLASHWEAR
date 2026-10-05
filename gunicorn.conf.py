"""Gunicorn configuration for the container runtime.

Referenced by the image CMD (``gunicorn --config gunicorn.conf.py config.wsgi:application``)
so an operator can size a container from the environment without rebuilding the image:

* ``PORT`` -- the bind port (8000; the Dockerfile EXPOSEs it).
* ``WEB_CONCURRENCY`` -- sync workers. Size it to the container's CPU allocation:
  each worker serves one request at a time, and every request here is a DB round trip.
* ``GUNICORN_TIMEOUT`` / ``GUNICORN_GRACEFUL_TIMEOUT`` -- seconds; the graceful value
  must exceed the longest plausible request so a deploy lets in-flight work finish.

No gunicorn imports: this module is plain data assignment, which keeps it importable
(and testable) outside a gunicorn process.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "3"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
capture_output = True

# Recycle workers after a request count so a slow leak cannot accumulate forever;
# the jitter stops every worker restarting in the same second.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = 100

# Worker heartbeat files on tmpfs in containers: no disk sync per heartbeat, and a
# hung worker is noticed on schedule rather than after a page-cache flush.
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None
