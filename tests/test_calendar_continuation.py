"""Calendar search cannot imply completeness from a limited first page."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from textual.app import App
from textual.widgets import Button, DataTable, Input

from gucmail.errors import ConfigurationError
from gucmail.models import CalendarEvent
from gucmail.services.calendar import CalendarService, CalendarViewKind
from gucmail.services.calendar_paging import query_calendar_window
from gucmail.tui.pim_screens import CalendarScreen, CalendarServiceBackend
from waiting import click_button, rounds

START = datetime(2026, 9, 1, tzinfo=UTC)


class WindowReader:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def list_events(self, *, start, end, calendar_reference, limit):
        self.calls.append((start, end, limit))
        return tuple(event for event in self.events if event.start < end and event.end > start)[
            :limit
        ]


def events(count):
    return [
        CalendarEvent(
            f"event-{n}",
            f"Academic {n}",
            START + timedelta(hours=n),
            START + timedelta(hours=n + 1),
            "UTC",
        )
        for n in range(count)
    ]


def test_search_matches_beyond_display_limit_before_limiting_results():
    reader = WindowReader(events(100))
    page = query_calendar_window(
        reader, start=START, end=START + timedelta(days=30), limit=25, query="Academic 41"
    )
    assert [event.reference for event in page.items] == ["event-41"]
    assert page.complete and page.cursor is None
    assert all(call[2] <= 200 for call in reader.calls)


def test_full_academic_year_continues_without_duplicates_or_missing_long_events():
    values = events(6000)
    values.insert(
        0,
        CalendarEvent(
            "long-event",
            "Long overlapping event",
            START - timedelta(days=1),
            START + timedelta(days=250),
            "UTC",
            is_all_day=True,
        ),
    )
    reader = WindowReader(values)
    cursor = None
    found = []
    for _ in range(1000):
        before = len(reader.calls)
        page = query_calendar_window(
            reader, start=START, end=START + timedelta(days=365), limit=25, cursor=cursor
        )
        assert len(reader.calls) - before <= 4 and len(page.items) <= 25
        found.extend(event.reference for event in page.items)
        cursor = page.cursor
        if cursor is None:
            assert page.complete
            break
    else:
        pytest.fail("The bounded academic-year traversal did not terminate")
    assert len(found) == len(set(found)) == 6001
    assert set(found) == {event.reference for event in values}


def test_empty_search_page_can_continue_and_cursor_cannot_change_scope():
    reader = WindowReader(events(1000))
    page = query_calendar_window(
        reader,
        start=START,
        end=START + timedelta(days=365),
        limit=25,
        query="Academic 999",
        request_budget=1,
    )
    assert not page.items and page.cursor and not page.complete
    with pytest.raises(ConfigurationError, match="refresh"):
        query_calendar_window(
            reader,
            start=START,
            end=START + timedelta(days=365),
            limit=25,
            query="different",
            cursor=page.cursor,
        )
    for _ in range(100):
        page = query_calendar_window(
            reader,
            start=START,
            end=START + timedelta(days=365),
            limit=25,
            query="Academic 999",
            cursor=page.cursor,
        )
        if page.items or page.cursor is None:
            break
    assert [event.reference for event in page.items] == ["event-999"]


def test_unresolvable_same_instant_density_is_explicitly_incomplete():
    values = [
        CalendarEvent(str(n), f"Dense {n}", START, START + timedelta(hours=1), "UTC")
        for n in range(250)
    ]
    reader = WindowReader(values)
    cursor = None
    for _ in range(100):
        page = query_calendar_window(
            reader, start=START, end=START + timedelta(seconds=1), limit=200, cursor=cursor
        )
        if page.cursor is None:
            break
        cursor = page.cursor
    assert not page.complete and "Incomplete" in page.notice


def test_cancel_prevents_reads_and_checks_between_matching_candidates():
    reader = WindowReader(events(100))
    with pytest.raises(InterruptedError):
        query_calendar_window(
            reader, start=START, end=START + timedelta(days=30), limit=25, cancelled=lambda: True
        )
    assert reader.calls == []


def test_paged_view_preserves_work_week_and_timezone_boundaries():
    reader = WindowReader(events(150))
    service = CalendarService(reader, display_timezone="Africa/Cairo")
    view = service.query_view(
        kind=CalendarViewKind.WORK_WEEK,
        anchor=date(2026, 9, 3),
        working_days=(0, 1, 2, 3, 4),
        limit=200,
    )
    assert view.timezone == "Africa/Cairo"
    assert all(bucket.date.weekday() in range(5) for bucket in view.days)
    assert view.complete


@pytest.mark.asyncio
async def test_user_can_page_back_and_search_beyond_first_page():
    reader = WindowReader(events(100))
    screen = CalendarScreen(CalendarServiceBackend(CalendarService(reader)), page_size=25)
    screen.anchor_date = START.date()

    class CalendarApp(App):
        def on_mount(self):
            self.push_screen(screen)

    app = CalendarApp()
    async with app.run_test(size=(120, 36)) as pilot:

        async def settled(count, *, unlike=None):
            # ``unlike``: the page shown before a click; the new page must differ from it,
            # because the old page also has ``count`` events until the load starts.
            for _ in rounds(100):
                await pilot.pause(0.02)
                shown = [event.reference for event in screen._events]
                if (
                    not screen._operation_active
                    and len(shown) == count
                    and (unlike is None or shown != unlike)
                ):
                    return
            pytest.fail("Calendar did not settle")

        await settled(25)
        first = [event.reference for event in screen._events]
        assert not screen.query_one("#calendar-more", Button).disabled
        await click_button(pilot, screen.query_one("#calendar-more", Button))
        await settled(25, unlike=first)
        second = [event.reference for event in screen._events]
        assert not set(first) & set(second)
        await click_button(pilot, screen.query_one("#calendar-earlier", Button))
        await settled(25, unlike=second)
        assert [event.reference for event in screen._events] == first
        search = screen.query_one("#calendar-search", Input)
        search.value = "Academic 41"
        search.focus()
        await pilot.press("enter")
        await settled(1)
        assert screen.query_one("#calendar-table", DataTable).row_count == 1
        assert screen._events[0].reference == "event-41"
        assert screen.query_one("#calendar-earlier", Button).disabled
        assert screen.query_one("#calendar-more", Button).disabled
