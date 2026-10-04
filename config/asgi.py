"""ASGI entrypoint (uvicorn ``config.asgi:application``).

Used when deploying behind an async stack or when the future mobile/websocket features
need persistent connections.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

application = get_asgi_application()
