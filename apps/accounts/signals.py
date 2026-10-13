"""Signal handlers for the accounts app.

Kept separate from :mod:`apps.accounts.models` so the model module stays declarative.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger("flashwear.accounts")


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="accounts_create_profile")
def ensure_profile(sender, instance, created: bool, **kwargs) -> None:
    """Give every user a profile row.

    Doing it in a signal rather than in the registration service means users created by the
    admin, a data import or a future API endpoint cannot end up without one. ``get_or_create``
    keeps it idempotent for the ``save()``-after-``create()`` paths Django itself uses.
    """
    from apps.accounts.models import Profile

    if created:
        Profile.objects.get_or_create(user=instance)
    elif not Profile.objects.filter(user=instance).exists():
        Profile.objects.create(user=instance)
        logger.info("Backfilled missing profile for user_id=%s", instance.pk)


@receiver(user_logged_in, dispatch_uid="accounts_sync_preferred_language")
def sync_language_on_login(sender, request, user, **kwargs) -> None:
    """Activate customer's saved preferred language upon sign-in."""
    from django.utils import translation

    if request is not None:
        profile = getattr(user, "profile", None)
        if profile and profile.preferred_language:
            lang = profile.preferred_language
            if translation.check_for_language(lang):
                translation.activate(lang)
                request.LANGUAGE_CODE = lang
