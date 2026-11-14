from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gucmail.errors import ThrottledError
from gucmail.models import Address, MessageSummary, NotificationEvent
from gucmail.services.monitoring import MailboxMonitor, MonitorStrategy
from gucmail.services.monitoring_outbox import InMemoryEventOutbox, OutboxState
from gucmail.services.notifications import (
    DeliveryResult,
    DeliveryState,
    NotificationDispatcher,
)
from gucmail.services.watcher_runtime import (
    InProcessWatcherLeaseManager,
    SleepResumeDetector,
    WatcherAlreadyRunningError,
)
from gucmail.state import StateStore

NOW = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)


def _message(reference: str, *, seconds: int = 0) -> MessageSummary:
    return MessageSummary(
        reference=reference,
        subject=f"Message {reference}",
        sender=Address("sender@example.test", "Sender"),
        received_at=NOW + timedelta(seconds=seconds),
        folder_path="Inbox",
        preview="Synthetic test message",
    )


def _event(event_id: str = "evt-1") -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        event_type="new_message",
        occurred_at=NOW,
        account_id="student@example.test",
        folder="Inbox",
        message_reference="m-1",
        subject="Synthetic event",
    )


class RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        self.events.append(event)
        return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)


def test_includes_last_uses_ews_inverse_continuation_semantics(tmp_path) -> None:
    monitor = MailboxMonitor(
        object(),
        StateStore(tmp_path / "state.sqlite3"),
        account_id="account-1",
    )

    complete = monitor._normalize_batch(
        {"items": (), "checkpoint": "done", "includes_last": True},
        None,
    )
    incomplete = monitor._normalize_batch(
        {"items": (), "checkpoint": "next", "includes_last": False},
        None,
    )

    assert complete.more_available is False
    assert incomplete.more_available is True


def test_monitor_drains_every_continuation_before_committing(tmp_path) -> None:
    class Gateway:
        monitor_strategy = "bounded_poll"

        def __init__(self) -> None:
            self.since: list[str | None] = []

        def poll_new_messages(self, _folder: str, since: str | None, _limit: int) -> object:
            self.since.append(since)
            if since is None:
                return {
                    "items": (_message("m-1", seconds=1), _message("m-2", seconds=2)),
                    "checkpoint": "continuation-2",
                    "more_available": True,
                }
            assert since == "continuation-2"
            return {
                "items": (_message("m-3", seconds=3),),
                "checkpoint": "committed-final",
                "more_available": False,
            }

    gateway = Gateway()
    provider = RecordingProvider()
    state = StateStore(tmp_path / "state.sqlite3")
    monitor = MailboxMonitor(
        gateway,
        state,
        account_id="account-1",
        dispatcher=NotificationDispatcher([provider], state=state),
        limit=2,
        notify_existing=True,
    )

    result = monitor.run_once()

    assert result.fully_succeeded
    assert result.folders[0].strategy is MonitorStrategy.POLLING
    assert gateway.since == [None, "continuation-2"]
    assert {event.message_reference for event in result.events} == {"m-1", "m-2", "m-3"}
    assert {event.message_reference for event in provider.events} == {"m-1", "m-2", "m-3"}
    assert monitor.status().folders["inbox"]["sync_state"] == "committed-final"


def test_monitor_fails_closed_when_continuation_does_not_progress(tmp_path) -> None:
    class Gateway:
        def poll_new_messages(self, _folder: str, since: str | None, _limit: int) -> object:
            return {
                "items": (_message("must-not-be-committed"),),
                "checkpoint": since,
                "more_available": True,
            }

    provider = RecordingProvider()
    state = StateStore(tmp_path / "state.sqlite3")
    outbox = InMemoryEventOutbox()
    monitor = MailboxMonitor(
        Gateway(),
        state,
        account_id="account-1",
        dispatcher=NotificationDispatcher([provider], state=state),
        outbox=outbox,
        notify_existing=True,
    )

    result = monitor.run_once()

    assert not result.succeeded
    assert result.events == ()
    assert provider.events == []
    assert outbox.entries() == ()
    assert monitor.status().folders["inbox"]["sync_state"] is None


def test_monitor_fails_closed_when_backlog_exceeds_request_budget(tmp_path) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        def poll_new_messages(self, _folder: str, _since: str | None, _limit: int) -> object:
            self.calls += 1
            return {
                "items": (_message(f"page-{self.calls}"),),
                "checkpoint": f"continuation-{self.calls}",
                "more_available": True,
            }

    gateway = Gateway()
    provider = RecordingProvider()
    state = StateStore(tmp_path / "state.sqlite3")
    monitor = MailboxMonitor(
        gateway,
        state,
        account_id="account-1",
        dispatcher=NotificationDispatcher([provider], state=state),
        maximum_drain_batches=2,
        notify_existing=True,
    )

    result = monitor.run_once()

    assert not result.succeeded
    assert gateway.calls == 2
    assert result.events == ()
    assert provider.events == []
    assert monitor.status().folders["inbox"]["sync_state"] is None


def test_throttling_stops_remaining_folder_requests_for_cycle(tmp_path) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def poll_new_messages(self, folder: str, _since: str | None, _limit: int) -> object:
            self.calls.append(folder)
            raise ThrottledError("synthetic throttling", retry_after=90)

    gateway = Gateway()
    monitor = MailboxMonitor(
        gateway,
        StateStore(tmp_path / "state.sqlite3"),
        account_id="account-1",
        folders=("inbox", "archive", "sent"),
    )

    result = monitor.run_once()

    assert gateway.calls == ["inbox"]
    assert [folder.folder for folder in result.folders] == ["inbox"]
    assert result.retry_after == 90


def test_provider_failure_keeps_normal_exchange_poll_cadence(tmp_path) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        def poll_new_messages(self, _folder: str, _since: str | None, _limit: int) -> object:
            self.calls += 1
            return ([_message("m-1")], f"checkpoint-{self.calls}")

    class FailedProvider:
        name = "failed-provider"

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            return DeliveryResult(event.event_id, self.name, DeliveryState.FAILED)

    sleeps: list[float] = []
    current_time = [NOW]

    def sleep_and_advance(seconds: float) -> None:
        sleeps.append(seconds)
        current_time[0] += timedelta(seconds=seconds)

    gateway = Gateway()
    state = StateStore(tmp_path / "state.sqlite3")
    monitor = MailboxMonitor(
        gateway,
        state,
        account_id="account-1",
        dispatcher=NotificationDispatcher([FailedProvider()], state=state),
        poll_interval_seconds=60,
        notify_existing=True,
        clock=lambda: current_time[0],
        monotonic=lambda: 0.0,
        sleep=sleep_and_advance,
    )

    reports = list(monitor.iter_foreground(max_cycles=2))

    assert [report.succeeded for report in reports] == [True, True]
    assert [report.delivery_succeeded for report in reports] == [False, False]
    assert sleeps == [60.0]
    assert gateway.calls == 2


def test_outbox_delivery_storage_failure_does_not_reclassify_exchange_poll(tmp_path) -> None:
    class Gateway:
        def poll_new_messages(self, _folder: str, _since: str | None, _limit: int) -> object:
            return ([_message("m-1")], "mailbox-checkpoint")

    class FailingDueOutbox:
        def enqueue(
            self,
            _event: NotificationEvent,
            *,
            folder: str,
            detected_at: datetime,
        ) -> bool:
            del folder, detected_at
            return True

        def due(self, *, folder: str, now: datetime, limit: int) -> object:
            del folder, now, limit
            raise RuntimeError("synthetic outbox read failure")

        def record(self, *_args, **_kwargs) -> object:
            raise AssertionError("no due entry may be recorded")

    state = StateStore(tmp_path / "state.sqlite3")
    monitor = MailboxMonitor(
        Gateway(),
        state,
        account_id="account-1",
        outbox=FailingDueOutbox(),  # type: ignore[arg-type]
        dispatcher=NotificationDispatcher([RecordingProvider()]),
        notify_existing=True,
    )

    result = monitor.run_once()

    assert result.succeeded
    assert not result.delivery_succeeded
    assert result.deliveries[0].provider == "notification-outbox"
    assert monitor.status().folders["inbox"]["sync_state"] == "mailbox-checkpoint"


def test_exchange_error_retry_never_breaks_safe_minimum_cadence(tmp_path) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        def poll_new_messages(self, _folder: str, _since: str | None, _limit: int) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic network failure")
            return ([], "recovered")

    sleeps: list[float] = []
    gateway = Gateway()
    monitor = MailboxMonitor(
        gateway,
        StateStore(tmp_path / "state.sqlite3"),
        account_id="account-1",
        random_source=lambda: 0.0,
        sleep=sleeps.append,
    )

    reports = list(monitor.iter_foreground(max_cycles=2))

    assert [report.succeeded for report in reports] == [False, True]
    assert sleeps == [60.0]
    assert gateway.calls == 2


def test_watcher_lease_rejects_duplicate_scope_and_releases_cleanly() -> None:
    manager = InProcessWatcherLeaseManager()

    with manager.acquire("account-profile") as lease:
        assert manager.status("account-profile") == lease
        with (
            pytest.raises(WatcherAlreadyRunningError, match="already watching"),
            manager.acquire("account-profile"),
        ):
            pass

    assert manager.status("account-profile") is None
    with manager.acquire("account-profile"):
        pass


def test_two_monitors_for_same_account_cannot_overlap(tmp_path) -> None:
    class Gateway:
        def poll_new_messages(self, _folder: str, _since: str | None, _limit: int) -> object:
            return ([], "checkpoint")

    manager = InProcessWatcherLeaseManager()
    state = StateStore(tmp_path / "state.sqlite3")
    first = MailboxMonitor(Gateway(), state, account_id="account-1", lease_manager=manager)
    second = MailboxMonitor(Gateway(), state, account_id="account-1", lease_manager=manager)
    foreground = first.iter_foreground(max_cycles=2)
    next(foreground)
    try:
        with pytest.raises(WatcherAlreadyRunningError, match="already watching"):
            second.run_once()
    finally:
        foreground.close()


def test_sleep_resume_is_detected_without_catchup_burst(tmp_path) -> None:
    class FakeTime:
        def __init__(self) -> None:
            self.value = 0.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.value

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.value += seconds + 300.0

    class Gateway:
        def __init__(self) -> None:
            self.calls = 0
            self.resumes: list[float] = []

        def poll_new_messages(self, _folder: str, _since: str | None, _limit: int) -> object:
            self.calls += 1
            return ([], f"checkpoint-{self.calls}")

        def handle_system_resume(self, elapsed: float) -> None:
            self.resumes.append(elapsed)

    fake_time = FakeTime()
    gateway = Gateway()
    monitor = MailboxMonitor(
        gateway,
        StateStore(tmp_path / "state.sqlite3"),
        account_id="account-1",
        poll_interval_seconds=60,
        resume_threshold_seconds=120,
        monotonic=fake_time.monotonic,
        sleep=fake_time.sleep,
    )

    reports = list(monitor.iter_foreground(max_cycles=2))

    assert gateway.calls == 2
    assert fake_time.sleeps == [60.0]
    assert gateway.resumes == [360.0]
    assert reports[1].resumed_after_seconds == 360.0


def test_sleep_resume_detector_ignores_clock_regression() -> None:
    detector = SleepResumeDetector(120)

    assert detector.observe(500) is None
    assert detector.observe(450) is None
    assert detector.observe(600) == 150


def test_outbox_retries_per_provider_and_dead_letters_with_redaction() -> None:
    outbox = InMemoryEventOutbox(retry_delay_seconds=10, maximum_delivery_cycles=2)
    event = _event()
    assert outbox.enqueue(event, folder="Inbox", detected_at=NOW)
    assert not outbox.enqueue(event, folder="Inbox", detected_at=NOW)
    assert [entry.event.event_id for entry in outbox.due(folder="inbox", now=NOW, limit=5)] == [
        event.event_id
    ]

    failed = DeliveryResult(
        event.event_id,
        "webhook",
        DeliveryState.FAILED,
        "failed for private@example.test",
        status_code=503,
    )
    retry = outbox.record(event.event_id, [failed], now=NOW)

    assert retry.state is OutboxState.RETRY_SCHEDULED
    assert retry.delivery_cycles == 1
    assert retry.provider_attempts[0].status_code == 503
    assert "private@example.test" not in str(retry.provider_attempts)
    assert outbox.due(folder="Inbox", now=NOW + timedelta(seconds=9), limit=5) == ()
    assert len(outbox.due(folder="Inbox", now=NOW + timedelta(seconds=10), limit=5)) == 1

    dead = outbox.record(event.event_id, [failed], now=NOW + timedelta(seconds=10))
    assert dead.state is OutboxState.DEAD_LETTER
    assert dead.next_attempt_at is None
    assert outbox.due(folder="Inbox", now=NOW + timedelta(days=1), limit=5) == ()

    delivered_event = replace(event, event_id="evt-2")
    outbox.enqueue(delivered_event, folder="Inbox", detected_at=NOW)
    delivered = outbox.record(
        delivered_event.event_id,
        [
            DeliveryResult(
                delivered_event.event_id,
                "console",
                DeliveryState.DELIVERED,
            )
        ],
        now=NOW,
    )
    assert delivered.state is OutboxState.DELIVERED
