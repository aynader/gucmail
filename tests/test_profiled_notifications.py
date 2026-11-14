from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from gucmail.models import Address, Importance, NotificationEvent
from gucmail.notification_events import EventDisclosurePolicy, PrivacyMode
from gucmail.notification_policies import (
    DigestCadence,
    DigestSchedule,
    MonitoringProfile,
    ProviderPolicy,
    QuietHoursBehavior,
    QuietHoursSchedule,
    QuietHoursWindow,
)
from gucmail.notification_rules import (
    BooleanCondition,
    BooleanField,
    NotificationRule,
    RuleAction,
    RuleActionKind,
)
from gucmail.services.monitoring import MailboxMonitor
from gucmail.services.notifications import DeliveryResult, DeliveryState
from gucmail.services.profiled_notifications import (
    ProfiledNotificationDispatcher,
    StateStoreRoutingQueue,
    disclosed_event,
)
from gucmail.state import MIGRATIONS, StateStore

NOW = datetime(2026, 8, 13, 12, 15, tzinfo=UTC)


def _event(event_id: str = "mail-profiled-001") -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        event_type="new_message",
        occurred_at=NOW,
        account_id="student@example.test",
        folder="Inbox",
        message_reference=f"m-{event_id}",
        sender=Address("advisor@example.test", "Academic Advisor"),
        subject="Synthetic timetable update",
        importance=Importance.HIGH,
        has_attachments=True,
        preview="Synthetic preview that must follow the configured privacy policy.",
        open_hint=f"gucmail messages show --reference m-{event_id}",
    )


def _rule(kind: RuleActionKind = RuleActionKind.ROUTE_IMMEDIATE) -> NotificationRule:
    return NotificationRule(
        "all-new-mail",
        "All new mail",
        BooleanCondition(BooleanField.HAS_ATTACHMENTS, True),
        (RuleAction(kind, "capture"),),
        enabled=True,
    )


def _profile(
    *,
    rule_kind: RuleActionKind = RuleActionKind.ROUTE_IMMEDIATE,
    privacy: PrivacyMode = PrivacyMode.PRIVATE,
    quiet_hours: QuietHoursSchedule | None = None,
    digest_schedules: tuple[DigestSchedule, ...] = (),
) -> MonitoringProfile:
    digest_id = digest_schedules[0].schedule_id if digest_schedules else None
    return MonitoringProfile(
        "study",
        "Study mailbox",
        ("Inbox",),
        rules=(_rule(rule_kind),),
        providers=(
            ProviderPolicy(
                "capture",
                enabled=True,
                privacy=EventDisclosurePolicy(mode=privacy, redact_addresses=False),
                digest_schedule_id=digest_id,
            ),
        ),
        quiet_hours=quiet_hours or QuietHoursSchedule(),
        digest_schedules=digest_schedules,
        enabled=True,
    )


def _persist_event(state: StateStore, event: NotificationEvent) -> None:
    assert event.sender is not None
    assert state.enqueue_notification_event(
        event_id=event.event_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        detected_at=NOW,
        payload={
            "event_id": event.event_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at.isoformat(),
            "account_id": event.account_id,
            "folder": event.folder,
            "message_reference": event.message_reference,
            "sender": {"email": event.sender.email, "name": event.sender.name},
            "subject": event.subject,
            "importance": event.importance.value,
            "has_attachments": event.has_attachments,
            "preview": event.preview,
            "open_hint": event.open_hint,
        },
        folder=event.folder,
        item_reference=event.message_reference,
    )


class CaptureProvider:
    name = "capture"

    def __init__(self, outcomes: list[DeliveryState] | None = None) -> None:
        self.events: list[NotificationEvent] = []
        self.outcomes = outcomes or []

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        self.events.append(event)
        state = self.outcomes.pop(0) if self.outcomes else DeliveryState.DELIVERED
        detail = "synthetic provider failure" if state is DeliveryState.FAILED else None
        return DeliveryResult(event.event_id, self.name, state, detail)


def _queue_rows(state: StateStore) -> list[dict[str, object]]:
    with state.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM notification_routing_queue ORDER BY event_id"
        ).fetchall()
    return [dict(row) for row in rows]


def test_contended_routing_retry_preserves_budget_and_cannot_undo_delivery(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite3", account_key="student@example.test")
    event = _event()
    _persist_event(state, event)
    queue = StateStoreRoutingQueue(state, maximum_attempts=1)
    assert queue.enqueue(
        profile_id="study",
        provider_id="capture",
        event_id=event.event_id,
        mode="quiet",
        due_at=NOW,
    )
    entry = queue.due(profile_id="study", at=NOW)[0]
    deferred = DeliveryResult(event.event_id, "capture", DeliveryState.DEFERRED, attempts=0)
    queue.record((entry,), deferred, at=NOW)
    row = _queue_rows(state)[0]
    assert row["state"] == "retry"
    assert row["attempts"] == 0
    assert not queue.due(profile_id="study", at=NOW)
    assert queue.due(profile_id="study", at=NOW + timedelta(seconds=30))

    delivered = DeliveryResult(event.event_id, "capture", DeliveryState.DELIVERED)
    queue.record((entry,), delivered, at=NOW + timedelta(seconds=1))
    # A second drain can finish after the first. Its stale contention result
    # must not revert an already-delivered route back to retry.
    queue.record((entry,), deferred, at=NOW + timedelta(seconds=2))
    row = _queue_rows(state)[0]
    assert row["state"] == "delivered"
    assert row["attempts"] == 1


def test_schema_v6_migrates_existing_state_without_losing_rows(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    # Build a real predecessor schema. Removing migration 6 from a current
    # database leaves migration 7 applied and cannot model a version-5 upgrade.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 6):
            connection.executescript(MIGRATIONS[version])
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)", (version, NOW.isoformat())
            )
        connection.execute(
            "INSERT INTO saved_searches VALUES (?, ?, ?, ?)",
            ("Keep me", '{"query": "synthetic"}', NOW.isoformat(), NOW.isoformat()),
        )

    migrated = StateStore(path)
    migrated.initialize()

    assert migrated.get_search("keep me") == {"query": "synthetic"}
    with migrated.connection() as connection:
        versions = {
            int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")
        }
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(notification_routing_queue)")
        }
    assert 6 in versions
    assert {"profile_id", "provider_id", "mode", "due_at", "state"} <= columns


def test_disclosure_policy_is_fail_closed_per_provider() -> None:
    event = _event()

    private = disclosed_event(event, EventDisclosurePolicy(mode=PrivacyMode.PRIVATE))
    assert private.account_id == "configured-mailbox"
    assert private.folder is None
    assert private.message_reference is None
    assert private.sender is None
    assert private.subject is None
    assert private.preview is None
    assert private.open_hint is None

    subject = disclosed_event(
        event,
        EventDisclosurePolicy(
            mode=PrivacyMode.SENDER_AND_SUBJECT,
            redact_addresses=False,
        ),
    )
    assert subject.sender == event.sender
    assert subject.subject == event.subject
    assert subject.preview is None
    assert subject.account_id == "configured-mailbox"


def test_immediate_route_evaluates_rules_and_applies_privacy(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite3", account_key="student@example.test")
    event = _event()
    _persist_event(state, event)
    provider = CaptureProvider()
    dispatcher = ProfiledNotificationDispatcher(
        _profile(privacy=PrivacyMode.SENDER_AND_SUBJECT),
        {"capture": provider},
        state=state,
        clock=lambda: NOW,
    )

    result = dispatcher.dispatch(event)

    assert result[0].state is DeliveryState.DELIVERED
    assert len(provider.events) == 1
    delivered = provider.events[0]
    assert delivered.sender == event.sender
    assert delivered.subject == event.subject
    assert delivered.preview is None
    assert delivered.account_id == "configured-mailbox"
    assert _queue_rows(state) == []


def test_disabled_profile_fails_closed_without_dispatching_or_draining(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    event = _event("mail-disabled")
    _persist_event(state, event)
    queue = StateStoreRoutingQueue(state)
    queue.enqueue(
        profile_id="study",
        provider_id="capture",
        event_id=event.event_id,
        mode="quiet",
        due_at=NOW,
    )
    provider = CaptureProvider()
    dispatcher = ProfiledNotificationDispatcher(
        replace(_profile(), enabled=False),
        {"capture": provider},
        state=state,
        queue=queue,
        clock=lambda: NOW,
    )

    result = dispatcher.dispatch(event)

    assert result[0].state is DeliveryState.SKIPPED
    assert "disabled" in (result[0].detail or "").casefold()
    assert dispatcher.drain_due() == ()
    assert provider.events == []
    assert _queue_rows(state)[0]["state"] == "pending"


def test_quiet_hours_are_durable_and_release_without_mailbox_access(tmp_path: Path) -> None:
    current = [NOW]
    quiet = QuietHoursSchedule(
        (
            QuietHoursWindow(
                "lunch",
                time(12),
                time(13),
                "UTC",
                behavior=QuietHoursBehavior.QUEUE,
            ),
        )
    )
    state = StateStore(tmp_path / "state.sqlite3")
    event = _event("mail-quiet")
    _persist_event(state, event)
    provider = CaptureProvider()
    dispatcher = ProfiledNotificationDispatcher(
        _profile(quiet_hours=quiet),
        {"capture": provider},
        state=state,
        clock=lambda: current[0],
    )

    queued = dispatcher.dispatch(event)
    assert queued[0].state is DeliveryState.SKIPPED
    assert provider.events == []
    rows = _queue_rows(state)
    assert rows[0]["mode"] == "quiet"
    assert rows[0]["state"] == "pending"

    current[0] = datetime(2026, 8, 13, 13, 1, tzinfo=UTC)
    released = dispatcher.drain_due()
    assert released[0].state is DeliveryState.DELIVERED
    assert [item.event_id for item in provider.events] == [event.event_id]
    assert _queue_rows(state)[0]["state"] == "delivered"
    assert dispatcher.drain_due() == ()


def test_digest_batches_due_events_exactly_once(tmp_path: Path) -> None:
    current = [NOW]
    schedule = DigestSchedule(
        "hourly",
        "Hourly digest",
        DigestCadence.HOURLY,
        "UTC",
    )
    state = StateStore(tmp_path / "state.sqlite3")
    provider = CaptureProvider()
    dispatcher = ProfiledNotificationDispatcher(
        _profile(
            rule_kind=RuleActionKind.ROUTE_DIGEST,
            privacy=PrivacyMode.FULL,
            digest_schedules=(schedule,),
        ),
        {"capture": provider},
        state=state,
        clock=lambda: current[0],
    )
    events = (_event("mail-digest-1"), _event("mail-digest-2"))
    for event in events:
        _persist_event(state, event)
        assert dispatcher.dispatch(event)[0].state is DeliveryState.SKIPPED

    current[0] = datetime(2026, 8, 13, 13, 1, tzinfo=UTC)
    results = dispatcher.drain_due()

    assert len(results) == 1
    assert results[0].state is DeliveryState.DELIVERED
    assert len(provider.events) == 1
    digest = provider.events[0]
    assert digest.event_type == "notification_digest"
    assert digest.subject == "2 new messages"
    assert "Synthetic timetable update" in (digest.preview or "")
    assert all(row["state"] == "delivered" for row in _queue_rows(state))
    assert dispatcher.drain_due() == ()


def test_deferred_provider_failure_uses_bounded_retry_state(tmp_path: Path) -> None:
    current = [NOW]
    state = StateStore(tmp_path / "state.sqlite3")
    event = _event("mail-retry")
    _persist_event(state, event)
    queue = StateStoreRoutingQueue(
        state,
        maximum_attempts=3,
        retry_base_seconds=30,
        retry_max_seconds=60,
    )
    queue.enqueue(
        profile_id="study",
        provider_id="capture",
        event_id=event.event_id,
        mode="quiet",
        due_at=NOW,
    )
    provider = CaptureProvider([DeliveryState.FAILED, DeliveryState.DELIVERED])
    dispatcher = ProfiledNotificationDispatcher(
        _profile(),
        {"capture": provider},
        state=state,
        queue=queue,
        clock=lambda: current[0],
    )

    first = dispatcher.drain_due()
    assert first[0].state is DeliveryState.FAILED
    row = _queue_rows(state)[0]
    assert row["state"] == "retry"
    assert row["attempts"] == 1
    assert dispatcher.drain_due() == ()

    current[0] += timedelta(seconds=31)
    second = dispatcher.drain_due()
    assert second[0].state is DeliveryState.DELIVERED
    assert _queue_rows(state)[0]["state"] == "delivered"


def test_monitor_drains_deferred_routes_when_no_new_mail_exists(tmp_path: Path) -> None:
    class EmptyGateway:
        def poll_new_messages(self, _folder: str, since: str | None, _limit: int) -> object:
            return ((), since or "checkpoint")

    class DeferredDispatcher:
        def __init__(self) -> None:
            self.drains = 0

        def dispatch(self, _event: NotificationEvent) -> tuple[DeliveryResult, ...]:
            raise AssertionError("No mailbox event should be dispatched")

        def drain_due(self) -> tuple[DeliveryResult, ...]:
            self.drains += 1
            return (
                DeliveryResult(
                    "deferred-event",
                    "capture",
                    DeliveryState.DELIVERED,
                ),
            )

    state = StateStore(tmp_path / "state.sqlite3")
    dispatcher = DeferredDispatcher()
    monitor = MailboxMonitor(
        EmptyGateway(),
        state,
        account_id="synthetic-account",
        dispatcher=dispatcher,
        clock=lambda: NOW,
    )

    result = monitor.run_once()

    assert result.succeeded
    assert result.events == ()
    assert result.deliveries[0].state is DeliveryState.DELIVERED
    assert dispatcher.drains == 1
