"""Shared infrastructure for operational product-area screens.

The screens in :mod:`gucmail.tui` deliberately depend on small protocols instead
of application services.  This keeps presentation code free of Exchange types,
allows offline backends, and makes every operation deterministic under Textual's
pilot tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Button, Label, Static
from textual.worker import Worker, WorkerState

from gucmail.errors import GUCmailError
from gucmail.security import sanitize_terminal
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.write_coordinator import (
    PENDING_CHANGE_MESSAGE,
    WriteLease,
    app_write_coordinator,
)


def current_row_highlight(event: DataTable.RowHighlighted) -> bool:
    """Reject queued highlights from a cleared table or a superseded cursor.

    Clearing/repopulating a DataTable queues intermediate first-row highlights.
    Those messages must not change the selected object used by later actions.
    """
    table = event.data_table
    return bool(
        table.row_count
        and event.row_key == table.coordinate_to_cell_key(table.cursor_coordinate).row_key
    )


class ViewState(StrEnum):
    """Visible lifecycle state shared by every product area."""

    IDLE = "idle"
    LOADING = "loading"
    READY = "ready"
    EMPTY = "empty"
    OFFLINE = "offline"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class OperationProgress:
    """Bounded progress information emitted by an injected backend."""

    phase: str
    completed: int = 0
    total: int | None = None
    detail: str = ""

    def render(self) -> str:
        phase = sanitize_terminal(self.phase, max_length=80)
        detail = sanitize_terminal(self.detail, max_length=160)
        if self.total is not None and self.total > 0:
            amount = f"{max(0, self.completed)}/{self.total}"
        elif self.completed:
            amount = str(max(0, self.completed))
        else:
            amount = ""
        return " · ".join(part for part in (phase, amount, detail) if part)


ProgressReporter = Callable[[OperationProgress], None]
BlockingOperation = Callable[[Event, ProgressReporter], object]
OperationSuccess = Callable[[object], None]


class PublicOperationError(RuntimeError):
    """An error with text that is explicitly safe to display to the user."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(sanitize_terminal(message, max_length=500))
        self.retryable = retryable


class OfflineOperationError(PublicOperationError):
    """Raised when a backend cannot perform an online-only action."""

    def __init__(self, message: str = "This action is unavailable while offline.") -> None:
        super().__init__(message, retryable=False)


class AreaAction(Message):
    """Observable lifecycle event for activity history and app integration."""

    def __init__(
        self,
        area: str,
        action: str,
        phase: str,
        *,
        detail: str = "",
    ) -> None:
        super().__init__()
        self.area = sanitize_terminal(area, max_length=40)
        self.action = sanitize_terminal(action, max_length=80)
        self.phase = sanitize_terminal(phase, max_length=40)
        self.detail = sanitize_terminal(detail, max_length=300)


class ConfirmationScreen(ModalScreen[bool]):
    """Small reusable confirmation dialog that never guesses consent."""

    DEFAULT_CSS = """
    ConfirmationScreen { align: center middle; }
    #operational-confirm-dialog {
        width: 72; max-width: 92%; height: 80%; max-height: 22; min-height: 10;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    #operational-confirm-title { text-style: bold; height: auto; margin-bottom: 1; }
    #operational-confirm-body { height: 1fr; min-height: 1; max-height: 12; }
    #operational-confirm-detail { height: auto; }
    #operational-confirm-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #operational-confirm-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "reject", "Cancel", show=False),
        Binding("n", "reject", "Cancel", show=False),
        Binding("y", "accept", "Confirm", show=False),
    ]

    def __init__(self, title: str, detail: str, *, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.title_text = sanitize_terminal(title, max_length=120)
        self.detail_text = sanitize_terminal(detail, max_length=2_000)
        self.confirm_label = sanitize_terminal(confirm_label, max_length=40)

    def compose(self) -> ComposeResult:
        with Vertical(id="operational-confirm-dialog"):
            yield Label(self.title_text, id="operational-confirm-title", markup=False)
            with VerticalScroll(id="operational-confirm-body"):
                yield Static(self.detail_text, id="operational-confirm-detail", markup=False)
            with Horizontal(id="operational-confirm-actions"):
                yield Button("Cancel", id="operational-confirm-no")
                yield Button(
                    self.confirm_label,
                    id="operational-confirm-yes",
                    variant="warning",
                )

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "operational-confirm-yes":
            self.dismiss(True)
        elif event.button.id == "operational-confirm-no":
            self.dismiss(False)

    def action_accept(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)


class OperationalScreen(Screen[None]):
    """Base screen for safe blocking operations.

    Blocking backend calls run in a worker thread.  Generation checks ensure a
    cancelled, timed-out, superseded, or unmounted operation cannot update the
    screen later.  Cancellation is cooperative through :class:`threading.Event`;
    this matters for EWS and backup implementations that cannot kill a request
    thread safely.
    """

    # Specialized forms own geometry while sharing semantic styling.
    SHARED_WORKSPACE_LAYOUT: ClassVar[bool] = True

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("ctrl+c", "cancel_operation", "Cancel operation", show=False),
    ]
    DEFAULT_CSS = """
    OperationalScreen Button { min-width: 0; }
    OperationalScreen .workspace-heading {
        width: 100%; background: $surface; color: $text;
        padding: 0 1; text-style: bold;
    }
    OperationalScreen #area-state { padding: 0 1; }
    OperationalScreen #area-state.workspace-error { color: $error; text-style: bold; }
    OperationalScreen #area-state.workspace-offline { color: $text-muted; }
    OperationalScreen #area-state.workspace-loading { color: $text; text-style: italic; }
    """
    DEFAULT_OPERATION_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        *,
        area_name: str,
        offline: bool = False,
        operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()
        if operation_timeout_seconds <= 0:
            raise ValueError("Operation timeout must be positive.")
        self.area_name = sanitize_terminal(area_name, max_length=40)
        self.offline = offline
        self.operation_timeout_seconds = operation_timeout_seconds
        self.view_state = ViewState.IDLE
        self._operation_generation = 0
        self._cancel_event = Event()
        self._active_worker: Worker[None] | None = None
        self._timeout_timer: Timer | None = None
        self._last_operation: (
            tuple[
                str,
                BlockingOperation,
                OperationSuccess,
                float,
            ]
            | None
        ) = None
        self._operation_active = False
        self._operation_retry_safe = False
        self._active_action = ""
        self._active_timeout = operation_timeout_seconds
        self._mutation_finished: Event | None = None
        self._write_lease: WriteLease | None = None
        self._workspace_titles: dict[Label, str] = {}

    @property
    def mutation_pending(self) -> bool:
        """A write thread can outlive cancellation or the visible timeout."""
        return (self._operation_active and not self._operation_retry_safe) or (
            self._mutation_finished is not None and not self._mutation_finished.is_set()
        )

    def _guard_pending_mutation(self, *, shared: bool = True) -> bool:
        if not self.mutation_pending and not (shared and app_write_coordinator(self.app).pending):
            return False
        # A rejected action has no retry target. Do not leave an older read's
        # Retry button under a warning about this different change.
        self._last_operation = None
        self._set_view_state(
            ViewState.LOADING if self._operation_active else ViewState.ERROR,
            PENDING_CHANGE_MESSAGE,
        )
        return True

    def on_mount(self) -> None:
        # Preserve each workspace's geometry and bindings while giving its title
        # and status the same hierarchy as the mail shell.
        if not self.SHARED_WORKSPACE_LAYOUT:
            return
        self.set_class(self.size.width < 100, "workspace-narrow")
        for label in self.query(Label):
            if label.id and label.id.endswith("-title"):
                self.set_workspace_title(label, str(label.content))
        self._layout_action_rows()

    def set_workspace_title(self, label: Label, title: str) -> None:
        self._workspace_titles[label] = title
        theme = getattr(self.app, "runtime_theme", getattr(self.app, "active_theme", None))
        palette = getattr(theme, "palette", None)
        label.add_class("workspace-heading")
        heading = Text("GUC Mail", style=getattr(palette, "logo_primary", "red"))
        heading.append(
            " · " + sanitize_terminal(title, max_length=120),
            style=f"bold {getattr(palette, 'primary_text', 'white')}",
        )
        label.update(heading)
        label.styles.height = 1

    def refresh_workspace_theme(self) -> None:
        """Rich title spans need repainting as well as CSS when a theme changes."""
        for label, title in tuple(self._workspace_titles.items()):
            if label.is_mounted:
                self.set_workspace_title(label, title)

    def _layout_action_rows(self) -> None:
        if not self.SHARED_WORKSPACE_LAYOUT or not self.has_class("frontend"):
            return
        narrow = self.size.width < 100
        for root in self.children:
            if isinstance(root, (Vertical, VerticalScroll)):
                root.styles.overflow_x = "hidden"
                if narrow:
                    root.styles.padding = (0, 1)
                else:
                    root.styles.padding = (1, 2)
        for row in self.query("Horizontal, HorizontalScroll"):
            children = list(row.children)
            if children and all(isinstance(child, Button) for child in children):
                visible = sum(child.display for child in children)
                row.add_class("workspace-actions")
                row.set_class(len(children) > 5, "many-actions")
                footer = "footer" in (row.id or "")
                columns = min(max(visible, 1), 3 if narrow and not footer else 4)
                required = sum(
                    cell_len(str(child.label)) + 4
                    for child in children
                    if isinstance(child, Button) and child.display
                )
                available = row.content_size.width or max(1, self.size.width - 4)
                grid = narrow or required > available
                row.styles.layout = "grid" if grid else "horizontal"
                row.styles.grid_size_columns = columns
                row.styles.grid_rows = "1"
                row.styles.height = max(1, (visible + columns - 1) // columns) if grid else 1
                row.styles.min_height = 1
                row.styles.overflow_x = "hidden"
                row.styles.overflow_y = "hidden"
                for child in children:
                    child.styles.width = "1fr" if grid else "auto"
                    child.styles.margin = 0

    @on(Resize)
    def workspace_resized(self, event: Resize) -> None:
        if self.SHARED_WORKSPACE_LAYOUT:
            self.set_class(event.size.width < 100, "workspace-narrow")
            self._layout_action_rows()

    def on_unmount(self) -> None:
        self._invalidate_operation()

    def _invalidate_operation(self) -> None:
        self._operation_generation += 1
        self._operation_active = False
        self._cancel_event.set()
        if self._write_lease is not None:
            self._write_lease.cancel_if_unstarted()
        if self._active_worker is not None:
            # A queued write must enter its finally block even if cancelled
            # before its thread starts. Cancelling the Worker would skip it.
            if self._operation_retry_safe:
                self._active_worker.cancel()
            self._active_worker = None
        if self._timeout_timer is not None:
            self._timeout_timer.stop()
            self._timeout_timer = None

    def _set_view_state(self, state: ViewState, message: str) -> None:
        self.view_state = state
        if state is not ViewState.LOADING:
            self._clear_progress()
        safe = sanitize_terminal(message, max_length=1_000)
        widgets = self.query("#area-state")
        if widgets:
            status = widgets.first(Static)
            status.update(safe)
            status.set_class(state is ViewState.ERROR, "workspace-error")
            status.set_class(state is ViewState.OFFLINE, "workspace-offline")
            status.set_class(state is ViewState.LOADING, "workspace-loading")
        retry_buttons = self.query("#area-retry")
        if retry_buttons:
            retry_buttons.first(Button).display = (
                state is ViewState.ERROR and self._last_operation is not None
            )
        cancel_buttons = self.query("#area-cancel-operation")
        if cancel_buttons:
            cancel_buttons.first(Button).display = self._operation_active
        self._layout_action_rows()

    def _begin_operation(
        self,
        action: str,
        operation: BlockingOperation,
        on_success: OperationSuccess,
        *,
        timeout_seconds: float | None = None,
        remember: bool = True,
        retry_safe: bool = False,
    ) -> None:
        if self._guard_pending_mutation(shared=not retry_safe):
            return
        timeout = timeout_seconds or self.operation_timeout_seconds
        if timeout <= 0:
            raise ValueError("Operation timeout must be positive.")
        lease = None if retry_safe else app_write_coordinator(self.app).reserve()
        if not retry_safe and lease is None:
            self._guard_pending_mutation()
            return
        self._invalidate_operation()
        self._cancel_event = Event()
        generation = self._operation_generation
        self._operation_active = True
        self._operation_retry_safe = retry_safe
        self._active_action = action
        self._active_timeout = timeout
        self._write_lease = lease
        mutation_finished = None if lease is None else lease.finished
        self._mutation_finished = mutation_finished
        if not retry_safe:
            self._last_operation = None
        elif remember:
            self._last_operation = (action, operation, on_success, timeout)
        self._clear_progress()
        self._set_view_state(ViewState.LOADING, f"{action}…")
        self.post_message(AreaAction(self.area_name, action, "started"))
        self._timeout_timer = self.set_timer(
            timeout,
            lambda: self._operation_timed_out(generation, action),
        )
        self._active_worker = self._execute_operation(
            generation,
            action,
            operation,
            on_success,
            self._cancel_event,
            lease,
        )

    @on(Worker.StateChanged)
    def _operation_worker_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is self._active_worker and event.state is WorkerState.CANCELLED:
            self.action_cancel_operation()

    @work(thread=True, exclusive=False, group="operational-area", exit_on_error=False)
    def _execute_operation(
        self,
        generation: int,
        action: str,
        operation: BlockingOperation,
        on_success: OperationSuccess,
        cancel_event: Event,
        lease: WriteLease | None,
    ) -> None:
        if lease is not None and not lease.start():
            return

        def report(progress: OperationProgress) -> None:
            try:
                self.app.call_from_thread(self._show_progress, generation, progress)
            except RuntimeError:
                return

        result: object | None = None
        error: BaseException | None = None
        try:
            if not cancel_event.is_set():
                result = operation(cancel_event, report)
        except BaseException as exc:  # the UI translates and contains backend failures
            error = exc
        finally:
            if lease is not None:
                lease.finish()
        try:
            self.app.call_from_thread(
                self._finish_operation,
                generation,
                action,
                on_success,
                result,
                error,
            )
        except RuntimeError:
            return

    def _show_progress(self, generation: int, progress: OperationProgress) -> None:
        if generation != self._operation_generation or not self.is_mounted:
            return
        rendered = progress.render() or "Working…"
        widgets = self.query("#area-progress")
        if widgets:
            widgets.first(Static).update(rendered)
        self._set_view_state(ViewState.LOADING, rendered)

    def _clear_progress(self) -> None:
        widgets = self.query("#area-progress")
        if widgets:
            widgets.first(Static).update("")

    def _finish_operation(
        self,
        generation: int,
        action: str,
        on_success: OperationSuccess,
        result: object | None,
        error: BaseException | None,
    ) -> None:
        if generation != self._operation_generation or not self.is_mounted:
            return
        if self._timeout_timer is not None:
            self._timeout_timer.stop()
            self._timeout_timer = None
        self._active_worker = None
        self._operation_active = False
        self._clear_progress()
        if error is not None:
            self._show_error(action, error)
            return
        try:
            on_success(result)
        except BaseException as exc:
            self._show_error(action, exc)
            return
        if self.view_state is ViewState.LOADING and generation == self._operation_generation:
            self._set_view_state(ViewState.READY, f"{action} complete.")
        self.post_message(AreaAction(self.area_name, action, "completed"))

    def _show_error(self, action: str, error: BaseException) -> None:
        if isinstance(error, PublicOperationError):
            message = str(error)
            retryable = error.retryable
            state = (
                ViewState.OFFLINE if isinstance(error, OfflineOperationError) else ViewState.ERROR
            )
        elif isinstance(error, GUCmailError):
            message = f"{action}: {error.message}"
            if error.context.mutation_uncertain:
                message += " The change may have completed. Refresh before another mutation."
            if error.context.suggestion:
                message += " " + error.context.suggestion
            retryable = (
                self._operation_retry_safe
                and error.context.retry_safe is not False
                and not error.context.mutation_uncertain
            )
            state = ViewState.ERROR
        else:
            error_type = sanitize_terminal(type(error).__name__, max_length=80) or "Error"
            message = (
                f"{action} failed ({error_type}). Details were omitted; use redacted logs "
                "or diagnostics."
            )
            retryable = True
            state = ViewState.ERROR
        self._set_view_state(state, message)
        retry_buttons = self.query("#area-retry")
        if retry_buttons:
            retry_buttons.first(Button).display = retryable and self._last_operation is not None
        if not retryable:
            self._last_operation = None
        self.post_message(AreaAction(self.area_name, action, "failed", detail=message))
        statuses = self.query("#area-state")
        if statuses:
            status = statuses.first(Static)
            self.call_after_refresh(
                lambda: (
                    status.scroll_visible(animate=False, immediate=True)
                    if status.is_mounted
                    else None
                )
            )

    def _operation_timed_out(self, generation: int, action: str) -> None:
        if generation != self._operation_generation or not self._operation_active:
            return
        self._cancel_event.set()
        if self._write_lease is not None:
            self._write_lease.cancel_if_unstarted()
        if self._active_worker is not None and self._operation_retry_safe:
            self._active_worker.cancel()
        self._operation_generation += 1
        self._operation_active = False
        self._active_worker = None
        self._timeout_timer = None
        message = f"{action} timed out after {self._active_timeout:g} seconds. " + (
            "The late result will be ignored; you can retry this read."
            if self._operation_retry_safe
            else "It may still complete. Check its result before starting the action again."
        )
        self._set_view_state(ViewState.ERROR, message)
        self.post_message(AreaAction(self.area_name, action, "timed-out", detail=message))

    def _confirm(
        self,
        title: str,
        detail: str,
        confirmed: Callable[[], None],
        *,
        confirm_label: str = "Confirm",
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        if self._guard_pending_mutation():
            return
        self.post_message(AreaAction(self.area_name, title, "confirmation-requested"))

        def resolved(value: bool | None) -> None:
            if value:
                confirmed()
            else:
                self.post_message(AreaAction(self.area_name, title, "cancelled"))
                if on_cancel is not None:
                    on_cancel()

        self.app.push_screen(
            ConfirmationScreen(title, detail, confirm_label=confirm_label),
            resolved,
        )

    def action_cancel_operation(self) -> None:
        if not self._operation_active:
            return
        self._cancel_event.set()
        self._invalidate_operation()
        self._set_view_state(
            ViewState.CANCELLED,
            "Stopped waiting for the operation."
            + (
                ""
                if self._operation_retry_safe
                else " It may still complete; check its result before repeating it."
            ),
        )
        self.post_message(AreaAction(self.area_name, "operation", "cancelled"))

    def action_retry(self) -> None:
        if self._last_operation is None or self._operation_active:
            return
        action, operation, on_success, timeout = self._last_operation
        self._begin_operation(
            action,
            operation,
            on_success,
            timeout_seconds=timeout,
            remember=False,
            retry_safe=True,
        )

    def action_close(self) -> None:
        self._invalidate_operation()
        self.dismiss(None)

    def action_refresh(self) -> None:
        """Subclasses implement their bounded refresh action."""

    @on(Button.Pressed, "#area-retry")
    def _retry_pressed(self) -> None:
        self.action_retry()

    @on(Button.Pressed, "#area-cancel-operation")
    def _cancel_pressed(self) -> None:
        self.action_cancel_operation()

    @on(Button.Pressed, ".area-close")
    def _close_pressed(self) -> None:
        self.action_close()


def human_bytes(value: int | None) -> str:
    """Render byte counts without implying unavailable capacity."""

    if value is None:
        return "Unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def clean_cell(value: object, *, max_length: int = 120) -> str:
    """Sanitize untrusted backend data before placing it in a table."""

    return sanitize_terminal(value, max_length=max_length)


__all__ = [
    "AreaAction",
    "BlockingOperation",
    "ConfirmationScreen",
    "OfflineOperationError",
    "OperationProgress",
    "OperationalScreen",
    "ProgressReporter",
    "PublicOperationError",
    "ViewState",
    "clean_cell",
    "human_bytes",
]
