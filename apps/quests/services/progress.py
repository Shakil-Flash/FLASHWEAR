"""The quest progress engine (Phase 14).

Pull-based by design. Nothing here trusts a client-supplied number: progress is *derived*
from the database every time it is read, via the quest's registered handler, and written
back as a cache of that derivation. Events (``order_paid``) and the Celery sweep are only
**invalidation triggers** that re-run the same derivation sooner -- they can never move a
number the handlers would not also produce.

Invariants, all enforced in this module:

* **Rows are lazy.** A user with zero measurable progress on a quest has no
  :class:`~apps.quests.models.UserQuest` row; the absence of a row means "nothing yet".
  Rows appear when progress first exceeds zero or when the user explicitly starts the
  quest (``start_quest``).
* **Completed is terminal.** Completion writes ``status``/``completed_at`` through a
  conditional ``UPDATE ... WHERE status='active'`` (optimistic lock): of two racing syncs
  exactly one wins, and only the winner calls :func:`apps.quests.services.rewards.issue_rewards`.
  Completed rows are never recomputed, so later refunds, deletions or moderator actions
  cannot claw a reward back -- progress is a record of what happened, not a live gauge.
* **Windows are authoritative.** A quest whose ``end_at`` has passed reads as ``ENDED`` at
  every level; the sync refuses to touch its rows, so a sweep that fires an hour late
  cannot extend an expired quest or complete it after the fact.
* **BOOLEAN quests complete on a 1.** The handler answers 0/1; the target is clamped to 1
  by the model, so "complete your FLASH DNA" is one row, not a percentage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.analytics.services import record_event
from apps.quests.models import Quest, UserQuest
from apps.quests.services import rewards as reward_service
from apps.quests.services.errors import QuestNotEligibleError
from apps.quests.services.handlers import compute_raw
from apps.quests.services.periods import effective_since, period_key_for

__all__ = ["QuestProgress", "progress_for", "start_quest", "sync_user_quests"]


@dataclass(frozen=True)
class QuestProgress:
    """A read model: everything a template or serializer needs about one quest, for one user.

    Purely a projection -- ``row`` is ``None`` when the user has no participation yet, and
    ``progress`` is 0 in that case. Callers never mutate this object.
    """

    quest: Quest
    state: str
    progress: int
    target: int
    percent: int
    period_key: str
    row: UserQuest | None = field(default=None, compare=False)

    @property
    def status(self) -> str:
        return self.row.status if self.row is not None else ""

    @property
    def is_completed(self) -> bool:
        return self.row is not None and self.row.is_completed

    @property
    def is_started(self) -> bool:
        """A row exists -- explicitly started, or discovered passively by the sync."""
        return self.row is not None

    @property
    def completed_at(self) -> datetime | None:
        return self.row.completed_at if self.row is not None else None

    @property
    def started_at(self) -> datetime | None:
        return self.row.started_at if self.row is not None else None

    @property
    def reward_reference(self) -> str:
        return self.row.reward_reference if self.row is not None else ""


def _row_for(user, quest: Quest, now, state: str, period_key: str) -> UserQuest | None:
    """The participation row that represents ``now`` (or the frozen last row if ended)."""
    qs = UserQuest.objects.filter(user=user, quest=quest)
    if state != Quest.State.ENDED:
        qs = qs.filter(period_key=period_key)
    return qs.order_by("-updated_at").first()


def _sync_quest(user, quest: Quest, now, row: UserQuest | None = None) -> UserQuest | None:
    """Re-derive one quest for one user. Returns the current row (or ``None`` if none).

    Only ``ACTIVE`` quests are written. Everything else is read-only: scheduled quests have
    no history yet, ended quests are frozen, drafts/archived never reach players. Callers
    that already fetched ``row`` pass it in to save a query; the fetch is repeated inside
    the transaction either way so we always race on fresh status.
    """
    state = quest.state_at(now)
    if state != Quest.State.ACTIVE:
        return row

    period_key = period_key_for(quest, now)
    target = quest.effective_target

    with transaction.atomic():
        if row is None:
            row = (
                UserQuest.objects.filter(user=user, quest=quest, period_key=period_key)
                .order_by("-updated_at")
                .first()
            )
        if row is not None and row.is_completed:
            return row

        since = effective_since(quest, now)
        raw = compute_raw(user, quest, since)
        progress = min(raw, target)

        if row is None:
            if raw <= 0:
                return None  # lazy: nothing to participate with yet
            try:
                # Its own savepoint: an IntegrityError on PostgreSQL aborts the whole
                # transaction, so the retry below only works if the failed INSERT was
                # rolled back first.
                with transaction.atomic():
                    row = UserQuest.objects.create(
                        user=user,
                        quest=quest,
                        period_key=period_key,
                        progress=progress,
                    )
            except IntegrityError:
                # Two syncs created the same (user, quest, period); the unique constraint
                # picked a winner and we continue against that row.
                row = UserQuest.objects.get(user=user, quest=quest, period_key=period_key)

        if row.is_completed:
            return row

        if raw >= target:
            reference = reward_service.reward_reference(quest, user, period_key)
            won = UserQuest.objects.filter(pk=row.pk, status=UserQuest.Status.ACTIVE).update(
                status=UserQuest.Status.COMPLETED,
                progress=target,
                completed_at=now,
                reward_reference=reference,
            )
            row.refresh_from_db()
            if won:
                # Only the racer that flipped the status pays out; issue_rewards is itself
                # idempotent, so this is belt *and* braces.
                reward_service.issue_rewards(row)
                # The completion is one row per attempt; a replayed sync is a no-op read.
                record_event(
                    "quest_completion",
                    user=user,
                    object_type="quest",
                    object_id=quest.pk,
                    metadata={"quest_type": quest.quest_type},
                    idempotency_key=f"quest_completion:{row.pk}",
                )
            return row

        if row.progress != progress:
            UserQuest.objects.filter(pk=row.pk).exclude(status=UserQuest.Status.COMPLETED).update(
                progress=progress
            )
            row.refresh_from_db()
        return row


def sync_user_quests(
    user,
    quest_types=None,
    *,
    now=None,
) -> list[UserQuest]:
    """Re-derive every published quest (optionally one type family) for ``user``.

    The workhorse behind page loads, the ``order_paid`` event and the Celery sweep: same
    code, same numbers, wherever it is called from.
    """
    now = now or timezone.now()
    quests = Quest.objects.filter(publish_state=Quest.PublishState.PUBLISHED)
    if quest_types is not None:
        quests = quests.filter(quest_type__in=list(quest_types))

    touched: list[UserQuest] = []
    for quest in quests.order_by("sort_order", "name"):
        row = _sync_quest(user, quest, now)
        if row is not None:
            touched.append(row)
    return touched


def progress_for(user, quest: Quest, *, now=None, persist: bool = True) -> QuestProgress:
    """The read model for one quest and one user.

    ``persist=True`` (the default) writes the derivation back -- this is how a customer
    who acted elsewhere gets credit the moment they open the quest list. ``persist=False``
    derives exactly the same number through the same handler but never writes it, for
    anything that must not mutate on GET.
    """
    now = now or timezone.now()
    state = quest.state_at(now)
    period_key = period_key_for(quest, now)
    target = quest.effective_target

    if persist and state == Quest.State.ACTIVE:
        # No separate row lookup first: _sync_quest selects inside its own transaction, so
        # the list page costs one row read per quest instead of two.
        row = _sync_quest(user, quest, now)
        progress = row.progress if row is not None else 0
    else:
        row = _row_for(user, quest, now, state, period_key)
        if row is not None and row.is_completed:
            progress = row.progress
        elif state == Quest.State.ACTIVE:
            # The pure read: the number the sync *would* write, derived with the same
            # handler and deliberately not written back, so a GET never mutates state.
            progress = min(compute_raw(user, quest, effective_since(quest, now)), target)
        else:
            progress = row.progress if row is not None else 0

    percent = min(100, (progress * 100) // target) if target else 0
    return QuestProgress(
        quest=quest,
        state=state,
        progress=progress,
        target=target,
        percent=percent,
        period_key=period_key,
        row=row,
    )


def start_quest(user, quest: Quest, *, now=None) -> UserQuest:
    """Explicitly join a quest, creating a zero-progress row the sync would not.

    Eligibility is the quest's *derived* state at ``now`` -- a scheduled quest cannot be
    started early, an ended one cannot be started late, and the error is a domain
    exception the view/ API translate. Starting twice is a no-op that returns the existing
    row (which may already be completed, if the user's data beat them to it).
    """
    now = now or timezone.now()
    state = quest.state_at(now)
    if state != Quest.State.ACTIVE:
        raise QuestNotEligibleError(
            f"{quest.name} is not open for participation right now.",
            code=f"quest_state_{state}",
        )

    period_key = period_key_for(quest, now)
    # get_or_create, not create/except-IntegrityError: the fallback query after a failed
    # INSERT needs a savepoint to roll back to, and only get_or_create guarantees one
    # (under an outer atomic block -- a test, a payment -- a bare retry poisons the
    # transaction and every later query raises TransactionManagementError).
    row, _created = UserQuest.objects.get_or_create(user=user, quest=quest, period_key=period_key)
    if row.started_at is None and not row.is_completed:
        UserQuest.objects.filter(pk=row.pk, started_at__isnull=True).update(started_at=now)
        row.refresh_from_db()

    # Fold in any progress the user already earned before joining.
    synced = _sync_quest(user, quest, now, row=row)
    return synced or row
