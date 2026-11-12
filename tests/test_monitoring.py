from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gucmail.errors import ThrottledError
from gucmail.models import Address, MessageSummary, NotificationEvent
from gucmail.services.monitoring import MailboxMonitor, MonitorStrategy
from gucmail.services.notifications import (
    DeliveryResult,
    DeliveryState,
    NotificationDispatcher,
)
from gucmail.state import StateStore

NOW = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)


def _message(reference: str, *, received_at: datetime = NOW) -> MessageSummary:
    return MessageSummary(
        reference=reference,
        subject=f"Message {reference}",
        sender=Address("sender@example.test", "Sender"),
        received_at=received_at,
        folder_path="Inbox",
        preview="Synthetic test message",
    )


class RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def notify(self, event: NotificationEvent) -> DeliveryResult:
        self.events.append(event)
        return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)


def test_incremental_one_shot_persists_checkpoint_and_deduplicates(tmp_path) -> None:
    message = _message("m-1")

    class Gateway:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None, int]] = []

        def poll_new_messages(self, folder: str, since: str | None, limit: int) -> object:
            self.calls.append((folder, since, limit))
            return ([message], "checkpoint-1")

    gateway = Gateway()
    state = StateStore(tmp_path / "state.sqlite3")
    provider = RecordingProvider()
    monitor = MailboxMonitor(
        gateway,
        state,
        account_id="account-1",
        dispatcher=NotificationDispatcher([provider], state=state),
        notify_existing=True,
    )

    first = monitor.run_once()
    second = monitor.run_once()

    assert first.succeeded
    assert [event.message_reference for event in first.events] == ["m-1"]
    assert second.events == ()
    assert second.folders[0].duplicates == 1
    assert gateway.calls == [
        ("inbox", None, 50),
        ("inbox", "checkpoint-1", 50),
    ]
    assert monitor.gateway is gateway
    assert len(provider.events) == 1
    assert monitor.status().folders["inbox"]["sync_state"] == "checkpoint-1"


def test_snapshot_fallback_establishes_quiet_baseline_then_notifies(tmp_path) -> None:
    old = _message("old", received_at=NOW - timedelta(minutes=5))
    new = _message("new", received_at=NOW)

    class Gateway:
        def __init__(self) -> None:
            self.items = (old,)

        def list_messages(
            self,
            folder: str,
            *,
            limit: int,
            offset: int,
            order_by: str,
        ) -> object:
            assert (folder, limit, offset, order_by) == (
                "inbox",
                50,
                0,
                "-datetime_received",
            )
            return self.items

    gateway = Gateway()
    state = StateStore(tmp_path / "state.sqlite3")
    provider = RecordingProvider()
    monitor = MailboxMonitor(
        gateway,
        state,
        account_id="account-1",
        dispatcher=NotificationDispatcher([provider], state=state),
    )

    baseline = monitor.one_shot()
    assert baseline.events == ()
    assert baseline.folders[0].baseline_items == 1
    assert baseline.folders[0].strategy is MonitorStrategy.SNAPSHOT

    gateway.items = (new, old)
    changed = monitor.one_shot()
    assert [event.message_reference for event in changed.events] == ["new"]
    assert provider.events[0].message_reference == "new"


@pytest.mark.parametrize("page_size", [1, 2])
def test_snapshot_pagination_rejects_shift_and_retries_without_losing_items(
    tmp_path, page_size: int
) -> None:
    old = _message("old", received_at=NOW - timedelta(minutes=30))
    items = tuple(
        _message(reference, received_at=NOW - timedelta(minutes=minute))
        for reference, minute in (("a", 0), ("b", 10), ("c", 11), ("d", 12))
    )

    class Gateway:
        def __init__(self) -> None:
            self.items = [old]
            self.shift = False

        def list_messages(self, _folder: str, *, limit: int, offset: int, order_by: str) -> object:
            assert order_by == "-datetime_received"
            page = self.items[offset : offset + limit]
            if self.shift and offset == 0:
                self.items.pop(0)
                self.shift = False
            return page

    gateway = Gateway()
    state = StateStore(tmp_path / "state.sqlite3")
    monitor = MailboxMonitor(gateway, state, account_id="account-1", limit=page_size)
    baseline = monitor.run_once()
    assert baseline.succeeded
    checkpoint = monitor.status().folders["inbox"]["sync_state"]
    gateway.items = [*items, old]
    gateway.shift = True

    failed = monitor.run_once()
    assert not failed.succeeded
    assert "changed during pagination" in (failed.folders[0].error or "")
    assert failed.events == ()
    assert monitor.status().folders["inbox"]["sync_state"] == checkpoint

    retry = monitor.run_once()
    assert retry.succeeded
    assert {event.message_reference for event in retry.events} == {"b", "c", "d"}
    assert monitor.status().folders["inbox"]["sync_state"] != checkpoint


def test_failed_notification_does_not_roll_back_mailbox_checkpoint(tmp_path) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.since: list[str | None] = []

        def poll_new_messages(self, _folder: str, since: str | None, _limit: int) -> object:
            self.since.append(since)
            return ([_message("retry-me")], "do-not-commit-yet")

    class FlakyProvider:
        name = "flaky"

        def __init__(self) -> None:
            self.fail = True
            self.calls = 0

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            self.calls += 1
            state = DeliveryState.FAILED if self.fail else DeliveryState.DELIVERED
            self.fail = False
            return DeliveryResult(event.event_id, self.name, state)

    gateway = Gateway()
    provider = FlakyProvider()
    state = StateStore(tmp_path / "state.sqlite3")
    current_time = [NOW]
    monitor = MailboxMonitor(
        gateway,
        state,
        account_id="account-1",
        dispatcher=NotificationDispatcher([provider], state=state),
        notify_existing=True,
        clock=lambda: current_time[0],
    )

    first = monitor.run_once()
    # Durable provider attempts use an exponential retry schedule (30 seconds
    # plus bounded jitter by default).  Advance the injected clock rather than
    # accidentally asserting an immediate tight-loop retry.
    current_time[0] += timedelta(minutes=1)
    second = monitor.run_once()

    assert first.succeeded
    assert not first.delivery_succeeded
    assert not first.fully_succeeded
    assert second.fully_succeeded
    assert second.events == ()
    assert second.deliveries[0].state is DeliveryState.DELIVERED
    assert provider.calls == 2
    assert gateway.since == [None, "do-not-commit-yet"]
    assert monitor.status().folders["inbox"]["sync_state"] == "do-not-commit-yet"


def test_foreground_respects_server_backoff_without_fast_polling(tmp_path) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        def poll_new_messages(self, _folder: str, _since: str | None, _limit: int) -> object:
            self.calls += 1
            if self.calls == 1:
                raise ThrottledError("synthetic throttling", retry_after=7)
            return ([], "recovered")

    sleeps: list[float] = []
    gateway = Gateway()
    monitor = MailboxMonitor(
        gateway,
        StateStore(tmp_path / "state.sqlite3"),
        account_id="account-1",
        sleep=sleeps.append,
    )

    reports = list(monitor.iter_foreground(max_cycles=2))
    assert [report.succeeded for report in reports] == [False, True]
    assert sleeps == [60.0]
    assert gateway.calls == 2


def test_monitor_rejects_aggressive_poll_interval(tmp_path) -> None:
    with pytest.raises(ValueError, match="at least"):
        MailboxMonitor(
            object(),
            StateStore(tmp_path / "state.sqlite3"),
            account_id="account-1",
            poll_interval_seconds=5,
        )
