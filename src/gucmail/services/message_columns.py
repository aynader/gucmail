"""Validated persistent column choices and deterministic adaptive widths."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from gucmail.errors import ConfigurationError

COLUMN_LABELS = {
    "state": "State",
    "sender": "From",
    "subject": "Subject",
    "received": "Received",
    "size": "Size",
    "attachments": "Files",
    "flagged": "Flag",
    "importance": "Importance",
    "sent": "Sent",
    "folder": "Folder",
    "conversation": "Conversation",
}


@dataclass(frozen=True, slots=True)
class MessageColumn:
    id: str
    visible: bool = True
    compact: bool = False
    width: int | None = None


DEFAULT_COLUMNS = tuple(
    MessageColumn(key, visible=index < 5, compact=index < 4)
    for index, key in enumerate(COLUMN_LABELS)
)


def columns_value(columns: tuple[MessageColumn, ...] = DEFAULT_COLUMNS) -> list[dict[str, object]]:
    return [asdict(column) for column in columns]


def parse_columns(value: object) -> tuple[MessageColumn, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ConfigurationError("Message columns must be a valid column list.") from exc
    if not isinstance(value, (list, tuple)) or len(value) != len(COLUMN_LABELS):
        raise ConfigurationError("Include each of the 11 message columns exactly once.")
    columns = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {"id", "visible", "compact", "width"}:
            raise ConfigurationError("Each column needs id, visible, compact and width.")
        key = row["id"]
        if not isinstance(key, str) or key not in COLUMN_LABELS or key in seen:
            raise ConfigurationError("Message columns contain an unknown or duplicate identifier.")
        seen.add(key)
        visible, compact, width = row["visible"], row["compact"], row["width"]
        if not isinstance(visible, bool) or not isinstance(compact, bool):
            raise ConfigurationError("Column visibility and compact choices must be booleans.")
        if width is not None and (type(width) is not int or not 1 <= width <= 120):
            raise ConfigurationError("Column width must be automatic or an integer from 1 to 120.")
        columns.append(MessageColumn(key, visible, compact, width))
    if not any(column.visible and column.compact for column in columns):
        raise ConfigurationError("Keep at least one visible column in compact terminals.")
    return tuple(columns)


MIN_SUBJECT_WIDTH = 20
_ESSENTIAL_COLUMNS = frozenset({"state", "sender", "subject"})


def fitted_columns(
    columns: tuple[MessageColumn, ...], width: int, cell_padding: int
) -> tuple[tuple[MessageColumn, int], ...]:
    width = max(18, width)
    visible = tuple(c for c in columns if c.visible and (width >= 90 or c.compact))
    automatic = {
        "state": 4,
        "sender": max(6, min(28, width // 4)),
        "subject": 4,
        "received": 12,
        "size": 8,
        "attachments": 5,
        "flagged": 4,
        "importance": 10,
        "sent": 20,
        "folder": 18,
        "conversation": 18,
    }
    while True:
        sizes = [c.width if c.width is not None else automatic[c.id] for c in visible]
        subject = None
        for index, column in enumerate(visible):
            if column.id == "subject" and column.width is None:
                subject = index
                sizes[index] = max(
                    4,
                    width
                    - 2 * cell_padding * len(visible)
                    - sum(size for i, size in enumerate(sizes) if i != index),
                )
        # Keep the subject readable. Extra automatic columns give way first, last one first.
        extras = [
            index
            for index, column in enumerate(visible)
            if column.id not in _ESSENTIAL_COLUMNS and column.width is None
        ]
        if subject is None or sizes[subject] >= MIN_SUBJECT_WIDTH or not extras:
            return tuple(zip(visible, sizes, strict=True))
        visible = tuple(c for i, c in enumerate(visible) if i != extras[-1])
