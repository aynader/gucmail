from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static
from typer.testing import CliRunner

from gucmail import cli
from gucmail.errors import ConfigurationError
from gucmail.models import Address
from gucmail.services.calendar import CalendarService
from gucmail.services.calendar_working_time import (
    WORKING_TIME_SETTINGS,
    CalendarWorkingTime,
    find_common_free_time,
    validate_free_time_request,
)
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.pim_screens import CalendarAvailabilityScreen, CalendarScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_cli import _environment
from test_compose_preferences import wait_for
from test_pim import CalendarMemoryGateway, _event
from test_settings_secondpass import _context

ATTENDEES = (Address("guest@example.test"), Address("other@example.test"))
START = datetime(2026, 10, 3, 8, tzinfo=UTC)
END = datetime(2026, 10, 7, 18, tzinfo=UTC)
VALUES = dict(
    zip(WORKING_TIME_SETTINGS, [("sunday", "tuesday", "thursday"), "11:00", "16:00"], strict=True)
)


def settings_for(context, **kwargs):
    return SettingsService(
        context.config_store, context=SettingsContext(account_id=context.config.email), **kwargs
    )


def configure(context):
    service = settings_for(context, revisions=InMemoryRevisionStore())
    session = service.begin()
    for key, value in VALUES.items():
        session.set(key, value)
    session.apply()
    return service


def responses(start=START, end=END, interval=30):
    slots = -(-(int((end - start).total_seconds())) // (interval * 60))
    return tuple(
        {"attendee": attendee, "available": True, "merged": "0" * slots, "events": ()}
        for attendee in ATTENDEES
    )


def test_schedule_stage_cancel_apply_scope_reopen_transfer_reset_undo_and_environment(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    service = configure(context)
    expected = CalendarWorkingTime(("sunday", "tuesday", "thursday"), "11:00", "16:00")
    assert CalendarWorkingTime.from_settings(service) == expected
    before = context.paths.config_file.read_bytes()
    session = service.begin().set(WORKING_TIME_SETTINGS[0], "Saturday, Monday")
    session.cancel()
    assert context.paths.config_file.read_bytes() == before
    assert CalendarWorkingTime.from_settings(settings_for(context)) == expected
    other = SettingsService(context.config_store, context=SettingsContext(account_id="other"))
    assert CalendarWorkingTime.from_settings(other) == CalendarWorkingTime()
    target = settings_for(_context(tmp_path / "import"))
    target.apply_import(target.plan_import(service.export(setting_ids=WORKING_TIME_SETTINGS)))
    assert CalendarWorkingTime.from_settings(target) == expected
    for key in WORKING_TIME_SETTINGS:
        service.reset(key)
        assert service.value(key).effective_value == service.registry.get(key).default
        service.undo()
        assert service.value(key).effective_value == service.registry.get(key).parse(VALUES[key])
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__WORKING_DAYS", "Sunday,Tuesday")
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__WORKDAY_START", "12:00")
    assert CalendarWorkingTime.from_settings(service) == CalendarWorkingTime(
        ("sunday", "tuesday"), "12:00", "16:00"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("calendar.working_days", ""),
        ("calendar.working_days", "Monday,Monday"),
        ("calendar.working_days", "Mon,Tue"),
        ("calendar.working_days", [True]),
        ("calendar.workday_start", "9:00"),
        ("calendar.workday_start", "24:00"),
        ("calendar.workday_end", "17:60"),
        ("calendar.workday_end", "17:00:00"),
        ("calendar.workday_start", "17:00"),
        ("calendar.workday_end", "08:00"),
    ],
)
def test_invalid_schedule_preserves_configuration(tmp_path, key, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        settings_for(context).set(key, value)
    assert context.paths.config_file.read_bytes() == before


def test_endpoints_validate_as_one_atomic_configured_and_effective_window(tmp_path, monkeypatch):
    context = _context(tmp_path)
    service = settings_for(context)
    session = (
        service.begin().set("calendar.workday_start", "21:00").set("calendar.workday_end", "22:00")
    )
    session.apply()
    assert CalendarWorkingTime.from_settings(service).start == "21:00"
    before = context.paths.config_file.read_bytes()
    monkeypatch.setenv("GUCMAIL_SETTING_CALENDAR__WORKDAY_END", "20:00")
    with pytest.raises(ConfigurationError, match="end must follow"):
        service.set("calendar.working_days", "sunday")
    assert context.paths.config_file.read_bytes() == before


def test_nonconsecutive_workweek_uses_one_bounded_window_and_half_open_overlap():
    def event(key, start, end):
        return _event(key, datetime.fromisoformat(start), datetime.fromisoformat(end))

    events = (
        event("sat", "2026-10-03T10:00:00+00:00", "2026-10-03T11:00:00+00:00"),
        event("overnight", "2026-10-03T23:00:00+00:00", "2026-10-04T01:00:00+00:00"),
        event("midnight-end", "2026-10-05T23:00:00+00:00", "2026-10-06T00:00:00+00:00"),
        replace(
            event("all-day", "2026-10-06T00:00:00+00:00", "2026-10-07T00:00:00+00:00"),
            is_all_day=True,
        ),
        event("after-hours", "2026-10-08T22:00:00+00:00", "2026-10-08T23:00:00+00:00"),
    )
    gateway = CalendarMemoryGateway(events)
    service = CalendarService(gateway)
    view = service.week(
        date(2026, 10, 6), first_weekday=5, work_week=True, working_days=(6, 1, 3), limit=17
    )
    assert [day.date for day in view.days] == [date(2026, 10, d) for d in (4, 6, 8)]
    assert [event.reference for event in view.events] == ["overnight", "all-day", "after-hours"]
    assert len(gateway.queries) == 1 and gateway.queries[0][3] == 17
    assert gateway.queries[0][:2] == (
        datetime(2026, 10, 3, tzinfo=UTC),
        datetime(2026, 10, 10, tzinfo=UTC),
    )
    full = service.week(date(2026, 10, 6), first_weekday=5)
    assert len(full.days) == 7 and full.events == events
    assert service.month(date(2026, 10, 6)).events == events
    assert gateway.events == {event.reference: event for event in events}


def test_free_time_days_hours_grid_rounding_and_explicit_all_hours_override():
    policy = CalendarWorkingTime(("monday", "wednesday"), "09:10", "10:30")
    suggestion = find_common_free_time(
        responses(),
        attendees=ATTENDEES,
        start=START,
        end=END,
        duration_minutes=45,
        working_time=policy,
    )
    assert suggestion.start == datetime(2026, 10, 5, 9, 30, tzinfo=UTC)
    assert suggestion.end == suggestion.start + timedelta(minutes=45) and suggestion.complete
    unrestricted = find_common_free_time(
        responses(), attendees=ATTENDEES, start=START, end=END, working_time=None
    )
    assert unrestricted.start == START
    short_end = datetime(2026, 10, 5, 9, 40, tzinfo=UTC)
    assert (
        find_common_free_time(
            responses(end=short_end),
            attendees=ATTENDEES,
            start=START,
            end=short_end,
            duration_minutes=45,
            working_time=policy,
        ).start
        is None
    )


@pytest.mark.parametrize(
    "kind", ["failed", "short", "wrong", "missing", "duplicate", "extra", "truthy"]
)
def test_free_time_never_promotes_incomplete_or_wrong_attendees(kind):
    values = [dict(value) for value in responses()]
    if kind == "failed":
        values[0]["available"] = False
    if kind == "truthy":
        values[0]["available"] = "true"
    if kind == "short":
        values[0]["merged"] = "00"
    if kind == "wrong":
        values[0]["attendee"] = "unknown@example.test"
    if kind == "missing":
        values.pop()
    if kind == "duplicate":
        values[0]["attendee"] = ATTENDEES[1]
    if kind == "extra":
        values.append(values[0])
    result = find_common_free_time(values, attendees=ATTENDEES, start=START, end=END)
    assert result.start is None and result.end is None and not result.complete


def test_free_time_busy_cells_whole_duration_and_window_end_are_respected():
    end = START + timedelta(minutes=100)
    values = [dict(value) for value in responses(end=end)]
    values[0]["merged"] = "2000"
    result = find_common_free_time(
        values, attendees=ATTENDEES, start=START, end=end, duration_minutes=45
    )
    assert result.start == START + timedelta(minutes=30)
    assert result.end == START + timedelta(minutes=75)
    values[0]["merged"] = "2200"
    assert (
        find_common_free_time(
            values, attendees=ATTENDEES, start=START, end=end, duration_minutes=45
        ).start
        is None
    )


def test_local_working_hours_across_dst_use_elapsed_time_and_reject_ambiguous_boundaries():
    policy = CalendarWorkingTime(("sunday",), "01:30", "03:30")
    assert policy.window(date(2026, 3, 29), "Europe/Berlin") == (
        datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
    )
    assert policy.window(date(2026, 10, 25), "Europe/Berlin") == (
        datetime(2026, 10, 24, 23, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 30, tzinfo=UTC),
    )
    ambiguous = CalendarWorkingTime(("sunday",), "02:30", "04:00")
    for day, message in [
        (date(2026, 3, 29), "does not exist"),
        (date(2026, 10, 25), "occurs twice"),
    ]:
        with pytest.raises(ConfigurationError, match=message):
            ambiguous.window(day, "Europe/Berlin")
    start, end = policy.window(date(2026, 3, 29), "Europe/Berlin")
    result = find_common_free_time(
        responses(start, end),
        attendees=ATTENDEES,
        start=start,
        end=end,
        timezone="Europe/Berlin",
        working_time=policy,
    )
    assert result.end.astimezone(UTC) - result.start.astimezone(UTC) == timedelta(hours=1)


@pytest.mark.parametrize("change", ["naive", "reverse", "too_long", "duplicate", "fractional"])
def test_request_bounds_reject_before_provider(change):
    start, end, attendees, interval = START, END, ATTENDEES, 30
    if change == "naive":
        start = start.replace(tzinfo=None)
    if change == "reverse":
        end = start
    if change == "too_long":
        end = start + timedelta(days=32)
    if change == "duplicate":
        attendees = (ATTENDEES[0], ATTENDEES[0])
    if change == "fractional":
        interval = 30.5
    with pytest.raises(ConfigurationError):
        validate_free_time_request(attendees, start, end, interval, 60)


class ReadOnlyCalendar(CalendarMemoryGateway):
    def __init__(self):
        super().__init__()
        self.availability_calls = []

    def get_availability(self, attendees, *, start, end, interval_minutes):
        self.availability_calls.append((tuple(attendees), start, end, interval_minutes))
        slots = -(-int((end - start).total_seconds()) // (interval_minutes * 60))
        return tuple(
            {"attendee": attendee, "available": True, "merged": "0" * slots, "events": ()}
            for attendee in attendees
        )


def invoke(root, arguments):
    return CliRunner().invoke(
        cli.app, ["--json", "--non-interactive", "calendar", *arguments], env=_environment(root)
    )


def test_cli_uses_canonical_workweek_and_same_free_time_policy(tmp_path, monkeypatch):
    context = _context(tmp_path)
    configure(context)
    gateway = ReadOnlyCalendar()
    monkeypatch.setattr(cli, "_gateway", lambda *_: gateway)
    result = invoke(tmp_path, ["week", "2026-10-06", "--work-week", "--limit", "19"])
    assert result.exit_code == 0, result.output
    assert [day["date"] for day in json.loads(result.output)["data"]["days"]] == [
        "2026-10-04",
        "2026-10-06",
        "2026-10-08",
    ]
    assert len(gateway.queries) == 1 and gateway.queries[0][3] == 19
    options = [
        "free-time",
        "--attendee",
        ATTENDEES[0].email,
        "--start",
        START.isoformat(),
        "--end",
        END.isoformat(),
    ]
    result = invoke(tmp_path, options)
    assert result.exit_code == 0, result.output
    expected = find_common_free_time(
        responses(),
        attendees=ATTENDEES,
        start=START,
        end=END,
        timezone=context.config.timezone,
        working_time=CalendarWorkingTime.from_settings(settings_for(context)),
    )
    assert datetime.fromisoformat(json.loads(result.output)["data"]["start"]) == expected.start
    result = invoke(tmp_path, [*options, "--all-hours"])
    assert (
        result.exit_code == 0
        and datetime.fromisoformat(json.loads(result.output)["data"]["start"]) == START
    )
    assert len(gateway.availability_calls) == 2


def test_cli_invalid_free_time_stays_local_and_help_explains_override(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "_gateway", lambda *_: pytest.fail("Invalid input resolved credentials")
    )
    result = invoke(
        tmp_path,
        [
            "free-time",
            "--attendee",
            ATTENDEES[0].email,
            "--start",
            START.isoformat(),
            "--end",
            (START + timedelta(days=32)).isoformat(),
        ],
    )
    assert result.exit_code != 0 and "31" in result.output
    result = invoke(tmp_path, ["free-time", "--help"])
    assert result.exit_code == 0 and "--all-hours" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_cancel_apply_workweek_free_time_and_captured_policy(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    gateway = ReadOnlyCalendar()
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
            await pilot.pause()
            await pilot.press("enter")

        async def stage():
            for key, value in VALUES.items():
                assert settings.action_select_setting(key)
                await pilot.pause()
                settings.query_one("#settings-editor", Input).value = (
                    ",".join(value) if isinstance(value, tuple) else value
                )
                await activate("#settings-stage")
                await wait_for(pilot, lambda key=key: key in settings._staged)

        await stage()
        await activate("#settings-cancel")
        await wait_for(pilot, lambda: not settings._staged)
        assert context.paths.config_file.read_bytes() == before
        await stage()
        await activate("#settings-apply")
        await wait_for(pilot, lambda: not settings._staged and not settings.mutation_pending)
        assert CalendarWorkingTime.from_settings(settings_for(context)).start == "11:00"
        settings.action_close()
        await pilot.pause()
        app.offline = False
        app._open_area("calendar")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, CalendarScreen)
                and app.screen.is_mounted
                and not app.screen._operation_active
            ),
        )
        screen = app.screen
        gateway.events = {
            event.reference: event
            for event in (
                _event("saturday", START, START + timedelta(hours=1)),
                _event("sunday", START + timedelta(days=1), START + timedelta(days=1, hours=1)),
                _event("tuesday", START + timedelta(days=3), START + timedelta(days=3, hours=1)),
            )
        }
        screen.anchor_date = date(2026, 10, 6)
        choice = screen.query_one("#calendar-view", Select)
        await wait_for(pilot, lambda: bool(choice.query("#label")))
        before_queries = len(gateway.queries)
        choice.value = "work_week"
        await wait_for(
            pilot,
            lambda: (
                screen.view_kind.value == "work_week"
                and len(gateway.queries) > before_queries
                and not screen._operation_active
                and screen.query_one("#calendar-table", DataTable).row_count == 2
            ),
        )
        assert screen._working_time.weekdays == (1, 3, 6)
        assert screen.query_one("#calendar-table", DataTable).row_count == 2
        assert set(screen._by_reference) == {"sunday", "tuesday"}
        screen.action_find_free_time()
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, CalendarAvailabilityScreen) and app.screen.is_mounted,
        )
        form = app.screen
        form.query_one("#availability-attendees", Input).value = ATTENDEES[0].email
        form.query_one("#availability-start", Input).value = START.isoformat()
        form.query_one("#availability-end", Input).value = END.isoformat()
        assert form.query_one("#availability-working-time", Checkbox).value
        assert form.working_time.start == "11:00"
        settings_for(context).set("calendar.workday_start", "12:00")
        assert form._value().working_time.start == "11:00"
        form.query_one("#availability-check", Button).focus()
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                len(gateway.availability_calls) == 1
                and "First common free slot"
                in str(screen.query_one("#calendar-detail", Static).content)
            ),
        )
        detail = str(screen.query_one("#calendar-detail", Static).content)
        assert "11:00-16:00 local" in detail
        screen.action_find_free_time()
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, CalendarAvailabilityScreen) and app.screen.is_mounted,
        )
        assert app.screen.working_time.start == "12:00"
        form = app.screen
        form.query_one("#availability-attendees", Input).value = ATTENDEES[0].email
        form.query_one("#availability-start", Input).value = START.isoformat()
        form.query_one("#availability-end", Input).value = END.isoformat()
        form.query_one("#availability-working-time", Checkbox).value = False
        assert form._value().working_time is None
        form.query_one("#availability-check", Button).focus()
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                len(gateway.availability_calls) == 2
                and "Searching all hours"
                in str(screen.query_one("#calendar-detail", Static).content)
            ),
        )
        assert START.astimezone(ZoneInfo(context.config.timezone)).isoformat(
            timespec="minutes"
        ) in str(screen.query_one("#calendar-detail", Static).content)
        assert set(gateway.events) == {"saturday", "sunday", "tuesday"}
        assert gateway.responses == [] and context._gateway is None
