"""Mailbox quota, storage analytics, and cleanup-plan TUI screens."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Event
from typing import ClassVar, Protocol, cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import Button, Collapsible, Label, Static

from gucmail.tui.area_common import (
    OperationalScreen,
    ProgressReporter,
    PublicOperationError,
    ViewState,
    clean_cell,
    current_row_highlight,
    human_bytes,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


@dataclass(frozen=True, slots=True)
class QuotaThresholdView:
    """One independently sourced Exchange quota threshold."""

    threshold_id: str
    label: str
    bytes_value: int | None
    source: str
    confidence: str = "unknown"
    observed_at: str | None = None
    user_override_bytes: int | None = None
    detail: str = ""

    @property
    def effective_bytes(self) -> int | None:
        return (
            self.user_override_bytes if self.user_override_bytes is not None else self.bytes_value
        )


@dataclass(frozen=True, slots=True)
class StorageUsageRow:
    row_id: str
    label: str
    category: str
    size_bytes: int | None
    item_count: int | None = None
    source: str = "server"
    details: str = ""
    cleanup_candidate: bool = False


@dataclass(frozen=True, slots=True)
class StorageSnapshotView:
    """Current usage and separate quota evidence from the strongest backend."""

    usage_bytes: int | None
    usage_source: str
    issue_warning: QuotaThresholdView
    prohibit_send: QuotaThresholdView
    prohibit_send_receive: QuotaThresholdView
    send_blocked: bool | None
    receive_blocked: bool | None
    total_capacity_bytes: int | None = None
    total_capacity_source: str = "unknown"
    total_capacity_is_fallback: bool = False
    usage_confidence: str = "unknown"
    usage_sample_count: int | None = None
    usage_sample_limit: int | None = None
    observed_at: str | None = None
    server_available: bool = True
    unavailable_reason: str | None = None
    overview_rows: tuple[StorageUsageRow, ...] = ()
    estimated_reclaimable_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class StorageAnalysisView:
    analysis_id: str
    title: str
    rows: tuple[StorageUsageRow, ...]
    bounded: bool = True
    limit: int = 100
    note: str = ""


@dataclass(frozen=True, slots=True)
class CleanupPlanEntryView:
    reference: str
    title: str
    folder: str
    size_bytes: int | None
    proposed_action: str
    recoverable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CleanupPlanView:
    plan_id: str
    entries: tuple[CleanupPlanEntryView, ...]
    estimated_reclaimable_bytes: int
    safety_margin_bytes: int = 0
    warnings: tuple[str, ...] = ()
    executable: bool = False
    confirmation_token: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class CleanupExecutionView:
    plan_id: str
    attempted_count: int
    succeeded_count: int
    failed_count: int
    stopped_early: bool
    undoable_count: int
    failed_references: tuple[str, ...] = ()
    uncertain_count: int = 0
    uncertain_references: tuple[str, ...] = ()
    recovery_failed_count: int = 0
    recovery_failed_references: tuple[str, ...] = ()
    not_started_count: int = 0
    operation_ids: tuple[str, ...] = ()
    journal_available: bool = False


class StorageScreenBackend(Protocol):
    """Bounded storage operations supplied by quota/insights services."""

    def load_storage(
        self,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> StorageSnapshotView: ...

    def analyze_storage(
        self,
        analysis_id: str,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> StorageAnalysisView: ...

    def create_cleanup_plan(
        self,
        analysis_id: str,
        candidate_references: tuple[str, ...],
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CleanupPlanView: ...

    def execute_cleanup_plan(
        self,
        plan: CleanupPlanView,
        *,
        confirmed: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> CleanupExecutionView: ...

    def export_statistics(
        self,
        snapshot: StorageSnapshotView,
        analysis: StorageAnalysisView | None,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> str: ...


class CleanupPlanScreen(OperationalScreen):
    """Exact cleanup preview with a separately confirmed recoverable executor."""

    DEFAULT_CSS = """
    CleanupPlanScreen { layout: vertical; }
    #cleanup-plan-root { width: 100%; height: 100%; padding: 1 2; }
    #cleanup-plan-title { text-style: bold; height: 2; }
    #cleanup-plan-summary-scroll { height: auto; min-height: 3; max-height: 7; }
    #cleanup-plan-summary { height: auto; }
    #cleanup-plan-table { height: 1fr; min-height: 3; }
    #cleanup-plan-detail-scroll { height: 7; border-top: solid $panel; padding: 1; }
    #cleanup-plan-detail { height: auto; }
    CleanupPlanScreen.workspace-narrow #cleanup-plan-summary-scroll { height: 4; }
    #cleanup-plan-actions { height: 3; align-horizontal: right; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """

    def __init__(
        self,
        plan: CleanupPlanView,
        backend: StorageScreenBackend,
        *,
        offline: bool,
    ) -> None:
        super().__init__(area_name="storage", offline=offline)
        self.plan = plan
        self.backend = backend
        self._entries = {entry.reference: entry for entry in plan.entries}
        self._execution_result: CleanupExecutionView | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="cleanup-plan-root"):
            yield Label("Cleanup plan", id="cleanup-plan-title")
            yield Static("", id="area-state", markup=False)
            with VerticalScroll(id="cleanup-plan-summary-scroll"):
                yield Static("", id="cleanup-plan-summary", markup=False)
            yield DataTable(id="cleanup-plan-table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="cleanup-plan-detail-scroll"):
                yield Static(
                    "Select a candidate for details.", id="cleanup-plan-detail", markup=False
                )
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="cleanup-plan-actions"):
                yield Button(
                    "Move to Deleted Items…",
                    id="cleanup-plan-execute",
                    variant="warning",
                    disabled=self.offline or not self.plan.executable or not self.plan.entries,
                )
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        summary = (
            f"Candidates: {len(self.plan.entries)} · Estimated reclaimable: "
            f"{human_bytes(self.plan.estimated_reclaimable_bytes)} · Safety margin: "
            f"{human_bytes(self.plan.safety_margin_bytes)}\n"
            "Analysis only. Nothing was moved or deleted. A separately reviewed and confirmed "
            "execution step is required."
        )
        if self.plan.warnings:
            summary += "\n" + "\n".join(
                f"Warning: {clean_cell(warning, max_length=300)}" for warning in self.plan.warnings
            )
        self.query_one("#cleanup-plan-summary", Static).update(summary)
        table = self.query_one("#cleanup-plan-table", DataTable)
        table.add_columns("Action", "Folder", "Title", "Size", "Recoverable")
        for entry in self.plan.entries:
            table.add_row(
                clean_cell(entry.proposed_action, max_length=30),
                clean_cell(entry.folder, max_length=70),
                clean_cell(entry.title, max_length=100),
                human_bytes(entry.size_bytes),
                "Yes" if entry.recoverable else "No",
                key=entry.reference,
            )
        if self.plan.entries:
            self._set_view_state(ViewState.READY, "Cleanup plan ready for review.")
            table.move_cursor(row=0)
            self._show_entry(self.plan.entries[0])
        else:
            self._set_view_state(
                ViewState.EMPTY, "No cleanup candidates met the analysis criteria."
            )

    @on(DataTable.RowHighlighted, "#cleanup-plan-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        entry = self._entries.get(str(event.row_key.value))
        if entry is not None:
            self._show_entry(entry)

    def _show_entry(self, entry: CleanupPlanEntryView) -> None:
        if self._execution_result is not None:
            return
        text = "\n".join(
            (
                clean_cell(entry.title, max_length=220) or "(untitled)",
                f"Reference: {clean_cell(entry.reference, max_length=180)}",
                f"Proposed action: {clean_cell(entry.proposed_action)}",
                f"Estimated size: {human_bytes(entry.size_bytes)}",
                f"Recoverable action: {'Yes' if entry.recoverable else 'No'}",
                f"Reason: {clean_cell(entry.reason, max_length=500)}",
            )
        )
        self.query_one("#cleanup-plan-detail", Static).update(text)

    @on(Button.Pressed, "#cleanup-plan-execute")
    def _execute_pressed(self) -> None:
        if self.offline or not self.plan.executable:
            self._show_error(
                "Execute cleanup plan",
                PublicOperationError(
                    "A live recoverable cleanup executor is unavailable.", retryable=False
                ),
            )
            return
        references = "\n".join(
            f"  move · {clean_cell(entry.reference, max_length=160)} · "
            f"{human_bytes(entry.size_bytes)}"
            for entry in self.plan.entries
        )
        self._confirm(
            "Execute this exact recoverable cleanup plan?",
            f"Plan: {clean_cell(self.plan.plan_id, max_length=100)}\n"
            f"Candidates: {len(self.plan.entries)}\n"
            f"Estimated reclaimable: {human_bytes(self.plan.estimated_reclaimable_bytes)}\n\n"
            f"{references}\n\n"
            "Each item is moved to Deleted Items, never permanently deleted. Successful "
            "moves receive a short-lived local undo journal entry. A provider timeout can "
            "leave the last item uncertain, so execution stops at the first error.",
            self._start_execution,
            confirm_label="Move exact items",
        )

    def _start_execution(self) -> None:
        self._begin_operation(
            "Executing cleanup plan",
            lambda cancel, progress: self.backend.execute_cleanup_plan(
                self.plan,
                confirmed=True,
                cancel=cancel,
                progress=progress,
            ),
            self._execution_finished,
            timeout_seconds=max(120.0, self.operation_timeout_seconds),
        )

    def _execution_finished(self, result: object) -> None:
        if not isinstance(result, CleanupExecutionView):
            raise PublicOperationError("The storage backend returned an invalid cleanup result.")
        self._execution_result = result
        summary = (
            f"Reviewed candidates: {len(self.plan.entries)}. "
            "Accepted moves remain in Deleted Items; no permanent delete was requested."
        )
        if result.uncertain_count:
            summary += (
                " Some moves may have completed. Do not repeat them; "
                "review Activity and Deleted Items."
            )
        if result.recovery_failed_count:
            summary += (
                " A recovery write failed after acceptance. Review the operation details below."
            )
        self.query_one("#cleanup-plan-summary", Static).update(summary)
        lines = [
            f"Attempted: {result.attempted_count}",
            f"Moved: {result.succeeded_count}",
            f"Failed: {result.failed_count}",
            f"Uncertain: {result.uncertain_count}",
            f"Not started: {result.not_started_count}",
            f"Undoable journal entries: {result.undoable_count}",
            f"Recovery write failures: {result.recovery_failed_count}",
        ]
        lines.extend(
            f"Failed reference: {clean_cell(reference, max_length=180)}"
            for reference in result.failed_references
        )
        lines.extend(
            f"Uncertain reference: {clean_cell(reference, max_length=180)}"
            for reference in result.uncertain_references
        )
        if result.uncertain_count:
            lines.append(
                "These moves may have completed. Do not repeat them; inspect Deleted Items."
            )
        if result.recovery_failed_count:
            lines.append(
                "A move was accepted, but its recovery record is incomplete. Do not repeat it."
            )
        if result.journal_available and result.operation_ids:
            lines.append("Review the durable operations in Activity → Operation details:")
            lines.extend(clean_cell(value) for value in result.operation_ids)
        self.query_one("#cleanup-plan-detail", Static).update("\n".join(lines))
        state = (
            ViewState.ERROR
            if result.failed_count or result.uncertain_count or result.recovery_failed_count
            else ViewState.READY
        )
        self._set_view_state(
            state,
            f"Cleanup {'stopped' if result.stopped_early else 'completed'}: "
            f"{result.succeeded_count} confirmed moved, {result.failed_count} failed, "
            f"{result.uncertain_count} uncertain. No permanent deletes were requested.",
        )
        self.query_one("#cleanup-plan-execute", Button).disabled = True


def storage_battery(used_bytes: int, capacity_bytes: int, *, width: int = 20) -> str:
    """Return a stable battery-style meter while preserving over-capacity percentages."""

    if width < 4:
        raise ValueError("Storage battery width must be at least four cells.")
    if used_bytes < 0 or capacity_bytes <= 0:
        raise ValueError("Storage battery values must be non-negative with positive capacity.")
    ratio = used_bytes / capacity_bytes
    filled = min(width, max(0, round(ratio * width)))
    return f"[{'█' * filled}{'░' * (width - filled)}]"


class StorageScreen(OperationalScreen):
    """Detailed storage area with stepped thresholds and bounded analytics."""

    ANALYSES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Mailbox cleanup overview", "mailbox-overview"),
        ("Storage by folder", "folders"),
        ("Largest folders", "largest-folders"),
        ("Largest messages", "largest-messages"),
        ("Largest attachments", "largest-attachments"),
        ("Duplicate messages", "duplicates"),
        ("Duplicate attachments", "duplicate-attachments"),
        ("Deleted Items", "deleted-items"),
        ("Recoverable Items", "recoverable-items"),
        ("Storage by sender", "senders"),
        ("Storage by date", "dates"),
        ("Storage by attachment type", "attachment-types"),
        ("Local cache / index / backups", "local"),
    )
    DEFAULT_CSS = """
    StorageScreen { layout: vertical; }
    #storage-root { width: 100%; height: 100%; padding: 1 2; }
    #storage-title { text-style: bold; height: 2; }
    #storage-summary { height: auto; min-height: 5; max-height: 9; }
    #storage-body { height: 1fr; min-height: 17; layout: vertical; }
    #storage-quota-pane { width: 100%; height: 8; }
    #storage-analysis-pane { width: 100%; height: 1fr; }
    #storage-threshold-table, #storage-analysis-table { height: 1fr; min-height: 7; }
    #storage-analysis-select { height: 3; }
    #storage-detail { height: 7; border-top: solid $panel; padding: 1; }
    #storage-actions { height: auto; min-height: 3; }
    #storage-actions Button { margin-right: 1; }
    #storage-footer-actions { height: 3; align-horizontal: right; }
    #storage-footer-actions Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    StorageScreen.compact-storage #storage-root { padding: 1; }
    StorageScreen.compact-storage #storage-summary { min-height: 7; max-height: 10; }
    StorageScreen.compact-storage #storage-body {
        layout: vertical;
        height: auto;
        min-height: 18;
    }
    StorageScreen.compact-storage #storage-quota-pane {
        width: 100%;
        height: 8;
        margin-right: 0;
    }
    StorageScreen.compact-storage #storage-analysis-pane { width: 100%; height: 10; }
    StorageScreen.compact-storage #storage-detail { height: 5; }
    StorageScreen.compact-storage #storage-footer-actions {
        dock: bottom;
        background: $footer-background;
    }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("a", "analyze", "Analyze", show=False),
        Binding("p", "cleanup_plan", "Cleanup plan", show=False),
    ]

    def __init__(
        self,
        backend: StorageScreenBackend,
        *,
        offline: bool = False,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
        analysis_limit: int = 100,
    ) -> None:
        super().__init__(
            area_name="storage",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if not 1 <= analysis_limit <= 1_000:
            raise ValueError("Storage analysis limit must be between 1 and 1000.")
        self.backend = backend
        self.analysis_limit = analysis_limit
        self._snapshot: StorageSnapshotView | None = None
        self._analysis: StorageAnalysisView | None = None
        self._rows: dict[str, StorageUsageRow] = {}
        self._selected_row: str | None = None
        self._pending_focus_label: str | None = None
        self._queued_analysis: tuple[str | None, str | None] | None = None
        self._queued_snapshot_focus: str | None = None
        self._queued_cleanup_plan = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="storage-root"):
            yield Label("Mailbox Storage", id="storage-title")
            yield Static("Loading storage information…", id="area-state", markup=False)
            yield Static("", id="storage-overview", markup=False)
            with Collapsible(title="Limits", id="storage-evidence"):
                yield Static("", id="storage-summary", markup=False)
                with Vertical(id="storage-quota-pane"):
                    yield DataTable(
                        id="storage-threshold-table", cursor_type="row", zebra_stripes=True
                    )
            with Horizontal(id="storage-actions"):
                yield Button("Analyze", id="storage-analyze", variant="primary")
                yield Button("Create cleanup plan", id="storage-cleanup-plan", disabled=True)
                yield Button("Export statistics", id="storage-export")
            with Horizontal(id="storage-body"), Vertical(id="storage-analysis-pane"):
                yield Select(
                    self.ANALYSES,
                    value="folders",
                    allow_blank=False,
                    id="storage-analysis-select",
                )
                yield DataTable(
                    id="storage-analysis-table",
                    cursor_type="row",
                    zebra_stripes=True,
                )
            yield Static(
                "Select a storage row for its source and details.",
                id="storage-detail",
                markup=False,
            )
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="storage-footer-actions"):
                retry = Button("Retry", id="area-retry")
                retry.display = False
                yield retry
                cancel = Button("Cancel operation", id="area-cancel-operation")
                cancel.display = False
                yield cancel
                yield Button("Refresh", id="storage-refresh")
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.set_class(self.size.width < 100 or self.size.height < 30, "compact-storage")
        table = self.query_one("#storage-threshold-table", DataTable)
        for label, key, width in (
            ("Threshold", "threshold", 22),
            ("Active value", "value", 12),
            ("Source", "source", max(12, self.size.width - 62)),
            ("Confidence", "confidence", 14),
        ):
            table.add_column(label, key=key, width=width)
        self.query_one("#storage-analysis-table", DataTable).add_columns(
            "Item", "Where", "Size", "Count"
        )
        self.call_after_refresh(self.action_refresh)

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 100 or event.size.height < 30, "compact-storage")
        self.call_after_refresh(self._fit_threshold_columns)

    def _fit_threshold_columns(self) -> None:
        table = self.query_one("#storage-threshold-table", DataTable)
        for key, column in table.columns.items():
            if key.value == "source":
                column.width = max(12, table.size.width - 58)
                column.auto_width = False
        table.refresh(layout=True)

    @on(DataTable.RowHighlighted, "#storage-threshold-table")
    def _threshold_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._snapshot is None or not current_row_highlight(event):
            return
        for threshold in (
            self._snapshot.issue_warning,
            self._snapshot.prohibit_send,
            self._snapshot.prohibit_send_receive,
        ):
            if threshold.threshold_id == str(event.row_key.value):
                self.query_one("#storage-detail", Static).update(
                    f"{clean_cell(threshold.label)} {human_bytes(threshold.effective_bytes)}"
                    if threshold.effective_bytes is not None
                    else f"{clean_cell(threshold.label)}: not reported by the server"
                )
                break

    def action_refresh(self) -> None:
        self._begin_operation(
            "Refreshing storage",
            lambda cancel, progress: self.backend.load_storage(
                cancel=cancel,
                progress=progress,
            ),
            self._storage_loaded,
            retry_safe=True,
        )

    def _storage_loaded(self, result: object) -> None:
        if not isinstance(result, StorageSnapshotView):
            raise PublicOperationError("The storage backend returned an invalid snapshot.")
        self._snapshot = result
        self._render_snapshot(result)
        self._render_rows(result.overview_rows)
        if self._queued_snapshot_focus is not None:
            # DataTable emits its initial RowHighlighted event after rows are rendered.
            # Apply command-specific quota focus after that event so it cannot be
            # immediately replaced by the first local-storage row's detail.
            self.set_timer(0.05, self._apply_queued_snapshot_focus)
        if not result.server_available:
            fallback_message = "The server doesn't report your quota" + (
                ". Showing an estimate." if result.usage_sample_count is not None else "."
            )
            self._set_view_state(
                ViewState.OFFLINE if self.offline else ViewState.READY,
                fallback_message,
            )
        elif result.overview_rows:
            self._set_view_state(
                ViewState.READY,
                "Mailbox storage",
            )
        else:
            self._set_view_state(
                ViewState.EMPTY,
                "Choose a view and press Analyze to see what uses space.",
            )
        queued = self._queued_analysis
        self._queued_analysis = None
        if queued is not None:
            analysis_id, focus_label = queued
            self.set_timer(
                0.01,
                lambda: self.action_analyze(analysis_id, focus_label=focus_label),
            )

    def queue_snapshot_focus(self, section: str) -> None:
        """Expose the exact quota/usage/remaining detail requested by a command."""

        if section not in {"quota_thresholds", "mailbox_usage", "remaining_space"}:
            return
        self._queued_snapshot_focus = section
        if self._snapshot is not None:
            self.set_timer(0.05, self._apply_queued_snapshot_focus)

    def _apply_queued_snapshot_focus(self) -> None:
        section, self._queued_snapshot_focus = self._queued_snapshot_focus, None
        if section is not None:
            self._show_snapshot_focus(section)

    def _show_snapshot_focus(self, section: str) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        if section == "quota_thresholds":
            self.query_one("#storage-evidence", Collapsible).collapsed = False
        if section == "quota_thresholds":
            capacity_label = (
                "Configured fallback capacity"
                if snapshot.total_capacity_is_fallback
                else "Server-reported total capacity"
            )
            lines = [
                f"{capacity_label}: {human_bytes(snapshot.total_capacity_bytes)}",
                f"Capacity source: {clean_cell(snapshot.total_capacity_source)}",
            ]
            lines.extend(
                f"{threshold.label}: {human_bytes(threshold.effective_bytes)} · "
                f"{clean_cell(threshold.source)} · {clean_cell(threshold.confidence)}"
                for threshold in (
                    snapshot.issue_warning,
                    snapshot.prohibit_send,
                    snapshot.prohibit_send_receive,
                )
            )
            if snapshot.total_capacity_is_fallback:
                lines.append("This editable capacity is not an Exchange-reported quota.")
        elif section == "mailbox_usage":
            lines = [
                f"Current mailbox usage: {human_bytes(snapshot.usage_bytes)}",
                f"Source: {clean_cell(snapshot.usage_source)}",
                f"Confidence: {clean_cell(snapshot.usage_confidence)}",
            ]
            if snapshot.usage_sample_count is not None:
                lines.append(
                    f"Estimate covers {snapshot.usage_sample_count} item(s)"
                    + (
                        f" of scan limit {snapshot.usage_sample_limit}"
                        if snapshot.usage_sample_limit is not None
                        else ""
                    )
                    + "; hidden/recoverable data may increase actual usage."
                )
        else:
            total = snapshot.total_capacity_bytes
            used = snapshot.usage_bytes
            if total is None or used is None or total <= 0:
                lines = [
                    "Remaining mailbox capacity: Unknown",
                    "Usage and an effective capacity are both required for this calculation.",
                ]
            else:
                remaining = total - used
                percent = used / total * 100
                lines = [
                    f"Used: {human_bytes(used)} of {human_bytes(total)} ({percent:.1f}%)",
                    f"Remaining: {human_bytes(max(remaining, 0))}",
                    f"Capacity source: {clean_cell(snapshot.total_capacity_source)}",
                    storage_battery(used, total),
                ]
                if remaining < 0:
                    lines.append(f"Over configured capacity by {human_bytes(abs(remaining))}.")
        self.query_one("#storage-detail", Static).update("\n".join(lines))

    def queue_cleanup_plan(self) -> None:
        """Analyze safe candidates first, then open the exact cleanup-plan preview."""

        self._queued_cleanup_plan = True
        if self._analysis is not None and any(row.cleanup_candidate for row in self._analysis.rows):
            self._queued_cleanup_plan = False
            self.action_cleanup_plan()
        elif self._snapshot is None:
            self._queued_analysis = ("largest-messages", None)
        else:
            self.action_analyze("largest-messages")

    def queue_analysis(
        self,
        analysis_id: str | None = None,
        *,
        focus_label: str | None = None,
    ) -> None:
        """Run an analysis after the initial quota/storage snapshot is ready."""

        if self._snapshot is None:
            self._queued_analysis = (analysis_id, focus_label)
            return
        self.action_analyze(analysis_id, focus_label=focus_label)

    def _render_snapshot(self, snapshot: StorageSnapshotView) -> None:
        thresholds = (
            snapshot.issue_warning,
            snapshot.prohibit_send,
            snapshot.prohibit_send_receive,
        )
        table = self.query_one("#storage-threshold-table", DataTable)
        table.clear()
        for threshold in thresholds:
            source = threshold.source
            if threshold.user_override_bytes is not None:
                source = f"User override; observed {human_bytes(threshold.bytes_value)}"
            table.add_row(
                clean_cell(threshold.label, max_length=50),
                human_bytes(threshold.effective_bytes),
                clean_cell(source, max_length=80) or "Unknown",
                clean_cell(threshold.confidence, max_length=30) or "Unknown",
                key=threshold.threshold_id,
            )

        usage = human_bytes(snapshot.usage_bytes)
        total = snapshot.total_capacity_bytes
        capacity_label = (
            "Configured fallback capacity"
            if snapshot.total_capacity_is_fallback
            else "Server-reported total capacity"
        )
        if snapshot.usage_bytes is not None and total is not None:
            remaining = total - snapshot.usage_bytes
            percent = max(snapshot.usage_bytes / total * 100, 0.0) if total > 0 else 0.0
            battery = storage_battery(snapshot.usage_bytes, total)
            total_line = (
                f"{capacity_label}: {human_bytes(total)} · "
                f"used: {percent:.1f}% {battery} · "
                f"remaining: {human_bytes(max(0, remaining))}"
                + (f" · over by {human_bytes(abs(remaining))}" if remaining < 0 else "")
                + f" · Source: {clean_cell(snapshot.total_capacity_source)}"
            )
        elif total is not None:
            total_line = (
                f"{capacity_label}: {human_bytes(total)} · usage/remaining unknown · "
                f"Source: {clean_cell(snapshot.total_capacity_source)}"
            )
        else:
            total_line = "Reported total capacity and remaining capacity: Unknown"
        send_threshold = snapshot.prohibit_send.effective_bytes
        if snapshot.usage_bytes is not None and send_threshold:
            ratio = snapshot.usage_bytes / send_threshold * 100
            delta = snapshot.usage_bytes - send_threshold
            relation = (
                f"{human_bytes(abs(delta))} above the sending threshold"
                if delta >= 0
                else f"{human_bytes(abs(delta))} below the sending threshold"
            )
            send_line = f"Usage relative to send threshold: {ratio:.1f}% · {relation}"
        else:
            send_line = "Usage relative to send threshold: Unknown"
        send_status = (
            "SEND BLOCKED"
            if snapshot.send_blocked is True
            else "Sending not detected as blocked"
            if snapshot.send_blocked is False
            else "Sending status unknown"
        )
        receive_status = (
            "RECEIVE BLOCKED"
            if snapshot.receive_blocked is True
            else "Receiving not detected as blocked"
            if snapshot.receive_blocked is False
            else "Receiving threshold/status unknown"
        )
        summary = (
            f"Current usage: {usage} · Source: {clean_cell(snapshot.usage_source)} · "
            f"Confidence: {clean_cell(snapshot.usage_confidence)}\n"
            f"{total_line}\n"
            f"Current status: {send_status} · {receive_status}\n"
            f"{send_line}\n"
            f"Estimated reclaimable space: {human_bytes(snapshot.estimated_reclaimable_bytes)}\n"
            f"Observed: {clean_cell(snapshot.observed_at or 'Unknown')}"
        )
        if snapshot.usage_sample_count is not None:
            summary += (
                "\nEstimate coverage: "
                f"{snapshot.usage_sample_count} visible items"
                + (
                    f" (scan limit {snapshot.usage_sample_limit})"
                    if snapshot.usage_sample_limit is not None
                    else ""
                )
                + "; hidden/recoverable data may increase actual usage."
            )
        if snapshot.unavailable_reason:
            summary += f"\nServer note: {clean_cell(snapshot.unavailable_reason, max_length=300)}"
        self.query_one("#storage-summary", Static).update(summary)
        if snapshot.usage_bytes is not None and send_threshold:
            left = max(send_threshold - snapshot.usage_bytes, 0)
            percent = round(100 * snapshot.usage_bytes / send_threshold)
            overview = [
                f"Used  {usage} of {human_bytes(send_threshold)} ({percent}%)    ·    "
                f"Left  {human_bytes(left)}"
            ]
        else:
            overview = [f"Used  {usage}"]
        if snapshot.usage_sample_count is not None:
            overview.append(f"Estimated from {snapshot.usage_sample_count} messages")
        if snapshot.send_blocked is True:
            overview.append("Sending is blocked by the mailbox quota.")
        if snapshot.receive_blocked is True:
            overview.append("Receiving is blocked by the mailbox quota.")
        if snapshot.usage_bytes is not None and send_threshold:
            overview.append(storage_battery(snapshot.usage_bytes, send_threshold))
        self.query_one("#storage-overview", Static).update("\n".join(overview))

    def _render_rows(self, rows: Sequence[StorageUsageRow]) -> None:
        self._rows = {row.row_id: row for row in rows}
        table = self.query_one("#storage-analysis-table", DataTable)
        table.clear()
        for row in rows:
            table.add_row(
                clean_cell(row.label, max_length=100),
                _where(row.category),
                human_bytes(row.size_bytes),
                str(row.item_count) if row.item_count is not None else "",
                key=row.row_id,
            )
        if rows:
            focused_index = self._focused_row_index(rows, self._pending_focus_label)
            focused_row = rows[focused_index]
            self._selected_row = focused_row.row_id
            table.move_cursor(row=focused_index)
            self._show_row(focused_row)
        else:
            self._selected_row = None
            self.query_one("#storage-detail", Static).update("Nothing to show for this view.")
        self._pending_focus_label = None
        self._update_cleanup_button()

    @staticmethod
    def _focused_row_index(
        rows: Sequence[StorageUsageRow],
        focus_label: str | None,
    ) -> int:
        if not focus_label:
            return 0

        def normalized(value: str) -> str:
            return value.strip().replace("\\", "/").strip("/").casefold()

        wanted = normalized(focus_label)
        exact = [index for index, row in enumerate(rows) if normalized(row.label) == wanted]
        if exact:
            return exact[0]

        wanted_leaf = wanted.rsplit("/", 1)[-1]
        leaf_matches = [
            index
            for index, row in enumerate(rows)
            if normalized(row.label).rsplit("/", 1)[-1] == wanted_leaf
        ]
        return leaf_matches[0] if len(leaf_matches) == 1 else 0

    @on(DataTable.RowHighlighted, "#storage-analysis-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        row = self._rows.get(str(event.row_key.value))
        if row is None:
            return
        self._selected_row = row.row_id
        self._show_row(row)
        self._update_cleanup_button()

    def _show_row(self, row: StorageUsageRow) -> None:
        self.query_one("#storage-detail", Static).update(
            "\n".join(
                (
                    clean_cell(row.label, max_length=220),
                    f"Where: {_where(row.category)}",
                    f"Size: {human_bytes(row.size_bytes)}"
                    + (f" · {row.item_count} items" if row.item_count is not None else ""),
                    "Can be cleaned up" if row.cleanup_candidate else "",
                    clean_cell(row.details, max_length=500),
                )
            )
        )

    def _analysis_id(self) -> str:
        value = self.query_one("#storage-analysis-select", Select).value
        if value is Select.BLANK:
            raise PublicOperationError("Choose a storage analysis.", retryable=False)
        return str(value)

    def action_analyze(
        self,
        analysis_id: str | None = None,
        *,
        focus_label: str | None = None,
    ) -> None:
        if analysis_id is not None:
            allowed = {value for _label, value in self.ANALYSES}
            if analysis_id not in allowed:
                self._show_error(
                    "Analyze storage",
                    PublicOperationError(
                        f"Unknown storage analysis: {clean_cell(analysis_id)}",
                        retryable=False,
                    ),
                )
                return
            self.query_one("#storage-analysis-select", Select).value = analysis_id
        self._pending_focus_label = focus_label
        try:
            analysis_id = self._analysis_id()
        except PublicOperationError as exc:
            self._show_error("Analyze storage", exc)
            return
        self._begin_operation(
            "Analyzing storage",
            lambda cancel, progress: self.backend.analyze_storage(
                analysis_id,
                limit=self.analysis_limit,
                cancel=cancel,
                progress=progress,
            ),
            self._analysis_loaded,
            timeout_seconds=max(60.0, self.operation_timeout_seconds),
            retry_safe=True,
        )

    def _analysis_loaded(self, result: object) -> None:
        if not isinstance(result, StorageAnalysisView):
            raise PublicOperationError("The storage backend returned an invalid analysis.")
        self._analysis = result
        self._render_rows(result.rows)
        message = (
            f"{clean_cell(result.title)}: {len(result.rows)} rows."
            if result.rows
            else f"{clean_cell(result.title)} returned no rows."
        )
        if result.note:
            message += f" {clean_cell(result.note, max_length=300)}"
        reclaimable = sum(
            max(row.size_bytes or 0, 0) for row in result.rows if row.cleanup_candidate
        )
        if reclaimable:
            message += f" Estimated reclaimable from review candidates: {human_bytes(reclaimable)}."
        self._set_view_state(ViewState.READY if result.rows else ViewState.EMPTY, message)
        if self._queued_cleanup_plan:
            self._queued_cleanup_plan = False
            self.set_timer(0.01, self.action_cleanup_plan)

    def _update_cleanup_button(self) -> None:
        row = self._rows.get(self._selected_row or "")
        self.query_one("#storage-cleanup-plan", Button).disabled = not (
            self._analysis is not None and row is not None and row.cleanup_candidate
        )

    def action_cleanup_plan(self) -> None:
        if self._analysis is None:
            self._show_error(
                "Create cleanup plan",
                PublicOperationError("Run a storage analysis first.", retryable=False),
            )
            return
        candidate_ids = tuple(row.row_id for row in self._analysis.rows if row.cleanup_candidate)
        if not candidate_ids:
            self._show_error(
                "Create cleanup plan",
                PublicOperationError(
                    "This analysis has no cleanup candidates. Nothing was changed.",
                    retryable=False,
                ),
            )
            return
        analysis_id = self._analysis.analysis_id
        self._begin_operation(
            "Creating cleanup plan",
            lambda cancel, progress: self.backend.create_cleanup_plan(
                analysis_id,
                candidate_ids,
                cancel=cancel,
                progress=progress,
            ),
            self._cleanup_plan_loaded,
            timeout_seconds=max(60.0, self.operation_timeout_seconds),
            retry_safe=True,
        )

    def _cleanup_plan_loaded(self, result: object) -> None:
        if not isinstance(result, CleanupPlanView):
            raise PublicOperationError("The storage backend returned an invalid cleanup plan.")
        self._set_view_state(
            ViewState.READY,
            "Cleanup plan ready. Nothing was moved or deleted.",
        )
        self.app.push_screen(CleanupPlanScreen(result, self.backend, offline=self.offline))

    def action_export(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            self._show_error(
                "Export statistics",
                PublicOperationError("Refresh storage before exporting.", retryable=False),
            )
            return
        self._begin_operation(
            "Exporting storage statistics",
            lambda cancel, progress: self.backend.export_statistics(
                snapshot,
                self._analysis,
                cancel=cancel,
                progress=progress,
            ),
            self._exported,
        )

    def _exported(self, result: object) -> None:
        path = cast(str, result)
        if not isinstance(path, str) or not path.strip():
            raise PublicOperationError("The storage backend did not return an export location.")
        self._set_view_state(
            ViewState.READY,
            f"Storage statistics exported to {clean_cell(path, max_length=400)}.",
        )

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "storage-refresh":
            self.action_refresh()
        elif button_id == "storage-analyze":
            self.action_analyze()
        elif button_id == "storage-cleanup-plan":
            self.action_cleanup_plan()
        elif button_id == "storage-export":
            self.action_export()


__all__ = [
    "CleanupExecutionView",
    "CleanupPlanEntryView",
    "CleanupPlanScreen",
    "CleanupPlanView",
    "QuotaThresholdView",
    "StorageAnalysisView",
    "StorageScreen",
    "StorageScreenBackend",
    "StorageSnapshotView",
    "StorageUsageRow",
]


def _where(category: str) -> str:
    labels = {"local": "This computer", "server": "Mailbox", "mailbox": "Mailbox"}
    return labels.get(category.casefold(), clean_cell(category.capitalize(), max_length=40))
