"""Canonical mailbox events and privacy-aware disclosure.

This module deliberately contains no Exchange or provider objects.  Watchers can build a
``NewMailEvent`` once, rule evaluation can use the immutable value, and every destination
must opt into an explicit disclosure policy before serializing it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from gucmail.models import Address, Importance
from gucmail.security import redact_text, sanitize_terminal


class MailEventType(StrEnum):
    """Mailbox changes that can enter the notification pipeline."""

    NEW_MESSAGE = "new_message"
    MOVED_MESSAGE = "moved_message"
    CHANGED_MESSAGE = "changed_message"
    DELETED_MESSAGE = "deleted_message"
    CALENDAR_CREATED = "calendar_created"
    CALENDAR_UPDATED = "calendar_updated"
    CALENDAR_DELETED = "calendar_deleted"


class PrivacyMode(StrEnum):
    """How much of a new message a notification may show. Used everywhere."""

    PRIVATE = "private"
    METADATA_ONLY = "metadata_only"
    SENDER_ONLY = "sender_only"
    SENDER_AND_SUBJECT = "sender_and_subject"
    FULL = "full"

    @classmethod
    def _missing_(cls, value: object) -> PrivacyMode | None:
        # Settings saved before 1.2 spelled this one "sender_subject".
        return cls.SENDER_AND_SUBJECT if value == "sender_subject" else None

    @property
    def label(self) -> str:
        return _PRIVACY_LABELS[self]


_PRIVACY_LABELS = {
    PrivacyMode.PRIVATE: "Only \"New message\"",
    PrivacyMode.METADATA_ONLY: "Folder only",
    PrivacyMode.SENDER_ONLY: "Sender only",
    PrivacyMode.SENDER_AND_SUBJECT: "Sender and subject",
    PrivacyMode.FULL: "Sender, subject and preview",
}


EVENT_PAYLOAD_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "occurred_at",
        "detected_at",
        "account_id",
        "folder",
        "message_reference",
        "sender",
        "reply_to",
        "recipients",
        "cc",
        "subject",
        "body_preview",
        "importance",
        "is_read",
        "has_attachments",
        "attachment_count",
        "attachment_filenames",
        "size_bytes",
        "categories",
        "conversation_id",
        "internet_message_id",
        "sender_in_contacts",
        "matched_rule_ids",
        "delivered_provider_ids",
        "summary",
    }
)

_PRIVATE_FIELDS = frozenset({"event_id", "event_type", "detected_at", "summary"})
_METADATA_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "occurred_at",
        "detected_at",
        "folder",
        "message_reference",
        "importance",
        "is_read",
        "has_attachments",
        "attachment_count",
        "size_bytes",
        "sender_in_contacts",
        "matched_rule_ids",
        "delivered_provider_ids",
        "summary",
    }
)
_SENDER_FIELDS = _METADATA_FIELDS | frozenset({"sender"})
_SUBJECT_FIELDS = _SENDER_FIELDS | frozenset({"subject"})
_FULL_FIELDS = EVENT_PAYLOAD_FIELDS


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a time zone")


@dataclass(frozen=True, slots=True)
class NewMailEvent:
    """Provider-neutral, immutable event for one mailbox change.

    Optional tuples use ``None`` to mean "not fetched" and an empty tuple to mean "fetched
    and empty".  This distinction makes rule evaluation fail closed instead of guessing when
    an expensive field was intentionally omitted.
    """

    event_id: str
    occurred_at: datetime
    detected_at: datetime
    account_id: str
    event_type: MailEventType = MailEventType.NEW_MESSAGE
    folder: str | None = None
    message_reference: str | None = None
    sender: Address | None = None
    reply_to: tuple[Address, ...] | None = None
    recipients: tuple[Address, ...] | None = None
    cc: tuple[Address, ...] | None = None
    subject: str | None = None
    body_preview: str | None = None
    importance: Importance | None = None
    is_read: bool | None = None
    has_attachments: bool | None = None
    attachment_count: int | None = None
    attachment_filenames: tuple[str, ...] | None = None
    size_bytes: int | None = None
    categories: tuple[str, ...] | None = None
    conversation_id: str | None = None
    internet_message_id: str | None = None
    sender_in_contacts: bool | None = None
    matched_rule_ids: tuple[str, ...] = ()
    delivered_provider_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.event_id.strip() or len(self.event_id) > 512:
            raise ValueError("event_id must be between 1 and 512 characters")
        if not self.account_id.strip() or len(self.account_id) > 512:
            raise ValueError("account_id must be between 1 and 512 characters")
        _aware(self.occurred_at, "occurred_at")
        _aware(self.detected_at, "detected_at")
        if self.attachment_count is not None and self.attachment_count < 0:
            raise ValueError("attachment_count cannot be negative")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        if self.has_attachments is False and self.attachment_count not in {None, 0}:
            raise ValueError("attachment_count conflicts with has_attachments=False")
        if self.has_attachments is False and self.attachment_filenames:
            raise ValueError("attachment_filenames conflict with has_attachments=False")
        for label, values in (
            ("matched_rule_ids", self.matched_rule_ids),
            ("delivered_provider_ids", self.delivered_provider_ids),
        ):
            if any(not value.strip() for value in values):
                raise ValueError(f"{label} cannot contain empty identifiers")


@dataclass(frozen=True, slots=True)
class EventDisclosurePolicy:
    """Fail-closed field policy applied independently for every destination."""

    mode: PrivacyMode = PrivacyMode.PRIVATE
    field_allowlist: frozenset[str] | None = None
    redact_addresses: bool = True
    include_attachment_names: bool = False
    include_server_identifiers: bool = False

    def __post_init__(self) -> None:
        if self.field_allowlist is None:
            return
        unknown = self.field_allowlist - EVENT_PAYLOAD_FIELDS
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown event disclosure fields: {names}")

    @property
    def permitted_fields(self) -> frozenset[str]:
        by_mode = {
            PrivacyMode.PRIVATE: _PRIVATE_FIELDS,
            PrivacyMode.METADATA_ONLY: _METADATA_FIELDS,
            PrivacyMode.SENDER_ONLY: _SENDER_FIELDS,
            PrivacyMode.SENDER_AND_SUBJECT: _SUBJECT_FIELDS,
            PrivacyMode.FULL: _FULL_FIELDS,
        }[self.mode]
        if not self.include_attachment_names:
            by_mode = by_mode - {"attachment_filenames"}
        if not self.include_server_identifiers:
            by_mode = by_mode - {"conversation_id", "internet_message_id"}
        if self.field_allowlist is not None:
            by_mode &= self.field_allowlist
        return frozenset(by_mode)


def stable_event_id(
    *,
    account_id: str,
    event_type: MailEventType | str,
    occurred_at: datetime,
    message_reference: str | None = None,
    internet_message_id: str | None = None,
    folder: str | None = None,
    sender_address: str | None = None,
    subject: str | None = None,
) -> str:
    """Return a restart-stable opaque ID without exposing mailbox identifiers.

    Stable local references are preferred.  The bounded metadata fallback exists for EWS
    delete/change records that do not carry an item reference.
    """

    if not account_id.strip():
        raise ValueError("account_id is required")
    _aware(occurred_at, "occurred_at")
    identity: tuple[str, ...]
    if message_reference:
        identity = ("local-reference", message_reference)
    elif internet_message_id:
        identity = ("internet-message-id", internet_message_id.casefold())
    else:
        identity = (
            "metadata",
            (folder or "").casefold(),
            occurred_at.astimezone(UTC).isoformat(),
            (sender_address or "").casefold(),
            subject or "",
        )
    material = "\x00".join((account_id.casefold(), str(event_type), *identity))
    digest = hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()
    return f"mail-{digest[:40]}"


def _safe_text(value: str, *, maximum: int, redact_addresses: bool) -> str:
    safe = sanitize_terminal(value, max_length=maximum)
    return redact_text(safe, redact_emails=True) if redact_addresses else safe


def _address_payload(address: Address, *, redact_addresses: bool) -> dict[str, str | None]:
    email = _safe_text(address.email, maximum=320, redact_addresses=redact_addresses)
    name = (
        _safe_text(address.name, maximum=300, redact_addresses=redact_addresses)
        if address.name
        else None
    )
    return {"address": email, "name": name}


def _addresses_payload(
    addresses: tuple[Address, ...] | None,
    *,
    redact_addresses: bool,
) -> list[dict[str, str | None]] | None:
    if addresses is None:
        return None
    return [_address_payload(address, redact_addresses=redact_addresses) for address in addresses]


def event_payload(
    event: NewMailEvent,
    policy: EventDisclosurePolicy | None = None,
) -> dict[str, Any]:
    """Serialize an event according to a provider-specific disclosure policy.

    The default is intentionally private.  Unknown future fields cannot leak because the
    payload is assembled from a fixed schema rather than from ``dataclasses.asdict``.
    """

    selected = policy or EventDisclosurePolicy()
    values: dict[str, Any] = {
        "event_id": sanitize_terminal(event.event_id, max_length=512),
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
        "detected_at": event.detected_at.astimezone(UTC).isoformat(),
        "account_id": _safe_text(
            event.account_id,
            maximum=512,
            redact_addresses=selected.redact_addresses,
        ),
        "folder": (
            _safe_text(event.folder, maximum=500, redact_addresses=selected.redact_addresses)
            if event.folder is not None
            else None
        ),
        "message_reference": (
            sanitize_terminal(event.message_reference, max_length=512)
            if event.message_reference is not None
            else None
        ),
        "sender": (
            _address_payload(event.sender, redact_addresses=selected.redact_addresses)
            if event.sender is not None
            else None
        ),
        "reply_to": _addresses_payload(
            event.reply_to,
            redact_addresses=selected.redact_addresses,
        ),
        "recipients": _addresses_payload(
            event.recipients,
            redact_addresses=selected.redact_addresses,
        ),
        "cc": _addresses_payload(event.cc, redact_addresses=selected.redact_addresses),
        "subject": (
            _safe_text(event.subject, maximum=1_000, redact_addresses=selected.redact_addresses)
            if event.subject is not None
            else None
        ),
        "body_preview": (
            _safe_text(
                event.body_preview,
                maximum=4_096,
                redact_addresses=selected.redact_addresses,
            )
            if event.body_preview is not None
            else None
        ),
        "importance": event.importance.value if event.importance is not None else None,
        "is_read": event.is_read,
        "has_attachments": event.has_attachments,
        "attachment_count": event.attachment_count,
        "attachment_filenames": (
            [
                _safe_text(name, maximum=500, redact_addresses=selected.redact_addresses)
                for name in event.attachment_filenames
            ]
            if event.attachment_filenames is not None
            else None
        ),
        "size_bytes": event.size_bytes,
        "categories": (
            [
                _safe_text(value, maximum=200, redact_addresses=selected.redact_addresses)
                for value in event.categories
            ]
            if event.categories is not None
            else None
        ),
        "conversation_id": (
            sanitize_terminal(event.conversation_id, max_length=1_000)
            if event.conversation_id is not None
            else None
        ),
        "internet_message_id": (
            _safe_text(
                event.internet_message_id,
                maximum=1_000,
                redact_addresses=selected.redact_addresses,
            )
            if event.internet_message_id is not None
            else None
        ),
        "sender_in_contacts": event.sender_in_contacts,
        "matched_rule_ids": list(event.matched_rule_ids),
        "delivered_provider_ids": list(event.delivered_provider_ids),
        "summary": "New GUC email",
    }
    return {name: values[name] for name in values if name in selected.permitted_fields}


def event_json(
    event: NewMailEvent,
    policy: EventDisclosurePolicy | None = None,
) -> str:
    """Return deterministic compact JSON for logs, hooks, and outbound providers."""

    return json.dumps(
        event_payload(event, policy),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "EVENT_PAYLOAD_FIELDS",
    "EventDisclosurePolicy",
    "MailEventType",
    "NewMailEvent",
    "PrivacyMode",
    "event_json",
    "event_payload",
    "stable_event_id",
]
