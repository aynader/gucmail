"""Bounded read-only server occurrence state, separate from a complete audit trail."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from gucmail.errors import ConfigurationError
from gucmail.security import sanitize_terminal


def history_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise ConfigurationError("Calendar history limit must be from 1 to 200 per list.")
    return value


@dataclass(frozen=True, slots=True)
class CalendarOccurrenceInfo:
    reference: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    original_start: datetime | None = None


@dataclass(frozen=True, slots=True)
class CalendarSeriesHistory:
    requested_reference: str
    series_reference: str
    item_type: str
    observed_at: datetime
    limit_per_list: int
    first_occurrence: CalendarOccurrenceInfo | None = None
    last_occurrence: CalendarOccurrenceInfo | None = None
    modified_occurrences: tuple[CalendarOccurrenceInfo, ...] = ()
    deleted_occurrences: tuple[CalendarOccurrenceInfo, ...] = ()
    modified_available: bool = False
    deleted_available: bool = False
    modified_truncated: bool = False
    deleted_truncated: bool = False
    notes: tuple[str, ...] = (
        "Current server exception/deletion metadata; not a complete change audit trail.",
        "Missing fields mean unavailable, not known-empty history.",
        "Read-only metadata cannot be restored as writable occurrence history.",
    )


def history_text(history: CalendarSeriesHistory, timezone: str) -> str:
    """Render original identities and localized times as literal, safe text."""
    zone = ZoneInfo(timezone)

    def timestamp(value: datetime | None) -> str:
        return value.astimezone(zone).isoformat() if value is not None else "unavailable"

    def occurrence(value: CalendarOccurrenceInfo) -> str:
        return (
            f"{sanitize_terminal(value.reference or '(no item reference)', max_length=200)} | "
            f"start {timestamp(value.start)} | end {timestamp(value.end)} | "
            f"original {timestamp(value.original_start)}"
        )

    lines = [
        "Calendar series occurrence history (read-only)",
        f"Requested: {sanitize_terminal(history.requested_reference, max_length=200)}",
        f"Series: {sanitize_terminal(history.series_reference, max_length=200)}",
        f"Item type: {sanitize_terminal(history.item_type, max_length=80)}",
        f"Display zone: {timezone}; observed {timestamp(history.observed_at)}",
        *(sanitize_terminal(note, max_length=400) for note in history.notes),
        "First: "
        + (occurrence(history.first_occurrence) if history.first_occurrence else "unavailable"),
        "Last: "
        + (occurrence(history.last_occurrence) if history.last_occurrence else "unavailable"),
    ]
    for label, values, available, truncated in (
        (
            "Modified",
            history.modified_occurrences,
            history.modified_available,
            history.modified_truncated,
        ),
        (
            "Deleted",
            history.deleted_occurrences,
            history.deleted_available,
            history.deleted_truncated,
        ),
    ):
        state = "unavailable" if not available else "truncated" if truncated else "returned"
        lines.append(f"{label}: {state}; {len(values)} entries; limit {history.limit_per_list}")
        lines.extend(occurrence(value) for value in values)
    return "\n".join(lines)
