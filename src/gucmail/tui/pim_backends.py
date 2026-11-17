"""Service backends behind the Calendar, Contacts and Tasks screens."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event
from typing import cast
from uuid import uuid4

from gucmail.errors import ConfigurationError, GUCmailError
from gucmail.models import (
    Address,
    CalendarEvent,
    CalendarResponse,
    ContactInfo,
    TaskInfo,
)
from gucmail.services.calendar import (
    UNSET as CALENDAR_UNSET,
)
from gucmail.services.calendar import (
    CalendarPatch,
    CalendarService,
    CalendarView,
    RecurrenceScope,
    localize_datetime,
)
from gucmail.services.calendar_display import (
    load_calendar_view,
)
from gucmail.services.calendar_history import CalendarSeriesHistory
from gucmail.services.contacts import (
    ContactMergePlan,
    ContactPatch,
    ContactsService,
    CorrespondenceEvent,
    CorrespondentSummary,
)
from gucmail.services.pim import MAX_PIM_PAGE_SIZE, PimPage
from gucmail.services.recurrence import normalize_recurrence_mapping
from gucmail.services.tasks import TaskPatch, TaskService, TaskStatus
from gucmail.services.tasks_display import TaskDisplayPreferences
from gucmail.tui.area_common import (
    OperationProgress,
    ProgressReporter,
    PublicOperationError,
    clean_cell,
)
from gucmail.tui.pim_common import (
    MAX_IMPORT_BYTES,
    BatchActionResult,
    CalendarEditorValue,
    CalendarListRequest,
    CapabilityState,
    ContactEditorValue,
    ContactMergePreviewView,
    ExportResult,
    PimCapability,
    TaskEditorValue,
    capability_map,
)


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
                else "no correspondence-history gateway is configured",
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
        progress(_progress("Searching the GUC directory"))
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
        progress(_progress("Loading correspondence history"))
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
        progress(_progress("Loading contact context"))
        known = self.service.list(limit=bounded)
        _check_cancel(cancel)
        progress(_progress("Summarizing correspondence history"))
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


def _optional(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None
