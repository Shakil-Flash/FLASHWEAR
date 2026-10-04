"""Domain exceptions for FLASH Support rules (Phase 15).

Same contract as the closet, engagement, loop and quest apps: services raise these with a
user-facing message and a stable ``code``; views render ``message`` through the messages
framework, the API maps ``code`` into a structured payload. Nothing here is a
``ValidationError`` -- one rule failure has to surface on both sides without duplicating
the rule itself.
"""

from __future__ import annotations


class SupportError(Exception):
    """A support rule was violated (transition, permission, duplicate, attachment)."""

    code = "support_invalid"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code

    @property
    def message(self) -> str:
        return str(self)


class TransitionError(SupportError):
    """The ticket is not in a state that allows the requested transition."""

    code = "support_bad_transition"


class ForbiddenError(SupportError):
    """The actor may not perform this action on this ticket.

    Named ``Forbidden`` rather than ``PermissionError`` so importing it never shadows the
    builtin in a caller's namespace.
    """

    code = "support_forbidden"


class DuplicateTicketError(SupportError):
    """The same unresolved problem already has a ticket; the message names it."""

    code = "support_duplicate"


class AttachmentError(SupportError):
    """An upload failed the content, size or type rules."""

    code = "support_attachment_rejected"


class ReferenceError(SupportError):
    """A reference could not be resolved for this actor (missing row, not theirs)."""

    code = "support_bad_reference"
