"""Save the first page of a folder so the app can show it before the server answers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from gucmail.models import Address, Importance, ItemSource, MessageSummary
from gucmail.state import StateStore

# Enough for one screen. The list is replaced by live mail as soon as it arrives.
MAX_CACHED_ROWS = 100


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def summary_to_dict(summary: MessageSummary) -> dict[str, Any]:
    sender = summary.sender
    return {
        "reference": summary.reference,
        "subject": summary.subject,
        "sender": None if sender is None else {"email": sender.email, "name": sender.name},
        "received_at": _time(summary.received_at),
        "sent_at": _time(summary.sent_at),
        "is_read": summary.is_read,
        "has_attachments": summary.has_attachments,
        "importance": str(summary.importance),
        "flagged": summary.flagged,
        "categories": list(summary.categories),
        "size": summary.size,
        "folder_path": summary.folder_path,
        "conversation_id": summary.conversation_id,
    }


def summary_from_dict(data: dict[str, Any]) -> MessageSummary | None:
    reference = data.get("reference")
    if not isinstance(reference, str) or not reference:
        return None
    raw_sender = data.get("sender")
    sender = None
    if isinstance(raw_sender, dict) and isinstance(raw_sender.get("email"), str):
        name = raw_sender.get("name")
        sender = Address(raw_sender["email"], name if isinstance(name, str) else None)
    try:
        importance = Importance(str(data.get("importance", "normal")))
    except ValueError:
        importance = Importance.NORMAL
    size = data.get("size")
    categories = data.get("categories")
    return MessageSummary(
        reference=reference,
        subject=str(data.get("subject") or ""),
        sender=sender,
        received_at=_parse_time(data.get("received_at")),
        sent_at=_parse_time(data.get("sent_at")),
        is_read=bool(data.get("is_read")),
        has_attachments=bool(data.get("has_attachments")),
        importance=importance,
        flagged=bool(data.get("flagged")),
        categories=tuple(str(c) for c in categories) if isinstance(categories, list) else (),
        size=size if isinstance(size, int) else None,
        folder_path=data.get("folder_path") if isinstance(data.get("folder_path"), str) else None,
        conversation_id=(
            data.get("conversation_id") if isinstance(data.get("conversation_id"), str) else None
        ),
        source=ItemSource.CACHE,
    )


def save_message_list(state: StateStore, folder: str, messages: list[MessageSummary]) -> None:
    state.put_message_list(folder, [summary_to_dict(m) for m in messages[:MAX_CACHED_ROWS]])


def load_message_list(state: StateStore, folder: str) -> list[MessageSummary]:
    payload = state.get_message_list(folder) or []
    rows = (summary_from_dict(item) for item in payload if isinstance(item, dict))
    return [row for row in rows if row is not None]
