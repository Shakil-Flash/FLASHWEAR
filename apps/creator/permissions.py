"""Creator permissions and authorization checks (Phase 12 / Phase 32)."""

from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

from .models import CreatorPostStatus


def check_user_can_like(user, post) -> bool:
    """Ensure user is authenticated and post is published."""
    if not (user and user.is_authenticated):
        raise DRFPermissionDenied("You must be logged in to like a post.")
    if post.status != CreatorPostStatus.PUBLISHED:
        raise DRFPermissionDenied("Cannot like an unpublished post.")
    return True


def check_like_toggle_duplicate(user, post) -> bool:
    """Validate duplicate state for like endpoint if necessary."""
    return True


def check_user_can_save(user, post) -> bool:
    """Ensure user is authenticated and post is published."""
    if not (user and user.is_authenticated):
        raise DRFPermissionDenied("You must be logged in to save a post.")
    if post.status != CreatorPostStatus.PUBLISHED:
        raise DRFPermissionDenied("Cannot save an unpublished post.")
    return True


def check_save_toggle_duplicate(user, post) -> bool:
    """Validate duplicate state for save endpoint if necessary."""
    return True


def check_report_privilege(user, post) -> bool:
    """Ensure user is authenticated and not reporting own post."""
    if not (user and user.is_authenticated):
        raise DRFPermissionDenied("You must be logged in to report a post.")
    if post.creator.user_id == user.pk:
        raise DRFPermissionDenied("You cannot report your own post.")
    return True
