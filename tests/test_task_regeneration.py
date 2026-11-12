"""Real Exchange types with owned synthetic responses; never a live request."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from exchangelib import Account, Task
from exchangelib.recurrence import DailyPattern, DailyRegeneration, NoEndPattern, TaskRecurrence
from exchangelib.services import UpdateItem
from exchangelib.version import Build, Version

from gucmail.errors import ConfigurationError, ExitCode, MutationResultUnavailableError
from gucmail.ews.mapping import _recurrence
from gucmail.models import TaskInfo, to_primitive
from gucmail.services.backup import BackupItem, BackupRequest, BackupSelection, BackupService
from gucmail.services.pim_preferences import TaskCreationPreferences
from gucmail.services.recurrence import (
    TaskRegenerationPattern,
    TaskRegenerationUnit,
    normalize_recurrence_mapping,
    recurrence_to_rfc_mapping,
)
from gucmail.services.task_recurrence import TaskRepeatKind, TaskRepeatPreferences
from gucmail.services.tasks import TaskService
from gucmail.state import StateStore
from test_acceptance_ews_regressions import projected_gateway as projected_gateway
from test_backup_scope import Snapshot
from test_compose_preferences import wait_for
from test_contact_operations import activate
from test_pim_creation_preferences import RecordingGateway, invoke, settings_for
from test_release_boundaries import _restore_gateway
from test_settings_secondpass import _context


@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("pattern_class", [DailyPattern, DailyRegeneration])
def test_native_completion_keeps_next_series_and_completed_instance_distinct(
    projected_gateway, monkeypatch, persistent, pattern_class
):
    gateway = projected_gateway
    if not persistent:
        gateway.state = None
    anchor = date(2026, 10, 4)
    rule = TaskRecurrence(pattern=pattern_class(interval=2), boundary=NoEndPattern(start=anchor))
    item = Task(
        account=gateway.account,
        id="synthetic-current-series",
        changekey="before-completion",
        subject="Synthetic recurring task",
        start_date=anchor,
        due_date=anchor,
        recurrence=rule,
        status="NotStarted",
        percent_complete=Decimal(0),
    )
    series_reference = gateway._remember("task", item)
    payloads, reads = [], []
    completion = datetime(2026, 10, 4, 9, tzinfo=UTC)

    def save(value, *, update_fields):
        payloads.append(
            UpdateItem(account=value.account).get_payload(
                items=[(value, update_fields)],
                message_disposition="SaveOnly",
                conflict_resolution="AutoResolve",
                send_meeting_invitations_or_cancellations="SendToNone",
                suppress_read_receipts=True,
            )
        )
        value.id = "synthetic-completed-instance"
        value.changekey = "completed-key"

    def fetch(account, *, ids, only_fields):
        remote_id = ids[0][0]
        reads.append(remote_id)
        completed = remote_id == "synthetic-completed-instance"
        assert completed or remote_id == "synthetic-current-series"
        return [
            Task(
                account=account,
                id=remote_id,
                changekey="completed-key" if completed else "next-series-key",
                subject="Synthetic recurring task",
                status="Completed" if completed else "NotStarted",
                percent_complete=Decimal(100 if completed else 0),
                start_date=anchor,
                due_date=anchor,
                recurrence=None if completed else rule,
                complete_date=completion if completed else None,
            )
        ]

    monkeypatch.setattr(Task, "save", save)
    monkeypatch.setattr(Account, "fetch", fetch)
    result = TaskService(gateway).complete(series_reference)
    assert result.reference != series_reference
    assert result.status == "completed" and result.completed_at == completion
    assert result.recurrence is None
    assert len(payloads) == 1
    assert not payloads[0].xpath('//*[local-name()="CompleteDate"]')
    assert gateway._resolve_item(series_reference, "task")[1].id == "synthetic-current-series"
    assert gateway._resolve_item(result.reference, "task")[1].id == "synthetic-completed-instance"
    next_task = TaskService(gateway).get(series_reference)
    assert next_task.reference == series_reference and next_task.status == "not_started"
    assert next_task.recurrence is not None
    assert TaskService(gateway).get(result.reference).status == "completed"
    assert reads == [
        "synthetic-completed-instance",
        "synthetic-current-series",
        "synthetic-completed-instance",
    ]
    if persistent:
        reopened = StateStore(gateway.state.path)
        assert (
            reopened.resolve_reference(series_reference, "task")["ews_id"]
            == "synthetic-current-series"
        )
        assert (
            reopened.resolve_reference(result.reference, "task")["ews_id"]
            == "synthetic-completed-instance"
        )


@pytest.mark.parametrize(
    "kind,unit,tag",
    [
        (TaskRepeatKind.AFTER_COMPLETION_DAILY, "day", "DailyRegeneration"),
        (TaskRepeatKind.AFTER_COMPLETION_WEEKLY, "week", "WeeklyRegeneration"),
        (TaskRepeatKind.AFTER_COMPLETION_MONTHLY, "month", "MonthlyRegeneration"),
        (TaskRepeatKind.AFTER_COMPLETION_YEARLY, "year", "YearlyRegeneration"),
    ],
)
@pytest.mark.parametrize("boundary", ["no_end", "numbered", "end_date"])
def test_all_native_regeneration_elements_and_boundaries_round_trip_without_account(
    projected_gateway, kind, unit, tag, boundary
):
    anchor = date(2026, 10, 4)
    rule = TaskRepeatPreferences(kind, 2, 3 if boundary == "numbered" else None).rule(
        anchor, until=date(2027, 10, 4) if boundary == "end_date" else None
    )
    assert rule["target"] == "task"
    assert rule["pattern"] == {"type": "task_regeneration", "unit": unit, "interval": 2}
    assert rule["boundary"]["type"] == boundary
    # The pure translator must not resolve even the owned synthetic account.
    projected_gateway._account_factory = lambda: pytest.fail("Account resolved during translation")
    native = projected_gateway._ews_recurrence(rule, target="task", start=anchor)
    assert isinstance(native, TaskRecurrence) and type(native.pattern).__name__ == tag
    xml = native.to_xml(version=Version(build=Build(15, 1, 2507, 6)))
    assert xml.xpath('//*[local-name()="' + tag + '"]/*[local-name()="Interval"]/text()') == ["2"]
    assert not xml.xpath('//*[contains(local-name(),"DaysOfWeek") or local-name()="DayOfMonth"]')
    assert _recurrence(native, task=True) == rule
    assert normalize_recurrence_mapping(rule, task=True, start=anchor) == rule
    with pytest.raises(ConfigurationError, match="cannot be represented"):
        recurrence_to_rfc_mapping(rule, target="task", start=anchor)
    with pytest.raises(ConfigurationError, match=r"target|only for tasks"):
        projected_gateway._ews_recurrence(rule, target="calendar", start=anchor)


@pytest.mark.parametrize("interval", [0, 1000, True, 1.5, "2", None])
def test_typed_regeneration_rejects_invalid_intervals(interval):
    with pytest.raises(ConfigurationError):
        TaskRegenerationPattern(TaskRegenerationUnit.DAY, interval)


@pytest.mark.parametrize("unit", ["hours", "DAY", "days", None, True])
def test_regeneration_mapping_rejects_unknown_units(unit):
    with pytest.raises(ConfigurationError, match="unit"):
        normalize_recurrence_mapping(
            {
                "target": "task",
                "pattern": {"type": "task_regeneration", "unit": unit, "interval": 2},
                "boundary": {"type": "no_end", "start": "2026-10-04"},
            },
            task=True,
        )


def test_calendar_rejects_undeclared_task_regeneration_and_extra_fixed_date_fields():
    rule = {
        "pattern": {"type": "task_regeneration", "unit": "week", "interval": 2},
        "boundary": {"type": "no_end", "start": "2026-10-04"},
    }
    with pytest.raises(ConfigurationError, match="only for tasks"):
        normalize_recurrence_mapping(rule, task=False)
    rule["pattern"]["weekdays"] = ["monday"]
    with pytest.raises(ConfigurationError, match="unknown field"):
        normalize_recurrence_mapping(rule, task=True)


@pytest.mark.parametrize("unit", list(TaskRegenerationUnit))
def test_regeneration_json_archive_and_restore_preserve_exact_native_meaning(
    tmp_path, monkeypatch, unit
):
    kind = {
        "day": TaskRepeatKind.AFTER_COMPLETION_DAILY,
        "week": TaskRepeatKind.AFTER_COMPLETION_WEEKLY,
        "month": TaskRepeatKind.AFTER_COMPLETION_MONTHLY,
        "year": TaskRepeatKind.AFTER_COMPLETION_YEARLY,
    }[unit.value]
    original = TaskCreationPreferences(repeat=TaskRepeatPreferences(kind, 3, 4)).new_task(
        subject="Synthetic regeneration archive",
        timezone="UTC",
        start_date=date(2030, 1, 1),
    )
    source = Snapshot(
        [BackupItem(original.reference, "task", to_primitive(original), folder_path="/Tasks")]
    )
    service = BackupService(tmp_path / "owned-backups")
    created = service.create(source, BackupRequest(selection=BackupSelection(item_kinds=("task",))))
    reopened = BackupService(tmp_path / "owned-backups").open_offline(created.backup_id)
    archived = reopened.get_item(original.reference)
    assert archived.data["recurrence"] == original.recurrence
    gateway, restored = _restore_gateway(monkeypatch, Task)
    gateway._restore_task(object(), archived.data)
    restored_task = restored[0][0]
    assert _recurrence(restored_task.recurrence, task=True) == original.recurrence
    assert restored_task.recurrence.pattern.interval == 3
    assert restored_task.recurrence.boundary.number == 4
    assert restored_task.status == "NotStarted"
    assert len(restored) == 1


@pytest.mark.parametrize(
    "kind", [kind for kind in TaskRepeatKind if kind.value.startswith("after_completion_")]
)
def test_cli_regeneration_saved_defaults_explicit_override_and_local_preview(
    tmp_path, monkeypatch, kind
):
    from gucmail import cli

    context = _context(tmp_path)
    service = settings_for(context)
    session = service.begin()
    for key, value in {
        "tasks.default_repeat": kind.value,
        "tasks.default_repeat_interval": 3,
        "tasks.default_repeat_count": 4,
    }.items():
        session.set(key, value)
    session.apply()
    before = context.paths.config_file.read_bytes()
    monkeypatch.setattr(cli, "_gateway", lambda *_: pytest.fail("Local preview resolved Exchange"))
    saved = invoke(tmp_path, "tasks", ["--start", "2030-01-01", "--dry-run"])
    assert saved.exit_code == 0, saved.output
    rule = json.loads(saved.output)["data"]["task"]["recurrence"]
    assert rule == TaskRepeatPreferences(kind, 3, 4).rule(date(2030, 1, 1))
    overridden = invoke(
        tmp_path,
        "tasks",
        [
            "--start",
            "2030-01-01",
            "--repeat",
            "after_completion_yearly",
            "--repeat-interval",
            "2",
            "--repeat-forever",
            "--dry-run",
        ],
    )
    assert overridden.exit_code == 0, overridden.output
    assert json.loads(overridden.output)["data"]["task"]["recurrence"] == TaskRepeatPreferences(
        TaskRepeatKind.AFTER_COMPLETION_YEARLY, 2
    ).rule(date(2030, 1, 1))
    assert context.paths.config_file.read_bytes() == before
    context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_apply_and_keyboard_fresh_task_keep_regeneration(
    tmp_path, monkeypatch, size
):
    from textual.widgets import Input, Select

    from gucmail.tui.app import GUCmailApp
    from gucmail.tui.pim_screens import TaskEditorScreen, TasksScreen
    from gucmail.tui.settings_screen import SettingsScreen

    context = _context(tmp_path)
    gateway = RecordingGateway()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        settings = app.screen
        for key, value in {
            "tasks.default_repeat": "after_completion_weekly",
            "tasks.default_repeat_interval": 2,
            "tasks.default_repeat_count": 3,
        }.items():
            assert settings.action_select_setting(key)
            await pilot.pause()
            if isinstance(value, str):
                settings.query_one("#settings-choice", Select).value = value
            else:
                settings.query_one("#settings-editor", Input).value = str(value)
            await activate(pilot, settings, "#settings-stage")
            await wait_for(pilot, lambda key=key: key in settings._staged)
        await activate(pilot, settings, "#settings-apply")
        await wait_for(
            pilot, lambda: not settings.mutation_pending and not settings._operation_active
        )
        assert TaskCreationPreferences.from_settings(
            settings_for(context)
        ).repeat == TaskRepeatPreferences(TaskRepeatKind.AFTER_COMPLETION_WEEKLY, 2, 3)
        settings.action_close()
        app.offline = False
        app._open_area("tasks")
        await wait_for(pilot, lambda: isinstance(app.screen, TasksScreen) and app.screen.is_mounted)
        tasks = app.screen
        await wait_for(pilot, lambda: not tasks._operation_active)
        tasks.clock = lambda: datetime(2030, 1, 1, 9, tzinfo=UTC)
        await activate(pilot, tasks, "#tasks-create")
        await wait_for(
            pilot, lambda: isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted
        )
        editor = app.screen
        await pilot.pause()
        assert editor.query_one("#task-edit-repeat", Select).value == "after_completion_weekly"
        assert editor.query_one("#task-repeat-options").display
        editor.query_one("#task-edit-subject", Input).value = "Synthetic after-completion task"
        settings_for(context).set("tasks.default_repeat", "daily")
        assert editor.query_one("#task-edit-repeat", Select).value == "after_completion_weekly"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: len(gateway.creates) == 1 and not tasks._operation_active)
        assert gateway.creates[0].task.recurrence == TaskRepeatPreferences(
            TaskRepeatKind.AFTER_COMPLETION_WEEKLY, 2, 3
        ).rule(date(2030, 1, 1))
        assert context._gateway is None and len(gateway.creates) == 1
    context.close()


@pytest.mark.parametrize("failure", ["read", "state"])
def test_accepted_task_completion_result_failure_never_replays_mutation(
    projected_gateway, monkeypatch, failure
):
    gateway = projected_gateway
    item = Task(
        account=gateway.account,
        id="synthetic-series-before-result-failure",
        changekey="before",
        subject="Synthetic accepted completion",
        status="NotStarted",
        percent_complete=Decimal(0),
    )
    reference = gateway._remember("task", item)
    writes, reads = [], []

    def save(value, *, update_fields):
        assert "complete_date" not in update_fields
        writes.append(value.id)
        value.id = "synthetic-completed-after-result-failure"
        value.changekey = "after"

    def fetch(*args, **kwargs):
        reads.append(kwargs["ids"])
        raise TimeoutError("Synthetic result read timeout")

    original_put = gateway.state.put_reference

    def put(local_reference, kind, remote_id, **kwargs):
        if remote_id == "synthetic-completed-after-result-failure" and failure == "state":
            raise RuntimeError("Synthetic state failure")
        return original_put(local_reference, kind, remote_id, **kwargs)

    monkeypatch.setattr(Task, "save", save)
    monkeypatch.setattr(Account, "fetch", fetch)
    monkeypatch.setattr(gateway.state, "put_reference", put)
    with pytest.raises(MutationResultUnavailableError, match="accepted") as caught:
        TaskService(gateway).complete(reference)
    error = caught.value.as_dict()
    assert error["exit_code"] == ExitCode.PARTIAL
    assert error["retry_safe"] is False and error["mutation_uncertain"] is True
    assert reference in error["suggestion"] and "refresh" in error["suggestion"]
    assert writes == ["synthetic-series-before-result-failure"]
    assert len(reads) == (1 if failure == "read" else 0)
    assert gateway.state.resolve_reference(reference, "task")["ews_id"] == writes[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_uncertain_completion_review_button_only_refreshes_reads(tmp_path, monkeypatch, size):
    from textual.widgets import Button, Static

    from gucmail.tui.app import GUCmailApp
    from gucmail.tui.area_common import ConfirmationScreen
    from gucmail.tui.pim_screens import TasksScreen

    class AcceptedResultFailure:
        def __init__(self):
            self.reads = 0
            self.writes = []
            self.task = TaskInfo("synthetic:task", "Synthetic recurring follow-up", "not_started")

        def search_tasks(self, query, **kwargs):
            self.reads += 1
            return (self.task,)

        def get_task(self, reference):
            assert reference == self.task.reference
            return self.task

        def update_task(self, plan):
            self.writes.append(plan)
            raise MutationResultUnavailableError(
                "Synthetic accepted completion result is unavailable.",
                suggestion="Inspect the current series before deciding to write again.",
            )

    context = _context(tmp_path)
    gateway = AcceptedResultFailure()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app.offline = False
        app._open_area("tasks")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, TasksScreen)
                and bool(app.screen._tasks)
                and not app.screen._operation_active
            ),
        )
        tasks = app.screen
        await activate(pilot, tasks, "#tasks-complete")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert gateway.writes == []
        await pilot.press("y")
        await wait_for(
            pilot,
            lambda: app.screen is tasks and not tasks._operation_active and bool(gateway.writes),
        )
        notice = str(tasks.query_one("#area-state", Static).content)
        assert "may have changed 1" in notice and "does not repeat the mutation" in notice
        retry = tasks.query_one("#area-retry", Button)
        assert retry.display and str(retry.label) == "Refresh for review"
        before = gateway.reads
        await activate(pilot, tasks, "#area-retry")
        await wait_for(pilot, lambda: gateway.reads == before + 1 and not tasks._operation_active)
        assert len(gateway.writes) == 1
        assert context._gateway is None
    context.close()
