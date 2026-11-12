"""Shared Mail defaults and exact conversation grouping within bounded pages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

from gucmail.errors import ConfigurationError
from gucmail.services._base import validate_reference

if TYPE_CHECKING:
    from gucmail.settings import SettingsService

SORT_FIELDS = ("received", "sent", "subject", "sender", "size", "importance")
SORT_DIRECTIONS = ("ascending", "descending")
_Row = TypeVar("_Row")


def _conversation_id(message: Any) -> str | None:
    value = (
        message.get("conversation_id")
        if isinstance(message, Mapping)
        else getattr(message, "conversation_id", None)
    )
    return value if isinstance(value, str) and value.strip() else None


def group_message_page(messages: Sequence[_Row], *, enabled: bool) -> tuple[_Row, ...]:
    """Group exact IDs within this page without fetching, hiding or replacing rows."""

    if not enabled:
        return tuple(messages)
    groups: dict[tuple[str, str | int], list[_Row]] = {}
    for index, message in enumerate(messages):
        identity = _conversation_id(message)
        key: tuple[str, str | int] = ("conversation", identity) if identity else ("row", index)
        groups.setdefault(key, []).append(message)
    return tuple(message for group in groups.values() for message in group)


def conversation_page_positions(messages: Sequence[Any]) -> tuple[tuple[int, int] | None, ...]:
    """Number actual group members; counts describe only the currently visible page."""

    identities = tuple(_conversation_id(message) for message in messages)
    totals = Counter(identity for identity in identities if identity is not None)
    seen: Counter[str] = Counter()
    positions: list[tuple[int, int] | None] = []
    for identity in identities:
        if identity is None or totals[identity] < 2:
            positions.append(None)
        else:
            seen[identity] += 1
            positions.append((seen[identity], totals[identity]))
    return tuple(positions)


class ReplyMode(StrEnum):
    SENDER = "sender"
    ALL = "all"


class DeleteBehavior(StrEnum):
    DELETED_ITEMS = "deleted_items"
    MOVE_TO_FOLDER = "move_to_folder"


@dataclass(frozen=True, slots=True)
class MailDeletionPreferences:
    """Ordinary Delete can only move mail; permanent deletion is always explicit."""

    behavior: DeleteBehavior = DeleteBehavior.DELETED_ITEMS
    folder: str | None = None

    @classmethod
    def from_settings(cls, service: SettingsService) -> MailDeletionPreferences:
        snapshot = service.snapshot(("mail.delete_behavior", "mail.delete_folder"))
        values = {row.spec.setting_id: row.effective_value for row in snapshot.values}
        folder = values["mail.delete_folder"]
        return cls(
            DeleteBehavior(str(values["mail.delete_behavior"])),
            folder if isinstance(folder, str) and folder else None,
        )

    def destination_for(
        self, *, behavior: DeleteBehavior | None = None, folder: str | None = None
    ) -> str | None:
        selected = behavior or (
            DeleteBehavior.MOVE_TO_FOLDER if folder is not None else self.behavior
        )
        if selected is DeleteBehavior.DELETED_ITEMS:
            if folder is not None:
                raise ConfigurationError("Choose either Deleted Items or an explicit destination.")
            return None
        destination = self.folder if folder is None else folder
        if not destination:
            raise ConfigurationError("Choose a Delete destination folder in Settings or with --to.")
        return validate_reference(destination, label="Delete destination folder")


@dataclass(frozen=True, slots=True)
class MailResponsePreferences:
    """Defaults for a new response; saved compositions keep their reviewed content."""

    reply_mode: ReplyMode = ReplyMode.SENDER
    reply_include_history: bool = True
    reply_all_include_history: bool = True
    forward_include_history: bool = True

    @classmethod
    def from_settings(cls, service: SettingsService) -> MailResponsePreferences:
        snapshot = service.snapshot(
            (
                "mail.default_reply_mode",
                "mail.reply_include_history",
                "mail.reply_all_include_history",
                "mail.forward_include_history",
            )
        )
        values = {row.spec.setting_id: row.effective_value for row in snapshot.values}
        return cls(
            reply_mode=ReplyMode(str(values["mail.default_reply_mode"])),
            reply_include_history=bool(values["mail.reply_include_history"]),
            reply_all_include_history=bool(values["mail.reply_all_include_history"]),
            forward_include_history=bool(values["mail.forward_include_history"]),
        )


_FIELDS = {
    "received": "datetime_received",
    "sent": "datetime_sent",
    "subject": "subject",
    "sender": "sender",
    "size": "size",
    "importance": "importance",
}


@dataclass(frozen=True, slots=True)
class MailSortPreferences:
    field: str = "received"
    direction: str = "descending"

    def __post_init__(self) -> None:
        if self.field not in SORT_FIELDS or self.direction not in SORT_DIRECTIONS:
            raise ConfigurationError("Choose a supported message sort field and direction.")

    @property
    def order_by(self) -> str:
        return ("-" if self.direction == "descending" else "") + _FIELDS[self.field]

    @classmethod
    def from_settings(cls, service: SettingsService) -> MailSortPreferences:
        return cls(
            field=str(service.value("mail.sort_field").effective_value),
            direction=str(service.value("mail.sort_direction").effective_value),
        )
