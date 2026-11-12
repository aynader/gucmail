from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import DataTable, Input, Select, Static

from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError
from gucmail.ews.adapter import EWSGateway
from gucmail.models import Address, AttachmentInfo, CalendarEvent, CalendarResponse
from gucmail.services.calendar import (
    CalendarPatch,
    CalendarService,
    CalendarViewKind,
    EventCreatePlan,
    EventDeletePlan,
    EventUpdatePlan,
    MeetingResponsePlan,
    calendar_event_search_text,
)
from gucmail.services.recurrence import RecurrenceScope
from gucmail.tui.area_common import (
    ConfirmationScreen as OperationalConfirmationScreen,
)
from gucmail.tui.pim_screens import (
    CalendarEditorScreen,
    CalendarScopeScreen,
    CalendarScreen,
    CalendarServiceBackend,
    FilePathScreen,
    MeetingResponseScreen,
    PimActionRequested,
    _shift_calendar_anchor,
)


class _CalendarGateway:
    def __init__(self, event: CalendarEvent) -> None:
        self.event = event

    def list_events(self, **_kwargs: object) -> tuple[CalendarEvent, ...]:
        return (self.event,)

    def get_event(self, reference: str) -> CalendarEvent:
        assert reference == self.event.reference
        return self.event

    def create_event(self, plan: object) -> CalendarEvent:
        del plan
        return self.event

    def update_event(self, plan: object) -> CalendarEvent:
        del plan
        return self.event

    def delete_event(self, plan: object) -> None:
        del plan

    def respond_to_meeting(self, plan: object) -> CalendarEvent:
        del plan
        return self.event


def _event() -> CalendarEvent:
    return CalendarEvent(
        reference="event-1",
        subject="Design review",
        start=datetime(2026, 8, 22, 9, tzinfo=UTC),
        end=datetime(2026, 8, 22, 10, tzinfo=UTC),
        timezone="UTC",
    )


def test_calendar_attachment_plans_search_fields_and_organizer_guard(tmp_path: Path) -> None:
    attachment_path = tmp_path / "agenda.pdf"
    attachment_path.write_bytes(b"synthetic")
    event = replace(
        _event(),
        is_meeting=True,
        organizer=Address("organizer@example.test", "Organizer"),
        optional_attendees=(Address("guest@example.test", "Guest Person"),),
        categories=("Course",),
        attachments=(AttachmentInfo("attachment-1", "briefing.pdf", 9),),
    )
    service = CalendarService(_CalendarGateway(event))

    create = service.plan_create(event, attachment_paths=(attachment_path, attachment_path))
    update = service.plan_update(
        event.reference,
        CalendarPatch(),
        current=event,
        attachment_paths=(attachment_path,),
        remove_attachment_references=("attachment-1",),
    )

    assert create.attachment_paths == (attachment_path.resolve(),)
    assert update.attachment_paths == (attachment_path.resolve(),)
    assert update.remove_attachment_references == ("attachment-1",)
    search_text = calendar_event_search_text(event).casefold()
    assert "guest person" in search_text
    assert "course" in search_text
    assert "briefing.pdf" in search_text

    organizer_copy = replace(event, response_status="Organizer")
    with pytest.raises(ConfigurationError, match="organizer"):
        service.plan_response(organizer_copy, CalendarResponse.ACCEPT)


def test_calendar_import_is_bounded_and_month_navigation_is_calendar_aware() -> None:
    service = CalendarService(_CalendarGateway(_event()))

    with pytest.raises(ConfigurationError, match="5 MiB"):
        service.import_ics("x" * (5 * 1024 * 1024 + 1))

    assert _shift_calendar_anchor(date(2025, 1, 31), CalendarViewKind.MONTH, 1) == date(2025, 2, 28)
    assert _shift_calendar_anchor(date(2024, 1, 31), CalendarViewKind.MONTH, 1) == date(2024, 2, 29)


def test_ews_calendar_fallback_view_keeps_events_overlapping_window() -> None:
    start = datetime(2026, 8, 22, 9, tzinfo=UTC)
    spanning = SimpleNamespace(
        id="ews-spanning",
        changekey="ck-1",
        subject="Spanning event",
        start=start - timedelta(hours=1),
        end=start + timedelta(hours=1),
        location=None,
        body="",
        required_attendees=(),
        optional_attendees=(),
        resources=(),
        categories=(),
        attachments=(),
    )
    outside = SimpleNamespace(
        id="ews-outside",
        changekey="ck-2",
        subject="Outside event",
        start=start - timedelta(hours=3),
        end=start - timedelta(hours=2),
        location=None,
        body="",
        required_attendees=(),
        optional_attendees=(),
        resources=(),
        categories=(),
        attachments=(),
    )

    class CalendarFolder:
        id = "calendar"
        changekey = "calendar-ck"
        name = "Calendar"

        @staticmethod
        def view(**_kwargs: object) -> tuple[object, ...]:
            return spanning, outside

    account = SimpleNamespace(
        calendar=CalendarFolder(),
        protocol=SimpleNamespace(close=lambda: None),
    )
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)

    result = gateway.list_events(
        start=start,
        end=start + timedelta(hours=2),
        calendar_reference=None,
        limit=10,
    )

    assert [event.subject for event in result] == ["Spanning event"]


def test_ews_calendar_recurrence_scope_distinguishes_occurrence_and_series() -> None:
    master = SimpleNamespace(type="RecurringMaster", refreshed=False)

    def refresh() -> None:
        master.refreshed = True

    master.refresh = refresh
    occurrence = SimpleNamespace(type="Occurrence", recurring_master=lambda: master)

    assert EWSGateway._calendar_scope_item(occurrence, RecurrenceScope.OCCURRENCE) is occurrence
    assert EWSGateway._calendar_scope_item(occurrence, RecurrenceScope.SERIES) is master
    assert master.refreshed is True


def test_ews_calendar_dispatches_all_invitation_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class MeetingItem:
        def accept(self, *, message_disposition: str) -> None:
            calls.append(("accept", message_disposition))

        def tentatively_accept(self, *, message_disposition: str) -> None:
            calls.append(("tentatively_accept", message_disposition))

        def decline(self, *, message_disposition: str) -> None:
            calls.append(("decline", message_disposition))

    gateway = EWSGateway(AppConfig(), account_factory=lambda: SimpleNamespace())
    item = MeetingItem()
    monkeypatch.setattr(gateway, "_resolve_item", lambda *_args: ("event", item))
    monkeypatch.setattr(
        gateway,
        "_bindings",
        lambda: SimpleNamespace(SEND_AND_SAVE_COPY="send", SAVE_ONLY="save"),
    )
    monkeypatch.setattr(gateway, "_call", lambda _name, operation, **_kwargs: operation())

    for response in CalendarResponse:
        gateway.respond_to_meeting(MeetingResponsePlan("event-1", response))

    assert calls == [
        ("accept", "send"),
        ("tentatively_accept", "send"),
        ("decline", "send"),
    ]


class _MutableCalendarGateway:
    def __init__(self, events: tuple[CalendarEvent, ...]) -> None:
        self.events = {event.reference: event for event in events}
        self.responses: list[MeetingResponsePlan] = []

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_reference: str | None,
        limit: int,
    ) -> tuple[CalendarEvent, ...]:
        assert calendar_reference is None
        return tuple(
            event
            for event in self.events.values()
            if event.start.astimezone(UTC) < end and event.end.astimezone(UTC) > start
        )[:limit]

    def get_event(self, reference: str) -> CalendarEvent:
        return self.events[reference]

    def create_event(self, plan: EventCreatePlan) -> CalendarEvent:
        self.events[plan.event.reference] = plan.event
        return plan.event

    def update_event(self, plan: EventUpdatePlan) -> CalendarEvent:
        updated = replace(self.events[plan.reference], **plan.changes)
        self.events[plan.reference] = updated
        return updated

    def delete_event(self, plan: EventDeletePlan) -> None:
        self.events.pop(plan.reference)

    def respond_to_meeting(self, plan: MeetingResponsePlan) -> CalendarEvent:
        self.responses.append(plan)
        return self.events[plan.reference]

    def get_availability(
        self,
        attendees: tuple[Address, ...],
        *,
        start: datetime,
        end: datetime,
        interval_minutes: int,
    ) -> tuple[dict[str, object], ...]:
        slots = int((end - start).total_seconds() // (interval_minutes * 60))
        return tuple(
            {
                "attendee": attendee,
                "available": True,
                "merged": "0" * slots,
                "events": (),
            }
            for attendee in attendees
        )


class _CalendarHarness(App[None]):
    def __init__(self, backend: CalendarServiceBackend) -> None:
        super().__init__()
        self.backend = backend
        self.pim_actions: list[PimActionRequested] = []

    def on_mount(self) -> None:
        self.push_screen(CalendarScreen(self.backend, timezone="UTC"))

    def on_pim_action_requested(self, event: PimActionRequested) -> None:
        self.pim_actions.append(event)


async def _wait_for_calendar_rows(
    app: _CalendarHarness,
    pilot: object,
    expected: int,
) -> CalendarScreen:
    for _ in range(50):
        await pilot.pause(0.05)  # type: ignore[attr-defined]
        if isinstance(app.screen, CalendarScreen):
            table = app.screen.query_one("#calendar-table", DataTable)
            if table.row_count == expected:
                return app.screen
    assert isinstance(app.screen, CalendarScreen)
    assert app.screen.query_one("#calendar-table", DataTable).row_count == expected
    return app.screen


@pytest.mark.asyncio
async def test_calendar_screen_executes_crud_response_ics_and_related_mail(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    meeting = CalendarEvent(
        reference="meeting-1",
        subject="Planning workshop",
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
        timezone="UTC",
        location="Room C-101",
        organizer=Address("organizer@example.test", "Organizer"),
        required_attendees=(Address("student@example.test", "Student"),),
        is_meeting=True,
        reminder_minutes=15,
        recurrence={"freq": "daily", "count": 2},
    )
    gateway = _MutableCalendarGateway((meeting,))
    app = _CalendarHarness(CalendarServiceBackend(CalendarService(gateway)))

    async with app.run_test(size=(120, 35)) as pilot:
        screen = await _wait_for_calendar_rows(app, pilot, 1)
        assert screen.query_one("#calendar-root").region.size == (120, 35)
        assert "Planning workshop" in str(screen.query_one("#calendar-detail", Static).content)

        screen.query_one("#calendar-search", Input).value = "workshop"
        screen.action_refresh()
        screen = await _wait_for_calendar_rows(app, pilot, 1)
        screen.query_one("#calendar-view", Select).value = CalendarViewKind.WEEK.value
        for _ in range(30):
            await pilot.pause(0.05)
            if screen.view_kind is CalendarViewKind.WEEK:
                break
        assert screen.view_kind is CalendarViewKind.WEEK
        screen.query_one("#calendar-search", Input).value = ""

        screen.action_details()
        for _ in range(30):
            await pilot.pause(0.05)
            if "Complete event details" in str(screen.query_one("#area-state", Static).content):
                break
        assert "Room C-101" in str(screen.query_one("#calendar-detail", Static).content)

        screen.action_respond(CalendarResponse.TENTATIVE)
        await pilot.pause()
        assert isinstance(app.screen, MeetingResponseScreen)
        app.screen.query_one("#meeting-response-send").press()
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(True)
        for _ in range(30):
            await pilot.pause(0.05)
            if gateway.responses:
                break
        assert gateway.responses[0].response is CalendarResponse.TENTATIVE

        screen = app.screen
        assert isinstance(screen, CalendarScreen)
        screen.action_create()
        await pilot.pause()
        assert isinstance(app.screen, CalendarEditorScreen)
        app.screen.query_one("#calendar-edit-subject", Input).value = "Created in TUI"
        app.screen.query_one("#calendar-edit-repeat", Select).value = "weekly"
        app.screen.query_one("#calendar-edit-repeat-interval", Input).value = "2"
        app.screen.query_one("#calendar-edit-repeat-count", Input).value = "4"
        app.screen.action_save()
        screen = await _wait_for_calendar_rows(app, pilot, 2)
        created_reference = next(
            reference
            for reference, event in gateway.events.items()
            if event.subject == "Created in TUI"
        )
        assert screen._highlighted == created_reference
        assert gateway.events[created_reference].recurrence is not None
        assert gateway.events[created_reference].recurrence["pattern"] == {
            "type": "weekly",
            "interval": 2,
            "weekdays": [gateway.events[created_reference].start.strftime("%A").casefold()],
            "first_day_of_week": "monday",
        }
        assert gateway.events[created_reference].recurrence["boundary"]["occurrences"] == 4

        screen.action_edit()
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, CalendarScopeScreen):
                break
        assert isinstance(app.screen, CalendarScopeScreen)
        app.screen.query_one("#calendar-scope-occurrence").press()
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, CalendarEditorScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, CalendarEditorScreen)
        app.screen.query_one("#calendar-edit-subject", Input).value = "Edited in TUI"
        app.screen.action_save()
        await pilot.pause()
        assert not isinstance(app.screen, CalendarEditorScreen), str(
            app.screen.query_one("#calendar-editor-error", Static).content
        )
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(True)
        screen = await _wait_for_calendar_rows(app, pilot, 2)
        assert gateway.events[created_reference].subject == "Edited in TUI"

        export_path = tmp_path / "selected.ics"
        screen._selected.clear()
        screen._select(created_reference)
        screen.action_toggle_selection()
        screen.action_export()
        await pilot.pause()
        assert isinstance(app.screen, FilePathScreen)
        app.screen.query_one("#pim-file-path", Input).value = str(export_path)
        app.screen.query_one("#pim-file-continue").press()
        for _ in range(30):
            await pilot.pause(0.05)
            if export_path.exists() and (
                isinstance(app.screen, CalendarScreen) and app.screen.is_mounted
            ):
                break
        assert "BEGIN:VCALENDAR" in export_path.read_text(encoding="utf-8")

        screen = app.screen
        assert isinstance(screen, CalendarScreen)
        imported = replace(
            meeting,
            reference="imported-1",
            subject="Imported from ICS",
            start=now + timedelta(hours=3),
            end=now + timedelta(hours=4),
            is_meeting=False,
            required_attendees=(),
            recurrence=None,
        )
        import_path = tmp_path / "import.ics"
        import_path.write_text(CalendarService(gateway).export_ics((imported,)), encoding="utf-8")
        screen.action_import()
        await pilot.pause()
        assert isinstance(app.screen, FilePathScreen)
        app.screen.query_one("#pim-file-path", Input).value = str(import_path)
        app.screen.query_one("#pim-file-continue").press()
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(True)
        screen = await _wait_for_calendar_rows(app, pilot, 3)
        assert any(event.subject == "Imported from ICS" for event in gateway.events.values())

        screen._selected.clear()
        screen._select("meeting-1")
        screen.action_related()
        await pilot.pause()
        assert app.pim_actions[-1].action == "related_messages"
        assert app.pim_actions[-1].references == ("meeting-1",)

        screen._selected.clear()
        screen._select(created_reference)
        screen.action_delete()
        await pilot.pause()
        assert isinstance(app.screen, CalendarScopeScreen)
        app.screen.query_one("#calendar-scope-occurrence").press()
        await pilot.pause()
        assert isinstance(app.screen, OperationalConfirmationScreen)
        app.screen.dismiss(True)
        await _wait_for_calendar_rows(app, pilot, 2)
        assert created_reference not in gateway.events


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 30), (79, 26), (192, 50)])
async def test_calendar_screen_reflows_without_hiding_operations(size: tuple[int, int]) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    event = CalendarEvent(
        "event-responsive",
        "Responsive calendar",
        now + timedelta(hours=1),
        now + timedelta(hours=2),
        timezone="UTC",
    )
    gateway = _MutableCalendarGateway((event,))
    app = _CalendarHarness(CalendarServiceBackend(CalendarService(gateway)))

    async with app.run_test(size=size) as pilot:
        screen = await _wait_for_calendar_rows(app, pilot, 1)
        assert screen.query_one("#calendar-root").region.size == size
        assert screen.query_one("#calendar-content").region.height > 0
        assert screen.query_one("#calendar-actions").region.height > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exchange_value,expected",
    [
        (None, "busy"),
        ("Free", "free"),
        ("Busy", "busy"),
        ("Tentative", "tentative"),
        ("OOF", "out_of_office"),
        ("WorkingElsewhere", "working_elsewhere"),
    ],
)
async def test_exchange_availability_and_disabled_reminder_round_trip_through_editor(
    exchange_value, expected
):
    from exchangelib import CalendarItem

    from gucmail.ews.mapping import map_calendar_event

    original = _event()
    mapped = map_calendar_event(
        CalendarItem(
            subject=original.subject,
            start=original.start,
            end=original.end,
            legacy_free_busy_status=exchange_value,
            reminder_is_set=False,
            reminder_minutes_before_start=15,
        )
    )
    assert mapped.availability == expected
    app = App()
    async with app.run_test(size=(110, 40)) as pilot:
        screen = CalendarEditorScreen(mapped, timezone="UTC")
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#calendar-edit-reminder", Input).value == ""
        selector = screen.query_one("#calendar-edit-availability", Select)
        assert selector.value == expected
        # The human-friendly Away label must emit the service's canonical value.
        selector.value = "out_of_office"
        edited = screen._value()
        assert edited.availability == "out_of_office"
        service = CalendarService(_CalendarGateway(mapped))
        plan = service.plan_update(
            mapped.reference, CalendarPatch(availability=edited.availability), current=mapped
        )
        assert plan.changes.get("availability", mapped.availability) == "out_of_office"
        await pilot.press("escape")
        assert not isinstance(app.screen, CalendarEditorScreen)
