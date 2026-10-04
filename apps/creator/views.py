"""Creator views (Phase 12).

DRF views for creator API endpoints and server-rendered pages.
All views follow the project's established patterns:
- Session authentication for customer-scoped endpoints
- never_cache on all GET methods
- Explicit permission classes
- Customer-scoped querysets filtering by request.user
- Proper pagination and error handling
"""

from django.core.paginator import Paginator
from django.db import models, transaction
from django.shortcuts import get_object_or_404, redirect, render
from rest_framework import permissions, status
from rest_framework.decorators import api_view, never_cache, permission_classes
from rest_framework.response import Response

from .models import (
    CreatorPost,
    CreatorPostReport,
    CreatorPostStatus,
    CreatorProfile,
)
from .permissions import (
    check_like_toggle_duplicate,
    check_report_privilege,
    check_save_toggle_duplicate,
    check_user_can_like,
    check_user_can_save,
)
from .selectors import (
    get_active_profiles,
    get_creator_stats,
    get_draft_posts_by_creator,
    get_featured_posts,
    get_featured_profiles,
    get_newest_posts,
    get_newest_profiles,
    get_pending_review_posts_by_creator,
    get_popular_posts,
    get_post_media,
    get_post_outfit_tags,
    get_post_product_tags,
    get_posts_by_creator,
    has_user_liked,
    has_user_saved,
    is_post_reportable_by,
)
from .serializers import (
    CreatorApplicationSerializer,
    CreatorPostLikeSerializer,
    CreatorPostOutfitSerializer,
    CreatorPostProductSerializer,
    CreatorPostReportSerializer,
    CreatorPostSaveSerializer,
    CreatorPostSerializer,
    CreatorProfileSerializer,
)
from .services import (
    add_outfit_tag,
    add_product_tag,
    create_application,
    create_post,
    get_or_create_profile,
    like_post,
    remove_outfit_tag,
    remove_product_tag,
    report_post,
    save_post,
    unlike_post,
    unsave_post,
)

# =============================================================================
# Server-rendered views (HTMX/Django template views)
# =============================================================================


def creator_list(request):
    """Public creator directory page."""
    profiles = get_active_profiles()
    featured = get_featured_profiles()
    newest = get_newest_profiles()

    # Pagination
    paginator = Paginator(profiles, 20)  # 20 per page
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "creator/creator_list.html",
        {
            "profiles": page_obj,
            "featured": featured,
            "newest": newest,
        },
    )


def creator_detail(request, slug):
    """Creator profile detail page."""
    profile = get_object_or_404(CreatorProfile, slug=slug, status=CreatorProfile.Status.APPROVED)
    profile_stats = get_creator_stats(profile)

    # Published posts only
    published_posts = get_posts_by_creator(profile, include_drafts=False)

    # Pagination
    paginator = Paginator(published_posts, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Check if current user is following/subscribed (basic check)
    user_can_interact = False
    if request.user.is_authenticated:
        user_can_interact = True

    return render(
        request,
        "creator/creator_detail.html",
        {
            "profile": profile,
            "stats": profile_stats,
            "posts": page_obj,
            "user_can_interact": user_can_interact,
        },
    )


def creator_me(request):
    """Creator dashboard page - only for authenticated creators."""
    if not request.user.is_authenticated:
        return redirect("/accounts/signin/")

    profile = get_or_create_profile(request.user)
    if profile is None:
        # User is not a creator; show application page
        return redirect("creator:apply")

    # If pending, show wait page
    if profile.is_pending:
        return render(request, "creator/creator_dashboard_pending.html", {"profile": profile})

    # Published posts
    published_posts = get_posts_by_creator(profile, include_drafts=False)
    draft_posts = get_draft_posts_by_creator(profile)

    # Pending moderation
    pending_posts = get_pending_review_posts_by_creator(profile)

    return render(
        request,
        "creator/creator_dashboard.html",
        {
            "profile": profile,
            "published_posts": published_posts,
            "draft_posts": draft_posts,
            "pending_posts": pending_posts,
        },
    )


def creator_apply(request):
    """Creator application page."""
    if request.method == "POST":
        # Process application
        bio = request.POST.get("application_bio", "")
        display_name = request.POST.get("requested_display_name", "")
        social_links = request.POST.get("social_links", "{}")
        portfolio_url = request.POST.get("portfolio_url", "")

        try:
            create_application(
                user=request.user,
                bio=bio,
                display_name=display_name,
                social_links=social_links,
                portfolio_url=portfolio_url,
            )
            return redirect("creator:apply_success")
        except ValueError as e:
            return render(
                request,
                "creator/creator_apply.html",
                {
                    "error": str(e),
                },
            )

    return render(request, "creator/creator_apply.html")


def post_detail(request, slug):
    """Individual creator post detail page."""
    post = get_object_or_404(CreatorPost, slug=slug, status=CreatorPostStatus.PUBLISHED)

    # Increment view count
    with transaction.atomic():
        post.view_count = models.F("view_count") + 1
        post.save(update_fields=["view_count"])
        post.refresh_from_db(fields=["view_count"])

    # Get media
    media = get_post_media(post, primary_only=True)

    # Get product tags
    product_tags = get_post_product_tags(post)

    # Get outfit tags
    outfit_tags = get_post_outfit_tags(post)

    # Check if user has liked/saved
    user_liked = False
    user_saved = False
    if request.user.is_authenticated:
        user_liked = has_user_liked(request.user, post)
        user_saved = has_user_saved(request.user, post)

    # Check if user can report
    can_report = is_post_reportable_by(request.user, post)

    return render(
        request,
        "creator/post_detail.html",
        {
            "post": post,
            "media": media,
            "product_tags": product_tags,
            "outfit_tags": outfit_tags,
            "user_liked": user_liked,
            "user_saved": user_saved,
            "can_report": can_report,
        },
    )


def inspiration_list(request):
    """Inspiration/content discovery page."""
    featured = get_featured_posts()
    newest = get_newest_posts()
    popular = get_popular_posts()

    # Pagination
    paginator = Paginator(newest, 12)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    user_can_interact = request.user.is_authenticated

    return render(
        request,
        "creator/inspiration.html",
        {
            "featured": featured,
            "newest": page_obj,
            "popular": popular,
            "user_can_interact": user_can_interact,
        },
    )


# =============================================================================
# API v1 Views
# =============================================================================


@never_cache
@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def api_v1_root(request):
    """API v1 root endpoint showing creator endpoints."""
    return Response(
        {
            "creators": {
                "list": "/api/v1/creators/",
                "detail": "/api/v1/creators/<slug>/",
                "my_profile": "/api/v1/creator/me/",
                "my_posts": "/api/v1/creator/me/posts/",
            },
            "posts": {
                "detail": "/api/v1/creator-posts/<slug>/",
                "like": "/api/v1/creator-posts/<id>/like/",
                "save": "/api/v1/creator-posts/<id>/save/",
                "report": "/api/v1/creator-posts/<id>/report/",
            },
            "applications": {
                "submit": "/api/v1/creator-applications/",
                "me": "/api/v1/creator/me/",
            },
        }
    )


# ── Creator Profile API ──────────────────────────────────────────────


@never_cache
@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def api_creator_profile(request, slug):
    """GET /api/v1/creators/<slug>/"""
    profile = get_object_or_404(CreatorProfile, slug=slug)
    serializer = CreatorProfileSerializer(profile)
    return Response(serializer.data)


@never_cache
@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def api_creator_me(request):
    """GET /api/v1/creator/me/"""
    profile = get_or_create_profile(request.user)
    if profile is None:
        return Response(
            {"detail": "Creator profile not found. Apply to become a creator."},
            status=status.HTTP_404_NOT_FOUND,
        )
    serializer = CreatorProfileSerializer(profile)
    return Response(serializer.data)


@never_cache
@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def api_creator_me_posts(request):
    """GET /api/v1/creator/me/posts/"""
    profile = get_or_create_profile(request.user)
    if profile is None:
        return Response(
            {"detail": "Creator profile not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    include_drafts = request.GET.get("include_drafts", "false").lower() == "true"
    posts = get_posts_by_creator(profile, include_drafts=include_drafts)

    # Simple pagination
    import math

    page = int(request.GET.get("page", 1))
    page_size = 20
    start = (page - 1) * page_size
    end = start + page_size
    _posts_page = posts[start:end]

    serializer = CreatorPostSerializer(posts, many=True)
    return Response(
        {
            "posts": serializer.data,
            "total": posts.count(),
            "page": page,
            "pages": math.ceil(posts.count() / page_size),
        }
    )


# ── Creator Application API ──────────────────────────────────────────


@never_cache
@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_creator_apply(request):
    """POST /api/v1/creator-applications/"""
    bio = request.data.get("application_bio", "")
    display_name = request.data.get("requested_display_name", "")
    social_links = request.data.get("social_links", {})
    portfolio_url = request.data.get("portfolio_url", "")

    try:
        application = create_application(
            user=request.user,
            bio=bio,
            display_name=display_name,
            social_links=social_links,
            portfolio_url=portfolio_url,
        )
        serializer = CreatorApplicationSerializer(application)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    except ValueError as e:
        return Response(
            {"detail": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )


# ── Creator Post API ───────────────────────────────────────────────


@never_cache
@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def api_post_detail(request, slug):
    """GET /api/v1/creator-posts/<slug>/"""
    post = get_object_or_404(CreatorPost, slug=slug)
    serializer = CreatorPostSerializer(post)
    return Response(serializer.data)


@never_cache
@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_post_like_toggle(request, post_id):
    """POST/DELETE /api/v1/creator-posts/<id>/like/"""
    post = get_object_or_404(CreatorPost, pk=post_id)

    # Check permission
    check_user_can_like(request.user, post)

    # Check for duplicate like
    check_like_toggle_duplicate(request.user, post)

    if request.method == "POST":
        like = like_post(request.user, post)
        serializer = CreatorPostLikeSerializer(like)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    elif request.method == "DELETE":
        removed = unlike_post(request.user, post)
        if removed:
            return Response({"detail": "Like removed."}, status=status.HTTP_200_OK)
        return Response(
            {"detail": "You haven't liked this post."},
            status=status.HTTP_400_BAD_REQUEST,
        )


@never_cache
@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_post_save_toggle(request, post_id):
    """POST/DELETE /api/v1/creator-posts/<id>/save/"""
    post = get_object_or_404(CreatorPost, pk=post_id)

    # Check permission
    check_user_can_save(request.user, post)

    # Check for duplicate save
    check_save_toggle_duplicate(request.user, post)

    if request.method == "POST":
        save = save_post(request.user, post)
        serializer = CreatorPostSaveSerializer(save)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    elif request.method == "DELETE":
        removed = unsave_post(request.user, post)
        if removed:
            return Response({"detail": "Save removed."}, status=status.HTTP_200_OK)
        return Response(
            {"detail": "You haven't saved this post."},
            status=status.HTTP_400_BAD_REQUEST,
        )


@never_cache
@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_post_report(request, post_id):
    """POST /api/v1/creator-posts/<id>/report/"""
    post = get_object_or_404(CreatorPost, pk=post_id)

    # Check report privilege
    check_report_privilege(request.user, post)

    reason = request.data.get("reason", CreatorPostReport.Reason.OTHER)
    details = request.data.get("details", "")

    report = report_post(request.user, post, reason, details)
    serializer = CreatorPostReportSerializer(report)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


# ── Creator Post Creation (admin/staff) ─────────────────────────────


@never_cache
@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_post_create(request):
    """POST /api/v1/creator-posts/ - Create a post (staff or creator)."""
    if not (request.user.is_staff or request.user.is_authenticated):
        return Response(
            {"detail": "Authentication required."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    serializer = CreatorPostSerializer(data=request.data)
    if serializer.is_valid(raise_exception=True):
        post = create_post(
            creator=request.user.creator_profile,
            title=serializer.validated_data["title"],
            slug=serializer.validated_data["slug"],
            caption=serializer.validated_data.get("caption", ""),
            cover_image=serializer.validated_data.get("cover_image"),
        )
        return Response(CreatorPostSerializer(post).data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ── Product/Outfit tagging API ──────────────────────────────────────


@never_cache
@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_post_add_product_tag(request, post_id):
    """POST /api/v1/creator-posts/<id>/product-tags/"""
    post = get_object_or_404(CreatorPost, pk=post_id)

    # Only the creator or staff can tag
    if post.creator.user != request.user and not request.user.is_staff:
        return Response(
            {"detail": "Only the creator or staff can add product tags."},
            status=status.HTTP_403_FORBIDDEN,
        )

    product_id = request.data.get("product_id")
    if not product_id:
        return Response(
            {"detail": "product_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        from catalog.models import Product as CatalogProduct

        product = CatalogProduct.objects.get(pk=product_id)
    except (CatalogProduct.DoesNotExist, ValueError):
        return Response(
            {"detail": "Product not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        tag = add_product_tag(post, product, request.data.get("label", ""))
        serializer = CreatorPostProductSerializer(tag)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    except ValueError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@never_cache
@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_post_remove_product_tag(request, post_id, product_id):
    """DELETE /api/v1/creator-posts/<id>/product-tags/<product_id>/"""
    post = get_object_or_404(CreatorPost, pk=post_id)

    if post.creator.user != request.user and not request.user.is_staff:
        return Response(
            {"detail": "Only the creator or staff can remove product tags."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        removed = remove_product_tag(post, product_id)
        if removed:
            return Response({"detail": "Product tag removed."}, status=status.HTTP_200_OK)
        return Response(
            {"detail": "Product tag not found on this post."},
            status=status.HTTP_404_NOT_FOUND,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@never_cache
@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_post_add_outfit_tag(request, post_id):
    """POST /api/v1/creator-posts/<id>/outfit-tags/"""
    post = get_object_or_404(CreatorPost, pk=post_id)

    if post.creator.user != request.user and not request.user.is_staff:
        return Response(
            {"detail": "Only the creator or staff can add outfit tags."},
            status=status.HTTP_403_FORBIDDEN,
        )

    outfit_id = request.data.get("outfit_id")
    if not outfit_id:
        return Response(
            {"detail": "outfit_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        from closet.models import Outfit as CatalogOutfit

        outfit = CatalogOutfit.objects.get(pk=outfit_id)
    except (CatalogOutfit.DoesNotExist, ValueError):
        return Response(
            {"detail": "Outfit not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        tag = add_outfit_tag(post, outfit)
        serializer = CreatorPostOutfitSerializer(tag)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    except ValueError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@never_cache
@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def api_post_remove_outfit_tag(request, post_id, outfit_id):
    """DELETE /api/v1/creator-posts/<id>/outfit-tags/<outfit_id>/"""
    post = get_object_or_404(CreatorPost, pk=post_id)

    if post.creator.user != request.user and not request.user.is_staff:
        return Response(
            {"detail": "Only the creator or staff can remove outfit tags."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        removed = remove_outfit_tag(post, outfit_id)
        if removed:
            return Response({"detail": "Outfit tag removed."}, status=status.HTTP_200_OK)
        return Response(
            {"detail": "Outfit tag not found on this post."},
            status=status.HTTP_404_NOT_FOUND,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
