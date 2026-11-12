"""Provider-independent delivery ledger, deduplication, and privacy-safe history views."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol

from gucmail.notification_events import (
    EventDisclosurePolicy,
    NewMailEvent,
    event_payload,
)
from gucmail.security import redact_text, sanitize_terminal


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a time zone")


class ProviderDeliveryState(StrEnum):
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    SUPPRESSED = "suppressed"
    QUEUED = "queued"
    DIGESTED = "digested"


class DeliveryClaimState(StrEnum):
    CLAIMED = "claimed"
    ALREADY_DELIVERED = "already_delivered"
    IN_PROGRESS = "in_progress"
    RETRY_NOT_DUE = "retry_not_due"


def delivery_idempotency_key(event_id: str, provider_id: str) -> str:
    """Return an opaque, stable key scoped independently to one provider."""

    if not event_id.strip() or not provider_id.strip():
        raise ValueError("event_id and provider_id are required")
    material = f"{event_id}\x00{provider_id.casefold()}".encode("utf-8", errors="replace")
    return "delivery-" + hashlib.sha256(material).hexdigest()[:40]


@dataclass(frozen=True, slots=True)
class NotificationHistoryEntry:
    event: NewMailEvent
    provider_id: str
    state: ProviderDeliveryState
    attempts: int
    created_at: datetime
    updated_at: datetime
    matching_rule_ids: tuple[str, ...] = ()
    lease_until: datetime | None = None
    retry_at: datetime | None = None
    last_error: str | None = None
    status_code: int | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or len(self.provider_id) > 200:
            raise ValueError("provider_id must be between 1 and 200 characters")
        if self.attempts < 0:
            raise ValueError("delivery attempts cannot be negative")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        if self.lease_until is not None:
            _aware(self.lease_until, "lease_until")
        if self.retry_at is not None:
            _aware(self.retry_at, "retry_at")
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be an HTTP-style status between 100 and 599")
        if any(not value.strip() for value in self.matching_rule_ids):
            raise ValueError("matching_rule_ids cannot contain empty values")

    @property
    def idempotency_key(self) -> str:
        return delivery_idempotency_key(self.event.event_id, self.provider_id)


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    state: DeliveryClaimState
    idempotency_key: str
    attempts: int
    lease_until: datetime | None = None
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HistoryDisclosurePolicy:
    """Event privacy plus conservative delivery-error disclosure."""

    event: EventDisclosurePolicy = field(default_factory=EventDisclosurePolicy)
    include_delivery_error: bool = False
    include_status_code: bool = True


def history_payload(
    entry: NotificationHistoryEntry,
    policy: HistoryDisclosurePolicy | None = None,
) -> dict[str, Any]:
    selected = policy or HistoryDisclosurePolicy()
    payload: dict[str, Any] = {
        "event": event_payload(entry.event, selected.event),
        "provider_id": sanitize_terminal(entry.provider_id, max_length=200),
        "state": entry.state.value,
        "attempts": entry.attempts,
        "created_at": entry.created_at.astimezone(UTC).isoformat(),
        "updated_at": entry.updated_at.astimezone(UTC).isoformat(),
        "matching_rule_ids": [
            sanitize_terminal(value, max_length=200) for value in entry.matching_rule_ids
        ],
        "retry_at": entry.retry_at.astimezone(UTC).isoformat() if entry.retry_at else None,
        "idempotency_key": entry.idempotency_key,
    }
    if selected.include_status_code:
        payload["status_code"] = entry.status_code
    if selected.include_delivery_error:
        payload["last_error"] = (
            redact_text(entry.last_error, redact_emails=True)[:1_000]
            if entry.last_error is not None
            else None
        )
    return payload


@dataclass(frozen=True, slots=True)
class NotificationHistoryQuery:
    text: str | None = None
    provider_ids: frozenset[str] = field(default_factory=frozenset)
    states: frozenset[ProviderDeliveryState] = field(default_factory=frozenset)
    rule_ids: frozenset[str] = field(default_factory=frozenset)
    after: datetime | None = None
    before: datetime | None = None
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        if self.after is not None:
            _aware(self.after, "history after")
        if self.before is not None:
            _aware(self.before, "history before")
        if self.after is not None and self.before is not None and self.after >= self.before:
            raise ValueError("history after must be earlier than before")
        if not 1 <= self.limit <= 500:
            raise ValueError("history limit must be between 1 and 500")
        if self.offset < 0:
            raise ValueError("history offset cannot be negative")
        providers = frozenset(value.casefold().strip() for value in self.provider_ids)
        rules = frozenset(value.casefold().strip() for value in self.rule_ids)
        if any(not value for value in (*providers, *rules)):
            raise ValueError("history filter identifiers cannot be empty")
        object.__setattr__(self, "provider_ids", providers)
        object.__setattr__(self, "rule_ids", rules)


class NotificationHistoryRepository(Protocol):
    """Repository interface intended for a transactional StateStore adapter."""

    def claim(
        self,
        event: NewMailEvent,
        provider_id: str,
        *,
        at: datetime,
        lease_for: timedelta,
        matching_rule_ids: Sequence[str] = (),
    ) -> DeliveryClaim: ...

    def record(
        self,
        event_id: str,
        provider_id: str,
        state: ProviderDeliveryState,
        *,
        at: datetime,
        retry_at: datetime | None = None,
        error: str | None = None,
        status_code: int | None = None,
    ) -> NotificationHistoryEntry: ...

    def history(
        self,
        query: NotificationHistoryQuery | None = None,
    ) -> tuple[NotificationHistoryEntry, ...]: ...

    def request_retry(
        self,
        event_id: str,
        provider_id: str,
        *,
        at: datetime,
    ) -> NotificationHistoryEntry: ...

    def clear_before(self, before: datetime) -> int: ...

    def provider_states(self, event_id: str) -> Mapping[str, ProviderDeliveryState]: ...


class InMemoryNotificationHistoryRepository:
    """Atomic reference ledger modeling restart-safe provider delivery semantics."""

    def __init__(
        self,
        *,
        retention_limit: int = 10_000,
        retention_age: timedelta = timedelta(days=90),
    ) -> None:
        if not 1 <= retention_limit <= 1_000_000:
            raise ValueError("retention_limit must be between 1 and 1000000")
        if not timedelta(days=1) <= retention_age <= timedelta(days=3650):
            raise ValueError("retention_age must be between 1 day and 10 years")
        self._lock = RLock()
        self._entries: dict[tuple[str, str], NotificationHistoryEntry] = {}
        self._retention_limit = retention_limit
        self._retention_age = retention_age

    @staticmethod
    def _key(event_id: str, provider_id: str) -> tuple[str, str]:
        if not event_id.strip() or not provider_id.strip():
            raise ValueError("event_id and provider_id are required")
        return event_id, provider_id.casefold()

    def _prune(self, at: datetime) -> None:
        cutoff = at.astimezone(UTC) - self._retention_age
        expired = [
            key for key, entry in self._entries.items() if entry.updated_at.astimezone(UTC) < cutoff
        ]
        for key in expired:
            del self._entries[key]
        excess = len(self._entries) - self._retention_limit
        if excess > 0:
            oldest = sorted(
                self._entries,
                key=lambda key: (self._entries[key].updated_at, key),
            )[:excess]
            for key in oldest:
                del self._entries[key]

    def claim(
        self,
        event: NewMailEvent,
        provider_id: str,
        *,
        at: datetime,
        lease_for: timedelta,
        matching_rule_ids: Sequence[str] = (),
    ) -> DeliveryClaim:
        _aware(at, "claim time")
        if not timedelta(seconds=1) <= lease_for <= timedelta(hours=24):
            raise ValueError("delivery lease must be between 1 second and 24 hours")
        key = self._key(event.event_id, provider_id)
        now = at.astimezone(UTC)
        with self._lock:
            current = self._entries.get(key)
            idempotency_key = delivery_idempotency_key(event.event_id, provider_id)
            if current is not None and current.state in {
                ProviderDeliveryState.DELIVERED,
                ProviderDeliveryState.SUPPRESSED,
                ProviderDeliveryState.DIGESTED,
            }:
                return DeliveryClaim(
                    DeliveryClaimState.ALREADY_DELIVERED,
                    idempotency_key,
                    current.attempts,
                )
            if (
                current is not None
                and current.state is ProviderDeliveryState.IN_PROGRESS
                and current.lease_until is not None
                and current.lease_until.astimezone(UTC) > now
            ):
                return DeliveryClaim(
                    DeliveryClaimState.IN_PROGRESS,
                    idempotency_key,
                    current.attempts,
                    current.lease_until,
                )
            if (
                current is not None
                and current.retry_at is not None
                and current.retry_at.astimezone(UTC) > now
            ):
                return DeliveryClaim(
                    DeliveryClaimState.RETRY_NOT_DUE,
                    idempotency_key,
                    current.attempts,
                    retry_at=current.retry_at,
                )
            lease_until = now + lease_for
            attempts = 1 if current is None else current.attempts + 1
            created = now if current is None else current.created_at
            entry = NotificationHistoryEntry(
                event,
                provider_id,
                ProviderDeliveryState.IN_PROGRESS,
                attempts,
                created,
                now,
                tuple(dict.fromkeys(matching_rule_ids)),
                lease_until,
            )
            self._entries[key] = entry
            self._prune(now)
            return DeliveryClaim(
                DeliveryClaimState.CLAIMED,
                idempotency_key,
                attempts,
                lease_until,
            )

    def record(
        self,
        event_id: str,
        provider_id: str,
        state: ProviderDeliveryState,
        *,
        at: datetime,
        retry_at: datetime | None = None,
        error: str | None = None,
        status_code: int | None = None,
    ) -> NotificationHistoryEntry:
        _aware(at, "delivery time")
        if retry_at is not None:
            _aware(retry_at, "retry_at")
        if state is ProviderDeliveryState.IN_PROGRESS:
            raise ValueError("record cannot create an in-progress transition; use claim")
        key = self._key(event_id, provider_id)
        now = at.astimezone(UTC)
        with self._lock:
            current = self._entries.get(key)
            if current is None:
                raise KeyError(f"delivery was not claimed: {event_id}/{provider_id}")
            safe_error = (
                redact_text(error, redact_emails=True)[:1_000] if error is not None else None
            )
            updated = replace(
                current,
                state=state,
                updated_at=now,
                lease_until=None,
                retry_at=retry_at,
                last_error=safe_error,
                status_code=status_code,
            )
            self._entries[key] = updated
            self._prune(now)
            return updated

    @staticmethod
    def _matches_text(entry: NotificationHistoryEntry, text: str) -> bool:
        event = entry.event
        sender = ""
        if event.sender is not None:
            sender = f"{event.sender.name or ''} {event.sender.email}"
        haystack = "\n".join(
            (
                event.event_id,
                event.message_reference or "",
                entry.provider_id,
                event.subject or "",
                sender,
                " ".join(entry.matching_rule_ids),
            )
        ).casefold()
        return text.casefold() in haystack

    def history(
        self,
        query: NotificationHistoryQuery | None = None,
    ) -> tuple[NotificationHistoryEntry, ...]:
        selected = query or NotificationHistoryQuery()
        with self._lock:
            values = list(self._entries.values())
        if selected.text:
            values = [entry for entry in values if self._matches_text(entry, selected.text)]
        if selected.provider_ids:
            values = [
                entry for entry in values if entry.provider_id.casefold() in selected.provider_ids
            ]
        if selected.states:
            values = [entry for entry in values if entry.state in selected.states]
        if selected.rule_ids:
            values = [
                entry
                for entry in values
                if {value.casefold() for value in entry.matching_rule_ids} & selected.rule_ids
            ]
        if selected.after is not None:
            values = [entry for entry in values if entry.updated_at >= selected.after]
        if selected.before is not None:
            values = [entry for entry in values if entry.updated_at < selected.before]
        values.sort(key=lambda entry: (entry.updated_at, entry.event.event_id), reverse=True)
        return tuple(values[selected.offset : selected.offset + selected.limit])

    def request_retry(
        self,
        event_id: str,
        provider_id: str,
        *,
        at: datetime,
    ) -> NotificationHistoryEntry:
        _aware(at, "retry request time")
        key = self._key(event_id, provider_id)
        with self._lock:
            current = self._entries.get(key)
            if current is None:
                raise KeyError(f"delivery does not exist: {event_id}/{provider_id}")
            if current.state not in {
                ProviderDeliveryState.FAILED,
                ProviderDeliveryState.DEAD_LETTER,
            }:
                raise ValueError("only failed or dead-letter deliveries can be retried")
            updated = replace(
                current,
                state=ProviderDeliveryState.FAILED,
                updated_at=at.astimezone(UTC),
                retry_at=at.astimezone(UTC),
                lease_until=None,
            )
            self._entries[key] = updated
            return updated

    def clear_before(self, before: datetime) -> int:
        _aware(before, "history retention boundary")
        with self._lock:
            keys = [key for key, value in self._entries.items() if value.updated_at < before]
            for key in keys:
                del self._entries[key]
            return len(keys)

    def provider_states(self, event_id: str) -> Mapping[str, ProviderDeliveryState]:
        with self._lock:
            return {
                entry.provider_id: entry.state
                for (stored_event_id, _provider), entry in self._entries.items()
                if stored_event_id == event_id
            }


__all__ = [
    "DeliveryClaim",
    "DeliveryClaimState",
    "HistoryDisclosurePolicy",
    "InMemoryNotificationHistoryRepository",
    "NotificationHistoryEntry",
    "NotificationHistoryQuery",
    "NotificationHistoryRepository",
    "ProviderDeliveryState",
    "delivery_idempotency_key",
    "history_payload",
]
