"""Fresh-item preferences through canonical persistence, CLI and mounted editors."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from textual.widgets import Button, Input, Select, Static
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import ConfigurationError
from gucmail.models import Address, Importance
from gucmail.services.calendar import CalendarService, normalize_event
from gucmail.services.pim_preferences import CalendarCreationPreferences, TaskCreationPreferences
from gucmail.services.tasks import TaskService
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.pim_screens import (
    CalendarEditorScreen,
    CalendarScreen,
    TaskEditorScreen,
    TasksScreen,
)
from gucmail.tui.settings_form import SettingRow
from gucmail.tui.settings_screen import SettingsScreen
from test_cli import _environment
from test_compose_preferences import wait_for
from test_settings_secondpass import _context

NOW = datetime(2026, 10, 3, 23, 30, tzinfo=UTC)
VALUES = {
    "calendar.default_duration_minutes": 90,
    "calendar.default_reminder_minutes": 15,
    "calendar.default_availability": "free",
    "tasks.default_due_days": 2,
    "tasks.default_reminder_minutes": 30,
    "tasks.default_importance": "high",
}


def settings_for(context, **kwargs):
    return SettingsService(
        context.config_store,
        context=SettingsContext(account_id=context.config.email),
        **kwargs,
    )


def configure(context):
    service = settings_for(context, revisions=InMemoryRevisionStore())
    session = service.begin()
    for identifier, value in VALUES.items():
        session.set(identifier, value)
    session.apply()
    return service


def test_pim_preferences_stage_cancel_reopen_scope_transfer_reset_undo(tmp_path):
    context = _context(tmp_path)
    service = configure(context)
    calendar = CalendarCreationPreferences(90, 15, "free")
    task = TaskCreationPreferences(2, 30, Importance.HIGH)
    assert CalendarCreationPreferences.from_settings(service) == calendar
    assert TaskCreationPreferences.from_settings(service) == task
    before = context.paths.config_file.read_bytes()
    session = service.begin()
    for identifier in VALUES:
        session.reset(identifier)
    session.cancel()
    assert context.paths.config_file.read_bytes() == before
    reopened = settings_for(context)
    assert CalendarCreationPreferences.from_settings(reopened) == calendar
    assert TaskCreationPreferences.from_settings(reopened) == task
    target = settings_for(_context(tmp_path / "import"))
    target.apply_import(target.plan_import(service.export(setting_ids=tuple(VALUES))))
    assert CalendarCreationPreferences.from_settings(target) == calendar
    assert TaskCreationPreferences.from_settings(target) == task
    for identifier in VALUES:
        service.reset(identifier)
        assert service.value(identifier).effective_value == service.registry.get(identifier).default
        service.undo()
        assert service.value(identifier).effective_value == VALUES[identifier]
    isolated = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert CalendarCreationPreferences.from_settings(isolated) == CalendarCreationPreferences()
    assert TaskCreationPreferences.from_settings(isolated) == TaskCreationPreferences()


@pytest.mark.parametrize(
    "identifier,value",
    [
        ("calendar.default_duration_minutes", 0),
        ("calendar.default_duration_minutes", True),
        ("calendar.default_duration_minutes", 1.5),
        ("calendar.default_reminder_minutes", -1),
        ("calendar.default_reminder_minutes", 1.5),
        ("calendar.default_reminder_minutes", 525601),
        ("calendar.default_availability", "unknown"),
        ("tasks.default_due_days", 0.5),
        ("tasks.default_due_days", 3651),
        ("tasks.default_due_days", True),
        ("tasks.default_reminder_minutes", "nan"),
        ("tasks.default_reminder_minutes", -1),
        ("tasks.default_importance", "urgent"),
    ],
)
def test_invalid_pim_defaults_preserve_configuration(tmp_path, identifier, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        settings_for(context).set(identifier, value)
    assert context.paths.config_file.read_bytes() == before


def test_disabled_zero_and_environment_override_reminders(tmp_path, monkeypatch):
    service = configure(_context(tmp_path))
    service.set("calendar.default_reminder_minutes", "")
    service.set("tasks.default_due_days", "none")
    service.set("tasks.default_reminder_minutes", 0)
    assert CalendarCreationPreferences.from_settings(service).reminder_minutes is None
    assert TaskCreationPreferences.from_settings(service).due_days is None
    assert TaskCreationPreferences.from_settings(service).reminder_minutes == 0
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__DEFAULT_REMINDER_MINUTES", "10")
    assert CalendarCreationPreferences.from_settings(service).reminder_minutes == 10


def test_calendar_elapsed_duration_dst_all_day_and_explicit_values():
    preferences = CalendarCreationPreferences(90, 15, "free")
    start = datetime.fromisoformat("2026-11-01T01:30:00-04:00")
    event = preferences.new_event(timezone="America/New_York", subject="DST", start=start)
    assert event.end.astimezone(UTC) - event.start.astimezone(UTC) == timedelta(minutes=90)
    assert event.end.hour == 2 and event.end.utcoffset() == timedelta(hours=-5)
    assert event.reminder_minutes == 15 and event.availability == "free"
    explicit = preferences.new_event(
        timezone="UTC",
        subject="Explicit",
        start=NOW,
        end=NOW + timedelta(minutes=20),
        reminder_minutes=None,
        availability="busy",
    )
    assert explicit.end - explicit.start == timedelta(minutes=20)
    assert explicit.reminder_minutes is None and explicit.availability == "busy"
    day = preferences.new_event(
        timezone="America/New_York",
        subject="All day",
        all_day=True,
        start=datetime.fromisoformat("2026-11-01T00:00:00-04:00"),
    )
    assert normalize_event(day).end.astimezone(UTC) - day.start.astimezone(UTC) == timedelta(
        hours=25
    )
    with pytest.raises(ConfigurationError, match="midnight"):
        normalize_event(
            preferences.new_event(timezone="UTC", subject="Bad", start=NOW, all_day=True)
        )
    with pytest.raises(ConfigurationError, match="occurs twice"):
        preferences.new_event(timezone="America/New_York", start=datetime(2026, 11, 1, 1, 30))


def test_task_local_dates_elapsed_reminders_and_explicit_clears():
    preferences = TaskCreationPreferences(2, 30, Importance.HIGH)
    task = preferences.new_task(timezone="Africa/Cairo", now=NOW, subject="Local")
    assert task.due_date == date(2026, 10, 6)
    assert task.reminder_at == NOW + timedelta(minutes=30)
    assert task.importance is Importance.HIGH
    started = preferences.new_task(timezone="UTC", now=NOW, start_date=date(2027, 1, 5))
    assert started.due_date == date(2027, 1, 7)
    explicit = preferences.new_task(
        timezone="UTC",
        now=NOW,
        due_date=None,
        reminder_at=None,
        importance=Importance.NORMAL,
    )
    assert explicit.due_date is None and explicit.reminder_at is None
    assert explicit.importance is Importance.NORMAL
    default = TaskCreationPreferences().new_task(timezone="UTC", now=NOW)
    assert default.due_date is None and default.reminder_at is None


def invoke(tmp_path, area, options, *, yes=False):
    return CliRunner().invoke(
        cli.app,
        [
            "--json",
            "--non-interactive",
            *(["--yes"] if yes else []),
            area,
            "create",
            "--subject",
            "Synthetic",
            *options,
        ],
        env=_environment(tmp_path),
    )


def test_cli_saved_creation_defaults_and_explicit_overrides_are_credential_free(
    tmp_path, monkeypatch
):
    configure(_context(tmp_path))
    monkeypatch.setattr(cli, "_gateway", lambda *_: pytest.fail("Preview resolved credentials"))
    result = invoke(tmp_path, "calendar", ["--start", NOW.isoformat(), "--dry-run"])
    assert result.exit_code == 0, result.output
    event = json.loads(result.output)["data"]["event"]
    assert datetime.fromisoformat(event["end"]) - datetime.fromisoformat(
        event["start"]
    ) == timedelta(minutes=90)
    assert event["reminder_minutes"] == 15 and event["availability"] == "free"
    result = invoke(
        tmp_path,
        "calendar",
        [
            "--start",
            NOW.isoformat(),
            "--duration-minutes",
            "20",
            "--no-reminder",
            "--availability",
            "busy",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    event = json.loads(result.output)["data"]["event"]
    assert datetime.fromisoformat(event["end"]) - datetime.fromisoformat(
        event["start"]
    ) == timedelta(minutes=20)
    assert event["reminder_minutes"] is None and event["availability"] == "busy"
    result = invoke(tmp_path, "tasks", ["--start", "2027-01-05", "--dry-run"])
    assert result.exit_code == 0, result.output
    task = json.loads(result.output)["data"]["task"]
    assert task["due_date"] == "2027-01-07" and task["importance"] == "high"
    assert task["reminder_at"] is not None
    result = invoke(
        tmp_path, "tasks", ["--no-due", "--no-reminder", "--importance", "normal", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    task = json.loads(result.output)["data"]["task"]
    assert (
        task["due_date"] is None and task["reminder_at"] is None and task["importance"] == "normal"
    )


@pytest.mark.parametrize(
    "area,options",
    [
        (
            "calendar",
            [
                "--start",
                NOW.isoformat(),
                "--end",
                (NOW + timedelta(hours=1)).isoformat(),
                "--duration-minutes",
                "20",
            ],
        ),
        ("calendar", ["--start", NOW.isoformat(), "--reminder-minutes", "0", "--no-reminder"]),
        ("calendar", ["--start", NOW.isoformat(), "--availability", "unknown"]),
        ("tasks", ["--due", "2027-01-05", "--no-due"]),
        ("tasks", ["--reminder-at", NOW.isoformat(), "--no-reminder"]),
        ("tasks", ["--start", "2027-01-05", "--due", "2027-01-04"]),
    ],
)
def test_invalid_cli_creation_fails_before_credentials(tmp_path, monkeypatch, area, options):
    monkeypatch.setattr(
        cli, "_gateway", lambda *_: pytest.fail("Invalid create resolved credentials")
    )
    result = invoke(tmp_path, area, [*options, "--dry-run"])
    assert result.exit_code == 3, result.output


@pytest.mark.parametrize(
    "area,options", [("calendar", ["--start", NOW.isoformat()]), ("tasks", [])]
)
def test_cli_creation_needs_confirmation_before_credentials(tmp_path, monkeypatch, area, options):
    monkeypatch.setattr(
        cli, "_gateway", lambda *_: pytest.fail("Unconfirmed create resolved credentials")
    )
    result = invoke(tmp_path, area, options)
    assert result.exit_code == 9, result.output


class RecordingGateway:
    def __init__(self):
        self.creates = []

    def create_event(self, plan):
        self.creates.append(plan)
        return replace(plan.event, reference="created-event")

    def create_task(self, plan):
        self.creates.append(plan)
        return replace(plan.task, reference="created-task")

    def list_events(self, **kwargs):
        return ()

    def search_tasks(self, query, **kwargs):
        return ()


@pytest.mark.parametrize(
    "area,options",
    [
        ("calendar", ["--start", NOW.isoformat(), "--no-invitations"]),
        ("tasks", ["--no-due", "--no-reminder"]),
    ],
)
def test_cli_approved_creation_executes_exact_plan_once(tmp_path, monkeypatch, area, options):
    configure(_context(tmp_path))
    gateway = RecordingGateway()
    monkeypatch.setattr(cli, "_gateway", lambda *_: gateway)
    result = invoke(tmp_path, area, options, yes=True)
    assert result.exit_code == 0, result.output
    assert len(gateway.creates) == 1
    plan = gateway.creates[0]
    if area == "calendar":
        assert plan.event.reminder_minutes == 15 and plan.event.availability == "free"
        assert plan.invitations.value == "send_none"
    else:
        assert plan.task.due_date is None and plan.task.reminder_at is None
        assert plan.task.importance is Importance.HIGH


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_cancel_apply_then_fresh_pim_forms(tmp_path, monkeypatch, size):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    gateway = RecordingGateway()
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        before = context.paths.config_file.read_bytes()
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen

        async def activate(identifier):
            button = screen.query_one(identifier, Button)
            await wait_for(
                pilot,
                lambda: (
                    not button.has_class("-active")
                    and not button.disabled
                    and not screen._operation_active
                ),
            )
            button.focus()
            await pilot.pause()
            await pilot.press("enter")

        async def stage():
            for identifier, value in VALUES.items():
                assert screen.action_select_setting(identifier)
                await pilot.pause()
                if isinstance(value, int):
                    screen.query_one("#settings-editor", Input).value = str(value)
                else:
                    screen.query_one("#settings-choice", Select).value = value
                await activate("#settings-stage")
                await wait_for(
                    pilot,
                    lambda identifier=identifier: (
                        identifier in screen._staged
                        and not screen._operation_active
                        and not screen._render_pending
                    ),
                )

        await stage()
        await activate("#settings-cancel")
        await wait_for(pilot, lambda: not screen._staged)
        assert context.paths.config_file.read_bytes() == before
        await stage()
        await activate("#settings-apply")
        await wait_for(
            pilot,
            lambda: (
                not screen._staged and not screen.mutation_pending and not screen._operation_active
            ),
        )
        assert all(
            settings_for(context).value(identifier).effective_value == value
            for identifier, value in VALUES.items()
        )
        assert (
            CalendarCreationPreferences.from_settings(settings_for(context)).duration_minutes == 90
        )
        screen.action_close()
        await pilot.pause()
        app.offline = False  # Normal PIM workspace with only the synthetic gateway above.
        app._open_area("calendar")
        await wait_for(
            pilot, lambda: isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        calendar = app.screen
        calendar.action_create()
        await wait_for(
            pilot, lambda: isinstance(app.screen, CalendarEditorScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        editor = app.screen
        start = datetime.fromisoformat(editor.query_one("#calendar-edit-start", Input).value)
        end = datetime.fromisoformat(editor.query_one("#calendar-edit-end", Input).value)
        assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(minutes=90)
        assert editor.query_one("#calendar-edit-reminder", Input).value == "15"
        assert editor.query_one("#calendar-edit-availability", Select).value == "free"
        settings_for(context).set("calendar.default_duration_minutes", 20)
        assert datetime.fromisoformat(editor.query_one("#calendar-edit-end", Input).value) == end
        editor.action_cancel()
        await pilot.pause()
        calendar.action_create_prefilled(
            [Address("guest@example.test")], subject="From message", start=NOW
        )
        await wait_for(
            pilot, lambda: isinstance(app.screen, CalendarEditorScreen) and app.screen.is_mounted
        )
        assert app.screen.event.subject == "From message"
        assert app.screen.event.end.astimezone(UTC) - NOW == timedelta(minutes=20)
        assert app.screen.event.required_attendees[0].email == "guest@example.test"
        app.screen.action_cancel()
        await pilot.pause()
        calendar.action_close()
        await pilot.pause()
        app._open_area("tasks")
        await wait_for(pilot, lambda: isinstance(app.screen, TasksScreen) and app.screen.is_mounted)
        await pilot.pause()
        tasks = app.screen
        tasks.offline = False
        tasks.action_create()
        await wait_for(
            pilot, lambda: isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        editor = app.screen
        assert editor.query_one("#task-edit-importance", Select).value == "high"
        assert editor.query_one("#task-edit-due", Input).value
        assert editor.query_one("#task-edit-reminder", Input).value
        editor.action_cancel()
        await pilot.pause()
        tasks.action_create_prefilled("Original subject", source_reference="mail-reference")
        await wait_for(
            pilot, lambda: isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted
        )
        assert app.screen.task_info.subject == "Follow up: Original subject"
        assert app.screen.task_info.body == "Source message: mail-reference"
        assert app.screen.task_info.importance is Importance.HIGH
        app.screen.action_cancel()
        assert gateway.creates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_clear_pim_defaults_has_explicit_labels(tmp_path, size):
    context = _context(tmp_path)
    service = configure(context)
    app = GUCmailApp(context, offline=True)
    cleared = {
        "calendar.default_reminder_minutes": "Reminder off",
        "tasks.default_due_days": "No due date",
        "tasks.default_reminder_minutes": "Reminder off",
    }
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, SettingsScreen)
                and app.screen._catalog
                and not app.screen._operation_active
            ),
        )
        screen = app.screen

        async def activate(identifier):
            button = screen.query_one(identifier, Button)
            await wait_for(pilot, lambda: not button.has_class("-active") and not button.disabled)
            button.focus()
            await pilot.pause()
            await pilot.press("enter")

        async def stage_clears():
            for identifier, label in cleared.items():
                assert screen.action_select_setting(identifier)
                await wait_for(
                    pilot,
                    lambda identifier=identifier: (
                        screen._selected_setting_id == identifier
                        and screen.query_one("#settings-editor", Input).is_mounted
                    ),
                )
                screen.query_one("#settings-editor", Input).value = ""
                await activate("#settings-stage")
                await wait_for(
                    pilot,
                    lambda identifier=identifier: (
                        identifier in screen._staged
                        and not screen._operation_active
                        and not screen._render_pending
                    ),
                )
                assert screen._staged[identifier] is None
                detail = str(screen.query_one("#settings-detail", Static).content)
                assert f"Value: {label}" in detail
                assert f"Default: {label}" in detail
                assert "Unknown" not in detail
                assert any(
                    row.setting_id == identifier and label in row.value
                    for row in screen.query(SettingRow)
                )

        before = context.paths.config_file.read_bytes()
        await stage_clears()
        await activate("#settings-cancel")
        await wait_for(pilot, lambda: not screen._staged and not screen._operation_active)
        assert context.paths.config_file.read_bytes() == before
        assert all(
            service.value(identifier).effective_value == VALUES[identifier]
            for identifier in cleared
        )
        await stage_clears()
        await activate("#settings-apply")
        await wait_for(
            pilot,
            lambda: (
                not screen._staged and not screen._operation_active and not screen.mutation_pending
            ),
        )
        assert all(
            settings_for(context).value(identifier).effective_value is None
            for identifier in cleared
        )
        screen.action_close()
        await wait_for(pilot, lambda: app.screen is not screen)
        app._open_area("settings")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, SettingsScreen)
                and app.screen._catalog
                and not app.screen._operation_active
            ),
        )
        screen = app.screen
        after = context.paths.config_file.read_bytes()
        for identifier, label in cleared.items():
            assert screen.action_select_setting(identifier)
            assert f"Value: {label}" in str(screen.query_one("#settings-detail", Static).content)
            assert screen.query_one("#settings-editor", Input).value == ""
        assert screen.action_select_setting("storage.issue_warning_mb")
        assert "Value: Unknown" in str(screen.query_one("#settings-detail", Static).content)
        assert context.paths.config_file.read_bytes() == after
        assert context._gateway is None
        for identifier in cleared:
            service.set(identifier, 0)
        screen.action_refresh()
        await wait_for(
            pilot,
            lambda: (
                not screen._operation_active
                and all(
                    screen._settings_by_id[identifier].effective_value == 0
                    for identifier in cleared
                )
            ),
        )
        for identifier in cleared:
            assert screen.action_select_setting(identifier)
            assert "Value: 0" in str(screen.query_one("#settings-detail", Static).content)
    context.close()


def test_complete_create_plans_and_imported_records_are_never_reseeded():
    original_event = CalendarCreationPreferences().new_event(
        timezone="UTC",
        subject="Existing",
        start=NOW,
        end=NOW + timedelta(minutes=17),
        reminder_minutes=None,
        availability="busy",
    )
    original_task = TaskCreationPreferences().new_task(
        timezone="UTC",
        subject="Existing",
        due_date=None,
        reminder_at=None,
        importance=Importance.LOW,
    )
    assert CalendarService(object()).plan_create(original_event).event == original_event
    assert TaskService(object()).plan_create(original_task).task == original_task


@pytest.mark.asyncio
@pytest.mark.parametrize("area", ["calendar", "tasks"])
async def test_fresh_form_edits_survive_preference_changes_and_exact_fake_creation(
    tmp_path, monkeypatch, area
):
    context = _context(tmp_path)
    service = configure(context)
    gateway = RecordingGateway()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        app.offline = False
        app._open_area(area)
        await pilot.pause()
        workspace = app.screen
        await wait_for(pilot, lambda: workspace.is_mounted and not workspace._operation_active)
        workspace.action_create()
        await pilot.pause()
        editor = app.screen
        if area == "calendar":
            editor.query_one("#calendar-edit-subject", Input).value = "Reviewed event"
            editor.query_one("#calendar-edit-reminder", Input).value = ""
            editor.query_one("#calendar-edit-availability", Select).value = "busy"
            service.set("calendar.default_reminder_minutes", 40)
            service.set("calendar.default_availability", "tentative")
        else:
            editor.query_one("#task-edit-subject", Input).value = "Reviewed task"
            editor.query_one("#task-edit-due", Input).value = ""
            editor.query_one("#task-edit-reminder", Input).value = ""
            editor.query_one("#task-edit-importance", Select).value = "low"
            service.set("tasks.default_due_days", 7)
            service.set("tasks.default_importance", "normal")
        editor.action_save()
        await wait_for(pilot, lambda: len(gateway.creates) == 1 and not workspace._operation_active)
        plan = gateway.creates[0]
        if area == "calendar":
            assert plan.event.subject == "Reviewed event"
            assert plan.event.reminder_minutes is None and plan.event.availability == "busy"
        else:
            assert plan.task.subject == "Reviewed task"
            assert plan.task.reminder_at is None and plan.task.due_date is None
            assert plan.task.importance is Importance.LOW


def test_task_due_default_overflow_is_actionable():
    with pytest.raises(ConfigurationError, match="supported date range"):
        TaskCreationPreferences(2).new_task(timezone="UTC", start_date=date.max, now=NOW)
