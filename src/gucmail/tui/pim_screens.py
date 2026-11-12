"""Operational Calendar, Contacts, and Tasks screens.

The presentation layer depends on small blocking backend protocols.  The concrete
backends in this module adapt the existing application services; tests and offline
views may inject deterministic implementations without opening a network session.
Every list request is one bounded page, every blocking call runs through
``OperationalScreen``, and every destructive action requires explicit confirmation.
"""

from __future__ import annotations

import calendar as month_calendar
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import ClassVar, Protocol, TypeVar, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from gucmail.errors import ConfigurationError, GUCmailError
from gucmail.models import (
    Address,
    CalendarEvent,
    CalendarResponse,
    ContactInfo,
    Importance,
    TaskInfo,
)
from gucmail.services.calendar import (
    UNSET as CALENDAR_UNSET,
)
from gucmail.services.calendar import (
    CalendarPatch,
    CalendarService,
    CalendarView,
    CalendarViewKind,
    RecurrenceScope,
    detect_conflicts,
    localize_datetime,
)
from gucmail.services.calendar_display import (
    CalendarDisplayPreferences,
    event_time_labels,
    load_calendar_view,
)
from gucmail.services.calendar_history import CalendarSeriesHistory, history_text
from gucmail.services.calendar_paging import CalendarCursor
from gucmail.services.calendar_preferences import CalendarOperationPreferences
from gucmail.services.calendar_working_time import (
    CalendarWorkingTime,
    find_common_free_time,
    validate_free_time_request,
)
from gucmail.services.contact_preferences import ContactOperationPreferences
from gucmail.services.contacts import (
    ContactMergePlan,
    ContactPatch,
    ContactsService,
    CorrespondenceEvent,
    CorrespondentSummary,
    find_duplicate_groups,
)
from gucmail.services.contacts_display import ContactsDisplayPreferences
from gucmail.services.pim import MAX_PIM_PAGE_SIZE, PimPage
from gucmail.services.pim_preferences import CalendarCreationPreferences, TaskCreationPreferences
from gucmail.services.recurrence import normalize_recurrence_mapping
from gucmail.services.task_history import TaskHistoryPage
from gucmail.services.task_history import history_text as task_history_text
from gucmail.services.task_recurrence import TaskRepeatKind, TaskRepeatPreferences
from gucmail.services.tasks import TaskDeletionMode, TaskPatch, TaskService, TaskStatus
from gucmail.services.tasks_display import TaskDisplayPreferences, TaskView
from gucmail.tui.area_common import (
    OfflineOperationError,
    OperationalScreen,
    OperationProgress,
    ProgressReporter,
    PublicOperationError,
    ViewState,
    clean_cell,
    current_row_highlight,
)
from gucmail.tui.contact_fields import ContactValuesEditor
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.pim_submission import SubmissionEditor, SubmissionWorkspace

MAX_IMPORT_BYTES = 5 * 1024 * 1024


class CapabilityState(StrEnum):
    """How an operation is exposed by the current backend."""

    AVAILABLE = "available"
    READ_ONLY = "read_only"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PimCapability:
    """One truthful, user-visible capability result."""

    key: str
    label: str
    state: CapabilityState
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.state is CapabilityState.AVAILABLE

    def render(self) -> str:
        marker = {
            CapabilityState.AVAILABLE: "Available control:",
            CapabilityState.READ_ONLY: "◐",
            CapabilityState.UNAVAILABLE: "—",
        }[self.state]
        detail = f": {clean_cell(self.detail, max_length=140)}" if self.detail else ""
        return f"{marker} {clean_cell(self.label, max_length=60)}{detail}"


def capability_map(*values: PimCapability) -> Mapping[str, PimCapability]:
    """Return an immutable-in-practice capability mapping keyed by operation."""

    return {value.key: value for value in values}


def _capability(
    capabilities: Mapping[str, PimCapability], key: str, *, default: bool = False
) -> bool:
    value = capabilities.get(key)
    return value.available if value is not None else default


def _capability_text(capabilities: Mapping[str, PimCapability]) -> str:
    if not capabilities:
        return "Capability discovery unavailable. Mutating controls remain disabled."
    return "Client actions (account permissions checked when used): " + "  ·  ".join(
        value.render() for value in capabilities.values()
    )


def _capability_summary(capabilities: Mapping[str, PimCapability]) -> str:
    """Keep common actions readable without repeating a status before every label."""
    if not capabilities:
        return "Actions unavailable until capabilities are loaded."
    unavailable = [value for value in capabilities.values() if not value.available]
    if not unavailable:
        return ""
    return "Unavailable: " + " · ".join(
        clean_cell(value.label, max_length=60) for value in unavailable
    )


@dataclass(frozen=True, slots=True)
class BatchActionResult:
    """Bounded batch outcome shown without leaking provider exceptions."""

    attempted: int
    completed: int
    failed_references: tuple[str, ...] = ()
    skipped_references: tuple[str, ...] = ()
    uncertain_references: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return (
            not self.failed_references
            and self.completed + len(self.skipped_references) == self.attempted
        )


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: str
    item_count: int
    byte_count: int


@dataclass(frozen=True, slots=True)
class CalendarListRequest:
    view: CalendarViewKind
    anchor: date
    query: str = ""
    limit: int = 100
    first_weekday: int = 0
    working_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    timezone: str | None = None
    cursor: CalendarCursor | None = None


@dataclass(frozen=True, slots=True)
class CalendarEditorValue:
    subject: str
    start: datetime
    end: datetime
    timezone: str
    location: str | None = None
    body: str = ""
    required_attendees: tuple[Address, ...] = ()
    optional_attendees: tuple[Address, ...] = ()
    is_all_day: bool = False
    reminder_minutes: int | None = None
    availability: str = "busy"
    recurrence: Mapping[str, object] | None = None
    attachment_paths: tuple[Path, ...] = ()
    remove_attachment_references: tuple[str, ...] = ()
    scope: RecurrenceScope = RecurrenceScope.OCCURRENCE


@dataclass(frozen=True, slots=True)
class ContactEditorValue:
    display_name: str
    given_name: str | None = None
    middle_name: str | None = None
    family_name: str | None = None
    nickname: str | None = None
    emails: tuple[Address, ...] = ()
    phones: Mapping[str, str] | None = None
    addresses: Mapping[str, str] | None = None
    company: str | None = None
    department: str | None = None
    job_title: str | None = None
    birthday: date | None = None
    notes: str | None = None
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskEditorValue:
    subject: str
    status: TaskStatus = TaskStatus.NOT_STARTED
    percent_complete: int = 0
    start_date: date | None = None
    due_date: date | None = None
    body: str = ""
    categories: tuple[str, ...] = ()
    importance: Importance = Importance.NORMAL
    reminder_at: datetime | None = None
    recurrence: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ContactMergePreviewView:
    """A safe preview plus an opaque backend token used only after confirmation."""

    primary_reference: str
    source_references: tuple[str, ...]
    display_name: str
    email_count: int
    phone_count: int
    conflicts: tuple[str, ...]
    token: object

    def render(self) -> str:
        lines = [
            f"Primary: {clean_cell(self.primary_reference, max_length=100)}",
            f"Merged name: {clean_cell(self.display_name, max_length=120)}",
            f"Sources: {len(self.source_references)} · Emails: {self.email_count} · "
            f"Phones: {self.phone_count}",
        ]
        if self.conflicts:
            lines.append("Conflicts requiring the preview's selected values:")
            lines.extend(f"  • {clean_cell(item, max_length=180)}" for item in self.conflicts)
        else:
            lines.append("No field conflicts were detected.")
        return "\n".join(lines)


class PimActionRequested(Message):
    """Request an application-level action such as compose/history navigation."""

    def __init__(
        self,
        area: str,
        action: str,
        references: Sequence[str],
        *,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.area = clean_cell(area, max_length=30)
        self.action = clean_cell(action, max_length=60)
        self.references = tuple(clean_cell(item, max_length=500) for item in references)
        self.payload = dict(payload or {})


class CalendarScreenBackend(Protocol):
    capabilities: Mapping[str, PimCapability]

    def list_events(
        self,
        request: CalendarListRequest,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[CalendarEvent]: ...

    def get_event(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> CalendarEvent: ...

    def get_series_event(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> CalendarEvent: ...

    def history(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> CalendarSeriesHistory: ...

    def create_event(
        self, value: CalendarEditorValue, *, cancel: Event, progress: ProgressReporter
    ) -> CalendarEvent: ...

    def update_event(
        self,
        reference: str,
        value: CalendarEditorValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CalendarEvent: ...

    def delete_events(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
        scope: RecurrenceScope = RecurrenceScope.OCCURRENCE,
    ) -> BatchActionResult: ...

    def respond(
        self,
        reference: str,
        response: CalendarResponse,
        comment: str | None,
        proposed_start: datetime | None,
        proposed_end: datetime | None,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CalendarEvent | None: ...

    def availability(
        self,
        attendees: tuple[Address, ...],
        start: datetime,
        end: datetime,
        interval_minutes: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[Mapping[str, object]]: ...

    def import_ics(
        self, path: str, *, cancel: Event, progress: ProgressReporter
    ) -> BatchActionResult: ...

    def export_ics(
        self,
        references: tuple[str, ...],
        path: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ExportResult: ...


class ContactsScreenBackend(Protocol):
    capabilities: Mapping[str, PimCapability]

    def list_contacts(
        self,
        query: str,
        limit: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[ContactInfo]: ...

    def get_contact(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> ContactInfo: ...

    def search_directory(
        self,
        query: str,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[Address]: ...

    def correspondence_history(
        self,
        references: tuple[str, ...],
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[CorrespondenceEvent]: ...

    def list_correspondents(
        self,
        order: str,
        limit: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[CorrespondentSummary]: ...

    def create_contact(
        self, value: ContactEditorValue, *, cancel: Event, progress: ProgressReporter
    ) -> ContactInfo: ...

    def update_contact(
        self,
        reference: str,
        value: ContactEditorValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ContactInfo: ...

    def delete_contacts(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult: ...

    def preview_merge(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ContactMergePreviewView: ...

    def apply_merge(
        self,
        preview: ContactMergePreviewView,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult: ...

    def import_vcard(
        self, path: str, *, cancel: Event, progress: ProgressReporter
    ) -> BatchActionResult: ...

    def export_vcard(
        self,
        references: tuple[str, ...],
        path: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ExportResult: ...


class TasksScreenBackend(Protocol):
    capabilities: Mapping[str, PimCapability]

    def list_tasks(
        self,
        query: str,
        view: str,
        limit: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[TaskInfo]: ...

    def get_task(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> TaskInfo: ...

    def create_task(
        self, value: TaskEditorValue, *, cancel: Event, progress: ProgressReporter
    ) -> TaskInfo: ...

    def update_task(
        self,
        reference: str,
        value: TaskEditorValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> TaskInfo: ...

    def complete_tasks(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult: ...

    def reopen_tasks(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult: ...

    def delete_tasks(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult: ...

    def export_tasks(
        self,
        references: tuple[str, ...],
        path: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ExportResult: ...


def _check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise PublicOperationError(
            "Operation cancelled before the next provider request.", retryable=False
        )


def _bounded_limit(value: int) -> int:
    if value <= 0:
        raise PublicOperationError("PIM page size must be positive.", retryable=False)
    return min(value, MAX_PIM_PAGE_SIZE)


def _read_import(path: str, *, expected_suffixes: tuple[str, ...]) -> str:
    target = Path(path).expanduser()
    if target.suffix.casefold() not in expected_suffixes:
        allowed = ", ".join(expected_suffixes)
        raise PublicOperationError(f"Import file must use one of: {allowed}.", retryable=False)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise PublicOperationError(f"Cannot read import file: {type(exc).__name__}.") from exc
    if size > MAX_IMPORT_BYTES:
        raise PublicOperationError("PIM import files are limited to 5 MiB.", retryable=False)
    try:
        return target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise PublicOperationError(f"Cannot decode import file: {type(exc).__name__}.") from exc


def _write_export(path: str, value: str, *, expected_suffix: str) -> ExportResult:
    target = Path(path).expanduser()
    if target.suffix.casefold() != expected_suffix:
        raise PublicOperationError(f"Export path must end in {expected_suffix}.", retryable=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = value.encode("utf-8")
    try:
        with target.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError as exc:
        raise PublicOperationError(
            "The export destination already exists. Choose a new filename; it was not overwritten.",
            retryable=False,
        ) from exc
    except OSError as exc:
        raise PublicOperationError(f"Cannot write export file: {type(exc).__name__}.") from exc
    return ExportResult(str(target.resolve()), 0, len(encoded))


class CalendarServiceBackend:
    """Blocking adapter from :class:`CalendarService` to the Calendar screen."""

    capabilities = capability_map(
        PimCapability("create", "Create/edit/delete", CapabilityState.AVAILABLE),
        PimCapability("respond", "Invitation responses", CapabilityState.AVAILABLE),
        PimCapability("recurrence", "Recurrence", CapabilityState.AVAILABLE),
        PimCapability("import", "ICS import", CapabilityState.AVAILABLE),
        PimCapability("export", "ICS export", CapabilityState.AVAILABLE),
        PimCapability("attachments", "Event attachments", CapabilityState.AVAILABLE),
    )

    def __init__(self, service: CalendarService) -> None:
        self.service = service
        gateway_type = type(service.gateway)
        availability_available = callable(
            getattr(gateway_type, "get_availability", None)
        ) or callable(getattr(gateway_type, "__getattr__", None))
        history_available = callable(
            getattr(gateway_type, "get_calendar_history", None)
        ) or callable(getattr(gateway_type, "__getattr__", None))
        self.capabilities = capability_map(
            *self.capabilities.values(),
            PimCapability(
                "availability",
                "Availability and free time",
                (
                    CapabilityState.AVAILABLE
                    if availability_available
                    else CapabilityState.UNAVAILABLE
                ),
                ""
                if availability_available
                else "the active gateway does not expose GetUserAvailability",
            ),
            PimCapability(
                "history",
                "Read-only series history",
                CapabilityState.AVAILABLE if history_available else CapabilityState.UNAVAILABLE,
                "" if history_available else "the active gateway cannot read occurrence metadata",
            ),
        )

    def list_events(
        self,
        request: CalendarListRequest,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[CalendarEvent]:
        return self.list_events_page(request, cancel=cancel, progress=progress).events

    def list_events_page(
        self,
        request: CalendarListRequest,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CalendarView:
        _check_cancel(cancel)
        progress(_progress("Loading calendar", detail=request.view.value))
        limit = _bounded_limit(request.limit)
        service = CalendarService(
            self.service.gateway,
            display_timezone=request.timezone or self.service.display_timezone,
            default_calendar=self.service.default_calendar,
            default_limit=self.service.default_limit,
        )
        view = load_calendar_view(
            service,
            kind=request.view,
            anchor=request.anchor,
            first_weekday=request.first_weekday,
            working_days=request.working_days,
            agenda_days=30,
            limit=limit,
            query=request.query,
            cursor=request.cursor,
            cancelled=cancel.is_set,
            paged=True,
        )
        _check_cancel(cancel)
        return view

    def get_event(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> CalendarEvent:
        _check_cancel(cancel)
        progress(_progress("Loading event details"))
        value = self.service.get(reference)
        _check_cancel(cancel)
        return value

    def get_series_event(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> CalendarEvent:
        _check_cancel(cancel)
        progress(_progress("Loading entire series"))
        value = self.service.get(reference, scope=RecurrenceScope.SERIES)
        _check_cancel(cancel)
        return value

    def history(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> CalendarSeriesHistory:
        _check_cancel(cancel)
        progress(_progress("Loading read-only series occurrence history"))
        result = self.service.history(reference)
        _check_cancel(cancel)
        return result

    def create_event(
        self, value: CalendarEditorValue, *, cancel: Event, progress: ProgressReporter
    ) -> CalendarEvent:
        _check_cancel(cancel)
        progress(_progress("Creating event"))
        result = self.service.create(
            _calendar_event_from_editor(value),
            attachment_paths=value.attachment_paths,
        )
        _check_cancel(cancel)
        return result

    def update_event(
        self,
        reference: str,
        value: CalendarEditorValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CalendarEvent:
        _check_cancel(cancel)
        progress(_progress("Updating event"))
        current = self.service.get(reference)
        patch = _calendar_patch(value)
        if value.scope is RecurrenceScope.OCCURRENCE and (current.item_type or "").casefold() in {
            "occurrence",
            "exception",
        }:
            patch = replace(patch, recurrence=CALENDAR_UNSET)
        result = self.service.update(
            reference,
            patch,
            current=current,
            scope=value.scope,
            attachment_paths=value.attachment_paths,
            remove_attachment_references=value.remove_attachment_references,
        )
        _check_cancel(cancel)
        return result

    def delete_events(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
        scope: RecurrenceScope = RecurrenceScope.OCCURRENCE,
    ) -> BatchActionResult:
        completed = 0
        failed: list[str] = []
        handled: dict[str, bool] = {}
        for index, reference in enumerate(references, start=1):
            _check_cancel(cancel)
            progress(_progress("Deleting/cancelling events", index, len(references)))
            try:
                current = self.service.get(reference, scope=scope)
                if current.reference in handled and not handled[current.reference]:
                    raise PublicOperationError("An earlier series deletion requires review.")
                if current.reference not in handled:
                    handled[current.reference] = False
                    self.service.delete(
                        self.service.plan_delete(current.reference, current=current, scope=scope)
                    )
                    handled[current.reference] = True
            except Exception:
                failed.append(reference)
            else:
                completed += 1
        return BatchActionResult(len(references), completed, tuple(failed))

    def respond(
        self,
        reference: str,
        response: CalendarResponse,
        comment: str | None,
        proposed_start: datetime | None,
        proposed_end: datetime | None,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CalendarEvent | None:
        _check_cancel(cancel)
        progress(_progress("Sending meeting response"))
        event = self.service.get(reference)
        result = self.service.respond(
            self.service.plan_response(
                event,
                response,
                comment=comment,
                proposed_start=proposed_start,
                proposed_end=proposed_end,
            )
        )
        _check_cancel(cancel)
        return result

    def availability(
        self,
        attendees: tuple[Address, ...],
        start: datetime,
        end: datetime,
        interval_minutes: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[Mapping[str, object]]:
        _check_cancel(cancel)
        progress(_progress("Checking attendee availability"))
        operation = getattr(self.service.gateway, "get_availability", None)
        if not callable(operation):
            raise PublicOperationError(
                "This Exchange connection does not expose availability lookup.",
                retryable=False,
            )
        result = tuple(
            operation(
                attendees,
                start=start,
                end=end,
                interval_minutes=interval_minutes,
            )
        )
        if len(result) > len(attendees):
            raise PublicOperationError("The availability provider returned excess results.")
        if not all(isinstance(value, Mapping) for value in result):
            raise PublicOperationError("The availability provider returned invalid results.")
        _check_cancel(cancel)
        return result

    def import_ics(
        self, path: str, *, cancel: Event, progress: ProgressReporter
    ) -> BatchActionResult:
        _check_cancel(cancel)
        progress(_progress("Reading ICS import"))
        plan = self.service.import_ics(_read_import(path, expected_suffixes=(".ics",)))
        if len(plan.events) > MAX_PIM_PAGE_SIZE:
            raise PublicOperationError("ICS import is limited to 200 events per operation.")
        result = self.service.apply_import_result(plan, continue_on_error=True)
        return BatchActionResult(
            result.attempted,
            len(result.completed),
            tuple(item.reference or f"event-{item.index + 1}" for item in result.failures),
            result.skipped_references,
        )

    def export_ics(
        self,
        references: tuple[str, ...],
        path: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ExportResult:
        values: list[CalendarEvent] = []
        for index, reference in enumerate(references, start=1):
            _check_cancel(cancel)
            progress(_progress("Loading events for export", index, len(references)))
            values.append(self.service.get(reference))
        rendered = self.service.export_ics(values)
        result = _write_export(path, rendered, expected_suffix=".ics")
        return ExportResult(result.path, len(values), result.byte_count)


class ContactsServiceBackend:
    """Blocking adapter from :class:`ContactsService` to the Contacts screen."""

    capabilities = capability_map(
        PimCapability("create", "Create/edit/delete", CapabilityState.AVAILABLE),
        PimCapability("merge", "Duplicate merge", CapabilityState.AVAILABLE),
        PimCapability("import", "vCard import", CapabilityState.AVAILABLE),
        PimCapability("export", "vCard export", CapabilityState.AVAILABLE),
        PimCapability(
            "directory",
            "GUC directory lookup",
            CapabilityState.UNAVAILABLE,
            "requires separately detected ResolveNames permission",
        ),
    )

    def __init__(self, service: ContactsService) -> None:
        self.service = service
        self.refresh_capabilities()

    def refresh_capabilities(self) -> None:
        service = self.service
        preferences = service.operation_preferences()
        gateway_type = type(service.gateway)
        directory_available = callable(getattr(gateway_type, "search_directory", None)) or callable(
            getattr(gateway_type, "__getattr__", None)
        )
        correspondence_available = service.correspondence_gateway is not None
        self.capabilities = capability_map(
            PimCapability("create", "Create/edit/delete", CapabilityState.AVAILABLE),
            PimCapability("merge", "Duplicate merge", CapabilityState.AVAILABLE),
            PimCapability("import", "vCard import", CapabilityState.AVAILABLE),
            PimCapability("export", "vCard export", CapabilityState.AVAILABLE),
            PimCapability(
                "directory",
                "Exchange directory lookup",
                CapabilityState.AVAILABLE
                if directory_available and preferences.directory_lookup
                else CapabilityState.UNAVAILABLE,
                "disabled in Contacts settings"
                if not preferences.directory_lookup
                else "server permission is checked on explicit lookup"
                if directory_available
                else "the active gateway does not expose ResolveNames",
            ),
            PimCapability(
                "correspondence",
                "Correspondence history",
                CapabilityState.AVAILABLE
                if correspondence_available and preferences.correspondence_history
                else CapabilityState.UNAVAILABLE,
                "disabled in Contacts settings"
                if not preferences.correspondence_history
                else ""
                if correspondence_available
                else "no bounded correspondence-history gateway is configured",
            ),
        )

    def list_contacts(
        self,
        query: str,
        limit: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[ContactInfo]:
        return self.list_contacts_page(query, limit, cancel=cancel, progress=progress).items

    def list_contacts_page(
        self, query: str, limit: int, *, cancel: Event, progress: ProgressReporter, offset: int = 0
    ) -> PimPage[ContactInfo]:
        _check_cancel(cancel)
        progress(_progress("Loading contacts"))
        bounded = _bounded_limit(limit)
        result = self.service.page(query, limit=bounded, offset=offset)
        _check_cancel(cancel)
        return result

    def get_contact(
        self, reference: str, *, cancel: Event, progress: ProgressReporter
    ) -> ContactInfo:
        _check_cancel(cancel)
        progress(_progress("Loading contact details"))
        result = self.service.get(reference)
        _check_cancel(cancel)
        return result

    def search_directory(
        self,
        query: str,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[Address]:
        _check_cancel(cancel)
        progress(_progress("Searching Exchange directory"))
        result = self.service.search_directory(query, limit=min(max(limit, 1), 100))
        _check_cancel(cancel)
        return result

    def correspondence_history(
        self,
        references: tuple[str, ...],
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[CorrespondenceEvent]:
        self.service.operation_preferences().require("history")
        addresses: list[Address] = []
        for index, reference in enumerate(references, start=1):
            _check_cancel(cancel)
            progress(_progress("Loading contact addresses", index, len(references)))
            addresses.extend(self.service.get(reference).emails)
        progress(_progress("Loading bounded correspondence history"))
        result = self.service.correspondence_history(
            addresses,
            limit=min(max(limit, 1), MAX_PIM_PAGE_SIZE),
        )
        _check_cancel(cancel)
        return result

    def list_correspondents(
        self,
        order: str,
        limit: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[CorrespondentSummary]:
        _check_cancel(cancel)
        self.service.operation_preferences().require(
            "frequent" if order == "frequency" else "history"
        )
        bounded = min(max(limit, 1), MAX_PIM_PAGE_SIZE)
        progress(_progress("Loading bounded contact context"))
        known = self.service.list(limit=bounded)
        _check_cancel(cancel)
        progress(_progress("Summarizing bounded correspondence history"))
        if order == "recent":
            result = self.service.recent_correspondents(
                known_contacts=known,
                limit=bounded,
            )
        elif order == "frequency":
            result = self.service.frequent_correspondents(
                known_contacts=known,
                limit=bounded,
            )
        else:
            raise PublicOperationError(f"Unsupported correspondent order: {order}")
        _check_cancel(cancel)
        return result

    def create_contact(
        self, value: ContactEditorValue, *, cancel: Event, progress: ProgressReporter
    ) -> ContactInfo:
        _check_cancel(cancel)
        progress(_progress("Creating contact"))
        return self.service.create(_contact_from_editor(value))

    def update_contact(
        self,
        reference: str,
        value: ContactEditorValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ContactInfo:
        _check_cancel(cancel)
        progress(_progress("Updating contact"))
        return self.service.update(reference, _contact_patch(value))

    def delete_contacts(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult:
        completed = 0
        failed: list[str] = []
        for index, reference in enumerate(references, start=1):
            _check_cancel(cancel)
            progress(_progress("Deleting contacts", index, len(references)))
            try:
                self.service.delete(self.service.plan_delete(reference))
            except Exception:
                failed.append(reference)
            else:
                completed += 1
        return BatchActionResult(len(references), completed, tuple(failed))

    def preview_merge(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ContactMergePreviewView:
        values: list[ContactInfo] = []
        for index, reference in enumerate(references, start=1):
            _check_cancel(cancel)
            progress(_progress("Preparing merge preview", index, len(references)))
            values.append(self.service.get(reference))
        preview = self.service.merge_preview(values, primary_reference=references[0])
        conflicts = tuple(
            f"{item.field}: {len(item.choices)} candidate values" for item in preview.conflicts
        )
        return ContactMergePreviewView(
            preview.primary_reference,
            preview.source_references,
            preview.merged.display_name,
            len(preview.merged.emails),
            len(preview.merged.phones),
            conflicts,
            self.service.plan_merge(preview),
        )

    def apply_merge(
        self,
        preview: ContactMergePreviewView,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult:
        _check_cancel(cancel)
        progress(_progress("Applying contact merge"))
        plan = preview.token
        # Tokens are created by this backend and deliberately never serialized.
        if not hasattr(plan, "preview") or not hasattr(plan, "deletes"):
            raise PublicOperationError("The contact merge preview is no longer valid.")
        result = self.service.apply_merge_result(
            cast(ContactMergePlan, plan),
            continue_on_error=True,
        )
        return BatchActionResult(
            result.attempted,
            len(result.completed),
            tuple(item.reference or f"merge-step-{item.index + 1}" for item in result.failures),
            result.skipped_references,
        )

    def import_vcard(
        self, path: str, *, cancel: Event, progress: ProgressReporter
    ) -> BatchActionResult:
        _check_cancel(cancel)
        progress(_progress("Reading vCard import"))
        value = _read_import(path, expected_suffixes=(".vcf", ".vcard"))
        plan = self.service.import_vcards(value)
        if len(plan.contacts) > MAX_PIM_PAGE_SIZE:
            raise PublicOperationError("vCard import is limited to 200 contacts per operation.")
        result = self.service.apply_import_result(plan, continue_on_error=True)
        return BatchActionResult(
            result.attempted,
            len(result.completed),
            tuple(item.reference or f"contact-{item.index + 1}" for item in result.failures),
            result.skipped_references,
        )

    def export_vcard(
        self,
        references: tuple[str, ...],
        path: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ExportResult:
        values: list[ContactInfo] = []
        for index, reference in enumerate(references, start=1):
            _check_cancel(cancel)
            progress(_progress("Loading contacts for export", index, len(references)))
            values.append(self.service.get(reference))
        rendered = self.service.export_vcards(values)
        result = _write_export(path, rendered, expected_suffix=".vcf")
        return ExportResult(result.path, len(values), result.byte_count)


class TasksServiceBackend:
    """Blocking adapter from :class:`TaskService` to the Tasks screen."""

    capabilities = capability_map(
        PimCapability("create", "Create/edit/delete", CapabilityState.AVAILABLE),
        PimCapability("complete", "Complete/reopen", CapabilityState.AVAILABLE),
        PimCapability("delete_scope", "Permanent deletion options", CapabilityState.AVAILABLE),
        PimCapability("recurrence", "Recurrence", CapabilityState.AVAILABLE),
        PimCapability("export", "JSON/CSV export", CapabilityState.AVAILABLE),
        PimCapability(
            "import",
            "Task import",
            CapabilityState.UNAVAILABLE,
            "no safe interchange importer is currently defined",
        ),
        PimCapability("reminder", "Task reminders", CapabilityState.AVAILABLE),
    )

    def __init__(
        self,
        service: TaskService,
        *,
        show_completed: bool = True,
        timezone: str = "UTC",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self.show_completed = show_completed
        self.timezone = timezone
        self.clock = clock or (lambda: datetime.now(UTC))

    def list_tasks(
        self,
        query: str,
        view: str,
        limit: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[TaskInfo]:
        return self.list_tasks_page(query, view, limit, cancel=cancel, progress=progress).items

    def list_tasks_page(
        self,
        query: str,
        view: str,
        limit: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
        offset: int = 0,
        preferences: TaskDisplayPreferences | None = None,
        today: date | None = None,
    ) -> PimPage[TaskInfo]:
        _check_cancel(cancel)
        progress(_progress("Loading tasks", detail=view))
        bounded = _bounded_limit(limit)
        display = preferences or TaskDisplayPreferences(
            show_completed=self.show_completed, timezone=self.timezone
        )
        criteria = display.query(view=view, text=query, today=today or display.today(self.clock()))
        values = self.service.page(criteria, limit=bounded, offset=offset)
        _check_cancel(cancel)
        return values

    def get_task(self, reference: str, *, cancel: Event, progress: ProgressReporter) -> TaskInfo:
        _check_cancel(cancel)
        progress(_progress("Loading task details"))
        result = self.service.get(reference)
        _check_cancel(cancel)
        return result

    def create_task(
        self, value: TaskEditorValue, *, cancel: Event, progress: ProgressReporter
    ) -> TaskInfo:
        _check_cancel(cancel)
        progress(_progress("Creating task"))
        return self.service.create(_task_from_editor(value))

    def update_task(
        self,
        reference: str,
        value: TaskEditorValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> TaskInfo:
        _check_cancel(cancel)
        progress(_progress("Updating task"))
        current = self.service.get(reference)
        _check_cancel(cancel)
        return self.service.update(
            reference,
            _task_patch(value, current=current),
            current=current,
        )

    def _lifecycle(
        self,
        references: tuple[str, ...],
        *,
        complete: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult:
        completed = 0
        failed: list[str] = []
        uncertain: list[str] = []
        label = "Completing tasks" if complete else "Reopening tasks"
        for index, reference in enumerate(references, start=1):
            _check_cancel(cancel)
            progress(_progress(label, index, len(references)))
            try:
                if complete:
                    self.service.complete(reference)
                else:
                    self.service.reopen(reference)
            except Exception as exc:
                failed.append(reference)
                if isinstance(exc, GUCmailError) and exc.context.mutation_uncertain:
                    uncertain.append(reference)
            else:
                completed += 1
        return BatchActionResult(
            len(references), completed, tuple(failed), uncertain_references=tuple(uncertain)
        )

    def complete_tasks(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult:
        return self._lifecycle(references, complete=True, cancel=cancel, progress=progress)

    def reopen_tasks(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult:
        return self._lifecycle(references, complete=False, cancel=cancel, progress=progress)

    def delete_tasks(
        self,
        references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult:
        return self.delete_tasks_scoped(
            references, hard_delete=False, delete_series=False, cancel=cancel, progress=progress
        )

    def delete_tasks_scoped(
        self,
        references: tuple[str, ...],
        *,
        hard_delete: bool,
        delete_series: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BatchActionResult:
        _check_cancel(cancel)
        if not 1 <= len(references) <= MAX_PIM_PAGE_SIZE or len(set(references)) != len(references):
            raise ConfigurationError("Select between 1 and 200 distinct tasks for deletion.")
        # Validate every plan before the first write.
        plans = tuple(
            self.service.plan_delete(ref, hard_delete=hard_delete, delete_series=delete_series)
            for ref in references
        )
        completed = 0
        failed: list[str] = []
        uncertain: list[str] = []
        skipped: tuple[str, ...] = ()
        for index, plan in enumerate(plans, start=1):
            if cancel.is_set():
                skipped = tuple(p.reference for p in plans[index - 1 :])
                break
            progress(_progress("Deleting tasks", index, len(references)))
            try:
                self.service.delete(plan)
            except Exception as exc:
                failed.append(plan.reference)
                if isinstance(exc, GUCmailError) and exc.context.mutation_uncertain:
                    uncertain.append(plan.reference)
                if hard_delete:
                    skipped = tuple(p.reference for p in plans[index:])
                    break
            else:
                completed += 1
        return BatchActionResult(
            len(references), completed, tuple(failed), skipped, tuple(uncertain)
        )

    def export_tasks(
        self,
        references: tuple[str, ...],
        path: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ExportResult:
        values: list[TaskInfo] = []
        for index, reference in enumerate(references, start=1):
            _check_cancel(cancel)
            progress(_progress("Loading tasks for export", index, len(references)))
            values.append(self.service.get(reference))
        suffix = Path(path).suffix.casefold()
        if suffix not in (".json", ".csv"):
            raise PublicOperationError("Task export path must end in .json or .csv.")
        rendered = self.service.export(values, format=suffix[1:])
        result = _write_export(path, rendered, expected_suffix=suffix)
        return ExportResult(result.path, len(values), result.byte_count)


def _progress(
    phase: str,
    completed: int = 0,
    total: int | None = None,
    *,
    detail: str = "",
) -> OperationProgress:
    return OperationProgress(phase, completed, total, detail)


def _calendar_event_from_editor(value: CalendarEditorValue) -> CalendarEvent:
    return CalendarEvent(
        reference=f"tui-new-event-{uuid4().hex}",
        subject=value.subject,
        start=value.start,
        end=value.end,
        timezone=value.timezone,
        location=value.location,
        body=value.body,
        required_attendees=value.required_attendees,
        optional_attendees=value.optional_attendees,
        is_all_day=value.is_all_day,
        is_meeting=bool(value.required_attendees or value.optional_attendees),
        reminder_minutes=value.reminder_minutes,
        availability=value.availability,
        recurrence=value.recurrence,
    )


def _calendar_patch(value: CalendarEditorValue) -> CalendarPatch:
    return CalendarPatch(
        subject=value.subject,
        start=value.start,
        end=value.end,
        timezone=value.timezone,
        location=value.location,
        body=value.body,
        required_attendees=value.required_attendees,
        optional_attendees=value.optional_attendees,
        is_all_day=value.is_all_day,
        reminder_minutes=value.reminder_minutes,
        availability=value.availability,
        recurrence=value.recurrence,
    )


def _contact_from_editor(value: ContactEditorValue) -> ContactInfo:
    return ContactInfo(
        reference=f"tui-new-contact-{uuid4().hex}",
        display_name=value.display_name,
        given_name=value.given_name,
        middle_name=value.middle_name,
        family_name=value.family_name,
        nickname=value.nickname,
        emails=value.emails,
        phones=value.phones or {},
        addresses=value.addresses or {},
        company=value.company,
        department=value.department,
        job_title=value.job_title,
        birthday=value.birthday,
        notes=value.notes,
        categories=value.categories,
    )


def _contact_patch(value: ContactEditorValue) -> ContactPatch:
    return ContactPatch(
        display_name=value.display_name,
        given_name=value.given_name,
        middle_name=value.middle_name,
        family_name=value.family_name,
        nickname=value.nickname,
        emails=value.emails,
        phones=value.phones or {},
        addresses=value.addresses or {},
        company=value.company,
        department=value.department,
        job_title=value.job_title,
        birthday=value.birthday,
        notes=value.notes,
        categories=value.categories,
    )


def _task_from_editor(value: TaskEditorValue) -> TaskInfo:
    return TaskInfo(
        reference=f"tui-new-task-{uuid4().hex}",
        subject=value.subject,
        status=value.status.value,
        percent_complete=value.percent_complete,
        start_date=value.start_date,
        due_date=value.due_date,
        completed_at=datetime.now(UTC) if value.status is TaskStatus.COMPLETED else None,
        body=value.body,
        categories=value.categories,
        importance=value.importance,
        reminder_at=value.reminder_at,
        recurrence=value.recurrence,
    )


def _task_patch(value: TaskEditorValue, *, current: TaskInfo) -> TaskPatch:
    current_status = TaskStatus(current.status)
    completed_at = (
        current.completed_at or datetime.now(UTC) if value.status is TaskStatus.COMPLETED else None
    )
    return TaskPatch(
        subject=value.subject,
        status=value.status,
        percent_complete=value.percent_complete,
        start_date=value.start_date,
        due_date=value.due_date,
        completed_at=completed_at
        if value.status is not current_status or value.status is TaskStatus.COMPLETED
        else None,
        body=value.body,
        categories=value.categories,
        importance=value.importance,
        reminder_at=value.reminder_at,
        recurrence=value.recurrence,
    )


def _parse_datetime(value: str, timezone: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise PublicOperationError(
            f"{field} must be an ISO date/time such as 2026-08-09 14:30.", retryable=False
        ) from exc
    try:
        return localize_datetime(parsed, timezone)
    except Exception as exc:
        # CalendarService performs the authoritative time-zone/DST validation.
        # Translate its configuration error into a form-safe message without a
        # traceback or provider detail.
        raise PublicOperationError(clean_cell(exc, max_length=400), retryable=False) from exc


def _parse_date(value: str, *, field: str) -> date | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return date.fromisoformat(stripped)
    except ValueError as exc:
        raise PublicOperationError(f"{field} must use YYYY-MM-DD.", retryable=False) from exc


def _parse_optional_aware_datetime(value: str, *, field: str) -> datetime | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicOperationError(
            f"{field} must be ISO-8601, for example 2026-08-24T09:00:00+02:00.",
            retryable=False,
        ) from exc
    if parsed.tzinfo is None:
        raise PublicOperationError(
            f"{field} must include a UTC offset so it cannot shift silently.",
            retryable=False,
        )
    return parsed


def _parse_recurrence(value: str) -> Mapping[str, object] | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise PublicOperationError("Recurrence must be a JSON object.", retryable=False) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise PublicOperationError("Recurrence must be a non-empty JSON object.", retryable=False)
    return cast(dict[str, object], parsed)


def _calendar_recurrence_editor_values(
    event: CalendarEvent | None,
) -> tuple[str, str, str, str, str]:
    """Return approachable repeat controls, preserving complex rules as advanced JSON."""

    if event is None or not event.recurrence:
        return "none", "1", "", "", ""
    try:
        normalized = normalize_recurrence_mapping(event.recurrence, start=event.start)
    except Exception:
        return "advanced", "1", "", "", json.dumps(event.recurrence, sort_keys=True)
    pattern = normalized.get("pattern")
    boundary = normalized.get("boundary")
    if not isinstance(pattern, Mapping) or not isinstance(boundary, Mapping):
        return "advanced", "1", "", "", json.dumps(normalized, sort_keys=True)
    pattern_type = str(pattern.get("type") or "")
    start_date = event.start.date()
    simple = pattern_type
    if pattern_type == "weekly":
        weekdays = pattern.get("weekdays")
        expected = start_date.strftime("%A").casefold()
        if not isinstance(weekdays, Sequence) or list(weekdays) != [expected]:
            simple = ""
    elif pattern_type == "absolute_monthly":
        simple = "monthly" if pattern.get("day_of_month") == start_date.day else ""
    elif pattern_type == "absolute_yearly":
        simple = (
            "yearly"
            if pattern.get("month") == start_date.month
            and pattern.get("day_of_month") == start_date.day
            else ""
        )
    elif pattern_type not in {"daily"}:
        simple = ""
    if not simple:
        return "advanced", "1", "", "", json.dumps(normalized, sort_keys=True)
    interval = str(pattern.get("interval") or 1)
    count = str(boundary.get("occurrences") or "") if boundary.get("type") == "numbered" else ""
    until = str(boundary.get("end") or "") if boundary.get("type") == "end_date" else ""
    return simple, interval, count, until, ""


def _calendar_recurrence_value(
    *,
    kind: str,
    interval_text: str,
    count_text: str,
    until_text: str,
    advanced_text: str,
    start: datetime,
) -> Mapping[str, object] | None:
    """Build and validate the Calendar recurrence selected in the form."""

    if kind == "none":
        return None
    if kind == "advanced":
        value = _parse_recurrence(advanced_text)
        if value is None:
            raise PublicOperationError(
                "Advanced recurrence needs a recurrence definition.", retryable=False
            )
        try:
            return normalize_recurrence_mapping(value, start=start)
        except Exception as exc:
            raise PublicOperationError(clean_cell(exc, max_length=400), retryable=False) from exc
    try:
        interval = int(interval_text.strip() or "1")
    except ValueError as exc:
        raise PublicOperationError(
            "Repeat interval must be a whole number.", retryable=False
        ) from exc
    if not 1 <= interval <= 999:
        raise PublicOperationError("Repeat interval must be between 1 and 999.", retryable=False)
    try:
        count = int(count_text.strip()) if count_text.strip() else None
    except ValueError as exc:
        raise PublicOperationError(
            "Occurrence count must be a whole number.", retryable=False
        ) from exc
    if count is not None and not 1 <= count <= 999:
        raise PublicOperationError("Occurrence count must be between 1 and 999.", retryable=False)
    until = _parse_date(until_text, field="Repeat-until date")
    if count is not None and until is not None:
        raise PublicOperationError(
            "Choose either an occurrence count or an end date, not both.", retryable=False
        )
    start_date = start.date()
    if until is not None and until < start_date:
        raise PublicOperationError(
            "Repeat-until date cannot be before the event starts.", retryable=False
        )
    if kind == "daily":
        pattern: dict[str, object] = {"type": "daily", "interval": interval}
    elif kind == "weekly":
        pattern = {
            "type": "weekly",
            "interval": interval,
            "weekdays": [start_date.strftime("%A").casefold()],
            "first_day_of_week": "monday",
        }
    elif kind == "monthly":
        pattern = {
            "type": "absolute_monthly",
            "interval": interval,
            "day_of_month": start_date.day,
        }
    elif kind == "yearly":
        if interval != 1:
            raise PublicOperationError(
                "Yearly recurrence uses a one-year interval on Exchange.", retryable=False
            )
        pattern = {
            "type": "absolute_yearly",
            "month": start_date.month,
            "day_of_month": start_date.day,
        }
    else:
        raise PublicOperationError("Choose a supported repeat pattern.", retryable=False)
    if count is not None:
        boundary: dict[str, object] = {
            "type": "numbered",
            "start": start_date.isoformat(),
            "occurrences": count,
        }
    elif until is not None:
        boundary = {
            "type": "end_date",
            "start": start_date.isoformat(),
            "end": until.isoformat(),
        }
    else:
        boundary = {"type": "no_end", "start": start_date.isoformat()}
    try:
        return normalize_recurrence_mapping({"pattern": pattern, "boundary": boundary}, start=start)
    except Exception as exc:
        raise PublicOperationError(clean_cell(exc, max_length=400), retryable=False) from exc


def _addresses(value: str) -> tuple[Address, ...]:
    result: list[Address] = []
    for candidate in value.replace(";", ",").split(","):
        email = candidate.strip()
        if not email:
            continue
        if "@" not in email or any(character.isspace() for character in email):
            raise PublicOperationError(f"Invalid email address: {email}.", retryable=False)
        result.append(Address(email))
    return tuple(result)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _labeled_values(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        candidate = part.strip()
        if not candidate:
            continue
        if "=" not in candidate:
            raise PublicOperationError(
                "Labeled contact values must use label=value pairs separated by semicolons.",
                retryable=False,
            )
        label, content = (item.strip() for item in candidate.split("=", 1))
        normalized_label = label.casefold()
        if not label or not content or normalized_label in result:
            raise PublicOperationError(
                "Contact value labels and values must be non-empty and unique.",
                retryable=False,
            )
        if any(character in label + content for character in "\r\n\x00"):
            raise PublicOperationError("A contact value contains invalid control text.")
        result[normalized_label] = content
    return result


def _render_labeled_values(values: Mapping[str, str]) -> str:
    return "; ".join(
        f"{clean_cell(label, max_length=40)}={clean_cell(value, max_length=300)}"
        for label, value in values.items()
    )


def _optional(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None


class FilePathScreen(ModalScreen[str | None]):
    """Small path prompt used by explicit PIM import/export actions."""

    DEFAULT_CSS = """
    FilePathScreen { align: center middle; }
    #pim-file-dialog {
        width: 94%; max-width: 78; height: auto; border: round $border;
        background: $surface; padding: 1 2;
    }
    #pim-file-title { height: 2; text-style: bold; }
    #pim-file-error { height: auto; min-height: 1; color: $error; }
    #pim-file-actions { height: 3; align-horizontal: right; }
    #pim-file-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, title: str, placeholder: str, *, initial: str = "") -> None:
        super().__init__()
        self.title_text = clean_cell(title, max_length=100)
        self.placeholder = clean_cell(placeholder, max_length=200)
        self.initial = clean_cell(initial, max_length=500)

    def compose(self) -> ComposeResult:
        with Vertical(id="pim-file-dialog"):
            yield Label(self.title_text, id="pim-file-title", markup=False)
            yield Input(value=self.initial, placeholder=self.placeholder, id="pim-file-path")
            yield Static("", id="pim-file-error", markup=False)
            with Horizontal(id="pim-file-actions"):
                yield Button("Cancel", id="pim-file-cancel")
                yield Button("Continue", id="pim-file-continue", variant="primary")

    @on(Input.Submitted, "#pim-file-path")
    def _submitted(self) -> None:
        self._accept()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pim-file-cancel":
            self.dismiss(None)
        elif event.button.id == "pim-file-continue":
            self._accept()

    def _accept(self) -> None:
        value = self.query_one("#pim-file-path", Input).value.strip()
        if not value:
            self.query_one("#pim-file-error", Static).update("Choose a file path.")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CalendarScopeScreen(ModalScreen[RecurrenceScope | None]):
    """Freeze the target while the user chooses a single occurrence or its series."""

    DEFAULT_CSS = """
    CalendarScopeScreen { align: center middle; }
    #calendar-scope { width: 90%; max-width: 76; height: auto;
        border: round $border; background: $surface; padding: 1 2; }
    #calendar-scope-title { text-style: bold; height: auto; margin-bottom: 1; }
    #calendar-scope-summary { height: auto; margin-bottom: 1; }
    #calendar-scope Button { width: 100%; margin-bottom: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, event: CalendarEvent, *, operation: str) -> None:
        super().__init__()
        self.event = event
        self.operation = operation

    def compose(self) -> ComposeResult:
        master = (self.event.item_type or "").casefold() == "recurringmaster"
        with Vertical(id="calendar-scope"):
            yield Label(f"{self.operation}: choose scope", id="calendar-scope-title", markup=False)
            yield Static(
                clean_cell(self.event.subject, max_length=180)
                + (
                    "\nThe selected item is the recurring series."
                    if master
                    else "\nThis occurrence changes one date; entire series affects every date."
                ),
                id="calendar-scope-summary",
                markup=False,
            )
            yield Button("This occurrence", id="calendar-scope-occurrence", disabled=master)
            yield Button("Entire series", id="calendar-scope-series")
            yield Button("Cancel", id="calendar-scope-cancel")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "calendar-scope-occurrence":
            self.dismiss(RecurrenceScope.OCCURRENCE)
        elif event.button.id == "calendar-scope-series":
            self.dismiss(RecurrenceScope.SERIES)
        elif event.button.id == "calendar-scope-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


def _recurring_event(event: CalendarEvent) -> bool:
    return bool(event.recurrence) or (event.item_type or "").casefold() in {
        "occurrence",
        "exception",
        "recurringmaster",
    }


_EditorResult = TypeVar("_EditorResult")


class _PimEditorScreen(SubmissionEditor[_EditorResult]):
    """Keep the current keyboard field visible after form layout and resize."""

    _validation_error = False

    def _validation_failed(self, message: str) -> None:
        self._validation_error = True
        self._submission_status(message)

    def _submit_value(self, value: _EditorResult) -> None:
        self._validation_error = False
        super()._submit_value(value)

    def _submission_blocked(self) -> bool:
        return self._save_pending or self._save_uncertain or self._save_complete

    @on(Input.Changed)
    @on(Select.Changed)
    @on(TextArea.Changed)
    @on(Checkbox.Changed)
    def _clear_local_validation(self) -> None:
        # Input changes invalidate a local validation result. They must never
        # conceal a pending, rejected or uncertain service submission outcome.
        if self._validation_error and not self._save_pending and not self._save_uncertain:
            self._validation_error = False
            for status in self.query(Static):
                if status.id and status.id.endswith("-editor-error"):
                    status.update("")

    def set_focus(
        self, widget: Widget | None, scroll_visible: bool = True, from_app_focus: bool = False
    ) -> None:
        super().set_focus(widget, scroll_visible=False, from_app_focus=from_app_focus)
        if scroll_visible:
            self._reveal_focus_now()
            self.call_after_refresh(self._reveal_focus)

    def _reveal_focus_now(self) -> None:
        focused = self.focused
        if focused is None or not focused.is_mounted:
            return
        for ancestor in focused.ancestors:
            if isinstance(ancestor, Widget):
                self.app.animator.force_stop_animation(ancestor, "scroll_x")
                self.app.animator.force_stop_animation(ancestor, "scroll_y")
        focused.scroll_visible(animate=False, force=True, immediate=True)

    async def _reveal_focus(self) -> None:
        focused = self.focused
        if focused is None or not focused.is_mounted:
            return
        # A no-distance reveal does not cancel an earlier scheduled scroll.
        # Stop it explicitly before checking visibility against the final layout.
        for ancestor in focused.ancestors:
            if isinstance(ancestor, Widget):
                await ancestor.stop_animation("scroll_x", complete=False)
                await ancestor.stop_animation("scroll_y", complete=False)
        if self.focused is focused and focused.is_mounted:
            focused.scroll_visible(animate=False, force=True, immediate=True)

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if event.widget is self.focused:
            self._reveal_focus_now()
            self.call_after_refresh(self._reveal_focus)

    def on_resize(self, event: Resize) -> None:
        self.call_after_refresh(self._reveal_focus)


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


class ContactEditorScreen(_PimEditorScreen[ContactEditorValue | None]):
    """Validated Contacts create/edit form."""

    DEFAULT_CSS = """
    ContactEditorScreen { align: center middle; }
    #contact-editor {
        width: 96%; max-width: 96; height: 96%; max-height: 38; border: round $border;
        background: $surface; padding: 1 2;
    }
    #contact-editor-title { height: 2; text-style: bold; }
    #contact-editor-fields { height: 1fr; }
    #contact-editor-fields Input { height: 3; }
    #contact-editor-notes { height: 7; }
    #contact-editor-error { min-height: 1; height: auto; color: $error; }
    #contact-editor-actions { height: 3; align-horizontal: right; }
    #contact-editor-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(self, contact: ContactInfo | None, *, creating: bool = False) -> None:
        super().__init__()
        self.contact = contact
        self.creating = creating

    def compose(self) -> ComposeResult:
        contact = self.contact
        with Vertical(id="contact-editor"):
            yield Label(
                "Create contact" if contact is None or self.creating else "Edit contact",
                id="contact-editor-title",
                markup=False,
            )
            with VerticalScroll(id="contact-editor-fields"):
                yield Label("Display name")
                yield Input(
                    value=contact.display_name if contact else "",
                    placeholder="Display name",
                    id="contact-edit-name",
                )
                yield Label("First name")
                yield Input(
                    value=contact.given_name or "" if contact else "",
                    placeholder="Given name",
                    id="contact-edit-given",
                )
                yield Label("Middle name")
                yield Input(
                    value=contact.middle_name or "" if contact else "",
                    placeholder="Middle name",
                    id="contact-edit-middle",
                )
                yield Label("Family name")
                yield Input(
                    value=contact.family_name or "" if contact else "",
                    placeholder="Family name",
                    id="contact-edit-family",
                )
                yield Label("Nickname")
                yield Input(
                    value=contact.nickname or "" if contact else "",
                    placeholder="Nickname",
                    id="contact-edit-nickname",
                )
                yield Label("Email addresses · comma separated")
                yield Input(
                    value=", ".join(address.email for address in contact.emails) if contact else "",
                    placeholder="Email addresses (comma separated)",
                    id="contact-edit-emails",
                )
                yield ContactValuesEditor(
                    "Phone",
                    contact.phones if contact else {},
                    (
                        ("Mobile", "MobilePhone"),
                        ("Work", "BusinessPhone"),
                        ("Home", "HomePhone"),
                        ("Primary", "PrimaryPhone"),
                        ("Work 2", "BusinessPhone2"),
                        ("Home 2", "HomePhone2"),
                        ("Other", "OtherTelephone"),
                        ("Company", "CompanyMainPhone"),
                        ("Work fax", "BusinessFax"),
                        ("Home fax", "HomeFax"),
                        ("Other fax", "OtherFax"),
                        ("Assistant", "AssistantPhone"),
                        ("Callback", "Callback"),
                        ("Car", "CarPhone"),
                        ("Pager", "Pager"),
                        ("Radio", "RadioPhone"),
                        ("Telex", "Telex"),
                        ("TTY/TDD", "TtyTddPhone"),
                        ("ISDN", "Isdn"),
                    ),
                    id="contact-edit-phones",
                )
                yield ContactValuesEditor(
                    "Postal address",
                    contact.addresses if contact else {},
                    (("Home", "Home"), ("Work", "Business"), ("Other", "Other")),
                    id="contact-edit-addresses",
                    multiline=True,
                )
                yield Label("Company")
                yield Input(
                    value=contact.company or "" if contact else "",
                    placeholder="Company",
                    id="contact-edit-company",
                )
                yield Label("Department")
                yield Input(
                    value=contact.department or "" if contact else "",
                    placeholder="Department",
                    id="contact-edit-department",
                )
                yield Label("Job title")
                yield Input(
                    value=contact.job_title or "" if contact else "",
                    placeholder="Job title",
                    id="contact-edit-job-title",
                )
                yield Label("Birthday · YYYY-MM-DD")
                yield Input(
                    value=contact.birthday.isoformat() if contact and contact.birthday else "",
                    placeholder="Birthday (YYYY-MM-DD)",
                    id="contact-edit-birthday",
                )
                yield Label("Categories · comma separated")
                yield Input(
                    value=", ".join(contact.categories) if contact else "",
                    placeholder="Categories (comma separated)",
                    id="contact-edit-categories",
                )
                yield Label("Notes")
                yield TextArea(contact.notes or "" if contact else "", id="contact-editor-notes")
            yield Static("", id="contact-editor-error", markup=False)
            with Horizontal(id="contact-editor-actions"):
                yield Button("Cancel", id="contact-edit-cancel")
                yield Button("Save", id="contact-edit-save", variant="primary")

    def _value(self) -> ContactEditorValue:
        display_name = self.query_one("#contact-edit-name", Input).value.strip()
        if not display_name:
            raise PublicOperationError("A contact needs a display name.", retryable=False)
        return ContactEditorValue(
            display_name=display_name,
            given_name=_optional(self.query_one("#contact-edit-given", Input).value),
            middle_name=_optional(self.query_one("#contact-edit-middle", Input).value),
            family_name=_optional(self.query_one("#contact-edit-family", Input).value),
            nickname=_optional(self.query_one("#contact-edit-nickname", Input).value),
            emails=_addresses(self.query_one("#contact-edit-emails", Input).value),
            phones=self.query_one("#contact-edit-phones", ContactValuesEditor).values(),
            addresses=self.query_one("#contact-edit-addresses", ContactValuesEditor).values(),
            company=_optional(self.query_one("#contact-edit-company", Input).value),
            department=_optional(self.query_one("#contact-edit-department", Input).value),
            job_title=_optional(self.query_one("#contact-edit-job-title", Input).value),
            birthday=_parse_date(
                self.query_one("#contact-edit-birthday", Input).value,
                field="Birthday",
            ),
            notes=_optional(self.query_one("#contact-editor-notes", TextArea).text),
            categories=_csv(self.query_one("#contact-edit-categories", Input).value),
        )

    def action_save(self) -> None:
        if self._submission_blocked():
            return
        try:
            value = self._value()
        except (PublicOperationError, ValueError) as exc:
            self._validation_failed(str(exc))
            return
        self._submit_value(value)

    def action_cancel(self) -> None:
        self._cancel_edit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "contact-edit-save":
            self.action_save()
        elif event.button.id == "contact-edit-cancel":
            self.action_cancel()


class TaskEditorScreen(_PimEditorScreen[TaskEditorValue | None]):
    """Validated Tasks create/edit form."""

    DEFAULT_CSS = """
    TaskEditorScreen { align: center middle; }
    #task-editor {
        width: 96%; max-width: 94; height: 96%; max-height: 36; border: round $border;
        background: $surface; padding: 1 2;
    }
    #task-editor-title { height: 2; text-style: bold; }
    #task-editor-fields { height: 1fr; }
    #task-editor-fields Input, #task-editor-fields Select { height: 3; }
    #task-editor-body { height: 7; }
    #task-repeat-options { height: auto; }
    #task-editor-error { min-height: 1; height: auto; color: $error; }
    #task-editor-actions { height: 3; align-horizontal: right; }
    #task-editor-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(
        self,
        task: TaskInfo | None,
        *,
        creating: bool = False,
        focus_field: str | None = None,
    ) -> None:
        super().__init__()
        self.task_info = task
        self.creating = creating
        self.focus_field = focus_field

    def on_mount(self) -> None:
        self._update_repeat_controls()
        selector = {
            "reminder": "#task-edit-reminder",
            "due_date": "#task-edit-due",
            "priority": "#task-edit-importance",
        }.get(self.focus_field or "")
        if selector:
            self.query_one(selector).focus()

    def compose(self) -> ComposeResult:
        task = self.task_info
        repeat_kind, repeat_interval, repeat_count, repeat_until = "none", "1", "", ""
        if task and task.recurrence:
            repeat_kind = "advanced"
            if self.creating:
                try:
                    rule = normalize_recurrence_mapping(
                        task.recurrence, task=True, start=task.start_date or task.due_date
                    )
                    pattern, boundary = rule["pattern"], rule["boundary"]
                    if not isinstance(pattern, Mapping) or not isinstance(boundary, Mapping):
                        raise TypeError("Recurrence pattern and boundary must be objects.")
                    kind = {
                        "daily": "daily",
                        "weekly": "weekly",
                        "absolute_monthly": "monthly",
                        "absolute_yearly": "yearly",
                    }.get(pattern["type"])
                    if pattern["type"] == "task_regeneration":
                        kind = {
                            "day": "after_completion_daily",
                            "week": "after_completion_weekly",
                            "month": "after_completion_monthly",
                            "year": "after_completion_yearly",
                        }.get(str(pattern.get("unit", "")))
                    if kind is not None:
                        interval = int(pattern.get("interval", 1))
                        count = (
                            int(boundary["occurrences"]) if boundary["type"] == "numbered" else None
                        )
                        until = (
                            date.fromisoformat(str(boundary["end"]))
                            if boundary["type"] == "end_date"
                            else None
                        )
                        anchor = task.start_date or task.due_date
                        if (
                            anchor
                            and TaskRepeatPreferences(TaskRepeatKind(kind), interval, count).rule(
                                anchor, until=until
                            )
                            == rule
                        ):
                            repeat_kind = kind
                            repeat_interval = str(interval)
                            repeat_count = str(count) if count is not None else ""
                            repeat_until = until.isoformat() if until is not None else ""
                except (ConfigurationError, KeyError, TypeError):
                    pass  # Preserve an imported/complex fresh rule in its advanced control.
        with Vertical(id="task-editor"):
            yield Label(
                "Create task" if task is None or self.creating else "Edit task",
                id="task-editor-title",
                markup=False,
            )
            with VerticalScroll(id="task-editor-fields"):
                yield Label("Subject")
                yield Input(
                    value=task.subject if task else "",
                    placeholder="Subject",
                    id="task-edit-subject",
                )
                yield Label("Status")
                yield Select(
                    tuple(
                        (status.value.replace("_", " ").title(), status.value)
                        for status in TaskStatus
                    ),
                    value=task.status if task else TaskStatus.NOT_STARTED.value,
                    allow_blank=False,
                    id="task-edit-status",
                )
                yield Label("Completed · 0-100%")
                yield Input(
                    value=str(task.percent_complete) if task else "0",
                    placeholder="Percent complete",
                    id="task-edit-percent",
                )
                yield Label("Start date · YYYY-MM-DD")
                yield Input(
                    value=task.start_date.isoformat() if task and task.start_date else "",
                    placeholder="Start date (YYYY-MM-DD)",
                    id="task-edit-start",
                )
                yield Label("Due date · YYYY-MM-DD")
                yield Input(
                    value=task.due_date.isoformat() if task and task.due_date else "",
                    placeholder="Due date (YYYY-MM-DD)",
                    id="task-edit-due",
                )
                yield Label("Priority")
                yield Select(
                    (
                        ("Low", Importance.LOW.value),
                        ("Normal", Importance.NORMAL.value),
                        ("High", Importance.HIGH.value),
                    ),
                    value=task.importance.value if task else Importance.NORMAL.value,
                    allow_blank=False,
                    id="task-edit-importance",
                )
                yield Label("Categories · comma separated")
                yield Input(
                    value=", ".join(task.categories) if task else "",
                    placeholder="Categories (comma separated)",
                    id="task-edit-categories",
                )
                yield Label("Reminder · YYYY-MM-DD HH:MM with UTC offset")
                yield Input(
                    value=task.reminder_at.isoformat() if task and task.reminder_at else "",
                    placeholder="Reminder time with UTC offset (optional)",
                    id="task-edit-reminder",
                )
                yield Label("Repeat")
                yield Select(
                    (
                        ("Does not repeat", "none"),
                        ("Daily", "daily"),
                        ("Weekly on start day", "weekly"),
                        ("Monthly on start date", "monthly"),
                        ("Yearly on start date", "yearly"),
                        ("Days after completion", "after_completion_daily"),
                        ("Weeks after completion", "after_completion_weekly"),
                        ("Months after completion", "after_completion_monthly"),
                        ("Years after completion", "after_completion_yearly"),
                        ("Advanced rule", "advanced"),
                    ),
                    value=repeat_kind,
                    allow_blank=False,
                    id="task-edit-repeat",
                )
                with Vertical(id="task-repeat-options"):
                    yield Label("Repeat every (number of periods)")
                    yield Input(value=repeat_interval, type="integer", id="task-repeat-interval")
                    yield Label("Occurrences (blank for no end)")
                    yield Input(value=repeat_count, type="integer", id="task-repeat-count")
                    yield Label("Or end on (YYYY-MM-DD)")
                    yield Input(value=repeat_until, id="task-repeat-until")
                yield Input(
                    value=json.dumps(task.recurrence, sort_keys=True)
                    if task and task.recurrence
                    else "",
                    placeholder="Advanced recurrence JSON",
                    id="task-edit-recurrence",
                )
                yield TextArea(task.body if task else "", id="task-editor-body")
            yield Static("", id="task-editor-error", markup=False)
            with Horizontal(id="task-editor-actions"):
                yield Button("Cancel", id="task-edit-cancel")
                yield Button("Save", id="task-edit-save", variant="primary")

    @on(Select.Changed, "#task-edit-repeat")
    def _repeat_changed(self) -> None:
        self._update_repeat_controls()

    def _update_repeat_controls(self) -> None:
        kind = str(self.query_one("#task-edit-repeat", Select).value)
        self.query_one("#task-repeat-options").display = kind not in {"none", "advanced"}
        self.query_one("#task-edit-recurrence").display = kind == "advanced"
        self.call_after_refresh(self._reveal_focus)

    def _recurrence_value(
        self, start: date | None, due: date | None
    ) -> Mapping[str, object] | None:
        kind = str(self.query_one("#task-edit-repeat", Select).value)
        if kind == "none":
            return None
        if kind == "advanced":
            rule = _parse_recurrence(self.query_one("#task-edit-recurrence", Input).value)
            if rule is None:
                raise PublicOperationError(
                    "Advanced recurrence needs a task rule.", retryable=False
                )
            try:
                return normalize_recurrence_mapping(rule, task=True, start=start or due)
            except ConfigurationError as exc:
                raise PublicOperationError(str(exc), retryable=False) from exc
        anchor = start or due
        if anchor is None:
            raise PublicOperationError(
                "Set a start or due date for a repeating task.", retryable=False
            )
        try:
            interval = int(self.query_one("#task-repeat-interval", Input).value.strip() or "1")
            count_text = self.query_one("#task-repeat-count", Input).value.strip()
            count = int(count_text) if count_text else None
            until = _parse_date(
                self.query_one("#task-repeat-until", Input).value, field="Repeat-until date"
            )
            return TaskRepeatPreferences(TaskRepeatKind(kind), interval, count).rule(
                anchor, until=until
            )
        except (ConfigurationError, ValueError) as exc:
            raise PublicOperationError(f"Invalid task repeat: {exc}", retryable=False) from exc

    def _value(self) -> TaskEditorValue:
        subject = self.query_one("#task-edit-subject", Input).value.strip()
        if not subject:
            raise PublicOperationError("A task needs a subject.", retryable=False)
        status_value = self.query_one("#task-edit-status", Select).value
        importance_value = self.query_one("#task-edit-importance", Select).value
        if status_value is Select.BLANK or importance_value is Select.BLANK:
            raise PublicOperationError("Choose task status and importance.", retryable=False)
        try:
            percent = int(self.query_one("#task-edit-percent", Input).value.strip())
        except ValueError as exc:
            raise PublicOperationError(
                "Percent complete must be a whole number.", retryable=False
            ) from exc
        if not 0 <= percent <= 100:
            raise PublicOperationError("Percent complete must be from 0 to 100.", retryable=False)
        status = TaskStatus(str(status_value))
        if status is TaskStatus.COMPLETED:
            percent = 100
        elif percent == 100:
            raise PublicOperationError(
                "Only completed tasks may be 100 percent complete.", retryable=False
            )
        elif status is TaskStatus.NOT_STARTED and percent != 0:
            raise PublicOperationError(
                "Not-started tasks must be zero percent complete.", retryable=False
            )
        start = _parse_date(self.query_one("#task-edit-start", Input).value, field="Start date")
        due = _parse_date(self.query_one("#task-edit-due", Input).value, field="Due date")
        if start and due and due < start:
            raise PublicOperationError("Due date cannot be before start date.", retryable=False)
        reminder = _parse_optional_aware_datetime(
            self.query_one("#task-edit-reminder", Input).value,
            field="Reminder time",
        )
        return TaskEditorValue(
            subject=subject,
            status=status,
            percent_complete=percent,
            start_date=start,
            due_date=due,
            body=self.query_one("#task-editor-body", TextArea).text,
            categories=_csv(self.query_one("#task-edit-categories", Input).value),
            importance=Importance(str(importance_value)),
            reminder_at=reminder,
            recurrence=self._recurrence_value(start, due),
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
        if event.button.id == "task-edit-save":
            self.action_save()
        elif event.button.id == "task-edit-cancel":
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
                    "The request is bounded to 100 attendees and one explicit time window. "
                    "No calendar item is changed.",
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
            yield Static("Loading a bounded calendar page…", id="area-state", markup=False)
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
        table.add_column("Start", key="start")
        table.add_column("End", key="end")
        table.add_column("Location", key="location")
        table.add_column("Kind", key="kind")
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
        self.set_workspace_title(
            self.query_one("#calendar-title", Label), "Calendar · " + self.timezone
        )
        table = self.query_one("#calendar-table", DataTable)
        for event in self._events:
            start, end = event_time_labels(event, self.timezone)
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
            start, end = event_time_labels(event, self.timezone)
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
                (
                    page.notice + " · "
                    if page is not None and page.notice
                    else f"{len(events)} events loaded · "
                )
                + f"{request.view.value if request else self.view_kind.value} · "
                f"anchor {(request.anchor if request else self.anchor_date).isoformat()} · "
                f"query zone {(request.timezone if request else self.timezone)}.",
            )
        else:
            self._highlighted = None
            message = (
                page.notice if page is not None else None
            ) or "No events match this bounded view."
            self.query_one("#calendar-detail", Static).update(message)
            self._update_buttons()
            self._set_view_state(
                ViewState.READY if self._next_calendar_cursor else ViewState.EMPTY,
                message or "No events match this bounded calendar view.",
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
            ViewState.READY, f"{len(self._selected)} events selected for bounded batch actions."
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
            "Loading read-only series history",
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
                else "Only this bounded selection is affected."
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
        self._set_view_state(ViewState.READY, "Bounded availability lookup completed.")

    def action_show_conflicts(self) -> None:
        conflicts = detect_conflicts(self._events)
        if not conflicts:
            self.query_one("#calendar-detail", Static).update(
                "No conflicts in this bounded calendar view."
            )
            self._set_view_state(ViewState.READY, "No blocking event overlaps detected.")
            return
        lines = [f"{len(conflicts)} conflict(s) in this bounded view:"]
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
            "The import is bounded to 200 events. Existing events are not deleted.",
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
    start, end = event_time_labels(event, timezone)
    lines = [
        clean_cell(event.subject or "(no subject)", max_length=180),
        f"Start: {start}",
        f"End: {end}",
        f"Display zone: {clean_cell(timezone, max_length=80)}",
        f"Event zone: {clean_cell(event.timezone, max_length=80)}",
        f"Location: {clean_cell(event.location or '(none)', max_length=160)}",
        "Type: "
        f"{'Meeting' if event.is_meeting else 'Event'} · "
        f"{'All day' if event.is_all_day else 'Timed'}",
        f"Availability: {clean_cell(event.availability, max_length=40)} · "
        "Reminder: "
        f"{event.reminder_minutes if event.reminder_minutes is not None else 'off'}",
        f"Required attendees: {len(event.required_attendees)} · "
        f"Optional attendees: {len(event.optional_attendees)}",
        f"Recurrence: {'configured' if event.recurrence else 'none'}",
        f"Response: {clean_cell(event.response_status or '(none)', max_length=60)}",
        f"Attachments: {len(event.attachments)}",
    ]
    if complete:
        lines.extend(
            ("Stored start: " + event.start.isoformat(), "Stored end: " + event.end.isoformat())
        )
        if event.organizer:
            lines.append(
                "Organizer: "
                + clean_cell(
                    event.organizer.name or event.organizer.email,
                    max_length=180,
                )
            )
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
        if event.recurrence:
            lines.append(
                "Recurrence rule: "
                + clean_cell(json.dumps(event.recurrence, sort_keys=True), max_length=600)
            )
        lines.extend(
            f"Attachment: {clean_cell(item.name, max_length=160)} · "
            f"{item.size or 0} bytes · ref {clean_cell(item.reference, max_length=160)}"
            for item in event.attachments
        )
    if complete and event.body:
        lines.extend(("", clean_cell(event.body, max_length=2_000)))
    return "\n".join(lines)


def _attachment_paths(value: str) -> tuple[Path, ...]:
    return tuple(
        dict.fromkeys(Path(part.strip()).expanduser() for part in value.split(";") if part.strip())
    )


def _batch_message(result: BatchActionResult, *, noun: str) -> tuple[ViewState, str]:
    """Render a reconciliation-friendly mutation summary for a later refresh."""

    parts = [f"Completed {result.completed}/{result.attempted} {noun} operation(s)"]
    if result.skipped_references:
        parts.append(f"skipped {len(result.skipped_references)}")
    if result.failed_references:
        parts.append(f"failed/uncertain {len(result.failed_references)}")
        if result.uncertain_references:
            parts.append(
                f"may have changed {len(result.uncertain_references)}; inspect current state"
            )
        parts.append("review before retrying")
        return ViewState.ERROR, " · ".join(parts) + "."
    return ViewState.READY, " · ".join(parts) + "."


class _PagedPimScreen(SubmissionWorkspace):
    """Shared explicit navigation for bounded contact/task candidate windows."""

    def _initial_refresh(self) -> None:
        if self._operation_generation == 0:
            self.action_refresh()

    def _reset_paging(self) -> None:
        self._page_offset = 0
        self._page_history: list[int] = []
        self._next_page_offset: int | None = None
        self._page_signature: tuple[str, ...] | None = None
        self._page_notice = ""

    def _prepare_page(self, *signature: str) -> int:
        if self._page_signature != signature:
            self._reset_paging()
            self._page_signature = signature
        for direction in ("prev", "next"):
            self.query_one(f"#{self.area_name}-{direction}", Button).disabled = True
        return self._page_offset

    def _page_result(self, result: object) -> object:
        records: object
        if isinstance(result, PimPage):
            self._next_page_offset = result.next_offset
            self._page_notice = result.notice
            records = result.items
        else:
            self._next_page_offset = None
            self._page_notice = ""
            records = result
        self.query_one(f"#{self.area_name}-prev", Button).disabled = not self._page_history
        self.query_one(f"#{self.area_name}-next", Button).disabled = self._next_page_offset is None
        return records

    def _page_summary(self, count: int) -> str:
        status = self.query_one("#area-state", Static)
        status.tooltip = self._page_notice
        return f"{count} {self.area_name} · Current page" + (
            " · More available" if self._next_page_offset is not None else ""
        )

    def action_previous_page(self) -> None:
        if self._page_history and not self._operation_active:
            self._page_offset = self._page_history.pop()
            self._load_page()

    def action_next_page(self) -> None:
        if self._next_page_offset is not None and not self._operation_active:
            self._page_history.append(self._page_offset)
            self._page_offset = self._next_page_offset
            self._load_page()

    def _load_page(self) -> None:
        raise NotImplementedError


class ContactsScreen(_PagedPimScreen):
    """Bounded mailbox Contacts view with CRUD, vCard, and reviewed merging."""

    DEFAULT_CSS = """
    ContactsScreen { layout: vertical; }
    #contacts-root { width: 100%; height: 100%; padding: 1 2; overflow-y: auto; }
    #contacts-title { height: 2; text-style: bold; }
    #contacts-toolbar { height: 3; }
    #contacts-search { width: 1fr; margin-right: 1; }
    #contacts-toolbar Button { margin-right: 1; }
    #contacts-capabilities { height: auto; min-height: 1; color: $text-muted; }
    #contacts-content { height: 1fr; min-height: 4; }
    #contacts-table { width: 3fr; height: 1fr; }
    #contacts-detail-pane {
        width: 2fr; height: 1fr; border-left: solid $panel;
        padding: 1;
    }
    #contacts-detail-pane:focus { border-left: solid $accent; }
    #contacts-detail { width: 1fr; height: auto; }
    #contacts-actions, #contacts-footer { height: auto; min-height: 3; }
    #contacts-actions Button { margin-right: 1; }
    #contacts-footer { dock: bottom; height: 3; align-horizontal: right; }
    #contacts-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("n", "create", "New contact", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("space", "toggle_selection", "Select", show=False),
        Binding("enter", "details", "Details", show=False),
    ]

    def __init__(
        self,
        backend: ContactsScreenBackend,
        *,
        page_size: int = 100,
        sort_order: str = "display_name",
        display_preferences: Callable[[], ContactsDisplayPreferences] | None = None,
        operation_preferences: Callable[[], ContactOperationPreferences] | None = None,
        offline: bool = False,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            area_name="contacts",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        self.backend = backend
        self.page_size = _bounded_limit(page_size)
        self._reset_paging()
        self.display_preferences = display_preferences or (
            lambda: ContactsDisplayPreferences(sort_order=sort_order)
        )
        self._display = self.display_preferences()
        self.operation_preferences = operation_preferences or ContactOperationPreferences
        self._operations = self.operation_preferences()
        self.contact_sort_order = self._display.sort_order
        self._complete_detail_reference: str | None = None
        self._contact_report: str | None = None
        self._contacts: tuple[ContactInfo, ...] = ()
        self._by_reference: dict[str, ContactInfo] = {}
        self._highlighted: str | None = None
        self._selected: set[str] = set()
        self._pending_notice: tuple[ViewState, str] | None = None

    @property
    def selected_contact_value(self) -> str | None:
        """Return the selected contact's first address, falling back to its reference."""

        contact = self._by_reference.get(self._highlighted or "")
        if contact is None:
            return None
        return contact.emails[0].email if contact.emails else contact.reference

    def compose(self) -> ComposeResult:
        with Vertical(id="contacts-root"):
            yield Label("Contacts", id="contacts-title")
            yield Static("Loading a bounded contact page…", id="area-state", markup=False)
            yield Input(
                placeholder="Search contacts…",
                id="contacts-search",
            )
            with Horizontal(id="contacts-toolbar"):
                yield Button("Search", id="contacts-search-button")
                yield Button("Clear", id="contacts-clear")
                yield Button("Find duplicates", id="contacts-duplicates")
                yield Button("Directory", id="contacts-directory")
                yield Button("Contact groups", id="contacts-groups")
                yield Button("Local history", id="contacts-history-index")
            yield Static(
                _capability_summary(self.backend.capabilities),
                id="contacts-capabilities",
                markup=False,
            )
            with Horizontal(id="contacts-content"):
                yield DataTable(id="contacts-table", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="contacts-detail-pane"):
                    yield Static(
                        "Select a contact to inspect it.", id="contacts-detail", markup=False
                    )
            with HorizontalScroll(id="contacts-actions"):
                yield Button("Create", id="contacts-create", variant="primary")
                yield Button("Edit", id="contacts-edit", disabled=True)
                yield Button("Delete", id="contacts-delete", disabled=True)
                yield Button("Merge selected", id="contacts-merge", disabled=True)
                yield Button("Import vCard", id="contacts-import")
                yield Button("Export vCard", id="contacts-export", disabled=True)
                yield Button("Compose", id="contacts-compose", disabled=True)
                yield Button("Meeting", id="contacts-meeting", disabled=True)
                yield Button("Correspondence", id="contacts-correspondence", disabled=True)
                yield Button("Open mail", id="contacts-correspondence-mail", disabled=True)
            yield Static("", id="area-progress", markup=False)
            with HorizontalScroll(id="contacts-footer"):
                retry = Button("Retry", id="area-retry")
                retry.display = False
                yield retry
                cancel = Button("Cancel operation", id="area-cancel-operation")
                cancel.display = False
                yield cancel
                yield Button("Previous", id="contacts-prev", disabled=True)
                yield Button("Next", id="contacts-next", disabled=True)
                yield Button("Refresh", id="contacts-refresh")
                yield Button("Close", classes="area-close")

    @on(Button.Pressed, "#contacts-groups")
    def _contact_groups_button(self, event: Button.Pressed) -> None:
        from gucmail.tui.contact_groups_screen import ContactGroupsRequested

        event.stop()
        self.post_message(ContactGroupsRequested())

    def on_mount(self) -> None:
        table = self.query_one("#contacts-table", DataTable)
        table.add_column("✓", key="selected", width=2)
        table.add_column("Name", key="name")
        table.add_column("Email", key="email")
        table.add_column("Company", key="company")
        table.add_column("Phone", key="phone")
        self._apply_capabilities()
        self.query_one("#contacts-capabilities").display = bool(
            _capability_summary(self.backend.capabilities)
        )
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Contacts are unavailable offline. Reopen without --offline to connect.",
            )
            return
        self.call_after_refresh(self._initial_refresh)

    def _initial_refresh(self) -> None:
        # A palette action may arrive before the first rendered frame. Never let
        # the deferred startup refresh cancel its directory/detail operation.
        if self._operation_generation == 0:
            self.action_refresh()

    def _apply_capabilities(self) -> None:
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        self.query_one("#contacts-create", Button).disabled = not mutate
        self.query_one("#contacts-import", Button).disabled = self.offline or not _capability(
            self.backend.capabilities, "import"
        )
        self._update_buttons()

    def on_screen_resume(self) -> None:
        if not self.is_mounted:
            return
        try:
            self._operations = self.operation_preferences()
            if (self._contact_report == "directory" and not self._operations.directory_lookup) or (
                self._contact_report == "history" and not self._operations.correspondence_history
            ):
                self.query_one("#contacts-detail", Static).update(
                    "This contact report is disabled in Contacts settings."
                )
                self._contact_report = None
            refresh = getattr(self.backend, "refresh_capabilities", None)
            if callable(refresh):
                refresh()
            self._apply_capabilities()
            self.query_one("#contacts-capabilities", Static).update(
                _capability_summary(self.backend.capabilities)
            )
            self.query_one("#contacts-capabilities").display = bool(
                _capability_summary(self.backend.capabilities)
            )
        except ConfigurationError as exc:
            self._show_error("Contacts preferences", exc)

    def _allow_contact_operation(self, operation: str) -> bool:
        try:
            self._operations = self.operation_preferences()
            self._operations.require(operation)
            return True
        except ConfigurationError as exc:
            self._show_error("Contacts preferences", exc)
            return False

    def action_refresh(self) -> None:
        self._reset_paging()
        self._load_page()

    def _load_page(self) -> None:
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Contacts are unavailable offline. Reopen without --offline to connect.",
            )
            return
        try:
            self._display = self.display_preferences()
            self.contact_sort_order = self._display.sort_order
        except ConfigurationError as exc:
            self._show_error("Contacts preferences", exc)
            return
        query = self.query_one("#contacts-search", Input).value.strip()
        offset = self._prepare_page(query)
        page_loader = getattr(self.backend, "list_contacts_page", None)
        self._begin_operation(
            "Loading contacts",
            lambda cancel, progress: (
                page_loader(query, self.page_size, offset=offset, cancel=cancel, progress=progress)
                if callable(page_loader)
                else tuple(
                    self.backend.list_contacts(
                        query,
                        self.page_size,
                        cancel=cancel,
                        progress=progress,
                    )
                )
            ),
            self._contacts_loaded,
            retry_safe=True,
        )

    def _contacts_loaded(self, result: object) -> None:
        contacts = cast(tuple[ContactInfo, ...], self._page_result(result))
        if not all(isinstance(item, ContactInfo) for item in contacts):
            raise PublicOperationError("The Contacts backend returned invalid records.")
        by_reference = {contact.reference: contact for contact in contacts}
        if len(by_reference) != len(contacts):
            raise PublicOperationError("The Contacts backend returned duplicate references.")
        self._contacts = contacts
        self._by_reference = by_reference
        self._complete_detail_reference = None
        self._selected.intersection_update(by_reference)
        self._render_contacts()
        if contacts:
            state, message = self._pending_notice or (
                ViewState.READY,
                f"{len(contacts)} contacts loaded from one bounded page.",
            )
        else:
            state, message = self._pending_notice or (
                ViewState.EMPTY,
                "No contacts match this bounded contact view.",
            )
        if self._page_notice:
            message = (
                message + " " + self._page_summary(len(contacts))
                if self._pending_notice
                else self._page_summary(len(contacts))
            )
        self._pending_notice = None
        self._set_view_state(state, message)

    def refresh_display_preferences(self) -> None:
        """Apply presentation and duplicate sensitivity locally to the loaded page."""
        self._display = self.display_preferences()
        self.contact_sort_order = self._display.sort_order
        if self.is_mounted:
            self._render_contacts()

    def _render_contacts(self) -> None:
        contacts = self._display.sort(
            self._by_reference[contact.reference] for contact in self._contacts
        )
        self._contacts = contacts
        table = self.query_one("#contacts-table", DataTable)
        table.clear()
        for contact in contacts:
            first_email = contact.emails[0].email if contact.emails else ""
            first_phone = next(iter(contact.phones.values()), "")
            table.add_row(
                "●" if contact.reference in self._selected else "",
                clean_cell(self._display.label(contact), max_length=100),
                clean_cell(first_email, max_length=100),
                clean_cell(contact.company or "", max_length=70),
                clean_cell(first_phone, max_length=40),
                key=contact.reference,
            )
        if contacts:
            reference = (
                self._highlighted
                if self._highlighted in self._by_reference
                else contacts[0].reference
            )
            self._select(reference)
            table.move_cursor(
                row=next(i for i, item in enumerate(contacts) if item.reference == reference)
            )
        else:
            self._highlighted = None
            self.query_one("#contacts-detail", Static).update("No contacts match this view.")
            self._update_buttons()

    @on(DataTable.RowHighlighted, "#contacts-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select(str(event.row_key.value))

    @on(DataTable.RowSelected, "#contacts-table")
    def _row_selected(self) -> None:
        self.action_details()

    def _select(self, reference: str) -> None:
        contact = self._by_reference.get(reference)
        if contact is None:
            return
        changed = reference != self._highlighted
        self._contact_report = None
        self._highlighted = reference
        self.query_one("#contacts-detail", Static).update(
            _render_contact(
                contact,
                complete=reference == self._complete_detail_reference,
                preferences=self._display,
            )
        )
        if changed:
            self.query_one("#contacts-detail-pane", VerticalScroll).scroll_home(animate=False)
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
        targets = self._targets()
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        single = len(targets) == 1
        self.query_one("#contacts-edit", Button).disabled = not single or not mutate
        self.query_one("#contacts-delete", Button).disabled = not targets or not mutate
        self.query_one("#contacts-merge", Button).disabled = (
            len(targets) < 2 or self.offline or not _capability(self.backend.capabilities, "merge")
        )
        self.query_one("#contacts-export", Button).disabled = not targets or not _capability(
            self.backend.capabilities, "export"
        )
        has_email = any(self._by_reference[reference].emails for reference in targets)
        self.query_one("#contacts-compose", Button).disabled = not has_email
        self.query_one("#contacts-meeting", Button).disabled = not has_email
        correspondence_disabled = (
            not targets
            or self.offline
            or not _capability(self.backend.capabilities, "correspondence")
            or not self._operations.correspondence_history
        )
        self.query_one("#contacts-correspondence", Button).disabled = correspondence_disabled
        self.query_one("#contacts-correspondence-mail", Button).disabled = (
            correspondence_disabled or not has_email
        )
        self.query_one("#contacts-directory", Button).disabled = (
            self.offline
            or not self._operations.directory_lookup
            or not _capability(self.backend.capabilities, "directory")
        )
        self.query_one("#contacts-duplicates", Button).disabled = (
            len(self._contacts) < 2 or not self._operations.duplicate_detection
        )

    def action_toggle_selection(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        if reference in self._selected:
            self._selected.remove(reference)
        else:
            self._selected.add(reference)
        self.query_one("#contacts-table", DataTable).update_cell(
            reference,
            "selected",
            "●" if reference in self._selected else "",
        )
        self._update_buttons()
        self._set_view_state(
            ViewState.READY,
            f"{len(self._selected)} contacts selected for bounded batch actions.",
        )

    def action_details(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        self._begin_operation(
            "Loading contact details",
            lambda cancel, progress: self.backend.get_contact(
                reference,
                cancel=cancel,
                progress=progress,
            ),
            self._detail_loaded,
            retry_safe=True,
        )

    def _detail_loaded(self, result: object) -> None:
        if not isinstance(result, ContactInfo):
            raise PublicOperationError("The Contacts backend returned invalid details.")
        self._by_reference[result.reference] = result
        self._complete_detail_reference = result.reference
        self.query_one("#contacts-detail", Static).update(
            _render_contact(result, complete=True, preferences=self._display)
        )
        self.query_one("#contacts-detail-pane", VerticalScroll).scroll_home(animate=False)
        self._set_view_state(ViewState.READY, "Complete contact details loaded.")

    def action_create(self) -> None:
        if self.offline or not _capability(self.backend.capabilities, "create"):
            self._show_error("Create contact", OfflineOperationError())
            return
        self._open_submission_editor(ContactEditorScreen(None), self._create_value)

    def action_create_prefilled(self, sender: Address) -> None:
        """Open the create form with a safely bounded message sender."""

        email = sender.email.strip()
        if not email:
            self._set_view_state(ViewState.EMPTY, "The selected message has no sender address.")
            return
        prefill = ContactInfo(
            reference=f"prefill-{uuid4().hex}",
            display_name=(sender.name or email).strip(),
            emails=(Address(email, sender.name),),
        )
        self._open_submission_editor(
            ContactEditorScreen(prefill, creating=True), self._create_value
        )

    def configure_search(self, query: str, *, directory: bool = False) -> None:
        """Apply a contextual palette search after the screen has mounted."""

        search = self.query_one("#contacts-search", Input)
        search.value = query.strip()
        search.focus()
        if not search.value:
            self._set_view_state(ViewState.EMPTY, "No contact search value was available.")
        elif directory:
            self.action_directory()
        else:
            self.action_refresh()

    def _create_value(self, value: ContactEditorValue | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Creating contact",
            lambda cancel, progress: self.backend.create_contact(
                value,
                cancel=cancel,
                progress=progress,
            ),
            self._mutation_loaded,
        )

    def action_edit(self) -> None:
        targets = self._targets()
        if len(targets) != 1 or self.offline:
            return
        reference = targets[0]
        self._begin_operation(
            "Loading contact for edit",
            lambda cancel, progress: self.backend.get_contact(
                reference,
                cancel=cancel,
                progress=progress,
            ),
            self._open_editor,
            retry_safe=True,
        )

    def _open_editor(self, result: object) -> None:
        if not isinstance(result, ContactInfo):
            raise PublicOperationError("The Contacts backend returned invalid details.")
        self._open_submission_editor(
            ContactEditorScreen(result),
            lambda value: self._edit_value(result.reference, value),
        )

    def _edit_value(self, reference: str, value: ContactEditorValue | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Updating contact",
            lambda cancel, progress: self.backend.update_contact(
                reference,
                value,
                cancel=cancel,
                progress=progress,
            ),
            self._mutation_loaded,
        )

    def _mutation_loaded(self, result: object) -> None:
        if not isinstance(result, ContactInfo):
            raise PublicOperationError("The Contacts backend returned an invalid contact.")
        self._highlighted = result.reference
        self._pending_notice = (ViewState.READY, "Contact mutation completed.")
        self.action_refresh()

    def action_delete(self) -> None:
        references = self._targets()
        if not references or self.offline or not _capability(self.backend.capabilities, "create"):
            return
        self._confirm(
            "Delete selected contacts?",
            f"Contacts: {len(references)}\n\n"
            "They are moved to Deleted Items. Only this bounded selection is affected.",
            lambda: self._start_delete(references),
            confirm_label="Delete",
        )

    def _start_delete(self, references: tuple[str, ...]) -> None:
        self._begin_operation(
            "Deleting contacts",
            lambda cancel, progress: self.backend.delete_contacts(
                references,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="contact"),
        )

    def _batch_finished(self, result: object, *, noun: str) -> None:
        if not isinstance(result, BatchActionResult):
            raise PublicOperationError("The Contacts backend returned an invalid batch result.")
        self._selected.clear()
        self._pending_notice = _batch_message(result, noun=noun)
        self.action_refresh()

    def action_find_duplicates(self) -> None:
        if not self._allow_contact_operation("duplicates"):
            return
        threshold = self._display.duplicate_threshold
        groups = find_duplicate_groups(self._contacts, threshold=threshold)
        if not groups:
            self._set_view_state(
                ViewState.READY,
                f"No explainable duplicates met threshold {threshold} in this bounded page.",
            )
            return
        group = groups[0]
        self._selected = {contact.reference for contact in group.contacts}
        table = self.query_one("#contacts-table", DataTable)
        for reference in self._by_reference:
            table.update_cell(
                reference,
                "selected",
                "●" if reference in self._selected else "",
            )
        reasons = sorted({reason for candidate in group.candidates for reason in candidate.reasons})
        self.query_one("#contacts-detail", Static).update(
            "Possible duplicate group\n"
            f"Contacts: {len(group.contacts)}\n"
            f"Maximum score: {group.maximum_score}\n"
            f"Threshold: {threshold}\n"
            f"Reasons: {', '.join(reasons) or '(none)'}\n\n"
            "Review the selected records, then use Merge selected."
        )
        self.query_one("#contacts-detail-pane", VerticalScroll).scroll_home(animate=False)
        self._update_buttons()
        self._set_view_state(
            ViewState.READY,
            f"Selected the strongest of {len(groups)} duplicate group(s) in this page.",
        )

    def action_merge(self) -> None:
        references = self._targets()
        if (
            len(references) < 2
            or self.offline
            or not _capability(self.backend.capabilities, "merge")
        ):
            return
        self._begin_operation(
            "Preparing contact merge",
            lambda cancel, progress: self.backend.preview_merge(
                references,
                cancel=cancel,
                progress=progress,
            ),
            self._merge_preview_loaded,
            retry_safe=True,
        )

    def _merge_preview_loaded(self, result: object) -> None:
        if not isinstance(result, ContactMergePreviewView):
            raise PublicOperationError("The Contacts backend returned an invalid merge preview.")
        self._confirm(
            "Apply this contact merge?",
            result.render()
            + "\n\nThe primary is updated first; duplicates then move to Deleted Items.",
            lambda: self._start_merge(result),
            confirm_label="Merge",
        )

    def _start_merge(self, preview: ContactMergePreviewView) -> None:
        self._begin_operation(
            "Applying contact merge",
            lambda cancel, progress: self.backend.apply_merge(
                preview,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="merge"),
        )

    def action_import(self) -> None:
        if self.offline or not _capability(self.backend.capabilities, "import"):
            return
        self.app.push_screen(
            FilePathScreen("Import contacts", "Path to .vcf or .vcard file"),
            self._import_path,
        )

    def _import_path(self, path: str | None) -> None:
        if path is None:
            return
        self._confirm(
            "Import contacts from this vCard file?",
            f"Source: {clean_cell(path, max_length=400)}\n"
            "The operation checks one bounded mailbox contact page for duplicates and imports "
            "at most 200 records.",
            lambda: self._start_import(path),
            confirm_label="Import",
        )

    def _start_import(self, path: str) -> None:
        self._begin_operation(
            "Importing contacts",
            lambda cancel, progress: self.backend.import_vcard(
                path,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="import"),
        )

    def action_export(self) -> None:
        references = self._targets()
        if not references or not _capability(self.backend.capabilities, "export"):
            return
        self.app.push_screen(
            FilePathScreen("Export selected contacts", "New .vcf destination"),
            lambda path: self._export_path(references, path),
        )

    def _export_path(self, references: tuple[str, ...], path: str | None) -> None:
        if path is None:
            return
        self._begin_operation(
            "Exporting contacts",
            lambda cancel, progress: self.backend.export_vcard(
                references,
                path,
                cancel=cancel,
                progress=progress,
            ),
            self._export_finished,
        )

    def _export_finished(self, result: object) -> None:
        if not isinstance(result, ExportResult):
            raise PublicOperationError("The Contacts backend returned an invalid export result.")
        self._set_view_state(
            ViewState.READY,
            f"Exported {result.item_count} contacts to {clean_cell(result.path, max_length=300)}.",
        )

    def action_compose(self) -> None:
        references = self._targets()
        recipients = tuple(
            address.email
            for reference in references
            for address in self._by_reference[reference].emails
        )
        if not recipients:
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "compose",
                references,
                payload={"recipients": recipients},
            )
        )

    def action_schedule_meeting(self) -> None:
        references = self._targets()
        attendees = tuple(
            address.email
            for reference in references
            for address in self._by_reference[reference].emails
            if address.email.strip()
        )
        if not attendees:
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "schedule_meeting",
                references,
                payload={"attendees": tuple(dict.fromkeys(attendees))},
            )
        )

    def action_directory(self) -> None:
        if not self._allow_contact_operation("directory") or self.offline:
            return
        query = self.query_one("#contacts-search", Input).value.strip()
        if not query:
            self._set_view_state(
                ViewState.EMPTY,
                "Enter a name or address in Contact search before checking the directory.",
            )
            self.query_one("#contacts-search", Input).focus()
            return
        self._begin_operation(
            "Searching Exchange directory",
            lambda cancel, progress: tuple(
                self.backend.search_directory(
                    query,
                    limit=min(self.page_size, 100),
                    cancel=cancel,
                    progress=progress,
                )
            ),
            self._directory_loaded,
            retry_safe=True,
        )

    def _directory_loaded(self, result: object) -> None:
        if not self._allow_contact_operation("directory"):
            return
        self._contact_report = "directory"
        values = cast(tuple[Address, ...], result)
        if not all(isinstance(value, Address) for value in values):
            raise PublicOperationError("The directory backend returned invalid entries.")
        if not values:
            self.query_one("#contacts-detail", Static).update(
                "No Exchange directory entries matched this bounded lookup."
            )
            self._set_view_state(ViewState.EMPTY, "No directory matches.")
            return
        lines = [f"Exchange directory · {len(values)} bounded result(s)"]
        lines.extend(
            f"{clean_cell(value.name or '(no display name)', max_length=140)} · "
            f"{clean_cell(value.email, max_length=180)} · "
            f"{clean_cell(value.mailbox_type or 'unknown type', max_length=60)}"
            for value in values
        )
        if len(values) > 1:
            lines.append("Ambiguous result: choose a specific address before sending.")
        self.query_one("#contacts-detail", Static).update("\n".join(lines))
        self._set_view_state(ViewState.READY, "Directory lookup completed without mutation.")

    def action_correspondence(self) -> None:
        if not self._allow_contact_operation("history"):
            return
        references = self._targets()
        if (
            not references
            or self.offline
            or not _capability(self.backend.capabilities, "correspondence")
        ):
            return
        self._begin_operation(
            "Loading correspondence history",
            lambda cancel, progress: tuple(
                self.backend.correspondence_history(
                    references,
                    limit=self.page_size,
                    cancel=cancel,
                    progress=progress,
                )
            ),
            self._correspondence_loaded,
            retry_safe=True,
        )

    def _correspondence_loaded(self, result: object) -> None:
        if not self._allow_contact_operation("history"):
            return
        self._contact_report = "history"
        events = cast(tuple[CorrespondenceEvent, ...], result)
        if not all(isinstance(value, CorrespondenceEvent) for value in events):
            raise PublicOperationError("The correspondence backend returned invalid history.")
        if not events:
            self.query_one("#contacts-detail", Static).update(
                "No correspondence was found in the bounded inbox/sent history."
            )
            self._set_view_state(ViewState.EMPTY, "No bounded correspondence history.")
            return
        lines = [f"Correspondence history · {len(events)} bounded message observation(s)"]
        lines.extend(
            f"{event.occurred_at.isoformat()} · {event.direction.value} · "
            f"{clean_cell(event.address.email, max_length=160)} · "
            f"message {clean_cell(event.message_reference or '(unknown)', max_length=120)}"
            for event in events
        )
        self.query_one("#contacts-detail", Static).update("\n".join(lines))
        self._set_view_state(ViewState.READY, "Bounded correspondence history loaded.")

    def action_open_correspondence(self) -> None:
        """Ask the application shell to render bounded related mail for selected contacts."""

        if not self._allow_contact_operation("history"):
            return
        references = self._targets()[:10]
        recipients = tuple(
            dict.fromkeys(
                address.email.strip()
                for reference in references
                for address in self._by_reference[reference].emails
                if address.email.strip()
            )
        )[:20]
        if (
            not recipients
            or self.offline
            or not _capability(self.backend.capabilities, "correspondence")
        ):
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "correspondence_messages",
                references,
                payload={"addresses": recipients},
            )
        )

    @on(Input.Submitted, "#contacts-search")
    def _search_submitted(self) -> None:
        self.action_refresh()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "contacts-history-index":
            self.post_message(PimActionRequested("contacts", "history_index", ()))
            return
        actions = {
            "contacts-search-button": self.action_refresh,
            "contacts-prev": self.action_previous_page,
            "contacts-next": self.action_next_page,
            "contacts-duplicates": self.action_find_duplicates,
            "contacts-create": self.action_create,
            "contacts-edit": self.action_edit,
            "contacts-delete": self.action_delete,
            "contacts-merge": self.action_merge,
            "contacts-import": self.action_import,
            "contacts-export": self.action_export,
            "contacts-compose": self.action_compose,
            "contacts-meeting": self.action_schedule_meeting,
            "contacts-correspondence": self.action_correspondence,
            "contacts-correspondence-mail": self.action_open_correspondence,
            "contacts-directory": self.action_directory,
            "contacts-refresh": self.action_refresh,
        }
        if event.button.id == "contacts-clear":
            self.query_one("#contacts-search", Input).value = ""
            self.action_refresh()
            return
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


class CorrespondentsScreen(OperationalScreen):
    """Actionable, bounded recent/frequent recipient history."""

    DEFAULT_CSS = """
    CorrespondentsScreen { layout: vertical; }
    #correspondents-root { width: 100%; height: 100%; padding: 1 2; }
    #correspondents-title { height: 2; text-style: bold; }
    #correspondents-content { height: 1fr; min-height: 8; }
    #correspondents-table { width: 3fr; height: 1fr; }
    #correspondents-detail {
        width: 2fr; height: 1fr; border-left: solid $panel;
        padding: 1; overflow-y: auto;
    }
    #correspondents-actions { height: auto; min-height: 3; }
    #correspondents-actions Button { margin-right: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("enter", "open_mail", "Open mail", show=False),
        Binding("c", "compose", "Compose", show=False),
    ]

    def __init__(
        self,
        backend: ContactsScreenBackend,
        *,
        order: str,
        page_size: int = 50,
        operation_preferences: Callable[[], ContactOperationPreferences] | None = None,
        offline: bool = False,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if order not in {"recent", "frequency"}:
            raise ValueError(f"Unknown correspondent order: {order}")
        super().__init__(
            area_name="contacts",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        self.backend = backend
        self.order = order
        self.page_size = _bounded_limit(page_size)
        self.operation_preferences = operation_preferences or ContactOperationPreferences
        self._values: dict[str, CorrespondentSummary] = {}
        self._highlighted: str | None = None

    def compose(self) -> ComposeResult:
        label = "Recent recipients" if self.order == "recent" else "Frequent correspondents"
        with Vertical(id="correspondents-root"):
            yield Label(f"Contacts · {label}", id="correspondents-title", markup=False)
            yield Static("Loading bounded correspondence history…", id="area-state", markup=False)
            with Horizontal(id="correspondents-content"):
                yield DataTable(id="correspondents-table", cursor_type="row", zebra_stripes=True)
                yield Static(
                    "Select a correspondent for counts and actions.",
                    id="correspondents-detail",
                    markup=False,
                )
            with HorizontalScroll(id="correspondents-actions"):
                yield Button("Compose", id="correspondents-compose", disabled=True)
                yield Button("Open mail", id="correspondents-open-mail", disabled=True)
                yield Button("Refresh", id="correspondents-refresh")
                yield Button("Close", classes="area-close")
            yield Static("", id="area-progress", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#correspondents-table", DataTable)
        table.add_column("Name", key="name")
        table.add_column("Address", key="address")
        table.add_column("Messages", key="count", width=10)
        table.add_column("In", key="inbound", width=6)
        table.add_column("Out", key="outbound", width=6)
        table.add_column("Last seen", key="last_seen")
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Correspondence history requires the configured bounded mail-history gateway.",
            )
            return
        self.call_after_refresh(self.action_refresh)

    def _history_allowed(self) -> bool:
        try:
            self.operation_preferences().require(
                "frequent" if self.order == "frequency" else "history"
            )
        except ConfigurationError as exc:
            self._values.clear()
            self._highlighted = None
            self.query_one("#correspondents-table", DataTable).clear()
            self.query_one("#correspondents-detail", Static).update("History access is disabled.")
            self.query_one("#correspondents-compose", Button).disabled = True
            self.query_one("#correspondents-open-mail", Button).disabled = True
            self._show_error("Contacts preferences", exc)
            return False
        return True

    def action_refresh(self) -> None:
        if self.offline or not self._history_allowed():
            return
        self._begin_operation(
            "Loading correspondents",
            lambda cancel, progress: tuple(
                self.backend.list_correspondents(
                    self.order,
                    self.page_size,
                    cancel=cancel,
                    progress=progress,
                )
            ),
            self._loaded,
            retry_safe=True,
        )

    def on_screen_resume(self) -> None:
        # Hide old results immediately if privacy changed while another
        # operation was in flight. Late results recheck the same policy.
        if self.is_mounted and self._history_allowed() and not self._operation_active:
            self.action_refresh()

    def _loaded(self, result: object) -> None:
        if not self._history_allowed():
            return
        values = cast(tuple[CorrespondentSummary, ...], result)
        if not all(isinstance(value, CorrespondentSummary) for value in values):
            raise PublicOperationError("The Contacts backend returned invalid correspondents.")
        self._values = {value.address.email.casefold(): value for value in values}
        table = self.query_one("#correspondents-table", DataTable)
        table.clear()
        for key, value in self._values.items():
            table.add_row(
                clean_cell(value.address.name or "(no display name)", max_length=100),
                clean_cell(value.address.email, max_length=160),
                str(value.count),
                str(value.inbound_count),
                str(value.outbound_count),
                value.last_seen.isoformat(timespec="minutes"),
                key=key,
            )
        if not values:
            self._highlighted = None
            self.query_one("#correspondents-detail", Static).update(
                "No bounded correspondence history is available yet."
            )
            self._set_actions(False)
            self._set_view_state(ViewState.EMPTY, "No correspondents found.")
            return
        self._select(values[0].address.email.casefold())
        ordering = "most recent first" if self.order == "recent" else "highest frequency first"
        self._set_view_state(
            ViewState.READY,
            f"{len(values)} bounded correspondent(s) · {ordering}.",
        )

    @on(DataTable.RowHighlighted, "#correspondents-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select(str(event.row_key.value))

    @on(DataTable.RowSelected, "#correspondents-table")
    def _row_selected(self) -> None:
        self.action_open_mail()

    def _select(self, key: str) -> None:
        value = self._values.get(key)
        if value is None:
            return
        self._highlighted = key
        self.query_one("#correspondents-detail", Static).update(
            "\n".join(
                (
                    clean_cell(value.address.name or "(no display name)", max_length=160),
                    clean_cell(value.address.email, max_length=200),
                    f"Messages: {value.count}",
                    f"Inbound: {value.inbound_count}",
                    f"Outbound: {value.outbound_count}",
                    f"First seen: {value.first_seen.isoformat()}",
                    f"Last seen: {value.last_seen.isoformat()}",
                )
            )
        )
        self._set_actions(True)

    def _set_actions(self, enabled: bool) -> None:
        self.query_one("#correspondents-compose", Button).disabled = not enabled
        self.query_one("#correspondents-open-mail", Button).disabled = not enabled

    def _selected(self) -> CorrespondentSummary | None:
        return self._values.get(self._highlighted or "")

    @property
    def selected_contact_value(self) -> str | None:
        value = self._selected()
        return value.address.email if value is not None else None

    def action_compose(self) -> None:
        value = self._selected()
        if value is None:
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "compose",
                (),
                payload={"recipients": (value.address.email,)},
            )
        )

    def action_open_mail(self) -> None:
        if not self._history_allowed():
            return
        value = self._selected()
        if value is None:
            return
        self.post_message(
            PimActionRequested(
                "contacts",
                "correspondence_messages",
                (),
                payload={"addresses": (value.address.email,)},
            )
        )

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "correspondents-compose": self.action_compose,
            "correspondents-open-mail": self.action_open_mail,
            "correspondents-refresh": self.action_refresh,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


class TasksScreen(_PagedPimScreen):
    """Bounded Exchange Tasks view with reviewed lifecycle and batch outcomes."""

    DEFAULT_CSS = """
    TasksScreen { layout: vertical; }
    #tasks-root { width: 100%; height: 100%; padding: 1 2; overflow-y: auto; }
    #tasks-title { height: 2; text-style: bold; }
    #tasks-toolbar { height: 3; }
    #tasks-search { width: 2fr; margin-right: 1; }
    #tasks-view { width: 1fr; margin-right: 1; }
    #tasks-toolbar Button { margin-right: 1; }
    #tasks-capabilities { height: auto; min-height: 1; color: $text-muted; }
    #tasks-content { height: 1fr; min-height: 4; }
    #tasks-table { width: 3fr; height: 1fr; }
    #tasks-detail-pane {
        width: 2fr; height: 1fr; border-left: solid $panel;
        padding: 1; overflow-y: auto;
    }
    #tasks-detail-pane:focus { border-left: solid $accent; }
    #tasks-detail { width: 100%; height: auto; }
    TasksScreen.compact-tasks #tasks-root { padding: 0 1; }
    TasksScreen.compact-tasks #tasks-capabilities { display: none; }
    #tasks-actions, #tasks-footer { height: auto; min-height: 3; }
    #tasks-actions Button { margin-right: 1; }
    #tasks-footer { dock: bottom; height: 3; align-horizontal: right; }
    #tasks-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("n", "create", "New task", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("x", "complete", "Complete", show=False),
        Binding("space", "toggle_selection", "Select", show=False),
        Binding("enter", "details", "Details", show=False),
    ]
    VIEWS = ("all", "open", "due", "today", "this_week", "overdue", "completed")

    def on_resize(self) -> None:
        self.set_class(self.size.height < 26, "compact-tasks")

    def __init__(
        self,
        backend: TasksScreenBackend,
        *,
        page_size: int = 100,
        view: str = "all",
        show_completed: bool = True,
        offline: bool = False,
        timezone: str = "UTC",
        creation_preferences: Callable[[], TaskCreationPreferences] = TaskCreationPreferences,
        display_preferences: Callable[[], TaskDisplayPreferences] | None = None,
        clock: Callable[[], datetime] | None = None,
        history_reader: Callable[..., TaskHistoryPage] | None = None,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            area_name="tasks",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if view not in self.VIEWS:
            raise ValueError(f"Unknown task view: {view}")
        self.backend = backend
        self.history_reader = history_reader
        self._history_page: TaskHistoryPage | None = None
        self.page_size = _bounded_limit(page_size)
        self._reset_paging()
        self.view = view
        self.show_completed = show_completed
        self.timezone = timezone
        self.creation_preferences = creation_preferences
        self.display_preferences = display_preferences or (
            lambda: TaskDisplayPreferences(
                default_view=TaskView(view), show_completed=show_completed, timezone=timezone
            )
        )
        self._display = self.display_preferences()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._view_today = self._display.today(self.clock())
        self._page_tasks: tuple[TaskInfo, ...] = ()
        self._complete_detail_reference: str | None = None
        self._tasks: tuple[TaskInfo, ...] = ()
        self._by_reference: dict[str, TaskInfo] = {}
        self._highlighted: str | None = None
        self._selected: set[str] = set()
        self._pending_notice: tuple[ViewState, str] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="tasks-root"):
            yield Label("Tasks", id="tasks-title")
            yield Static("Loading a bounded task page…", id="area-state", markup=False)
            with Horizontal(id="tasks-toolbar"):
                yield Input(placeholder="Search task subject or body", id="tasks-search")
                yield Select(
                    ((name.title(), name) for name in self.VIEWS),
                    value=self.view,
                    allow_blank=False,
                    id="tasks-view",
                )
                yield Button("Search", id="tasks-search-button")
                yield Button("Clear", id="tasks-clear")
            yield Static(
                _capability_summary(self.backend.capabilities),
                id="tasks-capabilities",
                markup=False,
            )
            with Horizontal(id="tasks-content"):
                yield DataTable(id="tasks-table", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="tasks-detail-pane"):
                    yield Static("Select a task to inspect it.", id="tasks-detail", markup=False)
            with Horizontal(id="tasks-actions"):
                yield Button("Create", id="tasks-create", variant="primary")
                yield Button("Edit", id="tasks-edit", disabled=True)
                yield Button("Complete", id="tasks-complete", disabled=True)
                yield Button("Reopen", id="tasks-reopen", disabled=True)
                yield Button("Delete", id="tasks-delete", disabled=True)
                yield Button("Delete options", id="tasks-delete-options", disabled=True)
                yield Button("Local history", id="tasks-history", disabled=True)
                yield Button("Export", id="tasks-export", disabled=True)
            yield Static("", id="area-progress", markup=False)
            with HorizontalScroll(id="tasks-footer"):
                retry = Button("Retry", id="area-retry")
                retry.display = False
                yield retry
                cancel = Button("Cancel operation", id="area-cancel-operation")
                cancel.display = False
                yield cancel
                older_history = Button("Older history", id="tasks-history-older")
                older_history.display = False
                yield older_history
                newest_history = Button("Newest history", id="tasks-history-newest")
                newest_history.display = False
                yield newest_history
                yield Button("Previous", id="tasks-prev", disabled=True)
                yield Button("Next", id="tasks-next", disabled=True)
                yield Button("Refresh", id="tasks-refresh")
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        table = self.query_one("#tasks-table", DataTable)
        self.on_resize()
        table.add_column("✓", key="selected", width=2)
        table.add_column("Status", key="status")
        table.add_column("Due", key="due")
        table.add_column("Subject", key="subject")
        table.add_column("Done", key="percent")
        table.add_column("Importance", key="importance")
        self._apply_capabilities()
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Tasks are unavailable offline. Reopen without --offline to connect.",
            )
            return
        self.call_after_refresh(self._initial_refresh)

    def _apply_capabilities(self) -> None:
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        self.query_one("#tasks-create", Button).disabled = not mutate
        self.query_one("#tasks-capabilities").display = bool(
            _capability_summary(self.backend.capabilities)
        )
        self._update_buttons()

    def action_refresh(self) -> None:
        self._reset_paging()
        self._load_page()

    def _load_page(self) -> None:
        if self.offline:
            self._set_view_state(
                ViewState.OFFLINE,
                "Tasks are unavailable offline. Reopen without --offline to connect.",
            )
            return
        try:
            self._display = self.display_preferences()
            self.show_completed = self._display.show_completed
            self.timezone = self._display.timezone
            self._view_today = self._display.today(self.clock())
        except ConfigurationError as exc:
            self._show_error("Task preferences", exc)
            return
        query = self.query_one("#tasks-search", Input).value.strip()
        offset = self._prepare_page(
            query, self.view, str(self.show_completed), self.timezone, self._view_today.isoformat()
        )
        page_loader = getattr(self.backend, "list_tasks_page", None)
        view = self.view
        display, today = self._display, self._view_today

        def load(cancel: Event, progress: ProgressReporter) -> object:
            if isinstance(self.backend, TasksServiceBackend):
                return self.backend.list_tasks_page(
                    query,
                    view,
                    self.page_size,
                    offset=offset,
                    cancel=cancel,
                    progress=progress,
                    preferences=display,
                    today=today,
                )
            if callable(page_loader):
                return page_loader(
                    query, view, self.page_size, offset=offset, cancel=cancel, progress=progress
                )
            return tuple(
                self.backend.list_tasks(
                    query, view, self.page_size, cancel=cancel, progress=progress
                )
            )

        self._begin_operation(
            "Loading tasks",
            load,
            self._tasks_loaded,
            retry_safe=True,
        )

    def _tasks_loaded(self, result: object) -> None:
        tasks = cast(tuple[TaskInfo, ...], result.items if isinstance(result, PimPage) else result)
        if not all(isinstance(item, TaskInfo) for item in tasks):
            raise PublicOperationError("The Tasks backend returned invalid records.")
        self._page_result(
            self._display.page(result, self._display.query(view=self.view, today=self._view_today))
            if isinstance(result, PimPage)
            else result
        )
        by_reference = {task.reference: task for task in tasks}
        if len(by_reference) != len(tasks):
            raise PublicOperationError("The Tasks backend returned duplicate references.")
        self._page_tasks = tasks
        self._complete_detail_reference = None
        self._by_reference = by_reference
        self._render_tasks()
        tasks = self._tasks
        if tasks:
            state, message = self._pending_notice or (
                ViewState.READY,
                f"{len(tasks)} tasks loaded · {self.view} · {self.timezone} · one bounded page.",
            )
        else:
            state, message = self._pending_notice or (
                ViewState.EMPTY,
                "No tasks match this bounded task view.",
            )
        if self._page_notice:
            message = (
                message + " " + self._page_summary(len(tasks))
                if self._pending_notice
                else self._page_summary(len(tasks))
            )
        reviewing_mutation = (
            self._pending_notice is not None and self._pending_notice[0] is ViewState.ERROR
        )
        if reviewing_mutation:
            message += (
                " Refresh for review reads the current list; it does not repeat the mutation."
            )
        self._pending_notice = None
        self._set_view_state(state, message)
        self.query_one("#area-retry", Button).label = (
            "Refresh for review" if reviewing_mutation else "Retry"
        )

    def refresh_display_preferences(self) -> None:
        """Re-sort/filter loaded candidates; refresh explicitly to fetch a new window."""
        self._display = self.display_preferences()
        self.show_completed = self._display.show_completed
        self.timezone = self._display.timezone
        self._view_today = self._display.today(self.clock())
        if self.is_mounted:
            self._render_tasks()
            self._set_view_state(
                ViewState.READY if self._tasks else ViewState.EMPTY,
                f"Task display updated · {self.timezone}. Refresh to update the candidate window.",
            )

    def _render_tasks(self) -> None:
        query = self._display.query(view=self.view, today=self._view_today)
        tasks = self._display.page(
            PimPage(tuple(self._by_reference[t.reference] for t in self._page_tasks), 0, 0), query
        ).items
        self._tasks = tasks
        visible = {task.reference for task in tasks}
        self._selected.intersection_update(visible)
        table = self.query_one("#tasks-table", DataTable)
        table.clear()
        for task in tasks:
            table.add_row(
                "●" if task.reference in self._selected else "",
                clean_cell(task.status.replace("_", " ").title(), max_length=30),
                task.due_date.isoformat() if task.due_date else "",
                clean_cell(task.subject or "(untitled)", max_length=100),
                f"{task.percent_complete}%",
                task.importance.value.title(),
                key=task.reference,
            )
        if tasks:
            reference = self._highlighted if self._highlighted in visible else tasks[0].reference
            self._select(reference)
            table.move_cursor(
                row=next(i for i, item in enumerate(tasks) if item.reference == reference)
            )
        else:
            self._highlighted = None
            self.query_one("#tasks-detail", Static).update("No tasks match this view.")
            self._update_buttons()

    @on(DataTable.RowHighlighted, "#tasks-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select(str(event.row_key.value))

    @on(DataTable.RowSelected, "#tasks-table")
    def _row_selected(self) -> None:
        self.action_details()

    def _select(self, reference: str) -> None:
        task = self._by_reference.get(reference)
        if task is None or not any(item.reference == reference for item in self._tasks):
            return
        changed = reference != self._highlighted
        self._highlighted = reference
        self._clear_history_view()
        self.query_one("#tasks-detail", Static).update(
            _render_task(task, complete=reference == self._complete_detail_reference)
        )
        if changed:
            self.query_one("#tasks-detail-pane", VerticalScroll).scroll_home(animate=False)
        self._update_buttons()

    @property
    def selected_task_reference(self) -> str | None:
        return self._highlighted

    def _targets(self) -> tuple[str, ...]:
        if self._selected:
            return tuple(task.reference for task in self._tasks if task.reference in self._selected)
        return (
            (self._highlighted,)
            if self._highlighted is not None and self._highlighted in self._by_reference
            else ()
        )

    def _update_buttons(self) -> None:
        targets = self._targets()
        self.query_one("#tasks-history", Button).disabled = (
            self._highlighted is None or self.history_reader is None
        )
        values = tuple(self._by_reference[reference] for reference in targets)
        mutate = not self.offline and _capability(self.backend.capabilities, "create")
        lifecycle = not self.offline and _capability(self.backend.capabilities, "complete")
        self.query_one("#tasks-edit", Button).disabled = len(targets) != 1 or not mutate
        self.query_one("#tasks-delete", Button).disabled = not targets or not mutate
        self.query_one("#tasks-delete-options", Button).disabled = (
            not targets or not mutate or not _capability(self.backend.capabilities, "delete_scope")
        )
        self.query_one("#tasks-complete", Button).disabled = (
            not any(task.status != TaskStatus.COMPLETED.value for task in values) or not lifecycle
        )
        self.query_one("#tasks-reopen", Button).disabled = (
            not any(task.status == TaskStatus.COMPLETED.value for task in values) or not lifecycle
        )
        self.query_one("#tasks-export", Button).disabled = not targets or not _capability(
            self.backend.capabilities, "export"
        )

    def action_toggle_selection(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        if reference in self._selected:
            self._selected.remove(reference)
        else:
            self._selected.add(reference)
        self.query_one("#tasks-table", DataTable).update_cell(
            reference,
            "selected",
            "●" if reference in self._selected else "",
        )
        self._update_buttons()
        self._set_view_state(
            ViewState.READY,
            f"{len(self._selected)} tasks selected for bounded batch actions.",
        )

    def _clear_history_view(self) -> None:
        self._history_page = None
        for identifier in ("#tasks-history-older", "#tasks-history-newest"):
            self.query_one(identifier, Button).display = False

    def action_history(self) -> None:
        self._load_history()

    def action_history_older(self) -> None:
        page = self._history_page
        if page is not None and page.next_before is not None:
            self._load_history(before=page.next_before)

    def _load_history(self, *, before: int | None = None) -> None:
        reference, reader = self._highlighted, self.history_reader
        if reference is None or reader is None:
            return
        self._begin_operation(
            "Loading local Task observations",
            lambda cancel, progress: reader(reference, limit=20, before=before),
            self._history_loaded,
            retry_safe=True,
        )

    def _history_loaded(self, result: object) -> None:
        if not isinstance(result, TaskHistoryPage):
            raise PublicOperationError("The local Task history returned an invalid page.")
        if result.requested_reference != self._highlighted:
            self._set_view_state(
                ViewState.READY, "History result discarded after selection changed."
            )
            return
        self._history_page = result
        self.query_one("#tasks-detail", Static).update(task_history_text(result))
        self.query_one("#tasks-detail-pane", VerticalScroll).scroll_home(animate=False)
        self.query_one("#tasks-history-older", Button).display = result.next_before is not None
        self.query_one("#tasks-history-newest", Button).display = True
        self._set_view_state(
            ViewState.READY, "Local observations loaded; unseen server history is unavailable."
        )

    def action_details(self) -> None:
        reference = self._highlighted
        if reference is None:
            return
        self._begin_operation(
            "Loading task details",
            lambda cancel, progress: self.backend.get_task(
                reference,
                cancel=cancel,
                progress=progress,
            ),
            self._detail_loaded,
            retry_safe=True,
        )

    def _detail_loaded(self, result: object) -> None:
        if not isinstance(result, TaskInfo):
            raise PublicOperationError("The Tasks backend returned invalid details.")
        self._clear_history_view()
        self._by_reference[result.reference] = result
        self._complete_detail_reference = result.reference
        self._highlighted = result.reference
        self.query_one("#tasks-detail", Static).update(_render_task(result, complete=True))
        self.query_one("#tasks-detail-pane", VerticalScroll).scroll_home(animate=False)
        self._set_view_state(ViewState.READY, "Complete task details loaded.")

    def action_create(self) -> None:
        if self.offline or not _capability(self.backend.capabilities, "create"):
            self._show_error("Create task", OfflineOperationError())
            return
        try:
            task = self.creation_preferences().new_task(
                timezone=self.display_preferences().timezone, now=self.clock()
            )
        except ConfigurationError as exc:
            self._show_error("Create task", exc)
            return
        self._open_submission_editor(TaskEditorScreen(task, creating=True), self._create_value)

    def configure_search(self, query: str) -> None:
        """Apply a palette query to the bounded Tasks search view."""

        search = self.query_one("#tasks-search", Input)
        search.value = query.strip()
        search.focus()
        self.action_refresh()

    def action_create_prefilled(self, subject: str, *, source_reference: str) -> None:
        """Open a real create form linked to one selected message reference."""

        if self.offline or not _capability(self.backend.capabilities, "create"):
            self._show_error("Create task from message", OfflineOperationError())
            return
        safe_subject = clean_cell(subject, max_length=180).strip() or "Selected message"
        safe_reference = clean_cell(source_reference, max_length=500).strip()
        if not safe_reference:
            self._set_view_state(ViewState.EMPTY, "Select a message before creating a task.")
            return
        try:
            seed = self.creation_preferences().new_task(
                reference=f"prefill-{uuid4().hex}",
                subject=f"Follow up: {safe_subject}",
                timezone=self.display_preferences().timezone,
                now=self.clock(),
            )
        except ConfigurationError as exc:
            self._show_error("Create task from message", exc)
            return
        task = replace(
            seed,
            body=f"Source message: {safe_reference}",
            categories=("From message",),
        )
        self._open_submission_editor(TaskEditorScreen(task, creating=True), self._create_value)

    def _create_value(self, value: TaskEditorValue | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Creating task",
            lambda cancel, progress: self.backend.create_task(
                value,
                cancel=cancel,
                progress=progress,
            ),
            self._mutation_loaded,
        )

    def action_edit(self, focus_field: str | None = None) -> None:
        targets = self._targets()
        if len(targets) != 1 or self.offline:
            return
        reference = targets[0]
        self._begin_operation(
            "Loading task for edit",
            lambda cancel, progress: self.backend.get_task(
                reference,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._open_editor(result, focus_field=focus_field),
            retry_safe=True,
        )

    def _open_editor(self, result: object, *, focus_field: str | None = None) -> None:
        if not isinstance(result, TaskInfo):
            raise PublicOperationError("The Tasks backend returned invalid details.")
        self._open_submission_editor(
            TaskEditorScreen(result, focus_field=focus_field),
            lambda value: self._edit_value(result.reference, value),
        )

    def _edit_value(self, reference: str, value: TaskEditorValue | None) -> None:
        if value is None:
            return
        self._begin_operation(
            "Updating task",
            lambda cancel, progress: self.backend.update_task(
                reference,
                value,
                cancel=cancel,
                progress=progress,
            ),
            self._mutation_loaded,
        )

    def _mutation_loaded(self, result: object) -> None:
        if not isinstance(result, TaskInfo):
            raise PublicOperationError("The Tasks backend returned an invalid task.")
        self._highlighted = result.reference
        self._pending_notice = (ViewState.READY, "Task mutation completed.")
        self.action_refresh()

    def action_complete(self) -> None:
        references = tuple(
            reference
            for reference in self._targets()
            if self._by_reference[reference].status != TaskStatus.COMPLETED.value
        )
        if not references or self.offline:
            return
        self._confirm(
            "Complete selected tasks?",
            f"Tasks: {len(references)}\nOnly non-completed tasks in this bounded selection change.",
            lambda: self._start_lifecycle(references, complete=True),
            confirm_label="Complete",
        )

    def action_reopen(self) -> None:
        references = tuple(
            reference
            for reference in self._targets()
            if self._by_reference[reference].status == TaskStatus.COMPLETED.value
        )
        if not references or self.offline:
            return
        self._confirm(
            "Reopen selected tasks?",
            f"Tasks: {len(references)}\nCompletion dates are cleared after confirmation.",
            lambda: self._start_lifecycle(references, complete=False),
            confirm_label="Reopen",
        )

    def _start_lifecycle(self, references: tuple[str, ...], *, complete: bool) -> None:
        action = "Completing tasks" if complete else "Reopening tasks"
        self._begin_operation(
            action,
            lambda cancel, progress: (
                self.backend.complete_tasks(
                    references,
                    cancel=cancel,
                    progress=progress,
                )
                if complete
                else self.backend.reopen_tasks(
                    references,
                    cancel=cancel,
                    progress=progress,
                )
            ),
            lambda result: self._batch_finished(result, noun="task"),
        )

    def action_delete(self) -> None:
        references = self._targets()
        if not references or self.offline:
            return
        self._confirm(
            "Delete selected tasks?",
            f"Tasks: {len(references)}\n\nThey are moved to Deleted Items after confirmation.",
            lambda: self._start_delete(references),
            confirm_label="Delete",
        )

    def action_delete_options(self) -> None:
        from gucmail.tui.task_delete_screen import TaskDeleteOptionsScreen

        references = self._targets()
        if (
            not references
            or self.offline
            or not _capability(self.backend.capabilities, "delete_scope")
            or not callable(getattr(self.backend, "delete_tasks_scoped", None))
        ):
            return
        self.app.push_screen(
            TaskDeleteOptionsScreen(references),
            lambda mode: self._scoped_delete_chosen(references, mode),
        )

    def _scoped_delete_chosen(
        self, references: tuple[str, ...], mode: TaskDeletionMode | None
    ) -> None:
        if mode is None:
            return
        scope = {
            TaskDeletionMode.MOVE: "Move stored tasks to Deleted Items with their recurrence",
            TaskDeletionMode.OCCURRENCE: "Permanently delete the specified occurrences",
            TaskDeletionMode.SERIES: "Permanently delete the active recurring series",
        }[mode]
        self._confirm(
            "Confirm task deletion scope?",
            f"Tasks: {len(references)}\nScope: {scope}.\n"
            + ("Permanent deletion leaves no Deleted Items copy.\n" if mode.hard_delete else "")
            + "\n".join(references),
            lambda: self._start_scoped_delete(references, mode),
            confirm_label="Delete permanently" if mode.hard_delete else "Move to Deleted Items",
        )

    def _start_scoped_delete(self, references: tuple[str, ...], mode: TaskDeletionMode) -> None:
        operation = getattr(self.backend, "delete_tasks_scoped", None)
        if not callable(operation):
            return
        self._begin_operation(
            "Deleting tasks with reviewed scope",
            lambda cancel, progress: operation(
                references,
                hard_delete=mode.hard_delete,
                delete_series=mode.delete_series,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="task"),
            retry_safe=False,
        )

    def _start_delete(self, references: tuple[str, ...]) -> None:
        self._begin_operation(
            "Deleting tasks",
            lambda cancel, progress: self.backend.delete_tasks(
                references,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._batch_finished(result, noun="task"),
        )

    def _batch_finished(self, result: object, *, noun: str) -> None:
        if not isinstance(result, BatchActionResult):
            raise PublicOperationError("The Tasks backend returned an invalid batch result.")
        self._selected.clear()
        self._pending_notice = _batch_message(result, noun=noun)
        self.action_refresh()

    def action_export(self) -> None:
        references = self._targets()
        if not references or not _capability(self.backend.capabilities, "export"):
            return
        self.app.push_screen(
            FilePathScreen("Export selected tasks", "New .json or .csv destination"),
            lambda path: self._export_path(references, path),
        )

    def _export_path(self, references: tuple[str, ...], path: str | None) -> None:
        if path is None:
            return
        self._begin_operation(
            "Exporting tasks",
            lambda cancel, progress: self.backend.export_tasks(
                references,
                path,
                cancel=cancel,
                progress=progress,
            ),
            self._export_finished,
        )

    def _export_finished(self, result: object) -> None:
        if not isinstance(result, ExportResult):
            raise PublicOperationError("The Tasks backend returned an invalid export result.")
        self._set_view_state(
            ViewState.READY,
            f"Exported {result.item_count} tasks to {clean_cell(result.path, max_length=300)}.",
        )

    @on(Input.Submitted, "#tasks-search")
    def _search_submitted(self) -> None:
        self.action_refresh()

    @on(Select.Changed, "#tasks-view")
    def _view_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK or str(event.value) == self.view:
            return
        self.view = str(event.value)
        self.action_refresh()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "tasks-search-button": self.action_refresh,
            "tasks-prev": self.action_previous_page,
            "tasks-next": self.action_next_page,
            "tasks-create": self.action_create,
            "tasks-edit": self.action_edit,
            "tasks-complete": self.action_complete,
            "tasks-reopen": self.action_reopen,
            "tasks-delete": self.action_delete,
            "tasks-delete-options": self.action_delete_options,
            "tasks-history": self.action_history,
            "tasks-history-older": self.action_history_older,
            "tasks-history-newest": self.action_history,
            "tasks-export": self.action_export,
            "tasks-refresh": self.action_refresh,
        }
        if event.button.id == "tasks-clear":
            self.query_one("#tasks-search", Input).value = ""
            self.view = "all"
            self.query_one("#tasks-view", Select).value = "all"
            self.action_refresh()
            return
        action = actions.get(event.button.id or "")
        if action is not None:
            action()


def _render_contact(
    contact: ContactInfo,
    *,
    complete: bool = False,
    preferences: ContactsDisplayPreferences | None = None,
) -> str:
    display = preferences or ContactsDisplayPreferences()
    lines = [
        clean_cell(display.label(contact), max_length=180),
        "Emails: "
        + (
            ", ".join(clean_cell(item.email, max_length=160) for item in contact.emails) or "(none)"
        ),
        "Phones: "
        + (
            ", ".join(
                f"{clean_cell(label, max_length=40)}={clean_cell(value, max_length=80)}"
                for label, value in contact.phones.items()
            )
            or "(none)"
        ),
        f"Company: {clean_cell(contact.company or '(none)', max_length=160)}",
        f"Department: {clean_cell(contact.department or '(none)', max_length=120)}",
        f"Job title: {clean_cell(contact.job_title or '(none)', max_length=120)}",
        "Categories: "
        + (", ".join(clean_cell(item, max_length=80) for item in contact.categories) or "(none)"),
    ]
    if complete:
        lines.extend(
            (
                f"Given name: {clean_cell(contact.given_name or '(none)', max_length=100)}",
                f"Stored name: {clean_cell(contact.display_name, max_length=180)}",
                f"Middle name: {clean_cell(contact.middle_name or '(none)', max_length=100)}",
                f"Family name: {clean_cell(contact.family_name or '(none)', max_length=100)}",
                f"Nickname: {clean_cell(contact.nickname or '(none)', max_length=100)}",
                f"Birthday: {contact.birthday.isoformat() if contact.birthday else '(none)'}",
                "Addresses: "
                + (
                    "; ".join(
                        f"{clean_cell(label, max_length=40)}={clean_cell(value, max_length=240)}"
                        for label, value in contact.addresses.items()
                    )
                    or "(none)"
                ),
                "Photo: "
                + (
                    f"available · {contact.photo_size or 'unknown'} bytes · "
                    f"{clean_cell(contact.photo_content_type or 'unknown type', max_length=80)}"
                    if contact.has_photo
                    else "(none)"
                ),
            )
        )
        if contact.notes:
            lines.extend(("", clean_cell(contact.notes, max_length=2_000)))
    return "\n".join(lines)


def _render_task(task: TaskInfo, *, complete: bool = False) -> str:
    lines = [
        clean_cell(task.subject or "(untitled task)", max_length=180),
        f"Status: {clean_cell(task.status.replace('_', ' ').title(), max_length=40)}",
        f"Complete: {task.percent_complete}%",
        f"Start: {task.start_date.isoformat() if task.start_date else '(none)'}",
        f"Due: {task.due_date.isoformat() if task.due_date else '(none)'}",
        f"Completed at: {task.completed_at.isoformat() if task.completed_at else '(none)'}",
        f"Importance: {task.importance.value.title()}",
        f"Reminder: {task.reminder_at.isoformat() if task.reminder_at else '(none)'}",
        "Categories: "
        + (", ".join(clean_cell(item, max_length=80) for item in task.categories) or "(none)"),
        f"Recurrence: {'configured' if task.recurrence else 'none'}",
    ]
    if complete and task.body:
        lines.extend(("", clean_cell(task.body, max_length=2_000)))
    return "\n".join(lines)


__all__ = [
    "BatchActionResult",
    "CalendarAvailabilityRequest",
    "CalendarAvailabilityScreen",
    "CalendarEditorScreen",
    "CalendarEditorValue",
    "CalendarScreen",
    "CalendarScreenBackend",
    "CalendarServiceBackend",
    "CapabilityState",
    "ContactEditorScreen",
    "ContactEditorValue",
    "ContactMergePreviewView",
    "ContactsScreen",
    "ContactsScreenBackend",
    "ContactsServiceBackend",
    "CorrespondentsScreen",
    "ExportResult",
    "FilePathScreen",
    "MeetingResponseScreen",
    "MeetingResponseValue",
    "PimActionRequested",
    "PimCapability",
    "TaskEditorScreen",
    "TaskEditorValue",
    "TasksScreen",
    "TasksScreenBackend",
    "TasksServiceBackend",
    "capability_map",
]
