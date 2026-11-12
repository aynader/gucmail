"""Provider-neutral monitoring event outbox models.

The mailbox watcher and notification delivery are deliberately separate concerns.  A
watcher first records an event, then advances its mailbox checkpoint.  Providers can
subsequently succeed, retry, or enter a bounded dead-letter state without forcing an
extra Exchange poll.

``InMemoryEventOutbox`` is the reference implementation used by unit tests and by the
current single-process watcher.  Its protocol is intentionally persistence-friendly so
the state database can implement the same contract without changing monitoring logic.
"""

from __future__ import annotations

import json
import random
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

from gucmail.models import Address, Importance, NotificationEvent
from gucmail.security import redact_text
from gucmail.services.notifications import DeliveryResult, DeliveryState

if TYPE_CHECKING:
    from gucmail.state import StateStore


_CONTENDED_RETRY_SECONDS = 30


class OutboxState(StrEnum):
    PENDING = "pending"
    RETRY_SCHEDULED = "retry_scheduled"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider: str
    state: DeliveryState
    attempts: int
    status_code: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    event: NotificationEvent
    folder: str
    state: OutboxState
    created_at: datetime
    updated_at: datetime
    delivery_cycles: int = 0
    next_attempt_at: datetime | None = None
    provider_attempts: tuple[ProviderAttempt, ...] = ()
    lease_owner: str | None = None
    lease_until: datetime | None = None


class EventOutbox(Protocol):
    """Minimal contract required by :class:`MailboxMonitor`."""

    def enqueue(
        self,
        event: NotificationEvent,
        *,
        folder: str,
        detected_at: datetime,
    ) -> bool: ...

    def due(
        self,
        *,
        folder: str | None,
        now: datetime,
        limit: int,
    ) -> tuple[OutboxEntry, ...]: ...

    def claim_due(
        self,
        *,
        folder: str | None,
        now: datetime,
        limit: int,
        lease_owner: str,
        lease_for: timedelta,
    ) -> tuple[OutboxEntry, ...]: ...

    def record(
        self,
        event_id: str,
        deliveries: Sequence[DeliveryResult],
        *,
        now: datetime,
        lease_owner: str | None = None,
    ) -> OutboxEntry: ...

    def next_due_at(self, *, folder: str | None = None) -> datetime | None: ...


class InMemoryEventOutbox:
    """Thread-safe bounded-retry reference outbox.

    Completed entries are retained for inspection during the process lifetime.  The
    persistent implementation may apply its own history-retention policy while keeping
    the same externally visible state transitions.
    """

    def __init__(
        self,
        *,
        retry_delay_seconds: float = 0.0,
        maximum_delivery_cycles: int = 10,
    ) -> None:
        if not 0 <= retry_delay_seconds <= 86_400:
            raise ValueError("outbox retry delay must be between 0 and 86400 seconds")
        if not 1 <= maximum_delivery_cycles <= 100:
            raise ValueError("outbox delivery cycles must be between 1 and 100")
        self._retry_delay = float(retry_delay_seconds)
        self._maximum_cycles = maximum_delivery_cycles
        self._entries: dict[str, OutboxEntry] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def enqueue(
        self,
        event: NotificationEvent,
        *,
        folder: str,
        detected_at: datetime,
    ) -> bool:
        now = self._aware(detected_at)
        with self._lock:
            if event.event_id in self._entries:
                return False
            self._entries[event.event_id] = OutboxEntry(
                event=event,
                folder=folder,
                state=OutboxState.PENDING,
                created_at=now,
                updated_at=now,
                next_attempt_at=now,
            )
        return True

    def due(
        self,
        *,
        folder: str | None,
        now: datetime,
        limit: int,
    ) -> tuple[OutboxEntry, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("outbox due limit must be between 1 and 10000")
        current = self._aware(now)
        with self._lock:
            candidates = [
                entry
                for entry in self._entries.values()
                if (folder is None or entry.folder.casefold() == folder.casefold())
                and entry.state in {OutboxState.PENDING, OutboxState.RETRY_SCHEDULED}
                and (entry.next_attempt_at is None or entry.next_attempt_at <= current)
                and (entry.lease_until is None or entry.lease_until <= current)
            ]
        candidates.sort(key=lambda entry: (entry.created_at, entry.event.event_id))
        return tuple(candidates[:limit])

    def claim_due(
        self,
        *,
        folder: str | None,
        now: datetime,
        limit: int,
        lease_owner: str,
        lease_for: timedelta,
    ) -> tuple[OutboxEntry, ...]:
        owner = lease_owner.strip()
        if not owner:
            raise ValueError("outbox lease owner may not be empty")
        if not timedelta(seconds=1) <= lease_for <= timedelta(hours=24):
            raise ValueError("outbox lease must be between 1 second and 24 hours")
        current = self._aware(now)
        with self._lock:
            due = self.due(folder=folder, now=current, limit=limit)
            claimed: list[OutboxEntry] = []
            for entry in due:
                updated = replace(
                    entry,
                    lease_owner=owner,
                    lease_until=current + lease_for,
                    updated_at=current,
                )
                self._entries[entry.event.event_id] = updated
                claimed.append(updated)
            return tuple(claimed)

    def record(
        self,
        event_id: str,
        deliveries: Sequence[DeliveryResult],
        *,
        now: datetime,
        lease_owner: str | None = None,
    ) -> OutboxEntry:
        current = self._aware(now)
        attempts = tuple(
            ProviderAttempt(
                provider=result.provider,
                state=result.state,
                attempts=max(0, result.attempts),
                status_code=result.status_code,
                detail=(
                    redact_text(result.detail, redact_emails=True)[:1000]
                    if result.detail is not None
                    else None
                ),
            )
            for result in deliveries
        )
        failed = any(result.state is DeliveryState.FAILED for result in deliveries)
        deferred = any(result.state is DeliveryState.DEFERRED for result in deliveries)
        with self._lock:
            entry = self._entries.get(event_id)
            if entry is None:
                raise KeyError(f"monitoring outbox event is missing: {event_id}")
            if entry.lease_owner != lease_owner:
                raise RuntimeError("monitoring outbox lease is no longer owned by this worker")
            if entry.state not in {OutboxState.PENDING, OutboxState.RETRY_SCHEDULED}:
                raise RuntimeError("monitoring outbox event is already terminal")
            # Another worker's active provider claim is not a delivery attempt and
            # must not exhaust the retry budget or be mistaken for completion.
            cycles = entry.delivery_cycles + (1 if failed or not deferred else 0)
            if failed and cycles >= self._maximum_cycles:
                state = OutboxState.DEAD_LETTER
                next_attempt = None
            elif failed:
                state = OutboxState.RETRY_SCHEDULED
                next_attempt = current + timedelta(seconds=self._retry_delay)
            elif deferred:
                state = OutboxState.RETRY_SCHEDULED
                next_attempt = current + timedelta(seconds=_CONTENDED_RETRY_SECONDS)
            else:
                state = OutboxState.DELIVERED
                next_attempt = None
            updated = replace(
                entry,
                state=state,
                updated_at=current,
                delivery_cycles=cycles,
                next_attempt_at=next_attempt,
                provider_attempts=attempts,
                lease_owner=None,
                lease_until=None,
            )
            self._entries[event_id] = updated
            return updated

    def next_due_at(self, *, folder: str | None = None) -> datetime | None:
        with self._lock:
            values = [
                entry.next_attempt_at or entry.created_at
                for entry in self._entries.values()
                if (folder is None or entry.folder.casefold() == folder.casefold())
                and entry.state in {OutboxState.PENDING, OutboxState.RETRY_SCHEDULED}
            ]
        return min(values) if values else None

    def entries(self) -> tuple[OutboxEntry, ...]:
        with self._lock:
            values = tuple(self._entries.values())
        return tuple(sorted(values, key=lambda entry: (entry.created_at, entry.event.event_id)))


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a time zone")
    return value.astimezone(UTC)


def _event_payload(event: NotificationEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "occurred_at": _aware(event.occurred_at, label="event time").isoformat(),
        "account_id": event.account_id,
        "folder": event.folder,
        "message_reference": event.message_reference,
        "sender": (
            {
                "email": event.sender.email,
                "name": event.sender.name,
                "mailbox_type": event.sender.mailbox_type,
            }
            if event.sender is not None
            else None
        ),
        "subject": event.subject,
        "importance": event.importance.value,
        "has_attachments": event.has_attachments,
        "preview": event.preview,
        "open_hint": event.open_hint,
    }


def _event_from_payload(value: str) -> NotificationEvent:
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise TypeError("event payload is not an object")
        sender_value = payload.get("sender")
        sender = None
        if sender_value is not None:
            if not isinstance(sender_value, dict):
                raise TypeError("event sender is not an object")
            sender = Address(
                email=str(sender_value["email"]),
                name=(str(sender_value["name"]) if sender_value.get("name") is not None else None),
                mailbox_type=(
                    str(sender_value["mailbox_type"])
                    if sender_value.get("mailbox_type") is not None
                    else None
                ),
            )
        occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
        return NotificationEvent(
            event_id=str(payload["event_id"]),
            event_type=str(payload["event_type"]),
            occurred_at=_aware(occurred_at, label="stored event time"),
            account_id=str(payload["account_id"]),
            folder=(str(payload["folder"]) if payload.get("folder") is not None else None),
            message_reference=(
                str(payload["message_reference"])
                if payload.get("message_reference") is not None
                else None
            ),
            sender=sender,
            subject=(str(payload["subject"]) if payload.get("subject") is not None else None),
            importance=Importance(str(payload.get("importance", Importance.NORMAL.value))),
            has_attachments=bool(payload.get("has_attachments", False)),
            preview=(str(payload["preview"]) if payload.get("preview") is not None else None),
            open_hint=(str(payload["open_hint"]) if payload.get("open_hint") is not None else None),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("notification outbox contains an invalid event payload") from exc


class StateStoreEventOutbox:
    """Durable SQLite outbox backed by the v5 :class:`StateStore` schema.

    Enqueue happens before a mailbox checkpoint advances.  Consequently a crash may
    cause a harmless re-detection, but can never strand a detected event only in
    process memory.  Claims make multiple delivery workers fail closed rather than
    sending the same event concurrently.
    """

    def __init__(
        self,
        state: StateStore,
        *,
        retry_base_seconds: float = 30.0,
        retry_max_seconds: float = 900.0,
        retry_jitter_fraction: float = 0.2,
        maximum_delivery_cycles: int = 10,
        random_source: Any = random.random,
    ) -> None:
        if not 0 <= retry_base_seconds <= retry_max_seconds <= 86_400:
            raise ValueError("outbox retry delays are invalid")
        if not 0 <= retry_jitter_fraction <= 1:
            raise ValueError("outbox retry jitter must be between 0 and 1")
        if not 1 <= maximum_delivery_cycles <= 100:
            raise ValueError("outbox delivery cycles must be between 1 and 100")
        self._state = state
        self._retry_base = float(retry_base_seconds)
        self._retry_max = float(retry_max_seconds)
        self._jitter = float(retry_jitter_fraction)
        self._maximum_cycles = maximum_delivery_cycles
        self._random = random_source

    @property
    def durable(self) -> bool:
        return True

    @property
    def account_key(self) -> str:
        return self._state.account_key

    @property
    def state_store(self) -> StateStore:
        """Return the backing store so monitor commits can share one transaction."""

        return self._state

    def enqueue(
        self,
        event: NotificationEvent,
        *,
        folder: str,
        detected_at: datetime,
    ) -> bool:
        detected = _aware(detected_at, label="event detection time")
        occurred = _aware(event.occurred_at, label="event time")
        return self._state.enqueue_notification_event(
            event_id=event.event_id,
            event_type=event.event_type,
            occurred_at=occurred,
            detected_at=detected,
            payload=_event_payload(event),
            folder=folder,
            item_reference=event.message_reference,
            dedupe_key=event.event_id,
        )

    @staticmethod
    def _entry(row: Any, attempts: Sequence[ProviderAttempt] = ()) -> OutboxEntry:
        def optional_time(value: object) -> datetime | None:
            if value is None:
                return None
            return _aware(datetime.fromisoformat(str(value)), label="stored outbox time")

        return OutboxEntry(
            event=_event_from_payload(str(row["payload_json"])),
            folder=str(row["folder"] or ""),
            state=OutboxState(str(row["state"])),
            created_at=cast(datetime, optional_time(row["created_at"])),
            updated_at=cast(datetime, optional_time(row["updated_at"])),
            delivery_cycles=int(row["delivery_cycles"]),
            next_attempt_at=optional_time(row["next_attempt_at"]),
            provider_attempts=tuple(attempts),
            lease_owner=(str(row["lease_owner"]) if row["lease_owner"] is not None else None),
            lease_until=optional_time(row["lease_until"]),
        )

    def due(
        self,
        *,
        folder: str | None,
        now: datetime,
        limit: int,
    ) -> tuple[OutboxEntry, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("outbox due limit must be between 1 and 10000")
        current = _aware(now, label="outbox due time").isoformat()
        self._state.initialize()
        with self._state.connection() as connection:
            if folder is None:
                rows = connection.execute(
                    """
                    SELECT o.*, e.payload_json
                    FROM notification_outbox AS o
                    JOIN event_ledger AS e
                      ON e.account_key=o.account_key AND e.event_id=o.event_id
                    WHERE o.account_key=?
                      AND o.state IN ('pending', 'retry_scheduled')
                      AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?)
                      AND (o.lease_until IS NULL OR o.lease_until<=?)
                    ORDER BY o.created_at, o.event_id
                    LIMIT ?
                    """,
                    (self.account_key, current, current, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT o.*, e.payload_json
                    FROM notification_outbox AS o
                    JOIN event_ledger AS e
                      ON e.account_key=o.account_key AND e.event_id=o.event_id
                    WHERE o.account_key=?
                      AND o.state IN ('pending', 'retry_scheduled')
                      AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?)
                      AND (o.lease_until IS NULL OR o.lease_until<=?)
                      AND lower(o.folder)=lower(?)
                    ORDER BY o.created_at, o.event_id
                    LIMIT ?
                    """,
                    (self.account_key, current, current, folder, limit),
                ).fetchall()
        return tuple(self._entry(row) for row in rows)

    def claim_due(
        self,
        *,
        folder: str | None,
        now: datetime,
        limit: int,
        lease_owner: str,
        lease_for: timedelta,
    ) -> tuple[OutboxEntry, ...]:
        owner = lease_owner.strip()
        if not owner:
            raise ValueError("outbox lease owner may not be empty")
        if not timedelta(seconds=1) <= lease_for <= timedelta(hours=24):
            raise ValueError("outbox lease must be between 1 second and 24 hours")
        current = _aware(now, label="outbox claim time")
        lease_seconds = max(5, int(lease_for.total_seconds()))
        records = self._state.claim_notification_outbox(
            worker_id=owner,
            now=current,
            lease_seconds=lease_seconds,
            folder=folder,
            limit=limit,
        )
        entries: list[OutboxEntry] = []
        for record in records:
            payload_json = json.dumps(
                record.event.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            entries.append(
                OutboxEntry(
                    event=_event_from_payload(payload_json),
                    folder=record.event.folder or "",
                    state=OutboxState(record.state),
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    delivery_cycles=record.delivery_cycles,
                    next_attempt_at=record.next_attempt_at,
                    lease_owner=record.lease_owner,
                    lease_until=record.lease_until,
                )
            )
        return tuple(entries)

    def _retry_delay(self, delivery_cycles: int) -> float:
        base = min(
            self._retry_base * (2.0 ** max(delivery_cycles - 1, 0)),
            self._retry_max,
        )
        fraction = max(0.0, min(float(self._random()), 1.0))
        return float(min(base * (1.0 + self._jitter * fraction), self._retry_max))

    def record(
        self,
        event_id: str,
        deliveries: Sequence[DeliveryResult],
        *,
        now: datetime,
        lease_owner: str | None = None,
    ) -> OutboxEntry:
        current = _aware(now, label="outbox result time")
        safe_results = tuple(deliveries)
        failed = tuple(result for result in safe_results if result.state is DeliveryState.FAILED)
        deferred = any(result.state is DeliveryState.DEFERRED for result in safe_results)
        with self._state.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT o.*, e.payload_json
                FROM notification_outbox AS o
                JOIN event_ledger AS e
                  ON e.account_key=o.account_key AND e.event_id=o.event_id
                WHERE o.account_key=? AND o.event_id=?
                """,
                (self.account_key, event_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"monitoring outbox event is missing: {event_id}")
            existing_owner = row["lease_owner"]
            if existing_owner != lease_owner:
                raise RuntimeError("monitoring outbox lease is no longer owned by this worker")
            if row["state"] not in {OutboxState.PENDING.value, OutboxState.RETRY_SCHEDULED.value}:
                raise RuntimeError("monitoring outbox event is already terminal")
            cycles = int(row["delivery_cycles"]) + (1 if failed or not deferred else 0)
            if failed and cycles >= self._maximum_cycles:
                next_state = OutboxState.DEAD_LETTER
                next_attempt = None
            elif failed:
                next_state = OutboxState.RETRY_SCHEDULED
                next_attempt = current + timedelta(seconds=self._retry_delay(cycles))
            elif deferred:
                next_state = OutboxState.RETRY_SCHEDULED
                next_attempt = current + timedelta(seconds=_CONTENDED_RETRY_SECONDS)
            else:
                next_state = OutboxState.DELIVERED
                next_attempt = None
            for result in safe_results:
                detail = (
                    redact_text(result.detail, redact_emails=True)[:1_000]
                    if result.detail is not None
                    else None
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_provider_attempts(
                        account_key, event_id, provider_id, attempt_no, state,
                        status_code, error_summary, retry_at, attempted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.account_key,
                        event_id,
                        result.provider,
                        cycles,
                        result.state.value,
                        result.status_code,
                        detail,
                        next_attempt.isoformat()
                        if result.state in {DeliveryState.FAILED, DeliveryState.DEFERRED}
                        and next_attempt is not None
                        else None,
                        current.isoformat(),
                    ),
                )
                if next_state is OutboxState.DEAD_LETTER and result in failed:
                    connection.execute(
                        """
                        INSERT INTO notification_dead_letters(
                            account_key, event_id, provider_id, reason, failed_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(account_key, event_id, provider_id) DO UPDATE SET
                            reason=excluded.reason,
                            failed_at=excluded.failed_at,
                            resolved_at=NULL,
                            resolution=NULL
                        """,
                        (
                            self.account_key,
                            event_id,
                            result.provider,
                            detail or "Notification provider exhausted its retry budget.",
                            current.isoformat(),
                        ),
                    )
            connection.execute(
                """
                UPDATE notification_outbox
                SET state=?, delivery_cycles=?, next_attempt_at=?, lease_owner=NULL,
                    lease_until=NULL, updated_at=?
                WHERE account_key=? AND event_id=?
                """,
                (
                    next_state.value,
                    cycles,
                    next_attempt.isoformat() if next_attempt is not None else None,
                    current.isoformat(),
                    self.account_key,
                    event_id,
                ),
            )
            updated = connection.execute(
                """
                SELECT o.*, e.payload_json
                FROM notification_outbox AS o
                JOIN event_ledger AS e
                  ON e.account_key=o.account_key AND e.event_id=o.event_id
                WHERE o.account_key=? AND o.event_id=?
                """,
                (self.account_key, event_id),
            ).fetchone()
        assert updated is not None
        attempts = tuple(
            ProviderAttempt(
                result.provider,
                result.state,
                max(0, result.attempts),
                result.status_code,
                (
                    redact_text(result.detail, redact_emails=True)[:1_000]
                    if result.detail is not None
                    else None
                ),
            )
            for result in safe_results
        )
        return self._entry(updated, attempts)

    def next_due_at(self, *, folder: str | None = None) -> datetime | None:
        self._state.initialize()
        with self._state.connection() as connection:
            if folder is None:
                row = connection.execute(
                    """
                    SELECT MIN(COALESCE(next_attempt_at, created_at))
                    FROM notification_outbox
                    WHERE account_key=? AND state IN ('pending', 'retry_scheduled')
                    """,
                    (self.account_key,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT MIN(COALESCE(next_attempt_at, created_at))
                    FROM notification_outbox
                    WHERE account_key=? AND state IN ('pending', 'retry_scheduled')
                      AND lower(folder)=lower(?)
                    """,
                    (self.account_key, folder),
                ).fetchone()
        if row is None or row[0] is None:
            return None
        return _aware(datetime.fromisoformat(str(row[0])), label="stored outbox due time")

    def entries(self, *, limit: int = 1_000) -> tuple[OutboxEntry, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("outbox entry limit must be between 1 and 10000")
        self._state.initialize()
        with self._state.connection() as connection:
            rows = connection.execute(
                """
                SELECT o.*, e.payload_json
                FROM notification_outbox AS o
                JOIN event_ledger AS e
                  ON e.account_key=o.account_key AND e.event_id=o.event_id
                WHERE o.account_key=? ORDER BY o.created_at, o.event_id LIMIT ?
                """,
                (self.account_key, limit),
            ).fetchall()
        return tuple(self._entry(row) for row in rows)


__all__ = [
    "EventOutbox",
    "InMemoryEventOutbox",
    "OutboxEntry",
    "OutboxState",
    "ProviderAttempt",
    "StateStoreEventOutbox",
]
