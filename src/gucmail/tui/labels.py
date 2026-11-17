"""Plain screen labels for internal values."""

from __future__ import annotations

from datetime import datetime

from gucmail.formatting import friendly_time

SEND_STATES = {
    "draft": "Draft",
    "scheduled": "Scheduled",
    "undo_delay": "Sending soon",
    "due": "Due",
    "waiting_connectivity": "Waiting for internet",
    "waiting_authentication": "Waiting for sign-in",
    "sending": "Sending",
    "sent": "Sent",
    "failed": "Failed",
    "retry_scheduled": "Will retry",
    "cancelled": "Cancelled",
    "send_state_uncertain": "Check Sent",
    "manual_review_required": "Check needed",
}


def send_state(value: str) -> str:
    return SEND_STATES.get(value, value.replace("_", " ").capitalize())


def when(value: str | datetime | None, timezone: str) -> str:
    """Friendly time for an ISO string or datetime. Falls back to the raw text."""

    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
    else:
        parsed = value
    try:
        return friendly_time(parsed, timezone=timezone)
    except Exception:
        return str(value)


_FOLDERS = {
    "inbox": "Inbox",
    "sentitems": "Sent",
    "sent": "Sent",
    "drafts": "Drafts",
    "deleteditems": "Deleted",
    "junkemail": "Junk",
    "archive": "Archive",
    "outbox": "Outbox",
}


def folder_label(value: str) -> str:
    """Show well-known folder names the way Outlook does. Paths pass through."""

    return _FOLDERS.get(value.casefold(), value)
