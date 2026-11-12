"""Runtime notification rules, quiet hours, digests, and provider routing.

The mailbox monitor persists provider-neutral events before this adapter runs.  This module
then evaluates one immutable monitoring profile, dispatches immediate routes, and stores
quiet-hour or digest routes in a separate durable queue.  Provider failures never alter the
mailbox checkpoint or polling cadence.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from gucmail.models import Address, Importance, NotificationEvent
from gucmail.notification_events import (
    EventDisclosurePolicy,
    MailEventType,
    NewMailEvent,
)
from gucmail.notification_policies import (
    DigestSchedule,
    MonitoringProfile,
    NotificationRouter,
    ProviderPolicy,
)
from gucmail.notification_rules import NotificationRuleEngine
from gucmail.security import sanitize_terminal
from gucmail.services.notifications import (
    DeliveryResult,
    DeliveryState,
    NotificationDispatcher,
    NotificationEventFilter,
    NotificationProvider,
    event_preference_result,
)
from gucmail.state import EventLedgerRecord, StateStore, iso

Clock = Callable[[], datetime]
ProfileLoader = Callable[[], MonitoringProfile]


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a time zone")
    return value.astimezone(UTC)


def _mail_event_type(value: str) -> MailEventType:
    aliases = {
        "created": MailEventType.NEW_MESSAGE,
        "new": MailEventType.NEW_MESSAGE,
        "moved": MailEventType.MOVED_MESSAGE,
        "changed": MailEventType.CHANGED_MESSAGE,
        "deleted": MailEventType.DELETED_MESSAGE,
    }
    try:
        return MailEventType(value.casefold())
    except ValueError:
        return aliases.get(value.casefold(), MailEventType.CHANGED_MESSAGE)


def monitoring_event(
    event: NotificationEvent,
    *,
    detected_at: datetime,
) -> NewMailEvent:
    """Convert the monitor's compact event into the canonical rule-engine event."""

    return NewMailEvent(
        event_id=event.event_id,
        event_type=_mail_event_type(event.event_type),
        occurred_at=_aware(event.occurred_at, label="notification event time"),
        detected_at=_aware(detected_at, label="notification detection time"),
        account_id=event.account_id,
        folder=event.folder,
        message_reference=event.message_reference,
        sender=event.sender,
        subject=event.subject,
        body_preview=event.preview,
        importance=event.importance,
        has_attachments=event.has_attachments,
    )


def _address(value: object) -> Address | None:
    if not isinstance(value, Mapping):
        return None
    email = value.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    name = value.get("name")
    mailbox_type = value.get("mailbox_type")
    return Address(
        email=email,
        name=name if isinstance(name, str) else None,
        mailbox_type=mailbox_type if isinstance(mailbox_type, str) else None,
    )


def event_from_ledger(record: EventLedgerRecord) -> NotificationEvent:
    """Rehydrate the fixed legacy event shape stored by the durable monitor outbox."""

    payload = record.payload
    importance_value = payload.get("importance", Importance.NORMAL.value)
    try:
        importance = Importance(str(importance_value))
    except ValueError:
        importance = Importance.NORMAL
    return NotificationEvent(
        event_id=record.event_id,
        event_type=record.event_type,
        occurred_at=record.occurred_at,
        account_id=str(payload.get("account_id") or record.account_key),
        folder=(str(payload["folder"]) if payload.get("folder") is not None else record.folder),
        message_reference=(
            str(payload["message_reference"])
            if payload.get("message_reference") is not None
            else record.item_reference
        ),
        sender=_address(payload.get("sender")),
        subject=str(payload["subject"]) if payload.get("subject") is not None else None,
        importance=importance,
        has_attachments=bool(payload.get("has_attachments", False)),
        preview=str(payload["preview"]) if payload.get("preview") is not None else None,
        open_hint=str(payload["open_hint"]) if payload.get("open_hint") is not None else None,
    )


def disclosed_event(
    event: NotificationEvent,
    policy: EventDisclosurePolicy,
) -> NotificationEvent:
    """Apply fail-closed profile privacy before a legacy provider sees an event."""

    permitted = policy.permitted_fields
    reference = event.message_reference if "message_reference" in permitted else None
    return NotificationEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        account_id=event.account_id if "account_id" in permitted else "configured-mailbox",
        folder=event.folder if "folder" in permitted else None,
        message_reference=reference,
        sender=event.sender if "sender" in permitted else None,
        subject=event.subject if "subject" in permitted else None,
        importance=event.importance if "importance" in permitted else Importance.NORMAL,
        has_attachments=event.has_attachments if "has_attachments" in permitted else False,
        preview=event.preview if "body_preview" in permitted else None,
        open_hint=event.open_hint if reference is not None else None,
    )


@dataclass(frozen=True, slots=True)
class RoutingQueueEntry:
    profile_id: str
    provider_id: str
    event: NotificationEvent
    mode: str
    schedule_id: str
    batch_key: str
    due_at: datetime
    attempts: int


class StateStoreRoutingQueue:
    """Persistent v6 routing queue for quiet-hour releases and digest batches."""

    def __init__(
        self,
        state: StateStore,
        *,
        maximum_attempts: int = 10,
        retry_base_seconds: float = 30.0,
        retry_max_seconds: float = 900.0,
    ) -> None:
        if not 1 <= maximum_attempts <= 100:
            raise ValueError("routing queue maximum_attempts must be between 1 and 100")
        if not 1 <= retry_base_seconds <= retry_max_seconds <= 86_400:
            raise ValueError("routing queue retry delays are invalid")
        self.state = state
        self.maximum_attempts = maximum_attempts
        self.retry_base_seconds = float(retry_base_seconds)
        self.retry_max_seconds = float(retry_max_seconds)

    def enqueue(
        self,
        *,
        profile_id: str,
        provider_id: str,
        event_id: str,
        mode: str,
        due_at: datetime,
        schedule_id: str = "",
        batch_key: str = "",
    ) -> bool:
        if mode not in {"quiet", "digest"}:
            raise ValueError("routing queue mode must be quiet or digest")
        due = iso(_aware(due_at, label="routing due time"))
        now = iso()
        with self.state.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notification_routing_queue(
                    account_key, profile_id, provider_id, event_id, mode,
                    schedule_id, batch_key, due_at, state, next_attempt_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    self.state.account_key,
                    profile_id.casefold(),
                    provider_id.casefold(),
                    event_id,
                    mode,
                    schedule_id.casefold(),
                    batch_key,
                    due,
                    due,
                    now,
                    now,
                ),
            )
        return cursor.rowcount == 1

    def due(
        self,
        *,
        profile_id: str,
        at: datetime,
        limit: int = 500,
    ) -> tuple[RoutingQueueEntry, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("routing queue limit must be between 1 and 10000")
        current = iso(_aware(at, label="routing queue read time"))
        self.state.initialize()
        with self.state.connection() as connection:
            rows = connection.execute(
                """
                SELECT queue.profile_id, queue.provider_id, queue.mode,
                       queue.schedule_id, queue.batch_key, queue.due_at,
                       queue.attempts, events.*
                FROM notification_routing_queue AS queue
                JOIN event_ledger AS events
                  ON events.account_key=queue.account_key
                 AND events.event_id=queue.event_id
                WHERE queue.account_key=? AND queue.profile_id=?
                  AND queue.state IN ('pending','retry')
                  AND COALESCE(queue.next_attempt_at, queue.due_at)<=?
                ORDER BY queue.due_at, queue.created_at, queue.event_id
                LIMIT ?
                """,
                (self.state.account_key, profile_id.casefold(), current, limit),
            ).fetchall()
        entries: list[RoutingQueueEntry] = []
        for row in rows:
            record = self.state.get_event(str(row["event_id"]))
            if record is None:
                continue
            entries.append(
                RoutingQueueEntry(
                    profile_id=str(row["profile_id"]),
                    provider_id=str(row["provider_id"]),
                    event=event_from_ledger(record),
                    mode=str(row["mode"]),
                    schedule_id=str(row["schedule_id"]),
                    batch_key=str(row["batch_key"]),
                    due_at=_aware(
                        datetime.fromisoformat(str(row["due_at"])),
                        label="stored routing due time",
                    ),
                    attempts=int(row["attempts"]),
                )
            )
        return tuple(entries)

    def record(
        self,
        entries: Sequence[RoutingQueueEntry],
        result: DeliveryResult,
        *,
        at: datetime,
    ) -> None:
        if not entries:
            return
        current = _aware(at, label="routing outcome time")
        deferred = result.state is DeliveryState.DEFERRED
        succeeded = result.state not in {DeliveryState.FAILED, DeliveryState.DEFERRED}
        with self.state.transaction(immediate=True) as connection:
            for entry in entries:
                attempts = entry.attempts + (0 if deferred else 1)
                if succeeded:
                    state = "delivered"
                    next_attempt = None
                    delivered = iso(current)
                    error = None
                elif deferred:
                    state = "retry"
                    next_attempt = iso(current + timedelta(seconds=30))
                    delivered = None
                    error = "Delivery is already in progress; retry is scheduled."
                elif attempts >= self.maximum_attempts:
                    state = "dead_letter"
                    next_attempt = None
                    delivered = None
                    error = sanitize_terminal(result.detail or "Provider failed", max_length=1_000)
                else:
                    state = "retry"
                    delay = min(
                        self.retry_base_seconds * (2 ** max(attempts - 1, 0)),
                        self.retry_max_seconds,
                    )
                    next_attempt = iso(current + timedelta(seconds=delay))
                    delivered = None
                    error = sanitize_terminal(result.detail or "Provider failed", max_length=1_000)
                connection.execute(
                    """
                    UPDATE notification_routing_queue
                    SET state=?, attempts=?, next_attempt_at=?, error_summary=?,
                        delivered_at=?, updated_at=?
                    WHERE account_key=? AND profile_id=? AND provider_id=?
                      AND event_id=? AND mode=? AND schedule_id=?
                      AND state IN ('pending', 'retry')
                    """,
                    (
                        state,
                        attempts,
                        next_attempt,
                        error,
                        delivered,
                        iso(current),
                        self.state.account_key,
                        entry.profile_id,
                        entry.provider_id,
                        entry.event.event_id,
                        entry.mode,
                        entry.schedule_id,
                    ),
                )


class _NamedProvider:
    def __init__(self, provider_id: str, provider: NotificationProvider) -> None:
        self.name = provider_id
        self._provider = provider

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        return replace(self._provider.notify(event), provider=self.name)


class ProfiledNotificationDispatcher:
    """Apply one durable monitoring profile before invoking provider plugins."""

    def __init__(
        self,
        profile: MonitoringProfile,
        providers: Mapping[str, NotificationProvider],
        *,
        state: StateStore,
        profile_loader: ProfileLoader | None = None,
        queue: StateStoreRoutingQueue | None = None,
        clock: Clock | None = None,
        event_allowed: NotificationEventFilter | None = None,
    ) -> None:
        self._configured_profile = profile
        self._profile_loader = profile_loader
        self._providers = {name.casefold(): provider for name, provider in providers.items()}
        self._state = state
        self._queue = queue or StateStoreRoutingQueue(state)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._event_allowed = event_allowed
        self._burst_minute: datetime | None = None
        self._burst_counts: dict[str, int] = {}

    @property
    def profile(self) -> MonitoringProfile:
        if self._profile_loader is None:
            return self._configured_profile
        loaded = self._profile_loader()
        self._configured_profile = loaded
        return loaded

    @staticmethod
    def _policies(profile: MonitoringProfile) -> dict[str, ProviderPolicy]:
        return {policy.provider_id.casefold(): policy for policy in profile.providers}

    def _provider_result(
        self,
        provider_id: str,
        event: NotificationEvent,
        policy: ProviderPolicy,
    ) -> DeliveryResult:
        preference = event_preference_result(event, self._event_allowed)
        if preference is not None:
            return preference
        provider = self._providers.get(provider_id.casefold())
        if provider is None:
            return DeliveryResult(
                event.event_id,
                provider_id,
                DeliveryState.FAILED,
                "The routed provider is not available in this watcher process.",
                attempts=0,
            )
        private_event = disclosed_event(event, policy.privacy)
        results = NotificationDispatcher(
            [_NamedProvider(provider_id, provider)],
            state=self._state,
            event_allowed=self._event_allowed,
        ).dispatch(private_event)
        return results[0]

    def _schedule(
        self,
        profile: MonitoringProfile,
        policy: ProviderPolicy,
    ) -> DigestSchedule | None:
        schedule_id = policy.digest_schedule_id
        if schedule_id is None:
            return next((item for item in profile.digest_schedules if item.enabled), None)
        return next(
            (
                item
                for item in profile.digest_schedules
                if item.schedule_id.casefold() == schedule_id.casefold() and item.enabled
            ),
            None,
        )

    def _burst_allowed(self, policy: ProviderPolicy, at: datetime) -> bool:
        minute = at.astimezone(UTC).replace(second=0, microsecond=0)
        if self._burst_minute != minute:
            self._burst_minute = minute
            self._burst_counts.clear()
        key = policy.provider_id.casefold()
        count = self._burst_counts.get(key, 0)
        if count >= policy.maximum_notifications_per_burst:
            return False
        self._burst_counts[key] = count + 1
        return True

    def dispatch(self, event: NotificationEvent) -> tuple[DeliveryResult, ...]:
        preference = event_preference_result(event, self._event_allowed)
        if preference is not None:
            return (preference,)
        now = _aware(self._clock(), label="notification routing time")
        profile = self.profile
        if not profile.enabled:
            return (
                DeliveryResult(
                    event.event_id,
                    "notification-profile",
                    DeliveryState.SKIPPED,
                    "The selected notification profile is disabled.",
                    attempts=0,
                ),
            )
        canonical = monitoring_event(event, detected_at=now)
        decision = NotificationRuleEngine(profile.rules).evaluate(canonical)
        plan = NotificationRouter().plan(canonical, decision, profile, at=now)
        policies = self._policies(profile)
        results: list[DeliveryResult] = []

        if plan.suppressed:
            return (
                DeliveryResult(
                    event.event_id,
                    "notification-rules",
                    DeliveryState.SKIPPED,
                    "The active notification profile suppressed this event.",
                    attempts=0,
                ),
            )

        for provider_id in plan.immediate_providers:
            policy = policies.get(provider_id.casefold())
            if policy is None:
                results.append(
                    DeliveryResult(
                        event.event_id,
                        provider_id,
                        DeliveryState.FAILED,
                        "The routing matrix references an unknown provider policy.",
                        attempts=0,
                    )
                )
                continue
            if self._burst_allowed(policy, now):
                results.append(self._provider_result(provider_id, event, policy))
                continue
            due_at = now + timedelta(minutes=1)
            self._queue.enqueue(
                profile_id=profile.profile_id,
                provider_id=provider_id,
                event_id=event.event_id,
                mode="quiet",
                due_at=due_at,
                batch_key=f"burst:{due_at.isoformat()}",
            )
            results.append(
                DeliveryResult(
                    event.event_id,
                    provider_id,
                    DeliveryState.SKIPPED,
                    "Queued until the next bounded burst window.",
                    attempts=0,
                )
            )

        for provider_id in plan.queued_providers:
            due_at = plan.release_at or now
            self._queue.enqueue(
                profile_id=profile.profile_id,
                provider_id=provider_id,
                event_id=event.event_id,
                mode="quiet",
                due_at=due_at,
                batch_key=plan.quiet_hours.window_id or "quiet-hours",
            )
            results.append(
                DeliveryResult(
                    event.event_id,
                    provider_id,
                    DeliveryState.SKIPPED,
                    f"Queued by quiet hours until {due_at.astimezone(UTC).isoformat()}.",
                    attempts=0,
                )
            )

        for provider_id in plan.digest_providers:
            policy = policies.get(provider_id.casefold())
            schedule = self._schedule(profile, policy) if policy is not None else None
            if policy is None or schedule is None:
                results.append(
                    DeliveryResult(
                        event.event_id,
                        provider_id,
                        DeliveryState.FAILED,
                        "Digest routing requires one enabled digest schedule.",
                        attempts=0,
                    )
                )
                continue
            window = schedule.window_for(now)
            self._queue.enqueue(
                profile_id=profile.profile_id,
                provider_id=provider_id,
                event_id=event.event_id,
                mode="digest",
                schedule_id=schedule.schedule_id,
                due_at=window.end,
                batch_key=window.window_id,
            )
            results.append(
                DeliveryResult(
                    event.event_id,
                    provider_id,
                    DeliveryState.SKIPPED,
                    f"Queued for digest {schedule.name!r}.",
                    attempts=0,
                )
            )

        if results:
            return tuple(results)
        return (
            DeliveryResult(
                event.event_id,
                "notification-rules",
                DeliveryState.SKIPPED,
                "No enabled rule routed this event to a provider.",
                attempts=0,
            ),
        )

    @staticmethod
    def _digest_event(entries: Sequence[RoutingQueueEntry]) -> NotificationEvent:
        ordered = sorted(entries, key=lambda item: (item.event.occurred_at, item.event.event_id))
        material = "\x00".join(item.event.event_id for item in ordered)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        subjects = [
            sanitize_terminal(item.event.subject, max_length=160)
            for item in ordered
            if item.event.subject
        ][:5]
        folders = {item.event.folder for item in ordered if item.event.folder}
        preview = "\n".join(f"• {subject}" for subject in subjects) or None
        return NotificationEvent(
            event_id=f"digest-{digest}",
            event_type="notification_digest",
            occurred_at=max(item.event.occurred_at for item in ordered),
            account_id=ordered[0].event.account_id,
            folder=next(iter(folders)) if len(folders) == 1 else "Multiple folders",
            subject=f"{len(ordered)} new mail notification(s)",
            importance=(
                Importance.HIGH
                if any(item.event.importance is Importance.HIGH for item in ordered)
                else Importance.NORMAL
            ),
            has_attachments=any(item.event.has_attachments for item in ordered),
            preview=preview,
            open_hint="gucmail notifications history",
        )

    def drain_due(self) -> tuple[DeliveryResult, ...]:
        """Deliver due quiet-hour events and complete digest batches without EWS access."""

        now = _aware(self._clock(), label="notification queue drain time")
        profile = self.profile
        if not profile.enabled:
            return ()
        policies = self._policies(profile)
        due_entries = self._queue.due(profile_id=profile.profile_id, at=now)
        results: list[DeliveryResult] = []
        entries: list[RoutingQueueEntry] = []
        # Filter each member before constructing a digest. A mixed digest must
        # not disclose a muted Calendar change inside an otherwise allowed mail batch.
        for entry in due_entries:
            preference = event_preference_result(entry.event, self._event_allowed)
            if preference is None:
                entries.append(entry)
            else:
                self._queue.record((entry,), preference, at=now)
                results.append(preference)

        for entry in (item for item in entries if item.mode == "quiet"):
            policy = policies.get(entry.provider_id.casefold())
            if policy is None:
                result = DeliveryResult(
                    entry.event.event_id,
                    entry.provider_id,
                    DeliveryState.FAILED,
                    "Queued route no longer has a provider policy.",
                    attempts=0,
                )
            else:
                result = self._provider_result(entry.provider_id, entry.event, policy)
            self._queue.record((entry,), result, at=now)
            results.append(result)

        grouped: dict[tuple[str, str, str], list[RoutingQueueEntry]] = defaultdict(list)
        for entry in entries:
            if entry.mode == "digest":
                grouped[(entry.provider_id, entry.schedule_id, entry.batch_key)].append(entry)
        for (provider_id, _schedule_id, _batch_key), batch in sorted(grouped.items()):
            current_batch: list[RoutingQueueEntry] = []
            for entry in batch:
                preference = event_preference_result(entry.event, self._event_allowed)
                if preference is None:
                    current_batch.append(entry)
                else:
                    self._queue.record((entry,), preference, at=now)
                    results.append(preference)
            if not current_batch:
                continue
            batch = current_batch
            policy = policies.get(provider_id.casefold())
            digest_event = self._digest_event(batch)
            if policy is None:
                result = DeliveryResult(
                    digest_event.event_id,
                    provider_id,
                    DeliveryState.FAILED,
                    "Digest route no longer has a provider policy.",
                    attempts=0,
                )
            else:
                result = self._provider_result(provider_id, digest_event, policy)
            self._queue.record(batch, result, at=now)
            results.append(result)
        return tuple(results)


__all__ = [
    "ProfiledNotificationDispatcher",
    "RoutingQueueEntry",
    "StateStoreRoutingQueue",
    "disclosed_event",
    "event_from_ledger",
    "monitoring_event",
]
