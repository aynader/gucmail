"""Human and structured rendering shared by the CLI and TUI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.table import Table
from rich.text import Text

from gucmail.models import QuotaInfo, to_primitive
from gucmail.security import sanitize_terminal


def format_timestamp(value: datetime | None, *, pattern: str, timezone: str) -> str:
    """Apply the display preference without changing structured timestamp values."""
    if value is None:
        return ""
    local = value.astimezone(ZoneInfo(timezone)) if value.tzinfo is not None else value
    try:
        text = local.strftime(pattern)
    except (ValueError, OverflowError):
        text = local.isoformat(sep=" ", timespec="minutes")
    return sanitize_terminal(text, max_length=500)


def friendly_time(
    value: datetime | None,
    *,
    timezone: str,
    now: datetime | None = None,
    date_only: bool = False,
) -> str:
    """Short local time for screens, such as "Today 10:00", "Wed 12:00" or "3 Dec 2027"."""

    if value is None:
        return ""
    zone = ZoneInfo(timezone)
    local = value.astimezone(zone) if value.tzinfo is not None else value
    today = (now or datetime.now(zone)).astimezone(zone).date()
    days = (local.date() - today).days
    clock = "" if date_only else " " + local.strftime("%H:%M")
    if days == 0:
        day = "Today"
    elif days == 1:
        day = "Tomorrow"
    elif days == -1:
        day = "Yesterday"
    elif 1 < days < 7:
        day = local.strftime("%a")
    elif local.year == today.year:
        day = f"{local.day} {local.strftime('%b')}"
    else:
        day = f"{local.day} {local.strftime('%b')} {local.year}"
    return day + clock


def list_time(value: datetime | None, *, timezone: str, now: datetime | None = None) -> str:
    """Short time for a message list. "10:42", "Yesterday", "Tue 10:42", "4 Mar", "4 Mar 2025"."""

    if value is None:
        return ""
    zone = ZoneInfo(timezone)
    local = value.astimezone(zone) if value.tzinfo is not None else value
    today = (now or datetime.now(zone)).astimezone(zone).date()
    days = (today - local.date()).days
    if days == 0:
        return local.strftime("%H:%M")
    if days == 1:
        return "Yesterday"
    if 1 < days < 7:
        return local.strftime("%a %H:%M")
    if local.year == today.year:
        return f"{local.day} {local.strftime('%b')}"
    return f"{local.day} {local.strftime('%b')} {local.year}"


def friendly_date(value: date | None, *, timezone: str = "UTC") -> str:
    """Like friendly_time, for a plain date."""

    if value is None:
        return ""
    return friendly_time(
        datetime(value.year, value.month, value.day, 12), timezone=timezone, date_only=True
    )


def json_text(value: Any, *, pretty: bool = True) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=pretty,
        separators=None if pretty else (",", ":"),
    )


def human_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def quota_text(quota: QuotaInfo, *, width: int = 10, monochrome: bool = False) -> str:
    percent = quota.percent_used
    if percent is None or quota.used_bytes is None or quota.quota_bytes is None:
        return "Mailbox: Quota unavailable"
    filled = min(width, max(0, round((percent / 100) * width)))
    if monochrome:
        bar = "#" * filled + "-" * (width - filled)
    else:
        bar = "█" * filled + "░" * (width - filled)
    return (
        f"Mailbox [{bar}] {percent:.1f}%  "
        f"{human_bytes(quota.used_bytes)}/{human_bytes(quota.quota_bytes)}"
    )


def _known_mapping(value: Mapping[str, Any]) -> str | None:
    """A one-line summary for recognized nested records (addresses, outcomes)."""

    email = value.get("email")
    if isinstance(email, str) and set(value) <= {"email", "name", "mailbox_type", "routing_type"}:
        name = value.get("name")
        return f"{name} <{email}>" if name else email
    summary = value.get("summary")
    if isinstance(summary, str) and "operation" in value:
        return summary
    reference = value.get("reference")
    if reference is not None and "status" in value:
        return f"{reference}: {_cell(value.get('status'))}"
    label = value.get("subject", value.get("name"))
    if reference is not None and isinstance(label, str):
        return f"{reference} ({label})"
    return None


def _mapping_cell(value: Mapping[str, Any]) -> str:
    """Summarize nested domain records instead of dumping raw JSON."""

    known = _known_mapping(value)
    if known is not None:
        return sanitize_terminal(known, max_length=500)
    return sanitize_terminal(_scalar_summary(value), max_length=240)


def _scalar_summary(value: Mapping[str, Any]) -> str:
    """Readable ``key: value`` pairs for the scalar fields of a nested record."""

    parts = []
    for key, item in value.items():
        if item is None or item == "" or isinstance(item, (Mapping, list, tuple, set)):
            continue
        parts.append(f"{str(key).replace('_', ' ')}: {_cell(item)}")
    return ", ".join(parts) if parts else "(details with --json)"


def setting_rows(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rows for a one-record "Setting / Value" table, expanding nested records once.

    A finished mutation already showed its plan as a preview, so a bulky ``plan``
    record is summarized (or omitted) rather than dumped as JSON.
    """

    rows: list[dict[str, Any]] = []
    for key, item in value.items():
        if key == "plan" and isinstance(item, Mapping) and len(value) > 1:
            summary = item.get("summary")
            if isinstance(summary, str) and summary:
                rows.append({"setting": key, "value": summary})
            continue
        if isinstance(item, Mapping) and item and _known_mapping(item) is None:
            for sub_key, sub_item in item.items():
                if sub_item in (None, "", [], {}):
                    continue
                rows.append({"setting": f"{key} {sub_key}".replace("_", " "), "value": sub_item})
            continue
        rows.append({"setting": key, "value": item})
    return rows


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_cell(item) for item in value)
    if isinstance(value, Mapping):
        return _mapping_cell(value)
    return sanitize_terminal(value, max_length=500)


# Human tables stay readable at 80 columns; --json always carries every field.
MAX_HUMAN_COLUMNS = 7
PREFERRED_COLUMNS = (
    "reference",
    "name",
    "path",
    "state",
    "status",
    "received_at",
    "updated_at",
    "sender",
    "to",
    "subject",
    "is_read",
    "flagged",
    "has_attachments",
    "size",
    "content_type",
    "dangerous",
    "unread_count",
    "total_count",
    "location",
    "revision",
)


def page_parts(value: Any) -> tuple[list[Any], dict[str, Any]] | None:
    """Return ``(items, metadata)`` for a primitive bounded page, else ``None``."""

    if (
        isinstance(value, Mapping)
        and isinstance(value.get("items"), list)
        and ("has_more" in value or "next_offset" in value)
    ):
        return list(value["items"]), {k: v for k, v in value.items() if k != "items"}
    return None


def compact_rows(
    rows: Sequence[Mapping[str, Any]], *, max_columns: int = MAX_HUMAN_COLUMNS
) -> tuple[list[dict[str, Any]], int]:
    """Choose the most useful columns for wide records; return hidden-column count."""

    columns: list[str] = []
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    if len(columns) <= max_columns:
        return [dict(row) for row in rows], 0
    chosen = [column for column in PREFERRED_COLUMNS if column in columns][:max_columns]
    for column in columns:
        if len(chosen) >= max_columns:
            break
        if column not in chosen:
            chosen.append(column)
    chosen.sort(key=columns.index)
    return [{column: row.get(column) for column in chosen} for row in rows], len(columns) - len(
        chosen
    )


def render_table(
    rows: Sequence[Mapping[str, Any]],
    *,
    title: str | None = None,
    console: Console | None = None,
    no_header: bool = False,
) -> None:
    output = console or Console()
    safe_title = Text(sanitize_terminal(title)) if title else None
    if not rows:
        if safe_title is not None:
            output.print(safe_title)
        output.print("No matching items.")
        return
    columns = list(rows[0].keys())
    table = Table(title=safe_title, show_header=not no_header, header_style="bold")
    for column in columns:
        table.add_column(Text(column.replace("_", " ").title()), overflow="fold")
    for row in rows:
        # Text() keeps hostile subjects such as "[/b]" literal: as markup they could
        # restyle output, emit hyperlinks, or crash Rich with a MarkupError.
        table.add_row(*(Text(_cell(row.get(column))) for column in columns))
    output.print(table)
