"""Operational Scheduled Mail center for the Textual interface.

The screen adapts :class:`~gucmail.services.scheduler.SchedulerService` through a
small presentation protocol.  Local queue management remains usable offline;
operations which can cross the Exchange send boundary are visibly disabled and
also rejected by the backend while offline.  Every blocking operation runs in an
``OperationalScreen`` worker and every cancellation or send requires a scoped
confirmation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event
from typing import ClassVar, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from gucmail.models import Address, Importance, SendState
from gucmail.services.recurring import RecurringService
from gucmail.services.scheduler import (
    JobExecution,
    ScheduledJobRecord,
    SchedulerRun,
    SchedulerService,
)
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
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


@dataclass(frozen=True, slots=True)
class ScheduledJobView:
    """Presentation-safe local snapshot of one durable scheduled message."""

    reference: str
    draft_reference: str
    scheduled_for: str
    timezone: str
    original_expression: str
    state: str
    subject: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    body: str
    importance: str
    attachment_count: int
    attempts: int
    next_attempt_at: str | None
    last_error: str | None
    require_final_confirmation: bool
    recurrence_id: str | None
    outcome_reference: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class SchedulerStatusView:
    paused: bool
    counts: Mapping[str, int]
    next_scheduled: str | None
    manual_review: int


@dataclass(frozen=True, slots=True)
class ScheduledEditValue:
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    importance: str
    require_final_confirmation: bool


@dataclass(frozen=True, slots=True)
class ScheduledOperationView:
    reference: str
    state: str
    detail: str


class ScheduledScreenBackend(Protocol):
    """Blocking operations consumed by :class:`ScheduledScreen`."""

    online: bool

    def list_jobs(
        self,
        *,
        states: tuple[str, ...],
        query: str,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
        window: str = "",
    ) -> tuple[SchedulerStatusView, Sequence[ScheduledJobView]]: ...

    def edit_job(
        self,
        reference: str,
        value: ScheduledEditValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledJobView: ...

    def reschedule(
        self,
        reference: str,
        expression: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledJobView: ...

    def duplicate(
        self,
        reference: str,
        expression: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledJobView: ...

    def cancel_job(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView: ...

    def undo_send(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView: ...

    def send_now(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView: ...

    def run_due(
        self,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView: ...

    def set_paused(
        self,
        paused: bool,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SchedulerStatusView: ...

    def set_recurrence_paused(
        self,
        reference: str,
        paused: bool,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView: ...

    def history(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[Mapping[str, object]]: ...


def _addresses(values: Sequence[Address]) -> tuple[str, ...]:
    return tuple(value.email for value in values)


class SchedulerServiceBackend:
    """Presentation adapter over the durable scheduler service."""

    def __init__(
        self,
        service: SchedulerService,
        *,
        online: bool,
        recurring: RecurringService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self.online = online
        self.recurring = recurring
        self.clock = clock

    @staticmethod
    def _check_cancel(cancel: Event) -> None:
        if cancel.is_set():
            raise PublicOperationError("The scheduled-mail operation was cancelled safely.")

    def _view(self, record: ScheduledJobRecord) -> ScheduledJobView:
        draft = self.service.drafts.get(record.message.draft_reference)
        message = record.message
        try:
            zone = ZoneInfo(message.timezone)
            timezone_label = message.timezone
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("UTC")
            timezone_label = "UTC (saved timezone unavailable)"
        return ScheduledJobView(
            reference=message.reference,
            draft_reference=message.draft_reference,
            scheduled_for=message.scheduled_for.astimezone(zone).isoformat(),
            timezone=timezone_label,
            original_expression=message.original_expression,
            state=message.state.value,
            subject=draft.subject,
            to=_addresses(draft.to),
            cc=_addresses(draft.cc),
            bcc=_addresses(draft.bcc),
            body=draft.body_text,
            importance=draft.importance.value,
            attachment_count=len(draft.attachment_paths),
            attempts=message.attempts,
            next_attempt_at=(
                message.next_attempt_at.astimezone(zone).isoformat()
                if message.next_attempt_at
                else None
            ),
            last_error=message.last_error,
            require_final_confirmation=message.require_final_confirmation,
            recurrence_id=message.recurrence_id,
            outcome_reference=message.outcome_reference,
            updated_at=message.updated_at.isoformat(),
        )

    def _status(self) -> SchedulerStatusView:
        status = self.service.status()
        raw_counts = status.get("counts")
        counts = (
            {str(key): int(value) for key, value in raw_counts.items()}
            if isinstance(raw_counts, Mapping)
            else {}
        )
        return SchedulerStatusView(
            paused=bool(status.get("paused")),
            counts=counts,
            next_scheduled=(
                str(status["next_scheduled"]) if status.get("next_scheduled") else None
            ),
            manual_review=int(status.get("manual_review") or 0),
        )

    def list_jobs(
        self,
        *,
        states: tuple[str, ...],
        query: str,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
        window: str = "",
    ) -> tuple[SchedulerStatusView, Sequence[ScheduledJobView]]:
        self._check_cancel(cancel)
        normalized_window = window.strip().casefold()
        if normalized_window not in {"", "next", "today", "this_week"}:
            raise PublicOperationError("Unknown scheduled-message time window.", retryable=False)
        parsed_states = tuple(SendState(value) for value in states)
        progress(OperationProgress("Loading durable queue", 0, limit))
        try:
            zone = ZoneInfo(self.service.timezone)
        except ZoneInfoNotFoundError as exc:
            raise PublicOperationError(
                "The configured scheduler timezone is unavailable.", retryable=False
            ) from exc
        now = self.clock() if self.clock is not None else datetime.now(zone)
        now = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)
        before: datetime | None = None
        after: datetime | None = None
        effective_states = parsed_states
        effective_limit = limit
        if normalized_window == "next":
            after = now
            effective_limit = 1
            if not effective_states:
                terminal = {
                    SendState.SENT,
                    SendState.CANCELLED,
                    SendState.UNCERTAIN,
                    SendState.MANUAL_REVIEW,
                }
                effective_states = tuple(state for state in SendState if state not in terminal)
        elif normalized_window == "today":
            after = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
                microseconds=1
            )
            before = after + timedelta(days=1, microseconds=1)
        elif normalized_window == "this_week":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
                days=now.weekday()
            )
            after = start - timedelta(microseconds=1)
            before = start + timedelta(days=7)
        records = self.service.list(
            states=effective_states or None,
            before=before,
            after=after,
            limit=effective_limit,
        )
        needle = query.casefold().strip()
        views: list[ScheduledJobView] = []
        for index, record in enumerate(records, start=1):
            self._check_cancel(cancel)
            view = self._view(record)
            haystack = " ".join(
                (
                    view.reference,
                    view.draft_reference,
                    view.subject,
                    *view.to,
                    view.state,
                    view.original_expression,
                )
            ).casefold()
            if not needle or needle in haystack:
                views.append(view)
            progress(OperationProgress("Loading durable queue", index, len(records)))
        return self._status(), tuple(views)

    def edit_job(
        self,
        reference: str,
        value: ScheduledEditValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledJobView:
        self._check_cancel(cancel)
        progress(OperationProgress("Updating local draft snapshot"))
        try:
            importance = Importance(value.importance)
        except ValueError as exc:
            raise PublicOperationError(
                "Choose Normal, High, or Low importance.", retryable=False
            ) from exc
        changes: dict[str, object] = {
            "to": [Address(email=item) for item in value.to],
            "cc": [Address(email=item) for item in value.cc],
            "bcc": [Address(email=item) for item in value.bcc],
            "subject": value.subject,
            "body_text": value.body,
            "importance": importance,
        }
        self.service.edit(
            reference,
            draft_changes=changes,
            require_final_confirmation=value.require_final_confirmation,
        )
        return self._view(self.service.get(reference))

    def reschedule(
        self,
        reference: str,
        expression: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledJobView:
        self._check_cancel(cancel)
        if not expression.strip():
            raise PublicOperationError(
                "Enter a future date, time, or delay before rescheduling.", retryable=False
            )
        progress(OperationProgress("Resolving future send time"))
        self.service.reschedule(reference, expression.strip())
        return self._view(self.service.get(reference))

    def duplicate(
        self,
        reference: str,
        expression: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledJobView:
        self._check_cancel(cancel)
        if not expression.strip():
            raise PublicOperationError(
                "Enter a new future time for the duplicate.", retryable=False
            )
        progress(OperationProgress("Duplicating local draft and schedule"))
        duplicated = self.service.duplicate(reference, expression=expression.strip())
        return self._view(self.service.get(duplicated.reference))

    def cancel_job(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView:
        self._check_cancel(cancel)
        progress(OperationProgress("Cancelling local schedule"))
        result = self.service.cancel(reference)
        return ScheduledOperationView(
            result.reference, result.state.value, "Schedule cancelled locally."
        )

    def undo_send(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView:
        self._check_cancel(cancel)
        progress(OperationProgress("Cancelling Undo Send delay"))
        result = self.service.undo(reference)
        return ScheduledOperationView(
            result.reference, result.state.value, "Send undone before Exchange acceptance."
        )

    def send_now(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView:
        if not self.online:
            raise OfflineOperationError(
                "Connect to Exchange before sending a scheduled message now."
            )
        self._check_cancel(cancel)
        progress(OperationProgress("Sending one confirmed scheduled message"))
        execution: JobExecution = self.service.send_now(reference, confirmed=True)
        return ScheduledOperationView(
            execution.reference, execution.state.value, execution.detail or execution.action.value
        )

    def run_due(
        self,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView:
        if not self.online:
            raise OfflineOperationError(
                "Connect to Exchange before running due scheduled messages."
            )
        self._check_cancel(cancel)
        progress(OperationProgress("Running bounded due queue", 0, limit))
        run: SchedulerRun = self.service.run_once(limit=limit)
        detail = (
            f"Claimed {run.claimed}; sent {run.sent}; manual review {run.needs_review}; "
            f"stale leases recovered {run.stale_recovered}."
        )
        return ScheduledOperationView("scheduler", "paused" if run.paused else "completed", detail)

    def set_paused(
        self,
        paused: bool,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SchedulerStatusView:
        self._check_cancel(cancel)
        progress(OperationProgress("Pausing scheduler" if paused else "Resuming scheduler"))
        (self.service.pause if paused else self.service.resume)()
        return self._status()

    def set_recurrence_paused(
        self,
        reference: str,
        paused: bool,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ScheduledOperationView:
        self._check_cancel(cancel)
        if self.recurring is None:
            raise PublicOperationError(
                "Recurring-rule management is unavailable in this screen.",
                retryable=False,
            )
        job = self.service.get(reference).message
        if not job.recurrence_id:
            raise PublicOperationError(
                "The selected scheduled message is not linked to a recurring rule.",
                retryable=False,
            )
        progress(
            OperationProgress(
                "Pausing recurring message" if paused else "Resuming recurring message"
            )
        )
        record = (
            self.recurring.pause(job.recurrence_id)
            if paused
            else self.recurring.resume(job.recurrence_id)
        )
        return ScheduledOperationView(
            record.reference,
            record.state.value,
            f"Recurring rule {record.reference} is now {record.state.value}.",
        )

    def history(
        self,
        reference: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[Mapping[str, object]]:
        self._check_cancel(cancel)
        progress(OperationProgress("Loading append-only schedule history"))
        return tuple(self.service.history(reference))


class ScheduledEditScreen(ModalScreen[ScheduledEditValue | None]):
    """Edit the local draft snapshot used by a scheduled job."""

    DEFAULT_CSS = """
    ScheduledEditScreen { align: center middle; }
    #scheduled-edit-dialog {
        width: min(100, 94%); height: min(38, 94%); border: thick $accent;
        background: $surface; padding: 1 2;
    }
    #scheduled-edit-title { height: 2; text-style: bold; }
    .scheduled-edit-row { height: 3; }
    .scheduled-edit-label { width: 10; }
    #scheduled-edit-body { height: 1fr; min-height: 4; }
    #scheduled-edit-footer { height: 3; align-horizontal: right; }
    #scheduled-edit-footer Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, job: ScheduledJobView) -> None:
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        with Vertical(id="scheduled-edit-dialog"):
            yield Label("Edit scheduled draft", id="scheduled-edit-title")
            for label, widget_id, value in (
                ("To", "scheduled-edit-to", ", ".join(self.job.to)),
                ("Cc", "scheduled-edit-cc", ", ".join(self.job.cc)),
                ("Bcc", "scheduled-edit-bcc", ", ".join(self.job.bcc)),
                ("Subject", "scheduled-edit-subject", self.job.subject),
            ):
                with Horizontal(classes="scheduled-edit-row"):
                    yield Label(label, classes="scheduled-edit-label", markup=False)
                    yield Input(value=value, id=widget_id)
            yield TextArea(self.job.body, id="scheduled-edit-body")
            with Horizontal(classes="scheduled-edit-row"):
                yield Label("Importance", classes="scheduled-edit-label")
                yield Select(
                    (("Normal", "normal"), ("High", "high"), ("Low", "low")),
                    value=self.job.importance,
                    id="scheduled-edit-importance",
                )
                yield Checkbox(
                    "Confirm again when due",
                    value=self.job.require_final_confirmation,
                    id="scheduled-edit-confirm",
                )
            with Horizontal(id="scheduled-edit-footer"):
                yield Button("Cancel", id="scheduled-edit-cancel")
                yield Button("Save local snapshot", id="scheduled-edit-save", variant="primary")

    @staticmethod
    def _split(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.replace(";", ",").split(",") if item.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "scheduled-edit-cancel":
            self.dismiss(None)
            return
        if event.button.id != "scheduled-edit-save":
            return
        importance = self.query_one("#scheduled-edit-importance", Select).value
        self.dismiss(
            ScheduledEditValue(
                to=self._split(self.query_one("#scheduled-edit-to", Input).value),
                cc=self._split(self.query_one("#scheduled-edit-cc", Input).value),
                bcc=self._split(self.query_one("#scheduled-edit-bcc", Input).value),
                subject=self.query_one("#scheduled-edit-subject", Input).value,
                body=self.query_one("#scheduled-edit-body", TextArea).text,
                importance=str(importance or "normal"),
                require_final_confirmation=self.query_one(
                    "#scheduled-edit-confirm", Checkbox
                ).value,
            )
        )


class ScheduleHistoryScreen(ModalScreen[None]):
    """Bounded, append-only audit history for one scheduled job."""

    DEFAULT_CSS = """
    ScheduleHistoryScreen { align: center middle; }
    #schedule-history-dialog {
        width: min(100, 94%); height: min(34, 90%); border: thick $accent;
        background: $surface; padding: 1 2;
    }
    #schedule-history-title { height: 2; text-style: bold; }
    #schedule-history-scroll { height: 1fr; }
    #schedule-history-close { dock: bottom; width: 16; align-horizontal: right; }
    """

    def __init__(self, reference: str, rows: Sequence[Mapping[str, object]]) -> None:
        super().__init__()
        self.reference = clean_cell(reference, max_length=160)
        self.rows = tuple(rows[-200:])

    def compose(self) -> ComposeResult:
        with Vertical(id="schedule-history-dialog"):
            yield Label(
                f"Schedule history · {self.reference}", id="schedule-history-title", markup=False
            )
            with VerticalScroll(id="schedule-history-scroll"):
                lines = []
                for row in self.rows:
                    lines.append(
                        f"{clean_cell(row.get('occurred_at'), max_length=40)}  "
                        f"{clean_cell(row.get('event'), max_length=50)}\n"
                        f"  {clean_cell(row.get('detail'), max_length=500)}"
                    )
                yield Static("\n\n".join(lines) or "No history entries.", markup=False)
            yield Button("Close", id="schedule-history-close", variant="primary")

    @on(Button.Pressed, "#schedule-history-close")
    def _close(self) -> None:
        self.dismiss(None)


class ScheduledScreen(OperationalScreen):
    """Manage the durable local scheduler without leaving the TUI."""

    DEFAULT_CSS = """
    ScheduledScreen { layout: vertical; }
    #scheduled-root { width: 100%; height: 100%; padding: 1 2; overflow-y: auto; }
    #scheduled-title { height: 2; text-style: bold; }
    #scheduled-toolbar { height: 3; }
    #scheduled-filter { width: 2fr; margin-right: 1; }
    #scheduled-state, #scheduled-window { width: 1fr; margin-right: 1; }
    #scheduled-time { width: 2fr; }
    #scheduled-summary { height: auto; min-height: 2; }
    #scheduled-table { height: 1fr; min-height: 7; }
    #scheduled-detail {
        height: auto; min-height: 3; max-height: 9; border-top: solid $panel; padding: 1;
    }
    #scheduled-actions, #scheduled-footer { height: auto; min-height: 3; }
    #scheduled-actions Button { margin-right: 1; }
    #scheduled-footer { align-horizontal: right; }
    #scheduled-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("e", "edit", "Edit", show=False),
        Binding("s", "reschedule", "Reschedule", show=False),
        Binding("d", "duplicate", "Duplicate", show=False),
        Binding("h", "history", "History", show=False),
    ]

    def __init__(
        self,
        backend: ScheduledScreenBackend,
        *,
        page_size: int = 200,
        offline: bool = False,
    ) -> None:
        super().__init__(area_name="scheduled", offline=offline)
        self.backend = backend
        self.page_size = max(1, min(page_size, 1_000))
        self._jobs: dict[str, ScheduledJobView] = {}
        self._highlighted: str | None = None
        self._status: SchedulerStatusView | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="scheduled-root"):
            yield Label("Scheduled Mail", id="scheduled-title")
            with Horizontal(id="scheduled-toolbar"):
                yield Input(
                    placeholder="Filter subject, recipient, state, or reference",
                    id="scheduled-filter",
                )
                yield Select(
                    (
                        ("All states", ""),
                        ("Upcoming", "scheduled"),
                        ("Undo Send", "undo_delay"),
                        ("Failed", "failed"),
                        ("Uncertain", "send_state_uncertain"),
                        ("Manual review", "manual_review_required"),
                        ("Sent", "sent"),
                        ("Cancelled", "cancelled"),
                    ),
                    value="",
                    id="scheduled-state",
                )
                yield Select(
                    (
                        ("All times", ""),
                        ("Next", "next"),
                        ("Today", "today"),
                        ("This week", "this_week"),
                    ),
                    value="",
                    id="scheduled-window",
                )
                yield Input(
                    placeholder='New time, e.g. "tomorrow 09:00" or "in 2 hours"',
                    id="scheduled-time",
                )
            yield Static("Loading scheduler status…", id="scheduled-summary", markup=False)
            yield Static("", id="area-state", markup=False)
            yield DataTable(id="scheduled-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "Select a scheduled message for details.", id="scheduled-detail", markup=False
            )
            yield Static("", id="area-progress", markup=False)
            # Scroll horizontally on narrow terminals so every action (notably Undo Send)
            # stays reachable instead of being clipped past the right edge.
            with HorizontalScroll(id="scheduled-actions"):
                yield Button("Edit draft", id="scheduled-edit")
                yield Button("Reschedule", id="scheduled-reschedule")
                yield Button("Duplicate", id="scheduled-duplicate")
                yield Button("History", id="scheduled-history")
                yield Button("Undo Send", id="scheduled-undo", variant="warning")
                yield Button("Cancel schedule", id="scheduled-cancel", variant="warning")
                yield Button("Send now", id="scheduled-send-now", variant="error")
            with HorizontalScroll(id="scheduled-footer"):
                yield Button("Run due (max 20)", id="scheduled-run-due", variant="warning")
                yield Button("Pause", id="scheduled-pause")
                yield Button("Refresh", id="scheduled-refresh")
                yield Button("Close", classes="area-close", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#scheduled-table", DataTable)
        table.add_columns("State", "When", "Subject", "To", "Attempts", "Reference")
        self.query_one("#scheduled-send-now", Button).disabled = (
            self.offline or not self.backend.online
        )
        self.query_one("#scheduled-run-due", Button).disabled = (
            self.offline or not self.backend.online
        )
        self.call_after_refresh(self.action_refresh)

    def _filters(self) -> tuple[tuple[str, ...], str, str]:
        state_value = self.query_one("#scheduled-state", Select).value
        states = (str(state_value),) if state_value else ()
        query = self.query_one("#scheduled-filter", Input).value.strip()
        window = str(self.query_one("#scheduled-window", Select).value or "")
        return states, query, window

    def action_refresh(self) -> None:
        states, query, window = self._filters()
        self._begin_operation(
            "Loading scheduled messages",
            lambda cancel, progress: self.backend.list_jobs(
                states=states,
                query=query,
                limit=self.page_size,
                cancel=cancel,
                progress=progress,
                window=window,
            ),
            self._loaded,
            retry_safe=True,
        )

    def configure_view(self, *, state: str = "", window: str = "") -> None:
        """Apply one palette-provided state/time view using visible controls."""

        allowed_states = {
            "",
            "scheduled",
            "undo_delay",
            "failed",
            "send_state_uncertain",
            "manual_review_required",
            "sent",
            "cancelled",
        }
        allowed_windows = {"", "next", "today", "this_week"}
        if state not in allowed_states or window not in allowed_windows:
            raise ValueError("Unsupported scheduled-message view.")
        self.query_one("#scheduled-state", Select).value = state
        self.query_one("#scheduled-window", Select).value = window
        self.action_refresh()

    def _loaded(self, result: object) -> None:
        status, jobs = cast(tuple[SchedulerStatusView, Sequence[ScheduledJobView]], result)
        previous = self._highlighted
        self._status = status
        self._jobs = {job.reference: job for job in jobs}
        table = self.query_one("#scheduled-table", DataTable)
        table.clear()
        for job in jobs:
            table.add_row(
                clean_cell(job.state, max_length=30),
                clean_cell(job.scheduled_for, max_length=40),
                clean_cell(job.subject or "(no subject)", max_length=90),
                clean_cell(", ".join(job.to), max_length=80),
                str(job.attempts),
                clean_cell(job.reference, max_length=70),
                key=job.reference,
            )
        summary = (
            ("Paused" if status.paused else "Running")
            + f" · {len(jobs)} visible / {sum(status.counts.values())} total"
            + f" · manual review {status.manual_review}"
            + (
                f" · next {status.next_scheduled}"
                if status.next_scheduled
                else " · no upcoming job"
            )
        )
        self.query_one("#scheduled-summary", Static).update(clean_cell(summary, max_length=500))
        pause = self.query_one("#scheduled-pause", Button)
        pause.label = "Resume" if status.paused else "Pause"
        pause.variant = "success" if status.paused else "default"
        if jobs:
            self._set_view_state(ViewState.READY, f"Loaded {len(jobs)} bounded scheduled messages.")
            target = previous if previous in self._jobs else jobs[0].reference
            row = next(index for index, item in enumerate(jobs) if item.reference == target)
            table.move_cursor(row=row)
            self._select(target)
        else:
            self._highlighted = None
            self._set_view_state(ViewState.EMPTY, "No scheduled messages match this bounded view.")
            self.query_one("#scheduled-detail", Static).update("No scheduled message selected.")
        self._update_actions()

    @on(DataTable.RowHighlighted, "#scheduled-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select(str(event.row_key.value))

    def _select(self, reference: str) -> None:
        job = self._jobs.get(reference)
        if job is None:
            return
        self._highlighted = reference
        recipients = ", ".join(job.to) or "(none)"
        details = [
            clean_cell(job.subject, max_length=240) or "(no subject)",
            f"To: {clean_cell(recipients, max_length=240)}",
            f"When: {clean_cell(job.scheduled_for)} ({clean_cell(job.timezone)})",
            f"State: {clean_cell(job.state)} · Attempts: {job.attempts} · "
            f"Attachments: {job.attachment_count}",
            f"Expression: {clean_cell(job.original_expression, max_length=240)}",
            f"Draft: {clean_cell(job.draft_reference, max_length=160)}",
        ]
        if job.recurrence_id:
            details.append(f"Recurrence: {clean_cell(job.recurrence_id, max_length=160)}")
        if job.next_attempt_at:
            details.append(f"Next attempt: {clean_cell(job.next_attempt_at)}")
        if job.last_error:
            details.append(f"Last error: {clean_cell(job.last_error, max_length=300)}")
        if job.require_final_confirmation:
            details.append("Final confirmation is required before sending.")
        if job.outcome_reference:
            details.append(f"Outcome: {clean_cell(job.outcome_reference, max_length=160)}")
        self.query_one("#scheduled-detail", Static).update("\n".join(details))
        self._update_actions()

    def _selected(self) -> ScheduledJobView | None:
        return self._jobs.get(self._highlighted or "")

    def _update_actions(self) -> None:
        job = self._selected()
        editable = bool(
            job
            and job.state
            in {
                "scheduled",
                "undo_delay",
                "due",
                "waiting_connectivity",
                "waiting_authentication",
                "retry_scheduled",
                "failed",
            }
        )
        for selector in (
            "#scheduled-edit",
            "#scheduled-reschedule",
            "#scheduled-duplicate",
            "#scheduled-cancel",
        ):
            self.query_one(selector, Button).disabled = not editable
        self.query_one("#scheduled-history", Button).disabled = job is None
        self.query_one("#scheduled-undo", Button).disabled = not (job and job.state == "undo_delay")
        self.query_one("#scheduled-send-now", Button).disabled = not (
            editable and self.backend.online and not self.offline
        )

    @on(Input.Submitted, "#scheduled-filter")
    @on(Select.Changed, "#scheduled-state")
    @on(Select.Changed, "#scheduled-window")
    def _filter_changed(self) -> None:
        self.action_refresh()

    def action_edit(self) -> None:
        job = self._selected()
        if job is None:
            return

        def resolved(value: ScheduledEditValue | None) -> None:
            if value is None:
                return
            self._begin_operation(
                "Updating scheduled draft",
                lambda cancel, progress: self.backend.edit_job(
                    job.reference, value, cancel=cancel, progress=progress
                ),
                lambda _result: self.action_refresh(),
            )

        self.app.push_screen(ScheduledEditScreen(job), resolved)

    def action_reschedule(self) -> None:
        job = self._selected()
        if job is None:
            return
        expression = self.query_one("#scheduled-time", Input).value.strip()
        if not expression:
            self.query_one("#scheduled-time", Input).focus()
            self._set_view_state(ViewState.READY, "Enter a new future time before rescheduling.")
            return
        self._begin_operation(
            "Rescheduling message",
            lambda cancel, progress: self.backend.reschedule(
                job.reference, expression, cancel=cancel, progress=progress
            ),
            lambda _result: self.action_refresh(),
        )

    def action_duplicate(self) -> None:
        job = self._selected()
        if job is None:
            return
        expression = self.query_one("#scheduled-time", Input).value.strip()
        if not expression:
            self.query_one("#scheduled-time", Input).focus()
            self._set_view_state(
                ViewState.READY,
                "Enter a future time for the duplicated scheduled message.",
            )
            return
        self._begin_operation(
            "Duplicating scheduled message",
            lambda cancel, progress: self.backend.duplicate(
                job.reference, expression, cancel=cancel, progress=progress
            ),
            lambda _result: self.action_refresh(),
        )

    def action_history(self) -> None:
        job = self._selected()
        if job is None:
            return

        def show_history(result: object) -> None:
            self.app.push_screen(
                ScheduleHistoryScreen(
                    job.reference,
                    cast(Sequence[Mapping[str, object]], result),
                )
            )

        self._begin_operation(
            "Loading schedule history",
            lambda cancel, progress: self.backend.history(
                job.reference, cancel=cancel, progress=progress
            ),
            show_history,
            retry_safe=True,
        )

    def _cancel_selected(self) -> None:
        job = self._selected()
        if job is None:
            return
        self._begin_operation(
            "Cancelling scheduled message",
            lambda cancel, progress: self.backend.cancel_job(
                job.reference, cancel=cancel, progress=progress
            ),
            lambda _result: self.action_refresh(),
        )

    def _undo_selected(self) -> None:
        job = self._selected()
        if job is None:
            return
        self._begin_operation(
            "Undoing delayed send",
            lambda cancel, progress: self.backend.undo_send(
                job.reference, cancel=cancel, progress=progress
            ),
            lambda _result: self.action_refresh(),
        )

    def _send_selected(self) -> None:
        job = self._selected()
        if job is None:
            return
        self._begin_operation(
            "Sending scheduled message now",
            lambda cancel, progress: self.backend.send_now(
                job.reference, cancel=cancel, progress=progress
            ),
            lambda _result: self.action_refresh(),
            timeout_seconds=60.0,
        )

    def _run_due(self) -> None:
        self._begin_operation(
            "Running bounded due queue",
            lambda cancel, progress: self.backend.run_due(
                limit=20, cancel=cancel, progress=progress
            ),
            lambda result: self._operation_complete(cast(ScheduledOperationView, result)),
            timeout_seconds=120.0,
        )

    def _operation_complete(self, result: ScheduledOperationView) -> None:
        self._set_view_state(ViewState.READY, result.detail)
        self.set_timer(0.05, self.action_refresh)

    def _toggle_pause(self) -> None:
        paused = not bool(self._status and self._status.paused)
        self._begin_operation(
            "Pausing scheduler" if paused else "Resuming scheduler",
            lambda cancel, progress: self.backend.set_paused(
                paused, cancel=cancel, progress=progress
            ),
            lambda _result: self.action_refresh(),
        )

    def action_cancel_schedule(self) -> None:
        """Review and cancel the highlighted local schedule."""

        job = self._selected()
        if job is None:
            return
        self._confirm(
            "Cancel this schedule?",
            f"This cancels only local job {clean_cell(job.reference)}. "
            "It does not delete the draft.",
            self._cancel_selected,
            confirm_label="Cancel schedule",
        )

    def action_send_now(self) -> None:
        """Review and send the highlighted scheduled message immediately."""

        job = self._selected()
        if job is None or self.offline or not self.backend.online:
            return
        self._confirm(
            "Send this message now?",
            f"Subject: {clean_cell(job.subject) or '(no subject)'}\n"
            f"Recipients: {len(job.to) + len(job.cc) + len(job.bcc)}\n\n"
            "This crosses the Exchange send boundary and cannot be undone after acceptance.",
            self._send_selected,
            confirm_label="Send now",
        )

    def action_run_due(self) -> None:
        """Review one bounded persistent-scheduler cycle."""

        if self.offline or not self.backend.online:
            return
        self._confirm(
            "Run due scheduled messages?",
            "Process at most 20 due jobs. Each durable job is claimed transactionally; "
            "uncertain outcomes are held for manual review rather than retried blindly.",
            self._run_due,
            confirm_label="Run due jobs",
        )

    def action_toggle_pause(self) -> None:
        """Pause or resume the durable local scheduler."""

        self._toggle_pause()

    def action_pause(self) -> None:
        """Pause the scheduler only when it is currently running."""

        if not (self._status and self._status.paused):
            self._toggle_pause()

    def action_resume(self) -> None:
        """Resume the scheduler only when it is currently paused."""

        if self._status and self._status.paused:
            self._toggle_pause()

    def action_pause_recurring(self) -> None:
        """Pause future materialization for the selected recurring rule."""

        self._review_recurrence_change(paused=True)

    def action_resume_recurring(self) -> None:
        """Resume future materialization for the selected recurring rule."""

        self._review_recurrence_change(paused=False)

    def _review_recurrence_change(self, *, paused: bool) -> None:
        job = self._selected()
        if job is None or not job.recurrence_id:
            self._set_view_state(
                ViewState.EMPTY,
                "Select a scheduled occurrence that belongs to a recurring rule.",
            )
            return
        verb = "Pause" if paused else "Resume"
        self._confirm(
            f"{verb} this recurring rule?",
            f"Rule: {clean_cell(job.recurrence_id)}\n"
            f"Selected occurrence: {clean_cell(job.reference)}\n\n"
            + (
                "Future occurrences will stop materializing; existing scheduled jobs "
                "remain auditable."
                if paused
                else "Future occurrences may materialize and send according to this rule."
            ),
            lambda: self._set_selected_recurrence_paused(paused),
            confirm_label=f"{verb} recurring",
        )

    def _set_selected_recurrence_paused(self, paused: bool) -> None:
        job = self._selected()
        if job is None:
            return
        self._begin_operation(
            "Pausing recurring message" if paused else "Resuming recurring message",
            lambda cancel, progress: self.backend.set_recurrence_paused(
                job.reference,
                paused,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._operation_complete(cast(ScheduledOperationView, result)),
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "scheduled-refresh":
            self.action_refresh()
        elif button_id == "scheduled-edit":
            self.action_edit()
        elif button_id == "scheduled-reschedule":
            self.action_reschedule()
        elif button_id == "scheduled-duplicate":
            self.action_duplicate()
        elif button_id == "scheduled-history":
            self.action_history()
        elif button_id == "scheduled-pause":
            self.action_toggle_pause()
        elif button_id == "scheduled-undo":
            job = self._selected()
            if job is not None:
                self._confirm(
                    "Undo this send?",
                    "Cancel local Undo Send delay for "
                    f"{clean_cell(job.subject) or '(no subject)'}?",
                    self._undo_selected,
                    confirm_label="Undo send",
                )
        elif button_id == "scheduled-cancel":
            self.action_cancel_schedule()
        elif button_id == "scheduled-send-now":
            self.action_send_now()
        elif button_id == "scheduled-run-due":
            self.action_run_due()
