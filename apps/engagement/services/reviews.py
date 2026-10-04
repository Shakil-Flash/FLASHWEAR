"""Review services: eligibility, moderation and aggregates (Phase 7).

Eligibility is purchase-gated and server-derived end to end -- the form never trusts a hidden
field, and neither does the API: :func:`qualifying_order` is the only source of both the order
foreign key and the ``verified_purchase`` flag.

The aggregate is **one database query** with conditional counts (never a Python loop over
rows): the product page, the API and the JSON-LD block all read it, and a catalogue of 10k
reviews must not turn into a per-product scan. Aggregates only ever see ``published`` rows --
pending and hidden reviews are invisible to every public surface, including counts.

Sorting uses an allowlist dict, not a request string, so nothing a customer types ever reaches
``order_by``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, DecimalField, Q
from django.utils import timezone

from apps.engagement.models import Review
from apps.engagement.services.errors import ReviewError

# The order states in which a purchase earns the right to review. PENDING_PAYMENT has not
# paid; CANCELLED never will. (REFUNDED is unreachable until the returns phase.)
REVIEWABLE_ORDER_STATUSES = (
    "paid",
    "processing",
    "shipped",
    "delivered",
)

# Public sort allowlist -> order_by tuple. Never pass a request value to order_by directly.
SORT_CHOICES = {
    "newest": ("-created_at",),
    "highest": ("-rating", "-created_at"),
    "lowest": ("rating", "-created_at"),
}
DEFAULT_SORT = "newest"


def qualifying_order(user, product):
    """The customer's most recent reviewable order containing ``product``, or ``None``."""
    from apps.orders.models import Order

    return (
        Order.objects.filter(
            user=user,
            status__in=REVIEWABLE_ORDER_STATUSES,
            items__variant__product=product,
        )
        .distinct()
        .order_by("-created_at")
        .first()
    )


def eligibility(user, product) -> tuple[bool, str]:
    """``(allowed, reason_code)`` for showing the form or the "why not" message.

    reason codes: ``anonymous``, ``unavailable``, ``already_reviewed``, ``purchase_required``.
    """
    if not user.is_authenticated:
        return False, "anonymous"
    if product is None or product.status != product.Status.ACTIVE:
        return False, "unavailable"
    if Review.objects.filter(author=user, product=product).exists():
        return False, "already_reviewed"
    if qualifying_order(user, product) is None:
        return False, "purchase_required"
    return True, ""


def create_review(*, user, product, rating: int, title: str, body: str) -> Review:
    """Create a pending review from server-derived facts only.

    Re-runs eligibility defensively (the race between check and insert is closed by the
    unique constraint, which is mapped back to a friendly error).
    """
    title, body = (title or "").strip(), (body or "").strip()
    if not title or not body:
        raise ReviewError("A review needs a title and some words.", code="invalid")

    allowed, reason = eligibility(user, product)
    if not allowed:
        raise ReviewError(ineligible_message(reason), code=reason)

    order = qualifying_order(user, product)
    try:
        with transaction.atomic():
            return Review.objects.create(
                product=product,
                author=user,
                order=order,
                rating=int(rating),
                title=title,
                body=body,
                verified_purchase=True,
                status=Review.Status.PENDING,
            )
    except IntegrityError as exc:
        raise ReviewError(
            "You have already reviewed this product.", code="already_reviewed"
        ) from exc


def update_review(review: Review, *, rating: int, title: str, body: str) -> Review:
    """Owner edit. A *content* change to a published review returns it to moderation.

    Re-moderation is the honest choice: the words a moderator approved are not the words the
    customer just wrote. No content change (identical resubmit) leaves the status alone.
    """
    content_changed = (
        int(rating) != review.rating or title.strip() != review.title or body.strip() != review.body
    )
    review.rating = int(rating)
    review.title = title.strip()
    review.body = body.strip()
    if content_changed and review.status == Review.Status.PUBLISHED:
        review.status = Review.Status.PENDING
        review.moderated_at = timezone.now()
    review.save()
    return review


def set_status(review: Review, status: str) -> Review:
    """Moderator transition for one review; stamps the audit timestamps, never deletes."""
    review.status = status
    review.moderated_at = timezone.now()
    if status == Review.Status.PUBLISHED:
        review.published_at = timezone.now()
    review.save(update_fields=["status", "moderated_at", "published_at", "updated_at"])
    return review


def moderate(queryset, status: str) -> int:
    """Admin action bulk transition. Returns rows updated (status only, no deletion).

    ``exclude(status=status)`` makes the action idempotent (publishing the published is a
    no-op) while still allowing a moderator to *unpublish* -- rejecting or hiding a review
    that is currently live must work, not silently skip.
    """
    moment = timezone.now()
    updates = {"status": status, "moderated_at": moment, "updated_at": moment}
    if status == Review.Status.PUBLISHED:
        updates["published_at"] = moment
    return queryset.exclude(status=status).update(**updates)


def aggregate_for(product) -> dict:
    """One query: count, 1-decimal average and the 1-5 rating histogram of *published* rows.

    Returns ``{"count", "average", "distribution", "shares"}`` where ``average`` is a
    ``Decimal`` rounded half-up (or ``None`` at count 0). Histogram and share keys are the
    **strings** ``"1".."5"`` so templates can reach them as ``distribution.5`` (Django's
    template dictionary lookup does not int-cast keys). ``shares`` are integer percentages
    summing to 100 (or all 0), so templates never divide.
    """
    stats = Review.objects.filter(product=product, status=Review.Status.PUBLISHED).aggregate(
        count=Count("id"),
        average=Avg("rating", output_field=DecimalField()),
        r1=Count("id", filter=Q(rating=1)),
        r2=Count("id", filter=Q(rating=2)),
        r3=Count("id", filter=Q(rating=3)),
        r4=Count("id", filter=Q(rating=4)),
        r5=Count("id", filter=Q(rating=5)),
    )
    count = stats["count"] or 0
    distribution = {
        "1": stats.get("r1") or 0,
        "2": stats.get("r2") or 0,
        "3": stats.get("r3") or 0,
        "4": stats.get("r4") or 0,
        "5": stats.get("r5") or 0,
    }
    average = None
    if count:
        average = (stats["average"] or Decimal(0)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    shares = {
        stars: (round(distribution[stars] * 100 / count) if count else 0) for stars in distribution
    }
    return {
        "count": count,
        "average": average,
        "distribution": distribution,
        "shares": shares,
    }


def public_queryset(product, *, sort: str = DEFAULT_SORT, verified_only: bool = False):
    """Published reviews for the product page/API, sorted from the allowlist."""
    order_by = SORT_CHOICES.get(sort, SORT_CHOICES[DEFAULT_SORT])
    queryset = (
        Review.objects.filter(product=product, status=Review.Status.PUBLISHED)
        .select_related("author")
        .order_by(*order_by)
    )
    if verified_only:
        queryset = queryset.filter(verified_purchase=True)
    return queryset


def own_review(user, product):
    """The customer's own review for this product, whatever its status.

    Authors may see their unpublished rows (that is how "awaiting moderation" is shown);
    everyone else only ever gets published ones from :func:`public_queryset`.
    """
    if not user.is_authenticated:
        return None
    return Review.objects.filter(author=user, product=product).first()


def ineligible_message(reason: str) -> str:
    """User-facing copy for an eligibility denial, keyed by the reason code."""
    return {
        "anonymous": "Sign in to review this product.",
        "unavailable": "This product can no longer be reviewed.",
        "already_reviewed": "You have already reviewed this product.",
        "purchase_required": "Only customers who bought this product can review it.",
    }.get(reason, "You cannot review this product right now.")
