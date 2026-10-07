"""Creator service layer for mutations, interactions, and tagging."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.text import slugify

from apps.creator.models import (
    CreatorApplication,
    CreatorPost,
    CreatorPostLike,
    CreatorPostOutfit,
    CreatorPostProduct,
    CreatorPostReport,
    CreatorPostSave,
    CreatorPostStatus,
    CreatorProfile,
    CreatorStatus,
)


def get_or_create_profile(user: Any) -> CreatorProfile | None:
    """Return the creator profile for a user if exists, or None."""
    return CreatorProfile.objects.filter(user=user).first()


def create_application(
    user: Any,
    bio: str,
    display_name: str,
    social_links: dict[str, str] | str = "",
    portfolio_url: str = "",
) -> CreatorApplication:
    """Submit a new application to become a verified creator."""
    if not user or not user.is_authenticated:
        raise ValueError("Authentication required to submit an application.")

    active = CreatorApplication.objects.filter(
        applicant=user, status__in=[CreatorStatus.PENDING, CreatorStatus.APPROVED]
    ).first()
    if active:
        raise ValueError("You already have an active application or approved profile.")

    if isinstance(social_links, str):
        import json

        try:
            social_links = json.loads(social_links) if social_links else {}
        except Exception:
            social_links = {}

    return CreatorApplication.objects.create(
        applicant=user,
        application_bio=bio.strip(),
        requested_display_name=display_name.strip() or user.get_full_name() or user.email,
        social_links=social_links,
        portfolio_url=portfolio_url.strip(),
        status=CreatorStatus.PENDING,
    )


def create_post(
    creator: CreatorProfile,
    title: str,
    slug: str = "",
    caption: str = "",
    cover_image: Any = None,
    status: str = CreatorPostStatus.PUBLISHED,
) -> CreatorPost:
    """Create a new creator fashion post."""
    if not slug:
        slug = slugify(title)

    # Ensure slug uniqueness
    base_slug = slug
    counter = 1
    while CreatorPost.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    post = CreatorPost.objects.create(
        creator=creator,
        title=title.strip(),
        slug=slug,
        caption=caption.strip(),
        cover_image=cover_image,
        status=status,
        published_at=timezone.now() if status == CreatorPostStatus.PUBLISHED else None,
    )
    return post


@transaction.atomic
def like_post(user: Any, post: CreatorPost) -> CreatorPostLike:
    """Like a creator post and increment like count."""
    like, created = CreatorPostLike.objects.get_or_create(user=user, post=post)
    if created:
        CreatorPost.objects.filter(pk=post.pk).update(like_count=F("like_count") + 1)
        post.refresh_from_db(fields=["like_count"])

        try:
            from apps.analytics.services import record_event

            record_event(
                name="creator_content_view",
                user=user,
                object_type="creator_post",
                object_id=post.pk,
                metadata={"action": "like", "slug": post.slug},
            )
        except Exception:
            pass

    return like


@transaction.atomic
def unlike_post(user: Any, post: CreatorPost) -> bool:
    """Unlike a creator post and decrement like count."""
    deleted_count, _ = CreatorPostLike.objects.filter(user=user, post=post).delete()
    if deleted_count > 0:
        CreatorPost.objects.filter(pk=post.pk, like_count__gt=0).update(
            like_count=F("like_count") - 1
        )
        post.refresh_from_db(fields=["like_count"])
        return True
    return False


@transaction.atomic
def save_post(user: Any, post: CreatorPost) -> CreatorPostSave:
    """Bookmark/save a creator post and increment save count."""
    save_obj, created = CreatorPostSave.objects.get_or_create(user=user, post=post)
    if created:
        CreatorPost.objects.filter(pk=post.pk).update(save_count=F("save_count") + 1)
        post.refresh_from_db(fields=["save_count"])

        try:
            from apps.analytics.services import record_event

            record_event(
                name="creator_content_view",
                user=user,
                object_type="creator_post",
                object_id=post.pk,
                metadata={"action": "save", "slug": post.slug},
            )
        except Exception:
            pass

    return save_obj


@transaction.atomic
def unsave_post(user: Any, post: CreatorPost) -> bool:
    """Unsave a creator post and decrement save count."""
    deleted_count, _ = CreatorPostSave.objects.filter(user=user, post=post).delete()
    if deleted_count > 0:
        CreatorPost.objects.filter(pk=post.pk, save_count__gt=0).update(
            save_count=F("save_count") - 1
        )
        post.refresh_from_db(fields=["save_count"])
        return True
    return False


def report_post(
    user: Any,
    post: CreatorPost,
    reason: str = CreatorPostReport.Reason.OTHER,
    details: str = "",
) -> CreatorPostReport:
    """File a report against a post."""
    report, _ = CreatorPostReport.objects.update_or_create(
        user=user,
        post=post,
        reason=reason,
        defaults={"details": details.strip(), "status": CreatorPostReport.Status.NEW},
    )
    return report


def add_product_tag(
    post: CreatorPost, product: Any, label: str = "", display_order: int = 0
) -> CreatorPostProduct:
    """Tag a catalog product on a post to make it shoppable."""
    tag, _ = CreatorPostProduct.objects.get_or_create(
        post=post,
        product=product,
        defaults={"label": label.strip(), "display_order": display_order},
    )
    return tag


def remove_product_tag(post: CreatorPost, product_id: int) -> bool:
    """Remove a product tag from a post."""
    deleted, _ = CreatorPostProduct.objects.filter(post=post, product_id=product_id).delete()
    return deleted > 0


def add_outfit_tag(
    post: CreatorPost, outfit: Any, display_order: int = 0
) -> CreatorPostOutfit:
    """Tag an outfit look on a post."""
    tag, _ = CreatorPostOutfit.objects.get_or_create(
        post=post, outfit=outfit, defaults={"display_order": display_order}
    )
    return tag


def remove_outfit_tag(post: CreatorPost, outfit_id: int) -> bool:
    """Remove an outfit tag from a post."""
    deleted, _ = CreatorPostOutfit.objects.filter(post=post, outfit_id=outfit_id).delete()
    return deleted > 0
