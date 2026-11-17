"""Searchable, staged, reversible Settings control center for Textual."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from time import monotonic
from typing import Any, ClassVar, Protocol, TypeAlias, cast

from rapidfuzz import fuzz
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.timer import Timer
from textual.validation import Number
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Static, TextArea

from gucmail.services.keyboard_bindings import KEYBOARD_SETTING
from gucmail.settings import MANAGED_SETTINGS
from gucmail.tui.area_common import (
    OperationalScreen,
    ProgressReporter,
    PublicOperationError,
    ViewState,
    clean_cell,
    current_row_highlight,
)
from gucmail.tui.branding import BrandingCatalog
from gucmail.tui.events import (
    AppearanceBrowseRequested,
    ColumnPreviewRequested,
    SettingsHelpRequested,
    SettingsSetupRequested,
    ShortcutSettingRequested,
)
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.message_columns_screen import MessageColumnsScreen
from gucmail.tui.settings_form import SettingRow, SettingsForm
from gucmail.tui.themes import ThemeRegistry

SettingValue: TypeAlias = str | int | float | bool | tuple[str, ...] | None


class SettingLevel(StrEnum):
    BASIC = "basic"
    ADVANCED = "advanced"
    EXPERT = "expert"


@dataclass(frozen=True, slots=True)
class SettingView:
    """Structured metadata and redaction-safe values for one setting."""

    setting_id: str
    label: str
    description: str
    category: str
    value_type: str
    default_value: SettingValue
    current_value: SettingValue
    effective_value: SettingValue
    source: str
    keywords: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    level: SettingLevel = SettingLevel.BASIC
    scope: str = "global"
    help_topic: str | None = None
    restart_required: bool = False
    secret: bool = False
    secret_configured: bool = False
    exportable: bool = True
    editable: bool = True
    previewable: bool = False
    external_operation: bool = False


@dataclass(frozen=True, slots=True)
class SettingsCatalogView:
    settings: tuple[SettingView, ...]
    revision: str
    active_level: SettingLevel = SettingLevel.BASIC
    warning: str | None = None
    recovered_from_corruption: bool = False


@dataclass(frozen=True, slots=True)
class SettingValidationView:
    setting_id: str
    valid: bool
    normalized_value: SettingValue = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SettingsPreviewView:
    token: str
    setting_id: str
    summary: str


@dataclass(frozen=True, slots=True)
class SettingsApplyView:
    revision: str
    changed_setting_ids: tuple[str, ...]
    restart_required_setting_ids: tuple[str, ...] = ()
    message: str = "Settings applied."


@dataclass(frozen=True, slots=True)
class SettingsExportView:
    payload: str
    format_name: str
    exported_setting_count: int
    omitted_secret_count: int


@dataclass(frozen=True, slots=True)
class SettingsImportChangeView:
    setting_id: str
    label: str
    current_display: str
    proposed_display: str
    supported: bool
    reason: str | None = None
    external_operation: bool = False


@dataclass(frozen=True, slots=True)
class SettingsImportPreviewView:
    token: str
    changes: tuple[SettingsImportChangeView, ...]
    unsupported_fields: tuple[str, ...] = ()
    ignored_secret_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SettingsRevisionView:
    revision: str
    created_at: str
    summary: str
    changed_setting_ids: tuple[str, ...]
    restorable: bool = True


class SettingsScreenBackend(Protocol):
    """Atomic configuration backend; secret values never cross this protocol."""

    def load_catalog(
        self,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsCatalogView: ...

    def validate_change(
        self,
        setting_id: str,
        raw_value: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingValidationView: ...

    def preview_change(
        self,
        setting_id: str,
        value: SettingValue,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsPreviewView: ...

    def revert_preview(
        self,
        token: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> None: ...

    def apply_settings(
        self,
        changes: Mapping[str, SettingValue],
        *,
        expected_revision: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsApplyView: ...

    def reset_settings(
        self,
        scope: str,
        target: str | None,
        *,
        expected_revision: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsApplyView: ...

    def export_settings(
        self,
        scope: str,
        target: str | None,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsExportView: ...

    def preview_import(
        self,
        payload: str,
        *,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsImportPreviewView: ...

    def apply_import(
        self,
        token: str,
        *,
        expected_revision: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsApplyView: ...

    def list_history(
        self,
        *,
        limit: int,
        cancel: Event,
        progress: ProgressReporter,
    ) -> Sequence[SettingsRevisionView]: ...

    def restore_revision(
        self,
        revision: str,
        *,
        expected_revision: str,
        cancel: Event,
        progress: ProgressReporter,
    ) -> SettingsApplyView: ...


def display_setting_value(setting: SettingView, value: SettingValue) -> str:
    """Never render secret material, even if a buggy backend supplied it."""

    if setting.secret:
        return "Configured" if setting.secret_configured else "Not configured"
    if value is None:
        return {
            "calendar.default_reminder_minutes": "Reminder off",
            "tasks.default_due_days": "No due date",
            "tasks.default_reminder_minutes": "Reminder off",
            "tasks.default_repeat_count": "No end",
        }.get(
            setting.setting_id,
            "Unknown" if setting.setting_id.startswith("storage.") else "Not set",
        )
    if isinstance(value, bool):
        return "On" if value else "Off"
    if isinstance(value, tuple):
        return ", ".join(clean_cell(item, max_length=80) for item in value) or "None"
    return clean_cell(value, max_length=180)


def setting_search_score(query: str, setting: SettingView) -> float:
    """Fuzzy and phrase-aware score over structured setting metadata."""

    normalized = " ".join(query.casefold().split())
    if not normalized:
        return 100.0
    fields = (
        setting.setting_id,
        setting.label,
        setting.description,
        setting.category,
        *setting.keywords,
        *setting.aliases,
    )
    corpus = " ".join(field.casefold() for field in fields)
    if normalized in corpus:
        return 100.0
    phrase_score = max(fuzz.WRatio(normalized, field.casefold()) for field in fields)
    token_scores = [
        max(fuzz.WRatio(token, field.casefold()) for field in fields)
        for token in normalized.split()
    ]
    token_score = sum(token_scores) / len(token_scores)
    partial_score = fuzz.partial_ratio(normalized, corpus)
    return max(float(phrase_score), float(partial_score), float(token_score))


class SettingsTransferScreen(OperationalScreen):
    """Non-secret settings export and validate-before-apply import flow."""

    SHARED_WORKSPACE_LAYOUT = False

    DEFAULT_CSS = """
    SettingsTransferScreen { layout: vertical; background: $gucmail-app-background;
        color: $gucmail-primary-text; }
    #settings-transfer-root { width: 100%; height: 100%; padding: 1 3; }
    #settings-transfer-title { text-style: bold; height: 1; color: $gucmail-primary-text; }
    SettingsTransferScreen Button.-style-default { border: none; height: 1; min-height: 1;
        min-width: 0; background: transparent; color: $gucmail-primary-text; text-style: none; }
    SettingsTransferScreen Button.-style-default:focus { border: none;
        color: $gucmail-primary-accent;
        background: $gucmail-hover; text-style: none; }
    #settings-transfer-controls { height: 3; }
    #settings-transfer-mode, #settings-transfer-scope { width: 1fr; margin-right: 1; }
    #settings-transfer-payload { height: 1fr; min-height: 5; background: $gucmail-elevated-surface;
        border: solid $gucmail-divider; }
    #settings-import-table { height: 10; display: none; }
    #settings-import-table.visible { display: block; }
    #settings-transfer-detail { height: auto; min-height: 3; max-height: 7; }
    #settings-transfer-actions { height: 1; }
    #settings-transfer-actions Button { margin-right: 1; }
    #settings-transfer-footer { height: 2; padding-top: 1; align-horizontal: right; }
    #settings-transfer-footer Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """

    def __init__(
        self,
        backend: SettingsScreenBackend,
        *,
        expected_revision: str,
        category: str | None,
        initial_mode: str = "export",
        auto_run: bool = False,
    ) -> None:
        super().__init__(area_name="settings", offline=True)
        if initial_mode not in {"export", "import"}:
            raise ValueError("Settings transfer mode must be export or import.")
        self.backend = backend
        self.expected_revision = expected_revision
        self.category = category
        self.initial_mode = initial_mode
        self.auto_run = auto_run
        self._import_preview: SettingsImportPreviewView | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-transfer-root"):
            yield Label("Settings import / export", id="settings-transfer-title")
            yield Static("Choose export or import.", id="area-state", markup=False)
            with Horizontal(id="settings-transfer-controls"):
                yield Select(
                    (("Export", "export"), ("Import", "import")),
                    value=self.initial_mode,
                    allow_blank=False,
                    id="settings-transfer-mode",
                )
                yield Select(
                    (("All non-secret settings", "all"), ("Current category", "category")),
                    value="all",
                    allow_blank=False,
                    id="settings-transfer-scope",
                )
            yield TextArea(
                "",
                id="settings-transfer-payload",
                placeholder="Export appears here, or paste settings to import.",
            )
            yield DataTable(id="settings-import-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "Exports omit secrets. Imports are validated and previewed before application.",
                id="settings-transfer-detail",
                markup=False,
            )
            with Horizontal(id="settings-transfer-actions"):
                yield Button("Generate export", id="settings-generate-export", variant="primary")
                yield Button("Preview import", id="settings-preview-import")
                yield Button("Apply import", id="settings-apply-import", disabled=True)
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="settings-transfer-footer"):
                yield Button("Retry", id="area-retry")
                yield Button("Cancel operation", id="area-cancel-operation")
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.query_one("#area-retry", Button).display = False
        self.query_one("#area-cancel-operation", Button).display = False
        self.query_one("#settings-import-table", DataTable).add_columns(
            "Setting",
            "Current",
            "Proposed",
            "Status",
        )
        self.call_after_refresh(self._initialize_mode)

    def _initialize_mode(self) -> None:
        self._apply_mode(self.initial_mode)
        if self.initial_mode == "export" and self.auto_run:
            self.action_generate_export()
        elif self.initial_mode == "import":
            self.query_one("#settings-transfer-payload", TextArea).focus()

    def _scope(self) -> tuple[str, str | None]:
        scope_value = self.query_one("#settings-transfer-scope", Select).value
        if scope_value == "category":
            if self.category is None:
                raise PublicOperationError(
                    "Choose a category in Settings before exporting the current category.",
                    retryable=False,
                )
            return "category", self.category
        return "all", None

    def action_generate_export(self) -> None:
        try:
            scope, target = self._scope()
        except PublicOperationError as exc:
            self._show_error("Export settings", exc)
            return
        self._begin_operation(
            "Exporting non-secret settings",
            lambda cancel, progress: self.backend.export_settings(
                scope,
                target,
                cancel=cancel,
                progress=progress,
            ),
            self._export_loaded,
        )

    def _export_loaded(self, result: object) -> None:
        if not isinstance(result, SettingsExportView):
            raise PublicOperationError("The settings backend returned an invalid export.")
        self.query_one("#settings-transfer-payload", TextArea).load_text(result.payload)
        self.query_one("#settings-transfer-detail", Static).update(
            f"Format: {clean_cell(result.format_name)} · Exported: "
            f"{result.exported_setting_count} · Secret fields omitted: "
            f"{result.omitted_secret_count}"
        )
        self._set_view_state(ViewState.READY, "Non-secret settings export generated locally.")

    def action_preview_import(self) -> None:
        payload = self.query_one("#settings-transfer-payload", TextArea).text.strip()
        if not payload:
            self._show_error(
                "Preview import",
                PublicOperationError("Paste a settings document to preview.", retryable=False),
            )
            return
        self._begin_operation(
            "Validating settings import",
            lambda cancel, progress: self.backend.preview_import(
                payload,
                cancel=cancel,
                progress=progress,
            ),
            self._import_preview_loaded,
            retry_safe=True,
        )

    def _import_preview_loaded(self, result: object) -> None:
        if not isinstance(result, SettingsImportPreviewView):
            raise PublicOperationError("The settings backend returned an invalid import preview.")
        self._import_preview = result
        table = self.query_one("#settings-import-table", DataTable)
        table.clear()
        table.add_class("visible")
        for index, change in enumerate(result.changes):
            table.add_row(
                clean_cell(change.label, max_length=80),
                clean_cell(change.current_display, max_length=80),
                clean_cell(change.proposed_display, max_length=80),
                "Supported" if change.supported else clean_cell(change.reason or "Unsupported"),
                key=f"{change.setting_id}:{index}",
            )
        details = [
            f"Supported changes: {sum(change.supported for change in result.changes)}",
            f"Unsupported fields: {len(result.unsupported_fields)}",
            f"Secret fields ignored: {len(result.ignored_secret_fields)}",
        ]
        details.extend(f"Warning: {clean_cell(item, max_length=300)}" for item in result.warnings)
        self.query_one("#settings-transfer-detail", Static).update("\n".join(details))
        supported = any(change.supported for change in result.changes)
        self.query_one("#settings-apply-import", Button).disabled = not supported
        self._set_view_state(
            ViewState.READY if supported else ViewState.EMPTY,
            "Import preview ready; nothing has been applied."
            if supported
            else "The import contains no supported non-secret changes.",
        )

    def action_apply_import(self) -> None:
        preview = self._import_preview
        if preview is None:
            return
        external = any(change.supported and change.external_operation for change in preview.changes)
        detail = (
            f"Supported changes: {sum(change.supported for change in preview.changes)}\n"
            f"Secrets ignored: {len(preview.ignored_secret_fields)}\n\n"
            "Dangerous hooks are never activated silently."
        )
        if external:
            detail += "\nThis import includes settings backed by external system operations."
        self._confirm(
            "Apply this settings import?",
            detail,
            lambda: self._start_apply_import(preview.token),
            confirm_label="Apply import",
        )

    def _start_apply_import(self, token: str) -> None:
        self._begin_operation(
            "Applying settings import",
            lambda cancel, progress: self.backend.apply_import(
                token,
                expected_revision=self.expected_revision,
                cancel=cancel,
                progress=progress,
            ),
            self._import_applied,
        )

    def _import_applied(self, result: object) -> None:
        if not isinstance(result, SettingsApplyView):
            raise PublicOperationError("The settings backend returned an invalid apply result.")
        self.expected_revision = result.revision
        self.query_one("#settings-apply-import", Button).disabled = True
        self._set_view_state(ViewState.READY, clean_cell(result.message, max_length=300))

    @on(Select.Changed, "#settings-transfer-mode")
    def _mode_changed(self, event: Select.Changed) -> None:
        self._apply_mode(str(event.value))

    def _apply_mode(self, mode: str) -> None:
        is_export = mode == "export"
        self.query_one("#settings-generate-export", Button).display = is_export
        self.query_one("#settings-preview-import", Button).display = not is_export
        self.query_one("#settings-apply-import", Button).display = not is_export
        editor = self.query_one("#settings-transfer-payload", TextArea)
        editor.read_only = is_export
        self._set_view_state(
            ViewState.IDLE,
            "Generate a redacted export."
            if is_export
            else "Paste settings, then preview the import.",
        )

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-generate-export":
            self.action_generate_export()
        elif event.button.id == "settings-preview-import":
            self.action_preview_import()
        elif event.button.id == "settings-apply-import":
            self.action_apply_import()


class SettingsHistoryScreen(OperationalScreen):
    """Bounded settings revision history with explicit restore confirmation."""

    SHARED_WORKSPACE_LAYOUT = False

    DEFAULT_CSS = """
    SettingsHistoryScreen { layout: vertical; background: $gucmail-app-background;
        color: $gucmail-primary-text; }
    #settings-history-root { width: 100%; height: 100%; padding: 1 3; overflow-y: auto; }
    #settings-history-title { text-style: bold; height: 1; color: $gucmail-primary-text; }
    SettingsHistoryScreen Button.-style-default { border: none; height: 1; min-height: 1;
        min-width: 0; background: transparent; color: $gucmail-primary-text; text-style: none; }
    SettingsHistoryScreen Button.-style-default:focus { border: none;
        color: $gucmail-primary-accent;
        background: $gucmail-hover; text-style: none; }
    #settings-history-table { height: 1fr; min-height: 4; }
    #settings-history-detail { height: 7; border-top: solid $gucmail-divider; padding: 1; }
    #settings-history-actions { height: 2; padding-top: 1; align-horizontal: right; }
    #settings-history-actions Button { margin-left: 1; }
    #area-state, #area-progress { height: auto; min-height: 1; }
    """

    def __init__(
        self,
        backend: SettingsScreenBackend,
        *,
        expected_revision: str,
        history_limit: int = 30,
    ) -> None:
        super().__init__(area_name="settings", offline=True)
        self.backend = backend
        self.expected_revision = expected_revision
        self.history_limit = history_limit
        self._revisions: dict[str, SettingsRevisionView] = {}
        self._selected_revision: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-history-root"):
            yield Label("Settings history", id="settings-history-title")
            yield Static("Loading settings history…", id="area-state", markup=False)
            yield DataTable(id="settings-history-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "Select a revision for details.", id="settings-history-detail", markup=False
            )
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="settings-history-actions"):
                yield Button("Retry", id="area-retry")
                yield Button("Cancel operation", id="area-cancel-operation")
                yield Button("Restore revision", id="settings-restore-revision", disabled=True)
                yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.query_one("#area-retry", Button).display = False
        self.query_one("#area-cancel-operation", Button).display = False
        self.query_one("#settings-history-table", DataTable).add_columns(
            "Time",
            "Summary",
            "Changed settings",
            "Revision",
        )
        self.call_after_refresh(self.action_refresh)

    def action_refresh(self) -> None:
        self._begin_operation(
            "Loading settings history",
            lambda cancel, progress: tuple(
                self.backend.list_history(
                    limit=self.history_limit,
                    cancel=cancel,
                    progress=progress,
                )
            ),
            self._history_loaded,
            retry_safe=True,
        )

    def _history_loaded(self, result: object) -> None:
        revisions = cast(tuple[SettingsRevisionView, ...], result)
        if not all(isinstance(item, SettingsRevisionView) for item in revisions):
            raise PublicOperationError("The settings backend returned invalid revision history.")
        self._revisions = {item.revision: item for item in revisions}
        table = self.query_one("#settings-history-table", DataTable)
        table.clear()
        for item in revisions:
            table.add_row(
                clean_cell(item.created_at, max_length=35),
                clean_cell(item.summary, max_length=100),
                str(len(item.changed_setting_ids)),
                clean_cell(item.revision, max_length=60),
                key=item.revision,
            )
        if revisions:
            table.move_cursor(row=0)
            self._select_revision(revisions[0].revision)
            self._set_view_state(ViewState.READY, f"{len(revisions)} revisions available.")
        else:
            self._set_view_state(ViewState.EMPTY, "No previous settings revisions are available.")

    @on(DataTable.RowHighlighted, "#settings-history-table")
    def _revision_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        self._select_revision(str(event.row_key.value))

    def _select_revision(self, revision_id: str) -> None:
        revision = self._revisions.get(revision_id)
        if revision is None:
            return
        self._selected_revision = revision_id
        self.query_one("#settings-history-detail", Static).update(
            f"{clean_cell(revision.summary, max_length=300)}\n"
            f"Revision: {clean_cell(revision.revision, max_length=180)}\n"
            f"Changed: "
            f"{', '.join(clean_cell(item, max_length=80) for item in revision.changed_setting_ids)}"
            "\n"
            "Restoring creates a new revision; it does not erase later history."
        )
        self.query_one("#settings-restore-revision", Button).disabled = not revision.restorable

    def action_restore_revision(self) -> None:
        revision = self._revisions.get(self._selected_revision or "")
        if revision is None or not revision.restorable:
            return
        self._confirm(
            "Restore this settings revision?",
            f"Revision: {clean_cell(revision.revision, max_length=180)}\n"
            f"Changed settings: {len(revision.changed_setting_ids)}\n\n"
            "A new revision will be created. Mailbox data, backups, and credentials "
            "are not removed.",
            lambda: self._start_restore_revision(revision.revision),
            confirm_label="Restore settings",
        )

    def _start_restore_revision(self, revision: str) -> None:
        self._begin_operation(
            "Restoring settings revision",
            lambda cancel, progress: self.backend.restore_revision(
                revision,
                expected_revision=self.expected_revision,
                cancel=cancel,
                progress=progress,
            ),
            self._revision_restored,
        )

    def _revision_restored(self, result: object) -> None:
        if not isinstance(result, SettingsApplyView):
            raise PublicOperationError("The settings backend returned an invalid restore result.")
        self.expected_revision = result.revision
        self._set_view_state(ViewState.READY, clean_cell(result.message, max_length=300))
        self.action_refresh()

    @on(Button.Pressed, "#settings-restore-revision")
    def _restore_pressed(self) -> None:
        self.action_restore_revision()


class SettingsScreen(OperationalScreen):
    """Tabbed preference forms with contextual editing and staged atomic changes."""

    TABS: ClassVar[tuple[str, ...]] = ("General", "Mail", "Appearance", "Accounts", "Advanced")
    TAB_CATEGORIES: ClassVar[dict[str, tuple[str, ...]]] = {
        "General": (
            "Keyboard",
            "Notifications & Monitoring",
            "Calendar",
            "Contacts",
            "Tasks",
            "Help",
        ),
        "Mail": ("Mail", "Composer & Drafts", "Storage", "Backup", "Search & Indexing"),
        "Appearance": ("Appearance", "Layout", "Accessibility"),
        "Accounts": ("Connection", "Privacy & Security"),
    }
    OVERVIEWS: ClassVar[dict[str, tuple[str, ...]]] = {
        "Mail": (
            "mail.default_folder",
            "mail.page_size",
            "mail.fill_viewport",
            "layout.split_orientation",
            "mail.mark_read_on_open",
            "layout.reader_mode",
        ),
        "General": (
            "keyboard.bindings",
            "mail.datetime_format",
            "monitoring.enabled",
            "monitoring.desktop_notifications",
            "monitoring.quiet_hours_enabled",
            "help.contextual_suggestions",
        ),
        "Appearance": (
            "appearance.theme",
            "appearance.density",
            "appearance.border_style",
            "layout.sidebar_visible",
            "layout.preview_visible",
            "appearance.branding",
        ),
        "Accounts": (
            "connection.email",
            "connection.username",
            "connection.endpoint",
            "connection.request_timeout_seconds",
            "connection.retry_attempts",
        ),
    }
    FORM_LABELS: ClassVar[dict[str, str]] = {
        "mail.default_folder": "Default folder",
        "mail.page_size": "Page size",
        "mail.fill_viewport": "Fill visible area",
        "layout.split_orientation": "Reading pane",
        "layout.reader_mode": "Open messages",
        "keyboard.bindings": "Keyboard shortcuts",
        "appearance.branding": "Branding",
    }
    SUBTITLES: ClassVar[dict[str, str]] = {
        "General": "Make the app work your way.",
        "Mail": "Make the inbox work your way.",
        "Appearance": "Make yourself at home.",
        "Accounts": "Your connection to GUC Mail.",
        "Advanced": "All preferences, when you need them.",
    }

    LEVELS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Basic", SettingLevel.BASIC.value),
        ("Advanced", SettingLevel.ADVANCED.value),
        ("Expert", SettingLevel.EXPERT.value),
    )
    SHARED_WORKSPACE_LAYOUT = False

    DEFAULT_CSS = """
    SettingsScreen { layout: vertical; background: $gucmail-app-background;
        color: $gucmail-primary-text; layers: base editor; }
    #settings-root { width: 100%; height: 100%; padding: 1 3; background: $gucmail-app-background; }
    #settings-masthead { height: 2; }
    #settings-brand { width: 13; color: $gucmail-logo-primary; }
    #settings-page-name { width: 1fr; color: $gucmail-primary-text; }
    #settings-unsaved { width: auto; color: $gucmail-secondary-text; padding-right: 2; }
    #settings-more { width: 8; color: $gucmail-muted-text; }
    SettingsScreen Button.-style-default { min-width: 0; height: 1; min-height: 1; border: none;
        padding: 0 1; background: transparent; color: $gucmail-primary-text; text-style: none; }
    SettingsScreen Button.-style-default:hover, SettingsScreen Button.-style-default:focus {
        border: none; background: $gucmail-hover; color: $gucmail-primary-accent;
        text-style: none; }
    SettingsScreen Button.-style-default.-active {
        border: none; background: $gucmail-hover; tint: $gucmail-app-background 0%; }
    SettingsScreen Button.-style-default:disabled {
        border: none; background: transparent; color: $gucmail-disabled-text; }
    #settings-tabs { height: 3; border-bottom: solid $gucmail-divider; }
    #settings-tabs Button { width: auto; height: 2; margin-right: 2; padding: 0;
        color: $gucmail-secondary-text; }
    #settings-tabs Button.active { border-bottom: solid $gucmail-primary-accent;
        color: $gucmail-primary-text; }
    #settings-search-row { height: 2; border-bottom: solid $gucmail-divider; }
    #settings-search { width: 1fr; height: 1; min-height: 1; border: none; padding: 0 1;
        background: transparent; color: $gucmail-primary-text; }
    #settings-search:focus { border: none; background: $gucmail-elevated-surface; }
    #settings-search-hint { width: 6; height: 1; color: $gucmail-secondary-text; }
    #settings-section-row { height: 3; display: none; margin-top: 1; }
    #settings-section-row.visible { display: block; }
    #settings-section { width: 1fr; margin-right: 1; }
    #settings-level { width: 20; }
    #settings-section-row SelectCurrent { background: $gucmail-elevated-surface;
        color: $gucmail-primary-text;
        border: none; border-bottom: solid $gucmail-divider; }
    SettingsScreen SelectOverlay { background: $gucmail-elevated-surface;
        color: $gucmail-primary-text;
        border: solid $gucmail-border; }
    #settings-form-viewport { height: 1fr; width: 100%; align-horizontal: center;
        padding-top: 3; scrollbar-size: 1 1; scrollbar-color: $gucmail-divider; }
    #settings-form-column { height: auto; width: 74; max-width: 100%; }
    #settings-form-heading { height: 1; text-style: bold; padding-left: 14; }
    #settings-form-subtitle { height: 1; color: $gucmail-muted-text; padding-left: 14;
        margin: 1 0 2 0; }
    #settings-form { height: auto; }
    #settings-empty { height: auto; color: $gucmail-muted-text; padding: 2 9; display: none; }
    #settings-category-more { margin-left: 8; margin-top: 1; color: $gucmail-muted-text;
        width: auto; }
    #area-state { height: auto; max-height: 3; display: none; color: $gucmail-warning; }
    #area-progress { display: none; }
    #settings-footer { height: 3; border-top: solid $gucmail-divider; padding-top: 1; }
    #settings-footer .area-close { width: 13; padding: 0; }
    #settings-footer-hints { width: 1fr; color: $gucmail-primary-text; }
    #settings-apply { width: 17; background: $gucmail-primary-accent; color: auto 100%; }
    #settings-apply:disabled { background: transparent; color: $gucmail-disabled-text;
        opacity: 1; text-opacity: 100%; }
    #settings-cancel { width: 11; margin-left: 2; }
    #area-retry, #area-cancel-operation { width: auto; margin-right: 1; }
    #settings-advanced-actions { display: none; height: auto; padding: 1;
        background: $gucmail-elevated-surface; }
    #settings-advanced-actions.visible { display: block; layout: grid; grid-size: 4; grid-rows: 2; }
    #settings-advanced-actions Button { width: 1fr; }
    #settings-editor-overlay { display: none; layer: editor; position: absolute;
        width: 100%; height: 100%; align: center middle; background: $gucmail-app-background 85%; }
    #settings-editor-overlay.visible { display: block; }
    #settings-editor-dialog { width: 70; max-width: 94%; height: auto; max-height: 94%;
        background: $gucmail-elevated-surface; border: solid $gucmail-border; padding: 1 2; }
    #settings-edit-heading { height: auto; text-style: bold; color: $gucmail-primary-accent;
        margin-bottom: 1; }
    #settings-detail-scroll { height: auto; min-height: 2; max-height: 10; margin-bottom: 1; }
    #settings-detail { height: auto; color: $gucmail-secondary-text; }
    #settings-editor-row { height: 3; }
    #settings-edit-status { display: none; height: auto; max-height: 3; color: $gucmail-error; }
    #settings-editor, #settings-choice { width: 1fr; background: $gucmail-elevated-surface; }
    #settings-editor { height: 2; min-height: 2; border: none;
        border-bottom: solid $gucmail-border; padding: 0 1; background: $gucmail-elevated-surface; }
    #settings-editor:focus { border: none; border-bottom: solid $gucmail-primary-accent; }
    #settings-choice SelectCurrent { background: $gucmail-elevated-surface; border: none;
        border-bottom: solid $gucmail-border; }
    #settings-choice { display: none; }
    #settings-edit-actions { height: 2; min-height: 2; }
    #settings-edit-actions Button { width: auto; margin-right: 1; }
    #settings-stage { background: $gucmail-primary-accent; color: auto 100%; }
    #settings-stage:disabled { background: transparent; color: $gucmail-disabled-text; }
    SettingsScreen.short #settings-root { padding: 0 2; }
    SettingsScreen.short #settings-masthead { height: 1; }
    SettingsScreen.short #settings-tabs { height: 3; }
    SettingsScreen.short #settings-form-viewport { padding-top: 1; }
    SettingsScreen.short #settings-form-subtitle { margin: 0 0 1 0; }
    SettingsScreen.short #settings-footer { height: 2; padding-top: 0; }
    SettingsScreen.short SettingRow { height: 1; }
    SettingsScreen.short SettingRow.selected { height: 2; }
    SettingsScreen.short SettingRow.group-start { height: 3; }
    SettingsScreen.short SettingRow.selected.group-start { height: 4; }
    SettingsScreen.narrow #settings-root { padding-left: 1; padding-right: 1; }
    SettingsScreen.narrow #settings-tabs Button { margin-right: 2; }
    SettingsScreen.narrow #settings-footer-hints { content-align: left top; }
    SettingsScreen.narrow #settings-form-heading,
    SettingsScreen.narrow #settings-form-subtitle { padding-left: 9; }
    SettingsScreen.narrow #settings-advanced-actions.visible { grid-size: 2; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        *OperationalScreen.BINDINGS,
        Binding("ctrl+f", "focus_search", "Search", show=False),
        Binding("ctrl+s", "apply", "Apply", show=False),
        Binding("ctrl+z", "cancel_changes", "Cancel changes", show=False),
        Binding("ctrl+left", "previous_tab", "Previous section", show=False),
        Binding("ctrl+right", "next_tab", "Next section", show=False),
    ]
    SEARCH_LIMIT = 40
    SEARCH_DELAY = 0.15

    def __init__(
        self,
        backend: SettingsScreenBackend,
        *,
        offline: bool = False,
        search_threshold: float = 62.0,
        operation_timeout_seconds: float = OperationalScreen.DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            area_name="settings",
            offline=offline,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if not 0 <= search_threshold <= 100:
            raise ValueError("Settings search threshold must be between 0 and 100.")
        self.backend = backend
        self.search_threshold = search_threshold
        self._catalog: SettingsCatalogView | None = None
        self._settings_by_id: dict[str, SettingView] = {}
        self._visible_settings: tuple[SettingView, ...] = ()
        self._selected_category: str | None = "Mail"
        self._active_tab = "Mail"
        self._overview = True
        self._render_generation = 0
        self._render_pending = False
        self._editor_open = False
        self._save_after_edit = False
        self._selected_setting_id: str | None = "mail.fill_viewport"
        self._render_lock = asyncio.Lock()
        self._initial_form_focus = True
        self._pending_category: str | None = None
        self._pending_transfer: tuple[str, bool] | None = None
        self._pending_reset: tuple[str, str] | None = None
        self._staged: dict[str, SettingValue] = {}
        self._close_review_pending = False
        self._preview: SettingsPreviewView | None = None
        self._search_timer: Timer | None = None
        self._last_search_change = 0.0

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-root"):
            with Horizontal(id="settings-masthead"):
                yield Static("GUC Mail", id="settings-brand", markup=False)
                yield Static("Settings", id="settings-page-name", markup=False)
                yield Static("", id="settings-unsaved", markup=False)
                yield Button("More", id="settings-more")
            with Horizontal(id="settings-tabs"):
                for tab in self.TABS:
                    yield Button(
                        tab,
                        id=f"settings-tab-{tab.lower()}",
                        classes="active" if tab == "Mail" else "",
                    )
            with Horizontal(id="settings-search-row"):
                yield Input(placeholder="Search settings…", id="settings-search")
                yield Static("Ctrl+F", id="settings-search-hint", markup=False)
            with Horizontal(id="settings-section-row"):
                yield Select(
                    [("All categories", "__all__")],
                    value="__all__",
                    allow_blank=False,
                    id="settings-section",
                )
                yield Select(
                    self.LEVELS,
                    value=SettingLevel.BASIC.value,
                    allow_blank=False,
                    id="settings-level",
                )
            with VerticalScroll(id="settings-form-viewport"), Vertical(id="settings-form-column"):
                yield Static("Mail", id="settings-form-heading", markup=False)
                yield Static(self.SUBTITLES["Mail"], id="settings-form-subtitle", markup=False)
                yield SettingsForm(id="settings-form")
                yield Static("No settings match your search.", id="settings-empty", markup=False)
                yield Button("All mail settings  >", id="settings-category-more")
            with Horizontal(id="settings-advanced-actions"):
                yield Button("Import / Export", id="settings-transfer")
                yield Button("History", id="settings-history")
                yield Button("Reset category", id="settings-reset-category", disabled=True)
                yield Button("Reset all preferences", id="settings-reset-all", variant="warning")
                yield Button("Open Help topic", id="settings-help", disabled=True)
                yield Button("Browse themes", id="settings-browse-themes")
                yield Button("Browse branding", id="settings-browse-branding")
                yield Button("Refresh", id="settings-refresh")
            yield Static("Loading settings…", id="area-state", markup=False)
            yield Static("", id="area-progress", markup=False)
            with Horizontal(id="settings-footer"):
                yield Button(Text("[Esc] Back"), classes="area-close")
                yield Static(
                    "[Tab] Next   [Enter] Change", id="settings-footer-hints", markup=False
                )
                yield Button("Retry", id="area-retry")
                yield Button("Stop", id="area-cancel-operation")
                yield Button(
                    Text("[Ctrl+S] Save"), id="settings-apply", variant="primary", disabled=True
                )
                yield Button("Discard", id="settings-cancel", disabled=True)
        with Vertical(id="settings-editor-overlay"), Vertical(id="settings-editor-dialog"):
            yield Static("Change setting", id="settings-edit-heading", markup=False)
            with VerticalScroll(id="settings-detail-scroll"):
                yield Static("Select a setting for details.", id="settings-detail", markup=False)
            with Horizontal(id="settings-editor-row"):
                yield Input(placeholder="Edit selected value", id="settings-editor", disabled=True)
                yield Select([("Select a value", "")], id="settings-choice", allow_blank=False)
            yield Static("", id="settings-edit-status", markup=False)
            with Horizontal(id="settings-edit-actions"):
                yield Button("Keep change", id="settings-stage", variant="primary", disabled=True)
                yield Button("Preview", id="settings-preview", disabled=True)
                yield Button("Open setup", id="settings-managed", disabled=True)
                yield Button("Reset", id="settings-reset-setting", disabled=True)
                yield Button("Cancel", id="settings-edit-cancel")

    def on_mount(self) -> None:
        self._apply_size(self.size.width, self.size.height)
        self.query_one("#area-retry", Button).display = False
        self.query_one("#area-cancel-operation", Button).display = False
        self.call_after_refresh(self.action_refresh)

    def _apply_size(self, width: int, height: int) -> None:
        self.set_class(width < 100, "narrow")
        self.set_class(height < 34, "short")
        hints = self.query_one("#settings-footer-hints", Static)
        hints.update("[Tab] Next  [Enter] Change" if width >= 80 else "[Enter] Change")
        self._fit_editor_detail()

    def _fit_editor_detail(self) -> None:
        reserve = 3 if self.query_one("#settings-edit-status", Static).display else 0
        self.query_one("#settings-detail-scroll").styles.max_height = max(
            2, min(10, int(self.size.height * 0.94) - 14 - reserve)
        )

    def _set_view_state(self, state: ViewState, message: str) -> None:
        super()._set_view_state(state, message)
        if state is ViewState.ERROR:
            self._save_after_edit = False
        statuses = self.query("#area-state")
        if statuses:
            statuses.first(Static).display = (
                state
                in {
                    ViewState.LOADING,
                    ViewState.ERROR,
                    ViewState.OFFLINE,
                    ViewState.CANCELLED,
                }
                or self._preview is not None
                or "Restart required" in message
                or "Cancel and Refresh" in message
            )

        editor_statuses = self.query("#settings-edit-status")
        if editor_statuses:
            status = editor_statuses.first(Static)
            status.update(clean_cell(message, max_length=600))
            status.display = self._editor_open and state in {ViewState.ERROR, ViewState.LOADING}
            if self._editor_open:
                self.query_one("#area-state", Static).display = False
            self._fit_editor_detail()

    def focus_next(self, selector: str | type[Any] = "*") -> Widget | None:
        return super().focus_next("#settings-editor-dialog *" if self._editor_open else selector)

    def focus_previous(self, selector: str | type[Any] = "*") -> Widget | None:
        return super().focus_previous(
            "#settings-editor-dialog *" if self._editor_open else selector
        )

    def _activate_tab(self, tab: str) -> None:
        self._close_editor(restore_focus=False)
        self._active_tab = tab
        self._overview = tab != "Advanced"
        self._selected_category = {
            "Mail": "Mail",
            "Appearance": "Appearance",
            "Accounts": "Connection",
        }.get(tab)
        self._selected_setting_id = "mail.fill_viewport" if tab == "Mail" else None
        with self.prevent(Input.Changed, Select.Changed):
            self.query_one("#settings-search", Input).value = ""
            if tab == "Advanced":
                self.query_one("#settings-level", Select).value = SettingLevel.EXPERT.value
        self._render_categories()
        self._render_settings()
        self.query_one("#settings-form-viewport", VerticalScroll).scroll_home(animate=False)

    def action_next_tab(self) -> None:
        self._activate_tab(self.TABS[(self.TABS.index(self._active_tab) + 1) % len(self.TABS)])

    def action_previous_tab(self) -> None:
        self._activate_tab(self.TABS[(self.TABS.index(self._active_tab) - 1) % len(self.TABS)])

    def action_edit_setting(self) -> None:
        setting = self._settings_by_id.get(self._selected_setting_id or "")
        if setting is None:
            return
        self._configure_editor(setting)
        self._editor_open = True
        self.query_one("#settings-edit-status", Static).display = False
        self._fit_editor_detail()
        self.query_one("#settings-editor-overlay").add_class("visible")
        self.query_one("#settings-edit-heading", Static).update(
            self.FORM_LABELS.get(setting.setting_id, setting.label)
        )
        self.call_after_refresh(self._focus_editor)

    def _focus_editor(self) -> None:
        if not self._editor_open:
            return
        if (
            self.focused is not None
            and self.focused in self.query("#settings-editor-dialog *")
            and self.focused.display
            and not self.focused.disabled
        ):
            return
        for selector in (
            "#settings-choice",
            "#settings-editor",
            "#settings-managed",
            "#settings-edit-cancel",
        ):
            widget = self.query_one(selector)
            if widget.display and not widget.disabled:
                self.set_focus(widget)
                break

    def _close_editor(self, *, restore_focus: bool = True) -> None:
        self._editor_open = False
        self._save_after_edit = False
        self.query_one("#settings-editor-overlay").remove_class("visible")
        if restore_focus and self._selected_setting_id is not None:
            self.query_one("#settings-form", SettingsForm).focus_setting(self._selected_setting_id)

    @on(Select.Changed, "#settings-section")
    def _section_changed(self, event: Select.Changed) -> None:
        if self._catalog is None:
            return
        self._selected_category = None if str(event.value) == "__all__" else str(event.value)
        self._selected_setting_id = None
        self._render_settings()

    def action_refresh(self) -> None:
        self._begin_operation(
            "Loading settings",
            lambda cancel, progress: self.backend.load_catalog(
                cancel=cancel,
                progress=progress,
            ),
            self._catalog_loaded,
            retry_safe=True,
        )

    def refresh_after_external_change(self, *, changed: str = "Keyboard assignments") -> None:
        """Refresh a clean view, retaining any independently staged preferences."""
        if self._staged or self._preview is not None:
            self._set_view_state(
                ViewState.READY,
                f"{changed} changed. Your staged preferences remain; "
                "Cancel and Refresh before applying them.",
            )
        else:
            self.action_refresh()

    def _catalog_loaded(self, result: object) -> None:
        if not isinstance(result, SettingsCatalogView):
            raise PublicOperationError("The settings backend returned an invalid catalog.")
        first_load = self._catalog is None
        self._catalog = result
        self._settings_by_id = {setting.setting_id: setting for setting in result.settings}
        if first_load:
            with self.prevent(Select.Changed):
                self.query_one("#settings-level", Select).value = result.active_level.value
        self._render_categories()
        self._render_settings()
        if result.recovered_from_corruption:
            message = "Recovered settings from the last known-good revision."
            if result.warning:
                message += f" {clean_cell(result.warning, max_length=300)}"
            self._set_view_state(ViewState.ERROR, message)
        elif result.settings:
            mode = "Offline-safe local settings" if self.offline else "Settings ready"
            suffix = f" · {clean_cell(result.warning, max_length=300)}" if result.warning else ""
            self._set_view_state(ViewState.READY, f"{mode}{suffix}")
            if result.warning:
                self.query_one("#area-state", Static).display = True
        else:
            self._set_view_state(ViewState.EMPTY, "No settings metadata is available.")
        pending_category = self._pending_category
        self._pending_category = None
        if pending_category is not None:
            self.action_open_category(pending_category)
        pending_transfer = self._pending_transfer
        self._pending_transfer = None
        if pending_transfer is not None:
            self.action_open_transfer(*pending_transfer)
        pending_reset = self._pending_reset
        self._pending_reset = None
        if pending_reset is not None:
            self.queue_reset(*pending_reset)

    def _selected_level(self) -> SettingLevel:
        value = self.query_one("#settings-level", Select).value
        try:
            return SettingLevel(str(value))
        except ValueError:
            return SettingLevel.BASIC

    @staticmethod
    def _level_visible(setting: SettingView, level: SettingLevel) -> bool:
        rank = {
            SettingLevel.BASIC: 0,
            SettingLevel.ADVANCED: 1,
            SettingLevel.EXPERT: 2,
        }
        return rank[setting.level] <= rank[level]

    def _matching_settings(self) -> tuple[SettingView, ...]:
        catalog = self._catalog
        if catalog is None:
            return ()
        level = self._selected_level()
        query = self.query_one("#settings-search", Input).value.strip()
        if not query and self._overview:
            return tuple(
                self._settings_by_id[setting_id]
                for setting_id in self.OVERVIEWS.get(self._active_tab, ())
                if setting_id in self._settings_by_id
            )
        matches: list[tuple[float, SettingView]] = []
        for setting in catalog.settings:
            if not query and not self._level_visible(setting, level):
                continue
            if query:
                score = setting_search_score(query, setting)
                if score < self.search_threshold:
                    continue
            else:
                score = 100.0
                if self._selected_category and setting.category != self._selected_category:
                    continue
                if (
                    self._selected_category is None
                    and self._active_tab != "Advanced"
                    and setting.category not in self.TAB_CATEGORIES.get(self._active_tab, ())
                ):
                    continue
            matches.append((score, setting))
        matches.sort(
            key=lambda item: (
                query.casefold()
                not in {
                    item[1].setting_id.casefold(),
                    item[1].label.casefold(),
                    *(alias.casefold() for alias in item[1].aliases),
                },
                -item[0],
                item[1].category.casefold(),
                item[1].label.casefold(),
            )
        )
        if query:
            matches = matches[: self.SEARCH_LIMIT]
        return tuple(item[1] for item in matches)

    def _render_categories(self) -> None:
        catalog = self._catalog
        if catalog is None:
            return
        categories = tuple(dict.fromkeys(setting.category for setting in catalog.settings))
        if self._active_tab != "Advanced":
            categories = tuple(
                category
                for category in categories
                if category in self.TAB_CATEGORIES.get(self._active_tab, ())
            )
        section = self.query_one("#settings-section", Select)
        with self.prevent(Select.Changed):
            section.set_options(
                [("All categories", "__all__"), *((name, name) for name in categories)]
            )
            section.value = (
                self._selected_category if self._selected_category in categories else "__all__"
            )
        for tab in self.TABS:
            self.query_one(f"#settings-tab-{tab.lower()}", Button).set_class(
                tab == self._active_tab, "active"
            )

    def _render_settings(self) -> None:
        settings = self._matching_settings()
        self._visible_settings = settings
        query = self.query_one("#settings-search", Input).value.strip()
        heading = (
            "Search"
            if query
            else self._active_tab
            if self._overview
            else self._selected_category or self._active_tab
        )
        self.query_one("#settings-form-heading", Static).update(heading)
        self.query_one("#settings-form-subtitle", Static).update(
            f"{len(settings)} matching preferences"
            if query
            else self.SUBTITLES.get(heading, "Choose a preference to change it.")
        )
        self.query_one("#settings-section-row").set_class(
            not self._overview and not query, "visible"
        )
        more = self.query_one("#settings-category-more", Button)
        more.display = self._overview and not query
        more.label = f"All {self._active_tab.lower()} settings  >"
        self.query_one("#settings-empty").display = not settings
        form = self.query_one("#settings-form", SettingsForm)
        had_focus = isinstance(self.focused, SettingRow)
        if settings:
            selected = (
                self._selected_setting_id
                if self._selected_setting_id in {item.setting_id for item in settings}
                else settings[0].setting_id
            )
            self._select_setting(selected)
        else:
            self._selected_setting_id = None
            self._configure_editor(None)
            self.query_one("#settings-detail", Static).update("No settings match. Try fewer words.")
            self._set_view_state(ViewState.EMPTY, "No settings match this search.")
        self._render_generation += 1
        generation = self._render_generation
        self._render_pending = True

        async def rebuild() -> None:
            async with self._render_lock:
                if generation != self._render_generation or not form.is_mounted:
                    return
                await form.remove_children()
                if generation != self._render_generation:
                    return
                rows = []
                for index, setting in enumerate(settings):
                    value = self._staged.get(setting.setting_id, setting.effective_value)
                    label = self.FORM_LABELS.get(setting.setting_id, setting.label)
                    display = self._present_value(setting, value)
                    if setting.setting_id in MANAGED_SETTINGS:
                        # The real state lives outside settings (e.g. a Windows task);
                        # a stored checkbox here showed "Off" while monitoring ran.
                        display = "Managed · Open setup"
                    elif isinstance(value, bool) and not setting.secret:
                        display = f"[{'x' if value else ' '}] {display}"
                    description = (
                        "Load enough headers to fill the window."
                        if setting.setting_id == "mail.fill_viewport"
                        else clean_cell(setting.description, max_length=500)
                    )
                    rows.append(
                        SettingRow(
                            setting.setting_id,
                            index + 1,
                            clean_cell(label),
                            display,
                            description,
                            group_start=self._overview
                            and self._active_tab == "Mail"
                            and index == 3,
                            staged=setting.setting_id in self._staged,
                        )
                    )
                if rows:
                    await form.mount(*rows)
                if generation != self._render_generation:
                    return
                for row in form.query(SettingRow):
                    row.set_selected(row.setting_id == self._selected_setting_id)
                self._render_pending = False
                # Row mounting may finish after the user moved to Save,
                # search or another control. Restore only the old form context;
                # a stale snapshot must not take focus from a newer choice.
                current_focus = self.focused
                restore_form_focus = self._initial_form_focus or (
                    had_focus
                    and (
                        current_focus is None
                        or current_focus is self.query_one("#settings-form-viewport")
                        or (
                            isinstance(current_focus, SettingRow)
                            and current_focus.setting_id == self._selected_setting_id
                        )
                    )
                )
                if not self._editor_open and restore_form_focus:
                    self._initial_form_focus = False
                    if self._selected_setting_id is not None:
                        form.focus_setting(self._selected_setting_id)

        self.call_later(rebuild)
        self._update_action_buttons()

    @on(SettingRow.Selected)
    def _form_row_selected(self, event: SettingRow.Selected) -> None:
        if self._render_pending or not event.row.has_focus:
            return
        if event.row not in self.query("#settings-form SettingRow"):
            return
        if event.row.setting_id != self._selected_setting_id:
            self._select_setting(event.row.setting_id)

    @on(SettingRow.EditRequested)
    def _form_row_edit_requested(self, event: SettingRow.EditRequested) -> None:
        if self._render_pending or event.row not in self.query("#settings-form SettingRow"):
            return
        self._select_setting(event.row.setting_id)
        self.action_edit_setting()

    def _select_setting(self, setting_id: str) -> None:
        setting = self._settings_by_id.get(setting_id)
        if setting is None:
            return
        self._selected_setting_id = setting_id
        for row in self.query("#settings-form SettingRow"):
            if isinstance(row, SettingRow):
                row.set_selected(row.setting_id == setting_id)
        current = self._present_value(setting, setting.current_value)
        effective = self._present_value(
            setting,
            self._staged.get(setting.setting_id, setting.effective_value),
        )
        options = (
            f"Options: {', '.join(clean_cell(item) for item in setting.options)}\n"
            if setting.options
            else ""
        )
        bounds = ""
        if setting.minimum is not None or setting.maximum is not None:
            lower = "unbounded" if setting.minimum is None else f"{setting.minimum:g}"
            upper = "unbounded" if setting.maximum is None else f"{setting.maximum:g}"
            bounds = f"Range: {lower} to {upper}\n"
        details = (
            f"{clean_cell(setting.description, max_length=500)}\n"
            f"Value: {effective} · Default: "
            f"{self._present_value(setting, setting.default_value)}\n"
            f"{bounds}"
        )
        if setting.restart_required:
            details += "Restart required after applying.\n"
        if current != effective:
            details += f"Saved value: {current} · Source: {clean_cell(setting.source)}\n"
        if self.query_one("#settings-level", Select).value != SettingLevel.BASIC.value:
            raw_value = display_setting_value(
                setting, self._staged.get(setting_id, setting.effective_value)
            )
            details += (
                f"ID: {clean_cell(setting.setting_id)} · Type: {clean_cell(setting.value_type)}\n"
                f"Raw value: {raw_value}\n"
                f"Source: {clean_cell(setting.source)} · Scope: {clean_cell(setting.scope)} · "
                f"Level: {setting.level.value.title()}\n{options}"
                f"Help: {clean_cell(setting.help_topic or 'No direct topic')}"
            )
        if setting.secret:
            details += "\nSecret values are never displayed or exported."
        if setting.external_operation:
            details += (
                "\nChanging this setting invokes an external system operation after confirmation."
            )
        self.query_one("#settings-detail", Static).update(details)
        self._configure_editor(setting)
        self._update_action_buttons()

    def _choice_label(self, setting: SettingView, value: str) -> str:
        if setting.setting_id == "layout.split_orientation":
            return {"vertical": "Right", "horizontal": "Below"}.get(value, clean_cell(value))
        if setting.setting_id == "appearance.theme":
            registry = getattr(getattr(self.app, "theme_browser_backend", None), "registry", None)
            registry = registry or ThemeRegistry()
            try:
                return clean_cell(registry.get(value).name)
            except ValueError:
                return clean_cell(value)
        if setting.setting_id == "appearance.branding":
            if value in {"custom", "none"}:
                return {"custom": "Custom artwork", "none": "No branding"}[value]
            catalog = getattr(getattr(self.app, "branding_browser_backend", None), "catalog", None)
            catalog = catalog or BrandingCatalog()
            try:
                return clean_cell(catalog.get(value).name)
            except ValueError:
                return clean_cell(value)
        return clean_cell(value.replace("_", " ").capitalize())

    def _present_value(self, setting: SettingView, value: SettingValue) -> str:
        if setting.setting_id == "mail.default_folder" and value == "inbox":
            return "Inbox"
        if setting.setting_id == "layout.split_orientation" and value in {"vertical", "horizontal"}:
            return "Right" if value == "vertical" else "Below"
        if (
            not setting.secret
            and isinstance(value, str)
            and (
                setting.options or setting.setting_id in {"appearance.theme", "appearance.branding"}
            )
        ):
            return self._choice_label(setting, value)
        return display_setting_value(setting, value)

    def _configure_editor(self, setting: SettingView | None) -> None:
        editor = self.query_one("#settings-editor", Input)
        choice = self.query_one("#settings-choice", Select)
        choice.display = False
        editor.display = True
        editor.type = "text"
        editor.validators = []
        if setting is None or not setting.editable or setting.secret:
            editor.value = ""
            editor.disabled = True
            editor.placeholder = (
                "Use the secure credential/integration workflow"
                if setting is not None and setting.secret
                else "Select an editable setting"
            )
            return
        editor.disabled = False
        if setting.value_type in {"integer", "number", "optional_number"}:
            editor.type = "integer" if setting.value_type == "integer" else "number"
            editor.validators = [Number(minimum=setting.minimum, maximum=setting.maximum)]
        value = self._staged.get(setting.setting_id, setting.current_value)
        choices: list[tuple[str, str]] = [
            (self._choice_label(setting, option), option) for option in setting.options
        ]
        if isinstance(value, bool):
            choices = [("On", "on"), ("Off", "off")]
        elif setting.setting_id == "appearance.theme":
            registry = getattr(getattr(self.app, "theme_browser_backend", None), "registry", None)
            registry = registry or ThemeRegistry()
            choices = sorted(
                ((f"{theme.name} · {theme.mode.value}", theme.id) for theme in registry.themes),
                key=lambda item: item[0].casefold(),
            )
        if setting.value_type == "structured":
            editor.value = json.dumps(value)
        elif isinstance(value, tuple):
            editor.value = ", ".join(value)
        elif isinstance(value, bool):
            editor.value = "on" if value else "off"
        elif value is None:
            editor.value = ""
        else:
            editor.value = str(value)
        editor.placeholder = clean_cell(setting.value_type, max_length=80)
        if choices:
            selected = editor.value
            if selected not in {option for _, option in choices}:
                choices.append((selected or "Default", selected))
            choice.set_options(choices)
            choice.value = selected
            choice.disabled = False
            choice.display = True
            editor.display = False
        if setting.setting_id in {"layout.message_columns", KEYBOARD_SETTING}:
            editor.display = False

    def on_resize(self, event: Resize) -> None:
        self._apply_size(event.size.width, event.size.height)

    def _update_action_buttons(self) -> None:
        setting = self._settings_by_id.get(self._selected_setting_id or "")
        editable = setting is not None and setting.editable and not setting.secret
        managed = self.query_one("#settings-managed", Button)
        appearance = setting is not None and setting.setting_id in {
            "appearance.theme",
            "appearance.branding",
        }
        columns = setting is not None and setting.setting_id == "layout.message_columns"
        keyboard = setting is not None and setting.setting_id == KEYBOARD_SETTING
        managed.disabled = not (
            appearance
            or (columns and editable)
            or (keyboard and editable)
            or (setting is not None and setting.setting_id in MANAGED_SETTINGS)
        )
        managed.label = (
            "Edit keys"
            if keyboard
            else "Edit columns"
            if columns
            else "Browse choices"
            if appearance
            else "Open setup"
        )
        self.query_one("#settings-stage", Button).disabled = not editable or columns or keyboard
        self.query_one("#settings-preview", Button).disabled = not (
            editable and setting is not None and setting.previewable
        )
        self.query_one("#settings-reset-setting", Button).disabled = setting is None
        self.query_one("#settings-help", Button).disabled = (
            setting is None or not setting.help_topic
        )
        dirty = bool(self._staged or self._preview)
        self.query_one("#settings-apply", Button).disabled = not self._staged
        self.query_one("#settings-cancel", Button).disabled = not dirty
        self.query_one("#settings-preview", Button).display = (
            editable and setting is not None and setting.previewable
        )
        managed.display = not managed.disabled
        self.query_one("#settings-stage", Button).display = (
            editable and not columns and not keyboard
        )
        self.query_one("#settings-reset-category", Button).disabled = (
            self._selected_category is None
        )
        count = len(self._staged)
        self.query_one("#settings-unsaved", Static).update(f"{count} unsaved" if count else "")

    def _validate_editor(self, *, preview: bool) -> None:
        setting = self._settings_by_id.get(self._selected_setting_id or "")
        if setting is None or not setting.editable or setting.secret:
            self._show_error(
                "Edit setting",
                PublicOperationError("Select an editable non-secret setting.", retryable=False),
            )
            return
        raw = self.query_one("#settings-editor", Input).value
        editor = self.query_one("#settings-editor", Input)
        if editor.display and raw.strip() and editor.validators:
            validation = editor.validate(raw)
            if validation is not None and not validation.is_valid:
                self._show_error(
                    "Edit setting",
                    PublicOperationError(
                        " ".join(validation.failure_descriptions), retryable=False
                    ),
                )
                return
        choice = self.query_one("#settings-choice", Select)
        if choice.display and choice.value is not Select.BLANK:
            raw = str(choice.value)
        if setting.setting_id == "layout.message_columns":
            raw = json.dumps(self._staged.get(setting.setting_id, setting.current_value))
        self._begin_operation(
            "Validating setting",
            lambda cancel, progress: self.backend.validate_change(
                setting.setting_id,
                raw,
                cancel=cancel,
                progress=progress,
            ),
            lambda result: self._validation_loaded(result, setting.setting_id, preview),
            retry_safe=True,
        )

    def _validation_loaded(self, result: object, setting_id: str, preview: bool) -> None:
        if not isinstance(result, SettingValidationView) or result.setting_id != setting_id:
            raise PublicOperationError(
                "The settings backend returned an invalid validation result."
            )
        if not result.valid:
            self._save_after_edit = False
            message = "\n".join(clean_cell(item, max_length=300) for item in result.errors)
            raise PublicOperationError(message or "The setting value is invalid.", retryable=False)
        save_after_edit = self._save_after_edit
        self._staged[setting_id] = result.normalized_value
        self._close_editor()
        self._render_settings()
        warnings = " ".join(clean_cell(item, max_length=240) for item in result.warnings)
        self._set_view_state(ViewState.READY, f"Change staged. {warnings}".strip())
        if warnings:
            self.query_one("#area-state", Static).display = True
        if preview:
            self._start_preview(setting_id, result.normalized_value)
        elif save_after_edit:
            self.action_apply()

    def _start_preview(self, setting_id: str, value: SettingValue) -> None:
        self._begin_operation(
            "Previewing setting",
            lambda cancel, progress: self.backend.preview_change(
                setting_id,
                value,
                cancel=cancel,
                progress=progress,
            ),
            self._preview_loaded,
        )

    def _preview_loaded(self, result: object) -> None:
        if not isinstance(result, SettingsPreviewView):
            raise PublicOperationError("The settings backend returned an invalid preview token.")
        self._preview = result
        self._set_view_state(
            ViewState.READY,
            f"Live preview active: {clean_cell(result.summary, max_length=300)}. "
            "Apply to keep it or Cancel to restore the exact prior state.",
        )
        self._update_action_buttons()

        if result.setting_id == "layout.message_columns":
            self.post_message(ColumnPreviewRequested(self))

    def action_apply(self) -> None:
        if self._editor_open and not self.query_one("#settings-stage", Button).disabled:
            self._save_after_edit = True
            self._validate_editor(preview=False)
            return
        catalog = self._catalog
        if catalog is None or not self._staged:
            return
        external = [
            setting.label
            for setting_id in self._staged
            if (setting := self._settings_by_id.get(setting_id)) is not None
            and setting.external_operation
        ]
        if external:
            self._confirm(
                "Apply settings with external effects?",
                "External operations:\n"
                + "\n".join(f"- {clean_cell(label, max_length=120)}" for label in external)
                + "\n\nProgress and resulting external state will be reported explicitly.",
                self._start_apply,
                confirm_label="Apply settings",
            )
        else:
            self._start_apply()

    def _start_apply(self) -> None:
        catalog = self._catalog
        if catalog is None:
            return
        changes = dict(self._staged)
        self._begin_operation(
            "Applying settings",
            lambda cancel, progress: self.backend.apply_settings(
                changes,
                expected_revision=catalog.revision,
                cancel=cancel,
                progress=progress,
            ),
            self._settings_applied,
        )

    def _settings_applied(self, result: object) -> None:
        if not isinstance(result, SettingsApplyView):
            raise PublicOperationError("The settings backend returned an invalid apply result.")
        self._staged.clear()
        self._preview = None
        restart = (
            " Restart required for: "
            f"{', '.join(clean_cell(item) for item in result.restart_required_setting_ids)}."
            if result.restart_required_setting_ids
            else ""
        )
        self._set_view_state(
            ViewState.READY,
            f"{clean_cell(result.message, max_length=300)}{restart}",
        )
        self.action_refresh()

    def action_cancel_changes(self) -> None:
        self._close_editor()
        preview = self._preview
        if preview is None:
            self._finish_cancel_changes(None)
            return
        self._begin_operation(
            "Cancelling live preview",
            lambda cancel, progress: self.backend.revert_preview(
                preview.token,
                cancel=cancel,
                progress=progress,
            ),
            self._finish_cancel_changes,
        )

    def action_close(self) -> None:
        if self._editor_open:
            if self._guard_pending_mutation():
                return
            if self._operation_active:
                self.action_cancel_operation()
            self._close_editor()
            return
        tools = self.query_one("#settings-advanced-actions")
        if tools.has_class("visible"):
            tools.remove_class("visible")
            self.query_one("#settings-more", Button).label = "More"
            return
        if self._guard_pending_mutation():
            return
        if self._staged:
            if self._close_review_pending:
                return
            staged = deepcopy(self._staged)
            self._close_review_pending = True

            def keep_editing() -> None:
                self._close_review_pending = False

            def discard_and_leave() -> None:
                self._close_review_pending = False
                if self._staged != staged:
                    self._set_view_state(
                        ViewState.ERROR,
                        "Preferences changed during review. Review them before leaving Settings.",
                    )
                    return
                self._leave_settings()

            count = len(staged)
            noun = "change" if count == 1 else "changes"
            self._confirm(
                "Unsaved changes",
                f"You have {count} unsaved {noun}.\n\nDiscard them and leave Settings?\n"
                "Any active preview will be restored. Cancel keeps your edits.",
                discard_and_leave,
                confirm_label="Discard and leave",
                on_cancel=keep_editing,
            )
            return
        self._leave_settings()

    def _leave_settings(self) -> None:
        """Close only after dirty-state review, reverting the exact preview first."""
        preview = self._preview
        if preview is None:
            super().action_close()
            return

        def restored(_result: object) -> None:
            self._finish_cancel_changes(None)
            super(SettingsScreen, self).action_close()

        self._begin_operation(
            "Restoring preview before closing",
            lambda cancel, progress: self.backend.revert_preview(
                preview.token, cancel=cancel, progress=progress
            ),
            restored,
        )

    def on_unmount(self) -> None:
        super().on_unmount()
        cleanup = getattr(self.backend, "abandon_previews", None)
        if callable(cleanup):
            cleanup()

    def _finish_cancel_changes(self, _result: object) -> None:
        self._staged.clear()
        self._preview = None
        self._render_settings()
        self._set_view_state(
            ViewState.READY,
            "Staged changes cancelled; the exact pre-preview state was restored.",
        )
        self._update_action_buttons()

    def _request_reset(self, scope: str, target: str | None, label: str) -> None:
        catalog = self._catalog
        if catalog is None:
            return
        self._confirm(
            f"Reset {label}?",
            "Only the selected preference scope is reset. Mailbox contents, backups, credentials, "
            "scheduled messages, and durable operational data are not deleted.",
            lambda: self._start_reset(scope, target, catalog.revision),
            confirm_label="Reset preferences",
        )

    def _start_reset(self, scope: str, target: str | None, revision: str) -> None:
        self._begin_operation(
            "Resetting preferences",
            lambda cancel, progress: self.backend.reset_settings(
                scope,
                target,
                expected_revision=revision,
                cancel=cancel,
                progress=progress,
            ),
            self._settings_applied,
        )

    def action_reset_setting(self) -> None:
        setting = self._settings_by_id.get(self._selected_setting_id or "")
        if setting is not None:
            self._request_reset("setting", setting.setting_id, setting.label)

    def action_reset_category(self) -> None:
        if self._selected_category is not None:
            self._request_reset("category", self._selected_category, self._selected_category)

    def action_reset_all(self) -> None:
        self._request_reset("all", None, "all preferences")

    def action_open_category(self, category: str) -> None:
        """Select an exact settings category, queuing safely until metadata loads."""

        catalog = self._catalog
        if catalog is None:
            self._pending_category = category
            return
        categories = tuple(dict.fromkeys(setting.category for setting in catalog.settings))
        selected = next(
            (candidate for candidate in categories if candidate.casefold() == category.casefold()),
            None,
        )
        if selected is None:
            self._show_error(
                "Open settings category",
                PublicOperationError(f"Unknown settings category: {category}", retryable=False),
            )
            return
        self._close_editor(restore_focus=False)
        self._active_tab = next(
            (tab for tab, names in self.TAB_CATEGORIES.items() if selected in names), "Advanced"
        )
        self._overview = False
        with self.prevent(Input.Changed, Select.Changed):
            self.query_one("#settings-search", Input).value = ""
            self.query_one("#settings-level", Select).value = SettingLevel.EXPERT.value
        self._selected_category = selected
        self._selected_setting_id = None
        self._render_categories()
        self._render_settings()
        self.query_one("#settings-form-viewport", VerticalScroll).scroll_home(animate=False)

    def action_search(self, query: str) -> None:
        search = self.query_one("#settings-search", Input)
        search.value = query.strip()
        search.focus()

    def action_select_setting(self, query: str) -> bool:
        """Select one exact or fuzzy setting and make its editor visible."""

        catalog = self._catalog
        if catalog is None:
            return False
        normalized = query.strip().casefold()
        exact = next(
            (
                setting
                for setting in catalog.settings
                if normalized in {setting.setting_id.casefold(), setting.label.casefold()}
            ),
            None,
        )
        if exact is None:
            ranked = sorted(
                (
                    (setting_search_score(query, setting), setting.setting_id, setting)
                    for setting in catalog.settings
                ),
                key=lambda item: (-item[0], item[1]),
            )
            exact = ranked[0][2] if ranked and ranked[0][0] >= self.search_threshold else None
        if exact is None:
            self._show_error(
                "Select setting",
                PublicOperationError(f"No setting matches: {query}", retryable=False),
            )
            return False
        if not self._level_visible(exact, self._selected_level()):
            self.query_one("#settings-level", Select).value = exact.level.value
        self._selected_category = None
        # The exact selection is rendered synchronously below. A second queued
        # search render must not reset an editor the user has started typing in.
        with self.prevent(Input.Changed):
            self.query_one("#settings-search", Input).value = exact.setting_id
        # Set the target before rebuilding the table. Otherwise a related old
        # row can remain the cursor target and its queued highlight replaces
        # this exact selection (for example header contents -> compactness).
        self._selected_setting_id = exact.setting_id
        self._render_categories()
        self._render_settings()
        self._select_setting(exact.setting_id)
        self.action_edit_setting()
        return True

    def queue_reset(self, scope: str, target: str) -> None:
        if self._catalog is None:
            self._pending_reset = (scope, target)
            return
        if scope == "category":
            self.action_open_category(target)
            self.action_reset_category()
        elif scope == "setting" and self.action_select_setting(target):
            self.action_reset_setting()

    def action_open_transfer(self, mode: str = "export", auto_run: bool = False) -> None:
        catalog = self._catalog
        if catalog is None:
            self._pending_transfer = (mode, auto_run)
            return
        screen = SettingsTransferScreen(
            self.backend,
            expected_revision=catalog.revision,
            category=self._selected_category,
            initial_mode=mode,
            auto_run=auto_run,
        )
        self.app.push_screen(screen, lambda _result: self.action_refresh())

    def action_open_history(self) -> None:
        catalog = self._catalog
        if catalog is None:
            return
        screen = SettingsHistoryScreen(self.backend, expected_revision=catalog.revision)
        self.app.push_screen(screen, lambda _result: self.action_refresh())

    def action_focus_search(self) -> None:
        self.query_one("#settings-search", Input).focus()

    @on(Input.Changed, "#settings-search")
    def _search_changed(self) -> None:
        # Rebuild at once for a single change. While typing fast, wait for a pause.
        now = monotonic()
        typing = now - self._last_search_change < self.SEARCH_DELAY * 2
        self._last_search_change = now
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None
        if typing:
            self._search_timer = self.set_timer(self.SEARCH_DELAY, self._apply_search)
        else:
            self._apply_search()

    def _apply_search(self) -> None:
        self._search_timer = None
        if self._catalog is not None and self.is_mounted:
            self._selected_setting_id = None
            self._render_settings()
            self.query_one("#settings-form-viewport", VerticalScroll).scroll_home(animate=False)

    @on(Input.Submitted, "#settings-search")
    def _search_submitted(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
            self._apply_search()
        if self._render_pending:
            # Rows are still mounting. The rebuild focuses the selected row when done.
            self._initial_form_focus = True
        elif self._selected_setting_id is not None:
            self.query_one("#settings-form", SettingsForm).focus_setting(self._selected_setting_id)

    @on(Input.Submitted, "#settings-editor")
    def _editor_submitted(self) -> None:
        self._validate_editor(preview=False)

    @on(Select.Changed, "#settings-level")
    def _level_changed(self) -> None:
        if self._catalog is not None:
            self._render_categories()
            self._render_settings()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id is not None and button_id.startswith("settings-tab-"):
            self._activate_tab(button_id.removeprefix("settings-tab-").title())
        elif button_id == "settings-category-more":
            self._overview = False
            self._render_categories()
            self._render_settings()
            self.query_one("#settings-form-viewport", VerticalScroll).scroll_home(animate=False)
        elif button_id == "settings-edit-cancel":
            self.action_close()
        elif button_id == "settings-refresh":
            self.action_refresh()
        elif button_id == "settings-stage":
            self._validate_editor(preview=False)
        elif button_id == "settings-preview":
            self._validate_editor(preview=True)
        elif button_id == "settings-apply":
            self.action_apply()
        elif button_id == "settings-cancel":
            self.action_cancel_changes()
        elif button_id == "settings-reset-setting":
            self.action_reset_setting()
        elif button_id == "settings-reset-category":
            self.action_reset_category()
        elif button_id == "settings-reset-all":
            self.action_reset_all()
        elif button_id == "settings-transfer":
            self.action_open_transfer()
        elif button_id == "settings-history":
            self.action_open_history()
        elif button_id == "settings-more":
            panel = self.query_one("#settings-advanced-actions", Horizontal)
            panel.toggle_class("visible")
            event.button.label = "Less" if panel.has_class("visible") else "More"
        elif button_id == "settings-managed":
            self._close_editor()
            if self._selected_setting_id == KEYBOARD_SETTING:
                self.post_message(ShortcutSettingRequested(KEYBOARD_SETTING))
                return
            if self._selected_setting_id == "layout.message_columns":
                column_setting = self._settings_by_id[self._selected_setting_id]
                if not column_setting.editable:
                    return

                def stage_columns(raw: str | None) -> None:
                    if raw is not None:
                        self._begin_operation(
                            "Validating message columns",
                            lambda cancel, progress: self.backend.validate_change(
                                "layout.message_columns", raw, cancel=cancel, progress=progress
                            ),
                            lambda result: self._validation_loaded(
                                result, "layout.message_columns", False
                            ),
                            retry_safe=True,
                        )

                self.app.push_screen(
                    MessageColumnsScreen(
                        self._staged.get(column_setting.setting_id, column_setting.current_value)
                    ),
                    stage_columns,
                )
                return
            if self._selected_setting_id in {"appearance.theme", "appearance.branding"}:
                self.post_message(
                    AppearanceBrowseRequested(
                        "themes" if self._selected_setting_id == "appearance.theme" else "branding"
                    )
                )
                return
            managed = MANAGED_SETTINGS.get(self._selected_setting_id or "")
            if managed is not None:
                self.post_message(SettingsSetupRequested(managed[0]))
        elif button_id == "settings-help":
            setting = self._settings_by_id.get(self._selected_setting_id or "")
            if setting is not None and setting.help_topic:
                self.post_message(SettingsHelpRequested(setting.setting_id, setting.help_topic))
        elif button_id == "settings-browse-themes":
            self.post_message(AppearanceBrowseRequested("themes"))
        elif button_id == "settings-browse-branding":
            self.post_message(AppearanceBrowseRequested("branding"))


__all__ = [
    "AppearanceBrowseRequested",
    "SettingLevel",
    "SettingValidationView",
    "SettingValue",
    "SettingView",
    "SettingsApplyView",
    "SettingsCatalogView",
    "SettingsExportView",
    "SettingsHelpRequested",
    "SettingsHistoryScreen",
    "SettingsImportChangeView",
    "SettingsImportPreviewView",
    "SettingsPreviewView",
    "SettingsRevisionView",
    "SettingsScreen",
    "SettingsScreenBackend",
    "SettingsTransferScreen",
    "display_setting_value",
    "setting_search_score",
]
