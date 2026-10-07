"""Creator selectors for reading approved and public creator data."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.creator.models import (
    CreatorPost,
    CreatorPostLike,
    CreatorPostMedia,
    CreatorPostOutfit,
    CreatorPostProduct,
    CreatorPostSave,
    CreatorPostStatus,
    CreatorProfile,
    CreatorStatus,
)


def get_active_profiles() -> QuerySet[CreatorProfile]:
    """Return all approved creator profiles."""
    return CreatorProfile.objects.filter(status=CreatorStatus.APPROVED).order_by("display_name")


def get_featured_profiles(limit: int = 6) -> list[CreatorProfile]:
    """Return featured approved creator profiles."""
    return list(
        CreatorProfile.objects.filter(status=CreatorStatus.APPROVED, is_featured=True).order_by(
            "-updated_at"
        )[:limit]
    )


def get_newest_profiles(limit: int = 6) -> list[CreatorProfile]:
    """Return newest approved creator profiles."""
    return list(
        CreatorProfile.objects.filter(status=CreatorStatus.APPROVED).order_by("-created_at")[:limit]
    )


def get_creator_stats(profile: CreatorProfile) -> dict[str, int]:
    """Compute verified stats for a creator profile."""
    published_posts = profile.posts.filter(status=CreatorPostStatus.PUBLISHED)
    return {
        "post_count": published_posts.count(),
        "total_likes": sum(p.like_count for p in published_posts),
        "total_saves": sum(p.save_count for p in published_posts),
    }


def get_posts_by_creator(
    profile: CreatorProfile, include_drafts: bool = False
) -> QuerySet[CreatorPost]:
    """Return posts belonging to a creator, filtered by publication state."""
    qs = profile.posts.all().select_related("creator").prefetch_related("product_tags__product")
    if not include_drafts:
        qs = qs.filter(status=CreatorPostStatus.PUBLISHED)
    return qs.order_by("-published_at", "-created_at")


def get_draft_posts_by_creator(profile: CreatorProfile) -> QuerySet[CreatorPost]:
    """Return draft posts for the creator's dashboard."""
    return profile.posts.filter(status=CreatorPostStatus.DRAFT).order_by("-updated_at")


def get_pending_review_posts_by_creator(profile: CreatorProfile) -> QuerySet[CreatorPost]:
    """Return posts awaiting review."""
    return profile.posts.filter(status=CreatorPostStatus.PENDING_REVIEW).order_by("-updated_at")


def get_featured_posts(limit: int = 6) -> list[CreatorPost]:
    """Return featured community posts with approved creators."""
    return list(
        CreatorPost.objects.filter(
            status=CreatorPostStatus.PUBLISHED,
            creator__status=CreatorStatus.APPROVED,
        )
        .select_related("creator")
        .prefetch_related(
            "product_tags__product",
            "product_tags__product__images",
            "product_tags__product__variants",
        )
        .order_by("-like_count", "-published_at")[:limit]
    )


def get_newest_posts(limit: int = 24) -> QuerySet[CreatorPost]:
    """Return newest published creator posts."""
    return (
        CreatorPost.objects.filter(
            status=CreatorPostStatus.PUBLISHED,
            creator__status=CreatorStatus.APPROVED,
        )
        .select_related("creator")
        .prefetch_related(
            "product_tags__product",
            "product_tags__product__images",
            "product_tags__product__variants",
        )
        .order_by("-published_at", "-created_at")
    )


def get_popular_posts(limit: int = 8) -> list[CreatorPost]:
    """Return popular published creator posts."""
    return list(
        CreatorPost.objects.filter(
            status=CreatorPostStatus.PUBLISHED,
            creator__status=CreatorStatus.APPROVED,
        )
        .select_related("creator")
        .prefetch_related(
            "product_tags__product",
            "product_tags__product__images",
            "product_tags__product__variants",
        )
        .order_by("-like_count", "-view_count")[:limit]
    )


def get_post_media(post: CreatorPost, primary_only: bool = False) -> list[CreatorPostMedia]:
    """Get media attached to a post."""
    qs = post.media.all().order_by("-is_primary", "sort_order", "id")
    if primary_only:
        primary = qs.filter(is_primary=True).first() or qs.first()
        return [primary] if primary else []
    return list(qs)


def get_post_product_tags(post: CreatorPost) -> list[CreatorPostProduct]:
    """Get shoppable products tagged on a post."""
    return list(
        post.product_tags.select_related("product", "product__brand")
        .prefetch_related("product__images", "product__variants")
        .order_by("display_order", "id")
    )


def get_post_outfit_tags(post: CreatorPost) -> list[CreatorPostOutfit]:
    """Get outfits tagged on a post."""
    return list(
        post.outfit_tags.select_related("outfit")
        .prefetch_related(
            "outfit__items",
            "outfit__items__closet_item",
            "outfit__items__closet_item__product",
            "outfit__items__closet_item__product__images",
            "outfit__items__closet_item__product__variants",
        )
        .order_by("display_order", "id")
    )


def has_user_liked(user: Any, post: CreatorPost) -> bool:
    """Check whether a user has liked a given post."""
    if not (user and user.is_authenticated):
        return False
    return CreatorPostLike.objects.filter(user=user, post=post).exists()


def has_user_saved(user: Any, post: CreatorPost) -> bool:
    """Check whether a user has saved a given post."""
    if not (user and user.is_authenticated):
        return False
    return CreatorPostSave.objects.filter(user=user, post=post).exists()


def is_post_reportable_by(user: Any, post: CreatorPost) -> bool:
    """Check if post is reportable by user (authenticated and not own post)."""
    if not (user and user.is_authenticated):
        return False
    return post.creator.user_id != user.pk


# ── Phase 32: Community Integrations ─────────────────────────────────


def get_community_posts_for_product(product: Any, limit: int = 6) -> list[CreatorPost]:
    """Find published creator posts that feature this catalog product."""
    tags = (
        CreatorPostProduct.objects.filter(
            product=product,
            post__status=CreatorPostStatus.PUBLISHED,
            post__creator__status=CreatorStatus.APPROVED,
        )
        .select_related("post", "post__creator")
        .prefetch_related(
            "post__product_tags",
            "post__product_tags__product",
            "post__product_tags__product__images",
            "post__outfit_tags",
            "post__outfit_tags__outfit",
        )
        .order_by("-post__like_count", "-post__published_at")[:limit]
    )
    posts = []
    seen = set()
    for tag in tags:
        if tag.post_id not in seen:
            seen.add(tag.post_id)
            posts.append(tag.post)
    return posts


def get_community_outfits_for_product(product: Any, limit: int = 4) -> list[CreatorPostOutfit]:
    """Find creator outfits featuring this product."""
    return list(
        CreatorPostOutfit.objects.filter(
            post__product_tags__product=product,
            post__status=CreatorPostStatus.PUBLISHED,
            post__creator__status=CreatorStatus.APPROVED,
        )
        .select_related("outfit", "post", "post__creator")
        .prefetch_related(
            "outfit__items__closet_item__product",
            "outfit__items__closet_item__product__images",
        )
        .order_by("-post__like_count")[:limit]
    )
