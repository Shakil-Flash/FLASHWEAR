"""FLASH Quests & Rewards data model (Phase 14).

Four rows, three ideas:

* :class:`Quest` -- staff-authored, deterministic. ``quest_type`` is a closed allowlist of
  things the platform can *measure* from authoritative data; ``objective_type`` is
  ``COUNT`` (progress toward ``target``) or ``BOOLEAN`` (all-or-nothing). A quest never
  stores a rule the client can influence -- rules live in
  :mod:`apps.quests.services.handlers`, and progress is recomputed from the database.
* :class:`UserQuest` -- one user's progress on one quest for one period. Rows are created
  lazily (a user with zero measurable progress on a quest has no row), keyed by
  ``(user, quest, period_key)`` where ``period_key`` is ``""`` for once-only quests and a
  UTC bucket (``20261004`` / ``2026-W40`` / ``202610``) for repeatable ones. ``COMPLETED``
  is terminal: later refunds, deletions or moderator reversals never claw a reward back.
* :class:`Badge` / :class:`UserBadge` -- achievements awarded either as a quest reward
  (:attr:`Quest.badge`) or by the standalone rule registry in
  :mod:`apps.quests.services.badges`. One row per ``(user, badge)``; a badge is PROTECTed
  once anyone holds it, so an achievement can never be deleted out from under its owners.

Quest *state* is deliberately split: ``publish_state`` (draft / published / archived) is
stored, while scheduled / active / ended is **derived** from ``start_at`` / ``end_at`` at
read time (:meth:`Quest.state_at`). A delayed sweeper re-deriving progress an hour late
therefore cannot resurrect a quest whose window closed.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.catalog.models.base import TimestampedModel

__all__ = ["Badge", "Quest", "UserBadge", "UserQuest"]


class Quest(TimestampedModel):
    """One staff-authored challenge with a deterministic completion rule."""

    class QuestType(models.TextChoices):
        """The closed allowlist of measurable behaviours.

        Each value maps 1:1 to a handler in :mod:`apps.quests.services.handlers`. Adding a
        type is a code change on purpose: a quest whose rule cannot be computed server-side
        from authoritative data does not belong here.
        """

        ORDER_PAID = "order_paid", _("Paid order")
        REVIEW_WRITTEN = "review_written", _("Published review")
        DNA_COMPLETED = "dna_completed", _("FLASH DNA complete")
        CLOSET_ADD = "closet_add", _("Closet item added")
        OUTFIT_SAVED = "outfit_saved", _("Outfit saved")
        WISHLIST_ADD = "wishlist_add", _("Wishlist product saved")
        DROP_PURCHASED = "drop_purchased", _("Purchased from a FLASH Drop")
        CREATOR_POST = "creator_post", _("Published creator post")
        LOOP_RESALE = "loop_resale", _("Resale listed")
        LOOP_TRADE_IN = "loop_trade_in", _("Trade-in accepted")
        LOOP_RECYCLE = "loop_recycle", _("Recycling accepted")
        PROFILE_COMPLETED = "profile_completed", _("Profile completed")

    class ObjectiveType(models.TextChoices):
        """How progress is measured. MILESTONE (arbitrary checkpoints) is deliberately absent:
        a COUNT quest already expresses "every 3 items" by sweeping the same authoritative
        count, so a second vocabulary would only duplicate it."""

        COUNT = "count", _("Count")
        BOOLEAN = "boolean", _("All-or-nothing")

    class RepeatPeriod(models.TextChoices):
        """Once-only (``NONE``, counted all-time) or a UTC calendar bucket that resets
        progress. Boolean quests are forced to ``NONE`` -- a "complete your DNA" quest that
        re-awarded every day would be a free reward, not a goal."""

        NONE = "none", _("Once")
        DAILY = "daily", _("Daily")
        WEEKLY = "weekly", _("Weekly")
        MONTHLY = "monthly", _("Monthly")

    class PublishState(models.TextChoices):
        """Stored lifecycle. Scheduled / active / ended are derived, never stored."""

        DRAFT = "draft", _("Draft")
        PUBLISHED = "published", _("Published")
        ARCHIVED = "archived", _("Archived")

    class State(models.TextChoices):
        """Full derived view used by every consumer (UI, API, progress engine)."""

        DRAFT = "draft", _("Draft")
        ARCHIVED = "archived", _("Archived")
        SCHEDULED = "scheduled", _("Scheduled")
        ACTIVE = "active", _("Active")
        ENDED = "ended", _("Ended")

    slug = models.SlugField(_("slug"), max_length=100, unique=True)
    name = models.CharField(_("name"), max_length=120)
    description = models.TextField(
        _("description"),
        blank=True,
        help_text=_("Plain-language copy: what to do and how progress is counted."),
    )

    quest_type = models.CharField(
        _("quest type"),
        max_length=24,
        choices=QuestType.choices,
        help_text=_(
            "Which server-side handler measures this quest. Immutable in practice: "
            "changing it reinterprets every existing row."
        ),
    )
    objective_type = models.CharField(
        _("objective type"),
        max_length=12,
        choices=ObjectiveType.choices,
        default=ObjectiveType.COUNT,
    )
    target = models.PositiveIntegerField(
        _("target"),
        default=1,
        help_text=_("Units required for COUNT quests. Forced to 1 for BOOLEAN quests."),
    )

    repeat_period = models.CharField(
        _("repeat period"),
        max_length=12,
        choices=RepeatPeriod.choices,
        default=RepeatPeriod.NONE,
        help_text=_("NONE counts all-time; the others reset progress on a UTC bucket."),
    )

    publish_state = models.CharField(
        _("publish state"),
        max_length=12,
        choices=PublishState.choices,
        default=PublishState.DRAFT,
        db_index=True,
    )
    start_at = models.DateTimeField(
        _("start at"),
        null=True,
        blank=True,
        help_text=_("NULL means the quest can start as soon as it is published."),
    )
    end_at = models.DateTimeField(
        _("end at"),
        null=True,
        blank=True,
        help_text=_("NULL means the quest stays open indefinitely."),
    )

    points_reward = models.PositiveIntegerField(
        _("points reward"),
        default=0,
        help_text=_("FLASH Points awarded once on completion, through the Phase 7 ledger."),
    )
    badge = models.ForeignKey(
        "Badge",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reward_quests",
        verbose_name=_("badge"),
        help_text=_("Optional badge awarded once on completion."),
    )

    sort_order = models.PositiveIntegerField(
        _("sort order"),
        default=0,
        help_text=_("Lower numbers appear first in the quest list."),
    )

    class Meta:
        verbose_name = _("quest")
        verbose_name_plural = _("quests")
        ordering = ("sort_order", "name")
        indexes = [
            models.Index(fields=["publish_state", "sort_order"], name="quest_publish_sort_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(start_at__isnull=True)
                | models.Q(end_at__isnull=True)
                | models.Q(end_at__gt=models.F("start_at")),
                name="quest_window_ordered",
            ),
            models.CheckConstraint(
                condition=models.Q(objective_type="boolean") | models.Q(target__gte=1),
                name="quest_target_positive",
            ),
            models.CheckConstraint(
                condition=~models.Q(objective_type="boolean")
                | models.Q(repeat_period="none", target=1),
                name="quest_boolean_is_once_target_one",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        """Boolean quests are once-only with an implicit target of 1 (see the constraint)."""
        super().clean()
        if self.objective_type == self.ObjectiveType.BOOLEAN:
            self.repeat_period = self.RepeatPeriod.NONE
            self.target = 1
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError(
                {"end_at": _("The quest must end after it starts.")},
                code="window_order",
            )

    def save(self, *args, **kwargs):
        """Normalise boolean quests on every path (create, update, loaddata)."""
        if self.objective_type == self.ObjectiveType.BOOLEAN:
            self.repeat_period = self.RepeatPeriod.NONE
            self.target = 1
        super().save(*args, **kwargs)

    @property
    def effective_target(self) -> int:
        """Boolean quests measure 0 or 1 regardless of the stored ``target``."""
        if self.objective_type == self.ObjectiveType.BOOLEAN:
            return 1
        return max(1, int(self.target))

    def state_at(self, now=None) -> str:
        """Derived lifecycle at ``now``.

        ``publish_state`` gates first; within a published quest the window decides between
        scheduled, active and ended. Because this is recomputed every read, a sweep that
        runs after ``end_at`` sees ``ENDED`` and refuses to move progress (see
        :mod:`apps.quests.services.progress`).
        """
        now = now or timezone.now()
        if self.publish_state == self.PublishState.DRAFT:
            return self.State.DRAFT
        if self.publish_state == self.PublishState.ARCHIVED:
            return self.State.ARCHIVED
        if self.start_at and now < self.start_at:
            return self.State.SCHEDULED
        if self.end_at and now > self.end_at:
            return self.State.ENDED
        return self.State.ACTIVE

    @property
    def is_once(self) -> bool:
        return self.repeat_period == self.RepeatPeriod.NONE


class Badge(TimestampedModel):
    """An achievement token. Rows are created lazily by the reward / achievement paths."""

    slug = models.SlugField(_("slug"), max_length=100, unique=True)
    name = models.CharField(_("name"), max_length=120)
    description = models.TextField(_("description"), blank=True)

    class Meta:
        verbose_name = _("badge")
        verbose_name_plural = _("badges")
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class UserQuest(TimestampedModel):
    """One user's progress on one quest for one period.

    A row exists only once the user has measurable progress or has explicitly started the
    quest; the absence of a row means "nothing yet", never "failed". ``COMPLETED`` is
    terminal -- :attr:`ALLOWED_TRANSITIONS` has no exit, and the progress engine skips
    completed rows entirely.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", _("In progress")
        COMPLETED = "completed", _("Completed")

    ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
        Status.ACTIVE: (Status.COMPLETED,),
        Status.COMPLETED: (),
    }

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="quest_progress",
        verbose_name=_("user"),
        help_text=_("CASCADE: quest progress is private account data and leaves with the account."),
    )
    quest = models.ForeignKey(
        Quest,
        on_delete=models.CASCADE,
        related_name="progress",
        verbose_name=_("quest"),
    )
    period_key = models.CharField(
        _("period key"),
        max_length=16,
        blank=True,
        default="",
        help_text=_("'' for once-only quests; a UTC bucket for repeatable ones."),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    progress = models.PositiveIntegerField(
        _("progress"),
        default=0,
        help_text=_("Server-computed, clamped to the quest target. Never client-writable."),
    )
    started_at = models.DateTimeField(
        _("started at"),
        null=True,
        blank=True,
        help_text=_("Set when the user explicitly starts the quest; NULL for passive rows."),
    )
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)
    reward_reference = models.CharField(
        _("reward reference"),
        max_length=64,
        blank=True,
        default="",
        help_text=_("The FLASH Points ledger reference issued on completion (audit)."),
    )

    class Meta:
        verbose_name = _("user quest")
        verbose_name_plural = _("user quests")
        ordering = ("-updated_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["user", "quest", "period_key"],
                name="userquest_user_quest_period_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(status="active", completed_at__isnull=True)
                | models.Q(status="completed", completed_at__isnull=False),
                name="userquest_completion_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=["quest", "status"], name="userquest_quest_status_idx"),
            models.Index(fields=["user", "status"], name="userquest_user_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.quest.name} ({self.progress})"

    def can_transition_to(self, new_status: str) -> bool:
        """Whether ``new_status`` is reachable from the current status. Terminal means terminal."""
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, ())

    @property
    def is_completed(self) -> bool:
        return self.status == self.Status.COMPLETED


class UserBadge(TimestampedModel):
    """One user's hold on one badge. Unique on ``(user, badge)`` -- achievements are once."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="quest_badges",
        verbose_name=_("user"),
        help_text=_("CASCADE: badges are private account data and leave with the account."),
    )
    badge = models.ForeignKey(
        Badge,
        on_delete=models.PROTECT,
        related_name="holders",
        verbose_name=_("badge"),
        help_text=_("PROTECT: a badge with holders can never be deleted out from under them."),
    )
    source = models.CharField(
        _("source"),
        max_length=64,
        blank=True,
        default="",
        help_text=_("Quest slug or achievement key that awarded it (audit)."),
    )

    class Meta:
        verbose_name = _("user badge")
        verbose_name_plural = _("user badges")
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["user", "badge"], name="userbadge_user_badge_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} — {self.badge.name}"
