"""FLASH Quests & Rewards (Phase 14): quests, progress, completion, rewards, badges.

The contract these tests protect:

* **progress is server-derived** -- every counter reads rows the platform itself wrote
  (orders, reviews, closet, outfits, wishlist, DNA, drops, loop); no endpoint, payload or
  client-supplied number can move it, and completion has no endpoint at all;
* **rewards are issued exactly once, automatically, at completion** -- one ledger
  ``BONUS`` row keyed ``quest:<id>:user:<id>[:period]``, one ``(user, badge)`` row, both
  idempotent under double syncs, replays and racing updates;
* **completed is terminal** -- later refunds, deletions or window endings never claw a
  reward back, and a late sweep cannot resurrect an expired quest;
* **windows and periods are authoritative** -- a quest counts only what happened inside
  its window (and its current UTC bucket for repeatables), and repeatable quests key
  participation rows per period without clobbering history;
* **every user surface is personal** -- no IDOR, no self-award, no writable progress field
  on any API, HTML form or admin screen (staff included).
"""

from __future__ import annotations

import itertools
import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib.admin.sites import site
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.closet.models import ClosetItem, Outfit
from apps.drops.models import DropProduct, FlashDrop
from apps.engagement.models import PointsTransaction, Review
from apps.loop.models import LoopItem
from apps.orders.services import cancel_order
from apps.quests.models import Badge, Quest, UserBadge, UserQuest
from apps.quests.services import badges as badge_service
from apps.quests.services import events, handlers
from apps.quests.services import progress as progress_service
from apps.quests.services import rewards as reward_service
from apps.quests.services.errors import QuestConfigurationError, QuestNotEligibleError
from apps.quests.services.periods import effective_since, period_key_for
from apps.quests.tasks import sweep_progress
from apps.shop.models import Wishlist, WishlistItem
from apps.styling.models import FlashDNA
from tests.phase6_helpers import message_texts, place_and_pay, placed_order
from tests.phase7_helpers import make_review

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------
# Builders: rows are written directly only where the service would be the thing under
# test; anything behavioural (orders, reviews) goes through the real services.
# --------------------------------------------------------------------------------------


_QUEST_SLUGS = itertools.count(1)


def make_quest(**overrides) -> Quest:
    params = {
        # Slugs are unique, so the default one has to move with the call.
        "slug": f"quest-{next(_QUEST_SLUGS)}",
        "name": "Place your first order",
        "quest_type": Quest.QuestType.ORDER_PAID,
        "target": 1,
        "publish_state": Quest.PublishState.PUBLISHED,
        "points_reward": 100,
    }
    params.update(overrides)
    return Quest.objects.create(**params)


def closet_item(user, name="White tee") -> ClosetItem:
    return ClosetItem.objects.create(user=user, category=ClosetItem.Category.TOPS, name=name)


def saved_outfit(user, name="Friday fit") -> Outfit:
    return Outfit.objects.create(user=user, name=name, status=Outfit.Status.SAVED)


def wishlisted(user, product, variant=None) -> WishlistItem:
    wishlist, _created = Wishlist.objects.get_or_create(user=user)
    return WishlistItem.objects.create(wishlist=wishlist, product=product, variant=variant)


def dna_profile(user, styles="minimal") -> FlashDNA:
    return FlashDNA.objects.create(user=user, styles=styles)


def drop_holding(product) -> FlashDrop:
    drop = FlashDrop.objects.create(name="Neon Drop", slug="neon-drop")
    DropProduct.objects.create(drop=drop, product=product)
    return drop


def loop_row(user, loop_type, status, *, price=None) -> LoopItem:
    return LoopItem.objects.create(
        user=user,
        type=loop_type,
        status=status,
        condition=LoopItem.Condition.GOOD,
        asking_price=price,
    )


def complete_quest(user, quest, *, now=None) -> UserQuest:
    """Drive the engine to completion for a quest whose data already satisfies the target."""
    row = progress_service.sync_user_quests(user, now=now)
    completed = [r for r in row if r.quest_id == quest.pk and r.is_completed]
    assert completed, f"{quest.slug} did not complete"
    return completed[0]


# --------------------------------------------------------------------------------------
# Model rules and derived state
# --------------------------------------------------------------------------------------


class TestQuestModelRules:
    def test_state_is_derived_from_publish_and_window(self):
        now = timezone.now()
        assert make_quest(publish_state=Quest.PublishState.DRAFT).state_at(now) == "draft"
        assert make_quest(publish_state=Quest.PublishState.ARCHIVED).state_at(now) == "archived"
        assert make_quest(start_at=now + timedelta(days=1)).state_at(now) == "scheduled"
        assert make_quest(end_at=now - timedelta(hours=1)).state_at(now) == "ended"
        assert make_quest().state_at(now) == "active"

    def test_boolean_quests_are_forced_once_with_target_one(self):
        quest = make_quest(
            quest_type=Quest.QuestType.DNA_COMPLETED,
            objective_type=Quest.ObjectiveType.BOOLEAN,
            repeat_period=Quest.RepeatPeriod.DAILY,
            target=7,
        )

        assert quest.repeat_period == Quest.RepeatPeriod.NONE
        assert quest.target == 1
        assert quest.effective_target == 1
        assert quest.is_once

    def test_a_boolean_quest_cannot_be_made_repeatable_at_the_database(self):
        quest = make_quest(objective_type=Quest.ObjectiveType.BOOLEAN)

        with pytest.raises(IntegrityError), transaction.atomic():
            Quest.objects.filter(pk=quest.pk).update(repeat_period=Quest.RepeatPeriod.DAILY)

    def test_the_window_must_end_after_it_starts(self):
        now = timezone.now()
        unsaved = Quest(
            slug="backwards",
            name="Backwards window",
            quest_type=Quest.QuestType.ORDER_PAID,
            start_at=now,
            end_at=now - timedelta(days=1),
        )

        with pytest.raises(ValidationError) as excinfo:
            unsaved.full_clean()

        assert "end_at" in excinfo.value.error_dict

        # And the constraint holds even for a write that skips validation entirely.
        with pytest.raises(IntegrityError), transaction.atomic():
            make_quest(start_at=now, end_at=now - timedelta(days=1))

    def test_completion_transitions_are_terminal(self):
        quest = make_quest()
        active = UserQuest(quest=quest)
        active.user_id = 1  # status logic does not touch the database
        assert active.can_transition_to(UserQuest.Status.COMPLETED) is True
        assert active.can_transition_to(UserQuest.Status.ACTIVE) is False

        completed = UserQuest(
            quest=quest, status=UserQuest.Status.COMPLETED, completed_at=timezone.now()
        )
        completed.user_id = 1
        assert completed.can_transition_to(UserQuest.Status.ACTIVE) is False

    def test_participation_requires_one_row_per_period(self, user):
        quest = make_quest()

        UserQuest.objects.create(user=user, quest=quest, period_key="")
        with pytest.raises(IntegrityError), transaction.atomic():
            UserQuest.objects.create(user=user, quest=quest, period_key="")

    def test_completion_consistency_constraint(self, user):
        quest = make_quest()

        with pytest.raises(IntegrityError), transaction.atomic():
            UserQuest.objects.create(
                user=user,
                quest=quest,
                status=UserQuest.Status.COMPLETED,
                completed_at=None,
            )


# --------------------------------------------------------------------------------------
# Calendar buckets
# --------------------------------------------------------------------------------------


class TestPeriodBuckets:
    def test_once_quests_key_on_the_empty_period(self):
        quest = make_quest()
        assert period_key_for(quest, timezone.now()) == ""

    @pytest.mark.parametrize(
        ("repeat", "moment", "expected"),
        [
            (Quest.RepeatPeriod.DAILY, "2026-10-04T15:30:00+00:00", "20261004"),
            (Quest.RepeatPeriod.WEEKLY, "2026-10-04T15:30:00+00:00", "2026-W40"),
            (Quest.RepeatPeriod.MONTHLY, "2026-10-04T15:30:00+00:00", "202610"),
        ],
    )
    def test_repeatable_periods_use_utc_buckets(self, repeat, moment, expected):
        from datetime import datetime

        quest = make_quest(repeat_period=repeat, objective_type=Quest.ObjectiveType.COUNT)
        assert period_key_for(quest, datetime.fromisoformat(moment)) == expected

    def test_effective_since_is_the_later_of_window_and_period_start(self):
        from datetime import datetime

        now = datetime.fromisoformat("2026-10-04T15:30:00+00:00")
        windowed = make_quest(start_at=timezone.make_aware(datetime(2026, 10, 1)))
        assert effective_since(windowed, now) == windowed.start_at

        daily = make_quest(repeat_period=Quest.RepeatPeriod.DAILY)
        since = effective_since(daily, now)
        assert since is not None
        assert since.isoformat().startswith("2026-10-04T00:00:00")

        bare = make_quest()
        assert effective_since(bare, now) is None or effective_since(bare, now) == bare.start_at


# --------------------------------------------------------------------------------------
# Participation (start + lazy rows)
# --------------------------------------------------------------------------------------


class TestParticipation:
    def test_starting_creates_a_zero_progress_row(self, user):
        quest = make_quest()

        row = progress_service.start_quest(user, quest)

        assert row.progress == 0
        assert row.status == UserQuest.Status.ACTIVE
        assert row.started_at is not None
        assert row.period_key == ""

    def test_starting_twice_returns_the_same_row(self, user):
        quest = make_quest()

        first = progress_service.start_quest(user, quest)
        second = progress_service.start_quest(user, quest)

        assert first.pk == second.pk
        assert UserQuest.objects.filter(user=user).count() == 1

    def test_a_scheduled_quest_cannot_be_started_early(self, user):
        quest = make_quest(start_at=timezone.now() + timedelta(days=1))

        with pytest.raises(QuestNotEligibleError) as excinfo:
            progress_service.start_quest(user, quest)

        assert excinfo.value.code == "quest_state_scheduled"

    def test_an_ended_quest_cannot_be_started_late(self, user):
        quest = make_quest(end_at=timezone.now() - timedelta(hours=1))

        with pytest.raises(QuestNotEligibleError):
            progress_service.start_quest(user, quest)

    def test_a_draft_quest_cannot_be_started(self, user):
        quest = make_quest(publish_state=Quest.PublishState.DRAFT)

        with pytest.raises(QuestNotEligibleError):
            progress_service.start_quest(user, quest)

    def test_zero_progress_earns_no_row_until_there_is_progress(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=3)

        assert progress_service.progress_for(user, quest).row is None

        closet_item(user)
        view = progress_service.progress_for(user, quest)

        assert view.row is not None
        assert view.progress == 1

    def test_persist_false_never_writes(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=3)
        closet_item(user)

        view = progress_service.progress_for(user, quest, persist=False)

        assert view.progress == 1
        assert UserQuest.objects.filter(user=user).count() == 0


# --------------------------------------------------------------------------------------
# Authoritative handlers: one test per subsystem
# --------------------------------------------------------------------------------------


class TestHandlersMeasureRealData:
    def test_paid_orders_count_and_unpaid_and_cancelled_do_not(self, user, product):
        quest = make_quest(quest_type=Quest.QuestType.ORDER_PAID, target=5)
        variant = product.variants.first()

        unpaid = placed_order(user, variant, stock=10)
        cancel_order(unpaid, actor=user)
        place_and_pay(user, variant, stock=None)  # re-seeding here would clobber the hold

        unpaid.refresh_from_db()
        assert unpaid.status == type(unpaid).Status.CANCELLED
        assert handlers.order_paid(user, quest, None) == 1

    def test_only_published_reviews_count(self, user, product):
        quest = make_quest(quest_type=Quest.QuestType.REVIEW_WRITTEN, target=5)
        order = place_and_pay(user, product.variants.first())
        make_review(order, user, product, status=Review.Status.PENDING)

        assert handlers.review_written(user, quest, None) == 0

        Review.objects.filter(author=user).update(
            status=Review.Status.PUBLISHED, published_at=timezone.now()
        )
        assert handlers.review_written(user, quest, None) == 1

    def test_dna_counts_as_one_when_preferences_exist(self, user):
        quest = make_quest(quest_type=Quest.QuestType.DNA_COMPLETED)

        assert handlers.dna_completed(user, quest, None) == 0
        dna_profile(user)
        assert handlers.dna_completed(user, quest, None) == 1

    def test_closet_counts_rows_in_the_window_only(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=9)
        item = closet_item(user)

        assert handlers.closet_add(user, quest, None) == 1

        ClosetItem.objects.filter(pk=item.pk).update(created_at=timezone.now() - timedelta(days=30))
        assert handlers.closet_add(user, quest, timezone.now() - timedelta(days=7)) == 0

    def test_only_saved_outfits_count(self, user):
        quest = make_quest(quest_type=Quest.QuestType.OUTFIT_SAVED, target=9)
        saved_outfit(user)
        Outfit.objects.create(user=user, name="Draft look", status=Outfit.Status.DRAFT)

        assert handlers.outfit_saved(user, quest, None) == 1

    def test_wishlist_counts_distinct_products(self, product, make_product, colour, size, user):
        quest = make_quest(quest_type=Quest.QuestType.WISHLIST_ADD, target=9)
        second = make_product(name="Canvas Tote", slug="canvas-tote")
        from apps.catalog.models import ProductVariant

        ProductVariant.objects.create(
            product=second, sku="FW-TOTE-SND-M", color=colour, size=size, price=Decimal("39.00")
        )

        wishlisted(user, product)
        wishlisted(user, product, variant=product.variants.first())  # same shirt, again
        wishlisted(user, second)

        assert handlers.wishlist_add(user, quest, None) == 2

    def test_drop_purchases_only_count_drop_products(self, user, product):
        quest = make_quest(quest_type=Quest.QuestType.DROP_PURCHASED, target=5)
        drop_holding(product)

        place_and_pay(user, product.variants.first())
        assert handlers.drop_purchased(user, quest, None) == 1

    def test_profile_completion_needs_name_and_one_contact_detail(self, user):
        quest = make_quest(quest_type=Quest.QuestType.PROFILE_COMPLETED)

        assert handlers.profile_completed(user, quest, None) == 0

        user.first_name = "Ada"
        user.last_name = "Lovelace"
        user.save()
        assert handlers.profile_completed(user, quest, None) == 0

        profile = user.profile
        profile.phone = "+1 555 0100"
        profile.save()
        assert handlers.profile_completed(user, quest, None) == 1

    @pytest.mark.parametrize(
        ("loop_type", "qualifying", "not_qualifying"),
        [
            (
                LoopItem.Type.RESALE,
                [LoopItem.Status.LISTED, LoopItem.Status.SOLD],
                [LoopItem.Status.SUBMITTED, LoopItem.Status.APPROVED],
            ),
            (
                LoopItem.Type.TRADE_IN,
                [LoopItem.Status.TRADE_IN_ACCEPTED, LoopItem.Status.TRADE_IN_COMPLETED],
                [LoopItem.Status.SUBMITTED, LoopItem.Status.UNDER_REVIEW],
            ),
            (
                LoopItem.Type.RECYCLE,
                [LoopItem.Status.RECYCLE_ACCEPTED, LoopItem.Status.RECYCLE_COMPLETED],
                [LoopItem.Status.DRAFT, LoopItem.Status.REJECTED],
            ),
        ],
    )
    def test_loop_quests_count_moderated_progress(
        self, user, loop_type, qualifying, not_qualifying
    ):
        quest_type = {
            LoopItem.Type.RESALE: Quest.QuestType.LOOP_RESALE,
            LoopItem.Type.TRADE_IN: Quest.QuestType.LOOP_TRADE_IN,
            LoopItem.Type.RECYCLE: Quest.QuestType.LOOP_RECYCLE,
        }[loop_type]
        quest = make_quest(quest_type=quest_type, target=9)

        for status in not_qualifying:
            loop_row(user, loop_type, status, price=Decimal("20.00"))
        assert handlers.compute_raw(user, quest, None) == 0

        for status in qualifying:
            loop_row(user, loop_type, status, price=Decimal("20.00"))
        assert handlers.compute_raw(user, quest, None) == len(qualifying)

    def test_creator_quests_measure_zero_while_the_app_is_dormant(self, user, settings):
        settings.INSTALLED_APPS = [app for app in settings.INSTALLED_APPS if app != "apps.creator"]
        assert "apps.creator" not in settings.INSTALLED_APPS
        quest = make_quest(quest_type=Quest.QuestType.CREATOR_POST)

        view = progress_service.progress_for(user, quest)

        assert view.progress == 0
        assert view.row is None

    def test_an_unknown_quest_type_is_a_loud_configuration_error(self, user):
        quest = make_quest(quest_type="not-a-real-type")

        with pytest.raises(QuestConfigurationError):
            handlers.compute_raw(user, quest, None)


# --------------------------------------------------------------------------------------
# Completion, windows and the terminal rule
# --------------------------------------------------------------------------------------


class TestCompletionRules:
    def test_sync_completes_and_records_the_reference(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=2, points_reward=50)
        closet_item(user)
        closet_item(user, name="Second tee")

        row = complete_quest(user, quest)

        assert row.status == UserQuest.Status.COMPLETED
        assert row.progress == 2
        assert row.completed_at is not None
        assert row.reward_reference == f"quest:{quest.pk}:user:{user.pk}"

    def test_progress_is_clamped_to_the_target(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=2)
        for i in range(5):
            closet_item(user, name=f"Tee {i}")

        row = complete_quest(user, quest)

        assert row.progress == 2

    def test_completed_rows_are_frozen_even_if_the_data_disappears(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=10)
        closet_item(user)
        complete_quest(user, quest)

        ClosetItem.objects.filter(user=user).delete()
        progress_service.sync_user_quests(user)

        row = UserQuest.objects.get(user=user, quest=quest)
        assert row.is_completed
        assert row.progress == 1

    def test_a_refund_does_not_claw_back_a_completion(self, user, product):
        quest = make_quest(quest_type=Quest.QuestType.ORDER_PAID, target=1, points_reward=100)
        order = place_and_pay(user, product.variants.first())
        complete_quest(user, quest)

        # The refund workflow itself does not exist yet (Phase 6/7); simulate its row.
        type(order).objects.filter(pk=order.pk).update(status=type(order).Status.REFUNDED)
        progress_service.sync_user_quests(user)

        row = UserQuest.objects.get(user=user, quest=quest)
        assert row.is_completed
        assert (
            PointsTransaction.objects.filter(
                transaction_type=PointsTransaction.TransactionType.BONUS,
                reference=f"quest:{quest.pk}:user:{user.pk}",
            ).count()
            == 1
        )

    def test_an_ended_quest_is_frozen_and_cannot_complete_late(self, user):
        quest = make_quest(
            quest_type=Quest.QuestType.CLOSET_ADD,
            target=2,
            end_at=timezone.now() + timedelta(hours=1),
        )
        closet_item(user)
        progress_service.sync_user_quests(user)
        row = UserQuest.objects.get(user=user, quest=quest)
        assert row.progress == 1 and not row.is_completed

        ClosetItem.objects.filter(user=user).update(
            created_at=timezone.now() - timedelta(days=1),
            updated_at=timezone.now() - timedelta(days=1),
        )
        quest = Quest.objects.get(pk=quest.pk)
        Quest.objects.filter(pk=quest.pk).update(end_at=timezone.now() - timedelta(minutes=1))
        closet_item(user, name="Too late")
        progress_service.sync_user_quests(user)

        row.refresh_from_db()
        assert row.progress == 1
        assert row.is_completed is False
        assert not PointsTransaction.objects.filter(
            reference=f"quest:{quest.pk}:user:{user.pk}"
        ).exists()

    def test_an_ended_quest_with_no_row_never_gains_one(self, user):
        quest = make_quest(
            quest_type=Quest.QuestType.CLOSET_ADD,
            target=1,
            end_at=timezone.now() - timedelta(hours=1),
        )
        closet_item(user)

        view = progress_service.progress_for(user, quest)

        assert view.state == "ended"
        assert view.row is None

    def test_data_from_before_the_window_does_not_count(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1)
        item = closet_item(user)
        ClosetItem.objects.filter(pk=item.pk).update(created_at=timezone.now() - timedelta(days=30))
        Quest.objects.filter(pk=quest.pk).update(start_at=timezone.now() - timedelta(days=7))

        progress_service.sync_user_quests(user)

        assert not UserQuest.objects.filter(user=user, quest=quest).exists()

    def test_once_quests_count_all_time_data(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1)
        item = closet_item(user)
        ClosetItem.objects.filter(pk=item.pk).update(created_at=timezone.now() - timedelta(days=90))

        row = complete_quest(user, quest)

        assert row.is_completed


# --------------------------------------------------------------------------------------
# Repeatable quests
# --------------------------------------------------------------------------------------


class TestRepeatableQuests:
    def test_a_daily_quest_completes_again_each_period(self, user):
        quest = make_quest(
            slug="daily-closet",
            quest_type=Quest.QuestType.CLOSET_ADD,
            repeat_period=Quest.RepeatPeriod.DAILY,
            target=1,
            points_reward=25,
        )
        yesterday = timezone.now() - timedelta(days=1)
        item = closet_item(user)
        ClosetItem.objects.filter(pk=item.pk).update(created_at=yesterday)

        progress_service.sync_user_quests(user, now=yesterday)
        first = UserQuest.objects.get(
            user=user, quest=quest, period_key=period_key_for(quest, yesterday)
        )
        assert first.is_completed

        closet_item(user, name="Today's tee")
        progress_service.sync_user_quests(user, now=timezone.now())
        today_key = period_key_for(quest, timezone.now())
        second = UserQuest.objects.get(user=user, quest=quest, period_key=today_key)

        assert second.pk != first.pk
        assert second.is_completed
        assert UserQuest.objects.filter(user=user, quest=quest).count() == 2
        references = set(
            PointsTransaction.objects.filter(
                transaction_type=PointsTransaction.TransactionType.BONUS
            ).values_list("reference", flat=True)
        )
        assert references == {
            f"quest:{quest.pk}:user:{user.pk}:{period_key_for(quest, yesterday)}",
            f"quest:{quest.pk}:user:{user.pk}:{today_key}",
        }

    def test_a_new_period_starts_at_zero_progress(self, user):
        quest = make_quest(
            slug="weekly-wishlist",
            quest_type=Quest.QuestType.WISHLIST_ADD,
            repeat_period=Quest.RepeatPeriod.WEEKLY,
            target=1,
        )
        last_week = timezone.now() - timedelta(days=7)
        row = UserQuest.objects.create(
            user=user,
            quest=quest,
            period_key=period_key_for(quest, last_week),
            status=UserQuest.Status.COMPLETED,
            completed_at=last_week,
            progress=1,
        )

        view = progress_service.progress_for(user, quest)

        assert view.row is None or view.row.pk != row.pk
        assert view.progress == 0
        assert view.period_key == period_key_for(quest, timezone.now())


# --------------------------------------------------------------------------------------
# Rewards: idempotency, the ledger, badges
# --------------------------------------------------------------------------------------


class TestRewards:
    def test_completion_issues_one_bonus_row_with_expiry(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=100)
        closet_item(user)

        complete_quest(user, quest)

        entry = PointsTransaction.objects.get(
            reference=f"quest:{quest.pk}:user:{user.pk}",
            transaction_type=PointsTransaction.TransactionType.BONUS,
        )
        assert entry.user_id == user.pk
        assert entry.amount == 100
        assert entry.expires_at is not None
        days_out = (entry.expires_at - timezone.now()).days
        assert days_out == int(settings.LOYALTY_EXPIRY_DAYS) - 1  # within one day of the policy

    def test_a_zero_point_quest_issues_no_ledger_row(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=0)
        closet_item(user)

        complete_quest(user, quest)

        assert not PointsTransaction.objects.filter(
            reference=f"quest:{quest.pk}:user:{user.pk}"
        ).exists()

    def test_double_sync_issues_one_transaction(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=40)
        closet_item(user)

        progress_service.sync_user_quests(user)
        progress_service.sync_user_quests(user)
        progress_service.sync_user_quests(user)

        assert (
            PointsTransaction.objects.filter(
                reference=f"quest:{quest.pk}:user:{user.pk}",
                transaction_type=PointsTransaction.TransactionType.BONUS,
            ).count()
            == 1
        )

    def test_issue_rewards_is_idempotent_on_its_own(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=40)
        closet_item(user)
        row = complete_quest(user, quest)

        reward_service.issue_rewards(row)
        reward_service.issue_rewards(row)

        assert (
            PointsTransaction.objects.filter(
                reference=f"quest:{quest.pk}:user:{user.pk}",
                transaction_type=PointsTransaction.TransactionType.BONUS,
            ).count()
            == 1
        )

    def test_only_the_racing_winner_pays_out(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=40)
        closet_item(user)
        row = UserQuest.objects.create(user=user, quest=quest, progress=1)
        reference = reward_service.reward_reference(quest, user, "")

        won_first = UserQuest.objects.filter(pk=row.pk, status=UserQuest.Status.ACTIVE).update(
            status=UserQuest.Status.COMPLETED, completed_at=timezone.now()
        )
        reward_service.issue_points(user, quest, "")
        won_second = UserQuest.objects.filter(pk=row.pk, status=UserQuest.Status.ACTIVE).update(
            status=UserQuest.Status.COMPLETED, completed_at=timezone.now()
        )
        reward_service.issue_points(user, quest, "")

        assert won_first == 1
        assert won_second == 0
        assert (
            PointsTransaction.objects.filter(
                reference=reference, transaction_type=PointsTransaction.TransactionType.BONUS
            ).count()
            == 1
        )

    def test_a_quest_badge_is_awarded_exactly_once(self, user):
        badge = Badge.objects.create(slug="closet-master", name="Closet Master")
        quest = make_quest(
            quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=0, badge=badge
        )
        closet_item(user)

        progress_service.sync_user_quests(user)
        progress_service.sync_user_quests(user)
        reward_service.issue_points(user, quest, "")

        assert UserBadge.objects.filter(user=user, badge=badge).count() == 1

    def test_repeatable_references_carry_the_period(self, user):
        quest = make_quest(
            slug="daily-again",
            quest_type=Quest.QuestType.CLOSET_ADD,
            repeat_period=Quest.RepeatPeriod.DAILY,
            target=1,
            points_reward=5,
        )
        now = timezone.now()
        key = period_key_for(quest, now)

        assert reward_service.reward_reference(quest, user, key) == (
            f"quest:{quest.pk}:user:{user.pk}:{key}"
        )

    def test_badges_survive_definition_deletion_attempts(self, user):
        badge = Badge.objects.create(slug="keeper", name="Keeper")
        badge_service.award_badge(user, badge)

        with pytest.raises(IntegrityError), transaction.atomic():
            Badge.objects.filter(pk=badge.pk).delete()


# --------------------------------------------------------------------------------------
# Achievements (standalone registry)
# --------------------------------------------------------------------------------------


class TestAchievements:
    def test_a_paid_order_unlocks_first_order(self, user, product):
        place_and_pay(user, product.variants.first())

        badge = Badge.objects.get(slug="first-order")
        assert badge.name == "First FLASH"
        assert badge.description  # copy arrives lazily with the row
        assert UserBadge.objects.filter(user=user, badge=badge).count() == 1

        # The payment event already ran the registry; re-running it must add nothing.
        assert badge_service.sync_achievements(user) == []
        assert UserBadge.objects.filter(user=user).count() == 1

    def test_a_published_review_unlocks_the_reviewer_badge(self, user, product):
        order = place_and_pay(user, product.variants.first())
        make_review(order, user, product, status=Review.Status.PUBLISHED)

        badge_service.sync_achievements(user)

        awarded = set(UserBadge.objects.filter(user=user).values_list("badge__slug", flat=True))
        assert awarded >= {"published-reviewer", "first-order"}

    def test_a_closed_loop_unlocks_the_pioneer_badge(self, user):
        loop_row(user, LoopItem.Type.RESALE, LoopItem.Status.SOLD, price=Decimal("25.00"))

        awarded = badge_service.sync_achievements(user)

        assert [h.badge.slug for h in awarded] == ["circular-pioneer"]


# --------------------------------------------------------------------------------------
# Events and the order_paid signal
# --------------------------------------------------------------------------------------


class TestEvents:
    def test_the_payment_signal_completes_purchase_quests_without_a_page_visit(self, user, product):
        make_quest(quest_type=Quest.QuestType.ORDER_PAID, target=1, points_reward=100)

        place_and_pay(user, product.variants.first())  # signal runs inside this

        row = UserQuest.objects.get(user=user)
        assert row.is_completed
        assert PointsTransaction.objects.filter(
            reference=f"quest:{row.quest_id}:user:{user.pk}",
            transaction_type=PointsTransaction.TransactionType.BONUS,
        ).exists()

    def test_the_payment_signal_fires_drop_quests_too(self, user, product):
        drop_holding(product)
        make_quest(slug="drop-buy", quest_type=Quest.QuestType.DROP_PURCHASED, target=1)

        place_and_pay(user, product.variants.first())

        assert UserQuest.objects.get(quest__slug="drop-buy").is_completed

    def test_an_unknown_event_is_a_deliberate_noop(self, user):
        assert events.record_event(user, "something_happened") == []

    def test_record_event_returns_the_rows_it_touched(self, user, product):
        quest = make_quest(quest_type=Quest.QuestType.ORDER_PAID, target=1)
        place_and_pay(user, product.variants.first())  # the signal already ran once

        touched = events.record_event(user, "order_paid")

        assert all(isinstance(row, UserQuest) for row in touched)
        assert all(row.user_id == user.pk for row in touched)
        assert quest.pk in {row.quest_id for row in touched}

    def test_record_event_is_scoped_to_the_user_it_was_given(self, user, other_user, product):
        make_quest(quest_type=Quest.QuestType.ORDER_PAID, target=1)
        place_and_pay(user, product.variants.first())

        touched = events.record_event(other_user, "order_paid")

        assert touched == []  # the other user has no rows and earns none from someone else

    def test_a_quest_engine_failure_never_fails_the_payment(
        self, user, product, monkeypatch, caplog
    ):
        """The documented failure policy: progress is a cache, payment is a promise."""
        caplog.set_level(logging.ERROR, logger="apps.quests.signals")

        def explode(*_args, **_kwargs):
            raise RuntimeError("quest engine exploded")

        monkeypatch.setattr(events, "sync_user_quests", explode)

        order = place_and_pay(user, product.variants.first())

        order.refresh_from_db()
        assert order.status == type(order).Status.PAID
        assert "Quest progress event failed" in caplog.text


# --------------------------------------------------------------------------------------
# The sweeper
# --------------------------------------------------------------------------------------


class TestSweep:
    def test_the_sweeper_finishes_rows_that_had_no_signal(self, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=2, points_reward=30)
        closet_item(user)
        progress_service.progress_for(user, quest)  # passive row at 1/2
        closet_item(user, name="The missing second tee")  # no event exists for the closet

        result = sweep_progress()

        row = UserQuest.objects.get(user=user, quest=quest)
        assert row.is_completed
        assert result["users_synced"] == 1
        assert PointsTransaction.objects.filter(
            reference=f"quest:{quest.pk}:user:{user.pk}",
            transaction_type=PointsTransaction.TransactionType.BONUS,
        ).exists()

    def test_the_sweeper_leaves_ended_quests_alone(self, user):
        quest = make_quest(
            quest_type=Quest.QuestType.CLOSET_ADD,
            target=1,
            end_at=timezone.now() - timedelta(hours=1),
        )
        closet_item(user)
        UserQuest.objects.create(user=user, quest=quest, progress=1)

        sweep_progress()

        assert not UserQuest.objects.get(pk=UserQuest.objects.get(user=user).pk).is_completed


# --------------------------------------------------------------------------------------
# HTML surfaces: account pages, navigation, isolation
# --------------------------------------------------------------------------------------


class TestAccountPages:
    def test_the_quest_list_requires_a_login(self, client):
        response = client.get("/account/quests/")

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_the_quest_list_buckets_every_state(self, client, user):
        make_quest(slug="running", name="Running quest")
        make_quest(
            slug="waiting", name="Waiting quest", start_at=timezone.now() + timedelta(days=3)
        )
        make_quest(slug="closed", name="Closed quest", end_at=timezone.now() - timedelta(hours=1))
        make_quest(
            slug="finished",
            name="Finished quest",
            quest_type=Quest.QuestType.CLOSET_ADD,
            target=1,
            points_reward=0,
        )
        closet_item(user)
        client.force_login(user)

        body = client.get("/account/quests/").content.decode()

        assert "Running quest" in body
        assert "Waiting quest" in body
        assert "Closed quest" in body
        assert "Finished quest" in body
        assert "In progress" in body
        assert "Coming up" in body
        assert "Completed" in body

    def test_the_quest_list_shows_only_your_own_progress(self, client, user, other_user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=2, name="My progress")
        closet_item(user)
        closet_item(user, name="Mine too")
        progress_service.sync_user_quests(user)  # user completes it
        assert UserQuest.objects.get(user=user, quest=quest).is_completed

        client.force_login(other_user)
        body = client.get("/account/quests/").content.decode()

        assert not UserQuest.objects.filter(user=other_user).exists()
        assert "Finished" not in body.split("My progress", 1)[-1][:400]
        assert "0 / 2" in body

    def test_the_quest_detail_renders_and_hides_unpublished(self, client, user):
        make_quest(slug="visible", name="Visible quest")
        make_quest(slug="secret", publish_state=Quest.PublishState.DRAFT)
        client.force_login(user)

        ok = client.get("/account/quests/visible/")
        missing = client.get("/account/quests/secret/")

        assert ok.status_code == 200
        assert "Visible quest" in ok.content.decode()
        assert missing.status_code == 404

    def test_starting_from_the_page_messages_and_persists(self, client, user):
        make_quest(slug="joinable", name="Joinable quest", quest_type=Quest.QuestType.CLOSET_ADD)
        client.force_login(user)

        response = client.post("/account/quests/joinable/start/", follow=True)

        assert response.status_code == 200
        assert "Quest started" in message_texts(response)
        assert UserQuest.objects.filter(user=user, quest__slug="joinable").exists()

    def test_start_via_get_is_not_allowed(self, client, user):
        make_quest(slug="post-only")
        client.force_login(user)

        assert client.get("/account/quests/post-only/start/").status_code == 405

    def test_starting_an_ended_quest_shows_a_domain_error(self, client, user):
        make_quest(slug="too-late", name="Too late", end_at=timezone.now() - timedelta(hours=1))
        client.force_login(user)

        response = client.post("/account/quests/too-late/start/", follow=True)

        assert "not open" in message_texts(response)
        assert not UserQuest.objects.filter(user=user).exists()

    def test_the_rewards_dashboard_is_private_and_noindex(self, client, user):
        client.force_login(user)

        response = client.get("/account/rewards/")

        assert response.status_code == 200
        assert "noindex, follow" in response.content.decode()

    def test_the_rewards_dashboard_requires_a_login(self, client):
        response = client.get("/account/rewards/")

        assert response.status_code == 302
        assert reverse("accounts:login") in response.url

    def test_the_rewards_dashboard_lists_badges_and_completions(self, client, user, product):
        make_quest(
            slug="badge-quest",
            name="Badge quest",
            quest_type=Quest.QuestType.CLOSET_ADD,
            target=1,
            points_reward=0,
        )
        closet_item(user)
        progress_service.sync_user_quests(user)
        place_and_pay(user, product.variants.first())
        badge_service.sync_achievements(user)
        client.force_login(user)

        body = client.get("/account/rewards/").content.decode()

        assert "Badge quest" in body
        assert "First FLASH" in body

    def test_navigation_offers_both_new_sections(self, client, user):
        from apps.accounts.navigation import account_sections

        labels = {section["label"] for section in account_sections()}
        live = {section["url"] for section in account_sections() if section["live"]}
        assert {"Quests", "Rewards"} <= labels
        assert {"/account/quests/", "/account/rewards/"} <= live

        client.force_login(user)
        body = client.get("/account/").content.decode()
        assert "/account/quests/" in body
        assert "/account/rewards/" in body


# --------------------------------------------------------------------------------------
# API: read-only progress, empty start, no self-award, no IDOR
# --------------------------------------------------------------------------------------


class TestApi:
    def test_the_list_requires_authentication(self, api_client):
        assert api_client.get("/api/v1/quests/").status_code in (401, 403)

    def test_the_list_returns_server_derived_progress_only(self, api_client, user):
        quest = make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=2, name="API quest")
        closet_item(user)
        api_client.force_authenticate(user=user)

        response = api_client.get("/api/v1/quests/")

        assert response.status_code == 200
        payload = response.data["results"][0]
        assert payload["slug"] == quest.slug
        assert payload["progress"] == 1
        assert payload["percent"] == 50
        assert payload["state"] == "active"
        assert payload["completed"] is False

    def test_progress_never_leaks_between_users(self, api_client, user, other_user):
        make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1)
        closet_item(user)
        progress_service.sync_user_quests(user)
        assert UserQuest.objects.get(user=user).is_completed

        api_client.force_authenticate(user=other_user)
        payload = api_client.get("/api/v1/quests/").data["results"][0]

        assert payload["progress"] == 0
        assert payload["completed"] is False

    def test_detail_404s_for_unknown_and_unpublished(self, api_client, user):
        make_quest(slug="drafty", publish_state=Quest.PublishState.DRAFT)
        api_client.force_authenticate(user=user)

        assert api_client.get("/api/v1/quests/missing/").status_code == 404
        assert api_client.get("/api/v1/quests/drafty/").status_code == 404

    def test_the_start_action_takes_no_input_and_ignores_a_forged_completion(
        self, api_client, user
    ):
        make_quest(
            slug="forged", quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=100
        )
        api_client.force_authenticate(user=user)

        response = api_client.post(
            "/api/v1/quests/forged/start/",
            {"progress": 99, "status": "completed", "reward_reference": "quest:evil"},
            format="json",
        )

        assert response.status_code == 200
        row = UserQuest.objects.get(user=user, quest__slug="forged")
        assert row.status == UserQuest.Status.ACTIVE
        assert row.progress == 0
        assert not PointsTransaction.objects.filter(reference__startswith="quest:").exists()

    def test_starting_an_ineligible_quest_returns_the_domain_code(self, api_client, user):
        make_quest(slug="not-yet", start_at=timezone.now() + timedelta(days=2))
        api_client.force_authenticate(user=user)

        response = api_client.post("/api/v1/quests/not-yet/start/")

        assert response.status_code == 400
        assert response.data["code"] == "quest_state_scheduled"

    def test_the_rewards_endpoint_reports_balance_and_tallies(self, api_client, user, product):
        make_quest(quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=20)
        closet_item(user)
        api_client.force_authenticate(user=user)
        progress_service.sync_user_quests(user)

        payload = api_client.get("/api/v1/rewards/").data

        assert payload["points_balance"] == 20
        assert payload["completed_quests"] == 1
        assert payload["badges"] == []

    def test_no_endpoint_ever_creates_a_quest_points_row_by_itself(self, api_client, user):
        make_quest(
            slug="watched", quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=100
        )
        api_client.force_authenticate(user=user)

        api_client.post("/api/v1/quests/watched/start/")
        api_client.get("/api/v1/quests/")
        api_client.get("/api/v1/quests/watched/")
        api_client.get("/api/v1/rewards/")

        # No closet item, no data, no completion -- so no money moves, on any surface.
        assert not PointsTransaction.objects.filter(
            reference__startswith="quest:", transaction_type=PointsTransaction.TransactionType.BONUS
        ).exists()

    def test_there_is_no_completion_or_claim_route_anywhere(self, client, api_client, user):
        """Guessing the URL of a completion must 404, not 403 or 200."""
        quest = make_quest(
            slug="target", quest_type=Quest.QuestType.CLOSET_ADD, target=1, points_reward=100
        )
        closet_item(user)  # the data already satisfies the target
        client.force_login(user)
        api_client.force_authenticate(user=user)

        for path in (
            "/account/quests/target/complete/",
            "/account/quests/target/claim/",
            "/account/quests/target/progress/",
            "/account/quests/target/reward/",
        ):
            assert client.post(path, {"progress": 1, "status": "completed"}).status_code == 404, (
                path
            )

        for path in (
            "/api/v1/quests/target/complete/",
            "/api/v1/quests/target/claim/",
            "/api/v1/quests/target/progress/",
        ):
            response = api_client.post(path, {"progress": 1}, format="json")
            assert response.status_code == 404, path

        # Nothing wrote a row, so nothing could have completed or paid out.
        assert not UserQuest.objects.filter(user=user, quest=quest).exists()
        assert not PointsTransaction.objects.filter(
            reference__startswith="quest:", transaction_type=PointsTransaction.TransactionType.BONUS
        ).exists()


# --------------------------------------------------------------------------------------
# Admin: staff author, nobody hand-awards
# --------------------------------------------------------------------------------------


class TestAdmin:
    def test_all_four_models_are_registered(self):
        for model in (Quest, Badge, UserQuest, UserBadge):
            assert model in site._registry

    def test_participation_and_badges_are_view_only(self, admin_user, rf):
        request = rf.get("/")
        request.user = admin_user

        for model in (UserQuest, UserBadge):
            admin = site._registry[model]
            assert admin.has_add_permission(request) is False
            assert admin.has_change_permission(request) is False
            assert admin.has_view_permission(request) is True

    def test_quest_and_badge_remain_editable(self, admin_user, rf):
        request = rf.get("/")
        request.user = admin_user

        for model in (Quest, Badge):
            admin = site._registry[model]
            assert admin.has_add_permission(request) is True
            assert admin.has_change_permission(request) is True

    def test_the_admin_cannot_be_used_to_write_progress(self):
        admin = site._registry[UserQuest]
        assert "progress" in admin.readonly_fields
        assert "status" in admin.readonly_fields


# --------------------------------------------------------------------------------------
# Query discipline: the quest list stays flat as the catalogue of quests grows
# --------------------------------------------------------------------------------------


class TestQueryCounts(TestCase):
    def test_the_quest_list_stays_within_a_fixed_query_budget(self):
        from django.contrib.auth import get_user_model

        actor = get_user_model().objects.create_user(
            email="budget@flashwear.test", password="Str0ng-Passw0rd!"
        )
        make_quest(slug="b1", quest_type=Quest.QuestType.ORDER_PAID, target=3)
        make_quest(slug="b2", quest_type=Quest.QuestType.CLOSET_ADD, target=3)
        make_quest(slug="b3", quest_type=Quest.QuestType.OUTFIT_SAVED, target=3)
        make_quest(slug="b4", quest_type=Quest.QuestType.DNA_COMPLETED)
        make_quest(slug="b5", quest_type=Quest.QuestType.WISHLIST_ADD, target=3)
        closet_item(actor)

        self.client.force_login(actor)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/account/quests/")

        self.assertEqual(response.status_code, 200)
        # Measured 33 for this fixture: session + user + 5 quests x (row + handler) +
        # savepoints, then balance, badge count and the base template's own lookups.
        # The ceiling is what guards against a per-quest N+1 creeping in.
        self.assertLessEqual(len(ctx.captured_queries), 40, "quest list query count exploded")
