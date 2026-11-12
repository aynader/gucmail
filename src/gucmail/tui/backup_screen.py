"""Operational backup center for the Textual interface."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, ClassVar, Protocol, cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, SelectionList, Static

from gucmail.tui.area_common import (
    AreaAction,
    OfflineOperationError,
    OperationalScreen,
    OperationProgress,
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
class BackupRecordView:
    """Presentation-safe summary of one on-disk backup."""

    backup_id: str
    kind: str
    state: str
    created_at: str | None
    completed_at: str | None
    item_count: int
    stored_bytes: int
    destination: str
    encrypted: bool
    verification_state: str = "not verified"
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class BackupCreateRequest:
    """User choices for an explicit backup operation."""

    destination: str
    kind: str
    folders: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()
    content_types: tuple[str, ...] = ()
    encrypted: bool = False
    verify_after_create: bool = True
    parent_backup_id: str | None = None


@dataclass(frozen=True, slots=True)
class BackupEstimateView:
    item_count: int
    attachment_count: int
    estimated_bytes: int
    unknown_size_count: int = 0
    note: str = ""


@dataclass(frozen=True, slots=True)
class BackupVerificationView:
    backup_id: str
    valid: bool
    complete: bool
    checked_objects: int
    checked_bytes: int
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BackupItemView:
    reference: str
    kind: str
    folder: str
    title: str
    timestamp: str | None = None
    size_bytes: int | None = None
    attachment_count: int = 0
    details: str = ""


@dataclass(frozen=True, slots=True)
class RestorePlanEntryView:
    reference: str
    kind: str
    destination: str
    action: str
    duplicate_reason: str | None = None
    warnings: tuple[str, ...] = ()
    recovery_status: str | None = None
    recovery_operation_id: str | None = None
    item_key: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class RestorePlanView:
    backup_id: str
    entries: tuple[RestorePlanEntryView, ...]
    create_count: int
    duplicate_count: int
    unsupported_count: int
    warnings: tuple[str, ...] = ()
    requires_confirmation: bool = True
    encrypted: bool = False
    confirmation_token: str = field(default="", repr=False)
    blocked_count: int = 0
    allow_completed_duplicates: bool = False


@dataclass(frozen=True, slots=True)
class RestoreResultView:
    backup_id: str
    attempted_count: int
    restored_count: int
    failed_count: int
    duplicate_count: int
    unsupported_count: int
    stopped_early: bool
    server_mutations_performed: bool
    failures: tuple[str, ...] = ()
    mutation_uncertain: bool = False
    cancelled: bool = False
    interrupted: bool = False
    operation_id: str | None = None
    journal_available: bool = False
    recovery_write_failed: bool = False
    blocked_count: int = 0
    uncertain_count: int = 0


@dataclass(frozen=True, slots=True)
class BackupPrunePlanView:
    candidates: tuple[str, ...]
    retained: tuple[str, ...]
    protected_ancestors: tuple[str, ...]
    potential_bytes: int
    keep_last: int
    confirmation_token: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class BackupPruneResultView:
    pruned_ids: tuple[str, ...]
    bytes_moved: int
    note: str


class BackupScreenBackend(Protocol):
    """Blocking backup operations executed outside the Textual event loop."""

    def list_backups(
        self,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[BackupRecordView]: ...

    def estimate_backup(
        self,
        request: BackupCreateRequest,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupEstimateView: ...

    def create_backup(
        self,
        request: BackupCreateRequest,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupRecordView: ...

    def resume_backup(
        self,
        backup_id: str,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupRecordView: ...

    def verify_backup(
        self,
        backup_id: str,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupVerificationView: ...

    def browse_backup(
        self,
        backup_id: str,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[BackupItemView]: ...

    def create_restore_plan(
        self,
        backup_id: str,
        item_references: tuple[str, ...],
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> RestorePlanView: ...

    def execute_restore(
        self,
        plan: RestorePlanView,
        *,
        confirmed: bool,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> RestoreResultView: ...

    def export_backup(
        self,
        backup_id: str,
        destination: str,
        *,
        passphrase: str | bytes | None = None,
        cancel: Event,
        progress: ProgressReporter,
    ) -> str: ...

    def plan_prune(
        self,
        keep_last: int,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupPrunePlanView: ...

    def execute_prune(
        self,
        plan: BackupPrunePlanView,
        *,
        confirmed: bool,
        cancel: Event,
        progress: ProgressReporter,
    ) -> BackupPruneResultView: ...

    def open_backup_folder(
        self,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> str: ...


class BackupPassphraseScreen(ModalScreen[str | None]):
    """Ephemeral masked passphrase prompt; its value is never persisted or rendered."""

    DEFAULT_CSS = """
    BackupPassphraseScreen { align: center middle; background: $background 75%; }
    #backup-passphrase-dialog {
        width: 92%; max-width: 72; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    #backup-passphrase-actions { height: 3; align-horizontal: right; }
    #backup-passphrase-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, purpose: str) -> None:
        super().__init__()
        self.purpose = clean_cell(purpose, max_length=120)

    def compose(self) -> ComposeResult:
        with Vertical(id="backup-passphrase-dialog"):
            yield Label("Encrypted backup passphrase")
            yield Static(
                f"Enter the passphrase to {self.purpose}. It is kept only for this operation.",
                markup=False,
            )
            yield Input(password=True, id="backup-passphrase-input")
            with Horizontal(id="backup-passphrase-actions"):
                yield Button("Cancel", id="backup-passphrase-cancel")
                yield Button("Continue", id="backup-passphrase-continue", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#backup-passphrase-input", Input).focus()

    def _finish(self) -> None:
        value = self.query_one("#backup-passphrase-input", Input).value
        self.dismiss(value if value else None)

    @on(Input.Submitted, "#backup-passphrase-input")
    def _submitted(self) -> None:
        self._finish()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "backup-passphrase-continue":
            self._finish()
        elif event.button.id == "backup-passphrase-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class BackupExportPathScreen(ModalScreen[str | None]):
    """Explicit destination prompt for a collision-safe portable backup export."""

    DEFAULT_CSS = """
    BackupExportPathScreen { align: center middle; background: $background 75%; }
    #backup-export-dialog {
        width: 94%; max-width: 88; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    #backup-export-actions { height: 3; align-horizontal: right; }
    #backup-export-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, default_destination: str) -> None:
        super().__init__()
        self.default_destination = clean_cell(default_destination, max_length=700)

    def compose(self) -> ComposeResult:
        with Vertical(id="backup-export-dialog"):
            yield Label("Export verified backup archive")
            yield Static(
                "Choose a new .zip path. Existing files are never overwritten and the "
                "archive is fully verified before export.",
                markup=False,
            )
            yield Input(value=self.default_destination, id="backup-export-destination")
            with Horizontal(id="backup-export-actions"):
                yield Button("Cancel", id="backup-export-cancel")
                yield Button("Export", id="backup-export-continue", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#backup-export-destination", Input).focus()

    def _finish(self) -> None:
        value = self.query_one("#backup-export-destination", Input).value.strip()
        self.dismiss(value or None)

    @on(Input.Submitted, "#backup-export-destination")
    def _submitted(self) -> None:
        self._finish()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "backup-export-continue":
            self._finish()
        elif event.button.id == "backup-export-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class BackupBrowseScreen(OperationalScreen):
    """Selectable, offline-safe view of bounded backup contents."""

    DEFAULT_CSS = """
    BackupBrowseScreen { layout: vertical; }
    #backup-browse-root { width: 100%; height: 100%; padding: 1 2; }
    #backup-browse-title { height: 2; text-style: bold; }
    #backup-browse-table { height: 1fr; min-height: 8; }
    #backup-browse-detail { height: 7; border-top: solid $panel; padding: 1; }
    #backup-browse-actions { height: 3; align-horizontal: right; }
    #backup-browse-actions Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("space", "toggle_selection", "Select item", show=False),
    ]

    def __init__(
        self,
        backup_id: str,
        items: Sequence[BackupItemView],
        *,
        backend: BackupScreenBackend | None = None,
        encrypted: bool = False,
        offline: bool = True,
    ) -> None:
        super().__init__(area_name="backups", offline=offline)
        self.backup_id = clean_cell(backup_id, max_length=160)
        self.items = tuple(items)
        self._items_by_reference = {item.reference: item for item in self.items}
        self.backend = backend
        self.encrypted = encrypted
        self._selected_references: set[str] = set()
        self._current_reference: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="backup-browse-root"):
            yield Label(
                f"Backup contents · {self.backup_id}", id="backup-browse-title", markup=False
            )
            yield Static("Offline backup view", id="area-state", markup=False)
            yield DataTable(
                id="backup-browse-table",
                cursor_type="row",
                zebra_stripes=True,
            )
            yield Static(
                "Select an item to inspect its safe metadata.",
                id="backup-browse-detail",
                markup=False,
            )
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="backup-browse-actions"):
                yield Button("Toggle selection", id="backup-browse-toggle", disabled=True)
                yield Button("Select all", id="backup-browse-select-all", disabled=not self.items)
                yield Button(
                    "Plan selected restore",
                    id="backup-browse-plan-selected",
                    variant="warning",
                    disabled=True,
                )
                yield Button("Close", classes="area-close", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#backup-browse-table", DataTable)
        table.add_column("Selected", key="selected")
        table.add_columns("Type", "Folder", "Title", "Date", "Size", "Attachments")
        for item in self.items:
            table.add_row(
                "[ ]",
                clean_cell(item.kind, max_length=30),
                clean_cell(item.folder, max_length=60),
                clean_cell(item.title, max_length=100),
                clean_cell(item.timestamp or "", max_length=40),
                human_bytes(item.size_bytes),
                str(item.attachment_count),
                key=item.reference,
            )
        if self.items:
            self._set_view_state(
                ViewState.READY, f"{len(self.items)} bounded items loaded offline."
            )
            table.move_cursor(row=0)
            self._current_reference = self.items[0].reference
            self._show_item(self.items[0])
            self._update_selection_controls()
        else:
            self._set_view_state(ViewState.EMPTY, "This backup contains no items in this view.")

    @on(DataTable.RowHighlighted, "#backup-browse-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        item = self._items_by_reference.get(str(event.row_key.value))
        if item is not None:
            self._current_reference = item.reference
            self._show_item(item)
            self._update_selection_controls()

    def _show_item(self, item: BackupItemView) -> None:
        text = "\n".join(
            (
                clean_cell(item.title, max_length=200) or "(untitled)",
                f"Reference: {clean_cell(item.reference, max_length=180)}",
                f"Folder: {clean_cell(item.folder, max_length=180) or '(none)'}",
                f"Stored size: {human_bytes(item.size_bytes)}",
                clean_cell(item.details, max_length=500),
            )
        )
        self.query_one("#backup-browse-detail", Static).update(text)

    def _update_selection_controls(self) -> None:
        self.query_one("#backup-browse-toggle", Button).disabled = (
            self._current_reference not in self._items_by_reference
        )
        selected_count = len(self._selected_references)
        self.query_one("#backup-browse-plan-selected", Button).disabled = (
            self.backend is None or selected_count == 0
        )
        select_all = self.query_one("#backup-browse-select-all", Button)
        select_all.label = (
            "Clear selection" if self.items and selected_count == len(self.items) else "Select all"
        )
        self._set_view_state(
            ViewState.READY if self.items else ViewState.EMPTY,
            f"Offline backup view · {selected_count} of {len(self.items)} item(s) selected.",
        )

    def _set_selected(self, reference: str, selected: bool) -> None:
        if reference not in self._items_by_reference:
            return
        if selected:
            self._selected_references.add(reference)
        else:
            self._selected_references.discard(reference)
        self.query_one("#backup-browse-table", DataTable).update_cell(
            reference,
            "selected",
            "[x]" if selected else "[ ]",
        )
        self._update_selection_controls()

    def action_toggle_selection(self) -> None:
        reference = self._current_reference
        if reference is not None:
            self._set_selected(reference, reference not in self._selected_references)

    def action_select_all(self) -> None:
        select = len(self._selected_references) != len(self.items)
        for item in self.items:
            self._set_selected(item.reference, select)

    def action_plan_selected_restore(self) -> None:
        if self.backend is None or not self._selected_references:
            self._show_error(
                "Plan selected restore",
                PublicOperationError("Select at least one backup item first.", retryable=False),
            )
            return

        def resolved(passphrase: str | None) -> None:
            if self.encrypted and not passphrase:
                self._set_view_state(ViewState.CANCELLED, "Restore planning cancelled safely.")
                return
            self._start_selected_plan(passphrase)

        if self.encrypted:
            self.app.push_screen(
                BackupPassphraseScreen("plan a restore for the selected items"),
                resolved,
            )
        else:
            resolved(None)

    def _start_selected_plan(self, passphrase: str | None) -> None:
        backend = self.backend
        if backend is None:
            return
        references = tuple(sorted(self._selected_references))
        self._begin_operation(
            "Planning selected restore",
            lambda cancel, progress: backend.create_restore_plan(
                self.backup_id,
                references,
                passphrase=passphrase,
                cancel=cancel,
                progress=progress,
            ),
            self._open_selected_plan,
            timeout_seconds=max(60.0, self.operation_timeout_seconds),
            retry_safe=True,
        )

    def _open_selected_plan(self, result: object) -> None:
        if not isinstance(result, RestorePlanView):
            raise PublicOperationError("The backup backend returned an invalid restore plan.")
        if self.backend is None:
            return
        self._set_view_state(
            ViewState.READY,
            f"Exact restore plan generated for {len(result.entries)} selected item(s).",
        )
        self.app.push_screen(RestorePlanScreen(result, self.backend, offline=self.offline))

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "backup-browse-toggle":
            self.action_toggle_selection()
        elif event.button.id == "backup-browse-select-all":
            self.action_select_all()
        elif event.button.id == "backup-browse-plan-selected":
            self.action_plan_selected_restore()


class BackupFolderBrowseScreen(OperationalScreen):
    """Folder-first, read-only navigation over one immutable local backup."""

    DEFAULT_CSS = """
    BackupFolderBrowseScreen { layout: vertical; }
    #backup-folder-root { width: 100%; height: 100%; padding: 1 2; }
    #backup-folder-title { height: 2; text-style: bold; }
    #backup-folder-source { height: auto; min-height: 2; color: $text-muted; }
    #backup-folder-table { height: 1fr; min-height: 8; }
    #backup-folder-detail { height: 7; border-top: solid $panel; padding: 1; }
    #backup-folder-actions { height: 3; align-horizontal: right; }
    #backup-folder-actions Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("enter", "open_folder", "Open folder", show=False),
    ]

    def __init__(
        self,
        backup_id: str,
        items: Sequence[BackupItemView],
        *,
        backend: BackupScreenBackend | None = None,
        encrypted: bool = False,
        requested_folder: str | None = None,
    ) -> None:
        super().__init__(area_name="backups", offline=True)
        self.backup_id = clean_cell(backup_id, max_length=160)
        self.items = tuple(items)
        self.backend = backend
        self.encrypted = encrypted
        grouped: dict[str, list[BackupItemView]] = {}
        for item in self.items:
            grouped.setdefault(item.folder.strip() or "(no folder)", []).append(item)
        self._folders = {
            f"folder:{index}": (folder, tuple(values))
            for index, (folder, values) in enumerate(
                sorted(grouped.items(), key=lambda pair: pair[0].casefold())
            )
        }
        self._selected_key: str | None = None
        self.requested_folder = (requested_folder or "").strip()

    def compose(self) -> ComposeResult:
        with Vertical(id="backup-folder-root"):
            yield Label(
                f"Offline folders · {self.backup_id}", id="backup-folder-title", markup=False
            )
            yield Static(
                "Source: immutable local backup · no Exchange connection or server mutation.",
                id="backup-folder-source",
                markup=False,
            )
            yield Static("Loading offline folder index…", id="area-state", markup=False)
            yield DataTable(id="backup-folder-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "Select a folder to inspect its bounded archive contents.",
                id="backup-folder-detail",
                markup=False,
            )
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="backup-folder-actions"):
                yield Button("Open folder", id="backup-folder-open", disabled=True)
                yield Button("Close", classes="area-close", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#backup-folder-table", DataTable)
        table.add_columns("Folder", "Items", "Types", "Stored size")
        selected_row = 0
        for row_index, (key, (folder, items)) in enumerate(self._folders.items()):
            kinds = ", ".join(sorted({item.kind for item in items}, key=str.casefold))
            size = sum(item.size_bytes or 0 for item in items)
            table.add_row(
                clean_cell(folder, max_length=120),
                str(len(items)),
                clean_cell(kinds, max_length=80),
                human_bytes(size),
                key=key,
            )
            if self.requested_folder and (
                folder.casefold() == self.requested_folder.casefold()
                or self.requested_folder.casefold() in folder.casefold()
            ):
                selected_row = row_index
        if self._folders:
            table.move_cursor(row=selected_row)
            key = tuple(self._folders)[selected_row]
            self._select_folder(key)
            self._set_view_state(
                ViewState.READY,
                f"{len(self._folders)} offline folder(s) from {len(self.items)} bounded item(s).",
            )
        else:
            self._set_view_state(ViewState.EMPTY, "This backup contains no foldered items.")

    @on(DataTable.RowHighlighted, "#backup-folder-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select_folder(str(event.row_key.value))

    def _select_folder(self, key: str) -> None:
        selected = self._folders.get(key)
        if selected is None:
            return
        self._selected_key = key
        folder, items = selected
        preview = tuple(item.title or "(untitled)" for item in items[:8])
        remaining = max(0, len(items) - len(preview))
        stored_size = human_bytes(sum(item.size_bytes or 0 for item in items))
        lines = [
            clean_cell(folder, max_length=220),
            f"Items: {len(items)} · Stored size: {stored_size}",
            *(f"• {clean_cell(title, max_length=160)}" for title in preview),
        ]
        if remaining:
            lines.append(f"… and {remaining} more item(s)")
        self.query_one("#backup-folder-detail", Static).update("\n".join(lines))
        self.query_one("#backup-folder-open", Button).disabled = False

    def action_open_folder(self) -> None:
        selected = self._folders.get(self._selected_key or "")
        if selected is None:
            return
        folder, items = selected
        self.app.push_screen(
            BackupBrowseScreen(
                self.backup_id,
                items,
                backend=self.backend,
                encrypted=self.encrypted,
                offline=True,
            )
        )
        self._set_view_state(
            ViewState.READY,
            f"Opened {clean_cell(folder, max_length=120)} from the local backup.",
        )

    @on(Button.Pressed, "#backup-folder-open")
    def _open_pressed(self) -> None:
        self.action_open_folder()


class RestoreStatusScreen(OperationalScreen):
    """Bounded, read-only local recovery; opening or paging can never restore."""

    SHARED_WORKSPACE_LAYOUT: ClassVar = False
    DEFAULT_CSS = """
    RestoreStatusScreen { align: center middle; background: $background 75%; }
    #restore-status-root {
        width: 94%; max-width: 110; height: 90%; max-height: 28; padding: 1 2;
        background: $surface;
    }
    #restore-status-heading { height: 2; border-bottom: solid $panel; }
    #restore-status-brand { width: 13; color: $primary; }
    #restore-status-title { width: 1fr; text-style: bold; }
    #restore-status-summary { height: auto; margin: 1 0; }
    #restore-status-table { height: 1fr; min-height: 3; }
    #restore-status-detail-scroll { height: 4; }
    #restore-status-detail { height: auto; }
    #restore-status-range { height: 1; }
    #restore-status-actions {
        height: 2; border-top: solid $panel; align-horizontal: right;
    }
    #restore-status-actions Button { width: auto; min-width: 0; margin-left: 1; }
    """

    def __init__(self, backend: object, operation_id: str):
        super().__init__(area_name="backups", offline=True)
        self.backend, self.operation_id = backend, operation_id
        self._status_offset = 0
        self._next_offset: int | None = None
        self._status_rows: dict[str, Mapping[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="restore-status-root"):
            with Horizontal(id="restore-status-heading"):
                yield Label("GUC Mail", id="restore-status-brand")
                yield Label("Restore status", id="restore-status-title")
            yield Static("", id="area-state", markup=False)
            yield Static("", id="restore-status-summary", markup=False)
            yield DataTable(id="restore-status-table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="restore-status-detail-scroll"):
                yield Static("", id="restore-status-detail", markup=False)
            yield Static("", id="restore-status-range", markup=False)
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="restore-status-actions"):
                yield Button("Previous", id="restore-status-previous", disabled=True)
                yield Button("Next", id="restore-status-next", disabled=True)
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.query_one("#restore-status-table", DataTable).add_columns(
            "Outcome", "Source", "Destination"
        )
        self.action_refresh()

    def action_refresh(self) -> None:
        loader = getattr(self.backend, "restore_status", None)
        if not callable(loader):
            self._show_error("Restore status", PublicOperationError("Local history unavailable."))
            return
        offset = self._status_offset
        self._begin_operation(
            "Reading local restore status",
            lambda cancel, progress: loader(
                self.operation_id, limit=100, offset=offset, cancel=cancel, progress=progress,
            ), self._loaded, retry_safe=True,
        )

    def _loaded(self, result: object) -> None:
        if not isinstance(result, Mapping):
            raise PublicOperationError("Invalid local restore status.")
        rows = result["targets"]
        self._status_rows = {str(row["ordinal"]): row for row in rows}
        table = self.query_one("#restore-status-table", DataTable)
        table.clear()
        for key, row in self._status_rows.items():
            label = {
                "not_started": "Not started", "dispatching": "Unconfirmed",
                "uncertain": "Unconfirmed", "completed": "Acknowledged",
                "rejected": "Rejected", "cancelled": "Cancelled",
            }.get(row["status"], row["status"])
            table.add_row(label, row["source_reference"], row["destination"], key=key)
        counts = result["counts"]
        unknown = counts.get("dispatching", 0) + counts.get("uncertain", 0)
        self.query_one("#restore-status-summary", Static).update(
            f"Acknowledged: {counts.get('completed', 0)} · Unconfirmed: {unknown} · "
            f"Rejected: {counts.get('rejected', 0)}\n"
            "Read-only local records. Unconfirmed items must not be repeated."
        )
        total = result["total_targets"]
        self._next_offset = result["next_offset"]
        self.query_one("#restore-status-range", Static).update(
            f"{self._status_offset + 1 if rows else 0}-"
            f"{self._status_offset + len(rows)} of {total} targets"
        )
        self.query_one("#restore-status-previous", Button).disabled = self._status_offset == 0
        self.query_one("#restore-status-next", Button).disabled = self._next_offset is None
        self._set_view_state(
            ViewState.READY, "Local recovery loaded; no mailbox operation requested."
        )

    @on(DataTable.RowHighlighted, "#restore-status-table")
    def _status_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        row = self._status_rows.get(str(event.row_key.value))
        if row is not None:
            self.query_one("#restore-status-detail", Static).update(
                f"Source: {clean_cell(row['source_reference'], max_length=8192)}\n"
                f"Destination: {clean_cell(row['destination'], max_length=8192)}\n"
                f"Restored reference: "
                f"{clean_cell(row['result_reference'] or 'not recorded', max_length=8192)}\n"
                f"Operation: {clean_cell(self.operation_id)}\n"
                f"Error type: {clean_cell(row['error_type'] or 'none recorded')}"
            )

    @on(Button.Pressed, "#restore-status-previous, #restore-status-next")
    def _status_page(self, event: Button.Pressed) -> None:
        if self._operation_active:
            return
        if event.button.id == "restore-status-previous":
            self._status_offset = max(0, self._status_offset - 100)
        elif self._next_offset is not None:
            self._status_offset = self._next_offset
        self.action_refresh()


class RestorePlanScreen(OperationalScreen):
    """Restore preview plus a separately confirmed, provider-backed execution path."""

    SHARED_WORKSPACE_LAYOUT: ClassVar = False
    DEFAULT_CSS = """
    RestorePlanScreen { layout: vertical; }
    #restore-plan-root { width: 100%; height: 100%; padding: 1 2; }
    #restore-plan-heading { height: 2; border-bottom: solid $panel; }
    #restore-plan-brand { width: 13; color: $primary; }
    #restore-plan-title { width: 1fr; text-style: bold; }
    #restore-plan-body { height: 1fr; }
    #restore-plan-summary { height: auto; min-height: 3; }
    #restore-plan-table { height: 10; min-height: 4; }
    #restore-plan-detail {
        height: auto; min-height: 4; border-top: solid $panel; padding: 1;
    }
    #restore-plan-actions {
        height: auto; border-top: solid $panel;
    }
    #restore-plan-main-actions { height: 2; align-horizontal: right; }
    #restore-plan-paging { height: 1; }
    #restore-plan-range { width: 1fr; }
    #restore-plan-actions Button { width: auto; min-width: 0; margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """

    def __init__(
        self,
        plan: RestorePlanView,
        backend: BackupScreenBackend,
        *,
        offline: bool,
    ) -> None:
        super().__init__(area_name="backups", offline=offline)
        self.plan = plan
        self.backend = backend
        self._entries: dict[str, RestorePlanEntryView] = {}
        self._restore_timed_out = False
        self._restore_request_started = Event()
        self._last_operation_id: str | None = None
        self._selected_reference: str | None = None
        self._entry_offset = 0
        self._outcome_detail: str | None = None
        self._has_completed_duplicates = any(
            entry.recovery_status == "completed" and entry.action == "skip_duplicate"
            for entry in self.plan.entries
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="restore-plan-root"):
            with Horizontal(id="restore-plan-heading"):
                yield Label("GUC Mail", id="restore-plan-brand")
                yield Label("Restore plan", id="restore-plan-title")
            with VerticalScroll(id="restore-plan-body"):
                yield Static("", id="area-state", markup=False)
                yield Static("", id="restore-plan-summary", markup=False)
                yield DataTable(id="restore-plan-table", cursor_type="row", zebra_stripes=True)
                yield Static("Select a row for warnings.", id="restore-plan-detail", markup=False)
            yield Static("", id="area-progress", markup=False)
            with Vertical(id="restore-plan-actions"):
                with Horizontal(id="restore-plan-paging"):
                    yield Static("", id="restore-plan-range", markup=False)
                    yield Button("Prev", id="restore-plan-previous")
                    yield Button("Next", id="restore-plan-next")
                with Horizontal(id="restore-plan-main-actions"):
                    yield Button(
                        "Restore previewed items",
                        id="restore-execute",
                        variant="warning",
                        disabled=self.offline or self.plan.create_count == 0,
                    )
                    yield Button("Another copy…", id="restore-another-copy", disabled=True)
                    yield Button("Restore status", id="restore-status", disabled=True)
                    yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self._show_plan()

    def _show_plan(self) -> None:
        parts = [f"Create: {self.plan.create_count}"]
        if self.plan.duplicate_count:
            parts.append(f"Already restored: {self.plan.duplicate_count}")
        if self.plan.blocked_count:
            parts.append(f"Needs review: {self.plan.blocked_count}")
        if self.plan.unsupported_count:
            parts.append(f"Unsupported: {self.plan.unsupported_count}")
        self.query_one("#restore-plan-summary", Static).update(
            " · ".join(parts)
            + "\nNo changes from this preview. Restore requires a separate confirmation."
        )
        table = self.query_one("#restore-plan-table", DataTable)
        table.clear(columns=True)
        table.add_column("Action", width=16)
        table.add_column("Item", width=30)
        table.add_column("Type", width=8)
        table.add_column("Destination", width=40)
        self._render_entries()
        state = ViewState.ERROR if self.plan.blocked_count else (
            ViewState.READY if self.plan.entries else ViewState.EMPTY
        )
        message = (
            "Review unconfirmed restores before another attempt."
            if self.plan.blocked_count else
            "Review the items and destination before restoring."
            if self.plan.entries
            else "The restore plan has no entries."
        )
        self._set_view_state(state, message)
        self._refresh_restore_actions()

    def _render_entries(self) -> None:
        page = self.plan.entries[self._entry_offset:self._entry_offset + 100]
        self._entries = {entry.reference: entry for entry in page}
        self._selected_reference = page[0].reference if page else None
        table = self.query_one("#restore-plan-table", DataTable)
        table.clear()
        table.styles.height = min(10, max(3, len(page) + 2))
        for entry in page:
            table.add_row(
                {"create": "Create", "skip_duplicate": "Already restored",
                 "review_required": "Needs review", "unsupported": "Unsupported"}.get(
                    entry.action, clean_cell(entry.action, max_length=30)
                ),
                clean_cell(entry.title or entry.reference, max_length=80),
                clean_cell(entry.kind, max_length=30),
                clean_cell(entry.destination, max_length=80),
                key=entry.reference,
            )
        total = len(self.plan.entries)
        self.query_one("#restore-plan-paging").display = total > 100
        self.query_one("#restore-plan-range", Static).update(
            f"Preview {self._entry_offset + 1 if page else 0}-{self._entry_offset + len(page)} "
            f"of {total} items"
        )
        self.query_one("#restore-plan-previous", Button).disabled = self._entry_offset == 0
        self.query_one("#restore-plan-next", Button).disabled = self._entry_offset + 100 >= total
        self._refresh_restore_actions()

    def _refresh_restore_actions(self) -> None:
        selected = self._entries.get(self._selected_reference or "")
        recovery_id = self._last_operation_id or (
            selected.recovery_operation_id if selected else None
        )
        status = self.query_one("#restore-status", Button)
        status.display = bool(recovery_id)
        status.disabled = (
            not recovery_id or not callable(getattr(self.backend, "restore_status", None))
        )
        another = self.query_one("#restore-another-copy", Button)
        another.display = self._has_completed_duplicates
        another.disabled = self.offline or not another.display

    @on(DataTable.RowHighlighted, "#restore-plan-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        entry = self._entries.get(str(event.row_key.value))
        if entry is None:
            return
        self._selected_reference = entry.reference
        if self._outcome_detail is not None:
            self._refresh_restore_actions()
            return
        details = [
            f"Item: {clean_cell(entry.title or '(untitled)', max_length=500)}",
            f"Source reference: {clean_cell(entry.reference, max_length=180)}",
            "Action: " + {"create": "Create", "skip_duplicate": "Already restored",
                          "review_required": "Needs review", "unsupported": "Unsupported"}.get(
                              entry.action, clean_cell(entry.action)
                          ),
            f"Destination: {clean_cell(entry.destination, max_length=180)}",
        ]
        if entry.duplicate_reason:
            details.append(f"Duplicate: {clean_cell(entry.duplicate_reason, max_length=300)}")
        details.extend(f"Warning: {clean_cell(item, max_length=300)}" for item in entry.warnings)
        self.query_one("#restore-plan-detail", Static).update("\n".join(details))
        self._refresh_restore_actions()

    @on(Button.Pressed, "#restore-status")
    def _open_status(self) -> None:
        selected = self._entries.get(self._selected_reference or "")
        reference = self._last_operation_id or (
            selected.recovery_operation_id if selected else None
        )
        if reference is not None:
            self.app.push_screen(RestoreStatusScreen(self.backend, reference))

    @on(Button.Pressed, "#restore-plan-previous, #restore-plan-next")
    def _plan_page(self, event: Button.Pressed) -> None:
        if self._operation_active:
            return
        if event.button.id == "restore-plan-previous":
            self._entry_offset = max(0, self._entry_offset - 100)
        elif self._entry_offset + 100 < len(self.plan.entries):
            self._entry_offset += 100
        self._render_entries()

    @on(Button.Pressed, "#restore-another-copy")
    def _another_copy(self) -> None:
        if self._operation_active or self._guard_pending_mutation() or self.offline:
            return
        self._confirm(
            "Review another copy of completed restores?",
            "This deliberately creates another copy of items with recorded completion. "
            "Unconfirmed restores remain blocked. A new exact restore plan and final "
            "confirmation will be required before anything is created.",
            self._replan_another_copy, confirm_label="Review another copy",
        )

    def _replan_another_copy(self) -> None:
        if self.plan.encrypted:
            def supplied(passphrase: str | None) -> None:
                if passphrase:
                    self._replan_copy_with_passphrase(passphrase)
                else:
                    self._set_view_state(ViewState.CANCELLED, "Additional-copy review cancelled.")
            self.app.push_screen(BackupPassphraseScreen("review an additional copy"), supplied)
            return
        self._replan_copy_with_passphrase(None)

    def _replan_copy_with_passphrase(self, passphrase: str | None) -> None:
        references = tuple(entry.item_key or entry.reference for entry in self.plan.entries)
        backend = cast(Any, self.backend)
        self._begin_operation(
            "Reviewing an intentional additional copy",
            lambda cancel, progress: backend.create_restore_plan(
                self.plan.backup_id, references, allow_completed_duplicates=True,
                passphrase=passphrase,
                cancel=cancel, progress=progress,
            ), self._replace_plan, retry_safe=True,
        )

    def _replace_plan(self, result: object) -> None:
        if not isinstance(result, RestorePlanView):
            raise PublicOperationError("Invalid replacement restore plan.")
        self.plan = result
        self._entry_offset = 0
        self._outcome_detail = None
        self._last_operation_id = None
        self._has_completed_duplicates = False
        self.query_one("#restore-execute", Button).disabled = (
            self.offline or not result.create_count
        )
        self._show_plan()

    @on(Button.Pressed, "#restore-execute")
    def _execute_pressed(self) -> None:
        if self.offline:
            self._show_error("Restore backup", OfflineOperationError())
            return
        self._confirm(
            "Restore previewed items to the mailbox?",
            f"Backup: {clean_cell(self.plan.backup_id, max_length=180)}\n"
            f"Items to create: {self.plan.create_count}\n"
            f"Duplicates skipped: {self.plan.duplicate_count}\n"
            f"Unsupported skipped: {self.plan.unsupported_count}\n\n"
            + (
                "This explicitly creates another copy of previously completed restores.\n\n"
                if self.plan.allow_completed_duplicates else ""
            )
            +
            "This creates new mailbox items in dedicated restore folders. It never sends "
            "restored messages, but a provider timeout can leave the last item uncertain.",
            self._confirmed_restore,
            confirm_label="Restore items",
        )

    def _confirmed_restore(self) -> None:
        if not self.plan.encrypted:
            self._start_restore(None)
            return

        def resolved(value: str | None) -> None:
            if value:
                self._start_restore(value)
            else:
                self._set_view_state(
                    ViewState.CANCELLED,
                    "Restore cancelled; no passphrase retained.",
                )

        self.app.push_screen(
            BackupPassphraseScreen("restore the previewed items"),
            resolved,
        )

    def action_close(self) -> None:
        if self._operation_active:
            self.action_cancel_operation()
            return
        if self._guard_pending_mutation():
            return
        super().action_close()

    def action_cancel_operation(self) -> None:
        if not self._operation_active:
            return
        self._cancel_event.set()
        # Keep the callback alive: the in-flight item may finish remotely, and
        # the shared service will stop before starting another item.
        self._show_progress(
            self._operation_generation,
            OperationProgress("Stop requested; waiting for current item"),
        )

    def _operation_timed_out(self, generation: int, action: str) -> None:
        if generation != self._operation_generation or not self._operation_active:
            return
        if self._operation_retry_safe:
            super()._operation_timed_out(generation, action)
            return
        self._restore_timed_out = True
        self._cancel_event.set()
        if self._timeout_timer is not None:
            self._timeout_timer.stop()
            self._timeout_timer = None
        # A deadline stops the batch; it cannot cancel an accepted provider request.
        # Keep its callback and lease until the actual thread returns.
        self._show_progress(generation, OperationProgress("Restore timed out"))
        self.post_message(
            AreaAction(
                self.area_name,
                action,
                "timed-out",
                detail=f"Restore timed out after {self._active_timeout:g} seconds; "
                "waiting for the current item before stopping.",
            )
        )

    def _show_progress(self, generation: int, progress: OperationProgress) -> None:
        if self._operation_active and self._cancel_event.is_set():
            progress = OperationProgress(
                "Restore timed out; waiting for current item"
                if self._restore_timed_out
                else "Stop requested; waiting for current item",
                detail="Items may still be created; results will be kept."
                if self._restore_timed_out
                else "Completed results will be kept.",
            )
        super()._show_progress(generation, progress)

    def _start_restore(self, passphrase: str | None) -> None:
        if self._guard_pending_mutation():
            return
        self.query_one("#restore-execute", Button).disabled = True
        self.query_one("#restore-plan-summary", Static).update(
            f"Previewed: {self.plan.create_count} create · "
            f"{self.plan.duplicate_count} duplicates · {self.plan.unsupported_count} unsupported\n"
            "Restore requested. Items may be created while the request is pending."
        )
        self._restore_timed_out = False
        self._restore_request_started = Event()

        def execute(cancel: Event, progress: ProgressReporter) -> RestoreResultView:
            self._restore_request_started.set()
            return self.backend.execute_restore(
                self.plan,
                confirmed=True,
                passphrase=passphrase,
                cancel=cancel,
                progress=progress,
            )

        self._begin_operation(
            "Restoring backup",
            execute,
            self._restore_finished,
            timeout_seconds=max(180.0, self.operation_timeout_seconds),
        )

    def _restore_finished(self, result: object) -> None:
        if (
            result is None
            and self._cancel_event.is_set()
            and not self._restore_request_started.is_set()
        ):
            # The executor skipped this cancelled request before backend entry.
            # A backend which ran and returned None still fails validation below.
            result = RestoreResultView(
                self.plan.backup_id,
                attempted_count=0,
                restored_count=0,
                failed_count=0,
                duplicate_count=self.plan.duplicate_count,
                unsupported_count=self.plan.unsupported_count,
                stopped_early=True,
                server_mutations_performed=False,
                cancelled=True,
            )
        if not isinstance(result, RestoreResultView):
            self.query_one("#restore-plan-summary", Static).update(
                f"Previewed: {self.plan.create_count} create · "
                f"{self.plan.duplicate_count} duplicates · "
                f"{self.plan.unsupported_count} unsupported\n"
                "Restore finished without a valid result. Items may have been created;\n"
                "inspect the destination before another plan."
            )
            raise PublicOperationError(
                "The backup backend returned an invalid restore result. "
                "Items may have been created; inspect the destination before another plan.",
                retryable=False,
            )
        self._last_operation_id = result.operation_id
        unconfirmed = result.uncertain_count or (1 if result.mutation_uncertain else 0)
        lines = [
            f"Attempted: {result.attempted_count}",
            f"Restored: {result.restored_count}",
            f"Unconfirmed: {unconfirmed}",
            f"Rejected/not started: {max(0, result.failed_count - unconfirmed)}",
            f"Duplicates skipped: {result.duplicate_count}",
            f"Unsupported skipped: {result.unsupported_count}",
            f"Needs review: {result.blocked_count}",
        ]
        if result.operation_id:
            lines.append("Use Restore status to inspect the saved per-item recovery.")
        if result.recovery_write_failed:
            lines.append(
                "An effect may be acknowledged here but its recovery update could not be saved. "
                "Do not repeat these items; review Restore status and the destination."
            )
        if not result.journal_available and result.attempted_count:
            lines.append(
                "No durable restore history is configured. Retain this result before closing."
            )
        if self._restore_timed_out:
            lines.append(
                "Restore request finished after its timeout; completed results are retained."
            )
        if result.interrupted:
            lines.append("Restore interrupted; completed and uncertain item results are retained.")
        elif result.cancelled:
            lines.append("Restore stopped at your request; completed item results are retained.")
        elif result.stopped_early:
            lines.append("Stopped after the first provider failure; review before retrying.")
        if result.mutation_uncertain:
            lines.append(
                "Exchange may have created the failed item. Inspect the destination before "
                "planning another restore; this result cannot be replayed."
            )
        lines.extend(
            f"Failed reference: {clean_cell(reference, max_length=180)}"
            for reference in result.failures
        )
        self._outcome_detail = "\n".join(lines)
        self.query_one("#restore-plan-detail", Static).update(self._outcome_detail)
        state = (
            ViewState.ERROR
            if result.failed_count or result.recovery_write_failed or result.blocked_count
            else ViewState.CANCELLED
            if result.cancelled or result.interrupted
            else ViewState.READY
        )
        mutation_note = (
            "Mailbox items were created."
            if result.restored_count > 0
            else "No successful item restore was verified."
        )
        if result.mutation_uncertain:
            mutation_note += " The failed item may exist; inspect the destination first."
        self.query_one("#restore-plan-summary", Static).update(
            f"Previewed: {self.plan.create_count} create · "
            f"{self.plan.duplicate_count} duplicates · {self.plan.unsupported_count} unsupported\n"
            "Restore attempt finished. Review the result below before planning another restore."
        )
        self.query_one("#restore-execute", Button).disabled = True
        outcome = (
            "needs review"
            if result.mutation_uncertain or result.recovery_write_failed or result.blocked_count
            else "stopped"
            if result.cancelled or result.interrupted or result.failed_count
            else "completed"
        )
        self._set_view_state(
            state,
            f"Restore {outcome}: "
            f"{result.restored_count} restored, "
            f"{unconfirmed} unconfirmed, "
            f"{max(0, result.failed_count - unconfirmed)} rejected/not started. {mutation_note}",
        )
        self._refresh_restore_actions()


class BackupScreen(OperationalScreen):
    """Full backup workflow with local-write confirmation and offline browsing."""

    DEFAULT_CSS = """
    BackupScreen { layout: vertical; }
    #backup-root { width: 100%; height: 100%; padding: 1 2; }
    #backup-title { height: 2; text-style: bold; }
    #backup-create-fields { height: 3; }
    #backup-create-fields Input { width: 2fr; margin-right: 1; }
    #backup-kind { width: 1fr; margin-right: 1; }
    #backup-advanced { height: auto; max-height: 10; display: none; }
    #backup-advanced.visible { display: block; }
    #backup-parent-summary { height: auto; }
    #backup-advanced Input { height: 3; }
    #backup-folders { height: 6; }
    #backup-folder-entry, #backup-content-types { height: 3; }
    #backup-folder-entry Input { width: 1fr; }
    #backup-content-types Checkbox { width: 1fr; }
    #backup-table { height: 1fr; min-height: 8; }
    #backup-detail { height: 8; border-top: solid $panel; padding: 1; }
    #backup-actions { height: auto; min-height: 3; align-horizontal: left; }
    #backup-actions Button { margin-right: 1; }
    #backup-repository-actions { height: auto; min-height: 3; align-horizontal: left; }
    #backup-repository-actions Button { margin-right: 1; }
    #backup-operation-status {
        height: auto; dock: bottom; background: $footer-background;
    }
    #backup-operation-status #area-state { height: auto; min-height: 1; }
    #backup-operation-status #area-progress { display: none; }
    #backup-footer-actions {
        height: 3; align-horizontal: right; background: $footer-background;
    }
    #backup-footer-actions Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    BackupScreen.compact-backup #backup-root { padding: 1; }
    BackupScreen.compact-backup #backup-create-fields {
        layout: vertical;
        height: auto;
        min-height: 9;
    }
    BackupScreen.compact-backup #backup-create-fields Input,
    BackupScreen.compact-backup #backup-kind,
    BackupScreen.compact-backup #backup-toggle-advanced {
        width: 100%;
        margin-right: 0;
    }
    BackupScreen.compact-backup #backup-table { height: 7; min-height: 7; }
    BackupScreen.compact-backup #backup-detail { height: 6; }
    BackupScreen.compact-backup #backup-actions {
        layout: vertical;
        height: auto;
    }
    BackupScreen.compact-backup #backup-repository-actions {
        layout: vertical;
        height: auto;
    }
    BackupScreen.compact-backup #backup-actions Button,
    BackupScreen.compact-backup #backup-repository-actions Button {
        width: 100%;
        margin-right: 0;
    }
    BackupScreen.compact-backup #backup-footer-actions {
        background: $footer-background;
    }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("e", "estimate", "Estimate", show=False),
        Binding("c", "create", "Create", show=False),
        Binding("u", "resume", "Resume", show=False),
        Binding("v", "verify", "Verify", show=False),
        Binding("x", "export_backup", "Export", show=False),
        Binding("p", "prune_old_backups", "Prune", show=False),
        Binding("o", "open_backup_folder", "Open folder", show=False),
        Binding("enter", "browse", "Browse", show=False),
    ]

    def __init__(
        self,
        backend: BackupScreenBackend,
        *,
        default_destination: str = "",
        default_kind: str = "full",
        default_encrypted: bool = False,
        default_verify_after_create: bool = True,
        retention_count: int = 5,
        default_folders: Sequence[str] = (),
        available_folders: Sequence[str] = (),
        selected_messages: Sequence[str] = (),
        offline: bool = False,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            area_name="backups",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        self.backend = backend
        self.default_destination = clean_cell(default_destination, max_length=500)
        allowed_kinds = {"full", "incremental", "selected-messages"}
        self.selected_messages = tuple(dict.fromkeys(str(value) for value in selected_messages))
        if self.selected_messages:
            default_kind = "selected-messages"
        self.default_kind = default_kind if default_kind in allowed_kinds else "full"
        self.default_encrypted = bool(default_encrypted)
        self.default_verify_after_create = bool(default_verify_after_create)
        self.retention_count = min(max(int(retention_count), 1), 100)
        self.default_folders = tuple(
            dict.fromkeys(
                clean_cell(str(value), max_length=300)
                for value in default_folders
                if str(value).strip()
            )
        )
        self._records: tuple[BackupRecordView, ...] = ()
        self.available_folders = tuple(dict.fromkeys((*default_folders, *available_folders)))
        self._records_by_id: dict[str, BackupRecordView] = {}
        self._selected_id: str | None = None
        self._repository_loaded = False
        self._pending_action: tuple[str, Callable[[], None], bool] | None = None

    @property
    def selected_backup_id(self) -> str | None:
        """Return the highlighted archive for context-aware local search."""

        return self._selected_id

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="backup-root"):
            yield Label("Backup Center", id="backup-title")
            with Horizontal(id="backup-create-fields"):
                yield Input(
                    value=self.default_destination,
                    placeholder="Backup destination",
                    id="backup-destination",
                )
                yield Select(
                    (
                        ("Full", "full"),
                        ("Incremental", "incremental"),
                        ("Selected messages", "selected-messages"),
                    ),
                    value=self.default_kind,
                    allow_blank=False,
                    id="backup-kind",
                )
                yield Button("Advanced", id="backup-toggle-advanced")
            with VerticalScroll(id="backup-advanced"):
                yield Label("Folders · Space selects · none selected uses configured scope")
                yield SelectionList[str](
                    *(
                        (clean_cell(path), path, path in self.default_folders)
                        for path in self.available_folders
                    ),
                    id="backup-folders",
                )
                with Horizontal(id="backup-folder-entry"):
                    yield Input(placeholder="Add an unloaded folder path", id="backup-folder-path")
                    yield Button("Add folder", id="backup-folder-add")
                yield Label("Content types · none selected includes all supported types")
                with Horizontal(id="backup-content-types"):
                    for name in ("mail", "calendar", "contacts", "tasks"):
                        yield Checkbox(name.title(), id=f"backup-content-{name}")
                yield Checkbox(
                    "Encrypt backup (passphrase is requested securely and never retained)",
                    value=self.default_encrypted,
                    id="backup-encrypt",
                )
                yield Checkbox(
                    "Verify after creation",
                    value=self.default_verify_after_create,
                    id="backup-verify-after",
                )
                yield Static(
                    f"Retention policy: keep {self.retention_count} completed backup(s). "
                    "Edit Backup retention in Settings; pruning is always separately previewed "
                    "and confirmed.",
                    id="backup-retention-summary",
                    markup=False,
                )
                if self.selected_messages:
                    yield Static(
                        "Selected-message scope (bounded):\n" + "\n".join(self.selected_messages),
                        id="backup-selection-summary",
                        markup=False,
                    )
            yield Static("", id="backup-parent-summary", markup=False)
            yield DataTable(id="backup-table", cursor_type="row", zebra_stripes=True)
            yield Static("Select a backup to inspect it.", id="backup-detail", markup=False)
            with Horizontal(id="backup-actions"):
                yield Button("Resume", id="backup-resume", disabled=True)
                yield Button("Verify", id="backup-verify", disabled=True)
                yield Button("Browse offline", id="backup-browse", disabled=True)
                yield Button("Restore plan", id="backup-restore-plan", disabled=True)
                yield Button(
                    "Restore selected…",
                    id="backup-restore-selected",
                    disabled=True,
                )
            with Horizontal(id="backup-repository-actions"):
                yield Button("Export archive…", id="backup-export", disabled=True)
                yield Button("Prune old backups…", id="backup-prune")
                yield Button("Open backup folder", id="backup-open-folder")
            with Vertical(id="backup-operation-status"):
                yield Static("Loading backups…", id="area-state", markup=False)
                # OperationalScreen reports progress through area-state too;
                # retain the shared hook without a duplicate visible message.
                yield Static("", id="area-progress", markup=False)
                with HorizontalScroll(id="backup-footer-actions"):
                    yield Button("Estimate", id="backup-estimate")
                    yield Button("Create backup", id="backup-create", variant="primary")
                    retry = Button("Retry", id="area-retry")
                    retry.display = False
                    yield retry
                    cancel = Button("Cancel operation", id="area-cancel-operation")
                    cancel.display = False
                    yield cancel
                    yield Button("Refresh", id="backup-refresh")
                    yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.set_class(self.size.width < 100 or self.size.height < 30, "compact-backup")
        self._update_create_mode()
        if self.default_folders or self.selected_messages:
            self.query_one("#backup-advanced", VerticalScroll).add_class("visible")
            self.query_one("#backup-toggle-advanced", Button).label = "Basic"
        table = self.query_one("#backup-table", DataTable)
        table.add_columns(
            "Created",
            "Type",
            "State",
            "Items",
            "Stored",
            "Encrypted",
            "Verification",
        )
        if self.offline:
            self.query_one("#backup-estimate", Button).disabled = True
            self.query_one("#backup-create", Button).disabled = True
        self.call_after_refresh(self.action_refresh)

    def configure_create(
        self,
        *,
        kind: str | None = None,
        folders: Sequence[str] = (),
    ) -> None:
        """Apply an explicit palette scope to the visible create form."""

        if kind in {"full", "incremental"} and not self.selected_messages:
            self.query_one("#backup-kind", Select).value = kind
        normalized_folders = tuple(
            dict.fromkeys(
                clean_cell(str(value), max_length=300) for value in folders if str(value).strip()
            )
        )
        if normalized_folders:
            self._select_folders(normalized_folders)
            self.query_one("#backup-advanced", VerticalScroll).add_class("visible")
            self.query_one("#backup-toggle-advanced", Button).label = "Basic"

    def _select_folders(self, folders: Sequence[str]) -> None:
        listing = self.query_one("#backup-folders", SelectionList)
        existing = {
            listing.get_option_at_index(index).value for index in range(listing.option_count)
        }
        listing.deselect_all()
        for path in folders:
            if path not in existing:
                listing.add_option((clean_cell(path), path))
                existing.add(path)
            listing.select(path)

    @on(Button.Pressed, "#backup-folder-add")
    def _add_folder_choice(self, event: Button.Pressed) -> None:
        event.stop()
        field = self.query_one("#backup-folder-path", Input)
        path = field.value.strip()
        if path:
            selected = self.query_one("#backup-folders", SelectionList).selected
            self._select_folders((*selected, path))
            field.value = ""
            field.focus()

    def queue_action(
        self,
        label: str,
        callback: Callable[[], None],
        *,
        requires_selection: bool,
    ) -> None:
        """Run a palette action after the initial repository listing is available."""

        loading_repository = self._operation_active and self._active_action == "Loading backups"
        if not self._repository_loaded or loading_repository:
            self._pending_action = (clean_cell(label, max_length=100), callback, requires_selection)
            self._set_view_state(ViewState.LOADING, f"Loading backups before {label}…")
            return
        if requires_selection and self._selected_id is None:
            self._show_error(
                label,
                PublicOperationError(
                    "No backup is available for this action. Create or choose a backup first.",
                    retryable=False,
                ),
            )
            return
        callback()

    def _run_pending_action(self) -> None:
        pending, self._pending_action = self._pending_action, None
        if pending is None:
            return
        label, callback, requires_selection = pending
        if requires_selection and self._selected_id is None:
            self._show_error(
                label,
                PublicOperationError(
                    "No backup is available for this action. Create or choose a backup first.",
                    retryable=False,
                ),
            )
            return
        callback()

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 100 or event.size.height < 30, "compact-backup")

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if self.has_class("compact-backup"):
            event.widget.scroll_visible(animate=False, force=True, immediate=True)

    @on(Select.Changed, "#backup-kind")
    def _kind_changed(self) -> None:
        self._update_create_mode()

    def _update_create_mode(self) -> None:
        incremental = self.query_one("#backup-kind", Select).value == "incremental"
        for selector in (
            "#backup-folders",
            "#backup-folder-path",
            "#backup-folder-add",
            "#backup-encrypt",
            "#backup-content-mail",
            "#backup-content-calendar",
            "#backup-content-contacts",
            "#backup-content-tasks",
        ):
            self.query_one(selector).disabled = incremental
        summary = self.query_one("#backup-parent-summary", Static)
        summary.display = incremental
        summary.update(
            "Parent: "
            + clean_cell(self._selected_id or "select a complete archive below")
            + " · inherits its saved scope and encryption."
        )

    def _parse_request(self) -> BackupCreateRequest:
        destination = self.query_one("#backup-destination", Input).value.strip()
        if not destination:
            raise PublicOperationError(
                "Choose a backup destination before estimating or creating a backup.",
                retryable=False,
            )
        kind_select = self.query_one("#backup-kind", Select)
        if kind_select.value is Select.BLANK:
            raise PublicOperationError("Choose full or incremental backup.", retryable=False)
        kind = str(kind_select.value)
        folders = tuple(self.query_one("#backup-folders", SelectionList).selected)
        content_types = tuple(
            name
            for name in ("mail", "calendar", "contacts", "tasks")
            if self.query_one(f"#backup-content-{name}", Checkbox).value
        )
        parent = None
        encrypted = self.query_one("#backup-encrypt", Checkbox).value
        messages = self.selected_messages
        if kind == "incremental":
            record = self._records_by_id.get(self._selected_id or "")
            if record is None or record.state != "complete":
                raise PublicOperationError(
                    "Select a complete archive below to use as the incremental parent.",
                    retryable=False,
                )
            if Path(destination).expanduser().resolve() != Path(record.destination).resolve():
                raise PublicOperationError(
                    "The destination must match the selected parent's repository.",
                    retryable=False,
                )
            parent, encrypted = record.backup_id, record.encrypted
            # The manifest, rather than unrelated form defaults, defines this scope.
            folders, messages, content_types = (), (), ()
        elif kind == "selected-messages" and not messages:
            raise PublicOperationError(
                "Select messages, search results or a cluster before opening this backup mode.",
                retryable=False,
            )
        return BackupCreateRequest(
            destination=destination,
            kind=kind,
            folders=folders,
            messages=messages,
            content_types=content_types,
            encrypted=encrypted,
            verify_after_create=self.query_one("#backup-verify-after", Checkbox).value,
            parent_backup_id=parent,
        )

    def _with_passphrase(
        self,
        *,
        encrypted: bool,
        purpose: str,
        callback: Callable[[str | None], None],
    ) -> None:
        if not encrypted:
            callback(None)
            return

        def resolved(value: str | None) -> None:
            if value:
                callback(value)
            else:
                self._set_view_state(
                    ViewState.CANCELLED,
                    "Encrypted backup operation cancelled; no passphrase retained.",
                )

        self.app.push_screen(BackupPassphraseScreen(purpose), resolved)

    def action_refresh(self) -> None:
        self._begin_operation(
            "Loading backups",
            lambda cancel, progress: tuple(
                self.backend.list_backups(cancel=cancel, progress=progress)
            ),
            self._backups_loaded,
            retry_safe=True,
        )

    def _backups_loaded(self, result: object) -> None:
        records = cast(tuple[BackupRecordView, ...], result)
        if not all(isinstance(item, BackupRecordView) for item in records):
            raise PublicOperationError("The backup backend returned invalid records.")
        self._records = records
        self._records_by_id = {record.backup_id: record for record in records}
        self._repository_loaded = True
        table = self.query_one("#backup-table", DataTable)
        table.clear()
        for record in records:
            table.add_row(
                clean_cell(record.created_at or "Incomplete", max_length=30),
                clean_cell(record.kind, max_length=24),
                clean_cell(record.state, max_length=24),
                str(record.item_count),
                human_bytes(record.stored_bytes),
                "Yes" if record.encrypted else "No",
                clean_cell(record.verification_state, max_length=30),
                key=record.backup_id,
            )
        if records:
            selected = (
                self._selected_id
                if self._selected_id in self._records_by_id
                else records[0].backup_id
            )
            self._select_backup(selected)
            self._set_view_state(ViewState.READY, f"{len(records)} backups available.")
        else:
            self._selected_id = None
            self._update_create_mode()
            self._update_selection_buttons()
            self.query_one("#backup-detail", Static).update(
                "No backups exist at this destination. Choose a destination, estimate, "
                "then create one."
            )
            state = ViewState.OFFLINE if self.offline else ViewState.EMPTY
            message = (
                "No local backups found. Offline mode is active."
                if self.offline
                else "No backups found."
            )
            self._set_view_state(state, message)
        self._run_pending_action()

    @on(DataTable.RowHighlighted, "#backup-table")
    def _backup_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select_backup(str(event.row_key.value))

    def _select_backup(self, backup_id: str) -> None:
        record = self._records_by_id.get(backup_id)
        if record is None:
            return
        self._selected_id = backup_id
        self._update_create_mode()
        self._update_selection_buttons()
        lines = [
            f"Backup: {clean_cell(record.backup_id, max_length=180)}",
            f"Destination: {clean_cell(record.destination, max_length=300)}",
            f"Created: {clean_cell(record.created_at or 'Unknown')}",
            f"Completed: {clean_cell(record.completed_at or 'Not complete')}",
            f"Items: {record.item_count} · Stored: {human_bytes(record.stored_bytes)}",
            f"Encryption: {'Enabled' if record.encrypted else 'Not enabled'}",
            f"Verification: {clean_cell(record.verification_state)}",
        ]
        if record.warning:
            lines.append(f"Warning: {clean_cell(record.warning, max_length=500)}")
        self.query_one("#backup-detail", Static).update("\n".join(lines))

    def _update_selection_buttons(self) -> None:
        selected = self._records_by_id.get(self._selected_id or "")
        self.query_one("#backup-verify", Button).disabled = selected is None
        complete = selected is not None and selected.state == "complete"
        self.query_one("#backup-browse", Button).disabled = not complete
        self.query_one("#backup-restore-plan", Button).disabled = not complete
        self.query_one("#backup-restore-selected", Button).disabled = not complete
        self.query_one("#backup-export", Button).disabled = not complete
        self.query_one("#backup-resume", Button).disabled = not (
            not self.offline and selected is not None and selected.state == "incomplete"
        )

    def action_estimate(self) -> None:
        if self.offline:
            self._show_error("Estimate backup", OfflineOperationError())
            return
        try:
            request = self._parse_request()
        except PublicOperationError as exc:
            self._show_error("Estimate backup", exc)
            return
        self._with_passphrase(
            encrypted=request.kind == "incremental" and request.encrypted,
            purpose="read the parent scope for this estimate",
            callback=lambda passphrase: self._execute_estimate(request, passphrase),
        )

    def _execute_estimate(self, request: BackupCreateRequest, passphrase: str | None) -> None:
        self._begin_operation(
            "Estimating backup",
            lambda cancel, progress: self.backend.estimate_backup(
                request,
                passphrase=passphrase,
                cancel=cancel,
                progress=progress,
            ),
            self._estimate_loaded,
            retry_safe=True,
        )

    def _estimate_loaded(self, result: object) -> None:
        if not isinstance(result, BackupEstimateView):
            raise PublicOperationError("The backup backend returned an invalid estimate.")
        text = (
            f"Estimated items: {result.item_count}\n"
            f"Attachments: {result.attachment_count}\n"
            f"Estimated transfer: {human_bytes(result.estimated_bytes)}\n"
            f"Unknown-size items: {result.unknown_size_count}\n"
            f"{clean_cell(result.note, max_length=500)}"
        )
        self.query_one("#backup-detail", Static).update(text)
        self._set_view_state(ViewState.READY, "Estimate complete; no backup data was downloaded.")

    def action_create(self) -> None:
        if self.offline:
            self._show_error("Create backup", OfflineOperationError())
            return
        try:
            request = self._parse_request()
        except PublicOperationError as exc:
            self._show_error("Create backup", exc)
            return
        encryption = "enabled" if request.encrypted else "not enabled"
        selected_scope = (
            "\nSelected references (exact bounded scope):\n" + "\n".join(request.messages)
            if request.messages
            else ""
        )
        if request.parent_backup_id:
            selected_scope = (
                f"\nParent: {clean_cell(request.parent_backup_id)}\n"
                "Uses the parent's saved scope, exclusions and encryption. "
                "Search/cluster references stay fixed; the query is not re-run."
            )
        self._confirm(
            "Create this backup?",
            f"Destination: {clean_cell(request.destination, max_length=400)}\n"
            f"Mode: {clean_cell(request.kind)}\nEncryption: {encryption}"
            f"{selected_scope}\n\n"
            "The operation reads mailbox data and writes a private local backup. It does not "
            "change mailbox items.",
            lambda: self._start_create(request),
            confirm_label="Create backup",
        )

    def _start_create(self, request: BackupCreateRequest) -> None:
        self._with_passphrase(
            encrypted=request.encrypted,
            purpose="create this backup",
            callback=lambda passphrase: self._execute_create(request, passphrase),
        )

    def _execute_create(
        self,
        request: BackupCreateRequest,
        passphrase: str | None,
    ) -> None:
        self._begin_operation(
            "Creating backup",
            lambda cancel, progress: self.backend.create_backup(
                request,
                passphrase=passphrase,
                cancel=cancel,
                progress=progress,
            ),
            self._backup_created,
            timeout_seconds=max(120.0, self.operation_timeout_seconds),
        )

    def _backup_created(self, result: object) -> None:
        if not isinstance(result, BackupRecordView):
            raise PublicOperationError("The backup backend returned an invalid result.")
        self._selected_id = result.backup_id
        self._set_view_state(
            ViewState.READY,
            f"Backup {clean_cell(result.backup_id, max_length=100)} completed safely.",
        )
        self.action_refresh()

    def action_resume(self) -> None:
        record = self._records_by_id.get(self._selected_id or "")
        if record is None or record.state != "incomplete":
            self._show_error(
                "Resume backup",
                PublicOperationError(
                    "Select an incomplete, resumable backup checkpoint.", retryable=False
                ),
            )
            return
        self._confirm(
            "Resume this backup?",
            f"Backup: {clean_cell(record.backup_id, max_length=180)}\n"
            f"Destination: {clean_cell(record.destination, max_length=300)}\n\n"
            "The existing checkpoint is verified before collection continues. Cancelling "
            "again leaves a resumable checkpoint.",
            lambda: self._start_resume(record),
            confirm_label="Resume backup",
        )

    def _stop_backup_collection(self, label: str) -> None:
        active_action = self._active_action
        if not self._operation_active or active_action not in {
            "Creating backup",
            "Resuming backup",
        }:
            self._show_error(
                label,
                PublicOperationError(
                    "No backup creation or resume operation is currently running.",
                    retryable=False,
                ),
            )
            return
        super().action_cancel_operation()
        self._set_view_state(
            ViewState.CANCELLED,
            f"{label} safely. The completed checkpoint was retained and can be resumed.",
        )

    def action_pause_backup(self) -> None:
        self._stop_backup_collection("Backup paused")

    def action_cancel_backup(self) -> None:
        self._stop_backup_collection("Backup cancelled")

    def _start_resume(self, record: BackupRecordView) -> None:
        self._with_passphrase(
            encrypted=record.encrypted,
            purpose="resume this backup",
            callback=lambda passphrase: self._execute_resume(record.backup_id, passphrase),
        )

    def _execute_resume(self, backup_id: str, passphrase: str | None) -> None:
        self._begin_operation(
            "Resuming backup",
            lambda cancel, progress: self.backend.resume_backup(
                backup_id,
                passphrase=passphrase,
                cancel=cancel,
                progress=progress,
            ),
            self._backup_created,
            timeout_seconds=max(120.0, self.operation_timeout_seconds),
        )

    def action_verify(self) -> None:
        record = self._records_by_id.get(self._selected_id or "")
        if record is None:
            self._show_error(
                "Verify backup",
                PublicOperationError("Select a backup to verify.", retryable=False),
            )
            return
        self._with_passphrase(
            encrypted=record.encrypted,
            purpose="verify this backup",
            callback=lambda passphrase: self._start_verify(record.backup_id, passphrase),
        )

    def _start_verify(self, backup_id: str, passphrase: str | None) -> None:
        self._begin_operation(
            "Verifying backup",
            lambda cancel, progress: self.backend.verify_backup(
                backup_id,
                passphrase=passphrase,
                cancel=cancel,
                progress=progress,
            ),
            self._verification_loaded,
            timeout_seconds=max(120.0, self.operation_timeout_seconds),
            retry_safe=True,
        )

    def _verification_loaded(self, result: object) -> None:
        if not isinstance(result, BackupVerificationView):
            raise PublicOperationError(
                "The backup backend returned an invalid verification report."
            )
        lines = [
            f"Backup: {clean_cell(result.backup_id, max_length=180)}",
            f"Integrity: {'Valid' if result.valid else 'Invalid'}",
            f"Complete: {'Yes' if result.complete else 'No'}",
            f"Checked: {result.checked_objects} objects / {human_bytes(result.checked_bytes)}",
        ]
        lines.extend(f"Warning: {clean_cell(item, max_length=300)}" for item in result.warnings)
        lines.extend(f"Error: {clean_cell(item, max_length=300)}" for item in result.errors)
        self.query_one("#backup-detail", Static).update("\n".join(lines))
        state = ViewState.READY if result.valid and result.complete else ViewState.ERROR
        self._set_view_state(
            state,
            "Verification passed." if state is ViewState.READY else "Verification found problems.",
        )

    def action_browse(self) -> None:
        self._browse_selected(folder_mode=False, requested_folder=None)

    def queue_browse_latest_complete(
        self,
        *,
        folder_mode: bool = False,
        requested_folder: str | None = None,
    ) -> None:
        """Select the newest complete local backup and open it without server access."""

        def browse_latest() -> None:
            complete = tuple(record for record in self._records if record.state == "complete")
            if not complete:
                self._show_error(
                    "Open latest backup offline",
                    PublicOperationError(
                        "No complete local backup is available. Create or finish a backup first.",
                        retryable=False,
                    ),
                )
                return
            latest = max(
                complete,
                key=lambda record: (
                    record.completed_at or record.created_at or "",
                    record.backup_id,
                ),
            )
            self._select_backup(latest.backup_id)
            self._browse_selected(
                folder_mode=folder_mode,
                requested_folder=requested_folder,
            )

        self.queue_action(
            "open latest complete backup offline",
            browse_latest,
            requires_selection=False,
        )

    def _browse_selected(
        self,
        *,
        folder_mode: bool,
        requested_folder: str | None,
    ) -> None:
        record = self._records_by_id.get(self._selected_id or "")
        if record is None:
            return
        self._with_passphrase(
            encrypted=record.encrypted,
            purpose="browse this backup offline",
            callback=lambda passphrase: self._start_browse(
                record.backup_id,
                passphrase,
                folder_mode=folder_mode,
                requested_folder=requested_folder,
            ),
        )

    def _start_browse(
        self,
        backup_id: str,
        passphrase: str | None,
        *,
        folder_mode: bool = False,
        requested_folder: str | None = None,
    ) -> None:
        self._begin_operation(
            "Opening backup offline",
            lambda cancel, progress: tuple(
                self.backend.browse_backup(
                    backup_id,
                    passphrase=passphrase,
                    cancel=cancel,
                    progress=progress,
                )
            ),
            lambda result: self._open_browse(
                backup_id,
                result,
                folder_mode=folder_mode,
                requested_folder=requested_folder,
            ),
            timeout_seconds=max(60.0, self.operation_timeout_seconds),
            retry_safe=True,
        )

    def _open_browse(
        self,
        backup_id: str,
        result: object,
        *,
        folder_mode: bool = False,
        requested_folder: str | None = None,
    ) -> None:
        items = cast(tuple[BackupItemView, ...], result)
        if not all(isinstance(item, BackupItemView) for item in items):
            raise PublicOperationError("The backup backend returned invalid item summaries.")
        self._set_view_state(ViewState.READY, "Backup opened in offline read-only mode.")
        record = self._records_by_id.get(backup_id)
        screen: BackupBrowseScreen | BackupFolderBrowseScreen
        if folder_mode:
            screen = BackupFolderBrowseScreen(
                backup_id,
                items,
                backend=self.backend,
                encrypted=bool(record and record.encrypted),
                requested_folder=requested_folder,
            )
        else:
            screen = BackupBrowseScreen(
                backup_id,
                items,
                backend=self.backend,
                encrypted=bool(record and record.encrypted),
                offline=self.offline,
            )
        self.app.push_screen(screen)

    def action_restore_plan(self) -> None:
        record = self._records_by_id.get(self._selected_id or "")
        if record is None:
            return
        self._with_passphrase(
            encrypted=record.encrypted,
            purpose="preview restore operations",
            callback=lambda passphrase: self._start_restore_plan(record.backup_id, passphrase),
        )

    def _start_restore_plan(self, backup_id: str, passphrase: str | None) -> None:
        self._begin_operation(
            "Generating restore plan",
            lambda cancel, progress: self.backend.create_restore_plan(
                backup_id,
                (),
                passphrase=passphrase,
                cancel=cancel,
                progress=progress,
            ),
            self._open_restore_plan,
            timeout_seconds=max(60.0, self.operation_timeout_seconds),
            retry_safe=True,
        )

    def _open_restore_plan(self, result: object) -> None:
        if not isinstance(result, RestorePlanView):
            raise PublicOperationError("The backup backend returned an invalid restore plan.")
        self._set_view_state(
            ViewState.READY,
            "Restore plan generated. No mailbox changes were made.",
        )
        self.app.push_screen(RestorePlanScreen(result, self.backend, offline=self.offline))

    def action_restore_selected_items(self) -> None:
        """Open the bounded selector that leads to exact preview and confirmed restore."""

        record = self._records_by_id.get(self._selected_id or "")
        if record is None or record.state != "complete":
            self._show_error(
                "Restore selected items",
                PublicOperationError("Select a complete backup first.", retryable=False),
            )
            return
        self.action_browse()

    @staticmethod
    def _default_export_destination(record: BackupRecordView) -> str:
        safe_id = "".join(
            character if character.isalnum() or character in "-_." else "_"
            for character in record.backup_id
        ).strip(".")[:120]
        filename = f"{safe_id or 'gucmail-backup'}.gucmail-backup.zip"
        return str(Path(record.destination).parent / filename)

    def action_export_backup(self) -> None:
        record = self._records_by_id.get(self._selected_id or "")
        if record is None or record.state != "complete":
            self._show_error(
                "Export backup",
                PublicOperationError("Select a complete backup first.", retryable=False),
            )
            return

        def destination_resolved(destination: str | None) -> None:
            if destination is None:
                self._set_view_state(ViewState.CANCELLED, "Backup export cancelled safely.")
                return
            self._with_passphrase(
                encrypted=record.encrypted,
                purpose="export this verified backup",
                callback=lambda passphrase: self._start_export(
                    record.backup_id,
                    destination,
                    passphrase,
                ),
            )

        self.app.push_screen(
            BackupExportPathScreen(self._default_export_destination(record)),
            destination_resolved,
        )

    def _start_export(
        self,
        backup_id: str,
        destination: str,
        passphrase: str | None,
    ) -> None:
        self._begin_operation(
            "Exporting backup",
            lambda cancel, progress: self.backend.export_backup(
                backup_id,
                destination,
                passphrase=passphrase,
                cancel=cancel,
                progress=progress,
            ),
            self._export_finished,
            timeout_seconds=max(120.0, self.operation_timeout_seconds),
        )

    def _export_finished(self, result: object) -> None:
        if not isinstance(result, str) or not result:
            raise PublicOperationError("The backup backend returned an invalid export path.")
        self.query_one("#backup-detail", Static).update(
            f"Portable verified archive exported to:\n{clean_cell(result, max_length=500)}\n"
            "The source backup remains unchanged."
        )
        self._set_view_state(ViewState.READY, "Backup export completed without overwriting files.")

    def action_prune_old_backups(self) -> None:
        self._begin_operation(
            "Planning backup retention",
            lambda cancel, progress: self.backend.plan_prune(
                self.retention_count,
                cancel=cancel,
                progress=progress,
            ),
            self._review_prune,
            retry_safe=True,
        )

    def _review_prune(self, result: object) -> None:
        if not isinstance(result, BackupPrunePlanView):
            raise PublicOperationError("The backup backend returned an invalid prune plan.")
        retained = "\n".join(f"  keep · {item}" for item in result.retained) or "  (none)"
        removed = "\n".join(f"  move to trash · {item}" for item in result.candidates) or "  (none)"
        summary = (
            f"Retention: keep {result.keep_last}\n"
            f"Potentially moved: {human_bytes(result.potential_bytes)}\n\n"
            f"Retained backup IDs:\n{retained}\n\n"
            f"Backup IDs moved to recoverable trash:\n{removed}"
        )
        self.query_one("#backup-detail", Static).update(summary)
        if not result.candidates:
            self._set_view_state(
                ViewState.READY,
                "Retention plan is already satisfied; no backups need pruning.",
            )
            return
        self._confirm(
            "Apply this exact backup retention plan?",
            summary + "\n\nNo backup is permanently deleted. Candidates are moved atomically to "
            "recoverable repository trash.",
            lambda: self._start_prune(result),
            confirm_label="Move listed backups to trash",
        )

    def _start_prune(self, plan: BackupPrunePlanView) -> None:
        self._begin_operation(
            "Applying backup retention",
            lambda cancel, progress: self.backend.execute_prune(
                plan,
                confirmed=True,
                cancel=cancel,
                progress=progress,
            ),
            self._prune_finished,
        )

    def _prune_finished(self, result: object) -> None:
        if not isinstance(result, BackupPruneResultView):
            raise PublicOperationError("The backup backend returned an invalid prune result.")
        ids = "\n".join(f"  {item}" for item in result.pruned_ids) or "  (none)"
        self.query_one("#backup-detail", Static).update(
            f"Moved to recoverable trash:\n{ids}\n"
            f"Moved bytes: {human_bytes(result.bytes_moved)}\n"
            f"{clean_cell(result.note, max_length=500)}"
        )
        self._set_view_state(
            ViewState.READY,
            f"Retention applied to {len(result.pruned_ids)} backup(s).",
        )
        self.action_refresh()

    def action_open_backup_folder(self) -> None:
        self._begin_operation(
            "Opening backup folder",
            lambda cancel, progress: self.backend.open_backup_folder(
                cancel=cancel,
                progress=progress,
            ),
            self._folder_opened,
        )

    def _folder_opened(self, result: object) -> None:
        if not isinstance(result, str) or not result:
            raise PublicOperationError("The backup backend returned an invalid folder path.")
        self._set_view_state(
            ViewState.READY,
            f"Opened backup folder: {clean_cell(result, max_length=400)}",
        )

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "backup-toggle-advanced":
            panel = self.query_one("#backup-advanced", VerticalScroll)
            panel.toggle_class("visible")
            event.button.label = "Basic" if panel.has_class("visible") else "Advanced"
        elif button_id == "backup-refresh":
            self.action_refresh()
        elif button_id == "backup-estimate":
            self.action_estimate()
        elif button_id == "backup-create":
            self.action_create()
        elif button_id == "backup-resume":
            self.action_resume()
        elif button_id == "backup-verify":
            self.action_verify()
        elif button_id == "backup-browse":
            self.action_browse()
        elif button_id == "backup-restore-plan":
            self.action_restore_plan()
        elif button_id == "backup-restore-selected":
            self.action_restore_selected_items()
        elif button_id == "backup-export":
            self.action_export_backup()
        elif button_id == "backup-prune":
            self.action_prune_old_backups()
        elif button_id == "backup-open-folder":
            self.action_open_backup_folder()


__all__ = [
    "BackupBrowseScreen",
    "BackupCreateRequest",
    "BackupEstimateView",
    "BackupExportPathScreen",
    "BackupFolderBrowseScreen",
    "BackupItemView",
    "BackupPrunePlanView",
    "BackupPruneResultView",
    "BackupRecordView",
    "BackupScreen",
    "BackupScreenBackend",
    "BackupVerificationView",
    "RestorePlanEntryView",
    "RestorePlanScreen",
    "RestorePlanView",
]
