"""Shared pieces of the Calendar, Contacts and Tasks screens."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import ClassVar, Protocol, TypeVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import DescendantFocus, Resize
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from gucmail.models import (
    Address,
    CalendarEvent,
    CalendarResponse,
    ContactInfo,
    Importance,
    TaskInfo,
)
from gucmail.services.calendar import (
    CalendarViewKind,
    RecurrenceScope,
)
from gucmail.services.calendar_history import CalendarSeriesHistory
from gucmail.services.calendar_paging import CalendarCursor
from gucmail.services.contacts import (
    CorrespondenceEvent,
    CorrespondentSummary,
)
from gucmail.services.pim import PimPage
from gucmail.services.tasks import TaskStatus
from gucmail.tui.area_common import (
    ProgressReporter,
    ViewState,
    clean_cell,
)
from gucmail.tui.literal_select import LiteralSelect as Select
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
    # Unavailable actions show as disabled buttons. Repeating them here is noise.
    return ""


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
        noun = self.area_name if count != 1 else self.area_name.rstrip("s")
        return f"{count} {noun}" + (
            " · more on the next page" if self._next_page_offset is not None else ""
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
