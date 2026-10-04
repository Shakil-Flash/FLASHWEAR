"""Quest services.

Modules, one concern each; callers import the submodule they need
(``from apps.quests.services import progress``). ``errors`` holds the domain exceptions both
the storefront views and the API translate into their own error vocabulary.

Import order matters only in one direction: ``handlers`` measures, ``progress`` decides,
``rewards`` / ``badges`` pay, ``events`` invalidates. Nothing imports back upward.
"""

from apps.quests.services import (
    badges,
    errors,
    events,
    handlers,
    periods,
    progress,
    rewards,
)
from apps.quests.services.errors import (
    QuestConfigurationError,
    QuestError,
    QuestNotEligibleError,
)

__all__ = [
    "QuestConfigurationError",
    "QuestError",
    "QuestNotEligibleError",
    "badges",
    "errors",
    "events",
    "handlers",
    "periods",
    "progress",
    "rewards",
]
