"""Storefront views: review submissions and the FLASH Points dashboard (Phase 7).

Review mutations are **POST-only** for delete, POST-or-GET for the edit form (the form posts
back to its own URL), and always scoped to the signed-in author -- someone else's review is a
404, never a 403 that confirms the id exists. Every outcome redirects back to the product page
with a message, because the form itself lives there: a failed submission re-renders the page
with the error rather than a bare template.

The product page's review *listing* stays in ``apps.catalog.views`` (it renders there) and
reaches engagement only through services, matching how checkout does it.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.catalog.models import Product
from apps.engagement.forms import ReviewForm
from apps.engagement.models import Review
from apps.engagement.services import loyalty, reviews
from apps.engagement.services.errors import ReviewError

__all__ = ["loyalty_dashboard", "review_create", "review_delete", "review_edit"]


def _to_product(product, *, query: str = ""):
    """Redirect back to the product page, landing the customer on the reviews section."""
    url = f"{product.get_absolute_url()}?{query}" if query else product.get_absolute_url()
    return redirect(f"{url}#reviews")


@require_POST
def review_create(request, slug: str):
    """``POST /products/<slug>/review/`` -- submit a new pending review."""
    product = get_object_or_404(Product, slug=slug)
    if not request.user.is_authenticated:
        messages.error(request, _("Sign in to review this product."))
        return _to_product(product)

    form = ReviewForm(request.POST)
    if form.is_valid():
        try:
            review = reviews.create_review(
                user=request.user,
                product=product,
                rating=form.cleaned_data["rating"],
                title=form.cleaned_data["title"],
                body=form.cleaned_data["body"],
            )
        except ReviewError as exc:
            messages.error(request, exc.message)
            return _to_product(product)
        messages.success(
            request, _("Thanks! Your review is awaiting moderation before it appears.")
        )
        return _to_product(product, query=f"review={review.pk}")
    messages.error(request, _("Please check the rating, title and review and try again."))
    return _to_product(product)


@login_required
def review_edit(request, pk: int):
    """``GET/POST /reviews/<pk>/edit/`` -- the author edits their own review."""
    review = get_object_or_404(Review, pk=pk, author=request.user)
    product = review.product

    if request.method == "POST":
        # Deliberately *no* instance= on the POST: ModelForm cleaning would mutate the review
        # with the submitted values before update_review gets to compare them, and "did the
        # content change?" is what decides whether it returns to moderation.
        form = ReviewForm(request.POST)
        if form.is_valid():
            updated = reviews.update_review(
                review,
                rating=form.cleaned_data["rating"],
                title=form.cleaned_data["title"],
                body=form.cleaned_data["body"],
            )
            if updated.status == Review.Status.PENDING:
                messages.success(request, _("Your changes are saved and awaiting moderation."))
            else:
                messages.success(request, _("Your review has been updated."))
            return _to_product(product)
        messages.error(request, _("Please check the rating, title and review and try again."))
        return _to_product(product)

    return render(
        request,
        "engagement/review_edit.html",
        {"review": review, "form": ReviewForm(instance=review), "product": product},
    )


@login_required
@require_POST
def review_delete(request, pk: int):
    """``POST /reviews/<pk>/delete/`` -- the author deletes their own review."""
    review = get_object_or_404(Review, pk=pk, author=request.user)
    product = review.product
    review.delete()
    messages.success(request, _("Your review has been deleted."))
    return _to_product(product)


@login_required
@never_cache
def loyalty_dashboard(request):
    """``/account/loyalty/`` -- balance, recent movements and upcoming expiries."""
    user = request.user
    transactions = user.points_transactions.select_related("order").order_by("-created_at")
    page = Paginator(transactions, 15).get_page(request.GET.get("page"))
    context = {
        "balance": loyalty.balance_for(user),
        "held": loyalty.held_for(user),
        "transactions": page,
        "expirations": loyalty.upcoming_expirations(user),
        "earn_rate": settings.LOYALTY_EARN_RATE,
        "redeem_rate": settings.LOYALTY_REDEEM_RATE,
        "redeem_increment": settings.LOYALTY_REDEEM_INCREMENT,
        "redemption_message": loyalty.redemption_message(user),
    }
    return render(request, "account/loyalty.html", context)
