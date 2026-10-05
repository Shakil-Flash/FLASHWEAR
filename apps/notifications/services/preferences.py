"""Preference resolution (Phase 17 §14).

The one place that answers "does this recipient want this channel for this category?".
Everything else -- dispatcher, bulk broadcast -- asks here, so the rules cannot drift:

* **mandatory categories win over stored rows** -- a tampered, buggy or migrated row cannot
  suppress an order, payment, delivery, support or account-security message;
* **optional categories read the stored row**, created lazily from the registry defaults so
  a customer who never opens the screen behaves as designed;
* **marketing email additionally needs the profile opt-in** -- promotions email is gated by
  both the category preference and ``Profile.marketing_email_opt_in``, which is what the
  one-click unsubscribe endpoint flips;
* **recipient validity is checked here too** -- no email address or inactive account means
  no email leg, regardless of preferences.

Writes from the preferences screen are coerced: mandatory categories are forced back to
enabled on the way in, so a hostile POST changes nothing that matters (the screen also
renders them disabled, and the API returns ``mandatory: true`` so a client can too).
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
    "marketing_subscribed",
    "preference_rows",
    "set_preferences",
    "user_can_receive_email",
]


def _profile_marketing_opt_in(user) -> bool:
    """Read the profile flag defensively: users may exist without a profile row."""
    profile = getattr(user, "profile", None)
    if profile is None:
        return False
    return bool(getattr(profile, "marketing_email_opt_in", False))


def marketing_subscribed(user) -> bool:
    """True when promotions email may flow (category preference AND profile opt-in)."""
    if not _profile_marketing_opt_in(user):
        return False
    pref = get_preference(user, NotificationCategory.PROMOTIONS)
    return pref.email_enabled


def get_preference(user, category: str) -> NotificationPreference:
    """Fetch or seed the preference row for ``(user, category)``.

    Seeding is a read-side concern on purpose: preferences must work for a customer who
    never visited the screen, and two concurrent seeds race into the unique constraint,
    which the ``IntegrityError`` fallback resolves to the winner's row.
    """
    try:
        return NotificationPreference.objects.get(user=user, category=category)
    except NotificationPreference.DoesNotExist:
        defaults = category_defaults(category)
        try:
            # A savepoint, not a bare INSERT: the dispatcher often runs inside the
            # caller's transaction, and a lost seeding race must not poison it.
            with transaction.atomic():
                return NotificationPreference.objects.create(
                    user=user,
                    category=category,
                    email_enabled=defaults["email"],
                    in_app_enabled=defaults["in_app"],
                )
        except IntegrityError:
            return NotificationPreference.objects.get(user=user, category=category)


def user_can_receive_email(user) -> bool:
    """A live account with a usable address. Checked on every dispatch, not once at signup."""
    if user is None:
        return False
    if not getattr(user, "is_active", False):
        return False
    email = (getattr(user, "email", "") or "").strip()
    return "@" in email


def effective_channels(
    user,
    notification_type: str,
    requested: Iterable[str] | None = None,
) -> list[str]:
    """Resolve the channels this delivery may use.

    ``requested`` narrows the registry's channel set (call sites use it to add an in-app
    copy without a second email, or vice versa); it can never *widen* it -- a caller must
    not be able to force a channel the type does not declare.
    """
    spec = get_spec(notification_type)
    wanted = [ch for ch in (requested or spec.channels) if ch in spec.channels]

    allowed: list[str] = []
    for channel in wanted:
        if channel == Channel.IN_APP:
            allowed.append(channel)
            continue
        # channel == EMAIL
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
    # In-app for optional categories still respects the stored toggle.
    if Channel.IN_APP in allowed and not spec.is_mandatory:
        if not get_preference(user, spec.category).in_app_enabled:
            allowed.remove(Channel.IN_APP)
    return allowed


def set_preferences(user, payload: dict[str, Any]) -> list[NotificationPreference]:
    """Apply a preferences payload: ``{category: {"email": bool, "in_app": bool}}``.

    Unknown categories are ignored; mandatory categories are coerced to enabled (a request
    cannot silence order, payment, delivery, support or account messages); categories the
    customer switches *on* for marketing email additionally require the profile opt-in --
    turning the toggle on without opting in is honoured by re-reading the profile flag at
    dispatch time, but we keep the stored row honest here too.

    Returns the rows as they now stand.
    """
    rows: list[NotificationPreference] = []
    for category, flags in payload.items():
        if category not in NotificationCategory.values:
            continue
        pref = get_preference(user, category)
        if category in MANDATORY_CATEGORIES:
            pref.email_enabled = True
            pref.in_app_enabled = True
        else:
            pref.email_enabled = bool((flags or {}).get("email", pref.email_enabled))
            pref.in_app_enabled = bool((flags or {}).get("in_app", pref.in_app_enabled))
        pref.save(update_fields=["email_enabled", "in_app_enabled", "updated_at"])
        rows.append(pref)
    return rows


def ensure_unsubscribe_token(user) -> UnsubscribeToken:
    """Fetch or mint the opaque marketing-unsubscribe handle for this recipient.

    Emails mint it lazily at render time (an unsubscribe URL with no token row would be a
    dead link), and the random value means no URL ever encodes who it belongs to.
    """
    token, _created = UnsubscribeToken.objects.get_or_create(user=user)
    return token


def find_unsubscribe_token(token: str) -> UnsubscribeToken | None:
    """Resolve a presented token. Lookup, never decode -- tampering reduces to guessing
    256 bits of randomness, and a forged token simply resolves to nobody."""
    if not token or len(token) > 64:
        return None
    return UnsubscribeToken.objects.select_related("user").filter(token=token).first()


def apply_unsubscribe(user) -> bool:
    """One-click unsubscribe (Phase 17 §16): turn promotions email off.

    Idempotent by construction -- running it twice flips nothing the second time. Also
    clears the profile marketing opt-in so no other marketing path re-arms the toggle; the
    preferences screen is the (authenticated) way to opt back in.
    """
    pref = get_preference(user, NotificationCategory.PROMOTIONS)
    changed = False
    if pref.email_enabled:
        pref.email_enabled = False
        pref.save(update_fields=["email_enabled", "updated_at"])
        changed = True
    profile = Profile.objects.filter(user=user).first()
    if profile is not None and profile.marketing_email_opt_in:
        profile.marketing_email_opt_in = False
        profile.save(update_fields=["marketing_email_opt_in", "updated_at"])
        changed = True
    return changed


def preference_rows(user) -> list[dict[str, Any]]:
    """The screen/API payload: every category, seeded, with mandatory flagged."""
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
                "mandatory": value in MANDATORY_CATEGORIES,
            }
        )
    return rows


def is_known_type(notification_type: str) -> bool:
    return notification_type in NotificationType.values and notification_type in _all_types()


def _all_types() -> set[str]:
    from apps.notifications.services.templates import registry

    return set(registry())
