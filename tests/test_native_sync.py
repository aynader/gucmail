"""Native provider serialization, page progress and durable monitoring boundaries."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from exchangelib import CalendarItem, Message
from exchangelib.errors import ErrorInvalidSyncStateData
from exchangelib.folders import Folder, Root
from exchangelib.properties import ItemId
from exchangelib.services import SyncFolderItems
from exchangelib.version import Build, Version
from lxml import etree
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError
from gucmail.ews.adapter import EWSGateway
from gucmail.ews.sync import SyncCursor, sync_scope
from gucmail.models import MessageSummary, NotificationEvent
from gucmail.services.monitoring import MailboxMonitor, PollBatch
from gucmail.services.monitoring_outbox import StateStoreEventOutbox
from gucmail.state import StateStore

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _gateway(monkeypatch, *, calendar=False):
    version = Version(build=Build(15, 1, 2375, 7))
    account = SimpleNamespace(version=version, protocol=SimpleNamespace(version=version))
    folder = Folder(
        root=Root(account=account, id="root"),
        id="folder",
        name="Calendar" if calendar else "Inbox",
        folder_class="IPF.Appointment" if calendar else "IPF.Note",
    )
    gateway = EWSGateway(AppConfig(email="synthetic@example.test"), account_factory=lambda: account)
    gateway._bindings()  # Registers production extended properties without connecting.
    monkeypatch.setattr(gateway, "_resolve_folder", lambda _folder: folder)
    monkeypatch.setattr(gateway, "_folder_path", lambda _folder: "/Synthetic")
    return gateway


@pytest.mark.parametrize("calendar", [False, True])
def test_sync_serializes_one_bounded_metadata_page(monkeypatch, calendar):
    gateway = _gateway(monkeypatch, calendar=calendar)
    requests = []

    def elements(service, *, payload):
        requests.append(etree.tostring(payload).decode())
        service.sync_state = "page-1"
        service.includes_last_item_in_range = False
        return iter(())

    monkeypatch.setattr(SyncFolderItems, "_get_elements", elements)
    result = gateway.sync_folder_items("calendar" if calendar else "inbox", max_changes=3)
    assert len(requests) == 1
    payload = etree.fromstring(requests[0].encode())
    assert payload.find(".//{*}BaseShape").text == "IdOnly"
    assert "<m:MaxChangesReturned>3</m:MaxChangesReturned>" in requests[0]
    assert "NormalItems" in requests[0]
    assert 'FieldURI="item:Body"' not in requests[0]
    assert 'FieldURI="item:Attachments"' not in requests[0]
    assert "MimeContent" not in requests[0]
    if calendar:
        assert 'FieldURI="calendar:Start"' not in requests[0]
        assert 'FieldURI="calendar:End"' not in requests[0]
    assert result.baseline and result.more_available
    assert "page-1" in result.checkpoint


def test_sync_change_identity_is_replay_safe_and_retains_later_updates(monkeypatch):
    gateway = _gateway(monkeypatch)
    message = Message(id="message", changekey="ck", subject="Synthetic")
    calls = []

    def call(service, **kwargs):
        calls.append(kwargs)
        service.sync_state = "next-" + (kwargs["sync_state"] or "initial")
        service.includes_last_item_in_range = True
        return iter(
            (
                ("update", message),
                ("read_flag_change", (ItemId(id="message"), True)),
                ("delete", ItemId(id="deleted")),
            )
        )

    monkeypatch.setattr(SyncFolderItems, "call", call)
    first = gateway.sync_folder_items("inbox")
    replay = gateway.sync_folder_items("inbox")
    later = gateway.sync_folder_items("inbox", first.checkpoint)
    assert [event.event_id for event in first.items] == [event.event_id for event in replay.items]
    assert first.items[0].event_id != later.items[0].event_id
    assert first.items[2].open_hint is None
    assert first.items[1].reference == first.items[0].reference
    assert not later.baseline
    assert len(calls) == 3
    assert gateway._memory_object(first.items[0].reference, "message") is None


def test_calendar_change_uses_calendar_reference_and_open_command(monkeypatch):
    gateway = _gateway(monkeypatch, calendar=True)

    def call(service, **kwargs):
        service.sync_state = "ready"
        service.includes_last_item_in_range = True
        return iter((("create", CalendarItem(id="event", subject="Synthetic event")),))

    monkeypatch.setattr(SyncFolderItems, "call", call)
    result = gateway.sync_folder_items("calendar")
    event = result.items[0]
    assert event.kind == "calendar_created"
    assert event.open_hint.startswith("gucmail calendar show ")
    assert "--reference" not in event.open_hint
    assert event.message.subject == "Synthetic event"


@pytest.mark.parametrize("failure", ["expired", "stalled", "missing", "oversized"])
def test_invalid_sync_page_never_advances_progress(monkeypatch, failure):
    gateway = _gateway(monkeypatch)
    cursor = SyncCursor(sync_scope(gateway.config.email, "folder", "message"), "old", False)
    calls = []

    def call(service, **kwargs):
        calls.append(kwargs)
        if failure == "expired":
            raise ErrorInvalidSyncStateData("Untrusted server text")
        service.sync_state = "old" if failure == "stalled" else "new"
        service.includes_last_item_in_range = None if failure == "missing" else False
        return iter([("delete", ItemId(id="deleted"))] * (4 if failure == "oversized" else 1))

    monkeypatch.setattr(SyncFolderItems, "call", call)
    with pytest.raises(ConfigurationError) as error:
        gateway.sync_folder_items("inbox", cursor.encode(), max_changes=3)
    assert "Untrusted server text" not in str(error.value)
    assert len(calls) == 1


def test_checkpoint_cannot_be_used_for_another_account_or_folder():
    scope = sync_scope("one@example.test", "one", "message")
    value = SyncCursor(scope, "opaque-token", False).encode()
    for other in (
        sync_scope("two@example.test", "one", "message"),
        sync_scope("one@example.test", "two", "message"),
        sync_scope("one@example.test", "one", "event"),
    ):
        with pytest.raises(ConfigurationError, match="does not match"):
            SyncCursor.read(value, scope=other)


def test_large_initial_baseline_resumes_quietly_across_processes(tmp_path):
    calls = []

    class Gateway:
        def sync_folder_items(self, folder, sync_state, max_changes):
            calls.append(sync_state)
            number = int(sync_state or 0) + 1
            message = MessageSummary(f"m-{number}", "Synthetic", None, NOW)
            return PollBatch((message,), str(number), number < 3, baseline=number <= 3)

    def run():
        state = StateStore(tmp_path / "state.sqlite3")
        return MailboxMonitor(
            Gateway(),
            state,
            account_id="synthetic",
            strategy_preference="incremental_sync",
            maximum_drain_batches=1,
        ).run_once()

    reports = [run() for _ in range(4)]
    assert calls == [None, "1", "2", "3"]
    assert all(report.succeeded for report in reports)
    assert [len(report.events) for report in reports] == [0, 0, 0, 1]
    assert [report.folders[0].more_available for report in reports] == [True, True, False, False]


def test_page_failure_retains_cursor_and_explicit_reset_is_scoped(tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")

    class Gateway:
        def sync_folder_items(self, **kwargs):
            raise ConfigurationError("Use watch --reset-checkpoint to rebuild the baseline.")

    monitor = MailboxMonitor(
        Gateway(), state, account_id="synthetic", strategy_preference="incremental_sync"
    )
    scope = monitor._scope("inbox")
    state.checkpoint(scope, sync_state="old", succeeded=True)
    state.checkpoint("another-scope", sync_state="keep", succeeded=True)
    state.remember_event("keep-event")
    result = monitor.run_once()
    assert not result.succeeded
    assert "--reset-checkpoint" in result.folders[0].error
    assert monitor.status().folders["inbox"]["sync_state"] == "old"
    monitor.reset_checkpoints()
    assert monitor.status().folders["inbox"]["sync_state"] is None
    assert not state.remember_event("keep-event")
    assert state.monitor_checkpoint("another-scope") == "keep"


def test_output_only_monitor_does_not_consume_pending_mail_notifications(tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    outbox = StateStoreEventOutbox(state)
    queued = NotificationEvent("queued-mail", "new_message", NOW, "synthetic")
    outbox.enqueue(queued, folder="inbox", detected_at=NOW)

    class Gateway:
        def sync_folder_items(self, **kwargs):
            return PollBatch((), "calendar-ready")

    result = MailboxMonitor(
        Gateway(),
        state,
        account_id="synthetic",
        folders=("calendar",),
        strategy_preference="incremental_sync",
        clock=lambda: NOW,
    ).run_once()
    assert result.succeeded
    assert [entry.event.event_id for entry in outbox.due(folder=None, now=NOW, limit=5)] == [
        "queued-mail"
    ]


def test_calendar_watch_cli_uses_sync_and_has_a_quiet_baseline(tmp_path, monkeypatch):
    from test_cli import _environment

    calls = []

    class Gateway:
        def sync_folder_items(self, **kwargs):
            calls.append(kwargs)
            return PollBatch((), "ready", baseline=True)

    monkeypatch.setattr(cli.core, "gateway", lambda _ctx: Gateway())
    result = CliRunner().invoke(
        cli.app, ["--json", "calendar", "watch"], env=_environment(tmp_path)
    )
    assert result.exit_code == 0, result.output
    assert calls == [{"folder": "calendar", "sync_state": None, "max_changes": 25}]
    assert "incremental_sync" in result.output


@pytest.mark.parametrize("fail", [False, True])
def test_watch_json_remains_parseable_and_failure_has_nonzero_exit(tmp_path, monkeypatch, fail):
    from test_cli import _environment

    class Gateway:
        def sync_folder_items(self, **kwargs):
            if fail:
                raise ConfigurationError("Synthetic failure")
            return PollBatch((MessageSummary("m-1", "Synthetic", None, NOW),), "ready")

    monkeypatch.setattr(cli.core, "gateway", lambda _ctx: Gateway())
    result = CliRunner().invoke(
        cli.app,
        ["--json", "watch", "--strategy", "incremental_sync", "--notify-existing", "--no-webhooks"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == (10 if fail else 0), result.output
    assert json.loads(result.stdout)["command"] == "watch"
    if not fail:
        assert "Synthetic" in result.stderr
