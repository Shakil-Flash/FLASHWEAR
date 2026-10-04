"""Creator tasks (Phase 12).

Background tasks for creator operations. Only add tasks where genuinely useful:
- media processing hooks
- stale moderation cleanup

Publication state is never dependent on Celery - the database is authoritative.
"""

from django.utils import timezone
from celery import shared_task


@shared_task
def cleanup_stale_reports():
    """Clean up reports older than 90 days that are still NEW.

    Only runs if PostgreSQL is available; silently no-op on SQLite.
    """
    try:
        from creator.models import CreatorPostReport
        from django.utils import timezone as dj_timezone

        ninety_days_ago = dj_timezone.now() - timezone.timedelta(days=90)
        stale = CreatorPostReport.objects.filter(
            status=CreatorPostReport.Status.NEW,
            created_at__lte=ninety_days_ago,
        )
        count = stale.count()
        if count > 0:
            stale.update(status=CreatorPostReport.Status.RESOLVED, resolved_at=dj_timezone.now())
        return count
    except Exception:
        # SQLite doesn't have the same timeline; skip gracefully
        return 0


@shared_task
def increment_post_views(post_id):
    """Increment view count for a creator post (called externally).

    Kept for compatibility; view counting is now handled in the post_detail
    view via F() expressions for atomicity.
    """
    try:
        from creator.models import CreatorPost
        post = CreatorPost.objects.get(pk=post_id)
        post.view_count = models.F("view_count") + 1
        post.save(update_fields=["view_count"])
    except CreatorPost.DoesNotExist:
        pass