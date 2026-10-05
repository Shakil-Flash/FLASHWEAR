"""Notification services (Phase 17).

Deliberately import-light: this package's ``__init__`` must never pull in the dispatcher,
because :mod:`apps.notifications.tasks` imports rendering services from here while the
dispatcher schedules those very tasks -- a cycle waiting to bite. Call sites import the
module they need::

    from apps.notifications.services.events import emit   # domain code (safe wrapper)
    from apps.notifications.services import dispatcher     # tests (strict)

Layering, top to bottom: ``events`` -> ``dispatcher`` -> {``templates``, ``preferences``,
``in_app``} -> ``models``. ``tasks`` sits beside the dispatcher and is imported lazily
from it. ``email`` knows only the provider seam. Nothing below imports a domain app.
"""
