"""Calendar screens: the calendar view, event editor and meeting replies."""

from __future__ import annotations

import calendar as month_calendar
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from gucmail.errors import ConfigurationError
from gucmail.formatting import human_bytes
from gucmail.models import (
    Address,
    CalendarEvent,
    CalendarResponse,
)
from gucmail.services.calendar import (
    CalendarView,
    CalendarViewKind,
    RecurrenceScope,
    detect_conflicts,
)
from gucmail.services.calendar_display import (
    CalendarDisplayPreferences,
    friendly_event_times,
)
from gucmail.services.calendar_history import CalendarSeriesHistory, history_text
from gucmail.services.calendar_paging import CalendarCursor
from gucmail.services.calendar_preferences import CalendarOperationPreferences
from gucmail.services.calendar_working_time import (
    CalendarWorkingTime,
    find_common_free_time,
    validate_free_time_request,
)
from gucmail.services.pim_preferences import CalendarCreationPreferences
from gucmail.tui.area_common import (
    OfflineOperationError,
    OperationalScreen,
    PublicOperationError,
    ViewState,
    clean_cell,
    current_row_highlight,
)
from gucmail.tui.events import PimActionRequested
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.pim_backends import (
    _addresses,
    _bounded_limit,
    _calendar_recurrence_editor_values,
    _calendar_recurrence_value,
    _optional,
    _parse_datetime,
)
from gucmail.tui.pim_common import (
    BatchActionResult,
    CalendarEditorValue,
    CalendarListRequest,
    CalendarScopeScreen,
    CalendarScreenBackend,
    ExportResult,
    FilePathScreen,
    _attachment_paths,
    _capability,
    _capability_summary,
    _capability_text,
    _PimEditorScreen,
    _recurring_event,
)
from gucmail.tui.pim_submission import SubmissionWorkspace


class CalendarEditorScreen(_PimEditorScreen[CalendarEditorValue | None]):
    """Validated Calendar create/edit form."""

    DEFAULT_CSS = """
    CalendarEditorScreen { align: center middle; }
    #calendar-editor {
        width: 96%; max-width: 102; height: 96%; max-height: 39; border: round $border;
        background: $surface; padding: 1 2;
    }
    #calendar-editor-title { height: 2; text-style: bold; }
    #calendar-editor-fields { height: 1fr; }
    #calendar-editor-fields Input, #calendar-editor-fields Select { height: 3; }
    #calendar-editor-body { height: 7; }
    #calendar-editor-error { min-height: 1; height: auto; color: $error; }
    #calendar-editor-actions { height: 3; align-horizontal: right; }
    #calendar-editor-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(
        self,
        event: CalendarEvent | None,
        *,
        timezone: str,
        focus_field: str | None = None,
        field_value: str | None = None,
        field_operation: str = "set",
        creating: bool = False,
        scope: RecurrenceScope = RecurrenceScope.OCCURRENCE,
    ) -> None:
        super().__init__()
        self.event = event
        self.timezone = timezone
        self.focus_field = focus_field
        self.field_value = field_value
        self.field_operation = field_operation
        self.creating = creating
        self.scope = scope

    def compose(self) -> ComposeResult:
        event = self.event
        now = datetime.now(ZoneInfo(self.timezone)).replace(second=0, microsecond=0)
        start = event.start if event else now + timedelta(hours=1)
        end = event.end if event else start + timedelta(hours=1)
        repeat_kind, repeat_interval, repeat_count, repeat_until, recurrence_json = (
            _calendar_recurrence_editor_values(event)
        )
        with Vertical(id="calendar-editor"):
            yield Label(
                "Create event"
                if event is None or self.creating
                else (
                    "Edit entire series"
                    if self.scope is RecurrenceScope.SERIES
                    else "Edit this occurrence"
                    if _recurring_event(event)
                    else "Edit event"
                ),
                id="calendar-editor-title",
                markup=False,
            )
            with VerticalScroll(id="calendar-editor-fields"):
                yield Label("Subject")
                yield Input(
                    value=event.subject if event else "",
                    placeholder="Subject",
                    id="calendar-edit-subject",
                )
                yield Label("Start · YYYY-MM-DD HH:MM (offset or selected zone)")
                yield Input(
                    value=start.isoformat(sep=" ", timespec="minutes"),
                    placeholder="Start (ISO date/time)",
                    id="calendar-edit-start",
                )
                yield Label("End · YYYY-MM-DD HH:MM (offset or selected zone)")
                yield Input(
                    value=end.isoformat(sep=" ", timespec="minutes"),
                    placeholder="End (ISO date/time)",
                    id="calendar-edit-end",
                )
                yield Label("Time zone · e.g. Africa/Cairo")
                yield Input(
                    value=event.timezone if event else self.timezone,
                    placeholder="Time zone",
                    id="calendar-edit-timezone",
                )
                yield Label("Location")
                yield Input(
                    value=event.location or "" if event else "",
                    placeholder="Location",
                    id="calendar-edit-location",
                )
                yield Label("Required attendees")
                yield Input(
                    value=", ".join(address.email for address in event.required_attendees)
                    if event
                    else "",
                    placeholder="Required attendees (comma separated)",
                    id="calendar-edit-required",
                )
                yield Label("Optional attendees")
                yield Input(
                    value=", ".join(address.email for address in event.optional_attendees)
                    if event
                    else "",
                    placeholder="Optional attendees (comma separated)",
                    id="calendar-edit-optional",
                )
                yield Label("Reminder · minutes before start (blank disables)")
                yield Input(
                    value=str(event.reminder_minutes)
                    if event and event.reminder_minutes is not None
                    else "",
                    placeholder="Reminder minutes (blank disables)",
                    id="calendar-edit-reminder",
                )
                yield Label("Availability")
                yield Select(
                    (
                        ("Free", "free"),
                        ("Working elsewhere", "working_elsewhere"),
                        ("Tentative", "tentative"),
                        ("Busy", "busy"),
                        ("Away", "out_of_office"),
                    ),
                    value=event.availability if event else "busy",
                    allow_blank=False,
                    id="calendar-edit-availability",
                )
                yield Static(
                    "Repeat · choose a common pattern, or Advanced for a complete rule",
                    id="calendar-repeat-label",
                    markup=False,
                )
                yield Select(
                    (
                        ("Does not repeat", "none"),
                        ("Daily", "daily"),
                        ("Weekly on the start day", "weekly"),
                        ("Monthly on the start date", "monthly"),
                        ("Yearly on the start date", "yearly"),
                        ("Advanced rule", "advanced"),
                    ),
                    value=repeat_kind,
                    allow_blank=False,
                    id="calendar-edit-repeat",
                )
                interval_label = Label(
                    "Repeat every · number of periods", id="calendar-repeat-interval-label"
                )
                interval_label.display = repeat_kind not in {"none", "advanced"}
                yield interval_label
                interval_input = Input(
                    value=repeat_interval,
                    placeholder="Repeat every N periods",
                    id="calendar-edit-repeat-interval",
                )
                interval_input.display = repeat_kind not in {"none", "advanced"}
                yield interval_input
                count_label = Label(
                    "Occurrences · blank for no end", id="calendar-repeat-count-label"
                )
                count_label.display = repeat_kind not in {"none", "advanced"}
                yield count_label
                count_input = Input(
                    value=repeat_count,
                    placeholder="Number of occurrences (blank = no end)",
                    id="calendar-edit-repeat-count",
                )
                count_input.display = repeat_kind not in {"none", "advanced"}
                yield count_input
                until_label = Label(
                    "Or repeat until · YYYY-MM-DD", id="calendar-repeat-until-label"
                )
                until_label.display = repeat_kind not in {"none", "advanced"}
                yield until_label
                until_input = Input(
                    value=repeat_until,
                    placeholder="Or repeat until YYYY-MM-DD",
                    id="calendar-edit-repeat-until",
                )
                until_input.display = repeat_kind not in {"none", "advanced"}
                yield until_input
                advanced_label = Label(
                    "Advanced recurrence rule · JSON", id="calendar-repeat-advanced-label"
                )
                advanced_label.display = repeat_kind == "advanced"
                yield advanced_label
                advanced_input = Input(
                    value=recurrence_json,
                    placeholder="Advanced canonical recurrence JSON",
                    id="calendar-edit-recurrence",
                )
                advanced_input.display = repeat_kind == "advanced"
                yield advanced_input
                if event and event.attachments:
                    yield Static(
                        "Existing attachments:\n"
                        + "\n".join(
                            f"{item.reference} · {item.name} · {item.size or 0} bytes"
                            for item in event.attachments
                        ),
                        id="calendar-existing-attachments",
                        markup=False,
                    )
                yield Label("Add attachment paths · semicolon separated")
                yield Input(
                    placeholder="Add attachment paths (semicolon separated)",
                    id="calendar-edit-attachments",
                )
                yield Label("Remove attachment references · comma separated")
                yield Input(
                    placeholder="Remove existing attachment references (comma separated)",
                    id="calendar-edit-remove-attachments",
                )
                yield Checkbox(
                    "All-day event",
                    value=event.is_all_day if event else False,
                    id="calendar-edit-all-day",
                )
                yield Label("Body")
                yield TextArea(event.body if event else "", id="calendar-editor-body")
            yield Static("", id="calendar-editor-error", markup=False)
            with Horizontal(id="calendar-editor-actions"):
                yield Button("Cancel", id="calendar-edit-cancel")
                yield Button("Save", id="calendar-edit-save", variant="primary")

    def on_mount(self) -> None:
        if self.event and (self.event.item_type or "").casefold() in {"occurrence", "exception"}:
            for disabled_selector in (
                "#calendar-edit-repeat",
                "#calendar-edit-repeat-interval",
                "#calendar-edit-repeat-count",
                "#calendar-edit-repeat-until",
                "#calendar-edit-recurrence",
            ):
                self.query_one(disabled_selector).disabled = True
        selector = {
            "required_attendees": "#calendar-edit-required",
            "optional_attendees": "#calendar-edit-optional",
            "reminder_minutes": "#calendar-edit-reminder",
            "recurrence": "#calendar-edit-repeat",
            "attachments": "#calendar-edit-attachments",
        }.get(self.focus_field or "")
        if selector == "#calendar-edit-required" and self.field_value:
            editor = self.query_one(selector, Input)
            existing = [
                candidate.strip()
                for candidate in editor.value.replace(";", ",").split(",")
                if candidate.strip()
            ]
            target = self.field_value.strip()
            if self.field_operation == "remove":
                existing = [
                    candidate for candidate in existing if candidate.casefold() != target.casefold()
                ]
            elif target and target.casefold() not in {
                candidate.casefold() for candidate in existing
            }:
                existing.append(target)
            editor.value = ", ".join(existing)
        if selector:
            self.query_one(selector).focus()

    @on(Select.Changed, "#calendar-edit-repeat")
    def _repeat_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        simple = str(event.value) not in {"none", "advanced"}
        for selector in (
            "#calendar-edit-repeat-interval",
            "#calendar-edit-repeat-count",
            "#calendar-edit-repeat-until",
        ):
            self.query_one(selector, Input).display = simple
        for selector in (
            "#calendar-repeat-interval-label",
            "#calendar-repeat-count-label",
            "#calendar-repeat-until-label",
        ):
            self.query_one(selector, Label).display = simple
        self.query_one("#calendar-edit-recurrence", Input).display = str(event.value) == "advanced"
        self.query_one("#calendar-repeat-advanced-label", Label).display = (
            str(event.value) == "advanced"
        )
        self.call_after_refresh(self._reveal_focus)

    def _value(self) -> CalendarEditorValue:
        timezone = self.query_one("#calendar-edit-timezone", Input).value.strip()
        subject = self.query_one("#calendar-edit-subject", Input).value.strip()
        if not subject:
            raise PublicOperationError("An event needs a subject.", retryable=False)
        if not timezone:
            raise PublicOperationError("An event needs a time zone.", retryable=False)
        start = _parse_datetime(
            self.query_one("#calendar-edit-start", Input).value, timezone, field="Start"
        )
        end = _parse_datetime(
            self.query_one("#calendar-edit-end", Input).value, timezone, field="End"
        )
        if end.astimezone(UTC) <= start.astimezone(UTC):
            raise PublicOperationError("Event end must be after its start.", retryable=False)
        reminder_text = self.query_one("#calendar-edit-reminder", Input).value.strip()
        try:
            reminder = int(reminder_text) if reminder_text else None
        except ValueError as exc:
            raise PublicOperationError(
                "Reminder minutes must be a whole number.", retryable=False
            ) from exc
        if reminder is not None and not 0 <= reminder <= 525_600:
            raise PublicOperationError(
                "Reminder minutes must be between 0 and 525600.", retryable=False
            )
        availability = self.query_one("#calendar-edit-availability", Select).value
        if availability is Select.BLANK:
            raise PublicOperationError("Choose event availability.", retryable=False)
        repeat_kind = self.query_one("#calendar-edit-repeat", Select).value
        if repeat_kind is Select.BLANK:
            raise PublicOperationError("Choose whether the event repeats.", retryable=False)
        return CalendarEditorValue(
            subject=subject,
            start=start,
            end=end,
            timezone=timezone,
            location=_optional(self.query_one("#calendar-edit-location", Input).value),
            body=self.query_one("#calendar-editor-body", TextArea).text,
            required_attendees=_addresses(self.query_one("#calendar-edit-required", Input).value),
            optional_attendees=_addresses(self.query_one("#calendar-edit-optional", Input).value),
            is_all_day=self.query_one("#calendar-edit-all-day", Checkbox).value,
            reminder_minutes=reminder,
            availability=str(availability),
            recurrence=_calendar_recurrence_value(
                kind=str(repeat_kind),
                interval_text=self.query_one("#calendar-edit-repeat-interval", Input).value,
                count_text=self.query_one("#calendar-edit-repeat-count", Input).value,
                until_text=self.query_one("#calendar-edit-repeat-until", Input).value,
                advanced_text=self.query_one("#calendar-edit-recurrence", Input).value,
                start=start,
            ),
            attachment_paths=_attachment_paths(
                self.query_one("#calendar-edit-attachments", Input).value
            ),
            remove_attachment_references=tuple(
                dict.fromkeys(
                    part.strip()
                    for part in self.query_one(
                        "#calendar-edit-remove-attachments", Input
                    ).value.split(",")
                    if part.strip()
                )
            ),
            scope=self.scope,
        )

    def action_save(self) -> None:
        if self._submission_blocked():
            return
        try:
            value = self._value()
        except PublicOperationError as exc:
            self._validation_failed(str(exc))
            return
        self._submit_value(value)

    def action_cancel(self) -> None:
        self._cancel_edit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "calendar-edit-save":
            self.action_save()
        elif event.button.id == "calendar-edit-cancel":
            self.action_cancel()


@dataclass(frozen=True, slots=True)
class MeetingResponseValue:
    response: CalendarResponse
    comment: str | None
    proposed_start: datetime | None = None
    proposed_end: datetime | None = None


@dataclass(frozen=True, slots=True)
class CalendarAvailabilityRequest:
    attendees: tuple[Address, ...]
    start: datetime
    end: datetime
    interval_minutes: int
    duration_minutes: int
    find_free_time: bool = False
    working_time: CalendarWorkingTime | None = None
    timezone: str | None = None


class CalendarAvailabilityScreen(ModalScreen[CalendarAvailabilityRequest | None]):
    """Bounded GetUserAvailability request with explicit attendees and window."""

    DEFAULT_CSS = """
    CalendarAvailabilityScreen { align: center middle; }
    #availability-dialog {
        width: 92%; max-width: 84; height: 90%; max-height: 28;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #availability-title { height: 2; text-style: bold; }
    #availability-fields { height: 1fr; }
    #availability-help { height: auto; min-height: 2; color: $text-muted; }
    .availability-field { width: 100%; }
    #availability-options { height: 3; }
    #availability-interval, #availability-duration { width: 1fr; margin-right: 1; }
    #availability-actions { height: 3; align-horizontal: right; }
    #availability-actions Button { margin-left: 1; }
    """

    def __init__(
        self,
        *,
        timezone: str,
        attendees: Sequence[Address] = (),
        start: datetime | None = None,
        end: datetime | None = None,
        find_free_time: bool = False,
        working_time: CalendarWorkingTime | None = None,
    ) -> None:
        super().__init__()
        self.timezone = timezone
        self.attendees = tuple(attendees)
        now = datetime.now(ZoneInfo(timezone)).replace(second=0, microsecond=0)
        self.start = start.astimezone(ZoneInfo(timezone)) if start else (now + timedelta(hours=1))
        self.end = end.astimezone(ZoneInfo(timezone)) if end else (self.start + timedelta(hours=8))
        self.find_free_time = find_free_time
        self.working_time = working_time

    def compose(self) -> ComposeResult:
        with Vertical(id="availability-dialog"):
            yield Label(
                ("Find free time" if self.find_free_time else "Check availability")
                + " · "
                + self.timezone,
                id="availability-title",
                markup=False,
            )
            with VerticalScroll(id="availability-fields"):
                yield Static(
                    "Up to 100 attendees and one time window. No calendar item is changed.",
                    id="availability-help",
                    markup=False,
                )
                yield Input(
                    value=", ".join(value.email for value in self.attendees),
                    placeholder="attendee1@example.edu, attendee2@example.edu",
                    id="availability-attendees",
                    classes="availability-field",
                )
                yield Input(
                    value=self.start.isoformat(sep=" ", timespec="minutes"),
                    placeholder="Start in local calendar time",
                    id="availability-start",
                    classes="availability-field",
                )
                yield Input(
                    value=self.end.isoformat(sep=" ", timespec="minutes"),
                    placeholder="End in local calendar time",
                    id="availability-end",
                    classes="availability-field",
                )
                with Horizontal(id="availability-options"):
                    yield Select(
                        (
                            ("15-minute grid", "15"),
                            ("30-minute grid", "30"),
                            ("60-minute grid", "60"),
                        ),
                        value="30",
                        allow_blank=False,
                        id="availability-interval",
                    )
                    yield Input(
                        value="60",
                        placeholder="Meeting minutes",
                        id="availability-duration",
                    )
                if self.find_free_time:
                    yield Checkbox(
                        "Use working days/hours",
                        value=True,
                        id="availability-working-time",
                    )
                    yield Static(
                        (self.working_time or CalendarWorkingTime()).description,
                        id="availability-working-description",
                        markup=False,
                    )
            with Horizontal(id="availability-actions"):
                yield Button("Cancel", id="availability-cancel")
                yield Button("Check", id="availability-check", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#availability-attendees", Input).focus()

    def _value(self) -> CalendarAvailabilityRequest:
        attendees = _addresses(self.query_one("#availability-attendees", Input).value)
        if not attendees or len(attendees) > 100:
            raise PublicOperationError(
                "Availability requires between 1 and 100 attendee addresses.",
                retryable=False,
            )
        start = _parse_datetime(
            self.query_one("#availability-start", Input).value,
            self.timezone,
            field="Availability start",
        )
        end = _parse_datetime(
            self.query_one("#availability-end", Input).value,
            self.timezone,
            field="Availability end",
        )
        if end.astimezone(UTC) <= start.astimezone(UTC):
            raise PublicOperationError("Availability end must be after start.", retryable=False)
        interval_value = self.query_one("#availability-interval", Select).value
        interval = int(str(interval_value))
        try:
            duration = int(self.query_one("#availability-duration", Input).value.strip())
        except ValueError as exc:
            raise PublicOperationError(
                "Meeting duration must be a whole number of minutes.", retryable=False
            ) from exc
        if not 5 <= duration <= 1440:
            raise PublicOperationError(
                "Meeting duration must be between 5 and 1440 minutes.", retryable=False
            )
        working_time = (
            self.working_time or CalendarWorkingTime()
            if self.find_free_time and self.query_one("#availability-working-time", Checkbox).value
            else None
        )
        try:
            validate_free_time_request(attendees, start, end, interval, duration)
            if working_time is not None:
                working_time.windows(start, end, self.timezone)
        except ConfigurationError as exc:
            raise PublicOperationError(str(exc), retryable=False) from exc
        return CalendarAvailabilityRequest(
            attendees,
            start,
            end,
            interval,
            duration,
            self.find_free_time,
            working_time,
            self.timezone,
        )

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "availability-cancel":
            self.dismiss(None)
            return
        try:
            self.dismiss(self._value())
        except PublicOperationError as exc:
            self.query_one("#availability-help", Static).update(clean_cell(exc, max_length=400))


class MeetingResponseScreen(ModalScreen[MeetingResponseValue | None]):
    """Explicit invitation response form."""

    DEFAULT_CSS = """
    MeetingResponseScreen { align: center middle; }
    #meeting-response-dialog {
        width: 92%; max-width: 72; height: auto; border: thick $warning;
        background: $surface; padding: 1 2;
    }
    #meeting-response-title { height: 2; text-style: bold; }
    #meeting-response-comment { height: 6; }
    #meeting-response-actions { height: 3; align-horizontal: right; }
    #meeting-response-actions Button { margin-left: 1; }
    """

    def __init__(
        self,
        initial_response: CalendarResponse = CalendarResponse.ACCEPT,
        *,
        timezone: str = "UTC",
        proposed_start: datetime | None = None,
        proposed_end: datetime | None = None,
    ) -> None:
        super().__init__()
        self.initial_response = initial_response
        self.timezone = timezone
        self.proposed_start = proposed_start
        self.proposed_end = proposed_end

    def compose(self) -> ComposeResult:
        with Vertical(id="meeting-response-dialog"):
            yield Label("Respond to invitation", id="meeting-response-title")
            yield Select(
                (
                    ("Accept", CalendarResponse.ACCEPT.value),
                    ("Tentative", CalendarResponse.TENTATIVE.value),
                    ("Decline", CalendarResponse.DECLINE.value),
                ),
                value=self.initial_response.value,
                allow_blank=False,
                id="meeting-response-value",
            )
            yield TextArea("", id="meeting-response-comment")
            if self.proposed_start is not None and self.proposed_end is not None:
                yield Input(
                    value=self.proposed_start.isoformat(sep=" ", timespec="minutes"),
                    id="meeting-response-proposed-start",
                )
                yield Input(
                    value=self.proposed_end.isoformat(sep=" ", timespec="minutes"),
                    id="meeting-response-proposed-end",
                )
            with Horizontal(id="meeting-response-actions"):
                yield Button("Cancel", id="meeting-response-cancel")
                yield Button("Send response", id="meeting-response-send", variant="warning")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "meeting-response-cancel":
            self.dismiss(None)
            return
        if event.button.id != "meeting-response-send":
            return
        selected = self.query_one("#meeting-response-value", Select).value
        if selected is Select.BLANK:
            return
        comment = _optional(self.query_one("#meeting-response-comment", TextArea).text)
        proposed_start = None
        proposed_end = None
        if self.query("#meeting-response-proposed-start"):
            try:
                proposed_start = _parse_datetime(
                    self.query_one("#meeting-response-proposed-start", Input).value,
                    self.timezone,
                    field="Proposed start",
                )
                proposed_end = _parse_datetime(
                    self.query_one("#meeting-response-proposed-end", Input).value,
                    self.timezone,
                    field="Proposed end",
                )
            except PublicOperationError as exc:
                self.query_one("#meeting-response-title", Label).update(
                    clean_cell(exc, max_length=300)
                )
                return
        self.dismiss(
            MeetingResponseValue(
                CalendarResponse(str(selected)),
                comment,
                proposed_start,
                proposed_end,
            )
        )


class CalendarScreen(SubmissionWorkspace):
    """Agenda/day/week/month Calendar with service-backed CRUD and invitations."""

    DEFAULT_CSS = """
    CalendarScreen { layout: vertical; }
    #calendar-root { width: 100%; height: 100%; padding: 1 2; }
    #calendar-title { height: 2; text-style: bold; }
    #calendar-nav { height: 3; }
    #calendar-nav Button { margin-right: 1; }
    #calendar-search { width: 2fr; margin-right: 1; }
    #calendar-view { width: 1fr; }
    #calendar-capabilities { height: auto; min-height: 1; color: $text-muted; }
    #calendar-content { height: 1fr; min-height: 9; }
    #calendar-table { width: 3fr; height: 1fr; }
    #calendar-detail-scroll {
        width: 2fr; height: 1fr; border-left: solid $panel;
        padding: 1; overflow-y: auto;
    }
    #calendar-detail { height: auto; }
    #calendar-actions, #calendar-footer { height: auto; min-height: 3; }
    #calendar-actions Button { margin-right: 1; }
    #calendar-footer { align-horizontal: right; }
    #calendar-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("n", "create", "New event", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("h", "correspondence", "Correspondence", show=False),
        Binding("space", "toggle_selection", "Select", show=False),
        Binding("enter", "details", "Details", show=False),
        Binding("left", "previous_period", "Previous", show=False),
        Binding("right", "next_period", "Next", show=False),
        Binding("t", "today", "Today", show=False),
    ]

    def __init__(
        self,
        backend: CalendarScreenBackend,
        *,
        timezone: str = "UTC",
        initial_view: str = "agenda",
        first_weekday: int = 0,
        creation_preferences: Callable[
            [], CalendarCreationPreferences
        ] = CalendarCreationPreferences,
        working_time_preferences: Callable[[], CalendarWorkingTime] = CalendarWorkingTime,
        display_preferences: Callable[[], CalendarDisplayPreferences] | None = None,
        operation_preferences: Callable[
            [], CalendarOperationPreferences
        ] = CalendarOperationPreferences,
        page_size: int = 100,
        offline: bool = False,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            area_name="calendar",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        self.backend = backend
        self.operation_preferences = operation_preferences
        self._operations = operation_preferences()
        self.display_preferences = display_preferences or (
            lambda: CalendarDisplayPreferences(
                CalendarViewKind(initial_view), first_weekday, timezone
            )
        )
        display = self.display_preferences()
        self.timezone = display.timezone
        self.page_size = _bounded_limit(page_size)
        self.anchor_date = datetime.now(ZoneInfo(self.timezone)).date()
        self.view_kind = display.default_view
        self.first_weekday = display.first_weekday
        self.creation_preferences = creation_preferences
        self.working_time_preferences = working_time_preferences
        self._working_time = CalendarWorkingTime()
        self._events: tuple[CalendarEvent, ...] = ()
        self._by_reference: dict[str, CalendarEvent] = {}
        self._selection_after_refresh: str | None = None
        self._highlighted: str | None = None
        self._selected: set[str] = set()
        self._pending_action: tuple[str, Callable[[], None]] | None = None
        self._next_calendar_cursor: CalendarCursor | None = None
        self._last_calendar_request: CalendarListRequest | None = None
        self._calendar_cursor: CalendarCursor | None = None
        self._calendar_page_history: list[tuple[CalendarCursor | None, str | None]] = []

    @property
    def selected_invitation_reference(self) -> str | None:
        """Return a respondable highlighted meeting, never a plain appointment."""

        event = self._by_reference.get(self._highlighted or "")
        if event is None or not event.is_meeting:
            return None
        status = (event.response_status or "").rsplit(".", 1)[-1].casefold()
        return None if status == "organizer" else event.reference

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="calendar-root"):
            yield Label("Calendar", id="calendar-title")
            yield Static("Loading a calendar page…", id="area-state", markup=False)
            with HorizontalScroll(id="calendar-nav"):
                yield Button("<", id="calendar-previous")
                yield Button("Today", id="calendar-today")
                yield Button(">", id="calendar-next")
                yield Input(placeholder="Search this date range", id="calendar-search")
                yield Select(
                    (
                        ("Agenda", CalendarViewKind.AGENDA.value),
                        ("Day", CalendarViewKind.DAY.value),
                        ("Work week", CalendarViewKind.WORK_WEEK.value),
                        ("Week", CalendarViewKind.WEEK.value),
                        ("Month", CalendarViewKind.MONTH.value),
                    ),
                    value=self.view_kind.value,
                    allow_blank=False,
                    id="calendar-view",
                )
            yield Static(
                _capability_summary(self.backend.capabilities),
                id="calendar-capabilities",
                markup=False,
            )
            with Horizontal(id="calendar-content"):
                yield DataTable(id="calendar-table", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="calendar-detail-scroll"):
                    yield Static(
                        "Select an event to inspect it.", id="calendar-detail", markup=False
                    )
            with HorizontalScroll(id="calendar-actions"):
                yield Button("Create", id="calendar-create", variant="primary")
                yield Button("Edit", id="calendar-edit", disabled=True)
                yield Button("Delete / cancel", id="calendar-delete", disabled=True)
                yield Button("Respond", id="calendar-respond", disabled=True)
                yield Button("Import ICS", id="calendar-import")
                yield Button("Export ICS", id="calendar-export", disabled=True)
                yield Button("Related mail", id="calendar-related", disabled=True)
                yield Button("Series history", id="calendar-history", disabled=True)
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="calendar-footer"):
                yield Button("Previous results", id="calendar-earlier", disabled=True)
                yield Button("More results", id="calendar-more", disabled=True)
                retry = Button("Retry", id="area-retry")
                retry.display = False
                yield retry
                cancel = Button("Cancel operation", id="area-cancel-operation")
                cancel.display = False
                yield cancel
                yield Button("Refresh", id="calendar-refresh")
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        table = self.query_one("#calendar-table", DataTable)
        table.add_column("✓", key="selected", width=2)
        table.add_column("Subject", key="subject", width=28)
        table.add_column("When", key="start")
        table.add_column("Until", key="end")
        table.add_column("Location", key="location")
        table.add_column("Type", key="kind")
        self._apply_capabilities()
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Calendar is unavailable offline. Reopen without --offline to connect.",
            )
            return
        self.call_after_refresh(self.action_refresh)

    def _apply_capabilities(self) -> None:
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        self.query_one("#calendar-create", Button).disabled = not mutate
        capabilities = self.query_one("#calendar-capabilities", Static)
        capabilities.display = bool(_capability_summary(self.backend.capabilities))
        capabilities.tooltip = _capability_text(self.backend.capabilities)
        self.query_one("#calendar-import", Button).disabled = self.offline or not _capability(
            self.backend.capabilities, "import"
        )
        self._update_buttons()

    def action_refresh(self) -> None:
        self._calendar_page_history.clear()
        self._load_calendar_page()

    def _load_calendar_page(self, cursor: CalendarCursor | None = None) -> None:
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Calendar is unavailable offline. Reopen without --offline to connect.",
            )
            return
        try:
            self.refresh_display_preferences()
            self._working_time = self.working_time_preferences()
        except ConfigurationError as exc:
            self._show_error("Calendar preferences", exc)
            return
        request = CalendarListRequest(
            self.view_kind,
            self.anchor_date,
            self.query_one("#calendar-search", Input).value.strip(),
            self.page_size,
            self.first_weekday,
            self._working_time.weekdays,
            self.timezone,
            cursor,
        )
        if self._last_calendar_request is not None and (
            replace(request, cursor=None) != replace(self._last_calendar_request, cursor=None)
        ):
            request = replace(request, cursor=None)
            self._calendar_page_history.clear()
        self._last_calendar_request = request
        self._calendar_cursor = request.cursor
        self.query_one("#calendar-more", Button).disabled = True
        self.query_one("#calendar-earlier", Button).disabled = True
        paged_loader = getattr(self.backend, "list_events_page", None)
        self._begin_operation(
            "Loading calendar",
            lambda cancel, progress: (
                paged_loader(request, cancel=cancel, progress=progress)
                if callable(paged_loader)
                else tuple(self.backend.list_events(request, cancel=cancel, progress=progress))
            ),
            lambda result: self._events_loaded(result, request=request),
            retry_safe=True,
        )

    @on(Button.Pressed, "#calendar-more")
    def action_more_results(self) -> None:
        if self._next_calendar_cursor is not None and not self._operation_active:
            self._calendar_page_history.append((self._calendar_cursor, self._highlighted))
            self._calendar_page_history = self._calendar_page_history[-64:]
            self._load_calendar_page(self._next_calendar_cursor)

    @on(Button.Pressed, "#calendar-earlier")
    def action_previous_results(self) -> None:
        if self._calendar_page_history and not self._operation_active:
            cursor, reference = self._calendar_page_history.pop()
            self._selection_after_refresh = reference
            self._load_calendar_page(cursor)

    def refresh_display_preferences(self) -> None:
        """Apply presentation locally; query boundaries are captured on the next refresh."""

        display = self.display_preferences()
        self.timezone = display.timezone
        self.first_weekday = display.first_weekday
        if not self.is_mounted:
            return
        self.set_workspace_title(self.query_one("#calendar-title", Label), "Calendar")
        table = self.query_one("#calendar-table", DataTable)
        for event in self._events:
            start, end = friendly_event_times(event, self.timezone)
            table.update_cell(event.reference, "start", clean_cell(start, max_length=35))
            table.update_cell(event.reference, "end", clean_cell(end, max_length=35))
        if self._highlighted is not None:
            self._select(self._highlighted)

    def refresh_operation_preferences(self) -> None:
        """Update local presentation and action availability without a mailbox read."""
        self._operations = self.operation_preferences()
        if not self.is_mounted:
            return
        table = self.query_one("#calendar-table", DataTable)
        for event in self._events:
            table.update_cell(event.reference, "subject", self._subject_cell(event))
        self._update_buttons()

    def on_screen_resume(self) -> None:
        if self.is_mounted:
            try:
                self.refresh_operation_preferences()
                self.refresh_display_preferences()
            except ConfigurationError as exc:
                self._show_error("Calendar preferences", exc)

    def _subject_cell(self, event: CalendarEvent) -> str | Text:
        subject = clean_cell(event.subject or "(no subject)", max_length=90)
        color = self._operations.event_color(event)
        return Text(subject, style=color) if color else subject

    def _events_loaded(self, result: object, *, request: CalendarListRequest | None = None) -> None:
        page = result if isinstance(result, CalendarView) else None
        events = page.events if page is not None else cast(tuple[CalendarEvent, ...], result)
        self._next_calendar_cursor = page.continuation if page is not None else None
        self.query_one("#calendar-more", Button).disabled = self._next_calendar_cursor is None
        self.query_one("#calendar-earlier", Button).disabled = not self._calendar_page_history
        if not all(isinstance(item, CalendarEvent) for item in events):
            raise PublicOperationError("The Calendar backend returned invalid event records.")
        self._events = events
        self._by_reference = {event.reference: event for event in events}
        self._selected.intersection_update(self._by_reference)
        self._operations = self.operation_preferences()
        table = self.query_one("#calendar-table", DataTable)
        table.clear()
        for event in events:
            start, end = friendly_event_times(event, self.timezone)
            table.add_row(
                "●" if event.reference in self._selected else "",
                self._subject_cell(event),
                clean_cell(start, max_length=35),
                clean_cell(end, max_length=35),
                clean_cell(event.location or "", max_length=50),
                "Meeting" if event.is_meeting else "Event",
                key=event.reference,
            )
        if events:
            preferred = self._selection_after_refresh or self._highlighted
            self._selection_after_refresh = None
            reference = preferred if preferred in self._by_reference else events[0].reference
            self._select(reference)
            table.move_cursor(
                row=next(i for i, item in enumerate(events) if item.reference == reference)
            )
            self._set_view_state(
                ViewState.READY,
                page.notice
                if page is not None and page.notice
                else f"{len(events)} event{'s' if len(events) != 1 else ''}",
            )
        else:
            self._highlighted = None
            message = (page.notice if page is not None else None) or "No events in this range."
            self.query_one("#calendar-detail", Static).update(message)
            self._update_buttons()
            self._set_view_state(
                ViewState.READY if self._next_calendar_cursor else ViewState.EMPTY,
                message or "No events match this calendar view.",
            )
        pending = self._pending_action
        self._pending_action = None
        if pending is not None:
            label, callback = pending
            if self._highlighted is None:
                self._set_view_state(
                    ViewState.EMPTY,
                    f"No event is available to {clean_cell(label, max_length=120)}.",
                )
            else:
                self.call_after_refresh(callback)

    def queue_action(
        self,
        label: str,
        callback: Callable[[], None],
        *,
        requires_selection: bool,
    ) -> None:
        """Run a palette action now or after the initial bounded page is ready."""

        if not requires_selection or self._highlighted is not None:
            callback()
            return
        if self._operation_active or self.view_state in {ViewState.IDLE, ViewState.LOADING}:
            self._pending_action = (label, callback)
            self._set_view_state(
                ViewState.LOADING,
                f"Loading calendar before {clean_cell(label, max_length=120)}…",
            )
            return
        self._set_view_state(
            ViewState.EMPTY,
            f"No event is available to {clean_cell(label, max_length=120)}.",
        )

    @on(DataTable.RowHighlighted, "#calendar-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select(str(event.row_key.value))

    @on(DataTable.RowSelected, "#calendar-table")
    def _row_selected(self) -> None:
        self.action_details()

    def _select(self, reference: str) -> None:
        event = self._by_reference.get(reference)
        if event is None:
            return
        self._highlighted = reference
        self.query_one("#calendar-detail", Static).update(
            _render_event(event, display_timezone=self.timezone)
        )
        self._update_buttons()

    def _targets(self) -> tuple[str, ...]:
        if self._selected:
            return tuple(
                reference for reference in self._by_reference if reference in self._selected
            )
        return (
            (self._highlighted,)
            if self._highlighted is not None and self._highlighted in self._by_reference
            else ()
        )

    def _update_buttons(self) -> None:
        has_target = bool(self._targets())
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        for selector in ("#calendar-edit", "#calendar-delete"):
            widgets = self.query(selector)
            if widgets:
                widgets.first(Button).disabled = not has_target or not mutate
        for selector, key in (("#calendar-export", "export"), ("#calendar-related", "related")):
            widgets = self.query(selector)
            if widgets:
                widgets.first(Button).disabled = (
                    not has_target
                    or not _capability(self.backend.capabilities, key, default=key == "related")
                    or (key == "related" and not self._operations.show_related_emails)
                )
        event = self._by_reference.get(self._highlighted or "")
        widgets = self.query("#calendar-history")
        if widgets:
            widgets.first(Button).disabled = (
                event is None
                or self.offline
                or not _capability(self.backend.capabilities, "history", default=True)
                or not callable(getattr(self.backend, "history", None))
            )
        widgets = self.query("#calendar-respond")
        if widgets:
            organizer = bool(
                event
                and event.response_status
                and event.response_status.rsplit(".", 1)[-1].casefold() == "organizer"
            )
            widgets.first(Button).disabled = (
                event is None
                or not event.is_meeting
                or organizer
                or self.offline
                or not _capability(self.backend.capabilities, "respond")
            )

    def action_toggle_selection(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        if reference in self._selected:
            self._selected.remove(reference)
        else:
            self._selected.add(reference)
        self.query_one("#calendar-table", DataTable).update_cell(
            reference, "selected", "●" if reference in self._selected else ""
        )
        self._update_buttons()
        self._set_view_state(
            ViewState.READY, f"{len(self._selected)} events selected for batch actions."
        )

    def action_details(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        self._begin_operation(
            "Loading event details",
            lambda cancel, progress: self.backend.get_event(
                reference, cancel=cancel, progress=progress
            ),
            self._detail_loaded,
            retry_safe=True,
        )

    def action_history(self) -> None:
        reference = self._highlighted
        operation = getattr(self.backend, "history", None)
        if reference is None or self.offline or not callable(operation):
            return
        self._begin_operation(
            "Loading series history",
            lambda cancel, progress: operation(reference, cancel=cancel, progress=progress),
            self._history_loaded,
            retry_safe=True,
        )

    def _history_loaded(self, result: object) -> None:
        if not isinstance(result, CalendarSeriesHistory):
            raise PublicOperationError("The Calendar backend returned invalid occurrence history.")
        if result.requested_reference != self._highlighted:
            self._set_view_state(
                ViewState.READY, "History result discarded after selection changed."
            )
            return
        self.query_one("#calendar-detail", Static).update(history_text(result, self.timezone))
        self._set_view_state(ViewState.READY, "Read-only series occurrence metadata loaded.")

    def _detail_loaded(self, result: object) -> None:
        if not isinstance(result, CalendarEvent):
            raise PublicOperationError("The Calendar backend returned invalid event details.")
        self._by_reference[result.reference] = result
        self.query_one("#calendar-detail", Static).update(
            _render_event(result, complete=True, display_timezone=self.timezone)
        )
        self._set_view_state(ViewState.READY, "Complete event details loaded.")

    def action_create(self) -> None:
        if self.offline or not _capability(self.backend.capabilities, "create"):
            self._show_error(
                "Create event",
                OfflineOperationError()
                if self.offline
                else PublicOperationError("Event creation is unavailable.", retryable=False),
            )
            return
        try:
            event = self.creation_preferences().new_event(timezone=self.timezone)
        except ConfigurationError as exc:
            self._show_error("Create event", exc)
            return
        self._open_submission_editor(
            CalendarEditorScreen(event, timezone=self.timezone, creating=True), self._create_value
        )

    def action_create_prefilled(
        self,
        required_attendees: Sequence[Address],
        *,
        subject: str = "",
        start: datetime | None = None,
    ) -> None:
        """Open a create form populated by another product area."""

        if self.offline or not _capability(self.backend.capabilities, "create"):
            self._show_error("Create event", OfflineOperationError())
            return
        unique: dict[str, Address] = {}
        for attendee in required_attendees:
            email = attendee.email.strip()
            if email:
                unique.setdefault(email.casefold(), Address(email, attendee.name))
            if len(unique) >= 100:
                break
        try:
            seed = self.creation_preferences().new_event(
                reference=f"prefill-{uuid4().hex}",
                subject=subject.strip(),
                timezone=self.timezone,
                start=start,
            )
        except ConfigurationError as exc:
            self._show_error("Create event", exc)
            return
        prefill = replace(
            seed,
            required_attendees=tuple(unique.values()),
        )
        self._open_submission_editor(
            CalendarEditorScreen(prefill, timezone=self.timezone, creating=True),
            self._create_value,
        )

    def _create_value(self, value: CalendarEditorValue | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Creating event",
            lambda cancel, progress: self.backend.create_event(
                value, cancel=cancel, progress=progress
            ),
            self._mutation_item_loaded,
        )

    def action_edit(
        self,
        focus_field: str | None = None,
        *,
        field_value: str | None = None,
        field_operation: str = "set",
    ) -> None:
        reference = self._highlighted
        if reference is None:
            return
        self._begin_operation(
            "Loading event for edit",
            lambda cancel, progress: self.backend.get_event(
                reference, cancel=cancel, progress=progress
            ),
            lambda result: self._open_editor(
                result,
                focus_field=focus_field,
                field_value=field_value,
                field_operation=field_operation,
            ),
            retry_safe=True,
        )

    def _open_editor(
        self,
        result: object,
        *,
        focus_field: str | None = None,
        field_value: str | None = None,
        field_operation: str = "set",
        scope: RecurrenceScope | None = None,
    ) -> None:
        if not isinstance(result, CalendarEvent):
            raise PublicOperationError("The Calendar backend returned invalid event details.")
        if scope is None and _recurring_event(result):
            self.app.push_screen(
                CalendarScopeScreen(result, operation="Edit event"),
                lambda choice: self._edit_scope_chosen(
                    result,
                    choice,
                    focus_field=focus_field,
                    field_value=field_value,
                    field_operation=field_operation,
                ),
            )
            return
        self._open_submission_editor(
            CalendarEditorScreen(
                result,
                timezone=self.timezone,
                focus_field=focus_field,
                field_value=field_value,
                field_operation=field_operation,
                scope=scope or RecurrenceScope.OCCURRENCE,
            ),
            lambda value: self._edit_value(result.reference, value),
        )

    def _edit_scope_chosen(
        self,
        event: CalendarEvent,
        scope: RecurrenceScope | None,
        *,
        focus_field: str | None,
        field_value: str | None,
        field_operation: str,
    ) -> None:
        if scope is None:
            return
        if scope is RecurrenceScope.OCCURRENCE:
            self._open_editor(
                event,
                scope=scope,
                focus_field=focus_field,
                field_value=field_value,
                field_operation=field_operation,
            )
            return
        self._begin_operation(
            "Loading series for edit",
            lambda cancel, progress: self.backend.get_series_event(
                event.reference, cancel=cancel, progress=progress
            ),
            lambda result: self._open_editor(
                result,
                scope=scope,
                focus_field=focus_field,
                field_value=field_value,
                field_operation=field_operation,
            ),
            retry_safe=True,
        )

    def _edit_value(self, reference: str, value: CalendarEditorValue | None) -> None:
        if value is None:
            return
        event = self._by_reference.get(reference)
        if value.scope is RecurrenceScope.SERIES or (event and _recurring_event(event)):
            scope_label = (
                "Entire series" if value.scope is RecurrenceScope.SERIES else "This occurrence"
            )
            self._confirm(
                "Save recurrence changes?",
                f"Scope: {scope_label}\n"
                f"Subject: {clean_cell(value.subject, max_length=180)}\n"
                f"Start: {value.start.isoformat()}\nEnd: {value.end.isoformat()}\n"
                "Meeting changes may notify attendees.",
                lambda: self._start_update(reference, value),
                confirm_label="Save changes",
            )
            return
        self._start_update(reference, value)

    def _start_update(self, reference: str, value: CalendarEditorValue) -> None:
        self._begin_operation(
            "Updating event",
            lambda cancel, progress: self.backend.update_event(
                reference, value, cancel=cancel, progress=progress
            ),
            self._mutation_item_loaded,
        )

    def _mutation_item_loaded(self, result: object) -> None:
        if not isinstance(result, CalendarEvent):
            raise PublicOperationError("The Calendar backend returned an invalid event.")
        self._selection_after_refresh = result.reference
        self._highlighted = result.reference
        self.action_refresh()

    def action_delete(self) -> None:
        references = self._targets()
        if not references:
            return
        events = tuple(self._by_reference[reference] for reference in references)
        if len(events) == 1 and _recurring_event(events[0]):
            self.app.push_screen(
                CalendarScopeScreen(events[0], operation="Delete / cancel"),
                lambda scope: self._delete_scope_chosen(events[0], scope),
            )
            return
        if any((event.item_type or "").casefold() == "recurringmaster" for event in events):
            self._show_error(
                "Delete events",
                PublicOperationError(
                    "Select one recurring series at a time to review its entire-series scope.",
                    retryable=False,
                ),
            )
            return
        self._confirm_delete(events, RecurrenceScope.OCCURRENCE)

    def _delete_scope_chosen(self, event: CalendarEvent, scope: RecurrenceScope | None) -> None:
        if scope is None:
            return
        if scope is RecurrenceScope.OCCURRENCE:
            self._confirm_delete((event,), scope)
            return
        self._begin_operation(
            "Loading series deletion preview",
            lambda cancel, progress: self.backend.get_series_event(
                event.reference, cancel=cancel, progress=progress
            ),
            lambda result: self._series_delete_loaded(result),
            retry_safe=True,
        )

    def _series_delete_loaded(self, result: object) -> None:
        if not isinstance(result, CalendarEvent):
            raise PublicOperationError("The Calendar backend returned invalid series details.")
        self._confirm_delete((result,), RecurrenceScope.SERIES)

    def _confirm_delete(self, events: tuple[CalendarEvent, ...], scope: RecurrenceScope) -> None:
        references = tuple(event.reference for event in events)
        meetings = sum(event.is_meeting for event in events)
        scope_label = (
            "Entire series" if scope is RecurrenceScope.SERIES else "Selected events / occurrences"
        )
        self._confirm(
            "Delete or cancel selected events?",
            f"Scope: {scope_label}\n"
            + "\n".join(clean_cell(event.subject, max_length=180) for event in events)
            + f"\nEvents: {len(references)} · Meetings that may send cancellation "
            f"notices: {meetings}\n\n"
            + (
                "Every date in this series is affected."
                if scope is RecurrenceScope.SERIES
                else "Only this selection is affected."
            ),
            lambda: self._start_delete(references, scope=scope),
            confirm_label="Delete / cancel",
        )

    def action_cancel_meeting(self) -> None:
        event = self._by_reference.get(self._highlighted or "")
        if event is None:
            self._set_view_state(ViewState.EMPTY, "Select a meeting to cancel.")
            return
        if not event.is_meeting:
            self._set_view_state(
                ViewState.READY,
                "The selected item is an appointment, not a meeting. Use Delete event instead.",
            )
            return
        if _recurring_event(event):
            self.app.push_screen(
                CalendarScopeScreen(event, operation="Cancel meeting"),
                lambda scope: self._delete_scope_chosen(event, scope),
            )
            return
        self._confirm(
            "Cancel selected meeting?",
            f"Meeting: {clean_cell(event.subject, max_length=180)}\n"
            "Exchange will send cancellation notices to attendees. Only this meeting is affected.",
            lambda: self._start_delete((event.reference,)),
            confirm_label="Cancel meeting",
        )

    def _start_delete(
        self,
        references: tuple[str, ...],
        *,
        scope: RecurrenceScope = RecurrenceScope.OCCURRENCE,
    ) -> None:
        self._begin_operation(
            "Deleting/cancelling events",
            lambda cancel, progress: self.backend.delete_events(
                references, cancel=cancel, progress=progress, scope=scope
            ),
            self._batch_finished,
        )

    def _batch_finished(self, result: object) -> None:
        if not isinstance(result, BatchActionResult):
            raise PublicOperationError("The Calendar backend returned an invalid batch result.")
        self._selected.clear()
        if result.failed_references:
            self._set_view_state(
                ViewState.ERROR,
                f"Completed {result.completed}/{result.attempted}; "
                f"{len(result.failed_references)} require review.",
            )
        self.action_refresh()

    def action_respond(self, response: CalendarResponse | None = None) -> None:
        event = self._by_reference.get(self._highlighted or "")
        if event is None or not event.is_meeting:
            return
        try:
            chosen = response or self.operation_preferences().invitation_response
        except ConfigurationError as exc:
            self._show_error("Calendar response preferences", exc)
            return
        self.app.push_screen(
            MeetingResponseScreen(chosen, timezone=self.timezone),
            lambda value: self._response_value(event.reference, value),
        )

    def action_propose_new_time(self) -> None:
        event = self._by_reference.get(self._highlighted or "")
        if event is None or not event.is_meeting:
            return
        self.app.push_screen(
            MeetingResponseScreen(
                CalendarResponse.TENTATIVE,
                timezone=self.timezone,
                proposed_start=event.start,
                proposed_end=event.end,
            ),
            lambda value: self._response_value(event.reference, value),
        )

    def action_check_availability(self) -> None:
        self._open_availability(find_free_time=False)

    def action_find_free_time(self) -> None:
        self._open_availability(find_free_time=True)

    def _open_availability(self, *, find_free_time: bool) -> None:
        event = self._by_reference.get(self._highlighted or "")
        attendees = (
            (*event.required_attendees, *event.optional_attendees) if event is not None else ()
        )
        try:
            self.refresh_display_preferences()
            working_time = self.working_time_preferences() if find_free_time else None
        except ConfigurationError as exc:
            self._show_error("Calendar working time", exc)
            return
        self.app.push_screen(
            CalendarAvailabilityScreen(
                timezone=self.timezone,
                attendees=attendees,
                start=event.start if event is not None else None,
                end=(event.start + timedelta(hours=8) if event is not None else None),
                find_free_time=find_free_time,
                working_time=working_time,
            ),
            self._availability_value,
        )

    def _availability_value(self, value: CalendarAvailabilityRequest | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Finding free time" if value.find_free_time else "Checking availability",
            lambda cancel, progress: tuple(
                self.backend.availability(
                    value.attendees,
                    value.start,
                    value.end,
                    value.interval_minutes,
                    cancel=cancel,
                    progress=progress,
                )
            ),
            lambda result: self._availability_loaded(result, value),
            retry_safe=True,
        )

    def _availability_loaded(
        self,
        result: object,
        request: CalendarAvailabilityRequest,
    ) -> None:
        values = cast(tuple[Mapping[str, object], ...], result)
        lines = [
            f"Availability · {request.start.isoformat(timespec='minutes')} to "
            f"{request.end.isoformat(timespec='minutes')} · {request.interval_minutes}-minute grid"
        ]
        for value in values:
            attendee = value.get("attendee")
            address = (
                attendee.email if isinstance(attendee, Address) else str(attendee or "unknown")
            )
            available = bool(value.get("available"))
            events = value.get("events")
            event_count = len(events) if isinstance(events, Sequence) else 0
            lines.append(
                f"{clean_cell(address, max_length=180)} · "
                + (
                    f"{event_count} busy event(s)"
                    if available
                    else f"unavailable ({clean_cell(value.get('error') or 'provider error')})"
                )
            )
        if request.find_free_time:
            suggestion = find_common_free_time(
                values,
                attendees=request.attendees,
                start=request.start,
                end=request.end,
                interval_minutes=request.interval_minutes,
                duration_minutes=request.duration_minutes,
                timezone=request.timezone or self.timezone,
                working_time=request.working_time,
            )
            if request.working_time is not None:
                lines.append("Working time: " + request.working_time.description)
            else:
                lines.append("Searching all hours in the explicit window.")
            if suggestion.start is None or suggestion.end is None:
                lines.append(
                    "A common free slot could not be confirmed in this window. " + suggestion.reason
                )
            else:
                lines.append(
                    "First common free slot: "
                    f"{suggestion.start.isoformat(timespec='minutes')} to "
                    f"{suggestion.end.isoformat(timespec='minutes')}"
                )
        self.query_one("#calendar-detail", Static).update("\n".join(lines))
        self._set_view_state(ViewState.READY, "Availability loaded.")

    def action_show_conflicts(self) -> None:
        conflicts = detect_conflicts(self._events)
        if not conflicts:
            self.query_one("#calendar-detail", Static).update("No conflicts in this calendar view.")
            self._set_view_state(ViewState.READY, "No blocking event overlaps detected.")
            return
        lines = [f"{len(conflicts)} conflict(s) in this view:"]
        lines.extend(
            f"{item.first.subject} ↔ {item.second.subject} · "
            + item.starts_at.astimezone(ZoneInfo(self.timezone)).isoformat(timespec="minutes")
            + " to "
            f"{item.ends_at.astimezone(ZoneInfo(self.timezone)).isoformat(timespec='minutes')}"
            for item in conflicts
        )
        self.query_one("#calendar-detail", Static).update("\n".join(lines))
        self._set_view_state(ViewState.READY, f"{len(conflicts)} conflict(s) detected.")

    def action_related(self) -> None:
        try:
            self.operation_preferences().require_related()
        except ConfigurationError as exc:
            self._show_error("Calendar related mail", exc)
            return
        references = self._targets()
        if references:
            self.post_message(PimActionRequested("calendar", "related_messages", references))

    def action_correspondence(self) -> None:
        self.action_related()

    def _response_value(self, reference: str, value: MeetingResponseValue | None) -> None:
        if value is None:
            return
        proposal = (
            f"\nProposed: {value.proposed_start.isoformat(timespec='minutes')} to "
            f"{value.proposed_end.isoformat(timespec='minutes')}"
            if value.proposed_start is not None and value.proposed_end is not None
            else ""
        )
        self._confirm(
            "Send meeting response?",
            f"Response: {value.response.value}{proposal}\nA response may be sent to the organizer.",
            lambda: self._start_response(reference, value),
            confirm_label="Send response",
        )

    def _start_response(self, reference: str, value: MeetingResponseValue) -> None:
        self._begin_operation(
            "Sending meeting response",
            lambda cancel, progress: self.backend.respond(
                reference,
                value.response,
                value.comment,
                value.proposed_start,
                value.proposed_end,
                cancel=cancel,
                progress=progress,
            ),
            lambda _result: self.action_refresh(),
        )

    def action_import(self) -> None:
        self.app.push_screen(
            FilePathScreen("Import calendar events", "Path to .ics file"), self._import_path
        )

    def _import_path(self, path: str | None) -> None:
        if path is None:
            return
        self._confirm(
            "Import events from this ICS file?",
            f"Source: {clean_cell(path, max_length=400)}\n"
            "Imports up to 200 events. Existing events are not deleted.",
            lambda: self._start_import(path),
            confirm_label="Import",
        )

    def _start_import(self, path: str) -> None:
        self._begin_operation(
            "Importing calendar events",
            lambda cancel, progress: self.backend.import_ics(
                path, cancel=cancel, progress=progress
            ),
            self._batch_finished,
        )

    def action_export(self) -> None:
        references = self._targets()
        if not references:
            return
        self.app.push_screen(
            FilePathScreen("Export selected events", "New .ics destination"),
            lambda path: self._export_path(references, path),
        )

    def _export_path(self, references: tuple[str, ...], path: str | None) -> None:
        if path is None:
            return
        self._begin_operation(
            "Exporting events",
            lambda cancel, progress: self.backend.export_ics(
                references, path, cancel=cancel, progress=progress
            ),
            self._export_finished,
        )

    def _export_finished(self, result: object) -> None:
        if not isinstance(result, ExportResult):
            raise PublicOperationError("The Calendar backend returned an invalid export result.")
        self._set_view_state(
            ViewState.READY,
            f"Exported {result.item_count} events to {clean_cell(result.path, max_length=300)}.",
        )

    def action_previous_period(self) -> None:
        self.anchor_date = _shift_calendar_anchor(self.anchor_date, self.view_kind, -1)
        self.action_refresh()

    def action_next_period(self) -> None:
        self.anchor_date = _shift_calendar_anchor(self.anchor_date, self.view_kind, 1)
        self.action_refresh()

    def action_today(self) -> None:
        self.anchor_date = datetime.now(ZoneInfo(self.timezone)).date()
        self.action_refresh()

    @on(Input.Submitted, "#calendar-search")
    def _search_submitted(self) -> None:
        self.action_refresh()

    @on(Select.Changed, "#calendar-view")
    def _view_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        selected = CalendarViewKind(str(event.value))
        if selected is self.view_kind:
            return
        self.view_kind = selected
        self.action_refresh()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "calendar-previous": self.action_previous_period,
            "calendar-today": self.action_today,
            "calendar-next": self.action_next_period,
            "calendar-create": self.action_create,
            "calendar-edit": self.action_edit,
            "calendar-delete": self.action_delete,
            "calendar-respond": self.action_respond,
            "calendar-import": self.action_import,
            "calendar-export": self.action_export,
            "calendar-refresh": self.action_refresh,
            "calendar-history": self.action_history,
        }
        if event.button.id == "calendar-related":
            self.action_related()
            return
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


def _calendar_step(kind: CalendarViewKind) -> timedelta:
    if kind is CalendarViewKind.DAY:
        return timedelta(days=1)
    if kind in (CalendarViewKind.WEEK, CalendarViewKind.WORK_WEEK):
        return timedelta(days=7)
    if kind is CalendarViewKind.MONTH:
        return timedelta(days=30)
    return timedelta(days=30)


def _shift_calendar_anchor(anchor: date, kind: CalendarViewKind, direction: int) -> date:
    if kind is not CalendarViewKind.MONTH:
        return anchor + direction * _calendar_step(kind)
    month_index = anchor.year * 12 + anchor.month - 1 + direction
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(anchor.day, month_calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _render_event(
    event: CalendarEvent, *, complete: bool = False, display_timezone: str | None = None
) -> str:
    timezone = display_timezone or event.timezone
    start, end = friendly_event_times(event, timezone)
    when = f"{start}, {end}" if event.is_all_day else f"{start} to {end}"
    lines = [clean_cell(event.subject or "(no subject)", max_length=180), f"When: {when}"]
    if event.timezone != timezone and not event.is_all_day:
        lines.append(
            f"Times in {clean_cell(timezone, max_length=80)}, "
            f"set in {clean_cell(event.timezone, max_length=80)}"
        )
    if event.location:
        lines.append(f"Where: {clean_cell(event.location, max_length=160)}")
    if event.is_meeting and event.organizer:
        lines.append(
            "Organizer: "
            + clean_cell(event.organizer.name or event.organizer.email, max_length=180)
        )
    attendees = len(event.required_attendees) + len(event.optional_attendees)
    if attendees:
        lines.append(f"People: {attendees}")
    lines.append(f"Show as: {clean_cell(event.availability.replace('_', ' '), max_length=40)}")
    if event.reminder_minutes is not None:
        lines.append(f"Reminder: {event.reminder_minutes} min before")
    if event.recurrence:
        lines.append("Repeats")
    if event.response_status and event.is_meeting:
        lines.append(f"Your response: {clean_cell(event.response_status, max_length=60)}")
    if event.attachments:
        lines.append(f"Attachments: {len(event.attachments)}")
    if complete:
        if event.required_attendees:
            lines.append(
                "Required: "
                + ", ".join(
                    clean_cell(item.name or item.email, max_length=120)
                    for item in event.required_attendees
                )
            )
        if event.optional_attendees:
            lines.append(
                "Optional: "
                + ", ".join(
                    clean_cell(item.name or item.email, max_length=120)
                    for item in event.optional_attendees
                )
            )
        lines.extend(
            f"Attachment: {clean_cell(item.name, max_length=160)}"
            + (f" ({human_bytes(item.size)})" if item.size else "")
            for item in event.attachments
        )
    if complete and event.body:
        lines.extend(("", clean_cell(event.body, max_length=2_000)))
    return "\n".join(lines)
