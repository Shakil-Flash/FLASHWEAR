"""Domain exceptions for FLASH Loop rules (Phase 13).

Same contract as the closet and engagement apps: services raise these
with a user-facing message and a stable ``code``; forms render
``message`` directly, the API maps ``code`` into a structured payload.
Nothing here is a ``ValidationError`` -- one rule failure has to
surface on both sides without duplicating the rule itself.
"""

from __future__ import annotations


class LoopError(Exception):
    """A FLASH Loop rule was violated (ownership, eligibility, transition)."""

    code = "loop_invalid"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code

    @property
    def message(self) -> str:
        return str(self)


class OwnershipError(LoopError):
    """The user cannot prove ownership of the item they submitted."""

    code = "loop_not_owner"


class EligibilityError(LoopError):
    """The item is not eligible for the requested loop path."""

    code = "loop_not_eligible"


class TransitionError(LoopError):
    """The item is not in a state that allows the requested transition."""

    code = "loop_bad_transition"
