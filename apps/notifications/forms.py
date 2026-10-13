"""Preferences form (Phase 17 §15, Phase 51 §4).

Fields are built per category at runtime (the registry owns the vocabulary), and the
mandatory ones are ``disabled``: Django keeps a disabled field's *initial* value no
matter what the POST contains, so a tampered submission cannot even reach
``set_preferences`` with a forced-off order toggle -- belt and braces on top of the
server-side coercion there.

Unconfigured channels (SMS/Push) are flagged and disabled appropriately so customers
are never misled into believing unconfigured channels will deliver.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import NotificationCategory
from apps.notifications.services.preferences import preference_rows
from apps.notifications.services.templates import MANDATORY_CATEGORIES

__all__ = ["PreferencesForm"]


class PreferencesForm(forms.Form):
    """``email__<category>`` / ``in_app__<category>`` / ``sms__<category>`` / ``push__<category>`` checkboxes."""

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self._raw_rows = preference_rows(user)
        self._row_map = {r["category"]: r for r in self._raw_rows}

        for row in self._raw_rows:
            category = row["category"]
            mandatory = category in MANDATORY_CATEGORIES
            label = row["label"]
            sms_avail = row.get("sms_available", False)
            push_avail = row.get("push_available", False)

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
            self.fields[f"sms__{category}"] = forms.BooleanField(
                label=_("SMS: %(label)s") % {"label": label},
                required=False,
                initial=row["sms"] if sms_avail else False,
                disabled=not sms_avail,
            )
            self.fields[f"push__{category}"] = forms.BooleanField(
                label=_("Push: %(label)s") % {"label": label},
                required=False,
                initial=row["push"] if push_avail else False,
                disabled=not push_avail,
            )

    def rows(self) -> list[dict]:
        """Render-ready rows: ``{label, category, mandatory, email, in_app, sms, push, sms_available, push_available}``."""
        rows = []
        for category in NotificationCategory.values:
            email_name = f"email__{category}"
            in_app_name = f"in_app__{category}"
            sms_name = f"sms__{category}"
            push_name = f"push__{category}"
            r_info = self._row_map.get(category, {})

            rows.append(
                {
                    "category": category,
                    "label": dict(NotificationCategory.choices)[category],
                    "mandatory": category in MANDATORY_CATEGORIES,
                    "email": self[email_name],
                    "in_app": self[in_app_name],
                    "sms": self[sms_name],
                    "push": self[push_name],
                    "sms_available": r_info.get("sms_available", False),
                    "push_available": r_info.get("push_available", False),
                }
            )
        return rows

    def as_payload(self) -> dict:
        """``{category: {"email": bool, "in_app": bool, "sms": bool, "push": bool}}`` for ``set_preferences``."""
        payload: dict[str, dict[str, bool]] = {}
        for category in NotificationCategory.values:
            r_info = self._row_map.get(category, {})
            is_mand = category in MANDATORY_CATEGORIES
            payload[category] = {
                "email": self.cleaned_data.get(
                    f"email__{category}",
                    is_mand or r_info.get("email", True),
                ),
                "in_app": self.cleaned_data.get(
                    f"in_app__{category}",
                    is_mand or r_info.get("in_app", True),
                ),
                "sms": self.cleaned_data.get(
                    f"sms__{category}",
                    r_info.get("sms", False),
                ),
                "push": self.cleaned_data.get(
                    f"push__{category}",
                    r_info.get("push", False),
                ),
            }
        return payload
