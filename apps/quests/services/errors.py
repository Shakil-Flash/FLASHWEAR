"""Domain exceptions for FLASH Quests rules (Phase 14).

Same contract as the closet, engagement and loop apps: services raise these with a
user-facing message and a stable ``code``; views render ``message`` through the messages
framework, the API maps ``code`` into a structured payload. Nothing here is a
``ValidationError`` -- one rule failure has to surface on both sides without duplicating
the rule itself.
"""

from __future__ import annotations


class QuestError(Exception):
    """A quest rule was violated (eligibility, unknown type, malformed configuration)."""

    code = "quest_invalid"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code

    @property
    def message(self) -> str:
        return str(self)


class QuestNotEligibleError(QuestError):
    """The quest is not in a state where this user may participate right now."""

    code = "quest_not_eligible"


class QuestConfigurationError(QuestError):
    """The quest row cannot be measured (unknown type, missing handler). A staff fix."""

    code = "quest_misconfigured"
