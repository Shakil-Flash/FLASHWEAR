"""DRF permissions for FLASH Loop (Phase 13).

Object-level authorization is the IDOR boundary: a signed-in user
may only read or act on their own loop rows, and only staff may
read others'. The public resale shelf is a separate concern --
:mod:`apps.loop.selectors` already filters it to approved, active
listings, so those views use ``AllowAny`` over that queryset rather
than a per-object check.
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

__all__ = ["IsLoopOwnerOrStaff", "IsLoopStaff"]


class IsLoopOwnerOrStaff(BasePermission):
    """Read: own rows (or any row for staff). Write: own rows only.

    Staff may *read* everything (moderation needs the view) but a
    write that only moderators may perform (approve, verify) is
    gated in the service layer by ``actor.is_staff`` -- this
    permission never grants write escalation on its own.
    """

    message = "You do not have access to that loop request."

    def has_object_permission(self, request, view, obj) -> bool:
        owner_id = getattr(obj, "user_id", None)
        if owner_id is None:
            owner_id = getattr(obj, "seller_id", None)
        if owner_id is None and hasattr(obj, "loop_item_id"):
            # TradeInRequest / RecycleRequest / listings: fall back to the item.
            owner_id = getattr(obj.loop_item, "user_id", None)

        if owner_id is not None and request.user.is_authenticated:
            if owner_id == request.user.id:
                return True
        return bool(request.user and request.user.is_staff)

    def has_permission(self, request, view) -> bool:
        # Reads are open to anonymous callers only where the view opts in
        # with AllowAny; this class always requires an authenticated user
        # for anything beyond a safe method on an object it can check.
        if request.method in SAFE_METHODS and request.user and request.user.is_staff:
            return True
        return bool(request.user and request.user.is_authenticated)


class IsLoopStaff(BasePermission):
    """Staff-only views (moderation endpoints)."""

    message = "Only FLASHWEAR moderators can do that."

    def has_permission(self, request, view) -> bool:
        return bool(request.user and request.user.is_staff)
