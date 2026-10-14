"""Preference resolution (Phase 17 §14, Phase 51 §4).

The one place that answers "does this recipient want this channel for this category?".
Everything else -- dispatcher, bulk broadcast -- asks here, so the rules cannot drift:

* **mandatory categories win over stored rows** -- a tampered, buggy or migrated row cannot
  suppress an order, payment, delivery, support or account-security message;
* **optional categories read the stored row**, created lazily from the registry defaults so
  a customer who never opens the screen behaves as designed;
* **marketing channels additionally need the profile opt-in** -- promotions email/sms/push are gated by
  both the category preference and profile opt-ins, which is what the one-click unsubscribe endpoint flips;
* **channel capabilities are checked here too** -- unconfigured SMS or push providers are never simulated;
* **recipient validity is checked here too** -- missing email/phone or inactive account stops delivery.

Writes from the preferences screen are coerced: mandatory categories are forced back to
enabled on the way in, and marketing opt-ins synchronize cleanly with Profile flags.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import IntegrityError, transaction

from apps.accounts.models import Profile
from apps.notifications.models import (
    Channel,
    NotificationCategory,
    NotificationPreference,
    NotificationType,
    UnsubscribeToken,
)
from apps.notifications.services.templates import (
    MANDATORY_CATEGORIES,
    MARKETING_CATEGORIES,
    category_defaults,
    get_spec,
)

__all__ = [
    "apply_unsubscribe",
    "effective_channels",
    "ensure_unsubscribe_token",
    "find_unsubscribe_token",
    "get_preference",
    "marketing_push_subscribed",
    "marketing_sms_subscribed",
    "marketing_subscribed",
    "preference_rows",
    "set_preferences",
    "user_can_receive_email",
    "user_can_receive_push",
    "user_can_receive_sms",
]


def _profile_marketing_opt_in(user) -> bool:
    """Read the profile email opt-in flag defensively."""
    profile = getattr(user, "profile", None)
    if profile is None:
        return False
    return bool(getattr(profile, "marketing_email_opt_in", False))


def _profile_marketing_sms_opt_in(user) -> bool:
    """Read the profile SMS opt-in flag defensively."""
    profile = getattr(user, "profile", None)
    if profile is None:
        return False
    return bool(getattr(profile, "marketing_sms_opt_in", False))


def _profile_marketing_push_opt_in(user) -> bool:
    """Read the profile push opt-in flag defensively."""
    profile = getattr(user, "profile", None)
    if profile is None:
        return False
    return bool(getattr(profile, "marketing_push_opt_in", False))


def marketing_subscribed(user, channel: str = Channel.EMAIL) -> bool:
    """True when promotions may flow on this channel (category preference AND profile opt-in)."""
    pref = get_preference(user, NotificationCategory.PROMOTIONS)
    if channel == Channel.EMAIL:
        return _profile_marketing_opt_in(user) and pref.email_enabled
    if channel == Channel.SMS:
        return _profile_marketing_sms_opt_in(user) and pref.sms_enabled
    if channel == Channel.PUSH:
        return _profile_marketing_push_opt_in(user) and pref.push_enabled
    if channel == Channel.IN_APP:
        return pref.in_app_enabled
    return False


def marketing_sms_subscribed(user) -> bool:
    """Convenience helper: check if user opted into SMS marketing."""
    return marketing_subscribed(user, channel=Channel.SMS)


def marketing_push_subscribed(user) -> bool:
    """Convenience helper: check if user opted into push marketing."""
    return marketing_subscribed(user, channel=Channel.PUSH)


def get_preference(user, category: str) -> NotificationPreference:
    """Fetch or seed the preference row for ``(user, category)``."""
    try:
        return NotificationPreference.objects.get(user=user, category=category)
    except NotificationPreference.DoesNotExist:
        defaults = category_defaults(category)
        try:
            with transaction.atomic():
                return NotificationPreference.objects.create(
                    user=user,
                    category=category,
                    email_enabled=defaults.get("email", True),
                    in_app_enabled=defaults.get("in_app", True),
                    sms_enabled=defaults.get("sms", False),
                    push_enabled=defaults.get("push", False),
                )
        except IntegrityError:
            return NotificationPreference.objects.get(user=user, category=category)


def user_can_receive_email(user) -> bool:
    """A live account with a usable email address."""
    if user is None:
        return False
    if not getattr(user, "is_active", False):
        return False
    email = (getattr(user, "email", "") or "").strip()
    return "@" in email


def user_can_receive_sms(user) -> bool:
    """Configured provider and a live account with a phone number."""
    from apps.notifications.services.sms import is_sms_configured

    if not is_sms_configured():
        return False
    if user is None or not getattr(user, "is_active", False):
        return False
    profile = getattr(user, "profile", None)
    if profile is None:
        return False
    phone = (getattr(profile, "phone", "") or "").strip()
    return bool(phone)


def user_can_receive_push(user) -> bool:
    """Configured push provider and a live account."""
    from apps.notifications.services.push import is_push_configured

    if not is_push_configured():
        return False
    if user is None or not getattr(user, "is_active", False):
        return False
    return True


def effective_channels(
    user,
    notification_type: str,
    requested: Iterable[str] | None = None,
) -> list[str]:
    """Resolve the channels this delivery may use.

    ``requested`` narrows the registry's channel set; it can never widen it.
    Rechecks consent and channel capabilities immediately before delivery.
    """
    spec = get_spec(notification_type)
    wanted = [ch for ch in (requested or spec.channels) if ch in spec.channels]

    allowed: list[str] = []
    for channel in wanted:
        if channel == Channel.IN_APP:
            if spec.is_mandatory or get_preference(user, spec.category).in_app_enabled:
                allowed.append(channel)
            continue

        if channel == Channel.EMAIL:
            if not user_can_receive_email(user):
                continue
            if spec.is_mandatory:
                allowed.append(channel)
                continue
            pref = get_preference(user, spec.category)
            if not pref.email_enabled:
                continue
            if spec.category in MARKETING_CATEGORIES and not _profile_marketing_opt_in(user):
                continue
            allowed.append(channel)
            continue

        if channel == Channel.SMS:
            if not user_can_receive_sms(user):
                continue
            if spec.is_mandatory:
                allowed.append(channel)
                continue
            pref = get_preference(user, spec.category)
            if not pref.sms_enabled:
                continue
            if spec.category in MARKETING_CATEGORIES and not _profile_marketing_sms_opt_in(user):
                continue
            allowed.append(channel)
            continue

        if channel == Channel.PUSH:
            if not user_can_receive_push(user):
                continue
            if spec.is_mandatory:
                allowed.append(channel)
                continue
            pref = get_preference(user, spec.category)
            if not pref.push_enabled:
                continue
            if spec.category in MARKETING_CATEGORIES and not _profile_marketing_push_opt_in(user):
                continue
            allowed.append(channel)
            continue

    return allowed


def set_preferences(user, payload: dict[str, Any]) -> list[NotificationPreference]:
    """Apply a preferences payload: ``{category: {"email": bool, "in_app": bool, "sms": bool, "push": bool}}``.

    Unknown categories are ignored; mandatory categories are coerced to enabled on essential channels;
    categories the customer switches on for marketing additionally synchronize with Profile opt-in flags.
    """
    rows: list[NotificationPreference] = []
    profile = getattr(user, "profile", None)
    user_phone = (getattr(profile, "phone", "") or "").strip() if profile else ""

    for category, flags in payload.items():
        if category not in NotificationCategory.values:
            continue
        pref = get_preference(user, category)
        flags = flags or {}
        if category in MANDATORY_CATEGORIES:
            pref.email_enabled = True
            pref.in_app_enabled = True
            pref.sms_enabled = bool(flags.get("sms", pref.sms_enabled))
            pref.push_enabled = bool(flags.get("push", pref.push_enabled))
        else:
            pref.email_enabled = bool(flags.get("email", pref.email_enabled))
            pref.in_app_enabled = bool(flags.get("in_app", pref.in_app_enabled))
            pref.sms_enabled = bool(flags.get("sms", pref.sms_enabled))
            pref.push_enabled = bool(flags.get("push", pref.push_enabled))

        if pref.sms_enabled and not user_phone:
            # Cannot enable marketing/optional SMS without a phone number on profile
            pref.sms_enabled = False

        pref.save(
            update_fields=[
                "email_enabled",
                "in_app_enabled",
                "sms_enabled",
                "push_enabled",
                "updated_at",
            ]
        )
        rows.append(pref)

        # Synchronize Profile marketing opt-in booleans for the PROMOTIONS category
        if category == NotificationCategory.PROMOTIONS and profile is not None:
            profile_dirty = False
            if profile.marketing_email_opt_in != pref.email_enabled:
                profile.marketing_email_opt_in = pref.email_enabled
                profile_dirty = True
            desired_sms_opt = pref.sms_enabled and bool(user_phone)
            if profile.marketing_sms_opt_in != desired_sms_opt:
                profile.marketing_sms_opt_in = desired_sms_opt
                profile_dirty = True
            if getattr(profile, "marketing_push_opt_in", False) != pref.push_enabled:
                profile.marketing_push_opt_in = pref.push_enabled
                profile_dirty = True
            if profile_dirty:
                profile.save()

    return rows


def ensure_unsubscribe_token(user) -> UnsubscribeToken:
    """Fetch or mint the opaque marketing-unsubscribe handle for this recipient."""
    token, _created = UnsubscribeToken.objects.get_or_create(user=user)
    return token


def find_unsubscribe_token(token: str) -> UnsubscribeToken | None:
    """Resolve a presented token. Lookup, never decode."""
    if not token or len(token) > 64:
        return None
    return UnsubscribeToken.objects.select_related("user").filter(token=token).first()


def apply_unsubscribe(
    user,
    *,
    channels: tuple[str, ...] = (Channel.EMAIL, Channel.SMS, Channel.PUSH),
) -> bool:
    """One-click unsubscribe (Phase 17 §16, Phase 51 §4): turn marketing off across channels.

    Idempotent by construction -- running it twice flips nothing the second time.
    Also clears the profile marketing opt-ins.
    """
    pref = get_preference(user, NotificationCategory.PROMOTIONS)
    changed = False
    pref_updates = []
    if Channel.EMAIL in channels and pref.email_enabled:
        pref.email_enabled = False
        pref_updates.append("email_enabled")
        changed = True
    if Channel.SMS in channels and pref.sms_enabled:
        pref.sms_enabled = False
        pref_updates.append("sms_enabled")
        changed = True
    if Channel.PUSH in channels and pref.push_enabled:
        pref.push_enabled = False
        pref_updates.append("push_enabled")
        changed = True

    if pref_updates:
        pref_updates.append("updated_at")
        pref.save(update_fields=pref_updates)

    profile = Profile.objects.filter(user=user).first()
    if profile is not None:
        prof_updates = []
        if Channel.EMAIL in channels and profile.marketing_email_opt_in:
            profile.marketing_email_opt_in = False
            prof_updates.append("marketing_email_opt_in")
            changed = True
        if Channel.SMS in channels and profile.marketing_sms_opt_in:
            profile.marketing_sms_opt_in = False
            prof_updates.append("marketing_sms_opt_in")
            changed = True
        if Channel.PUSH in channels and getattr(profile, "marketing_push_opt_in", False):
            profile.marketing_push_opt_in = False
            prof_updates.append("marketing_push_opt_in")
            changed = True
        if prof_updates:
            prof_updates.append("updated_at")
            profile.save(update_fields=prof_updates)

    return changed


def preference_rows(user) -> list[dict[str, Any]]:
    """The screen/API payload: every category, seeded, with mandatory flagged."""
    from apps.notifications.services.push import is_push_configured
    from apps.notifications.services.sms import is_sms_configured

    sms_avail = is_sms_configured()
    push_avail = is_push_configured()
    rows = []
    for category in NotificationCategory.choices:
        value = category[0]
        pref = get_preference(user, value)
        rows.append(
            {
                "category": value,
                "label": str(dict(NotificationCategory.choices)[value]),
                "email": pref.email_enabled,
                "in_app": pref.in_app_enabled,
                "sms": pref.sms_enabled,
                "push": pref.push_enabled,
                "sms_available": sms_avail,
                "push_available": push_avail,
                "mandatory": value in MANDATORY_CATEGORIES,
            }
        )
    return rows


def is_known_type(notification_type: str) -> bool:
    return notification_type in NotificationType.values and notification_type in _all_types()


def _all_types() -> set[str]:
    from apps.notifications.services.templates import registry

    return set(registry())
