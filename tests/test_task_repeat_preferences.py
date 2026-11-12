"""Shared task recurrence defaults and actual installed EWS wire validation."""

import json
from datetime import UTC, date, datetime

import pytest
from exchangelib.version import Build, Version
from textual.app import App
from textual.widgets import Button, Input, Select, Static

from gucmail.errors import ConfigurationError
from gucmail.ews.adapter import EWSGateway
from gucmail.models import TaskInfo
from gucmail.services.pim_preferences import TaskCreationPreferences
from gucmail.services.task_recurrence import (
    TASK_REPEAT_SETTINGS,
    TaskRepeatKind,
    TaskRepeatPreferences,
)
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.pim_screens import TaskEditorScreen, TasksScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_compose_preferences import wait_for
from test_pim_creation_preferences import RecordingGateway, invoke, settings_for
from test_settings_secondpass import _context

NOW = datetime(2026, 10, 3, 23, 30, tzinfo=UTC)
ANCHOR = date(2026, 10, 4)
VALUES = dict(zip(TASK_REPEAT_SETTINGS, ("weekly", 2, 3), strict=True))


def configure(context):
    service = settings_for(context, revisions=InMemoryRevisionStore())
    session = service.begin()
    for key, value in VALUES.items():
        session.set(key, value)
    session.apply()
    return service


def test_task_repeat_persistence_cancel_scope_transfer_reset_undo(tmp_path):
    context = _context(tmp_path)
    service = configure(context)
    expected = TaskRepeatPreferences(TaskRepeatKind.WEEKLY, 2, 3)
    before = context.paths.config_file.read_bytes()
    staged = service.begin()
    for key in TASK_REPEAT_SETTINGS:
        staged.reset(key)
    staged.cancel()
    assert context.paths.config_file.read_bytes() == before
    assert TaskCreationPreferences.from_settings(settings_for(context)).repeat == expected
    other = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert TaskCreationPreferences.from_settings(other).repeat == TaskRepeatPreferences()
    target = settings_for(_context(tmp_path / "import"))
    target.apply_import(target.plan_import(service.export(setting_ids=TASK_REPEAT_SETTINGS)))
    assert TaskCreationPreferences.from_settings(target).repeat == expected
    for key in TASK_REPEAT_SETTINGS:
        service.reset(key)
        assert service.value(key).effective_value == service.registry.get(key).default
        service.undo()
        assert service.value(key).effective_value == VALUES[key]
    context.close()


@pytest.mark.parametrize(
    "key,value",
    [
        ("tasks.default_repeat", "hourly"),
        ("tasks.default_repeat_interval", 0),
        ("tasks.default_repeat_interval", True),
        ("tasks.default_repeat_interval", 1.5),
        ("tasks.default_repeat_interval", 100),
        ("tasks.default_repeat_count", 0),
        ("tasks.default_repeat_count", 1000),
        ("tasks.default_repeat_count", 1.5),
        ("tasks.default_repeat_count", "nan"),
    ],
)
def test_invalid_task_repeat_defaults_do_not_change_config(tmp_path, key, value):
    context = _context(tmp_path)
    service = configure(context)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        service.set(key, value)
    assert context.paths.config_file.read_bytes() == before
    context.close()


def test_repeat_cross_validation_checks_effective_environment_and_joint_change(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = configure(context)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError, match="one year"):
        service.set("tasks.default_repeat", "yearly")
    assert context.paths.config_file.read_bytes() == before
    staged = service.begin()
    staged.set("tasks.default_repeat", "yearly")
    staged.set("tasks.default_repeat_interval", 1)
    staged.apply()
    assert TaskCreationPreferences.from_settings(service).repeat.kind is TaskRepeatKind.YEARLY
    before = context.paths.config_file.read_bytes()
    monkeypatch.setenv("GUCMAIL_SETTING_TASKS__DEFAULT_REPEAT_INTERVAL", "2")
    with pytest.raises(ConfigurationError, match="one year"):
        service.set("tasks.default_repeat_count", 5)
    assert context.paths.config_file.read_bytes() == before
    monkeypatch.delenv("GUCMAIL_SETTING_TASKS__DEFAULT_REPEAT_INTERVAL")
    monkeypatch.setenv("GUCMAIL_SETTING_TASKS__DEFAULT_REPEAT_COUNT", "7")
    assert TaskCreationPreferences.from_settings(service).repeat.count == 7
    context.close()


@pytest.mark.parametrize("kind", list(TaskRepeatKind)[1:])
@pytest.mark.parametrize("boundary", ["no_end", "numbered", "end_date"])
def test_saved_repeat_patterns_reach_real_task_xml_with_each_boundary(kind, boundary):
    prefs = TaskCreationPreferences(
        repeat=TaskRepeatPreferences(
            kind, 1 if kind is TaskRepeatKind.YEARLY else 2, 3 if boundary == "numbered" else None
        )
    )
    task = prefs.new_task(
        timezone="Africa/Cairo",
        now=NOW,
        repeat_until=date(2027, 10, 4) if boundary == "end_date" else None,
    )
    assert task.start_date == ANCHOR and task.due_date is None
    rule = task.recurrence
    assert rule["target"] == "task" and rule["boundary"]["type"] == boundary
    assert rule["boundary"]["start"] == ANCHOR.isoformat()
    if kind is TaskRepeatKind.WEEKLY:
        assert rule["pattern"]["weekdays"] == ["sunday"]
    if kind in {TaskRepeatKind.MONTHLY, TaskRepeatKind.YEARLY}:
        assert rule["pattern"]["day_of_month"] == 4
    recurrence = object.__new__(EWSGateway)._ews_recurrence(rule, target="task", start=ANCHOR)
    version = Version(build=Build(15, 1, 2375, 7))
    recurrence.clean(version=version)
    xml = recurrence.to_xml(version=version)
    names = [node.tag.rsplit("}", 1)[-1] for node in xml.iter()]
    assert names[0] == "Recurrence"
    assert {
        "no_end": "NoEndRecurrence",
        "numbered": "NumberedRecurrence",
        "end_date": "EndDateRecurrence",
    }[boundary] in names


def test_repeat_anchor_explicit_start_due_local_date_and_override_none():
    prefs = TaskCreationPreferences(repeat=TaskRepeatPreferences(TaskRepeatKind.WEEKLY))
    for kwargs, expected in [
        ({"timezone": "Africa/Cairo"}, ANCHOR),
        ({"timezone": "America/Los_Angeles"}, date(2026, 10, 3)),
        ({"timezone": "UTC", "start_date": date(2027, 1, 5)}, date(2027, 1, 5)),
        ({"timezone": "UTC", "due_date": date(2027, 1, 9)}, date(2027, 1, 9)),
    ]:
        task = prefs.new_task(now=NOW, **kwargs)
        assert task.recurrence["boundary"]["start"] == expected.isoformat()
    task = prefs.new_task(timezone="UTC", now=NOW, repeat=TaskRepeatPreferences())
    assert task.recurrence is None and task.start_date is None
    with pytest.raises(ConfigurationError):
        prefs.new_task(timezone="UTC", now=NOW, repeat_until=date(2026, 10, 2))


@pytest.mark.parametrize(
    "options,kind,boundary",
    [
        ([], "weekly", "numbered"),
        (["--repeat", "daily", "--repeat-interval", "5"], "daily", "numbered"),
        (["--repeat-forever"], "weekly", "no_end"),
        (["--repeat-until", "2027-02-01"], "weekly", "end_date"),
        (["--repeat-count", "1"], "weekly", "numbered"),
        (["--repeat", "none"], None, None),
    ],
)
def test_cli_repeat_overrides_preview_without_credentials_or_saved_changes(
    tmp_path, monkeypatch, options, kind, boundary
):
    from gucmail import cli

    context = _context(tmp_path)
    configure(context)
    before = context.paths.config_file.read_bytes()
    monkeypatch.setattr(cli, "_gateway", lambda *_: pytest.fail("Preview resolved a gateway"))
    result = invoke(tmp_path, "tasks", ["--start", "2027-01-05", *options, "--dry-run"])
    assert result.exit_code == 0, result.output
    rule = json.loads(result.output)["data"]["task"]["recurrence"]
    if kind is None:
        assert rule is None
    else:
        assert rule["target"] == "task" and rule["pattern"]["type"] == kind
        assert rule["boundary"]["type"] == boundary
        if "--repeat-interval" in options:
            assert rule["pattern"]["interval"] == 5
        if "--repeat-count" in options:
            assert rule["boundary"]["occurrences"] == 1
        if "--repeat-until" in options:
            assert rule["boundary"]["end"] == "2027-02-01"
    assert context.paths.config_file.read_bytes() == before
    context.close()


@pytest.mark.parametrize(
    "options",
    [
        ["--repeat-count", "0"],
        ["--repeat-count", "1000"],
        ["--repeat-interval", "100"],
        ["--repeat", "yearly"],
        ["--repeat", "none", "--repeat-count", "3"],
        ["--repeat-count", "3", "--repeat-forever"],
        ["--repeat-forever", "--repeat-until", "2027-01-07"],
        ["--repeat-until", "2027-01-04"],
    ],
)
def test_invalid_cli_repeat_fails_before_gateway(tmp_path, monkeypatch, options):
    from gucmail import cli

    context = _context(tmp_path)
    configure(context)
    before = context.paths.config_file.read_bytes()
    monkeypatch.setattr(cli, "_gateway", lambda *_: pytest.fail("Invalid plan resolved gateway"))
    result = invoke(tmp_path, "tasks", ["--start", "2027-01-05", *options, "--dry-run"])
    assert result.exit_code == 3, result.output
    assert context.paths.config_file.read_bytes() == before
    context.close()


def test_approved_cli_create_uses_exact_repeat_once(tmp_path, monkeypatch):
    from gucmail import cli

    context = _context(tmp_path)
    configure(context)
    gateway = RecordingGateway()
    monkeypatch.setattr(cli, "_gateway", lambda *_: gateway)
    result = invoke(tmp_path, "tasks", ["--start", "2027-01-05", "--repeat-forever"], yes=True)
    assert result.exit_code == 0, result.output
    assert len(gateway.creates) == 1
    assert gateway.creates[0].task.recurrence == TaskRepeatPreferences(
        TaskRepeatKind.WEEKLY, 2
    ).rule(date(2027, 1, 5))
    context.close()


def test_cli_repeat_anchor_uses_effective_account_zone(tmp_path, monkeypatch):
    from gucmail import cli
    from gucmail.services import pim_preferences

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    context = _context(tmp_path)
    service = configure(context)
    monkeypatch.setattr(pim_preferences, "datetime", Clock)
    monkeypatch.setattr(cli, "_gateway", lambda *_: pytest.fail("Preview resolved gateway"))
    service.set("calendar.timezone", "America/Los_Angeles")
    result = invoke(tmp_path, "tasks", ["--dry-run"])
    assert result.exit_code == 0, result.output
    task = json.loads(result.output)["data"]["task"]
    assert task["start_date"] == "2026-10-03"
    assert task["recurrence"]["pattern"]["weekdays"] == ["saturday"]
    context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_repeat_defaults_and_fresh_form_preservation(
    tmp_path, monkeypatch, size
):
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

        async def activate(selector):
            button = screen.query_one(selector, Button)
            await wait_for(pilot, lambda: not button.disabled and not button.has_class("-active"))
            button.focus()
            await pilot.pause()
            await pilot.press("enter")

        async def stage():
            for key, value in VALUES.items():
                assert screen.action_select_setting(key)
                await pilot.pause()
                if isinstance(value, int):
                    screen.query_one("#settings-editor", Input).value = str(value)
                else:
                    screen.query_one("#settings-choice", Select).value = value
                await activate("#settings-stage")
                await wait_for(pilot, lambda key=key: key in screen._staged)

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
        assert TaskCreationPreferences.from_settings(settings_for(context)).repeat.count == 3
        # Blank is a deliberate no-end choice, never Unknown or zero occurrences.
        assert screen.action_select_setting("tasks.default_repeat_count")
        screen.query_one("#settings-editor", Input).value = ""
        await activate("#settings-stage")
        await wait_for(
            pilot,
            lambda: (
                "tasks.default_repeat_count" in screen._staged
                and screen._staged["tasks.default_repeat_count"] is None
                and "Value: No end" in str(screen.query_one("#settings-detail", Static).content)
            ),
        )
        assert "Value: No end" in str(screen.query_one("#settings-detail", Static).content)
        await activate("#settings-cancel")
        await wait_for(pilot, lambda: not screen._staged)
        screen.action_close()
        await pilot.pause()
        app.offline = False
        app._open_area("tasks")
        await wait_for(pilot, lambda: isinstance(app.screen, TasksScreen) and app.screen.is_mounted)
        tasks = app.screen
        await wait_for(pilot, lambda: not tasks._operation_active)
        tasks.clock = lambda: NOW
        # Workspace opened with Cairo; a later saved zone applies only to new forms.
        settings_for(context).set("calendar.timezone", "America/Los_Angeles")
        tasks.action_create()
        await wait_for(
            pilot, lambda: isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted
        )
        editor = app.screen
        await pilot.pause()
        assert editor.query_one("#task-edit-start", Input).value == "2026-10-03"
        assert editor.query_one("#task-edit-repeat", Select).value == "weekly"
        assert editor.query_one("#task-repeat-interval", Input).value == "2"
        assert editor.query_one("#task-repeat-count", Input).value == "3"
        target = editor.query_one("#task-repeat-count", Input)
        editor.query_one("#task-edit-subject", Input).focus()
        for _ in range(20):
            await pilot.press("tab")
            if editor.focused is target:
                break
        await pilot.pause()
        assert editor.focused is target
        assert target.region.intersection(editor.query_one("#task-editor-fields").region).height > 0
        editor.query_one("#task-edit-subject", Input).value = "Reviewed repeat"
        editor.query_one("#task-repeat-count", Input).value = "5"
        settings_for(context).set("tasks.default_repeat", "daily")
        assert editor.query_one("#task-edit-repeat", Select).value == "weekly"
        expected = TaskRepeatPreferences(TaskRepeatKind.WEEKLY, 2, 5).rule(date(2026, 10, 3))
        editor.action_save()
        await wait_for(pilot, lambda: len(gateway.creates) == 1 and not tasks._operation_active)
        assert gateway.creates[0].task.recurrence == expected
        tasks.action_create_prefilled("Source subject", source_reference="synthetic:mail")
        await wait_for(
            pilot, lambda: isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted
        )
        await pilot.pause()
        assert app.screen.query_one("#task-edit-repeat", Select).value == "daily"
        assert app.screen.task_info.body == "Source message: synthetic:mail"
        app.screen.action_cancel()
        assert len(gateway.creates) == 1 and context._gateway is None
    context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("creating", [True, False])
async def test_existing_complex_task_rule_preserved_in_advanced_form(creating):
    rule = {
        "pattern": {
            "type": "weekly",
            "interval": 2,
            "weekdays": ["monday", "wednesday"],
            "first_day_of_week": "monday",
        },
        "boundary": {"type": "no_end", "start": ANCHOR.isoformat()},
    }
    task = TaskInfo(
        "synthetic:task", "Complex original", "not_started", start_date=ANCHOR, recurrence=rule
    )
    app = App()
    async with app.run_test(size=(80, 24)) as pilot:
        editor = TaskEditorScreen(task, creating=creating)
        app.push_screen(editor)
        await pilot.pause()
        assert editor.query_one("#task-edit-repeat", Select).value == "advanced"
        value = editor._value()
        assert value.recurrence["pattern"]["weekdays"] == ["monday", "wednesday"]
        assert value.recurrence["target"] == "task"
        assert task.recurrence == rule
        editor.action_cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["daily", "weekly", "monthly", "yearly"])
async def test_normal_task_repeat_form_serializes_as_task(kind):
    app = App()
    task = TaskInfo(
        "synthetic:task", "Synthetic repeating task", "not_started", start_date=date(2026, 10, 4)
    )
    async with app.run_test(size=(80, 24)) as pilot:
        editor = TaskEditorScreen(task)
        app.push_screen(editor)
        await pilot.pause()
        editor.query_one("#task-edit-repeat", Select).value = kind
        editor.query_one("#task-repeat-count", Input).value = "3"
        await pilot.pause()
        value = editor._value()
        recurrence = object.__new__(EWSGateway)._ews_recurrence(
            value.recurrence, target="task", start=value.start_date
        )
        version = Version(build=Build(15, 1, 2375, 7))
        recurrence.clean(version=version)
        assert recurrence.to_xml(version=version) is not None
        assert value.recurrence["target"] == "task"
        editor.action_cancel()
