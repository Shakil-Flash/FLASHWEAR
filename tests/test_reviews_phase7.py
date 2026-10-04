"""Reviews Phase 7: purchase gating, moderation, the product page and the review API.

Eligibility is tested at the service (the only place the rule lives), then again through HTTP
because the product page is where customers actually meet it. Moderation tests prove status
moves never delete; display tests prove every public surface sees ``published`` only, counts
included. The API is covered separately: anonymous reads, author-scoped writes, and a
non-purchaser rejected before the payload is read (403, not 400). Anonymous *writes* are 403
too: SessionAuthentication offers no ``WWW-Authenticate`` challenge, so DRF coerces its 401
to 403 -- the tests pin the actual contract rather than the RFC preference.
"""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.urls import reverse

from apps.engagement.models import Review
from apps.engagement.services import reviews as review_services
from apps.engagement.services.errors import ReviewError
from tests.phase6_helpers import message_texts, place_and_pay, placed_order
from tests.phase7_helpers import make_review, review_payload

pytestmark = pytest.mark.django_db


@pytest.fixture
def second_reviewer(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email="second-reviewer@flashwear.test", password="Str0ng-Passw0rd!"
    )


@pytest.fixture
def third_reviewer(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email="third-reviewer@flashwear.test", password="Str0ng-Passw0rd!"
    )


@pytest.fixture
def reviewer_factory(db):
    """Unique reviewers for histogram/pagination fixtures."""
    from django.contrib.auth import get_user_model

    def _make(index: int):
        return get_user_model().objects.create_user(
            email=f"reviewer-{index}@flashwear.test", password="Str0ng-Passw0rd!"
        )

    return _make


# =============================================================================
# Eligibility (the rule, at the service)
# =============================================================================


class TestEligibility:
    def test_anonymous_is_told_to_sign_in(self, product):
        allowed, reason = review_services.eligibility(AnonymousUser(), product)

        assert (allowed, reason) == (False, "anonymous")

    def test_a_signed_in_customer_without_a_purchase_may_not_review(self, user, product):
        allowed, reason = review_services.eligibility(user, product)

        assert (allowed, reason) == (False, "purchase_required")

    def test_an_unpaid_order_does_not_earn_the_right(self, user, product):
        placed_order(user, product.variants.first(), stock=10)

        allowed, reason = review_services.eligibility(user, product)

        assert (allowed, reason) == (False, "purchase_required")

    def test_a_paid_purchase_earns_the_right(self, user, product):
        place_and_pay(user, product.variants.first(), stock=10)

        allowed, reason = review_services.eligibility(user, product)

        assert (allowed, reason) == (True, "")

    def test_an_inactive_product_cannot_be_reviewed(self, user, product):
        product.status = product.Status.DRAFT
        product.save(update_fields=["status"])

        allowed, reason = review_services.eligibility(user, product)

        assert (allowed, reason) == (False, "unavailable")

    def test_a_second_review_is_refused_once_one_exists(self, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review_services.create_review(
            user=user, product=product, rating=5, title="Great", body="Really great."
        )

        allowed, reason = review_services.eligibility(user, product)

        assert (allowed, reason) == (False, "already_reviewed")
        assert order.reviews.count() == 1


# =============================================================================
# Creation (the service)
# =============================================================================


class TestCreateReview:
    def test_a_purchase_creates_a_pending_verified_review(self, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)

        review = review_services.create_review(
            user=user, product=product, rating=4, title="Good", body="Good enough, fits well."
        )

        assert review.status == Review.Status.PENDING
        assert review.verified_purchase is True
        assert review.order == order
        assert review.published_at is None

    def test_content_fields_are_required(self, user, product):
        place_and_pay(user, product.variants.first(), stock=10)

        with pytest.raises(ReviewError) as exc:
            review_services.create_review(
                user=user, product=product, rating=5, title="   ", body="x"
            )

        assert exc.value.code == "invalid"

    def test_a_non_purchaser_is_refused_with_the_reason_copy(self, user, product):
        with pytest.raises(ReviewError) as exc:
            review_services.create_review(
                user=user, product=product, rating=5, title="Nice", body="Nice enough."
            )

        assert exc.value.code == "purchase_required"
        assert "Only customers who bought this product" in exc.value.message

    def test_a_duplicate_is_refused(self, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        review_services.create_review(
            user=user, product=product, rating=5, title="Great", body="Really great."
        )

        with pytest.raises(ReviewError) as exc:
            review_services.create_review(
                user=user, product=product, rating=1, title="Bad", body="Changed my mind."
            )

        assert exc.value.code == "already_reviewed"
        assert Review.objects.count() == 1

    def test_the_unique_constraint_maps_a_lost_race_to_a_friendly_error(
        self, user, product, monkeypatch
    ):
        place_and_pay(user, product.variants.first(), stock=10)
        review_services.create_review(
            user=user, product=product, rating=5, title="Great", body="Really great."
        )
        # Two tabs can both pass the existence check; the constraint closes the gap.
        monkeypatch.setattr(review_services, "eligibility", lambda u, p: (True, ""))

        with pytest.raises(ReviewError) as exc:
            review_services.create_review(
                user=user, product=product, rating=1, title="Bad", body="Changed my mind."
            )

        assert exc.value.code == "already_reviewed"
        assert Review.objects.count() == 1


# =============================================================================
# The product page and the submission form
# =============================================================================


class TestReviewFormFlow:
    def test_an_anonymous_visitor_gets_a_sign_in_prompt_not_a_form(self, client, product):
        response = client.get(product.get_absolute_url())

        assert response.status_code == 200
        assert b"Write a review" not in response.content
        assert "to share how this piece worked for you" in response.content.decode()

    def test_a_non_purchaser_sees_why_they_cannot_review(self, client, user, product):
        client.force_login(user)

        response = client.get(product.get_absolute_url())

        assert b"Write a review" not in response.content
        assert "Only customers who bought this product" in response.content.decode()

    def test_a_purchaser_gets_the_form(self, client, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        client.force_login(user)

        response = client.get(product.get_absolute_url())

        assert b"Write a review" in response.content

    def test_an_anonymous_post_never_creates_a_review(self, client, product):
        url = reverse("catalog:review-create", args=[product.slug])

        response = client.post(url, review_payload(), follow=True)

        assert Review.objects.count() == 0
        assert "Sign in to review this product." in message_texts(response)

    def test_a_purchase_submits_a_pending_review_and_lands_on_reviews(self, client, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        client.force_login(user)

        response = client.post(
            reverse("catalog:review-create", args=[product.slug]), review_payload()
        )

        assert response.status_code == 302
        assert response["Location"].endswith("#reviews")
        review = Review.objects.get()
        assert review.status == Review.Status.PENDING
        assert review.author == user

    def test_submitting_twice_keeps_one_review(self, client, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        client.force_login(user)
        url = reverse("catalog:review-create", args=[product.slug])

        client.post(url, review_payload())
        response = client.post(url, review_payload(rating=1, title="Other"), follow=True)

        assert Review.objects.count() == 1
        assert "already reviewed" in message_texts(response).lower()

    def test_an_out_of_range_rating_is_rejected(self, client, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        client.force_login(user)

        response = client.post(
            reverse("catalog:review-create", args=[product.slug]),
            review_payload(rating=9),
            follow=True,
        )

        assert Review.objects.count() == 0
        assert "check the rating" in message_texts(response).lower()

    def test_the_pending_review_is_shown_to_its_author_but_not_in_the_public_list(
        self, client, user, other_user, product
    ):
        from django.test import Client

        place_and_pay(user, product.variants.first(), stock=10)
        client.force_login(user)
        client.post(reverse("catalog:review-create", args=[product.slug]), review_payload())

        author_page = client.get(product.get_absolute_url()).content.decode()
        assert "In moderation" in author_page  # authors may see their own unpublished row
        assert "Wore it all week" in author_page

        stranger = Client()
        stranger.force_login(other_user)
        stranger_page = stranger.get(product.get_absolute_url()).content.decode()
        assert "Wore it all week" not in stranger_page
        assert "In moderation" not in stranger_page

    def test_get_on_the_create_url_is_not_allowed(self, client, product):
        response = client.get(reverse("catalog:review-create", args=[product.slug]))

        assert response.status_code == 405


# =============================================================================
# Editing and deleting (author-scoped)
# =============================================================================


class TestReviewEditing:
    def test_the_author_gets_the_edit_page(self, client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PENDING)
        client.force_login(user)

        response = client.get(reverse("engagement:review-edit", args=[review.pk]))

        assert response.status_code == 200
        assert review.title in response.content.decode()

    def test_someone_elses_review_is_a_404_never_a_403(self, client, user, other_user, product):
        order = place_and_pay(other_user, product.variants.first(), stock=10)
        review = make_review(order, other_user, product, status=Review.Status.PENDING)
        client.force_login(user)

        response = client.get(reverse("engagement:review-edit", args=[review.pk]))

        assert response.status_code == 404

    def test_the_edit_page_requires_sign_in(self, client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PENDING)

        response = client.get(reverse("engagement:review-edit", args=[review.pk]))

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("accounts:login"))

    def test_editing_published_content_returns_it_to_moderation(self, client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PUBLISHED)
        client.force_login(user)

        response = client.post(
            reverse("engagement:review-edit", args=[review.pk]),
            review_payload(title="Rewritten", body="Totally new words here."),
            follow=True,
        )

        review.refresh_from_db()
        assert review.status == Review.Status.PENDING
        assert review.title == "Rewritten"
        assert "awaiting moderation" in message_texts(response)

    def test_an_identical_resubmit_keeps_the_published_status(self, client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PUBLISHED)
        client.force_login(user)

        client.post(
            reverse("engagement:review-edit", args=[review.pk]),
            review_payload(rating=review.rating, title=review.title, body=review.body),
            follow=True,
        )

        review.refresh_from_db()
        assert review.status == Review.Status.PUBLISHED

    def test_editing_someone_elses_review_is_a_404(self, client, user, other_user, product):
        order = place_and_pay(other_user, product.variants.first(), stock=10)
        review = make_review(order, other_user, product, status=Review.Status.PENDING)
        client.force_login(user)

        response = client.post(
            reverse("engagement:review-edit", args=[review.pk]), review_payload()
        )

        assert response.status_code == 404
        review.refresh_from_db()
        assert review.title == "Solid piece"


class TestReviewDeletion:
    def test_the_author_deletes_their_own_review(self, client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PUBLISHED)
        client.force_login(user)

        response = client.post(reverse("engagement:review-delete", args=[review.pk]), follow=True)

        assert Review.objects.count() == 0
        assert "deleted" in message_texts(response).lower()

    def test_deletion_is_post_only(self, client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PUBLISHED)
        client.force_login(user)

        response = client.get(reverse("engagement:review-delete", args=[review.pk]))

        assert response.status_code == 405
        assert Review.objects.filter(pk=review.pk).exists()

    def test_someone_elses_review_cannot_be_deleted(self, client, user, other_user, product):
        order = place_and_pay(other_user, product.variants.first(), stock=10)
        review = make_review(order, other_user, product, status=Review.Status.PUBLISHED)
        client.force_login(user)

        response = client.post(reverse("engagement:review-delete", args=[review.pk]))

        assert response.status_code == 404
        assert Review.objects.filter(pk=review.pk).exists()


# =============================================================================
# Moderation
# =============================================================================


class TestModeration:
    def test_publishing_stamps_the_audit_timestamps(self, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PENDING)

        review_services.set_status(review, Review.Status.PUBLISHED)

        review.refresh_from_db()
        assert review.published_at is not None
        assert review.moderated_at is not None

    def test_the_bulk_action_is_idempotent_but_can_always_unpublish(self, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        first = make_review(order, user, product, status=Review.Status.PENDING)

        published = review_services.moderate(
            Review.objects.filter(pk=first.pk), Review.Status.PUBLISHED
        )
        again = review_services.moderate(
            Review.objects.filter(pk=first.pk), Review.Status.PUBLISHED
        )
        hidden = review_services.moderate(Review.objects.filter(pk=first.pk), Review.Status.HIDDEN)

        assert (published, again, hidden) == (1, 0, 1)
        first.refresh_from_db()
        assert first.status == Review.Status.HIDDEN

    def test_moderation_never_deletes(self, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PENDING)

        review_services.moderate(Review.objects.filter(pk=review.pk), Review.Status.REJECTED)

        assert Review.objects.filter(pk=review.pk, status=Review.Status.REJECTED).exists()

    def test_the_admin_exposes_bulk_moderation_actions(self):
        from apps.engagement.admin import ReviewAdmin

        assert {"approve_selected", "reject_selected", "hide_selected"} <= set(ReviewAdmin.actions)


# =============================================================================
# Display: aggregate, sorting, pagination, JSON-LD
# =============================================================================


class TestProductPageDisplay:
    def test_the_summary_counts_published_reviews_only(
        self, client, user, second_reviewer, third_reviewer, product
    ):
        order = place_and_pay(user, product.variants.first(), stock=10)
        make_review(order, user, product, rating=5)
        make_review(order, second_reviewer, product, rating=3)
        make_review(order, third_reviewer, product, rating=1, status=Review.Status.PENDING)

        response = client.get(product.get_absolute_url())
        summary = response.context["review_summary"]

        assert summary["count"] == 2
        assert summary["average"] == pytest.approx(4.0)
        assert summary["distribution"] == {"1": 0, "2": 0, "3": 1, "4": 0, "5": 1}
        assert sum(summary["shares"].values()) == 100

    def test_an_unknown_sort_falls_back_without_erroring(self, client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        make_review(order, user, product, rating=2)

        response = client.get(f"{product.get_absolute_url()}?reviews_sort=;drop table")

        assert response.status_code == 200
        assert len(response.context["review_page"]) == 1

    def test_sorting_by_rating_is_allowlisted(self, client, user, second_reviewer, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        low = make_review(order, user, product, rating=2)
        high = make_review(order, second_reviewer, product, rating=5)

        response = client.get(f"{product.get_absolute_url()}?reviews_sort=highest")

        assert list(response.context["review_page"]) == [high, low]

    def test_the_list_paginates_with_the_configured_page_size(
        self, client, user, reviewer_factory, product
    ):
        order = place_and_pay(user, product.variants.first(), stock=10)
        for index in range(7):
            make_review(order, reviewer_factory(index), product, rating=5)

        first_page = client.get(product.get_absolute_url())
        second_page = client.get(f"{product.get_absolute_url()}?reviews_page=2")

        assert len(first_page.context["review_page"]) == 6
        assert len(second_page.context["review_page"]) == 1
        assert second_page.context["review_page"].number == 2

    def test_json_ld_carries_the_aggregate_once_a_review_exists(self, client, user, product):
        without = client.get(product.get_absolute_url()).content.decode()
        order = place_and_pay(user, product.variants.first(), stock=10)
        make_review(order, user, product, rating=4)
        with_review = client.get(product.get_absolute_url()).content.decode()

        assert "aggregateRating" not in without
        assert '"aggregateRating"' in with_review
        assert '"ratingValue"' in with_review

    def test_pending_reviews_never_reach_the_aggregate(self, client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        make_review(order, user, product, rating=1, status=Review.Status.PENDING)

        response = client.get(product.get_absolute_url())

        assert response.context["review_summary"]["count"] == 0


# =============================================================================
# The API
# =============================================================================


def _review_url(slug: str) -> str:
    return f"/api/v1/products/{slug}/reviews/"


class TestReviewAPI:
    def test_the_list_is_public_and_published_only(
        self, api_client, user, second_reviewer, product
    ):
        order = place_and_pay(user, product.variants.first(), stock=10)
        make_review(order, user, product, rating=5, title="Public words")
        make_review(
            order,
            second_reviewer,
            product,
            rating=1,
            title="Hidden words",
            status=Review.Status.PENDING,
        )

        response = api_client.get(_review_url(product.slug))

        assert response.status_code == 200
        assert response.data["count"] == 1
        assert response.data["results"][0]["title"] == "Public words"

    def test_the_list_filters_on_verified_purchase(
        self, api_client, user, second_reviewer, product
    ):
        order = place_and_pay(user, product.variants.first(), stock=10)
        make_review(order, user, product, rating=5)
        make_review(order, second_reviewer, product, rating=4, verified=False)

        everything = api_client.get(_review_url(product.slug))
        verified = api_client.get(f"{_review_url(product.slug)}?verified=1")

        assert everything.data["count"] == 2
        assert verified.data["count"] == 1

    def test_an_unknown_sort_parameter_falls_back(self, api_client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        make_review(order, user, product, rating=5)

        response = api_client.get(f"{_review_url(product.slug)}?sort=nonsense")

        assert response.status_code == 200
        assert response.data["count"] == 1

    def test_an_anonymous_post_is_refused(self, api_client, product):
        response = api_client.post(_review_url(product.slug), review_payload())

        assert response.status_code == 403

    def test_a_non_purchaser_is_forbidden_before_the_payload_is_read(
        self, api_client, user, product
    ):
        api_client.force_authenticate(user=user)

        response = api_client.post(_review_url(product.slug), {"rating": "garbage"})

        assert response.status_code == 403
        assert Review.objects.count() == 0

    def test_a_purchaser_creates_a_pending_review_through_the_api(self, api_client, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        api_client.force_authenticate(user=user)

        response = api_client.post(_review_url(product.slug), review_payload())

        assert response.status_code == 201
        assert response.data["status"] == Review.Status.PENDING
        assert response.data["verified_purchase"] is True

    def test_an_invalid_rating_is_a_client_error(self, api_client, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        api_client.force_authenticate(user=user)

        response = api_client.post(_review_url(product.slug), review_payload(rating=9))

        assert response.status_code == 400

    def test_the_payload_cannot_claim_publication(self, api_client, user, product):
        place_and_pay(user, product.variants.first(), stock=10)
        api_client.force_authenticate(user=user)

        response = api_client.post(_review_url(product.slug), review_payload(status="published"))

        assert response.status_code == 201
        assert Review.objects.get().status == Review.Status.PENDING


class TestReviewDetailAPI:
    def test_a_published_review_reads_publicly(self, api_client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PUBLISHED)

        response = api_client.get(f"/api/v1/reviews/{review.pk}/")

        assert response.status_code == 200
        assert response.data["title"] == review.title

    def test_a_pending_review_is_invisible_to_anonymous_readers(self, api_client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PENDING)

        response = api_client.get(f"/api/v1/reviews/{review.pk}/")

        assert response.status_code == 404

    def test_its_author_still_sees_it(self, api_client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PENDING)
        api_client.force_authenticate(user=user)

        response = api_client.get(f"/api/v1/reviews/{review.pk}/")

        assert response.status_code == 200

    def test_anonymous_edits_are_refused(self, api_client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PUBLISHED)

        patched = api_client.patch(f"/api/v1/reviews/{review.pk}/", {"title": "Hijacked"})
        deleted = api_client.delete(f"/api/v1/reviews/{review.pk}/")

        assert patched.status_code == 403
        assert deleted.status_code == 403
        assert Review.objects.filter(pk=review.pk).exists()

    def test_a_foreign_write_is_a_404_not_a_403(self, api_client, user, other_user, product):
        order = place_and_pay(other_user, product.variants.first(), stock=10)
        review = make_review(order, other_user, product, status=Review.Status.PUBLISHED)
        api_client.force_authenticate(user=user)

        patched = api_client.patch(f"/api/v1/reviews/{review.pk}/", {"title": "Mine now"})
        deleted = api_client.delete(f"/api/v1/reviews/{review.pk}/")

        assert patched.status_code == 404
        assert deleted.status_code == 404
        assert Review.objects.filter(pk=review.pk).exists()

    def test_the_author_edits_through_the_service_and_returns_to_moderation(
        self, api_client, user, product
    ):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PUBLISHED)
        api_client.force_authenticate(user=user)

        response = api_client.patch(f"/api/v1/reviews/{review.pk}/", {"title": "A better title"})

        assert response.status_code == 200
        review.refresh_from_db()
        assert review.title == "A better title"
        assert review.status == Review.Status.PENDING

    def test_the_author_deletes_through_the_api(self, api_client, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product, status=Review.Status.PUBLISHED)
        api_client.force_authenticate(user=user)

        response = api_client.delete(f"/api/v1/reviews/{review.pk}/")

        assert response.status_code == 204
        assert not Review.objects.filter(pk=review.pk).exists()


# =============================================================================
# Admin: bulk moderation
# =============================================================================


class TestReviewAdminFlows:
    def _post_action(self, client, admin_user, action, review):
        from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME

        client.force_login(admin_user)
        return client.post(
            reverse("admin:engagement_review_changelist"),
            {ACTION_CHECKBOX_NAME: [str(review.pk)], "action": action},
        )

    def test_the_queue_offers_the_moderation_actions(self, client, admin_user, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        make_review(order, user, product)
        client.force_login(admin_user)

        response = client.get(reverse("admin:engagement_review_changelist"))

        content = response.content.decode()
        assert "Approve selected reviews (publish)" in content
        assert "Reject selected reviews" in content
        assert "Hide selected reviews (unpublish)" in content

    def test_bulk_approve_publishes_a_pending_review(self, client, admin_user, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product)

        response = self._post_action(client, admin_user, "approve_selected", review)

        assert response.status_code == 302
        review.refresh_from_db()
        assert review.status == Review.Status.PUBLISHED
        assert review.published_at is not None

    def test_bulk_reject_and_hide_move_the_status(
        self, client, admin_user, user, product, second_reviewer
    ):
        order = place_and_pay(user, product.variants.first(), stock=10)
        rejected = make_review(order, user, product, title="Rejected one")
        hidden = make_review(order, second_reviewer, product, title="Hidden one", rating=4)

        self._post_action(client, admin_user, "reject_selected", rejected)
        self._post_action(client, admin_user, "hide_selected", hidden)

        rejected.refresh_from_db()
        hidden.refresh_from_db()
        assert rejected.status == Review.Status.REJECTED
        assert hidden.status == Review.Status.HIDDEN

    def test_moderation_never_deletes_anything(self, client, admin_user, user, product):
        order = place_and_pay(user, product.variants.first(), stock=10)
        review = make_review(order, user, product)

        self._post_action(client, admin_user, "reject_selected", review)

        assert Review.objects.filter(pk=review.pk).exists()
