from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static
from typer.testing import CliRunner

from gucmail import cli
from gucmail.errors import ConfigurationError
from gucmail.models import to_primitive
from gucmail.services import calendar as calendar_module
from gucmail.services.calendar import CalendarService, CalendarViewKind
from gucmail.services.calendar_display import (
    DISPLAY_SETTINGS,
    CalendarDisplayPreferences,
    event_time_labels,
    load_calendar_view,
)
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui import pim_screens
from gucmail.tui.app import GUCmailApp
from gucmail.tui.pim_screens import (
    CalendarAvailabilityScreen,
    CalendarEditorScreen,
    CalendarListRequest,
    CalendarScreen,
    CalendarServiceBackend,
)
from gucmail.tui.settings_screen import SettingsScreen
from test_calendar_working_time import ReadOnlyCalendar, settings_for
from test_cli import _environment
from test_compose_preferences import wait_for
from test_pim import _event
from test_settings_secondpass import _context

INSTANT = datetime(2026, 10, 1, 23, 30, tzinfo=UTC)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(INSTANT.timestamp(), tz or UTC)


def set_display(context, view="month", week="monday", timezone="Pacific/Kiritimati"):
    service = settings_for(context, revisions=InMemoryRevisionStore())
    session = service.begin()
    for key, value in zip(DISPLAY_SETTINGS, (view, week, timezone), strict=True):
        session.set(key, value)
    session.apply()
    return service


def invoke(root, arguments, *, human=False):
    return CliRunner().invoke(
        cli.app,
        ["--no-color" if human else "--json", "--non-interactive", "calendar", *arguments],
        env=_environment(root),
    )


def test_display_policy_stage_cancel_apply_reopen_scope_transfer_reset_undo_and_environment(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = set_display(context)
    expected = CalendarDisplayPreferences(CalendarViewKind.MONTH, 0, "Pacific/Kiritimati")
    assert CalendarDisplayPreferences.from_settings(service) == expected
    before = context.paths.config_file.read_bytes()
    service.begin().set("calendar.default_view", "day").cancel()
    assert context.paths.config_file.read_bytes() == before
    assert CalendarDisplayPreferences.from_settings(settings_for(context)) == expected
    other = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert CalendarDisplayPreferences.from_settings(other) == CalendarDisplayPreferences()
    target = settings_for(_context(tmp_path / "transfer"))
    target.apply_import(target.plan_import(service.export(setting_ids=DISPLAY_SETTINGS)))
    assert CalendarDisplayPreferences.from_settings(target) == expected
    for key in DISPLAY_SETTINGS:
        service.reset(key)
        assert service.value(key).effective_value == service.registry.get(key).default
        service.undo()
        assert CalendarDisplayPreferences.from_settings(service) == expected
    before_environment = context.paths.config_file.read_bytes()
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__DEFAULT_VIEW", "work_week")
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__WEEK_START", "sunday")
    monkeypatch.setenv("GUCMAIL_TIMEZONE", "Europe/Berlin")
    assert CalendarDisplayPreferences.from_settings(service) == CalendarDisplayPreferences(
        CalendarViewKind.WORK_WEEK, 6, "Europe/Berlin"
    )
    assert context.paths.config_file.read_bytes() == before_environment


@pytest.mark.parametrize(
    "key,value",
    [
        ("calendar.default_view", "upcoming"),
        ("calendar.default_view", "invalid"),
        ("calendar.week_start", "Friday"),
        ("calendar.timezone", "Invalid/Zone"),
    ],
)
def test_invalid_display_values_preserve_configuration(tmp_path, key, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        settings_for(context).set(key, value)
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize("view", ["agenda", "day", "week", "work_week", "month"])
@pytest.mark.parametrize("timezone", ["Pacific/Kiritimati", "America/Los_Angeles"])
def test_bare_cli_uses_saved_view_zone_today_and_explicit_command_is_ephemeral(
    tmp_path, monkeypatch, view, timezone
):
    context = _context(tmp_path)
    set_display(context, view=view, timezone=timezone)
    settings_for(context).set("calendar.working_days", "Sunday,Tuesday")
    gateway = ReadOnlyCalendar()
    gateway.events = {"original": _event("original", INSTANT, INSTANT + timedelta(hours=1))}
    original = to_primitive(gateway.events)
    monkeypatch.setattr(calendar_module, "datetime", FixedDatetime)
    monkeypatch.setattr(cli, "_gateway", lambda *_: gateway)
    before = context.paths.config_file.read_bytes()
    result = invoke(tmp_path, [])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["kind"] == view and data["timezone"] == timezone
    start = datetime.fromisoformat(data["start"])
    assert start.utcoffset() == INSTANT.astimezone(ZoneInfo(timezone)).utcoffset()
    today = INSTANT.astimezone(ZoneInfo(timezone)).date()
    if view in {"agenda", "day"}:
        assert start.date() == today
    if view in {"week", "work_week", "month"}:
        assert start.weekday() == 0
    if view == "work_week":
        assert [date.fromisoformat(day["date"]).weekday() for day in data["days"]] == [1, 6]
    assert len(gateway.queries) == 1 and gateway.queries[0][3] == 200
    result = invoke(tmp_path, ["day", "2026-10-06", "--limit", "13"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["kind"] == "day"
    assert gateway.queries[-1][3] == 13 and len(gateway.queries) == 2
    assert to_primitive(gateway.events) == original
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize(
    "week,first,last",
    [
        ("monday", "2026-09-28", "2026-11-02"),
        ("saturday", "2026-09-26", "2026-11-07"),
        ("sunday", "2026-09-27", "2026-11-01"),
    ],
)
def test_cli_month_complete_grid_matches_tui_backend_and_marks_adjacent_dates(
    tmp_path, monkeypatch, week, first, last
):
    from threading import Event

    context = _context(tmp_path)
    set_display(context, week=week, timezone="Europe/Berlin")
    gateway = ReadOnlyCalendar()
    monkeypatch.setattr(cli, "_gateway", lambda *_: gateway)
    result = invoke(tmp_path, ["month", "2026-10-06", "--limit", "17"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["start"].startswith(first) and data["end"].startswith(last)
    assert len(data["days"]) % 7 == 0
    assert all(
        row["in_primary_range"] == (date.fromisoformat(row["date"]).month == 10)
        for row in data["days"]
    )
    before = gateway.queries[-1]
    preferences = CalendarDisplayPreferences.from_settings(settings_for(context))
    backend = CalendarServiceBackend(CalendarService(gateway, display_timezone="UTC"))
    backend.list_events(
        CalendarListRequest(
            CalendarViewKind.MONTH,
            date(2026, 10, 6),
            limit=17,
            first_weekday=preferences.first_weekday,
            timezone=preferences.timezone,
        ),
        cancel=Event(),
        progress=lambda _: None,
    )
    assert len(gateway.queries) == 2
    # Date/scope semantics match; the paged TUI scans a bounded candidate window
    # before applying the 17-result display limit, rather than searching 17 rows.
    assert gateway.queries[-1][:3] == before[:3]
    assert 17 <= gateway.queries[-1][3] <= 200
    assert backend.service.display_timezone == "UTC"


@pytest.mark.parametrize("anchor,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_display_day_window_keeps_dst_elapsed_boundaries(anchor, hours):
    gateway = ReadOnlyCalendar()
    view = load_calendar_view(
        CalendarService(gateway, display_timezone="Europe/Berlin"),
        kind=CalendarViewKind.DAY,
        anchor=anchor,
    )
    assert (view.end.astimezone(UTC) - view.start.astimezone(UTC)).total_seconds() == hours * 3600
    assert len(gateway.queries) == 1


@pytest.mark.parametrize("week,first", [("monday", 5), ("saturday", 3), ("sunday", 4)])
def test_explicit_cli_week_uses_saved_alignment_and_one_query(tmp_path, monkeypatch, week, first):
    context = _context(tmp_path)
    set_display(context, week=week, timezone="UTC")
    gateway = ReadOnlyCalendar()
    monkeypatch.setattr(cli, "_gateway", lambda *_: gateway)
    result = invoke(tmp_path, ["week", "2026-10-06", "--limit", "23"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["start"].startswith(f"2026-10-{first:02}") and len(data["days"]) == 7
    assert len(gateway.queries) == 1 and gateway.queries[0][3] == 23


def test_timed_projection_retains_dst_fold_and_all_day_dates_without_source_mutation():
    first = _event(
        "first", datetime(2026, 10, 25, 0, 30, tzinfo=UTC), datetime(2026, 10, 25, 1, tzinfo=UTC)
    )
    second = replace(
        first,
        reference="second",
        start=first.start + timedelta(hours=1),
        end=first.end + timedelta(hours=1),
    )
    assert "+02:00" in event_time_labels(first, "Europe/Berlin")[0]
    assert "+01:00" in event_time_labels(second, "Europe/Berlin")[0]
    zone = ZoneInfo("Pacific/Kiritimati")
    all_day = replace(
        first,
        start=datetime(2026, 10, 2, tzinfo=zone),
        end=datetime(2026, 10, 3, tzinfo=zone),
        timezone=zone.key,
        is_all_day=True,
    )
    before = to_primitive((first, second, all_day))
    assert event_time_labels(all_day, "America/Los_Angeles") == (
        "2026-10-02 (all day)",
        "2026-10-03 (exclusive)",
    )
    assert to_primitive((first, second, all_day)) == before


def test_cli_human_projection_json_original_and_invalid_environment_stays_local(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    set_display(context, timezone="Europe/Berlin")
    gateway = ReadOnlyCalendar()
    event = _event("original", INSTANT, INSTANT + timedelta(hours=1))
    gateway.events = {event.reference: event}
    monkeypatch.setattr(cli, "_gateway", lambda *_: gateway)
    result = invoke(tmp_path, ["show", event.reference], human=True)
    assert result.exit_code == 0, result.output
    assert "2026-10-02 01:30+02:00" in result.output and "Europe/Berlin" in result.output
    result = invoke(tmp_path, ["show", event.reference])
    assert result.exit_code == 0
    data = json.loads(result.output)["data"]
    assert data["start"] == event.start.isoformat() and data["timezone"] == event.timezone
    result = invoke(tmp_path, ["day", "2026-10-06"], human=True)
    assert result.exit_code == 0 and "No matching items" in result.output
    assert "Europe/Berlin" in result.output and "2026-10-06" in result.output
    monkeypatch.setenv("GUCMAIL_TIMEZONE", "Invalid/Zone")
    monkeypatch.setattr(
        cli, "_gateway", lambda *_: pytest.fail("Invalid zone resolved credentials")
    )
    result = invoke(tmp_path, [])
    assert result.exit_code != 0 and "zone" in result.output.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_calendar_display_apply_refresh_reopen_and_captured_form(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    gateway = ReadOnlyCalendar()
    event = _event("original", INSTANT, INSTANT + timedelta(hours=1))
    gateway.events = {event.reference: event}
    original = to_primitive(event)
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: gateway)
    async with app.run_test(size=size) as pilot:
        before = context.paths.config_file.read_bytes()
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        settings = app.screen

        async def activate(selector):
            button = settings.query_one(selector, Button)
            await wait_for(pilot, lambda: not button.disabled and not button.has_class("-active"))
            button.focus()
            await pilot.press("enter")

        async def stage():
            for key, value in zip(
                DISPLAY_SETTINGS, ("month", "monday", "Europe/Berlin"), strict=True
            ):
                assert settings.action_select_setting(key)
                await pilot.pause()
                if key == "calendar.timezone":
                    settings.query_one("#settings-editor", Input).value = value
                else:
                    choice = settings.query_one("#settings-choice", Select)
                    await wait_for(pilot, lambda choice=choice: bool(choice.query("#label")))
                    choice.value = value
                await activate("#settings-stage")
                await wait_for(pilot, lambda key=key: key in settings._staged)

        await stage()
        await activate("#settings-cancel")
        await wait_for(pilot, lambda: not settings._staged)
        assert context.paths.config_file.read_bytes() == before
        await stage()
        await activate("#settings-apply")
        await wait_for(pilot, lambda: not settings._staged and not settings.mutation_pending)
        settings.action_close()
        await pilot.pause()
        app.offline = False
        app._open_area("calendar")
        await wait_for(
            pilot, lambda: isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
        )
        screen = app.screen
        await wait_for(pilot, lambda: not screen._operation_active)
        screen.anchor_date = date(2026, 10, 6)
        screen.action_refresh()
        await wait_for(pilot, lambda: not screen._operation_active and len(screen._events) == 1)
        assert screen.view_kind is CalendarViewKind.MONTH and screen.first_weekday == 0
        assert screen.query_one("#calendar-view", Select).value == "month"
        assert "2026-10-02 01:30+02:00" in map(
            str, screen.query_one("#calendar-table", DataTable).get_row(event.reference)
        )
        assert "Europe/Berlin" in str(screen.query_one("#calendar-detail", Static).content)
        screen.action_edit()
        await wait_for(
            pilot, lambda: isinstance(app.screen, CalendarEditorScreen) and app.screen.is_mounted
        )
        editor = app.screen
        assert editor.query_one("#calendar-edit-timezone", Input).value == event.timezone
        assert editor._value().start == event.start
        editor.dismiss(None)
        await pilot.pause()
        screen.action_find_free_time()
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, CalendarAvailabilityScreen) and app.screen.is_mounted,
        )
        form = app.screen
        form.query_one("#availability-attendees", Input).value = "guest@example.test"
        form.query_one("#availability-start", Input).value = "2026-10-06 09:00"
        form.query_one("#availability-end", Input).value = "2026-10-06 17:00"
        captured = form._value()
        assert captured.timezone == "Europe/Berlin" and captured.start.utcoffset() == timedelta(
            hours=2
        )
        queries = len(gateway.queries)
        updates = settings_for(context).begin()
        updates.set("calendar.timezone", "America/Los_Angeles")
        updates.set("calendar.week_start", "sunday")
        updates.set("calendar.default_view", "day")
        updates.apply()
        app._apply_committed_settings(DISPLAY_SETTINGS)
        assert screen.timezone == "America/Los_Angeles" and len(gateway.queries) == queries
        assert form._value() == captured
        assert screen.view_kind is CalendarViewKind.MONTH and screen.first_weekday == 6
        assert "2026-10-01 16:30-07:00" in map(
            str, screen.query_one("#calendar-table", DataTable).get_row(event.reference)
        )
        form.dismiss(captured)
        await wait_for(
            pilot, lambda: len(gateway.availability_calls) == 1 and not screen._operation_active
        )
        assert "09:00+02:00" in str(screen.query_one("#calendar-detail", Static).content)
        screen.action_refresh()
        await wait_for(
            pilot, lambda: len(gateway.queries) == queries + 1 and not screen._operation_active
        )
        assert gateway.queries[-1][0] == datetime(2026, 9, 27, 7, tzinfo=UTC)
        assert screen.view_kind is CalendarViewKind.MONTH
        screen.action_close()
        await pilot.pause()
        app._open_area("calendar")
        await wait_for(
            pilot, lambda: isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
        )
        assert (
            app.screen.timezone == "America/Los_Angeles"
            and app.screen.view_kind is CalendarViewKind.DAY
        )
        await wait_for(pilot, lambda: not app.screen._operation_active)
        monkeypatch.setattr(pim_screens, "datetime", FixedDatetime)
        app.screen.action_today()
        await wait_for(pilot, lambda: not app.screen._operation_active)
        assert app.screen.anchor_date == date(2026, 10, 1)
        assert to_primitive(gateway.events[event.reference]) == original and gateway.responses == []
        assert context._gateway is None
