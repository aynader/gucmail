"""Account-local controls for explicit contact discovery and history operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from gucmail.errors import ConfigurationError

if TYPE_CHECKING:
    from gucmail.settings import SettingsService

CONTACT_OPERATION_SETTINGS = (
    "contacts.frequent_correspondents",
    "contacts.directory_lookup",
    "contacts.duplicate_detection",
    "contacts.correspondence_history",
    "contacts.history_indexing",
    "contacts.history_retention_days",
)


@dataclass(frozen=True, slots=True)
class ContactOperationPreferences:
    frequent_correspondents: bool = True
    directory_lookup: bool = True
    duplicate_detection: bool = True
    correspondence_history: bool = True
    history_indexing: bool = False
    history_retention_days: int = 30

    def __post_init__(self) -> None:
        for name in (
            "frequent_correspondents",
            "directory_lookup",
            "duplicate_detection",
            "correspondence_history",
            "history_indexing",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ConfigurationError(
                    f"{name.replace('_', ' ').capitalize()} must be on or off."
                )
        days = self.history_retention_days
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ConfigurationError("Contact history retention must be between 1 and 365 days.")

    @classmethod
    def from_settings(cls, service: SettingsService) -> ContactOperationPreferences:
        values = {
            row.spec.setting_id.removeprefix("contacts."): row.effective_value
            for row in service.snapshot(CONTACT_OPERATION_SETTINGS).values
        }
        return cls(**values)

    def require(self, operation: str) -> None:
        field = {
            "directory": "directory_lookup",
            "duplicates": "duplicate_detection",
            "frequent": "frequent_correspondents",
            "history": "correspondence_history",
        }.get(operation)
        if field is None:
            raise ConfigurationError("Unknown contact operation.")
        if not getattr(self, field):
            raise ConfigurationError(
                f"{field.replace('_', ' ').capitalize()} is disabled in Contacts settings."
            )
        if operation == "frequent" and not self.correspondence_history:
            self.require("history")
