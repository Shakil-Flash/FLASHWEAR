"""Staff capabilities for the FLASH Support desk (Phase 15).

The project has no role system beyond ``User.is_staff``, and support must not invent one by
reading ``is_staff`` at twenty call sites: a flag that grants "may sit behind the help desk"
also grants admin access, and turning either on should not silently turn the other on.

So capability is a **group plus the staff flag**:

* ``Support Agent`` -- may work the queue: reply publicly, leave notes, assign, set
  priority, change status, link references.
* ``Support Manager`` -- the agent set plus escalation and unassignment of others.
* A superuser implies every capability (Django's own contract, so the admin stays usable).
* ``is_staff`` alone implies **nothing** here: an operator who is only meant to browse the
  admin cannot open the desk, and a customer in the group cannot either.

Groups are rows, created idempotently by migration ``0002`` and by
:func:`ensure_capabilities`, so a fresh test database behaves the same as production.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import QuerySet

__all__ = [
    "AGENT_GROUP",
    "MANAGER_GROUP",
    "SUPPORT_AGENT",
    "SUPPORT_CAPABILITIES",
    "SUPPORT_MANAGER",
    "ensure_capabilities",
    "group_for",
    "has_capability",
    "staff_with_capability",
]

SUPPORT_AGENT = "support.agent"
SUPPORT_MANAGER = "support.manager"
SUPPORT_CAPABILITIES = (SUPPORT_AGENT, SUPPORT_MANAGER)

AGENT_GROUP = "Support Agent"
MANAGER_GROUP = "Support Manager"

_GROUP_BY_CAPABILITY = {
    SUPPORT_AGENT: AGENT_GROUP,
    SUPPORT_MANAGER: MANAGER_GROUP,
}


def group_for(capability: str) -> str:
    """The group name that carries ``capability`` (a capability is a group in disguise)."""
    try:
        return _GROUP_BY_CAPABILITY[capability]
    except KeyError as exc:  # a typo must not silently mean "no access"
        raise KeyError(f"Unknown support capability: {capability!r}") from exc


def has_capability(user, capability: str) -> bool:
    """Does ``user`` hold ``capability``?

    Deliberately strict: anonymous, inactive and missing users are all ``False``, a
    superuser is always ``True``, and everyone else needs both the staff flag and the
    group. Group membership without ``is_staff`` is not enough, because every support
    surface sits behind a staff-only door.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not user.is_active:
        return False
    if user.is_superuser:
        return True
    if not user.is_staff:
        return False
    names = {AGENT_GROUP, MANAGER_GROUP} & set(user.groups.values_list("name", flat=True))
    if capability == SUPPORT_MANAGER:
        return MANAGER_GROUP in names
    if capability == SUPPORT_AGENT:
        return bool(names)
    raise KeyError(f"Unknown support capability: {capability!r}")


def staff_with_capability(capability: str) -> QuerySet:
    """Every active user who holds ``capability`` -- the notification recipient list."""
    User = get_user_model()
    group = group_for(capability)
    return (
        User.objects.filter(is_active=True, is_staff=True, groups__name=group)
        .distinct()
        .order_by(User.USERNAME_FIELD)
    )


def ensure_capabilities() -> list[str]:
    """Create both groups (idempotent). Returns the names that were missing.

    The groups are permission-free on purpose: access to this app is expressed by
    :func:`has_capability`, not by Django's model permissions, so there is exactly one
    answer to "may this user work the desk?" instead of two that can disagree.
    """
    created: list[str] = []
    for name in (AGENT_GROUP, MANAGER_GROUP):
        _, was_created = Group.objects.get_or_create(name=name)
        if was_created:
            created.append(name)
    return created
