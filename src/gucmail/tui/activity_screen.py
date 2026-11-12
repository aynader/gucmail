"""Operational watcher, notification, and sanitized activity history screen."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event
from typing import ClassVar, Protocol, cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.message import Message
from textual.widgets import Button, Collapsible, Input, Label, Static

from gucmail.formatting import format_timestamp
from gucmail.tui.area_common import (
    OfflineOperationError,
    OperationalScreen,
    ProgressReporter,
    PublicOperationError,
    ViewState,
    clean_cell,
    current_row_highlight,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


@dataclass(frozen=True, slots=True)
class WatcherStatusView:
    enabled: bool
    state: str
    active_method: str
    configured_method: str
    active_profile: str | None
    monitored_folders: tuple[str, ...]
    last_success_at: str | None
    last_error: str | None = None
    next_check_at: str | None = None
    process_id: int | None = None
    notification_queue_count: int = 0


@dataclass(frozen=True, slots=True)
class ActivityEventView:
    event_id: str
    occurred_at: str
    category: str
    severity: str
    summary: str
    detail: str
    correlation_id: str | None = None
    retryable: bool = False
    provider: str | None = None
    attempt_count: int = 0
    next_retry_at: str | None = None
    undoable: bool = False


@dataclass(frozen=True, slots=True)
class ActivitySnapshotView:
    watcher: WatcherStatusView
    events: tuple[ActivityEventView, ...]
    retention_description: str
    bounded_limit: int
    offline: bool = False
    redaction_enabled: bool = True


@dataclass(frozen=True, slots=True)
class ActivityClearPlanView:
    event_count: int
    categories: tuple[str, ...]
    oldest_at: str | None
    newest_at: str | None
    preserved_operational_records: tuple[str, ...]
    confirmation_token: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class ActivityClearResultView:
    cleared_count: int
    cleared_before: str
    preserved_operational_records: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActivityUndoEntryView:
    reference: str
    current_reference: str
    original_folder: str


@dataclass(frozen=True, slots=True)
class ActivityUndoPlanView:
    event_id: str
    entries: tuple[ActivityUndoEntryView, ...]
    moved_at: str
    expires_at: str
    confirmation_token: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class ActivityUndoResultView:
    restored_count: int
    failed_count: int
    failed_references: tuple[str, ...] = ()


class ActivityScreenBackend(Protocol):
    """Blocking activity operations supplied by the durable watcher runtime."""

    def load_activity(
        self,
        *,
        category: str | None,
        text: str | None,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivitySnapshotView: ...

    def set_watcher_enabled(
        self,
        enabled: bool,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> WatcherStatusView: ...

    def control_watcher(
        self,
        action: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> WatcherStatusView: ...

    def retry_delivery(
        self,
        event_id: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityEventView: ...

    def test_notifications(
        self,
        *,
        provider: str = "console",
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityEventView: ...

    def export_activity(
        self,
        *,
        category: str | None,
        text: str | None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> str: ...

    def plan_clear_activity(
        self,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityClearPlanView: ...

    def execute_clear_activity(
        self,
        plan: ActivityClearPlanView,
        *,
        confirmed: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityClearResultView: ...

    def plan_cleanup_undo(
        self,
        event_id: str | None,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityUndoPlanView: ...

    def execute_cleanup_undo(
        self,
        plan: ActivityUndoPlanView,
        *,
        confirmed: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> ActivityUndoResultView: ...


class MessageOperationRequested(Message):
    """Request an exact, read-only recovery record from the owning app."""

    def __init__(self, operation_id: str) -> None:
        super().__init__()
        self.operation_id = operation_id


class ActivityScreen(OperationalScreen):
    """Filterable activity history with watcher and delivery controls."""

    FILTERS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("All activity", "all"),
        ("Mail", "mail"),
        ("Connection", "connection"),
        ("Watcher", "watcher"),
        ("Notification", "notification"),
        ("Webhook", "webhook"),
        ("Scheduler", "scheduler"),
        ("Backup", "backup"),
        ("Errors and retries", "error"),
    )
    DEFAULT_CSS = """
    ActivityScreen { layout: vertical; }
    #activity-root { width: 100%; height: 100%; padding: 1 2; }
    #activity-body { height: 1fr; }
    #activity-title { text-style: bold; height: 2; }
    #activity-watcher { height: auto; min-height: 5; max-height: 8; }
    #activity-filters { height: 3; }
    #activity-category { width: 1fr; margin-right: 1; }
    #activity-search { width: 2fr; }
    #activity-table { height: 1fr; min-height: 8; }
    #activity-detail-scroll { height: 8; border-top: solid $panel; padding: 1; }
    #activity-detail { height: auto; }
    #activity-watcher-actions, #activity-actions { height: auto; min-height: 3; }
    #activity-watcher-actions Button, #activity-actions Button { margin-right: 1; }
    #activity-watcher-actions Button, #activity-actions Button,
    #activity-footer-actions Button { height: 1; min-height: 1; border: none; }
    #activity-footer-actions { height: 3; align-horizontal: right; }
    #activity-footer-actions Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    ActivityScreen.compact-activity #activity-root { padding: 1; }
    ActivityScreen.compact-activity #activity-watcher {
        min-height: 6;
        max-height: 8;
    }
    ActivityScreen.compact-activity #activity-filters {
        layout: horizontal;
        height: 2;
    }
    ActivityScreen.compact-activity #activity-category,
    ActivityScreen.compact-activity #activity-search { width: 1fr; height: 2; }
    ActivityScreen.compact-activity #activity-table { height: 1fr; min-height: 5; }
    ActivityScreen.compact-activity #activity-detail-scroll { height: 4; padding: 0 1; }
    ActivityScreen.compact-activity #activity-watcher-actions,
    ActivityScreen.compact-activity #activity-actions {
        layout: grid; grid-size: 3; grid-rows: 1;
        height: 2; min-height: 2;
    }
    ActivityScreen.compact-activity #activity-watcher-actions Button,
    ActivityScreen.compact-activity #activity-actions Button {
        width: 1fr;
        margin-right: 0;
    }
    ActivityScreen.compact-activity #activity-footer-actions {
        dock: bottom;
        background: $footer-background;
    }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("f", "focus_filter", "Filter", show=False),
        Binding("t", "toggle_watcher", "Toggle watcher", show=False),
        Binding("p", "pause_resume_watcher", "Pause/resume", show=False),
        Binding("ctrl+r", "force_synchronization", "Force sync", show=False),
    ]

    def __init__(
        self,
        backend: ActivityScreenBackend,
        *,
        offline: bool = False,
        history_limit: int = 250,
        timezone: str = "Africa/Cairo",
        datetime_format: str = "%Y-%m-%d %H:%M %Z",
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            area_name="activity",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if not 1 <= history_limit <= 5_000:
            raise ValueError("Activity history limit must be between 1 and 5000.")
        self.backend = backend
        self.history_limit = history_limit
        self.timezone = timezone
        self.datetime_format = datetime_format
        self._snapshot: ActivitySnapshotView | None = None
        self._events: tuple[ActivityEventView, ...] = ()
        self._events_by_id: dict[str, ActivityEventView] = {}
        self._selected_event_id: str | None = None
        self._queued_watcher_action: str | None = None
        self._queued_notification_test: str | None = None
        self._queued_delivery_retry: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="activity-root"):
            yield from self._compose_body()
            with Horizontal(id="activity-watcher-actions"):
                yield Button("Start watcher", id="activity-toggle-watcher", variant="primary")
                yield Button("Pause watcher", id="activity-pause-watcher", disabled=True)
                yield Button("Force sync", id="activity-force-sync")
                yield Button("Test notifications", id="activity-test-notification")
            with Horizontal(id="activity-actions"):
                yield Button("Operation details", id="activity-operation-details", disabled=True)
                yield Button("Retry delivery", id="activity-retry-delivery", disabled=True)
                yield Button("Undo cleanup move…", id="activity-undo-cleanup", disabled=True)
                yield Button("Export", id="activity-export")
                yield Button("Clear history…", id="activity-clear-history", variant="warning")
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="activity-footer-actions"):
                yield Button("Retry", id="area-retry")
                yield Button("Cancel operation", id="area-cancel-operation")
                yield Button("Refresh", id="activity-refresh")
                yield Button("Close", classes="area-close")

    def _compose_body(self) -> ComposeResult:
        with VerticalScroll(id="activity-body"):
            yield Label("Activity", id="activity-title")
            yield Static("Loading sanitized activity history…", id="area-state", markup=False)
            yield Static("Watcher status is loading.", id="activity-overview", markup=False)
            with Collapsible(title="Monitoring details", id="activity-monitoring-details"):
                yield Static("", id="activity-watcher", markup=False)
            with Horizontal(id="activity-filters"):
                yield Select(
                    self.FILTERS,
                    value="all",
                    allow_blank=False,
                    id="activity-category",
                )
                yield Input(
                    placeholder="Filter summaries, event IDs, or correlation IDs",
                    id="activity-search",
                )
            yield DataTable(id="activity-table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="activity-detail-scroll"):
                yield Static(
                    "Select an event for sanitized details.", id="activity-detail", markup=False
                )

    def on_mount(self) -> None:
        self.set_class(self.size.width < 100 or self.size.height < 30, "compact-activity")
        self.query_one("#area-retry", Button).display = False
        self.query_one("#area-cancel-operation", Button).display = False
        table = self.query_one("#activity-table", DataTable)
        table.add_columns("Time", "Category", "Level", "Summary")
        if self.offline:
            self.query_one("#activity-force-sync", Button).disabled = True
            self.query_one("#activity-test-notification", Button).disabled = True
        self.call_after_refresh(self.action_refresh)

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 100 or event.size.height < 30, "compact-activity")

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if self.has_class("compact-activity"):
            event.widget.scroll_visible(animate=False, force=True, immediate=True)

    def _filters(self) -> tuple[str | None, str | None]:
        value = self.query_one("#activity-category", Select).value
        category = None if value in (Select.BLANK, "all") else str(value)
        text = self.query_one("#activity-search", Input).value.strip() or None
        return category, text

    def action_refresh(self) -> None:
        category, text = self._filters()
        self._begin_operation(
            "Loading activity",
            lambda cancel, progress: self.backend.load_activity(
                category=category,
                text=text,
                limit=self.history_limit,
                cancel=cancel,
                progress=progress,
            ),
            self._activity_loaded,
            retry_safe=True,
        )

    def _activity_loaded(self, result: object) -> None:
        if not isinstance(result, ActivitySnapshotView):
            raise PublicOperationError("The activity backend returned an invalid snapshot.")
        self._snapshot = result
        self._render_watcher(result.watcher)
        self._render_events(result.events)
        mode = "Offline history" if result.offline or self.offline else "Live local history"
        redaction = "redaction enabled" if result.redaction_enabled else "redaction disabled"
        self.query_one("#area-state", Static).tooltip = clean_cell(
            result.retention_description, max_length=180
        )
        if result.events:
            self._set_view_state(
                ViewState.OFFLINE if result.offline or self.offline else ViewState.READY,
                f"{mode}: {len(result.events)} events · {redaction}",
            )
        else:
            self._set_view_state(
                ViewState.OFFLINE if result.offline or self.offline else ViewState.EMPTY,
                f"{mode}: no events match this filter · {redaction}.",
            )
        if self._queued_watcher_action is not None:
            self.set_timer(0.01, self._run_queued_watcher_control)
        if self._queued_notification_test is not None:
            self.set_timer(0.01, self._run_queued_notification_test)
        if self._queued_delivery_retry is not None:
            self.set_timer(0.01, self._run_queued_delivery_retry)

    def _render_watcher(self, watcher: WatcherStatusView) -> None:
        folders = ", ".join(
            clean_cell(folder, max_length=60) for folder in watcher.monitored_folders
        )
        text = (
            f"Watcher: {clean_cell(watcher.state)} · "
            f"Configured: {clean_cell(watcher.configured_method)} · "
            f"Active: {clean_cell(watcher.active_method)}\n"
            f"Profile: {clean_cell(watcher.active_profile or 'None')} · "
            f"Folders: {folders or 'None'}\n"
            f"Last successful check: {self._time(watcher.last_success_at, 'Never')} · "
            f"Next check: {self._time(watcher.next_check_at, 'Not scheduled')} · "
            f"Queued notifications: {watcher.notification_queue_count}"
        )
        if watcher.last_error:
            text += f"\nLast error: {clean_cell(watcher.last_error, max_length=300)}"
        self.query_one("#activity-watcher", Static).update(text)
        overview = (
            f"Watcher  {clean_cell(watcher.state)}    ·    "
            f"Notifications queued  {watcher.notification_queue_count}\n"
            f"Last check  {self._time(watcher.last_success_at, 'Never')}"
        )
        if watcher.last_error:
            overview += f"\nLast error: {clean_cell(watcher.last_error, max_length=300)}"
        self.query_one("#activity-overview", Static).update(overview)
        toggle = self.query_one("#activity-toggle-watcher", Button)
        toggle.label = "Stop watcher" if watcher.enabled else "Start watcher"
        toggle.variant = "warning" if watcher.enabled else "primary"
        toggle.disabled = self.offline and not watcher.enabled
        paused = watcher.state in {"paused", "pause requested"}
        pause = self.query_one("#activity-pause-watcher", Button)
        pause.label = "Resume watcher" if paused else "Pause watcher"
        pause.disabled = not watcher.enabled or (self.offline and paused)

    def _time(self, value: str | None, empty: str = "Unknown") -> str:
        if not value:
            return empty
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # Legacy SQLite CURRENT_TIMESTAMP records are UTC without an offset.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return format_timestamp(parsed, pattern=self.datetime_format, timezone=self.timezone)
        except ValueError:
            return clean_cell(value, max_length=50)

    def _render_events(self, events: Sequence[ActivityEventView]) -> None:
        self._events = tuple(events)
        self._events_by_id = {event.event_id: event for event in events}
        table = self.query_one("#activity-table", DataTable)
        table.clear()
        for item in events:
            table.add_row(
                self._time(item.occurred_at),
                clean_cell(item.category, max_length=25),
                clean_cell(item.severity, max_length=18),
                clean_cell(item.summary, max_length=100),
                key=item.event_id,
            )
        if events:
            selected = (
                self._selected_event_id
                if self._selected_event_id in self._events_by_id
                else events[0].event_id
            )
            self._select_event(selected)
            table.move_cursor(
                row=next(i for i, item in enumerate(events) if item.event_id == selected)
            )
        else:
            self._selected_event_id = None
            self.query_one("#activity-detail", Static).update(
                "No sanitized activity events match this filter."
            )
            self._update_retry_button()

    @on(DataTable.RowHighlighted, "#activity-table")
    def _event_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select_event(str(event.row_key.value))

    def _select_event(self, event_id: str) -> None:
        item = self._events_by_id.get(event_id)
        if item is None:
            return
        self._selected_event_id = event_id
        lines = [
            clean_cell(item.summary, max_length=220),
            clean_cell(item.detail, max_length=700),
            f"Time: {self._time(item.occurred_at)}",
            f"Event ID: {clean_cell(item.event_id, max_length=180)}",
            f"Correlation ID: {clean_cell(item.correlation_id or 'None', max_length=180)}",
            f"Category: {clean_cell(item.category)} · Severity: {clean_cell(item.severity)}",
            f"Provider: {clean_cell(item.provider or 'None')} · Attempts: {item.attempt_count}",
            f"Next retry: {self._time(item.next_retry_at, 'Not scheduled')}",
        ]
        self.query_one("#activity-detail", Static).update("\n".join(lines))
        self._update_retry_button()

    def _update_retry_button(self) -> None:
        item = self._events_by_id.get(self._selected_event_id or "")
        self.query_one("#activity-operation-details", Button).disabled = not (
            item is not None and item.event_id.startswith("message-operation:")
        )
        self.query_one("#activity-retry-delivery", Button).disabled = (
            item is None or not item.retryable
        )
        self.query_one("#activity-undo-cleanup", Button).disabled = not (
            not self.offline and item is not None and item.undoable
        )

    def action_focus_filter(self) -> None:
        self.query_one("#activity-search", Input).focus()

    @on(Button.Pressed, "#activity-operation-details")
    def action_inspect_message_operation(self) -> None:
        item = self._events_by_id.get(self._selected_event_id or "")
        if item is not None and item.event_id.startswith("message-operation:"):
            self.post_message(
                MessageOperationRequested(item.event_id.removeprefix("message-operation:"))
            )

    def action_toggle_watcher(self) -> None:
        watcher = self._snapshot.watcher if self._snapshot is not None else None
        enable = not watcher.enabled if watcher is not None else True
        if self.offline and enable:
            self._show_error("Change watcher", OfflineOperationError())
            return
        label = "start" if enable else "stop"
        self._confirm(
            f"{label.title()} mailbox watcher?",
            (
                "Starting enables the configured monitoring profile and its notification routes."
                if enable
                else "Stopping pauses new-mail detection and notification delivery until restarted."
            ),
            lambda: self._start_watcher_control("start" if enable else "stop"),
            confirm_label=f"{label.title()} watcher",
        )

    def action_control_watcher(self) -> None:
        """Keyboard-compatible fallback; routed commands supply an exact control."""

        self.action_toggle_watcher()

    def _start_toggle_watcher(self, enabled: bool) -> None:
        self._start_watcher_control("start" if enabled else "stop")

    def queue_watcher_control(self, action: str) -> None:
        """Execute an exact palette lifecycle request once initial state is loaded."""

        normalized = action.casefold().strip()
        if normalized not in {"start", "stop", "pause", "resume", "sync"}:
            raise ValueError("unknown watcher control action")
        self._queued_watcher_action = normalized
        if self._snapshot is not None:
            # Palette dispatch and tests may call this from outside Textual's
            # current message callback.  Re-enter through the screen timer so
            # worker creation always belongs to the active app message loop.
            self.set_timer(0.01, self._run_queued_watcher_control)

    def _run_queued_watcher_control(self) -> None:
        control = self._queued_watcher_action
        if control is None or self._snapshot is None:
            return
        self._queued_watcher_action = None
        self._start_watcher_control(control)

    def _start_watcher_control(self, control: str) -> None:
        labels = {
            "start": "Starting watcher",
            "stop": "Stopping watcher",
            "pause": "Pausing watcher",
            "resume": "Resuming watcher",
            "sync": "Requesting watcher synchronization",
        }
        self._begin_operation(
            labels[control],
            lambda cancel, progress: self.backend.control_watcher(
                control,
                cancel=cancel,
                progress=progress,
            ),
            self._watcher_changed,
        )

    def action_pause_resume_watcher(self) -> None:
        watcher = self._snapshot.watcher if self._snapshot is not None else None
        if watcher is None or not watcher.enabled:
            self._show_error(
                "Pause watcher",
                PublicOperationError("Start the watcher before pausing it.", retryable=False),
            )
            return
        paused = watcher.state in {"paused", "pause requested"}
        if self.offline and paused:
            self._show_error("Resume watcher", OfflineOperationError())
            return
        control = "resume" if paused else "pause"
        self._confirm(
            f"{control.title()} mailbox watcher?",
            (
                "Resume conservative mailbox checks using the existing watcher session."
                if paused
                else "Pause mailbox checks without discarding checkpoints or stopping the process."
            ),
            lambda: self._start_watcher_control(control),
            confirm_label=f"{control.title()} watcher",
        )

    def action_force_synchronization(self) -> None:
        if self.offline:
            self._show_error("Force synchronization", OfflineOperationError())
            return
        self._start_watcher_control("sync")

    def _watcher_changed(self, result: object) -> None:
        if not isinstance(result, WatcherStatusView):
            raise PublicOperationError("The activity backend returned an invalid watcher status.")
        self._render_watcher(result)
        if self._snapshot is not None:
            self._snapshot = ActivitySnapshotView(
                result,
                self._snapshot.events,
                self._snapshot.retention_description,
                self._snapshot.bounded_limit,
                self._snapshot.offline,
                self._snapshot.redaction_enabled,
            )
        self._set_view_state(
            ViewState.READY,
            f"Watcher state: {clean_cell(result.state)}.",
        )

    def action_retry_delivery(self) -> None:
        item = self._events_by_id.get(self._selected_event_id or "")
        if item is None or not item.retryable:
            self._show_error(
                "Retry delivery",
                PublicOperationError("Select a retryable delivery event.", retryable=False),
            )
            return
        self._confirm(
            "Retry this delivery?",
            f"Event: {clean_cell(item.event_id, max_length=180)}\n"
            f"Provider: {clean_cell(item.provider or 'configured provider')}\n\n"
            "This schedules the durable event for the watcher immediately. Its stable event ID "
            "is reused for provider-side idempotency.",
            lambda: self._start_retry_delivery(item.correlation_id or item.event_id),
            confirm_label="Schedule retry",
        )

    def queue_delivery_retry(self, category: str = "webhook") -> None:
        """Focus and confirm the newest retryable delivery in one activity category."""

        normalized = category.casefold().strip()
        valid = {str(value) for _label, value in self.FILTERS}
        if normalized not in valid or normalized == "all":
            raise ValueError("unknown retry delivery category")
        self._queued_delivery_retry = normalized
        selector = self.query_one("#activity-category", Select)
        if selector.value != normalized:
            selector.value = normalized
            self.action_refresh()
        elif self._snapshot is not None:
            self.set_timer(0.01, self._run_queued_delivery_retry)

    def _run_queued_delivery_retry(self) -> None:
        category = self._queued_delivery_retry
        if category is None or self._snapshot is None:
            return
        self._queued_delivery_retry = None
        selected = next(
            (
                item
                for item in self._snapshot.events
                if item.category == category and item.retryable
            ),
            None,
        )
        if selected is None:
            self._set_view_state(
                ViewState.EMPTY,
                f"No retryable {clean_cell(category)} delivery is available.",
            )
            return
        self._select_event(selected.event_id)
        self.action_retry_delivery()

    def _start_retry_delivery(self, event_id: str) -> None:
        self._begin_operation(
            "Retrying delivery",
            lambda cancel, progress: self.backend.retry_delivery(
                event_id,
                cancel=cancel,
                progress=progress,
            ),
            self._delivery_completed,
        )

    def _delivery_completed(self, result: object) -> None:
        if not isinstance(result, ActivityEventView):
            raise PublicOperationError("The activity backend returned an invalid delivery event.")
        self._set_view_state(ViewState.READY, "Delivery retry completed; activity was recorded.")
        self.action_refresh()

    def action_test_notifications(self) -> None:
        self._confirm(
            "Run a synthetic local notification test?",
            "The privacy-safe local console provider receives one clearly marked synthetic "
            "event and records its outcome. No mailbox message, private mailbox data, webhook, "
            "or external provider is used from this Activity action.",
            self._start_test_notifications,
            confirm_label="Run local test",
        )

    def queue_notification_test(self, provider: str = "desktop") -> None:
        """Execute one exact synthetic provider test after Activity mounts."""

        normalized = provider.casefold().strip()
        if normalized not in {"console", "desktop"}:
            raise ValueError("unknown notification test provider")
        self._queued_notification_test = normalized
        if self._snapshot is not None:
            self.set_timer(0.01, self._run_queued_notification_test)

    def _run_queued_notification_test(self) -> None:
        provider = self._queued_notification_test
        if provider is None or self._snapshot is None:
            return
        self._queued_notification_test = None
        self._start_test_notifications(provider)

    def action_test_desktop_notification(self) -> None:
        self.queue_notification_test("desktop")

    def _start_test_notifications(self, provider: str = "console") -> None:
        self._begin_operation(
            f"Testing {provider} notifications",
            lambda cancel, progress: self.backend.test_notifications(
                provider=provider,
                cancel=cancel,
                progress=progress,
            ),
            self._test_completed,
        )

    def _test_completed(self, result: object) -> None:
        if not isinstance(result, ActivityEventView):
            raise PublicOperationError("The activity backend returned an invalid test event.")
        self._set_view_state(
            ViewState.READY,
            f"Test recorded as event {clean_cell(result.event_id, max_length=100)}.",
        )
        self.action_refresh()

    def action_export(self) -> None:
        category, text = self._filters()
        self._begin_operation(
            "Exporting activity",
            lambda cancel, progress: self.backend.export_activity(
                category=category,
                text=text,
                cancel=cancel,
                progress=progress,
            ),
            self._exported,
        )

    def _exported(self, result: object) -> None:
        path = cast(str, result)
        if not isinstance(path, str) or not path.strip():
            raise PublicOperationError("The activity backend did not return an export location.")
        self._set_view_state(
            ViewState.READY,
            f"Sanitized activity exported to {clean_cell(path, max_length=400)}.",
        )

    def action_clear_local_activity_history(self) -> None:
        self._begin_operation(
            "Planning activity-history clear",
            lambda cancel, progress: self.backend.plan_clear_activity(
                cancel=cancel,
                progress=progress,
            ),
            self._review_clear_history,
            retry_safe=True,
        )

    def _review_clear_history(self, result: object) -> None:
        if not isinstance(result, ActivityClearPlanView):
            raise PublicOperationError("The activity backend returned an invalid clear plan.")
        if result.event_count == 0:
            self._set_view_state(ViewState.READY, "Local Activity history is already empty.")
            return
        categories = ", ".join(clean_cell(value) for value in result.categories) or "none"
        preserved = "\n".join(
            f"  keep · {clean_cell(value, max_length=180)}"
            for value in result.preserved_operational_records
        )
        self._confirm(
            "Clear this exact local Activity history scope?",
            f"Visible local events: {result.event_count}\n"
            f"Oldest: {clean_cell(result.oldest_at or 'Unknown')}\n"
            f"Newest: {clean_cell(result.newest_at or 'Unknown')}\n"
            f"Categories: {categories}\n\n"
            "The Activity view will durably hide these existing records. New events remain "
            "visible. Safety-critical operational state is preserved:\n"
            f"{preserved}\n\nNo mailbox, server, backup archive, or scheduled message is changed.",
            lambda: self._start_clear_history(result),
            confirm_label="Clear listed local history",
        )

    def _start_clear_history(self, plan: ActivityClearPlanView) -> None:
        self._begin_operation(
            "Clearing local Activity history",
            lambda cancel, progress: self.backend.execute_clear_activity(
                plan,
                confirmed=True,
                cancel=cancel,
                progress=progress,
            ),
            self._history_cleared,
        )

    def _history_cleared(self, result: object) -> None:
        if not isinstance(result, ActivityClearResultView):
            raise PublicOperationError("The activity backend returned an invalid clear result.")
        self._set_view_state(
            ViewState.READY,
            f"Cleared {result.cleared_count} local Activity event(s) through "
            f"{clean_cell(result.cleared_before)}; operational state was preserved.",
        )
        self.action_refresh()

    def action_undo_recent_cleanup(self) -> None:
        selected = self._events_by_id.get(self._selected_event_id or "")
        event_id = selected.event_id if selected is not None and selected.undoable else None
        self._begin_operation(
            "Planning cleanup undo",
            lambda cancel, progress: self.backend.plan_cleanup_undo(
                event_id,
                cancel=cancel,
                progress=progress,
            ),
            self._review_cleanup_undo,
            retry_safe=True,
        )

    def _review_cleanup_undo(self, result: object) -> None:
        if not isinstance(result, ActivityUndoPlanView):
            raise PublicOperationError("The activity backend returned an invalid undo plan.")
        entries = "\n".join(
            f"  restore · {clean_cell(entry.current_reference, max_length=160)} → "
            f"{clean_cell(entry.original_folder, max_length=160)}"
            for entry in result.entries
        )
        self._confirm(
            "Undo this exact cleanup move?",
            f"Moved at: {clean_cell(result.moved_at)}\n"
            f"Undo expires: {clean_cell(result.expires_at)}\n"
            f"Items: {len(result.entries)}\n\n{entries}\n\n"
            "The items are moved from Deleted Items back to their recorded original folders. "
            "The operation stops at the first provider error and never sends or deletes mail.",
            lambda: self._start_cleanup_undo(result),
            confirm_label="Restore exact items",
        )

    def _start_cleanup_undo(self, plan: ActivityUndoPlanView) -> None:
        self._begin_operation(
            "Undoing cleanup move",
            lambda cancel, progress: self.backend.execute_cleanup_undo(
                plan,
                confirmed=True,
                cancel=cancel,
                progress=progress,
            ),
            self._cleanup_undo_finished,
        )

    def _cleanup_undo_finished(self, result: object) -> None:
        if not isinstance(result, ActivityUndoResultView):
            raise PublicOperationError("The activity backend returned an invalid undo result.")
        state = ViewState.READY if result.failed_count == 0 else ViewState.ERROR
        self._set_view_state(
            state,
            f"Cleanup undo completed: {result.restored_count} restored, "
            f"{result.failed_count} failed.",
        )
        self.action_refresh()

    @on(Select.Changed, "#activity-category")
    def _category_changed(self) -> None:
        if self.is_mounted:
            self.action_refresh()

    @on(Input.Submitted, "#activity-search")
    def _search_submitted(self) -> None:
        self.action_refresh()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "activity-refresh":
            self.action_refresh()
        elif button_id == "activity-toggle-watcher":
            self.action_toggle_watcher()
        elif button_id == "activity-pause-watcher":
            self.action_pause_resume_watcher()
        elif button_id == "activity-force-sync":
            self.action_force_synchronization()
        elif button_id == "activity-retry-delivery":
            self.action_retry_delivery()
        elif button_id == "activity-test-notification":
            self.action_test_notifications()
        elif button_id == "activity-undo-cleanup":
            self.action_undo_recent_cleanup()
        elif button_id == "activity-export":
            self.action_export()
        elif button_id == "activity-clear-history":
            self.action_clear_local_activity_history()


__all__ = [
    "ActivityClearPlanView",
    "ActivityClearResultView",
    "ActivityEventView",
    "ActivityScreen",
    "ActivityScreenBackend",
    "ActivitySnapshotView",
    "ActivityUndoEntryView",
    "ActivityUndoPlanView",
    "ActivityUndoResultView",
    "WatcherStatusView",
]
