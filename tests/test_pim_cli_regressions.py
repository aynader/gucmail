"""CLI regressions found during beta QA of calendar, contacts and tasks commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from typer.main import get_command
from typer.testing import CliRunner

import gucmail.cli as cli_module
from gucmail.cli import app
from gucmail.models import Address, CalendarEvent
from gucmail.services.interchange import export_vcards
from test_cli import _environment
from test_pim import (
    CalendarMemoryGateway,
    ContactsMemoryGateway,
    TasksMemoryGateway,
    _contact,
    _event,
    _task,
)


def _invoke(tmp_path, monkeypatch, gateway, *arguments: str):
    monkeypatch.setattr(cli_module, "_gateway", lambda *_args, **_kwargs: gateway)
    return CliRunner().invoke(app, list(arguments), env=_environment(tmp_path))


def test_calendar_cancel_refuses_an_appointment_without_deleting_it(tmp_path, monkeypatch):
    start = datetime(2026, 11, 12, 9, tzinfo=UTC)
    gateway = CalendarMemoryGateway((_event("appointment", start, start + timedelta(hours=1)),))

    result = _invoke(tmp_path, monkeypatch, gateway, "--yes", "calendar", "cancel", "appointment")

    assert result.exit_code == 3
    assert "not a meeting" in result.output
    assert "appointment" in gateway.events


def test_calendar_cancel_still_cancels_a_meeting(tmp_path, monkeypatch):
    start = datetime(2026, 11, 12, 9, tzinfo=UTC)
    meeting = CalendarEvent(
        "meeting",
        "Synthetic meeting",
        start,
        start + timedelta(hours=1),
        "UTC",
        required_attendees=(Address("attendee@example.test"),),
        is_meeting=True,
    )
    gateway = CalendarMemoryGateway((meeting,))

    result = _invoke(
        tmp_path, monkeypatch, gateway, "--json", "--yes", "calendar", "cancel", "meeting"
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["cancelled"] is True
    assert "meeting" not in gateway.events


def test_task_progress_moves_a_not_started_task_into_progress(tmp_path, monkeypatch):
    gateway = TasksMemoryGateway((_task(),))

    result = _invoke(
        tmp_path, monkeypatch, gateway, "--yes", "tasks", "edit", "task-1", "--percent", "40"
    )

    assert result.exit_code == 0, result.output
    assert gateway.tasks["task-1"].percent_complete == 40
    assert gateway.tasks["task-1"].status == "in_progress"


def test_task_progress_of_100_points_to_complete(tmp_path, monkeypatch):
    gateway = TasksMemoryGateway((_task(),))

    result = _invoke(
        tmp_path, monkeypatch, gateway, "--yes", "tasks", "edit", "task-1", "--percent", "100"
    )

    assert result.exit_code == 3
    assert "tasks complete" in result.output
    assert gateway.updates == []


def test_task_export_refuses_to_replace_an_existing_file(tmp_path, monkeypatch):
    gateway = TasksMemoryGateway((_task(),))
    target = tmp_path / "tasks.json"
    target.write_text("keep me", encoding="utf-8")

    refused = _invoke(tmp_path, monkeypatch, gateway, "tasks", "export", "--output", str(target))
    assert refused.exit_code == 3
    assert target.read_text(encoding="utf-8") == "keep me"

    replaced = _invoke(
        tmp_path,
        monkeypatch,
        gateway,
        "--yes",
        "tasks",
        "export",
        "--output",
        str(target),
        "--overwrite",
    )
    assert replaced.exit_code == 0, replaced.output
    assert json.loads(target.read_text(encoding="utf-8"))[0]["reference"] == "task-1"


def test_contact_import_reports_skipped_duplicates(tmp_path, monkeypatch):
    existing = _contact("ada", "Ada Lovelace", "ada@example.test")
    gateway = ContactsMemoryGateway((existing,))
    source = tmp_path / "contacts.vcf"
    source.write_text(export_vcards((existing,)), encoding="utf-8")

    result = _invoke(
        tmp_path,
        monkeypatch,
        gateway,
        "--json",
        "--yes",
        "contacts",
        "import",
        str(source),
        "--apply",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"] == []
    assert any("Skipped 1 imported contact" in warning for warning in payload["warnings"])
    assert list(gateway.contacts) == ["ada"]


def test_outbox_human_list_uses_columns_not_raw_records(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Queueing and listing must stay local.")

    monkeypatch.setattr(cli_module, "_gateway", forbidden)
    runner = CliRunner()
    queued = runner.invoke(
        app,
        [
            "--json",
            "--yes",
            "send",
            "--to",
            "peer@example.test",
            "--subject",
            "Synthetic",
            "--body",
            "Synthetic body",
            "--at",
            "2099-01-05T09:00:00+02:00",
        ],
        env=_environment(tmp_path),
    )
    assert queued.exit_code == 0, queued.output
    reference = json.loads(queued.output)["data"]["reference"]

    listed = runner.invoke(
        app, ["scheduled", "list"], env={**_environment(tmp_path), "COLUMNS": "200"}
    )
    assert listed.exit_code == 0, listed.output
    assert reference in listed.output
    assert "scheduled" in listed.output
    assert '{"reference"' not in listed.output
    as_json = runner.invoke(app, ["--json", "scheduled", "list"], env=_environment(tmp_path))
    assert json.loads(as_json.output)["data"][0]["message"]["reference"] == reference


def test_negative_pim_flags_have_no_double_negative_alias():
    root = get_command(app)
    for group, command, flag in (
        ("calendar", "create", "--no-reminder"),
        ("tasks", "create", "--no-reminder"),
        ("tasks", "create", "--no-due"),
        ("backup", "create", "--no-compression"),
        ("backup", "estimate", "--no-compression"),
    ):
        option = next(
            param for param in root.commands[group].commands[command].params if flag in param.opts
        )
        assert option.secondary_opts == []


def _backup_root(tmp_path):
    return tmp_path / "data" / "backups"


def test_backup_verify_exits_nonzero_when_the_report_is_invalid(tmp_path, monkeypatch):
    from gucmail.services.backup import BackupItem, BackupService

    service = BackupService(_backup_root(tmp_path))
    service.create([BackupItem("one", "message", {"subject": "Synthetic"})], backup_id="qa-verify")
    healthy = _invoke(tmp_path, monkeypatch, object(), "--json", "backup", "verify", "qa-verify")
    assert healthy.exit_code == 0, healthy.output
    assert json.loads(healthy.output)["data"]["valid"] is True

    for blob in (_backup_root(tmp_path) / "qa-verify" / "objects").rglob("*"):
        if blob.is_file():
            blob.write_bytes(b"corrupted synthetic object")
    damaged = _invoke(tmp_path, monkeypatch, object(), "--json", "backup", "verify", "qa-verify")
    assert damaged.exit_code == 11, damaged.output
    assert json.loads(damaged.output)["data"]["valid"] is False


def test_backup_restore_apply_with_no_matching_selection_says_nothing_was_restored(
    tmp_path, monkeypatch
):
    from gucmail.services.backup import BackupItem, BackupService

    BackupService(_backup_root(tmp_path)).create(
        [BackupItem("one", "message", {"subject": "Synthetic"})], backup_id="qa-restore"
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("An empty restore must not resolve the mailbox.")

    monkeypatch.setattr(cli_module, "_gateway", forbidden)
    result = CliRunner().invoke(
        app,
        ["--json", "--yes", "backup", "restore", "qa-restore", "--apply", "--selected", "missing"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 0, result.output
    warnings = json.loads(result.output)["warnings"]
    assert any("Nothing was restored" in item and "--selected" in item for item in warnings)


class _PagedMessages:
    """Mimics the EWS gateway contract: at most 200 summaries per request."""

    def __init__(self, total: int) -> None:
        from gucmail.models import MessageSummary

        start = datetime(2026, 11, 1, tzinfo=UTC)
        self.messages = [
            MessageSummary(
                f"msg-{index}",
                f"Synthetic {index % 7}",
                Address(f"sender{index % 5}@example.test"),
                start - timedelta(hours=index),
                folder_path="/Inbox",
                size=100 + index,
            )
            for index in range(total)
        ]
        self.requests: list[tuple[int, int]] = []

    def list_messages(self, folder="inbox", limit=50, offset=0, order_by="-datetime_received"):
        if not 1 <= limit <= 200:
            raise AssertionError("limit must be between 1 and 200.")
        self.requests.append((limit, offset))
        return tuple(self.messages[offset : offset + limit])


def test_message_scan_pages_within_the_gateway_bound():
    from gucmail.services.message_scan import scan_message_summaries

    gateway = _PagedMessages(450)
    assert len(scan_message_summaries(gateway, folder="inbox", limit=500)) == 450
    assert gateway.requests == [(200, 0), (200, 200), (100, 400)]

    small = _PagedMessages(30)
    assert len(scan_message_summaries(small, folder="inbox", limit=500)) == 30
    assert small.requests == [(200, 0)]


def test_default_500_item_analysis_commands_respect_the_page_bound(tmp_path, monkeypatch):
    gateway = _PagedMessages(260)
    for arguments in (
        ("insights", "overview"),
        ("insights", "storage"),
        ("clusters", "list"),
        ("cleanup", "analyze"),
        ("index", "build"),
    ):
        gateway.requests.clear()
        result = _invoke(tmp_path, monkeypatch, gateway, "--json", *arguments)
        assert result.exit_code == 0, (arguments, result.output)
        assert gateway.requests == [(200, 0), (200, 200)], arguments


def test_quota_folders_default_limit_pages_within_the_gateway_bound(tmp_path, monkeypatch):
    from gucmail.models import FolderInfo

    class Folders:
        def __init__(self) -> None:
            self.folders = [
                FolderInfo(f"fld-{n}", f"Folder {n}", f"/Folder {n}", None) for n in range(230)
            ]
            self.requests: list[tuple[int, int]] = []

        def list_folders(self, parent_reference=None, limit=50, offset=0):
            if not 1 <= limit <= 200:
                raise AssertionError("limit must be between 1 and 200.")
            self.requests.append((limit, offset))
            return tuple(self.folders[offset : offset + limit])

    gateway = Folders()
    result = _invoke(tmp_path, monkeypatch, gateway, "--json", "quota", "folders")
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["data"]["folders"]) == 230
    assert gateway.requests == [(200, 0), (200, 200)]


def test_unknown_inputs_are_not_reported_as_internal_failures(tmp_path, monkeypatch):
    for arguments, code in (
        (("ews", "describe", "NoSuchOperation"), 6),
        (("ews", "request-preview", "NoSuchOperation"), 6),
        (("recurring", "delete", "missing-rule"), 6),
        (("clusters", "list", "--by", "bogus"), 3),
    ):
        result = _invoke(tmp_path, monkeypatch, _PagedMessages(3), "--json", "--yes", *arguments)
        assert result.exit_code == code, (arguments, result.output)
        assert json.loads(result.output)["data"]["message"] != "The command could not be completed."
