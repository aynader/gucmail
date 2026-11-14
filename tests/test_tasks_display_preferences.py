"""Canonical Task preferences through bounded CLI, provider and real mounted UI paths."""

import json
from datetime import UTC, date, datetime, timedelta
from threading import Event

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Input, Select, Static
from typer.testing import CliRunner

from gucmail import cli
from gucmail.errors import ConfigurationError
from gucmail.models import Importance, TaskInfo, to_primitive
from gucmail.services.pim import PimPage
from gucmail.services.tasks import TaskQuery, TaskService, TaskStatus
from gucmail.services.tasks_display import (
    TASK_DISPLAY_SETTINGS,
    TaskDisplayPreferences,
    TaskSortOrder,
    TaskView,
)
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.pim_screens import TaskEditorScreen, TasksScreen, TasksServiceBackend
from gucmail.tui.settings_screen import SettingsScreen
from test_calendar_working_time import settings_for
from test_cli import _environment
from test_compose_preferences import wait_for
from test_pim import TasksMemoryGateway
from test_settings_secondpass import _context

NOW = datetime(2026, 10, 3, 23, 30, tzinfo=UTC)
TODAY = date(2026, 10, 4)


def tasks():
    return (
        TaskInfo(
            "a",
            "Zulu",
            "not_started",
            due_date=TODAY - timedelta(days=1),
            body="Original complete body\n" * 25,
            categories=("synthetic",),
            reminder_at=NOW,
            recurrence={"preserved": "synthetic original rule"},
        ),
        TaskInfo("b", "Alpha", "not_started", importance=Importance.HIGH),
        TaskInfo(
            "c",
            "Beta",
            "not_started",
            due_date=TODAY + timedelta(days=1),
            importance=Importance.LOW,
        ),
        TaskInfo("d", "Gamma", "completed", percent_complete=100, due_date=TODAY, completed_at=NOW),
        TaskInfo(
            "e",
            "Delta",
            "in_progress",
            percent_complete=25,
            due_date=TODAY,
            importance=Importance.HIGH,
            body="Full Delta body\n" * 25,
        ),
    )


class ReadOnlyTasks(TasksMemoryGateway):
    def __init__(self):
        super().__init__(tasks())
        self.calls = []

    def search_tasks_page(self, query, *, folder_reference, limit, offset=0):
        self.calls.append(("page", query, folder_reference, limit, offset))
        return PimPage(tuple(self.tasks.values())[:limit], offset, 7, offset + 7, "Synthetic page.")

    def get_task(self, reference):
        self.calls.append(("get", reference))
        return super().get_task(reference)


def configure(context, view="all", show=True, sort="due_date", zone="Africa/Cairo"):
    service = settings_for(context, revisions=InMemoryRevisionStore())
    session = service.begin()
    for key, value in zip(TASK_DISPLAY_SETTINGS, (view, show, sort, zone), strict=True):
        session.set(key, value)
    session.apply()
    return service


def invoke(root, arguments, *, env=None):
    return CliRunner().invoke(
        cli.app,
        ["--json", "--non-interactive", "tasks", *arguments],
        env={**_environment(root), **(env or {})},
    )


def test_settings_roundtrip_scope_transfer_reset_undo_and_environment(tmp_path, monkeypatch):
    context = _context(tmp_path)
    service = configure(context, "this_week", True, "importance", "Pacific/Kiritimati")
    expected = TaskDisplayPreferences(
        TaskView.THIS_WEEK, True, TaskSortOrder.IMPORTANCE, "Pacific/Kiritimati"
    )
    assert TaskDisplayPreferences.from_settings(service) == expected
    before = context.paths.config_file.read_bytes()
    session = service.begin()
    for key in TASK_DISPLAY_SETTINGS:
        session.reset(key)
    session.cancel()
    assert context.paths.config_file.read_bytes() == before
    assert TaskDisplayPreferences.from_settings(settings_for(context)) == expected
    isolated = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert TaskDisplayPreferences.from_settings(isolated) == TaskDisplayPreferences()
    target = settings_for(_context(tmp_path / "import"))
    target.apply_import(target.plan_import(service.export(setting_ids=TASK_DISPLAY_SETTINGS)))
    assert TaskDisplayPreferences.from_settings(target) == expected
    for key in TASK_DISPLAY_SETTINGS:
        service.reset(key)
        assert service.value(key).effective_value == service.registry.get(key).default
        service.undo()
        assert TaskDisplayPreferences.from_settings(service) == expected
    before = context.paths.config_file.read_bytes()
    monkeypatch.setenv("GUCMAIL_SETTING_TASKS__DEFAULT_VIEW", "completed")
    monkeypatch.setenv("GUCMAIL_SETTING_TASKS__SHOW_COMPLETED", "false")
    monkeypatch.setenv("GUCMAIL_SETTING_TASKS__SORT_ORDER", "subject")
    monkeypatch.setenv("GUCMAIL_TIMEZONE", "America/Los_Angeles")
    assert TaskDisplayPreferences.from_settings(service) == TaskDisplayPreferences(
        TaskView.COMPLETED, False, TaskSortOrder.SUBJECT, "America/Los_Angeles"
    )
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("tasks.default_view", "missing"),
        ("tasks.show_completed", "unknown"),
        ("tasks.sort_order", "body"),
        ("calendar.timezone", "Missing/Zone"),
    ],
)
def test_invalid_setting_preserves_saved_bytes(tmp_path, key, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        settings_for(context).set(key, value)
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize("view", list(TaskView))
@pytest.mark.parametrize("show", [False, True])
def test_cli_saved_views_push_restrictions_and_preserve_page_metadata_and_records(
    tmp_path, monkeypatch, view, show
):
    context = _context(tmp_path)
    configure(context, view, show)
    gateway = ReadOnlyTasks()
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    original_today = TaskDisplayPreferences.today
    monkeypatch.setattr(
        TaskDisplayPreferences, "today", lambda self, now=None: original_today(self, NOW)
    )
    original = to_primitive(gateway.tasks)
    exported = TaskService(gateway).export(gateway.tasks.values())
    before = context.paths.config_file.read_bytes()
    result = invoke(tmp_path, ["list", "--limit", "5", "--offset", "11", "--page"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    expected = {
        "open": {"a", "b", "c", "e"},
        "due": {"a", "e"},
        "today": {"d", "e"} if show else {"e"},
        "this_week": {"c", "d", "e"} if show else {"c", "e"},
        "overdue": {"a"},
        "completed": {"d"},
        "all": {"a", "b", "c", "d", "e"} if show else {"a", "b", "c", "e"},
    }[view]
    assert {row["reference"] for row in data["items"]} == expected
    assert (data["offset"], data["scanned"], data["next_offset"], data["residual"]) == (
        11,
        7,
        18,
        "Synthetic page.",
    )
    assert len(gateway.calls) == 1 and gateway.calls[0][-2:] == (5, 11)
    query = gateway.calls[0][1]
    assert query.exclude_completed == (
        view in {"open", "due", "overdue"} or (view != "completed" and not show)
    )
    if view == "completed":
        assert query.status is TaskStatus.COMPLETED
    if view in {"due", "today"}:
        assert query.due_before == TODAY + timedelta(days=1)
    if view == "this_week":
        assert (query.due_after, query.due_before) == (
            TODAY - timedelta(days=1),
            TODAY + timedelta(days=7),
        )
    if view == "overdue":
        assert query.overdue_at == TODAY
    assert to_primitive(gateway.tasks) == original
    assert TaskService(gateway).export(gateway.tasks.values()) == exported
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize(
    "sort,expected",
    [
        ("due_date", ["a", "e", "c", "b", "d"]),
        ("subject", ["b", "c", "e", "d", "a"]),
        ("importance", ["e", "b", "a", "d", "c"]),
        ("status", ["d", "e", "a", "c", "b"]),
    ],
)
def test_cli_list_and_search_share_saved_sort_with_ephemeral_overrides(
    tmp_path, monkeypatch, sort, expected
):
    context = _context(tmp_path)
    configure(context, sort=sort)
    gateway = ReadOnlyTasks()
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    before = context.paths.config_file.read_bytes()
    for arguments in (["list"], ["search", "needle"]):
        result = invoke(tmp_path, arguments)
        assert result.exit_code == 0, result.output
        assert [r["reference"] for r in json.loads(result.output)["data"]] == expected
    result = invoke(
        tmp_path, ["list", "--view", "completed", "--no-completed", "--sort", "subject"]
    )
    assert result.exit_code == 0 and [
        r["reference"] for r in json.loads(result.output)["data"]
    ] == ["d"]
    assert context.paths.config_file.read_bytes() == before and gateway.updates == []


def test_search_status_and_overdue_are_explicit_and_invalid_requests_never_connect(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    configure(context, "due", False)
    gateway = ReadOnlyTasks()
    monkeypatch.setattr(cli.core, "gateway", lambda *_: gateway)
    result = invoke(tmp_path, ["search", "--status", "completed"])
    assert result.exit_code == 0
    assert [r["reference"] for r in json.loads(result.output)["data"]] == ["d"]
    assert gateway.calls[-1][1] == TaskQuery(status=TaskStatus.COMPLETED)
    result = invoke(tmp_path, ["search", "needle", "--completed"])
    assert result.exit_code == 0 and len(json.loads(result.output)["data"]) == 5
    assert gateway.calls[-1][1].due_before is None  # saved Due view must not narrow search
    original_today = TaskDisplayPreferences.today
    monkeypatch.setattr(
        TaskDisplayPreferences, "today", lambda self, now=None: original_today(self, NOW)
    )
    result = invoke(tmp_path, ["search", "--overdue"])
    assert result.exit_code == 0 and gateway.calls[-1][1].overdue_at == TODAY
    assert [r["reference"] for r in json.loads(result.output)["data"]] == ["a"]
    calls = []
    monkeypatch.setattr(cli.core, "gateway", lambda *_: calls.append("unsafe"))
    for args in (
        ["search"],
        ["search", "--overdue", "--status", "completed"],
        ["list", "--view", "missing"],
        ["list", "--sort", "missing"],
    ):
        assert invoke(tmp_path, args).exit_code != 0
    for key in TASK_DISPLAY_SETTINGS:
        env = {
            "GUCMAIL_TIMEZONE"
            if key == "calendar.timezone"
            else "GUCMAIL_SETTING_" + key.upper().replace(".", "__"): "invalid"
        }
        assert invoke(tmp_path, ["list"], env=env).exit_code != 0
    assert calls == []


@pytest.mark.parametrize(
    "zone,today",
    [("Pacific/Kiritimati", date(2026, 10, 4)), ("America/Los_Angeles", date(2026, 10, 3))],
)
def test_backend_and_view_clock_use_configured_zone_before_filtering(zone, today):
    display = TaskDisplayPreferences(timezone=zone)
    assert display.today(NOW) == today
    gateway = ReadOnlyTasks()
    backend = TasksServiceBackend(
        TaskService(gateway), show_completed=False, timezone=zone, clock=lambda: NOW
    )
    for view in ("today", "due", "this_week", "overdue"):
        backend.list_tasks("", view, 5, cancel=Event(), progress=lambda _: None)
        assert gateway.calls[-1][1] == display.query(view=view, today=today)
    with pytest.raises(ConfigurationError):
        display.today(NOW.replace(tzinfo=None))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_apply_preserves_current_view_selection_details_and_editor(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    configure(context)
    gateway = ReadOnlyTasks()
    original = to_primitive(gateway.tasks)
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app.offline = False
        app._open_area("tasks")
        screen = app.screen
        assert isinstance(screen, TasksScreen)
        screen.clock = lambda: NOW
        await wait_for(pilot, lambda: len(screen._tasks) == 5 and not screen._operation_active)
        assert len(gateway.calls) == 1
        table = screen.query_one("#tasks-table", DataTable)
        table.focus()
        await wait_for(pilot, lambda: table.has_focus)
        await pilot.press("down", "space", "enter")
        await wait_for(
            pilot, lambda: screen._complete_detail_reference == "e" and not screen._operation_active
        )
        assert screen._selected == {"e"}
        before = context.paths.config_file.read_bytes()
        calls = len(gateway.calls)
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        settings = app.screen

        async def activate(identifier):
            button = settings.query_one(identifier, Button)
            await wait_for(pilot, lambda: not button.disabled and not button.has_class("-active"))
            button.focus()
            await pilot.pause()
            await pilot.press("enter")

        async def stage():
            for key, value in {
                "tasks.sort_order": "subject",
                "tasks.show_completed": False,
                "tasks.default_view": "due",
            }.items():
                assert settings.action_select_setting(key)
                await pilot.pause()
                await wait_for(pilot, lambda: bool(settings.query("#settings-choice #label")))
                settings.query_one("#settings-choice", Select).value = (
                    "off" if value is False else value
                )
                await activate("#settings-stage")
                await wait_for(pilot, lambda key=key: key in settings._staged)

        await stage()
        await activate("#settings-cancel")
        await wait_for(pilot, lambda: not settings._staged)
        assert context.paths.config_file.read_bytes() == before and len(screen._tasks) == 5
        await stage()
        await activate("#settings-apply")
        await wait_for(
            pilot,
            lambda: (
                not settings._staged
                and not settings.mutation_pending
                and screen._display.sort_order is TaskSortOrder.SUBJECT
            ),
        )
        settings.action_close()
        await wait_for(pilot, lambda: app.screen is screen)
        assert screen.view == "all" and [t.reference for t in screen._tasks] == ["b", "c", "e", "a"]
        assert screen._highlighted == "e" and screen._selected == {"e"}
        assert "Full Delta body" in str(screen.query_one("#tasks-detail", Static).render())
        assert len(gateway.calls) == calls
        screen.action_edit()
        await wait_for(
            pilot, lambda: isinstance(app.screen, TaskEditorScreen) and app.screen.is_mounted
        )
        editor = app.screen
        assert editor.query_one("#task-edit-subject", Input).value == "Delta"
        assert editor.query_one("#task-edit-due", Input).value == TODAY.isoformat()
        editor.action_cancel()
        await wait_for(pilot, lambda: app.screen is screen)
        screen.action_refresh()
        await wait_for(pilot, lambda: len(gateway.calls) > calls and not screen._operation_active)
        assert screen.view == "all" and screen._selected == {"e"}
        assert gateway.calls[-1][1].exclude_completed and gateway.calls[-1][-1] == 0
        screen.action_close()
        await pilot.pause()
        app._open_area("tasks")
        reopened = app.screen
        assert isinstance(reopened, TasksScreen)
        reopened.clock = lambda: NOW
        await wait_for(pilot, lambda: not reopened._operation_active and len(reopened._tasks) == 2)
        assert reopened.view == "due" and [t.reference for t in reopened._tasks] == ["e", "a"]
        assert (
            gateway.updates == []
            and to_primitive(gateway.tasks) == original
            and context._gateway is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_task_detail_is_reachable_and_scrolls_with_actual_keyboard(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    configure(context)
    gateway = ReadOnlyTasks()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        app.offline = False
        app._open_area("tasks")
        screen = app.screen
        assert isinstance(screen, TasksScreen)
        screen.clock = lambda: NOW
        await wait_for(pilot, lambda: len(screen._tasks) == 5 and not screen._operation_active)
        table = screen.query_one("#tasks-table", DataTable)
        table.focus()
        await wait_for(pilot, lambda: table.has_focus)
        await pilot.press("space", "enter")
        await wait_for(
            pilot, lambda: screen._complete_detail_reference == "a" and not screen._operation_active
        )
        calls = len(gateway.calls)
        for _ in range(12):
            await pilot.press("tab")
            if app.focused and app.focused.id == "tasks-detail-pane":
                break
        pane = screen.query_one("#tasks-detail-pane", VerticalScroll)
        assert pane.has_focus and pane.max_scroll_y > 0
        if size[1] < 26:
            assert pane.region.height >= 5
        assert screen.query_one("#tasks-edit", Button).region.bottom <= screen.size.height
        assert screen.query_one("#tasks-refresh", Button).region.bottom <= screen.size.height
        await pilot.press("end")
        await wait_for(pilot, lambda: pane.scroll_y == pane.max_scroll_y)
        await pilot.press("home")
        await wait_for(pilot, lambda: pane.scroll_y == 0)
        await pilot.press("pagedown")
        await wait_for(pilot, lambda: pane.scroll_y > 0)
        await pilot.press("pageup")
        await wait_for(pilot, lambda: pane.scroll_y == 0)
        await pilot.press("shift+tab")
        assert not pane.has_focus
        assert screen._selected == {"a"} and len(gateway.calls) == calls and gateway.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "zone,expected", [("Pacific/Kiritimati", {"a", "e"}), ("America/Los_Angeles", {"a"})]
)
async def test_normal_due_workspace_uses_same_captured_local_date_in_query_and_rows(
    tmp_path, monkeypatch, zone, expected
):
    context = _context(tmp_path)
    configure(context, view="due", show=True, zone=zone)
    gateway = ReadOnlyTasks()
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=(70, 20)) as pilot:
        app.offline = False
        app._open_area("tasks")
        screen = app.screen
        assert isinstance(screen, TasksScreen)
        screen.clock = lambda: NOW
        await wait_for(pilot, lambda: len(gateway.calls) >= 1 and not screen._operation_active)
        assert {t.reference for t in screen._tasks} == expected
        today = TaskDisplayPreferences(timezone=zone).today(NOW)
        assert gateway.calls[-1][1].due_before == today + timedelta(days=1)
        assert screen._view_today == today
        assert len(gateway.calls) == 1
        state = str(screen.query_one("#area-state", Static).render())
        assert f"{len(expected)} task" in state and "Current page" in state
        assert "7 candidates examined" in screen.query_one("#area-state", Static).tooltip
        assert gateway.updates == [] and context._gateway is None
