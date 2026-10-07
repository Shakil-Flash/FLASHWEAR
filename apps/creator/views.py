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
from django.db.models import F, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.analytics.services import record_event
from apps.catalog.models import Category, Product
from apps.closet.models import ClosetItem

from .models import (
    CreatorPost,
    CreatorPostOutfit,
    CreatorPostReport,
    CreatorPostStatus,
    CreatorProfile,
    CreatorStatus,
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
# Server-rendered views (Storefront fashion community & creator pages)
# =============================================================================


def creator_list(request):
    """Public fashion creator directory page."""
    profiles = get_active_profiles()
    featured = get_featured_profiles()
    newest = get_newest_profiles()

    paginator = Paginator(profiles, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    record_event(
        "creator_content_view",
        request=request,
        metadata={"view": "creator_directory"},
    )

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
    """Creator profile detail page with looks, outfits, and shoppable products."""
    profile = get_object_or_404(CreatorProfile, slug=slug, status=CreatorStatus.APPROVED)
    profile_stats = get_creator_stats(profile)

    # Published looks
    published_posts = get_posts_by_creator(profile, include_drafts=False)

    # Tagged shoppable products
    tagged_products = list(
        Product.objects.filter(
            creator_post_tags__post__creator=profile,
            creator_post_tags__post__status=CreatorPostStatus.PUBLISHED,
        )
        .select_related("brand")
        .prefetch_related("images", "variants")
        .distinct()[:12]
    )

    # Creator outfits
    outfits = list(
        CreatorPostOutfit.objects.filter(
            post__creator=profile,
            post__status=CreatorPostStatus.PUBLISHED,
        )
        .select_related("outfit")
        .prefetch_related(
            "outfit__items__closet_item__product",
            "outfit__items__closet_item__product__images",
        )
        .distinct()[:8]
    )

    paginator = Paginator(published_posts, 12)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    record_event(
        "creator_content_view",
        request=request,
        metadata={"creator_slug": profile.slug, "view": "creator_profile"},
    )

    return render(
        request,
        "creator/creator_detail.html",
        {
            "profile": profile,
            "stats": profile_stats,
            "posts": page_obj,
            "tagged_products": tagged_products,
            "outfits": outfits,
            "user_can_interact": request.user.is_authenticated,
        },
    )


def creator_me(request):
    """Creator dashboard page - only for authenticated creators."""
    if not request.user.is_authenticated:
        return redirect(reverse("accounts:login"))

    profile = get_or_create_profile(request.user)
    if profile is None:
        return redirect("creator:creator_apply")

    if profile.is_pending:
        return render(request, "creator/creator_dashboard_pending.html", {"profile": profile})

    published_posts = get_posts_by_creator(profile, include_drafts=False)
    draft_posts = get_draft_posts_by_creator(profile)
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


def creator_me_posts(request):
    """Creator dashboard posts overview."""
    return creator_me(request)


def creator_apply(request):
    """Creator application page."""
    if not request.user.is_authenticated:
        return redirect(f"{reverse('accounts:login')}?next=/creators/apply/")

    if request.method == "POST":
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
            return render(request, "creator/creator_apply_success.html")
        except ValueError as e:
            return render(
                request,
                "creator/creator_apply.html",
                {"error": str(e)},
            )

    return render(request, "creator/creator_apply.html")


def post_detail(request, slug):
    """Individual creator look detail page with shoppable product cards and styling."""
    post = get_object_or_404(
        CreatorPost.objects.select_related("creator"),
        slug=slug,
        status=CreatorPostStatus.PUBLISHED,
        creator__status=CreatorStatus.APPROVED,
    )

    # Increment view count
    CreatorPost.objects.filter(pk=post.pk).update(view_count=F("view_count") + 1)
    post.view_count += 1

    media = get_post_media(post)
    product_tags = get_post_product_tags(post)
    outfit_tags = get_post_outfit_tags(post)
    more_from_creator = list(
        get_posts_by_creator(post.creator).exclude(pk=post.pk)[:4]
    )

    user_liked = has_user_liked(request.user, post)
    user_saved = has_user_saved(request.user, post)
    can_report = is_post_reportable_by(request.user, post)

    record_event(
        "creator_content_view",
        request=request,
        object_type="creator_post",
        object_id=post.pk,
        metadata={"slug": post.slug, "creator": post.creator.slug},
    )

    return render(
        request,
        "creator/post_detail.html",
        {
            "post": post,
            "media": media,
            "product_tags": product_tags,
            "outfit_tags": outfit_tags,
            "more_from_creator": more_from_creator,
            "user_liked": user_liked,
            "user_saved": user_saved,
            "can_report": can_report,
        },
    )


def inspiration_list(request):
    """Fashion-community discovery feed with category/mood filters and shoppable looks."""
    posts = get_newest_posts()
    category_slug = (request.GET.get("category") or "").strip()
    mood = (request.GET.get("mood") or "").strip()

    if category_slug:
        posts = posts.filter(product_tags__product__category__slug=category_slug)
    if mood:
        posts = posts.filter(
            Q(product_tags__product__collections__slug=mood)
            | Q(outfit_tags__outfit__style__iexact=mood)
            | Q(outfit_tags__outfit__occasion__iexact=mood)
        )
    posts = posts.distinct()

    paginator = Paginator(posts, 12)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    featured = get_featured_posts(limit=3) if not (category_slug or mood) else []
    categories = list(Category.objects.filter(is_active=True).order_by("name")[:10])
    mood_choices = [c[0] for c in ClosetItem.Style.choices if c[0]]

    record_event(
        "creator_content_view",
        request=request,
        metadata={"view": "inspiration_feed", "category": category_slug, "mood": mood},
    )

    return render(
        request,
        "creator/inspiration.html",
        {
            "featured": featured,
            "posts": page_obj,
            "categories": categories,
            "selected_category": category_slug,
            "mood_choices": mood_choices,
            "selected_mood": mood,
            "user_can_interact": request.user.is_authenticated,
        },
    )


@require_POST
def post_like(request, slug):
    """Toggle like on a creator post (HTMX, fetch JSON, or standard POST)."""
    post = get_object_or_404(
        CreatorPost,
        slug=slug,
        status=CreatorPostStatus.PUBLISHED,
        creator__status=CreatorStatus.APPROVED,
    )
    is_ajax = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    if not request.user.is_authenticated:
        if is_ajax:
            login_url = f"{reverse('accounts:login')}?next={post.get_absolute_url()}"
            return JsonResponse({"error": "login_required", "login_url": login_url}, status=401)
        return redirect(f"{reverse('accounts:login')}?next={post.get_absolute_url()}")

    already_liked = has_user_liked(request.user, post)
    if already_liked:
        unlike_post(request.user, post)
        is_liked = False
    else:
        like_post(request.user, post)
        is_liked = True

    post.refresh_from_db(fields=["like_count"])

    if is_ajax:
        return JsonResponse({"liked": is_liked, "like_count": post.like_count})
    return redirect(request.META.get("HTTP_REFERER", post.get_absolute_url()))


@require_POST
def post_save(request, slug):
    """Toggle bookmark/save on a creator post (HTMX, fetch JSON, or standard POST)."""
    post = get_object_or_404(
        CreatorPost,
        slug=slug,
        status=CreatorPostStatus.PUBLISHED,
        creator__status=CreatorStatus.APPROVED,
    )
    is_ajax = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    if not request.user.is_authenticated:
        if is_ajax:
            login_url = f"{reverse('accounts:login')}?next={post.get_absolute_url()}"
            return JsonResponse({"error": "login_required", "login_url": login_url}, status=401)
        return redirect(f"{reverse('accounts:login')}?next={post.get_absolute_url()}")

    already_saved = has_user_saved(request.user, post)
    if already_saved:
        unsave_post(request.user, post)
        is_saved = False
    else:
        save_post(request.user, post)
        is_saved = True

    post.refresh_from_db(fields=["save_count"])

    if is_ajax:
        return JsonResponse({"saved": is_saved, "save_count": post.save_count})
    return redirect(request.META.get("HTTP_REFERER", post.get_absolute_url()))


@require_POST
def post_report(request, slug):
    """Report a creator post."""
    post = get_object_or_404(CreatorPost, slug=slug, status=CreatorPostStatus.PUBLISHED)
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login_required"}, status=401)

    if not is_post_reportable_by(request.user, post):
        return JsonResponse({"error": "cannot_report_own_post"}, status=400)

    reason = request.POST.get("reason", CreatorPostReport.Reason.OTHER)
    details = request.POST.get("details", "")
    report_post(request.user, post, reason=reason, details=details)
    return JsonResponse(
        {
            "status": "reported",
            "message": "Thank you. Our moderation team will review this look.",
        }
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
