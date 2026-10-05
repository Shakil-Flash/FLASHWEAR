"""Preferences form (Phase 17 §15).

Fields are built per category at runtime (the registry owns the vocabulary), and the
mandatory ones are ``disabled``: Django keeps a disabled field's *initial* value no
matter what the POST contains, so a tampered submission cannot even reach
``set_preferences`` with a forced-off order toggle -- belt and braces on top of the
server-side coercion there.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import NotificationCategory
from apps.notifications.services.preferences import preference_rows
from apps.notifications.services.templates import MANDATORY_CATEGORIES

__all__ = ["PreferencesForm"]


class PreferencesForm(forms.Form):
    """``email__<category>`` / ``in_app__<category>`` checkboxes for one recipient."""

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        for row in preference_rows(user):
            category = row["category"]
            mandatory = category in MANDATORY_CATEGORIES
            label = row["label"]
            self.fields[f"email__{category}"] = forms.BooleanField(
                label=_("Email: %(label)s") % {"label": label},
                required=False,
                initial=row["email"],
                disabled=mandatory,
            )
            self.fields[f"in_app__{category}"] = forms.BooleanField(
                label=_("In-app: %(label)s") % {"label": label},
                required=False,
                initial=row["in_app"],
                disabled=mandatory,
            )

    def rows(self) -> list[dict]:
        """Render-ready rows: ``{label, mandatory, email, in_app}`` per category.

        Templates cannot subscript the form by a computed name (``form.email__orders``
        only works as literal attribute syntax), so the pairing happens here and the
        template just renders the two BoundFields it is handed.
        """
        rows = []
        for category in NotificationCategory.values:
            email_name = f"email__{category}"
            in_app_name = f"in_app__{category}"
            rows.append(
                {
                    "category": category,
                    "label": dict(NotificationCategory.choices)[category],
                    "mandatory": category in MANDATORY_CATEGORIES,
                    "email": self[email_name],
                    "in_app": self[in_app_name],
                }
            )
        return rows

    def as_payload(self) -> dict:
        """``{category: {"email": bool, "in_app": bool}}`` for ``set_preferences``.

        Disabled (mandatory) fields are absent from ``cleaned_data`` by Django's rules --
        we take their *initial* truth from the registry instead, and ``set_preferences``
        coerces them again server-side.
        """
        payload: dict[str, dict[str, bool]] = {}
        for category in NotificationCategory.values:
            payload[category] = {
                "email": self.cleaned_data.get(
                    f"email__{category}",
                    category in MANDATORY_CATEGORIES,
                ),
                "in_app": self.cleaned_data.get(
                    f"in_app__{category}",
                    category in MANDATORY_CATEGORIES,
                ),
            }
        return payload
