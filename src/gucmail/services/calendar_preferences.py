"""Shared ordinary invitation, presentation, related-mail and delivery choices."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gucmail.errors import ConfigurationError
from gucmail.models import CalendarEvent, CalendarResponse, NotificationEvent
from gucmail.services.exchange_values import normalize_availability

if TYPE_CHECKING:
    from gucmail.settings import SettingsService


CALENDAR_OPERATION_SETTINGS = (
    "calendar.invitation_response",
    "calendar.color_behavior",
    "calendar.notifications",
    "calendar.show_related_emails",
)
_CATEGORY_COLORS = ("cyan", "green", "yellow", "blue", "magenta", "bright_cyan", "bright_magenta")
_AVAILABILITY_COLORS = {
    "free": "green",
    "working_elsewhere": "cyan",
    "tentative": "yellow",
    "busy": "blue",
    "out_of_office": "magenta",
}


@dataclass(frozen=True, slots=True)
class CalendarOperationPreferences:
    invitation_response: CalendarResponse = CalendarResponse.ACCEPT
    color_behavior: str = "none"
    notifications: bool = True
    show_related_emails: bool = True

    def __post_init__(self) -> None:
        try:
            response = CalendarResponse(self.invitation_response)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "Choose accept, tentative or decline for invitation responses."
            ) from exc
        if self.color_behavior not in {"none", "availability", "category"}:
            raise ConfigurationError("Choose no event colors, availability or category colors.")
        if type(self.notifications) is not bool or type(self.show_related_emails) is not bool:
            raise ConfigurationError(
                "Calendar notification and related-mail choices must be On or Off."
            )
        object.__setattr__(self, "invitation_response", response)

    @classmethod
    def from_settings(cls, service: SettingsService) -> CalendarOperationPreferences:
        values = {
            row.spec.setting_id: row.effective_value
            for row in service.snapshot(CALENDAR_OPERATION_SETTINGS).values
        }
        return cls(
            CalendarResponse(str(values["calendar.invitation_response"])),
            str(values["calendar.color_behavior"]),
            values["calendar.notifications"],
            values["calendar.show_related_emails"],
        )

    def require_related(self) -> None:
        if not self.show_related_emails:
            raise ConfigurationError("Related-mail searches are disabled in Calendar settings.")

    def allows_notification(self, event: NotificationEvent) -> bool:
        # Native Calendar event types, never folder names or guessed subjects.
        return not event.event_type.startswith("calendar_") or self.notifications

    def event_color(self, event: CalendarEvent) -> str | None:
        """Known color tokens only; category text never becomes a Rich style."""
        if self.color_behavior == "none":
            return None
        if self.color_behavior == "availability":
            return _AVAILABILITY_COLORS.get(normalize_availability(event.availability))
        category = next(
            (name.strip().casefold() for name in event.categories if name.strip()), None
        )
        if category is None:
            return None
        digest = hashlib.sha256(category.encode("utf-8")).digest()
        return _CATEGORY_COLORS[int.from_bytes(digest[:4], "big") % len(_CATEGORY_COLORS)]
