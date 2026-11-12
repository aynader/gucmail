"""Responsive Textual mail client over the same gateway used by the CLI."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import math
import os
import platform
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, get_ident
from time import perf_counter_ns
from typing import Any, ClassVar, Literal, TypeVar, cast, overload
from uuid import uuid4
from zoneinfo import ZoneInfo

from rich.console import RenderableType
from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.dom import NoScreen
from textual.message import Message
from textual.screen import ModalScreen, Screen, ScreenResultCallbackType
from textual.timer import Timer
from textual.widget import AwaitMount, Widget
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    Static,
    TextArea,
    Tree,
)
from textual.worker import Worker, get_current_worker

from gucmail.bootstrap import AppContext, build_context
from gucmail.commands import (
    AvailabilityState,
    CommandAvailability,
    CommandConfirmationRequired,
    CommandContext,
    CommandExecutor,
    CommandInvocation,
    CommandPlan,
    CommandSpec,
    CommandUnavailableError,
    Exclusion,
    ParameterSpec,
    ParameterType,
    Ranking,
    ThreadResponseState,
)
from gucmail.config import normalize_theme_identifier
from gucmail.constants import MAX_PAGE_SIZE
from gucmail.dates import parse_datetime_expression
from gucmail.errors import (
    ConfigurationError,
    ConflictError,
    ItemNotFoundError,
    MutationExecutionError,
    SendOutcomeUncertainError,
)
from gucmail.formatting import format_timestamp, human_bytes
from gucmail.help import (
    HelpNavigator,
    HelpPage,
    HelpRepository,
    HelpRuntimeContext,
    HelpStateStore,
    HelpUserState,
    parse_help_link,
)
from gucmail.models import (
    Address,
    AttachmentInfo,
    CalendarResponse,
    DraftData,
    DraftLocation,
    FolderInfo,
    Importance,
    ItemSource,
    MessageDetail,
    MessageSummary,
    NotificationEvent,
    SearchExclusion,
    SearchQuery,
    SearchRanking,
)
from gucmail.nlp import (
    DateRange,
    FolderEntity,
    InterpretationKind,
    NaturalLanguageResult,
    NaturalLanguageSequence,
    PersonEntity,
    SizeConstraint,
    extract_date_range,
    extract_exclusions,
    extract_ranking,
    extract_size_constraint,
    extract_thread_response_state,
)
from gucmail.notification_policies import MonitoringProfile
from gucmail.palette import (
    PaletteEngine,
    PaletteSuggestion,
    SuggestionSource,
    universal_palette_registry,
)
from gucmail.secret_store import SecretStore
from gucmail.security import html_to_safe_text, redact_text, sanitize_terminal
from gucmail.services._base import MutationPlan
from gucmail.services.attachment_preferences import AttachmentPreferences
from gucmail.services.attachments import AttachmentService, CollisionPolicy
from gucmail.services.automatic_replies import AutomaticReplyService
from gucmail.services.backup import BackupService
from gucmail.services.calendar import CalendarService
from gucmail.services.calendar_display import CalendarDisplayPreferences
from gucmail.services.calendar_preferences import (
    CALENDAR_OPERATION_SETTINGS,
    CalendarOperationPreferences,
)
from gucmail.services.calendar_working_time import CalendarWorkingTime
from gucmail.services.compose import (
    ComposeKind,
    ComposeService,
    ComposeValidation,
    ComposeValidationError,
)
from gucmail.services.compose_preferences import ComposerPreferences
from gucmail.services.contact_groups import ContactGroupService, LocalContactGroups
from gucmail.services.contact_preferences import ContactOperationPreferences
from gucmail.services.contacts import ContactsService
from gucmail.services.contacts_display import CONTACT_DISPLAY_SETTINGS, ContactsDisplayPreferences
from gucmail.services.delegates import DelegateService
from gucmail.services.drafts import DraftService, DraftSyncCoordinator
from gucmail.services.editor import ExternalDraftEditor
from gucmail.services.folders import FolderService
from gucmail.services.gateway import DeferredGateway as _DeferredGateway
from gucmail.services.inbox_rules import InboxRuleService
from gucmail.services.index import SearchIndex
from gucmail.services.keyboard_bindings import (
    KEYBOARD_SETTING,
    KEYBOARD_SETTING_IDS,
    validate_binding_configuration,
)
from gucmail.services.keyboard_preferences import KeyboardPreferencesService
from gucmail.services.layout_presets import PRESET_FIELDS, LayoutPresetService
from gucmail.services.local_search import LocalSearchMode, LocalSearchService
from gucmail.services.mail_preferences import (
    MailDeletionPreferences,
    MailResponsePreferences,
    MailSortPreferences,
    conversation_page_positions,
    group_message_page,
)
from gucmail.services.message_columns import (
    COLUMN_LABELS,
    DEFAULT_COLUMNS,
    MessageColumn,
    columns_value,
    fitted_columns,
    parse_columns,
)
from gucmail.services.message_export import MessageExportService
from gucmail.services.messages import MessageService
from gucmail.services.notification_configuration import NotificationConfigurationService
from gucmail.services.notification_simulation import (
    NotificationSample,
    NotificationSimulationService,
)
from gucmail.services.notifications import (
    DeliveryResult,
    NotificationDispatcher,
    notification_event_schema,
)
from gucmail.services.ocr import OcrService
from gucmail.services.pim_preferences import CalendarCreationPreferences, TaskCreationPreferences
from gucmail.services.quota import (
    QuotaService,
    compact_quota_status,
    quota_service_from_settings,
)
from gucmail.services.recurring import RecurringService
from gucmail.services.scheduler import SchedulerService
from gucmail.services.search import evaluate_residual, parse_query
from gucmail.services.semantic import SemanticStore, account_identity
from gucmail.services.semantic_search import SemanticSearchService
from gucmail.services.shell_preferences import (
    HEADER_DEFAULT,
    STATUS_DEFAULT,
    fit_header,
    header_variants,
    parse_shell_fields,
    status_text,
)
from gucmail.services.shortcuts import SHORTCUT_DEFAULTS, normalize_shortcut, validate_shortcuts
from gucmail.services.startup_runtime import StartupService
from gucmail.services.tasks import TaskService
from gucmail.services.tasks_display import TASK_DISPLAY_SETTINGS, TaskDisplayPreferences
from gucmail.services.ui_session import (
    FOCUS_TARGETS,
    MAX_SCROLL,
    SEARCH_SCOPES,
    MailSession,
    MailSessionStore,
)
from gucmail.services.webhooks import (
    WebhookConfig,
    WebhookNotificationProvider,
    WebhookRegistry,
)
from gucmail.settings import DEFAULT_SETTINGS, SettingsContext, SettingsService
from gucmail.tui.activity_screen import ActivityScreen, MessageOperationRequested
from gucmail.tui.appearance_screen import ThemeBrowserBackend, ThemeBrowserScreen
from gucmail.tui.area_backends import (
    ActivityAreaBackend,
    BackupAreaBackend,
    SettingsAreaBackend,
    StorageAreaBackend,
)
from gucmail.tui.area_common import (
    OperationalScreen,
    OperationProgress,
    ProgressReporter,
    ViewState,
    current_row_highlight,
)
from gucmail.tui.automatic_replies_screen import AutomaticRepliesScreen
from gucmail.tui.backup_screen import BackupScreen
from gucmail.tui.branding import (
    GERMAN_BLACK,
    GERMAN_GOLD,
    GERMAN_RED,
    SLIM_TITLE,
    BrandAlignment,
    BrandColorBehavior,
    BrandingPreferences,
    BrandSize,
    RenderedBranding,
    render_branding,
    slim_brand,
)
from gucmail.tui.branding_screen import BrandingBrowserBackend, BrandingBrowserScreen
from gucmail.tui.composer_submission import (
    ComposerSubmissionSession,
    SubmissionOutcome,
    attempt_message,
)
from gucmail.tui.contact_groups_screen import (
    ContactGroupComposeRequested,
    ContactGroupsRequested,
    ContactGroupsScreen,
)
from gucmail.tui.delegates_screen import DelegatesScreen
from gucmail.tui.diagnostics_screen import DiagnosticsScreen, LocalDiagnosticsBackend
from gucmail.tui.drafts_attachments_backend import ServiceDraftAttachmentBackend
from gucmail.tui.drafts_attachments_screen import (
    DraftAttachmentScreen,
    DraftHandoff,
    DraftHandoffKind,
    DraftRecoveryRequested,
    DraftScheduleScreen,
)
from gucmail.tui.inbox_rules_screen import InboxRulesScreen
from gucmail.tui.keyboard_bindings import apply_keyboard_bindings
from gucmail.tui.keyboard_editor import KeyboardEditorScreen
from gucmail.tui.layout_presets_screen import LayoutPresetsScreen
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.local_search_screen import (
    LocalSearchHelpRequested,
    LocalSearchIndexRequested,
    LocalSearchOpenRequested,
    LocalSearchScopeChanged,
    LocalSearchScreen,
    LocalSearchSemanticSetupRequested,
    ServiceLocalSearchBackend,
)
from gucmail.tui.message_columns_preview import MessageColumnsPreviewScreen
from gucmail.tui.notification_settings_screen import (
    AutomaticRepliesRequested,
    InboxRulesRequested,
    NotificationSettingsScreen,
)
from gucmail.tui.ocr_screen import OcrScreen
from gucmail.tui.offline_pim_screen import OfflinePimScreen
from gucmail.tui.palette_routes import (
    CATALOG_ROUTES,
    PaletteRoute,
    RouteKind,
    RouteTarget,
    catalog_handler_ids,
    catalog_route,
)
from gucmail.tui.pim_screens import (
    CalendarScreen,
    CalendarServiceBackend,
    ContactsScreen,
    ContactsServiceBackend,
    CorrespondentsScreen,
    FilePathScreen,
    PimActionRequested,
    TasksScreen,
    TasksServiceBackend,
)
from gucmail.tui.saved_search_screen import (
    SavedSearchHelpRequested,
    SavedSearchRunRequested,
    SavedSearchScreen,
    StateSavedSearchBackend,
)
from gucmail.tui.scheduled_screen import ScheduledScreen, SchedulerServiceBackend
from gucmail.tui.semantic_screen import SemanticScreen
from gucmail.tui.settings_screen import (
    AppearanceBrowseRequested,
    ColumnPreviewRequested,
    SettingsHelpRequested,
    SettingsScreen,
    SettingsSetupRequested,
)
from gucmail.tui.shortcuts_screen import (
    ShortcutSettingRequested,
    ShortcutsRequested,
    ShortcutsScreen,
    runtime_shortcut_records,
)
from gucmail.tui.stable_footer import StableFooter as Footer
from gucmail.tui.startup_screen import StartupScreen
from gucmail.tui.storage_screen import StorageScreen
from gucmail.tui.theme_repository import CustomThemeRepository
from gucmail.tui.themes import (
    ColorCapability,
    ThemeDefinition,
    ThemePolicy,
    ThemePreferences,
    ThemeRegistry,
    ThemeSession,
    ThemeValidationError,
    adapt_theme,
    adapt_theme_high_contrast,
    to_textual_theme,
)
from gucmail.tui.tutorial_screen import TutorialHelpRequested
from gucmail.tui.visual_language import FRONTEND_CSS
from gucmail.tui.write_coordinator import (
    CHANGE_IN_PROGRESS_MESSAGE,
    PENDING_CHANGE_MESSAGE,
    WriteBlocked,
    WriteStateChanged,
    app_write_coordinator,
    coordinated_write,
)

TextualBorderStyle = Literal["none", "ascii", "solid", "round", "double", "heavy", "dashed"]


def _safe_failure(exc: BaseException, *, action: str = "Operation unavailable") -> str:
    """Return a useful failure label without echoing arbitrary exception text."""

    if isinstance(exc, MutationExecutionError):
        return exc.message + " " + (exc.context.suggestion or "")
    error_type = sanitize_terminal(type(exc).__name__, max_length=100) or "Error"
    return f"{action} ({error_type}). Details omitted; use gucmail doctor or redacted logs."


def _open_file_with_registered_handler(path: Path) -> None:
    """Open one validated file through the OS registry without a command shell."""

    resolved = path.resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise OSError("The exported message is not a regular file.")
    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if not callable(startfile):
            raise OSError("The Windows registered-file launcher is unavailable.")
        startfile(str(resolved))
        return
    command = (
        ["open", "--", str(resolved)] if sys.platform == "darwin" else ["xdg-open", str(resolved)]
    )
    subprocess.Popen(  # noqa: S603 - fixed executable plus one validated absolute path
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


_ShortcutScreenResult = TypeVar("_ShortcutScreenResult")


@dataclass(slots=True)
class UIState:
    folder: str = "inbox"
    selected_reference: str | None = None
    search: str = ""
    online: bool = False
    busy: str | None = None


@dataclass(frozen=True, slots=True)
class _AutomaticReadRequest:
    generation: int
    reference: str
    view: Widget
    screen_updates: int
    cancel: Event


class AccountStatus(Static):
    """Keep connection and qualified storage facts readable as the header shrinks."""

    _variants: tuple[str, ...] = ()

    def set_mailbox_status(
        self,
        *,
        quota: str,
        watcher: str,
        folder: str,
        detail: str,
        connection: str = "Connected",
        fields: tuple[str, ...] = HEADER_DEFAULT,
        compactness: str = "adaptive",
    ) -> None:
        self.tooltip = Text(sanitize_terminal(detail, max_length=1000))
        self._variants = header_variants(
            {
                "account": connection,
                "storage": quota,
                "monitoring": watcher,
                "folder": folder,
            },
            fields,
            compactness=compactness,
        )
        self._fit_status()

    def on_resize(self, event: events.Resize) -> None:
        self._fit_status()

    def _fit_status(self) -> None:
        if self._variants:
            self.update(fit_header(self._variants, self.content_size.width))


class CompactHeader(Widget):
    """A permanently one-row header; unlike Textual's stock Header it never expands."""

    DEFAULT_CSS = (
        """
    CompactHeader {
        height: 1;
        width: 100%;
        layout: horizontal;
        background: $gucmail-header;
        color: $foreground;
    }
    CompactHeader #compact-title {
        width: auto;
        min-width: 14;
        padding: 0 0 0 1;
        text-wrap: nowrap;
    }
    CompactHeader .brand-letter {
        width: 1;
        height: 1;
        text-style: bold;
        text-align: center;
    }
    CompactHeader #brand-g { color: $foreground; background: transparent; }
    CompactHeader #brand-u { color: __GERMAN_RED__; }
    CompactHeader #brand-c { color: __GERMAN_GOLD__; }
    CompactHeader.monochrome-brand #brand-g,
    CompactHeader.monochrome-brand #brand-u,
    CompactHeader.monochrome-brand #brand-c {
        color: $foreground;
        background: transparent;
    }
    CompactHeader #brand-name {
        width: 11;
        height: 1;
        text-style: bold;
        padding: 0 1 0 1;
    }
    CompactHeader .compact-header-status {
        width: 1fr;
        padding: 0 1;
        text-align: right;
        text-overflow: ellipsis;
        text-wrap: nowrap;
        color: $text-muted;
    }
    CompactHeader #palette-launcher {
        width: auto;
        min-width: 3;
        height: 1;
        padding: 0 1;
        text-style: bold;
        color: $accent;
    }
    """.replace("__GERMAN_BLACK__", GERMAN_BLACK)
        .replace("__GERMAN_RED__", GERMAN_RED)
        .replace("__GERMAN_GOLD__", GERMAN_GOLD)
    )

    def __init__(
        self,
        status: str = "",
        *,
        status_id: str | None = None,
        branding: RenderedBranding | None = None,
        launcher_label: str | None = None,
        launcher_first: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._initial_status = sanitize_terminal(status, max_length=500)
        self._status_id = status_id
        self._branding = branding
        self._launcher_label = launcher_label
        self._launcher_first = launcher_first

    def compose(self) -> ComposeResult:
        if self._launcher_label and self._launcher_first:
            yield Static(self._launcher_label, id="palette-launcher", markup=False)
        with Horizontal(id="compact-title"):
            yield Static(SLIM_TITLE[0], id="brand-g", classes="brand-letter", markup=False)
            yield Static(SLIM_TITLE[1], id="brand-u", classes="brand-letter", markup=False)
            yield Static(SLIM_TITLE[2], id="brand-c", classes="brand-letter", markup=False)
            yield Static(SLIM_TITLE[3:], id="brand-name", markup=False)
        status_widget = AccountStatus if self._status_id == "account-bar" else Static
        yield status_widget(
            self._initial_status,
            id=self._status_id,
            classes="compact-header-status",
            markup=False,
        )
        if self._launcher_label and not self._launcher_first:
            yield Static(self._launcher_label, id="palette-launcher", markup=False)

    def on_mount(self) -> None:
        self._apply_branding()

    def set_branding(self, branding: RenderedBranding) -> None:
        """Update exact semantic G/U/C colors and any accessible swatches."""

        self._branding = branding
        if self.is_mounted:
            self._apply_branding()

    def _apply_branding(self) -> None:
        if self._branding is None or not self._branding.lines:
            return
        spans = self._branding.lines[0].spans
        if len(spans) < 3:
            return
        # Colourless spans mean monochrome output: fall back to the stylesheet's
        # foreground rule instead of keeping the fixed German-flag CSS colours.
        self.set_class(all(span.foreground is None for span in spans[:3]), "monochrome-brand")
        for selector, span in zip(
            ("#brand-g", "#brand-u", "#brand-c"),
            spans[:3],
            strict=True,
        ):
            widget = self.query_one(selector, Static)
            if span.foreground is not None:
                widget.styles.color = span.foreground
            widget.styles.background = span.background or "transparent"


class PaneDivider(Static):
    """Mouse-draggable divider that reports cell deltas to the application."""

    class Moved(Message):
        def __init__(self, delta: int, *, final: bool = False) -> None:
            super().__init__()
            self.delta = delta
            self.final = final

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._dragging = False
        self._last_coordinate = 0

    def _coordinate(self, event: events.MouseEvent) -> int:
        app = self.app
        orientation = getattr(app, "_split_orientation", "vertical")
        return event.screen_y if orientation == "horizontal" else event.screen_x

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragging = True
        self._last_coordinate = self._coordinate(event)
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        coordinate = self._coordinate(event)
        delta = coordinate - self._last_coordinate
        self._last_coordinate = coordinate
        if delta:
            self.post_message(self.Moved(delta))
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            self.post_message(self.Moved(0, final=True))
            event.stop()


class MessagePreviewScroll(VerticalScroll):
    """Reader scroll surface with deterministic Home/End behavior after content swaps."""

    CONTEXT_BINDINGS: ClassVar = (
        Binding("end", "scroll_end", "End of message preview"),
        Binding("home", "scroll_home", "Start of message preview"),
    )

    def on_key(self, event: events.Key) -> None:
        if event.key == self.CONTEXT_BINDINGS[0].key:
            self.call_after_refresh(self.scroll_end, animate=False)
            event.prevent_default()
            event.stop()
        elif event.key == self.CONTEXT_BINDINGS[1].key:
            self.call_after_refresh(self.scroll_home, animate=False)
            event.prevent_default()
            event.stop()


class FloatingReader(Vertical):
    """A movable, modeless reader that leaves the message list usable."""

    DEFAULT_CSS = """
    FloatingReader {
        overlay: screen;
        layer: overlay;
        width: 62%;
        max-width: 110;
        min-width: 36;
        height: 78%;
        min-height: 12;
        offset: 30 2;
        border: thick $accent;
        background: $gucmail-elevated-surface;
    }
    FloatingReader #floating-reader-titlebar {
        height: 3;
        width: 100%;
        layout: horizontal;
        background: $panel;
    }
    FloatingReader #floating-reader-title {
        width: 1fr;
        padding: 1;
        text-style: bold;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }
    FloatingReader #floating-reader-close {
        width: 7;
        min-width: 7;
    }
    FloatingReader #floating-reader-scroll {
        width: 100%;
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, title: str, content: Any) -> None:
        super().__init__(id="floating-reader")
        self.reader_title = sanitize_terminal(title or "(no subject)", max_length=300)
        self.reader_content = content
        self._dragging = False
        self._grab = (0, 0)

    def compose(self) -> ComposeResult:
        with Horizontal(id="floating-reader-titlebar"):
            yield Static(self.reader_title, id="floating-reader-title", markup=False)
            yield Button("Close", id="floating-reader-close")
        with VerticalScroll(id="floating-reader-scroll"):
            yield Static(self.reader_content, id="floating-reader-content", markup=False)

    def set_message(self, title: str, content: Any) -> None:
        self.reader_title = sanitize_terminal(title or "(no subject)", max_length=300)
        self.reader_content = content
        if self.is_mounted:
            self.query_one("#floating-reader-title", Static).update(self.reader_title)
            self.query_one("#floating-reader-content", Static).update(content)

    @on(Button.Pressed, "#floating-reader-close")
    def close_reader(self, _event: Button.Pressed) -> None:
        self.remove()

    @on(events.MouseDown, "#floating-reader-titlebar")
    def start_drag(self, event: events.MouseDown) -> None:
        self._dragging = True
        self._grab = (event.screen_x - self.region.x, event.screen_y - self.region.y)
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        maximum_x = max(0, self.app.size.width - self.region.width)
        maximum_y = max(0, self.app.size.height - self.region.height)
        new_x = min(max(0, event.screen_x - self._grab[0]), maximum_x)
        new_y = min(max(0, event.screen_y - self._grab[1]), maximum_y)
        self.styles.offset = (new_x, new_y)
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            event.stop()


class ReaderScreen(Screen[None]):
    """Full-screen reader fallback for terminals where the preview pane is hidden."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("backspace", "dismiss", "Close", show=False),
    ]

    def __init__(self, title: str, content: Any) -> None:
        super().__init__()
        self.reader_title = sanitize_terminal(title or "(no subject)", max_length=300)
        self.reader_content = content

    def compose(self) -> ComposeResult:
        yield CompactHeader(f"Reading · {self.reader_title}")
        with VerticalScroll(id="reader-screen-scroll"):
            yield Static(self.reader_content, id="reader-screen-content", markup=False)
        yield Footer()


class ReaderModeScreen(ModalScreen[str | None]):
    """Explicit per-message reader choice used by the persisted ``ask`` mode."""

    DEFAULT_CSS = """
    ReaderModeScreen { align: center middle; background: $background 70%; }
    #reader-mode-box {
        width: 92%; max-width: 72; height: auto; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #reader-mode-title { height: 2; text-style: bold; }
    #reader-mode-detail { height: auto; min-height: 2; color: $text-muted; }
    #reader-mode-actions { height: auto; min-height: 3; align-horizontal: center; }
    #reader-mode-actions Button { margin: 0 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="reader-mode-box"):
            yield Label("Open selected message", id="reader-mode-title")
            yield Static(
                "Choose where to read this message. External open exports one private "
                "temporary EML and still asks for confirmation.",
                id="reader-mode-detail",
                markup=False,
            )
            with Horizontal(id="reader-mode-actions"):
                yield Button("Preview", id="reader-mode-preview")
                yield Button("Full screen", id="reader-mode-full", variant="primary")
                yield Button("Floating", id="reader-mode-floating")
                yield Button("External", id="reader-mode-external")

    @on(Button.Pressed, "#reader-mode-actions Button")
    def _choice(self, event: Button.Pressed) -> None:
        choices = {
            "reader-mode-preview": "pane",
            "reader-mode-full": "full",
            "reader-mode-floating": "floating",
            "reader-mode-external": "external",
        }
        choice = choices.get(event.button.id or "")
        if choice is not None:
            self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsHelpStateStore:
    """Mirror real Help state into editable Settings without creating two truths."""

    def __init__(self, state: HelpStateStore, settings: SettingsService) -> None:
        self.state = state
        self.settings = settings

    def load_help_state(self) -> HelpUserState:
        stored = self.state.load_help_state()
        try:
            favorites = self.settings.value("help.favorites")
            recent = self.settings.value("help.recent_pages")
        except ConfigurationError:
            return stored
        favorite_ids = (
            frozenset(str(value) for value in favorites.effective_value)
            if favorites.source.value != "default"
            and isinstance(favorites.effective_value, (list, tuple))
            else stored.favorite_page_ids
        )
        recent_ids = (
            tuple(str(value) for value in recent.effective_value)
            if recent.source.value != "default"
            and isinstance(recent.effective_value, (list, tuple))
            else stored.recent_page_ids
        )
        mirrored = HelpUserState(
            recent_page_ids=recent_ids,
            favorite_page_ids=favorite_ids,
            recent_queries=stored.recent_queries,
        )
        if mirrored != stored:
            self.state.save_help_state(mirrored)
        return mirrored

    def save_help_state(self, state: HelpUserState) -> None:
        self.state.save_help_state(state)
        try:
            session = self.settings.begin()
            session.set("help.favorites", sorted(state.favorite_page_ids))
            session.set("help.recent_pages", list(state.recent_page_ids))
            session.apply(reason="Help favorites and history updated")
        except (ConfigurationError, OSError, ValueError):
            return


class HelpCommandRequested(Message):
    """Request a safe palette preview for a command link from Help."""

    def __init__(self, command_id: str) -> None:
        super().__init__()
        self.command_id = command_id


class HelpSettingRequested(Message):
    """Request the exact Settings row linked from Help."""

    def __init__(self, setting_id: str) -> None:
        super().__init__()
        self.setting_id = setting_id


class HelpTutorialsRequested(Message):
    def __init__(self, page_id: str) -> None:
        super().__init__()
        self.page_id = page_id


class HelpScreen(Screen[None]):
    """Searchable, offline documentation center backed by packaged Help pages."""

    DEFAULT_CSS = """
    HelpScreen #help-toolbar {
        height: 3;
        padding: 0 1;
        background: $panel;
    }
    HelpScreen #help-toolbar Button { min-width: 5; }
    HelpScreen #help-search { width: 1fr; }
    HelpScreen #help-category { width: 28; }
    HelpScreen #help-body { height: 1fr; }
    HelpScreen #help-index {
        width: 36%;
        min-width: 28;
        max-width: 54;
        border-right: solid $primary-background;
    }
    HelpScreen #help-results { height: 1fr; }
    HelpScreen .help-result { height: auto; min-height: 4; padding: 0 1; }
    HelpScreen .help-result Label { width: 1fr; height: auto; text-wrap: wrap; }
    HelpScreen #help-article-pane { width: 1fr; height: 1fr; }
    HelpScreen #help-breadcrumbs {
        height: 2;
        padding: 0 1;
        color: $text-muted;
        background: $boost;
    }
    HelpScreen #help-article-scroll { height: 1fr; padding: 0 2; }
    HelpScreen #help-article { width: 100%; }
    HelpScreen #help-notice {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $panel;
    }
    HelpScreen.narrow-help #help-body { layout: vertical; }
    HelpScreen.narrow-help #help-index {
        width: 100%; min-width: 1; max-width: 100%; height: 36%; min-height: 6;
        border-right: none; border-bottom: solid $primary-background;
    }
    HelpScreen.narrow-help #help-article-pane { width: 100%; height: 1fr; }
    HelpScreen.narrow-help #help-category { display: none; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("?", "dismiss", "Close"),
        Binding("ctrl+f", "focus_search", "Search"),
        Binding("alt+left", "back", "Back", show=False),
        Binding("alt+right", "forward", "Forward", show=False),
        Binding("ctrl+home", "home", "Help home", show=False),
        Binding("ctrl+d", "favorite", "Favorite", show=False),
        Binding("alt+t", "tutorials", "Tutorials", show=False),
    ]

    def __init__(
        self,
        *,
        context: HelpRuntimeContext | None = None,
        initial_page_id: str | None = None,
        initial_query: str = "",
        repository: HelpRepository | None = None,
        store: HelpStateStore | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository or HelpRepository.load_default()
        self.navigator = HelpNavigator(self.repository, store=store)
        self.help_context = context or HelpRuntimeContext(active_view="home")
        self.initial_page_id = initial_page_id
        self.initial_query = " ".join(initial_query.split())
        self._result_page_ids: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        yield CompactHeader("Help Center · offline documentation")
        categories = (
            ("All topics", ""),
            *((category, category) for category in self.repository.categories),
        )
        with Horizontal(id="help-toolbar"):
            yield Button("←", id="help-back", tooltip="Back (Alt+Left)")
            yield Button("→", id="help-forward", tooltip="Forward (Alt+Right)")
            yield Button("Home", id="help-home")
            yield Button("Tutorials", id="help-tutorials", tooltip="Guided practice (Alt+T)")
            yield Button("Keys", id="help-shortcuts", tooltip="Browse current keyboard bindings")
            yield Input(
                value=self.initial_query,
                placeholder="Search Help, errors, commands, settings, or ask a question…",
                id="help-search",
            )
            yield Select(categories, value="", allow_blank=False, id="help-category")
            yield Button("☆", id="help-favorite", tooltip="Favorite this article (Ctrl+D)")
        with Horizontal(id="help-body"):
            with Vertical(id="help-index"):
                yield ListView(id="help-results")
            with Vertical(id="help-article-pane"):
                yield Static("Help", id="help-breadcrumbs")
                with VerticalScroll(id="help-article-scroll"):
                    yield Markdown("Loading Help…", id="help-article", open_links=False)
        yield Static(
            "Type to search · Enter opens the best result · internal links stay in Help",
            id="help-notice",
        )
        yield Footer()

    async def on_mount(self) -> None:
        page_id = self.initial_page_id or self.repository.home_page_id
        try:
            page = self.navigator.open(page_id)
        except KeyError:
            page = self.navigator.home()
        await self._render_page(page.page_id, add_to_history=False)
        await self._refresh_results(self.initial_query)
        self.query_one("#help-search", Input).focus()
        self.set_class(self.size.width < 90, "narrow-help")

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 90, "narrow-help")

    async def on_screen_resume(self) -> None:
        if not self.is_mounted or self.navigator.current is None:
            return
        refresh_context = getattr(self.app, "_help_runtime_context", None)
        if callable(refresh_context):
            self.help_context = refresh_context(self.help_context.active_view or "help")
            await self._render_page(self.navigator.current.page_id, add_to_history=False)

    def _default_pages(self) -> tuple[HelpPage, ...]:
        """Return deduplicated context, recent, favorite, and start-here pages."""

        ordered: list[HelpPage] = []
        active_view = self.help_context.active_view
        if active_view:
            ordered.extend(
                self.repository.pages_for_context(
                    active_view,
                    platform=self.help_context.platform,
                )
            )
        for page_id in self.navigator.state.recent_page_ids:
            try:
                ordered.append(self.repository.get(page_id))
            except KeyError:
                continue
        ordered.extend(self.navigator.favorite_pages())
        ordered.extend(self.repository.pages_in_category("Start Here"))
        ordered.append(self.repository.get(self.repository.home_page_id))
        seen: set[str] = set()
        result: list[HelpPage] = []
        for page in ordered:
            if page.page_id in seen:
                continue
            seen.add(page.page_id)
            result.append(page)
        return tuple(result[:20])

    async def _refresh_results(self, query: str, *, record_query: bool = False) -> None:
        cleaned = " ".join(query.split())
        if cleaned:
            matches = (
                self.navigator.search(cleaned, limit=30, context=self.help_context)
                if record_query
                else self.repository.search(cleaned, limit=30, context=self.help_context)
            )
            pages = tuple(match.page for match in matches)
        else:
            pages = self._default_pages()
        self._result_page_ids = tuple(page.page_id for page in pages)
        recent = set(self.navigator.state.recent_page_ids)
        favorites = self.navigator.state.favorite_page_ids
        items: list[ListItem] = []
        for page in pages:
            markers = ("★" if page.page_id in favorites else "") + (
                "↶" if page.page_id in recent else ""
            )
            title = f"{markers + ' ' if markers else ''}{page.category} · {page.title}"
            items.append(
                ListItem(
                    Label(f"{title}\n{page.summary}", markup=False),
                    classes="help-result",
                )
            )
        if not items:
            items.append(
                ListItem(
                    Label(
                        "No Help topic matched\n"
                        "Try fewer words, an error message, or a feature name.",
                        markup=False,
                    ),
                    classes="help-result",
                    disabled=True,
                )
            )
        results = self.query_one("#help-results", ListView)
        await results.clear()
        await results.extend(items)
        results.index = 0 if self._result_page_ids else None

    async def _render_page(self, page_id: str, *, add_to_history: bool = True) -> None:
        page = self.navigator.open(page_id) if add_to_history else self.repository.get(page_id)
        rendered = self.repository.render(page.page_id, context=self.help_context)
        breadcrumbs = "  >  ".join(self.navigator.breadcrumbs())
        self.query_one("#help-breadcrumbs", Static).update(breadcrumbs)
        await self.query_one("#help-article", Markdown).update(rendered.markdown)
        favorite = page.page_id in self.navigator.state.favorite_page_ids
        self.query_one("#help-favorite", Button).label = "★" if favorite else "☆"
        self.query_one("#help-back", Button).disabled = not self.navigator.can_go_back
        self.query_one("#help-forward", Button).disabled = not self.navigator.can_go_forward
        self.query_one("#help-notice", Static).update(
            f"{page.category} · {page.skill_level.value.title()} · {rendered.availability_detail}"
        )

    @on(Input.Changed, "#help-search")
    async def search_changed(self, event: Input.Changed) -> None:
        await self._refresh_results(event.value)

    @on(Input.Submitted, "#help-search")
    async def search_submitted(self, event: Input.Submitted) -> None:
        await self._refresh_results(event.value, record_query=True)
        if self._result_page_ids:
            await self._render_page(self._result_page_ids[0])

    @on(Select.Changed, "#help-category")
    async def category_changed(self, event: Select.Changed) -> None:
        category = event.value if isinstance(event.value, str) else ""
        if not category:
            await self._refresh_results(self.query_one("#help-search", Input).value)
            return
        pages = self.repository.pages_in_category(category)
        self._result_page_ids = tuple(page.page_id for page in pages)
        items = [
            ListItem(
                Label(f"{page.title}\n{page.summary}", markup=False),
                classes="help-result",
            )
            for page in pages
        ]
        results = self.query_one("#help-results", ListView)
        await results.clear()
        await results.extend(items)
        results.index = 0 if items else None

    @on(ListView.Selected, "#help-results")
    async def result_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._result_page_ids):
            await self._render_page(self._result_page_ids[index])

    @on(Markdown.LinkClicked, "#help-article")
    async def help_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        try:
            link = parse_help_link(event.href)
        except ValueError:
            self.query_one("#help-notice", Static).update(
                "That Help link is invalid and was blocked."
            )
            return
        if link.scheme == "help":
            await self._render_page(link.identifier)
        elif link.scheme == "anchor":
            self.query_one("#help-article", Markdown).goto_anchor(link.identifier)
        elif link.scheme == "https":
            self.query_one("#help-notice", Static).update(
                "External link not opened automatically. Copy it from the article if needed."
            )
        elif link.scheme == "command":
            self.post_message(HelpCommandRequested(link.identifier))
            self.query_one("#help-notice", Static).update(
                f"Opening a safe palette preview for command:{link.identifier}."
            )
        elif link.scheme == "setting":
            self.post_message(HelpSettingRequested(link.identifier))
            self.query_one("#help-notice", Static).update(f"Opening setting:{link.identifier}.")
        else:
            self.query_one("#help-notice", Static).update(
                f"Use the command palette for {link.scheme}:{link.identifier}."
            )

    @on(Button.Pressed, "#help-back")
    async def back_pressed(self) -> None:
        await self.action_back()

    @on(Button.Pressed, "#help-forward")
    async def forward_pressed(self) -> None:
        await self.action_forward()

    @on(Button.Pressed, "#help-home")
    async def home_pressed(self) -> None:
        await self.action_home()

    @on(Button.Pressed, "#help-tutorials")
    def action_tutorials(self) -> None:
        page = self.navigator.current
        self.post_message(HelpTutorialsRequested(page.page_id if page else "start.interface"))

    @on(Button.Pressed, "#help-shortcuts")
    def shortcuts_pressed(self) -> None:
        self.post_message(ShortcutsRequested())

    @on(Button.Pressed, "#help-favorite")
    async def favorite_pressed(self) -> None:
        await self.action_favorite()

    def action_focus_search(self) -> None:
        self.query_one("#help-search", Input).focus()

    async def action_back(self) -> None:
        page = self.navigator.back()
        if page is not None:
            await self._render_page(page.page_id, add_to_history=False)

    async def action_forward(self) -> None:
        page = self.navigator.forward()
        if page is not None:
            await self._render_page(page.page_id, add_to_history=False)

    async def action_home(self) -> None:
        page = self.navigator.home()
        await self._render_page(page.page_id, add_to_history=False)

    async def action_favorite(self) -> None:
        page = self.navigator.current
        if page is None:
            return
        enabled = self.navigator.toggle_favorite(page.page_id)
        self.query_one("#help-favorite", Button).label = "★" if enabled else "☆"
        await self._refresh_results(self.query_one("#help-search", Input).value)


class ConfirmationScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmationScreen { align: center middle; }
    #confirm-box {
        width: 76; max-width: 94%; height: 80%; max-height: 26; min-height: 10;
        padding: 1 2;
        border: thick $warning; background: $gucmail-modal-background;
    }
    #confirm-detail { height: 1fr; min-height: 1; }
    #confirm-detail Static { height: auto; }
    #confirm-buttons { height: 3; min-height: 3; margin-top: 1; align-horizontal: right; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("y", "confirm", "Confirm", show=False),
        Binding("n,escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, title: str, detail: str, *, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self._dialog_title = title
        self.detail = detail
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._dialog_title, classes="dialog-title", markup=False)
            with VerticalScroll(id="confirm-detail"):
                yield Static(self.detail, markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, variant="warning", id="confirm")

    @on(Button.Pressed)
    def on_button(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class FolderMoveScreen(ModalScreen[str | None]):
    """Collect one destination selector before an exact move preview is shown."""

    DEFAULT_CSS = """
    FolderMoveScreen { align: center middle; }
    #folder-move-box {
        width: 72; max-width: 94%; height: auto; max-height: 95%; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #folder-move-source { height: auto; margin-bottom: 1; color: $text-muted; }
    #folder-move-actions { height: auto; align-horizontal: right; margin-top: 1; }
    #folder-move-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, source_path: str, *, initial_destination: str = "") -> None:
        super().__init__()
        self.source_path = sanitize_terminal(source_path, max_length=500)
        self.initial_destination = initial_destination

    def compose(self) -> ComposeResult:
        with Vertical(id="folder-move-box"):
            yield Label("Move folder", classes="dialog-title")
            yield Static(
                f"Source: {self.source_path}\n"
                "Choose the destination parent by path or id:<reference>.",
                id="folder-move-source",
                markup=False,
            )
            yield Input(
                value=self.initial_destination,
                placeholder="Destination folder path or id:<reference>",
                id="folder-move-destination",
            )
            with Horizontal(id="folder-move-actions"):
                yield Button("Cancel", id="folder-move-cancel")
                yield Button("Preview move", id="folder-move-submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#folder-move-destination", Input).focus()

    def _submit(self) -> None:
        destination = self.query_one("#folder-move-destination", Input).value.strip()
        if destination:
            self.dismiss(destination)

    @on(Input.Submitted, "#folder-move-destination")
    def destination_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "folder-move-submit":
            self._submit()
        elif event.button.id == "folder-move-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MessageExportPathScreen(ModalScreen[str | None]):
    """Collect an explicit directory before previewing a bounded mail export."""

    DEFAULT_CSS = """
    MessageExportPathScreen { align: center middle; background: $background 75%; }
    #message-export-box {
        width: 94%; max-width: 88; height: auto; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #message-export-detail { height: auto; margin-bottom: 1; color: $text-muted; }
    #message-export-actions { height: auto; align-horizontal: right; margin-top: 1; }
    #message-export-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, default_destination: str, *, selection_count: int) -> None:
        super().__init__()
        self.default_destination = default_destination
        self.selection_count = selection_count

    def compose(self) -> ComposeResult:
        with Vertical(id="message-export-box"):
            yield Label("Export selected messages", classes="dialog-title")
            yield Static(
                f"Choose a directory for exactly {self.selection_count} selected message(s). "
                "Files are exported as raw .eml and existing files are never overwritten.",
                id="message-export-detail",
                markup=False,
            )
            yield Input(value=self.default_destination, id="message-export-destination")
            with Horizontal(id="message-export-actions"):
                yield Button("Cancel", id="message-export-cancel")
                yield Button("Preview export", id="message-export-submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#message-export-destination", Input).focus()

    def _submit(self) -> None:
        destination = self.query_one("#message-export-destination", Input).value.strip()
        if destination:
            self.dismiss(destination)

    @on(Input.Submitted, "#message-export-destination")
    def destination_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "message-export-submit":
            self._submit()
        elif event.button.id == "message-export-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class FolderNameScreen(ModalScreen[str | None]):
    """Collect one folder display name before the service builds a mutation plan."""

    DEFAULT_CSS = """
    FolderNameScreen { align: center middle; }
    #folder-name-box {
        width: 72; max-width: 94%; height: auto; max-height: 95%; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #folder-name-detail { height: auto; margin-bottom: 1; color: $text-muted; }
    #folder-name-actions { height: auto; align-horizontal: right; margin-top: 1; }
    #folder-name-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        title: str,
        detail: str,
        *,
        initial_name: str = "",
        submit_label: str = "Preview",
        placeholder: str = "Folder display name",
    ) -> None:
        super().__init__()
        self.dialog_title = sanitize_terminal(title, max_length=120)
        self.detail = sanitize_terminal(detail, max_length=1_000)
        self.initial_name = initial_name
        self.submit_label = submit_label
        self.placeholder = sanitize_terminal(placeholder, max_length=120)

    def compose(self) -> ComposeResult:
        with Vertical(id="folder-name-box"):
            yield Label(self.dialog_title, classes="dialog-title", markup=False)
            yield Static(self.detail, id="folder-name-detail", markup=False)
            yield Input(
                value=self.initial_name,
                placeholder=self.placeholder,
                id="folder-name-input",
            )
            with Horizontal(id="folder-name-actions"):
                yield Button("Cancel", id="folder-name-cancel")
                yield Button(self.submit_label, id="folder-name-submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#folder-name-input", Input).focus()

    def _submit(self) -> None:
        value = self.query_one("#folder-name-input", Input).value.strip()
        if value:
            self.dismiss(value)

    @on(Input.Submitted, "#folder-name-input")
    def name_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "folder-name-submit":
            self._submit()
        elif event.button.id == "folder-name-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MailQueryScreen(FolderNameScreen):
    """Collect a literal mail query in a compact keyboard-accessible dialog."""

    DEFAULT_CSS = """
    MailQueryScreen #folder-name-box { max-width: 94%; max-height: 95%; }
    """


class FolderDetailsScreen(ModalScreen[None]):
    """Show sanitized folder identity, properties, and server-reported counts."""

    DEFAULT_CSS = """
    FolderDetailsScreen { align: center middle; }
    #folder-details-box {
        width: 76; max-width: 95%; height: 27; max-height: 90%; padding: 1 2;
        border: thick $accent; background: $gucmail-modal-background;
    }
    #folder-details-scroll { height: 1fr; min-height: 1; }
    #folder-details-content { height: auto; }
    #folder-details-actions { height: auto; align-horizontal: right; margin-top: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, folder: FolderInfo) -> None:
        super().__init__()
        self.folder = folder

    @staticmethod
    def _value(value: object | None) -> str:
        return sanitize_terminal("Unavailable" if value is None else str(value), max_length=2_000)

    def compose(self) -> ComposeResult:
        folder = self.folder
        read_count = (
            max(folder.total_count - folder.unread_count, 0)
            if folder.total_count is not None and folder.unread_count is not None
            else None
        )
        details = "\n".join(
            (
                f"Name: {self._value(folder.name)}",
                f"Path: {self._value(folder.path)}",
                f"Parent reference: {self._value(folder.parent_reference)}",
                f"Folder class: {self._value(folder.folder_class)}",
                f"Distinguished role: {self._value(folder.distinguished_role)}",
                f"Total items: {self._value(folder.total_count)}",
                f"Unread items: {self._value(folder.unread_count)}",
                f"Read items: {self._value(read_count)}",
                f"Child folders: {self._value(folder.child_count)}",
                f"Local reference: {self._value(folder.reference)}",
                f"EWS ID: {self._value(folder.ews_id)}",
                f"Change key: {self._value(folder.change_key)}",
                "\nUse Show Folder Storage for bounded size analysis across folders.",
            )
        )
        with Vertical(id="folder-details-box"):
            yield Label("Folder properties", classes="dialog-title")
            with VerticalScroll(id="folder-details-scroll"):
                yield Static(details, id="folder-details-content", markup=False)
            with Horizontal(id="folder-details-actions"):
                yield Button("Close", id="folder-details-close", variant="primary")

    @on(Button.Pressed, "#folder-details-close")
    def close_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True, slots=True)
class PaletteSubmission:
    """Typed palette result returned without converting it to CLI arguments."""

    invocation: CommandInvocation
    interpretation: NaturalLanguageResult
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class PaletteSequenceSubmission:
    """A parser-allowlisted sequence that still requires a result preview."""

    sequence: NaturalLanguageSequence


@dataclass(frozen=True, slots=True)
class PaletteSequenceItem:
    """One bounded message or attachment retained for an exact result handoff."""

    kind: str
    reference: str
    owner_reference: str
    title: str
    size: int | None
    attachment: AttachmentInfo | None = None


@dataclass(frozen=True, slots=True)
class PaletteSequencePreview:
    """Read-only evidence collected before a chained follow-up can be confirmed."""

    sequence: NaturalLanguageSequence
    items: tuple[PaletteSequenceItem, ...]
    search_summary: str
    truncated: bool = False
    blocked_reason: str | None = None

    @property
    def target_command_id(self) -> str:
        invocation = self.sequence.steps[-1].invocation
        return invocation.command_id if invocation is not None else ""

    @property
    def known_bytes(self) -> int:
        return sum(item.size or 0 for item in self.items)

    @property
    def unknown_size_count(self) -> int:
        return sum(item.size is None for item in self.items)


@dataclass(frozen=True, slots=True)
class PaletteSequenceDecision:
    """User-reviewed continuation of a bounded sequence preview."""

    preview: PaletteSequencePreview
    destination: Path | None = None


class PaletteSequencePreviewScreen(ModalScreen[PaletteSequenceDecision | None]):
    """Show real result counts and editable destinations before any follow-up."""

    MAX_BULK_ATTACHMENT_BYTES = 500 * 1024 * 1024
    DEFAULT_CSS = """
    PaletteSequencePreviewScreen { align: center middle; }
    #sequence-box {
        width: 92%; max-width: 108; height: 86%; min-height: 16;
        border: thick $accent; background: $gucmail-modal-background; padding: 1 2;
    }
    #sequence-summary, #sequence-safety { height: auto; margin-bottom: 1; }
    #sequence-content { height: 1fr; }
    #sequence-results { height: 8; min-height: 5; }
    #sequence-destination { height: 3; margin-top: 1; }
    #sequence-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #sequence-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, preview: PaletteSequencePreview, *, destination: Path | None) -> None:
        super().__init__()
        self.preview = preview
        self.destination = destination

    def compose(self) -> ComposeResult:
        target = self.preview.target_command_id
        known = human_bytes(self.preview.known_bytes)
        unknown = self.preview.unknown_size_count
        qualifier = "at least " if self.preview.truncated else ""
        size_text = f"{known} known" + (f" + {unknown} unknown-size" if unknown else "")
        with Vertical(id="sequence-box"):
            yield Label("Review command sequence", classes="dialog-title")
            with VerticalScroll(id="sequence-content"):
                yield Static(
                    f"1. Search (read-only)\n2. {self._target_label(target)}\n\n"
                    f"Matched: {qualifier}{len(self.preview.items)} bounded item(s)\n"
                    f"Estimated size: {size_text}\n{self.preview.search_summary}",
                    id="sequence-summary",
                    markup=False,
                )
                yield DataTable(id="sequence-results", cursor_type="row", zebra_stripes=True)
                if self._needs_destination(target):
                    yield Input(
                        value=str(self.destination or ""),
                        placeholder="Review destination directory",
                        id="sequence-destination",
                    )
                yield Static(self._safety_text(), id="sequence-safety", markup=False)
            with Horizontal(id="sequence-actions"):
                yield Button("Cancel", id="sequence-cancel")
                yield Button(
                    self._continue_label(target), id="sequence-continue", variant="primary"
                )

    def on_mount(self) -> None:
        table = self.query_one("#sequence-results", DataTable)
        table.add_columns("Type", "Item", "Size")
        for index, item in enumerate(self.preview.items[:100]):
            table.add_row(
                item.kind,
                sanitize_terminal(item.title, max_length=180),
                human_bytes(item.size),
                key=f"{index}:{item.owner_reference}:{item.reference}",
            )
        blocked = (
            bool(self.preview.blocked_reason)
            or not self.preview.items
            or (
                self.preview.target_command_id == "attachments.download_all_attachments"
                and (
                    self.preview.unknown_size_count > 0
                    or self.preview.known_bytes > self.MAX_BULK_ATTACHMENT_BYTES
                )
            )
        )
        self.query_one("#sequence-continue", Button).disabled = blocked

    @staticmethod
    def _needs_destination(command_id: str) -> bool:
        return command_id in {
            "backup.back_up_search_results",
            "attachments.download_all_attachments",
        }

    @staticmethod
    def _target_label(command_id: str) -> str:
        return {
            "mail.mark_read": "Mark the exact result set read",
            "mail.mark_unread": "Mark the exact result set unread",
            "backup.back_up_search_results": "Configure a selected-message backup",
            "attachments.download_all_attachments": "Download the exact attachment set",
        }.get(command_id, "Review follow-up")

    @staticmethod
    def _continue_label(command_id: str) -> str:
        return "Configure backup" if command_id == "backup.back_up_search_results" else "Continue"

    def _safety_text(self) -> str:
        if self.preview.blocked_reason:
            return self.preview.blocked_reason
        if not self.preview.items:
            return "No matching items; nothing can run."
        if self.preview.target_command_id == "attachments.download_all_attachments":
            if self.preview.unknown_size_count:
                return (
                    "Bulk download is blocked because every attachment needs a known size. "
                    "Use the attachment workspace to review unknown-size files individually."
                )
            if self.preview.known_bytes > self.MAX_BULK_ATTACHMENT_BYTES:
                return "Bulk download is blocked above the 500 MiB sequence limit."
            return (
                "No payload has been downloaded. Review the destination; a second exact "
                "confirmation is required and files are never opened automatically."
            )
        if self.preview.target_command_id == "backup.back_up_search_results":
            return (
                "Review the destination here, then review and confirm the immutable backup in "
                "Backup Center."
            )
        return "No message has changed. Continue to a separate exact-target confirmation."

    @on(Button.Pressed, "#sequence-actions Button")
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sequence-cancel":
            self.dismiss(None)
            return
        destination: Path | None = None
        if self._needs_destination(self.preview.target_command_id):
            raw = self.query_one("#sequence-destination", Input).value.strip()
            if not raw or any(ord(character) < 32 for character in raw):
                self.query_one("#sequence-safety", Static).update(
                    "Enter a valid destination directory before continuing."
                )
                return
            destination = Path(raw).expanduser().resolve(strict=False)
        self.dismiss(PaletteSequenceDecision(self.preview, destination))

    def action_cancel(self) -> None:
        self.dismiss(None)


class PaletteParameterReviewScreen(ModalScreen[CommandInvocation | None]):
    """Editable, typed review of parameters interpreted for a mutating command."""

    DEFAULT_CSS = """
    PaletteParameterReviewScreen { align: center middle; }
    #parameter-review-box {
        width: 86%; max-width: 88; height: 88%; max-height: 28; min-height: 12;
        border: thick $warning; background: $gucmail-modal-background; padding: 1 2;
    }
    #parameter-review-summary, #parameter-review-error { height: auto; margin-bottom: 1; }
    #parameter-review-content { height: 1fr; }
    #parameter-review-fields { height: auto; }
    .parameter-review-label { height: 1; color: $text-muted; }
    .parameter-review-input { height: 3; margin-bottom: 1; }
    #parameter-review-actions { height: 3; align-horizontal: right; }
    #parameter-review-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        command: CommandSpec,
        invocation: CommandInvocation,
        preview: Sequence[str],
        *,
        coercer: Callable[[Mapping[str, str]], CommandInvocation],
        confirmation_required: bool = True,
    ) -> None:
        super().__init__()
        self.command = command
        self.invocation = invocation
        self.preview = tuple(preview)
        self.coercer = coercer
        self.confirmation_required = confirmation_required
        self._parameters = invocation.parameters

    def compose(self) -> ComposeResult:
        with Vertical(id="parameter-review-box"):
            yield Label(f"Review · {self.command.name}", classes="dialog-title", markup=False)
            with VerticalScroll(id="parameter-review-content"):
                yield Static(
                    "Inspect or edit the interpreted values. Nothing has executed.\n"
                    + " · ".join(self.preview[:4]),
                    id="parameter-review-summary",
                    markup=False,
                )
                with Vertical(id="parameter-review-fields"):
                    for index, (name, value) in enumerate(self._parameters):
                        spec = next(
                            (
                                parameter
                                for parameter in self.command.parameters
                                if parameter.name == name
                            ),
                            None,
                        )
                        choices = (
                            f" · choices: {', '.join(spec.choices)}"
                            if spec and spec.choices
                            else ""
                        )
                        yield Label(
                            f"{name.replace('_', ' ').title()}{choices}",
                            classes="parameter-review-label",
                            markup=False,
                        )
                        yield Input(
                            value=self._editable_value(value),
                            id=f"parameter-review-{index}",
                            classes="parameter-review-input",
                        )
                yield Static("", id="parameter-review-error", markup=False)
            with Horizontal(id="parameter-review-actions"):
                yield Button("Cancel", id="parameter-review-cancel")
                yield Button(
                    "Review confirmation"
                    if self.confirmation_required
                    else "Apply"
                    if self.command.changes_local and not self.command.changes_server
                    else "Continue",
                    id="parameter-review-continue",
                    variant="primary",
                )

    @staticmethod
    def _editable_value(value: object) -> str:
        if isinstance(value, DateRange):
            start = value.start.isoformat() if value.start is not None else ""
            end = value.end.isoformat() if value.end is not None else ""
            return f"{start} .. {end}"
        if isinstance(value, SizeConstraint):
            if value.minimum_bytes is not None and value.maximum_bytes is not None:
                return f"between {value.minimum_bytes} bytes and {value.maximum_bytes} bytes"
            if value.minimum_bytes is not None:
                return f"larger than {value.minimum_bytes} bytes"
            return f"under {value.maximum_bytes or 0} bytes"
        if isinstance(value, (tuple, list)):
            return ", ".join(PaletteParameterReviewScreen._editable_value(item) for item in value)
        for attribute in ("local", "expression", "email", "canonical", "display"):
            candidate = getattr(value, attribute, None)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)

    @on(Button.Pressed, "#parameter-review-actions Button")
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "parameter-review-cancel":
            self.dismiss(None)
            return
        raw = {
            name: self.query_one(f"#parameter-review-{index}", Input).value.strip()
            for index, (name, _value) in enumerate(self._parameters)
        }
        try:
            invocation = self.coercer(raw)
        except (ConfigurationError, ValueError) as exc:
            self.query_one("#parameter-review-error", Static).update(
                sanitize_terminal(str(exc), max_length=500)
            )
            return
        self.dismiss(invocation)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PaletteScreen(ModalScreen[PaletteSubmission | PaletteSequenceSubmission | None]):
    """One Spotlight-style surface for commands, natural language, and mail search."""

    DEFAULT_CSS = """
    PaletteScreen { align: center top; }
    #palette-box {
        width: 92%; max-width: 100; height: 1fr; max-height: 21; min-height: 10;
        margin-top: 2;
        border: tall $accent; background: $gucmail-command-palette-background;
    }
    #palette-input { margin: 1 1 0 1; }
    #palette-explanation {
        height: auto; min-height: 2; max-height: 6; margin: 0 1; color: $text-muted;
        text-wrap: wrap;
    }
    #palette-suggestions { height: 1fr; min-height: 3; }
    .palette-category { height: 1; padding: 0 1; color: $accent; background: $boost; }
    .palette-item { height: auto; min-height: 2; padding: 0 1; }
    .palette-item Label { width: 1fr; height: auto; text-wrap: wrap; }
    #palette-hint {
        height: auto; min-height: 1; max-height: 2; padding: 0 1;
        color: $text-muted; background: $boost; text-wrap: wrap;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    CONTEXT_BINDINGS: ClassVar = (
        Binding("down", "focus_suggestions", "From query to matching suggestions"),
        Binding("up", "focus_query", "From first suggestion to query"),
    )

    def __init__(
        self,
        timezone: str,
        *,
        command_context: CommandContext | Mapping[str, str] | None = None,
        context_supplier: Callable[[], CommandContext] | None = None,
        folder_candidates: Sequence[str] = (),
        person_candidates: Sequence[str] = (),
        recent_command_ids: Sequence[str] = (),
        supplemental_suggestions: Sequence[PaletteSuggestion] = (),
        usage_counts: Mapping[str, int] | None = None,
        handler_ids: Sequence[str] | None = None,
        fuzzy_threshold: float = 0.68,
        natural_language: bool = True,
        suggestion_count: int = 18,
        show_descriptions: bool = True,
    ) -> None:
        super().__init__()
        self.timezone = timezone
        if isinstance(command_context, CommandContext):
            runtime = command_context
        else:
            legacy = dict(command_context or {})
            runtime = CommandContext.from_mapping(
                legacy,
                online=legacy.get("online", "").casefold() == "true",
                offline_mode=legacy.get("offline", "").casefold() == "true",
            )
        if handler_ids is not None:
            runtime = replace(runtime, handler_ids=frozenset(handler_ids))
        self.runtime_context = runtime
        self._context_supplier = context_supplier
        self._context_timer: Timer | None = None
        self.command_context = dict(runtime.values)
        self.folder_candidates = tuple(folder_candidates)
        self.person_candidates = tuple(person_candidates)
        self.recent_command_ids = tuple(dict.fromkeys(recent_command_ids))[:20]
        self.supplemental_suggestions = tuple(supplemental_suggestions)[:12]
        self.usage_counts = {
            command_id: max(int(count), 0)
            for command_id, count in (usage_counts or {}).items()
            if command_id in {command.command_id for command in universal_palette_registry().all()}
        }
        self.natural_language = natural_language
        self.suggestion_count = min(max(suggestion_count, 3), 50)
        self.show_descriptions = show_descriptions
        self.registry = universal_palette_registry()
        threshold = fuzzy_threshold / 100.0 if fuzzy_threshold > 1.0 else fuzzy_threshold
        self.engine = PaletteEngine(
            self.registry,
            timezone=self.timezone,
            fuzzy_threshold=min(max(threshold, 0.0), 1.0),
            usage_score=lambda command_id: min(
                self.usage_counts.get(command_id, 0) / 10.0,
                1.0,
            ),
        )
        self._interpretation = self.engine.interpret(
            "",
            context=self.runtime_context,
            folder_candidates=self.folder_candidates,
            person_candidates=self.person_candidates,
        )
        self._suggestions: tuple[PaletteSuggestion, ...] = ()
        self._display_suggestions: tuple[PaletteSuggestion | None, ...] = ()
        self._sequence: NaturalLanguageSequence | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Input(placeholder="Search or describe an action…", id="palette-input")
            yield Static(
                "Type a command, mail search, setting, or question. Interpretation stays local.",
                id="palette-explanation",
            )
            yield ListView(id="palette-suggestions")
            yield Static(
                "↑↓ Choose · Enter Run/open · Tab Focus · Esc Close",
                id="palette-hint",
            )

    async def on_mount(self) -> None:
        await self._update_interpretation("")
        self.query_one("#palette-input", Input).focus()
        if self._context_supplier is not None:
            self._context_timer = self.set_interval(0.25, self._refresh_availability)

    def on_unmount(self) -> None:
        if self._context_timer is not None:
            self._context_timer.stop()

    def _refresh_runtime_context(self) -> bool:
        if self._context_supplier is None:
            return False
        runtime = self._context_supplier()
        if runtime == self.runtime_context:
            return False
        self.runtime_context = runtime
        self.command_context = dict(runtime.values)
        return True

    async def _refresh_availability(self) -> None:
        if self.app.screen is self and self._refresh_runtime_context():
            await self._update_interpretation(
                self.query_one("#palette-input", Input).value, preserve_selection=True
            )

    @on(Input.Changed, "#palette-input")
    async def explain(self, event: Input.Changed) -> None:
        await self._update_interpretation(event.value)

    @on(Input.Submitted, "#palette-input")
    def submit(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            return
        if self._sequence is not None and self._sequence.complete:
            self.dismiss(PaletteSequenceSubmission(self._sequence))
            return
        if self._interpretation.invocation is not None:
            self._submit_invocation(self._interpretation.invocation, self._interpretation)
            return
        suggestions = self.query_one("#palette-suggestions", ListView)
        index = suggestions.index or 0
        if 0 <= index < len(self._display_suggestions):
            suggestion = self._display_suggestions[index]
            if suggestion is not None:
                self._submit_suggestion(suggestion)

    @on(ListView.Selected, "#palette-suggestions")
    def suggestion_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._display_suggestions):
            suggestion = self._display_suggestions[index]
            if suggestion is not None:
                self._submit_suggestion(suggestion)

    @on(ListView.Highlighted, "#palette-suggestions")
    def suggestion_highlighted(self, event: ListView.Highlighted) -> None:
        if not event.list_view.has_focus:
            return
        index = event.list_view.index
        if index is None or not 0 <= index < len(self._display_suggestions):
            return
        suggestion = self._display_suggestions[index]
        if suggestion is not None:
            self.query_one("#palette-explanation", Static).update(
                self._suggestion_detail(suggestion)
            )

    def on_key(self, event: events.Key) -> None:
        suggestions = self.query_one("#palette-suggestions", ListView)
        if (
            event.key == self.CONTEXT_BINDINGS[0].key
            and isinstance(self.focused, Input)
            and self._suggestions
        ):
            suggestions.focus()
            index = suggestions.index
            if index is not None and 0 <= index < len(self._display_suggestions):
                suggestion = self._display_suggestions[index]
                if suggestion is not None:
                    self.query_one("#palette-explanation", Static).update(
                        self._suggestion_detail(suggestion)
                    )
            event.prevent_default()
            event.stop()
        elif (
            event.key == self.CONTEXT_BINDINGS[1].key
            and self.focused is suggestions
            and suggestions.index in {None, 0}
        ):
            self.query_one("#palette-input", Input).focus()
            event.prevent_default()
            event.stop()

    async def _update_interpretation(self, text: str, *, preserve_selection: bool = False) -> None:
        self._refresh_runtime_context()
        list_view = self.query_one("#palette-suggestions", ListView)
        index = list_view.index
        selected = (
            self._display_suggestions[index]
            if preserve_selection
            and index is not None
            and 0 <= index < len(self._display_suggestions)
            else None
        )
        self._sequence = None
        interpreted = self.engine.interpret(
            text,
            context=self.runtime_context,
            folder_candidates=self.folder_candidates,
            person_candidates=self.person_candidates,
        )
        if self.natural_language or not text.strip():
            self._interpretation = interpreted
            if text.strip():
                sequence = self.engine.interpret_sequence(
                    text,
                    context=self.runtime_context,
                    folder_candidates=self.folder_candidates,
                    person_candidates=self.person_candidates,
                )
                if sequence.complete and len(sequence.steps) > 1:
                    self._sequence = sequence
            rows = self.engine.suggest(
                text,
                context=self.runtime_context,
                folder_candidates=self.folder_candidates,
                person_candidates=self.person_candidates,
                recent_command_ids=self.recent_command_ids,
                limit=self.suggestion_count,
            )
        else:
            self._interpretation = NaturalLanguageResult(
                original=text,
                normalized=text.casefold(),
                kind=InterpretationKind.INCOMPLETE,
                invocation=None,
                candidates=(),
                entities=interpreted.entities,
                explanation="Natural-language interpretation is disabled; showing command matches.",
            )
            rows = tuple(
                PaletteSuggestion(
                    command=command,
                    score=1.0,
                    source=SuggestionSource.COMMAND,
                    availability=self.registry.availability(
                        command.command_id, self.runtime_context
                    ),
                    reason="Command metadata match",
                )
                for command in self.registry.search(text, limit=self.suggestion_count)
            )
        rows = tuple(self._apply_route_availability(row) for row in rows)
        if not text.strip() and self.supplemental_suggestions:
            suggested_rows = tuple(row for row in rows if row.group == "Suggested actions")
            ordinary_rows = tuple(row for row in rows if row.group != "Suggested actions")
            rows = (
                *suggested_rows,
                *tuple(
                    self._apply_route_availability(row) for row in self.supplemental_suggestions
                ),
                *ordinary_rows,
            )[: self.suggestion_count]
        categories: dict[str, list[PaletteSuggestion]] = {}
        for row in rows:
            count = self.usage_counts.get(row.command.command_id, 0)
            if row.group:
                category = row.group
            elif not text.strip() and count > 1:
                category = "Frequently used commands"
            elif not text.strip() and row.command.command_id in self.recent_command_ids:
                category = "Recently used commands"
            elif not text.strip() and row.command.category == self.command_context.get(
                "current_area"
            ):
                category = "Context-sensitive commands"
            else:
                category = row.command.category
            categories.setdefault(category, []).append(row)
        rows = tuple(row for group in categories.values() for row in group)
        explanation = (
            self._sequence_detail(self._sequence)
            if self._sequence is not None
            else self._interpretation_detail(self._interpretation)
        )
        self.query_one("#palette-explanation", Static).update(explanation)
        self._suggestions = rows
        recent = set(self.recent_command_ids)
        items: list[ListItem] = []
        display_rows: list[PaletteSuggestion | None] = []
        for category, suggestions_for_category in categories.items():
            items.append(
                ListItem(
                    Label(category.title(), markup=False),
                    classes="palette-category",
                    disabled=True,
                )
            )
            display_rows.append(None)
            for suggestion in suggestions_for_category:
                command = suggestion.command
                unavailable = (
                    "; ".join(suggestion.availability.reasons)
                    if not suggestion.availability.available
                    else ""
                )
                shortcut = f" · {command.shortcut}" if command.shortcut else ""
                count = self.usage_counts.get(command.command_id, 0)
                marker = (
                    f"Frequent x{count} · "
                    if count > 1
                    else "Recent · "
                    if command.command_id in recent
                    and suggestion.source
                    not in {
                        SuggestionSource.SUGGESTED,
                        SuggestionSource.RECENT_SEARCH,
                        SuggestionSource.RECENT_FOLDER,
                        SuggestionSource.RECENT_PERSON,
                    }
                    else ""
                )
                status = "Unavailable" if unavailable else ""
                description = (
                    self._metadata_description(command.name, command.description)
                    if self.show_descriptions
                    else ""
                )
                detail = " · ".join(part for part in (description, status) if part)
                label = f"{marker}{suggestion.display_name or command.name}{shortcut}"
                if detail:
                    label += "\n" + detail
                items.append(
                    ListItem(
                        Label(label, markup=False),
                        classes="palette-item",
                    )
                )
                display_rows.append(suggestion)
        self._display_suggestions = tuple(display_rows)
        suggestions = self.query_one("#palette-suggestions", ListView)
        await suggestions.clear()
        await suggestions.extend(items)
        suggestions.index = next(
            (
                index
                for index, row in enumerate(display_rows)
                if selected is not None
                and row is not None
                and row.command.command_id == selected.command.command_id
                and row.display_name == selected.display_name
            ),
            1 if len(items) > 1 else None,
        )

    @staticmethod
    def _metadata_description(title: str, description: str) -> str:
        """Suppress a repeated trusted metadata title, preserving other detail."""

        def normalized(value: str) -> str:
            return " ".join(value.split()).rstrip(".").casefold()

        if normalized(title) == normalized(description):
            return ""
        prefix = title.strip().rstrip(".") + ". "
        if description.casefold().startswith(prefix.casefold()):
            return description[len(prefix) :].lstrip()
        return description

    def _interpretation_detail(self, result: NaturalLanguageResult) -> str:
        if result.invocation is None:
            return sanitize_terminal(result.explanation, max_length=500)
        plan = self.engine.plan(result.invocation, context=self.runtime_context)
        command = plan.command
        route_reason = self._route_gate_reason(command.command_id)
        if route_reason:
            return sanitize_terminal(
                f"→ {command.name} ({result.confidence:.0%}) · unavailable\n{route_reason}",
                max_length=1_000,
            )
        parameters = self._format_parameters(result.invocation)
        if command.command_id in {
            "search.search_current_folder",
            "search.search_all_mail",
            "search.mail",
        } and not result.invocation.get("query"):
            parameters = "Enter opens a prompt for message text."
        effect = (
            "changes Exchange mailbox and local application data"
            if command.changes_server and command.changes_local
            else "changes Exchange mailbox data"
            if command.changes_server
            else "changes local application data"
            if command.changes_local
            else "changes state"
            if command.mutates
            else "read-only"
        )
        safety = " · " + ("confirmation required · " if plan.confirmation_required else "") + effect
        # The first two plan preview fields are this same name/description.
        # Keep the real plan intact for review; avoid repeating it in the heading.
        detail = parameters or self._metadata_description(command.name, command.description)
        heading = f"→ {command.name} ({result.confidence:.0%}){safety}"
        return sanitize_terminal(
            heading + ("\n" + detail if detail else ""),
            max_length=1_000,
        )

    def _sequence_detail(self, sequence: NaturalLanguageSequence) -> str:
        names = [
            self.registry.get(step.invocation.command_id).name
            for step in sequence.steps
            if step.invocation is not None
        ]
        first = sequence.steps[0]
        parameters = self._format_parameters(first.invocation) if first.invocation else ""
        return sanitize_terminal(
            "Bounded sequence · "
            + " → ".join(names)
            + f"\n{parameters or 'No search parameters'}\n"
            + "Enter previews real matches; no follow-up runs before a second confirmation.",
            max_length=1_000,
        )

    def _suggestion_detail(self, suggestion: PaletteSuggestion) -> str:
        command = suggestion.command
        invocation = suggestion.invocation or CommandInvocation(command.command_id)
        plan = self.engine.plan(invocation, context=self.runtime_context)
        cli = (
            "gucmail " + " ".join(plan.cli.argv)
            if plan.cli.available and plan.cli.argv
            else ""
        )
        follow_ups = tuple(
            self.registry.get(command_id).name for command_id in command.follow_up_commands[:3]
        )
        next_actions = "Next: " + ", ".join(follow_ups) if follow_ups else ""
        availability = (
            "Available"
            if suggestion.availability.available
            else "; ".join(suggestion.availability.reasons)
        )
        detail = self._metadata_description(
            command.name, suggestion.reason or command.long_description
        )
        parts = (f"{suggestion.display_name or command.name} · {availability}",
                 detail, cli, next_actions)
        return sanitize_terminal("\n".join(part for part in parts if part), max_length=1_000)

    @staticmethod
    def _format_parameters(invocation: CommandInvocation) -> str:
        values = []
        for name, value in invocation.parameters:
            display = (
                getattr(value, "display", None)
                or getattr(value, "expression", None)
                or getattr(value, "original", None)
                or getattr(value, "local", None)
                or value
            )
            values.append(f"{name.replace('_', ' ').title()}: {display}")
        return " · ".join(values)

    def _coerce_reviewed_invocation(
        self,
        invocation: CommandInvocation,
        raw_parameters: Mapping[str, str],
    ) -> CommandInvocation:
        """Rebuild edited palette parameters using the command registry's declared types."""

        command = self.registry.get(invocation.command_id)
        specifications = {parameter.name: parameter for parameter in command.parameters}
        parameters: dict[str, object] = {}
        for name, raw_value in raw_parameters.items():
            specification = specifications.get(name)
            if specification is None:
                raise ConfigurationError(f"{name!r} is not a parameter of {command.name}.")
            if not raw_value.strip():
                if specification.required:
                    raise ConfigurationError(f"{name.replace('_', ' ').title()} cannot be empty.")
                continue
            parameters[name] = self._coerce_reviewed_parameter(specification, raw_value)
        revised = CommandInvocation.from_mapping(
            invocation.command_id,
            parameters,
            original_text=invocation.original_text,
            confidence=invocation.confidence,
            explanation=invocation.explanation,
        )
        errors = self.registry.validate_invocation(revised)
        if errors:
            raise ConfigurationError("; ".join(errors))
        return revised

    def _coerce_reviewed_parameter(self, specification: ParameterSpec, raw_value: str) -> object:
        if specification.multiple:
            parts = tuple(part.strip() for part in raw_value.split(",") if part.strip())
            if not parts:
                raise ConfigurationError(
                    f"{specification.name.replace('_', ' ').title()} needs at least one value."
                )
            scalar = replace(specification, multiple=False)
            return tuple(self._coerce_reviewed_parameter(scalar, part) for part in parts)

        parameter_type = specification.type
        if parameter_type is ParameterType.INTEGER:
            try:
                return int(raw_value)
            except ValueError as exc:
                raise ConfigurationError(
                    f"{specification.name.replace('_', ' ').title()} must be a whole number."
                ) from exc
        if parameter_type is ParameterType.NUMBER:
            try:
                return float(raw_value)
            except ValueError as exc:
                raise ConfigurationError(
                    f"{specification.name.replace('_', ' ').title()} must be a number."
                ) from exc
        if parameter_type is ParameterType.BOOLEAN:
            normalized = raw_value.casefold()
            if normalized in {"true", "yes", "on", "1"}:
                return True
            if normalized in {"false", "no", "off", "0"}:
                return False
            raise ConfigurationError(
                f"{specification.name.replace('_', ' ').title()} must be true or false."
            )
        if parameter_type is ParameterType.PERSON:
            return PersonEntity(
                raw_value,
                raw_value if "@" in raw_value else None,
                source="palette_review",
            )
        if parameter_type is ParameterType.FOLDER:
            return FolderEntity(raw_value, raw_value, source="palette_review")
        if parameter_type is ParameterType.DATETIME:
            return parse_datetime_expression(
                raw_value,
                timezone=self.timezone,
                future_required=True,
            )
        if parameter_type is ParameterType.DATE_RANGE:
            if ".." not in raw_value:
                parsed_range = extract_date_range(raw_value, timezone=self.timezone)
                if parsed_range is None:
                    raise ConfigurationError(
                        "Date range must be a recognizable phrase or 'start .. end'."
                    )
                return parsed_range
            raw_start, _separator, raw_end = raw_value.partition("..")

            def parse_boundary(value: str) -> datetime | None:
                if not value.strip():
                    return None
                try:
                    parsed = datetime.fromisoformat(value.strip())
                except ValueError as exc:
                    raise ConfigurationError(
                        "Date-range boundaries must use ISO format, such as 2026-08-10."
                    ) from exc
                return (
                    parsed.replace(tzinfo=ZoneInfo(self.timezone))
                    if parsed.tzinfo is None
                    else parsed.astimezone(ZoneInfo(self.timezone))
                )

            start = parse_boundary(raw_start)
            end = parse_boundary(raw_end)
            if start is None and end is None:
                raise ConfigurationError("Date range needs a start or end boundary.")
            if start is not None and end is not None and end <= start:
                raise ConfigurationError("Date-range end must be after its start.")
            return DateRange(start, end, self.timezone, raw_value)
        if parameter_type in {ParameterType.SIZE, ParameterType.SIZE_RANGE}:
            size = extract_size_constraint(raw_value)
            if size is None:
                raise ConfigurationError(
                    "Size must include a value and unit, such as 'larger than 5 MB'."
                )
            return size
        if parameter_type is ParameterType.EXCLUSION:
            exclusions = extract_exclusions(raw_value)
            if len(exclusions) != 1:
                raise ConfigurationError(
                    "Each exclusion must use a phrase such as 'not from Ahmed'."
                )
            return exclusions[0]
        if parameter_type is ParameterType.RANKING:
            ranking = extract_ranking(raw_value)
            if ranking is None:
                raise ConfigurationError(
                    "Ranking must be a phrase such as 'oldest', 'newest', or 'largest'."
                )
            return ranking
        if parameter_type is ParameterType.THREAD_RESPONSE:
            response = extract_thread_response_state(raw_value)
            if response is not None:
                return response
            normalized = raw_value.casefold().strip()
            if normalized in {"waiting_on_me", "waiting_on_them"}:
                return ThreadResponseState(normalized, raw_value)
            raise ConfigurationError("Response state must be 'waiting on me' or 'waiting on them'.")
        return raw_value

    def _unavailable_reason(
        self, command: CommandSpec, invocation: CommandInvocation | None = None
    ) -> str | None:
        availability = self.registry.availability(
            command.command_id,
            self.runtime_context,
            invocation=invocation,
        )
        reasons = list(availability.reasons) if not availability.available else []
        route_reason = self._route_gate_reason(command.command_id)
        if route_reason:
            reasons.append(route_reason)
        return "; ".join(dict.fromkeys(reasons)) if reasons else None

    @staticmethod
    def _route_gate_reason(command_id: str) -> str | None:
        """Return a TUI-specific capability gate for generated catalog actions."""

        try:
            route = catalog_route(command_id)
        except KeyError:
            return None
        return route.gate_reason if route.is_gate else None

    def _apply_route_availability(self, suggestion: PaletteSuggestion) -> PaletteSuggestion:
        reason = self._route_gate_reason(suggestion.command.command_id)
        if not reason:
            return suggestion
        existing = suggestion.availability.reasons
        availability = CommandAvailability(
            AvailabilityState.UNAVAILABLE,
            tuple(dict.fromkeys((*existing, reason))),
            suggestion.availability.missing_context,
            suggestion.availability.missing_services,
        )
        return replace(suggestion, availability=availability)

    def _submit_suggestion(self, suggestion: PaletteSuggestion) -> None:
        if suggestion.invocation is not None:
            self._submit_invocation(suggestion.invocation, self._interpretation)
        else:
            self._submit_spec(suggestion.command)

    def _submit_spec(self, command: CommandSpec) -> None:
        self._refresh_runtime_context()
        reason = self._unavailable_reason(command)
        if reason:
            self.query_one("#palette-explanation", Static).update(
                f"{command.name} is unavailable: {reason}. Choose another command or "
                "restore context."
            )
            return
        parameters: dict[str, object] = {}
        selected = next(
            (
                self.command_context[requirement]
                for requirement in command.context_requirements
                if self.command_context.get(requirement)
            ),
            None,
        )
        allowed = {parameter.name for parameter in command.parameters}
        if selected is not None:
            if "reference" in allowed:
                parameters["reference"] = selected
            if "selection" in allowed:
                parameters["selection"] = (selected,)
        presets = {
            "mail.open_inbox": "inbox",
            "mail.open_sent": "sent",
            "mail.open_drafts": "drafts",
            "mail.open_deleted": "deleted",
            "mail.open_junk": "junk",
            "mail.open_archive": "archive",
        }
        if command.command_id in presets and "folder" in allowed:
            parameters["folder"] = presets[command.command_id]
        query = self.query_one("#palette-input", Input).value.strip()
        if query and "query" in allowed:
            parameters["query"] = query
        invocation = CommandInvocation.from_mapping(
            command.command_id,
            parameters,
            original_text=query or command.name,
            confidence=1.0,
            explanation="Explicit palette selection",
        )
        interpretation = NaturalLanguageResult(
            original=query or command.name,
            normalized=query.casefold() or command.name.casefold(),
            kind=InterpretationKind.COMMAND,
            invocation=invocation,
            candidates=(),
            entities=self._interpretation.entities,
            explanation=f"Explicitly selected {command.name}.",
        )
        self._submit_invocation(invocation, interpretation)

    def _submit_invocation(
        self,
        invocation: CommandInvocation,
        interpretation: NaturalLanguageResult,
    ) -> None:
        self._refresh_runtime_context()
        command = self.registry.get(invocation.command_id)
        plan: CommandPlan = self.engine.plan(invocation, context=self.runtime_context)
        reason = "; ".join(plan.availability.reasons) if not plan.availability.available else ""
        route_reason = self._route_gate_reason(invocation.command_id)
        if route_reason:
            reason = "; ".join(dict.fromkeys(part for part in (reason, route_reason) if part))
        if plan.validation_errors:
            reason = (
                "; ".join((*plan.validation_errors, reason))
                if reason
                else "; ".join(plan.validation_errors)
            )
        if reason:
            self.query_one("#palette-explanation", Static).update(
                f"{command.name} is unavailable: {reason}."
            )
            return
        if command.mutates and invocation.parameters and command.command_id != "mail.restore":

            def reviewed(revised: CommandInvocation | None) -> None:
                if revised is not None:
                    self._finish_submission(revised, interpretation)

            self.app.push_screen(
                PaletteParameterReviewScreen(
                    command,
                    invocation,
                    plan.preview,
                    coercer=lambda raw: self._coerce_reviewed_invocation(invocation, raw),
                    confirmation_required=plan.confirmation_required,
                ),
                reviewed,
            )
            return
        self._finish_submission(invocation, interpretation)

    def _finish_submission(
        self,
        invocation: CommandInvocation,
        interpretation: NaturalLanguageResult,
    ) -> None:
        self._refresh_runtime_context()
        command = self.registry.get(invocation.command_id)
        plan: CommandPlan = self.engine.plan(invocation, context=self.runtime_context)
        reason = "; ".join(plan.availability.reasons) if not plan.availability.available else ""
        route_reason = self._route_gate_reason(invocation.command_id)
        if route_reason:
            reason = "; ".join(dict.fromkeys(part for part in (reason, route_reason) if part))
        if plan.validation_errors:
            reason = (
                "; ".join((*plan.validation_errors, reason))
                if reason
                else "; ".join(plan.validation_errors)
            )
        if reason:
            self.query_one("#palette-explanation", Static).update(
                f"{command.name} is unavailable: {reason}."
            )
            return
        # Restore's exact item/destination plan owns the one final confirmation.
        # This submission authorizes opening that preview only, never execution.
        if invocation.command_id == "mail.restore":
            self.dismiss(PaletteSubmission(invocation, interpretation))
            return
        if plan.confirmation_required:
            parameters = self._format_parameters(invocation) or "No parameters"
            scope = (
                f"Server data: {'changes' if command.changes_server else 'unchanged'}\n"
                f"Local data: {'changes' if command.changes_local else 'unchanged'}"
            )
            detail = sanitize_terminal(
                f"{interpretation.explanation}\n\n{parameters}\n\n{scope}\n\n"
                + "\n".join(plan.preview),
                max_length=2_000,
            )

            def confirmed(value: bool | None) -> None:
                if value:
                    self.dismiss(PaletteSubmission(invocation, interpretation, confirmed=True))

            self.app.push_screen(ConfirmationScreen(f"Confirm · {command.name}", detail), confirmed)
            return
        self.dismiss(PaletteSubmission(invocation, interpretation))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ComposerInput(Input):
    """Input that treats Backspace on an empty field as modal navigation."""

    def action_delete_left(self) -> None:
        if not self.value and isinstance(self.screen, ComposeScreen):
            self.screen.action_close()
            return
        super().action_delete_left()


class ComposerTextArea(TextArea):
    """Composer body with the same empty-Backspace navigation convention."""

    def action_delete_left(self) -> None:
        if not self.text and isinstance(self.screen, ComposeScreen):
            self.screen.action_close()
            return
        super().action_delete_left()


def _compose_attachment_paths(value: str) -> list[str]:
    """Read legacy delimited input or an unambiguous encoded list from the file picker."""
    if value.startswith("["):
        with contextlib.suppress(json.JSONDecodeError):
            decoded = json.loads(value)
            if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
                return decoded
    return [part.strip() for part in value.replace("\n", ";").split(";") if part.strip()]


def _encode_compose_attachment_paths(values: Sequence[str]) -> str:
    if any(";" in value or "\n" in value or value.startswith("[") for value in values):
        return json.dumps(list(values), ensure_ascii=False)
    return "; ".join(values)


class ComposeScreen(ComposerSubmissionSession):
    USES_GLOBAL_PALETTE_SHORTCUTS: ClassVar = True
    DEFAULT_CSS = """
    ComposeScreen { align: center middle; }
    #composer {
        width: 96%; max-width: 120; height: 96%; border: round $border;
        background: $gucmail-modal-background; padding: 1 2;
    }
    #compose-fields { height: 1fr; }
    #composer TextArea { height: 1fr; min-height: 8; }
    #compose-note { height: auto; min-height: 1; color: $text-muted; }
    #compose-inherited { height: auto; margin-bottom: 1; }
    #compose-actions { height: 3; align-horizontal: right; }
    #compose-actions Button {
        width: auto; min-width: 8; height: 3; margin-left: 1; padding: 0 1;
    }
    #compose-actions #compose-save { width: 14; }
    #compose-actions #compose-send { width: 16; }
    #compose-importance { width: 14; }
    #compose-options { height: 3; }
    #compose-receipts { height: 3; width: 1fr; }
    #compose-options Select { width: 22; margin-right: 1; }
    #compose-options Checkbox { width: auto; margin-right: 2; }
    #compose-sent-copy { height: 3; width: auto; }
    .compose-label { width: 8; padding-top: 1; }
    #compose-extra-addresses {
        height: 1; min-height: 1; border: none; width: auto; margin-left: 8;
    }
    #compose-attachment-controls { height: 3; }
    #compose-attachment-controls Select { width: 1fr; }
    #compose-attachment-controls Button { width: 14; min-width: 0; }
    #compose-attachments { display: none; }
    .compose-row { height: 3; }
    .compose-row Input { width: 1fr; }
    ComposeScreen.compact-compose #compose-options { layout: vertical; height: 6; }
    ComposeScreen.compact-compose #compose-body-mode { width: 100%; }
    ComposeScreen.compact-compose #compose-importance { width: 14; }
    ComposeScreen.compact-compose #compose-actions Button { min-width: 6; margin-left: 1; }
    ComposeScreen.compact-compose #compose-actions #compose-save { width: 12; }
    ComposeScreen.compact-compose #compose-actions #compose-send { width: 13; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("backspace", "close", "Close", show=False),
    ]

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        event.widget.scroll_visible(animate=False, force=True, immediate=True)

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 85, "compact-compose")
        self.call_after_refresh(self._reveal_focus)

    def _reveal_focus(self) -> None:
        if self.focused is not None:
            self.focused.scroll_visible(animate=False, force=True, immediate=True)

    def __init__(
        self,
        draft: DraftData | None = None,
        *,
        kind: ComposeKind = ComposeKind.NEW,
        inherited_attachments: tuple[AttachmentInfo, ...] | None = None,
    ) -> None:
        super().__init__()
        candidate = draft or DraftData(reference=f"dft-{uuid4().hex}")
        self.draft = (
            replace(candidate, reference=f"dft-{uuid4().hex}")
            if candidate.reference.startswith("preview:")
            else candidate
        )
        self.kind = (
            ComposeKind.FORWARD
            if self.draft.forward_of_reference
            else ComposeKind.REPLY
            if self.draft.reply_to_reference and kind is ComposeKind.NEW
            else kind
        )
        self.inherited_attachments = (
            inherited_attachments if self.draft.forward_of_reference else ()
        )

    @staticmethod
    def _addresses(values: list[Address]) -> str:
        return ", ".join(value.email for value in values)

    def composition_reference(self) -> str:
        return self.draft.reference

    def attachments_ready(self) -> bool:
        return self.inherited_attachments is not None

    def compose(self) -> ComposeResult:
        with Vertical(id="composer"):
            with Horizontal(id="compose-heading"):
                yield Label("GUC Mail", id="compose-brand")
                yield Label("Compose message", classes="dialog-title")
                yield Button("Options", id="compose-options-toggle")
                yield Button("Send status", id="compose-send-status")
            with VerticalScroll(id="compose-fields"):
                with Horizontal(classes="compose-row"):
                    yield Label("To", classes="compose-label")
                    yield ComposerInput(value=self._addresses(self.draft.to), id="compose-to")
                yield Button("Show Cc/Bcc", id="compose-extra-addresses")
                with Horizontal(classes="compose-row", id="compose-cc-row"):
                    yield Label("Cc", classes="compose-label")
                    yield ComposerInput(value=self._addresses(self.draft.cc), id="compose-cc")
                with Horizontal(classes="compose-row", id="compose-bcc-row"):
                    yield Label("Bcc", classes="compose-label")
                    yield ComposerInput(value=self._addresses(self.draft.bcc), id="compose-bcc")
                with Horizontal(classes="compose-row"):
                    yield Label("Subject", classes="compose-label")
                    yield ComposerInput(value=self.draft.subject, id="compose-subject")
                yield ComposerInput(
                    value=_encode_compose_attachment_paths(
                        [str(path) for path in self.draft.attachment_paths]
                    ),
                    id="compose-attachments",
                )
                with Horizontal(id="compose-attachment-controls"):
                    yield Select(
                        (("No local files attached", ""),),
                        value="",
                        allow_blank=False,
                        id="compose-attachment-list",
                    )
                    yield Button("Attach file", id="compose-attachment-add")
                    yield Button("Remove", id="compose-attachment-remove", disabled=True)
                yield Static(
                    self._inherited_attachment_text(), id="compose-inherited", markup=False
                )
                with Vertical(id="compose-options-area"):
                    with Horizontal(id="compose-options"):
                        yield Select(
                            (("Plain text", "plain"), ("HTML source", "html")),
                            value="html" if self.draft.body_html is not None else "plain",
                            allow_blank=False,
                            id="compose-body-mode",
                        )
                        with Horizontal(id="compose-receipts"):
                            yield Checkbox(
                                self._receipt_label("Read", self.draft.request_read_receipt),
                                value=self.draft.request_read_receipt,
                                id="compose-read-receipt",
                            )
                            yield Checkbox(
                                self._receipt_label(
                                    "Delivery", self.draft.request_delivery_receipt
                                ),
                                value=self.draft.request_delivery_receipt,
                                id="compose-delivery-receipt",
                            )
                    with Horizontal(id="compose-delivery-options"):
                        yield Select(
                            [
                                ("Normal importance", "normal"),
                                ("High importance", "high"),
                                ("Low importance", "low"),
                            ],
                            value=self.draft.importance.value,
                            allow_blank=False,
                            id="compose-importance",
                        )
                        yield Checkbox(
                            f"Sent copy: {'on' if self.draft.save_sent_copy else 'off'}",
                            value=self.draft.save_sent_copy,
                            id="compose-sent-copy",
                            tooltip="Save a copy in Exchange Sent Items after sending. "
                            "Your choice is retained in local drafts, recovery and scheduled jobs.",
                        )
                yield Label("Message", classes="compose-body-label")
                yield ComposerTextArea(
                    self.draft.body_html
                    if self.draft.body_html is not None
                    else self.draft.body_text,
                    id="compose-body",
                )
            yield Static("", id="compose-note", markup=False)
            with Horizontal(id="compose-actions"):
                yield Button("Cancel", id="compose-cancel")
                yield Button("Editor", id="compose-external-editor")
                yield Button("Save draft", id="compose-save")
                yield Button("Review send", variant="primary", id="compose-send")

    def on_mount(self) -> None:
        self.set_class(self.size.width < 85, "compact-compose")
        self._show_extra_addresses(bool(self.draft.cc or self.draft.bcc))
        self._show_options(
            self.draft.body_html is not None
            or self.draft.importance is not Importance.NORMAL
            or self.draft.request_read_receipt
            or self.draft.request_delivery_receipt
            or not self.draft.save_sent_copy
        )
        self._refresh_attachment_list()
        self.query_one("#compose-inherited").display = bool(self.draft.forward_of_reference)
        self.query_one("#compose-send-status", Button).display = False
        self.refresh_submission_actions()
        if self.inherited_attachments is None and isinstance(self.app, GUCmailApp):
            self.app.load_forward_attachments(self)
        if isinstance(self.app, GUCmailApp):
            self.request_send_status()
            for shortcut in (
                self.app._palette_bound_shortcuts if self.USES_GLOBAL_PALETTE_SHORTCUTS else ()
            ):
                self._bindings.bind(shortcut, "app.palette", "Commands", show=False, priority=True)
            self._refresh_recipient_suggestions(refresh_values=False)
            self.set_interval(
                self.app.context.config.autosave_seconds,
                self._request_autosave,
            )

    def on_screen_resume(self) -> None:
        if self.is_mounted:
            self._refresh_recipient_suggestions()

    def _refresh_recipient_suggestions(self, *, refresh_values: bool = True) -> None:
        from gucmail.services.recipient_assistance import RecipientPreferences
        from gucmail.tui.recipient_suggester import RecipientSuggester

        if not isinstance(self.app, GUCmailApp):
            return
        try:
            policy = RecipientPreferences.from_settings(self.app._settings_service())
        except (ConfigurationError, OSError, ValueError):
            policy = RecipientPreferences(autocomplete=False)
        for name in ("to", "cc", "bcc"):
            field = self.query_one(f"#compose-{name}", Input)
            previous = field.suggester
            enabled = policy.autocomplete and policy.recent_recipients
            if (isinstance(previous, RecipientSuggester) and previous.preferences == policy) or (
                previous is None and not enabled
            ):
                continue
            field.suggester = RecipientSuggester(policy) if enabled else None
            if refresh_values:
                field.mutate_reactive(Input.value)

    def _payload(self, *, action: str) -> dict[str, Any]:
        body = self.query_one("#compose-body", TextArea).text
        body_mode = str(self.query_one("#compose-body-mode", Select).value)
        return {
            "recovery_sequence": perf_counter_ns(),
            "reference": self.draft.reference,
            "revision": self.draft.revision,
            "location": self.draft.location.value,
            "server_id": self.draft.server_id,
            "server_change_key": self.draft.server_change_key,
            "to": self.query_one("#compose-to", Input).value,
            "cc": self.query_one("#compose-cc", Input).value,
            "bcc": self.query_one("#compose-bcc", Input).value,
            "subject": self.query_one("#compose-subject", Input).value,
            "body": html_to_safe_text(body) if body_mode == "html" else body,
            "body_html": body if body_mode == "html" else None,
            "body_mode": body_mode,
            "attachment_paths": self.query_one("#compose-attachments", Input).value,
            "request_read_receipt": self.query_one("#compose-read-receipt", Checkbox).value,
            "request_delivery_receipt": self.query_one("#compose-delivery-receipt", Checkbox).value,
            "save_sent_copy": self.query_one("#compose-sent-copy", Checkbox).value,
            "importance": self.query_one("#compose-importance", Select).value,
            "action": action,
            "kind": self.kind.value,
            "reply_to_reference": self.draft.reply_to_reference,
            "forward_of_reference": self.draft.forward_of_reference,
        }

    def _request_autosave(self) -> None:
        if (
            self._submission_pending or self._submission_uncertain or self._submission_complete
            or self._attempt_pending or self._attempt_blocked
        ):
            return
        payload = self._payload(action="autosave")
        content_fields = ("to", "cc", "bcc", "subject", "body")
        if (
            any(str(payload[field]).strip() for field in content_fields)
            and isinstance(self.app, GUCmailApp)
            and bool(self.app._setting_value("composer.local_recovery", True))
        ):
            self.app.autosave_composer(payload)

    def _has_unsaved_changes(self) -> bool:
        """Compare editor values with the draft that opened the composer."""

        payload = self._payload(action="compare")
        original = {
            "to": self._addresses(self.draft.to),
            "cc": self._addresses(self.draft.cc),
            "bcc": self._addresses(self.draft.bcc),
            "subject": self.draft.subject,
            "body": self.draft.body_text,
            "importance": self.draft.importance.value,
            "body_html": self.draft.body_html,
            "attachment_paths": _encode_compose_attachment_paths(
                [str(path) for path in self.draft.attachment_paths]
            ),
            "request_read_receipt": self.draft.request_read_receipt,
            "request_delivery_receipt": self.draft.request_delivery_receipt,
            "save_sent_copy": self.draft.save_sent_copy,
        }
        return any(str(payload[key]) != str(value) for key, value in original.items())

    def _set_note(self, value: str) -> None:
        safe = sanitize_terminal(value, max_length=500)
        if self._attempt_controls:
            # A newer action/validation warning must survive the pending status
            # placeholder. Status failures and uncertain outcomes still take priority.
            self._attempt_previous_note = safe
        self.query_one("#compose-note", Static).update(safe)

    def _show_extra_addresses(self, visible: bool) -> None:
        self.query_one("#compose-cc-row").display = visible
        self.query_one("#compose-bcc-row").display = visible
        self.query_one("#compose-extra-addresses", Button).label = (
            "Hide Cc/Bcc" if visible else "Show Cc/Bcc"
        )

    @on(Input.Changed, "#compose-attachments")
    def _attachments_changed(self) -> None:
        self._refresh_attachment_list()

    def _refresh_attachment_list(self) -> None:
        values = _compose_attachment_paths(self.query_one("#compose-attachments", Input).value)
        listing = self.query_one("#compose-attachment-list", Select)
        previous = listing.value
        listing.set_options(
            [(sanitize_terminal(Path(value).name), value) for value in values]
            or [("No local files attached", "")]
        )
        listing.value = previous if previous in values else values[-1] if values else ""
        self.query_one("#compose-attachment-remove", Button).disabled = not values
        listing.display = bool(values)
        self.query_one("#compose-attachment-remove", Button).display = bool(values)
        self.query_one("#compose-attachment-controls").set_class(bool(values), "has-files")

    def _show_options(self, visible: bool) -> None:
        self.query_one("#compose-options-area").display = visible
        self.query_one("#compose-options-toggle", Button).label = (
            "Hide options" if visible else "Options"
        )

    def action_toggle_options(self) -> None:
        self._show_options(not self.query_one("#compose-options-area").display)

    def action_add_attachment(self) -> None:
        if self._guard_running_change():
            return

        def selected(value: str | None) -> None:
            if value:
                self.add_attachment_path(Path(value).expanduser())

        self.app.push_screen(FilePathScreen("Add attachment", "Path to a local file"), selected)

    def action_remove_attachment(self) -> None:
        if self._guard_running_change():
            return
        field = self.query_one("#compose-attachments", Input)
        selected = self.query_one("#compose-attachment-list", Select).value
        values = _compose_attachment_paths(field.value)
        if selected in values:
            values.remove(str(selected))
            field.value = _encode_compose_attachment_paths(values)
            self._set_note(
                f"Removed {sanitize_terminal(Path(str(selected)).name)} from this draft."
            )
        else:
            self._set_note("This draft has no local file attachment to remove.")

    def add_attachment_path(self, path: Path) -> None:
        if self.defer_status_action(lambda: self.add_attachment_path(path)):
            return
        if self._guard_running_change():
            return
        field = self.query_one("#compose-attachments", Input)
        values = _compose_attachment_paths(field.value)
        resolved = str(path)
        if resolved not in values:
            values.append(resolved)
        field.value = _encode_compose_attachment_paths(values)
        self._set_note(f"Attached {path.name}.")

    def action_toggle_html_plain_text(self) -> None:
        if self._guard_running_change():
            return
        selector = self.query_one("#compose-body-mode", Select)
        body = self.query_one("#compose-body", TextArea)
        if selector.value == "html":
            body.text = html_to_safe_text(body.text)
            selector.value = "plain"
            self._set_note("Converted HTML to safe plain text without fetching resources.")
        else:
            body.text = f"<pre>{html.escape(body.text)}</pre>"
            selector.value = "html"
            self._set_note("Converted plain text to escaped HTML.")

    def action_request_read_receipt(self) -> None:
        if self._guard_running_change():
            return
        control = self.query_one("#compose-read-receipt", Checkbox)
        control.value = not control.value

    @staticmethod
    def _receipt_label(kind: str, enabled: bool) -> str:
        return f"{kind} receipt: {'on' if enabled else 'off'}"

    @on(Checkbox.Changed, "#compose-read-receipt, #compose-delivery-receipt")
    def _receipt_changed(self, event: Checkbox.Changed) -> None:
        kind = "Read" if event.checkbox.id == "compose-read-receipt" else "Delivery"
        event.checkbox.label = self._receipt_label(kind, event.value)

    @on(Checkbox.Changed, "#compose-sent-copy")
    def _sent_copy_changed(self, event: Checkbox.Changed) -> None:
        event.checkbox.label = f"Sent copy: {'on' if event.value else 'off'}"

    def action_request_delivery_receipt(self) -> None:
        if self._guard_running_change():
            return
        control = self.query_one("#compose-delivery-receipt", Checkbox)
        control.value = not control.value

    def action_open_external_editor(self) -> None:
        if self._guard_running_change():
            return
        if isinstance(self.app, GUCmailApp):
            self.app.open_composer_external_editor(self, self._payload(action="external-editor"))

    def action_forward_as_attachment(self) -> None:
        if self._guard_running_change():
            return
        if isinstance(self.app, GUCmailApp):
            self.app.attach_selected_message_to_composer(self)

    def action_schedule_draft(self, *, expression: str = "tomorrow 09:00") -> None:
        if self._guard_running_change():
            return
        if isinstance(self.app, GUCmailApp):
            self.app.schedule_composer_draft(
                self,
                self._payload(action="schedule"),
                expression=expression,
            )

    def apply_external_draft(self, draft: DraftData) -> None:
        if self.defer_status_action(lambda: self.apply_external_draft(draft)):
            return
        if self._guard_running_change():
            return
        self.draft = draft
        self.query_one("#compose-body-mode", Select).value = "plain"
        self.query_one("#compose-body", TextArea).text = draft.body_text
        self._set_note("External editor changes were saved to the recoverable local draft.")

    def _inherited_attachment_text(self) -> str:
        if self.inherited_attachments is None:
            return "Checking original message attachments before send review…"
        if not self.inherited_attachments:
            return "Original message: no inherited attachments."
        rows = [f"Included from original ({len(self.inherited_attachments)}):"]
        rows.extend(
            f"  {sanitize_terminal(item.name)} · {human_bytes(item.size)}"
            for item in self.inherited_attachments
        )
        return "\n".join(rows)

    def show_forward_attachments(self, attachments: tuple[AttachmentInfo, ...]) -> None:
        self.inherited_attachments = attachments
        self.query_one("#compose-inherited", Static).update(self._inherited_attachment_text())
        self.refresh_submission_actions()
        self._schedule_initial_review()

    def _guard_running_change(self) -> bool:
        if self._submission_pending:
            self._set_note("Waiting for this submission's result. Your composition remains here.")
            return True
        if self._attempt_pending:
            self._submission_note("Checking local send status. Wait before changing this draft.")
            return True
        if self._submission_uncertain or self._attempt_blocked:
            self._set_note(
                "This composition is read-only while its send outcome needs review. "
                "Use Send status."
            )
            return True
        if isinstance(self.app, GUCmailApp) and app_write_coordinator(self.app).pending:
            self._set_note(PENDING_CHANGE_MESSAGE + " The composer remains open.")
            return True
        return False

    def action_review_send(self) -> None:
        """Show the same final send review used by the composer button."""

        if self._guard_running_change():
            return
        if self._attempt_pending or self._review_pending:
            return
        if self.inherited_attachments is None:
            self._set_note(
                "Original attachment details are unavailable. "
                "Reopen the draft online before sending."
            )
            return
        payload = self._payload(action="send")
        self._review_pending = True
        self.refresh_submission_actions()
        self._set_note("Checking recipients, message and attachment files…")
        self._validate_send(payload)

    @work(thread=True, exclusive=True, group="compose-review", exit_on_error=False)
    def _validate_send(self, payload: dict[str, Any]) -> None:
        validation = None
        error = ""
        try:
            attempt = self.read_send_attempt()
        except Exception:
            self.app.call_from_thread(self._review_status_unavailable)
            return
        if attempt and attempt.state != "failed":
            self.app.call_from_thread(self._review_blocked_by_attempt, attempt)
            return
        try:
            service = ComposeService()
            if isinstance(self.app, GUCmailApp):
                service = ComposeService(
                    max_attachment_bytes=self.app.context.config.max_attachment_bytes,
                    preferences=ComposerPreferences.from_settings(self.app._settings_service()),
                )
            plan = service.plan_send(
                GUCmailApp._draft_from_payload(payload),
                kind=self.kind,
                source_has_attachments=bool(self.inherited_attachments),
            )
            validation = plan.validation
            payload["confirmation_scope"] = plan.confirmation_scope
            payload["allow_empty_subject"] = service.preferences.allow_empty_subject
        except ComposeValidationError as exc:
            validation = exc.validation
        except Exception:
            error = "Could not validate the message. Check addresses and attachment paths."
        self.app.call_from_thread(self._send_checked, payload, validation, error)

    def _review_blocked_by_attempt(self, attempt: Any) -> None:
        self._review_pending = False
        self._attempt_generation += 1
        self.send_attempt_checked(attempt)

    def _review_status_unavailable(self) -> None:
        self._review_pending = False
        self._attempt_generation += 1
        self.send_attempt_checked(
            None,
            "Local send status could not be checked. "
            "Your input is retained. Use Send status to check again.",
        )

    def _reviewed_content(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self._payload(action="send").items()
            if key != "recovery_sequence"
        }

    def _send_checked(
        self, payload: dict[str, Any], validation: ComposeValidation | None, error: str
    ) -> None:
        if not self.is_mounted:
            return
        self._review_pending = False
        self.refresh_submission_actions()
        reviewed_input = {
            key: value
            for key, value in payload.items()
            if key not in {"confirmation_scope", "allow_empty_subject", "recovery_sequence"}
        }
        if reviewed_input != self._reviewed_content():
            self._set_note("The message changed during validation. Review send again when ready.")
            return
        if error or validation is None:
            self._set_note(error)
            return
        if validation.errors:
            self._set_note("\n".join(issue.message for issue in validation.errors))
            field = validation.errors[0].field or "to"
            target = {"recipients": "to", "attachments": "attachment-add"}.get(field, field)
            matches = self.query(f"#compose-{target}")
            if matches:
                matches.first().focus()
            return
        if self._guard_running_change():
            return
        self._set_note("")
        recipient_count = sum(
            len(values)
            for values in (validation.draft.to, validation.draft.cc, validation.draft.bcc)
        )
        detail = (
            f"Recipients: {recipient_count}\nSubject: "
            f"{sanitize_terminal(payload['subject']) or '(empty)'}\n\n"
            f"Format: {'HTML' if validation.draft.body_html is not None else 'Plain text'}\n"
            f"Importance: {validation.draft.importance.value}\n"
            f"Read receipt: {'requested' if validation.draft.request_read_receipt else 'off'}\n"
            "Delivery receipt: "
            f"{'requested' if validation.draft.request_delivery_receipt else 'off'}\n\n"
            f"Sent Items copy: {'save' if validation.draft.save_sent_copy else 'none'}\n\n"
            "The message cannot be recalled by the local Undo Send feature after Exchange "
            "accepts it."
        )
        for label, values in (
            ("To", validation.draft.to),
            ("Cc", validation.draft.cc),
            ("Bcc", validation.draft.bcc),
        ):
            if values:
                detail += f"\n{label}: " + ", ".join(
                    sanitize_terminal(value.email) for value in values
                )
        local_paths = [
            Path(value).name
            for value in _compose_attachment_paths(str(payload["attachment_paths"]))
        ]
        detail += f"\n\nLocal attachments ({len(local_paths)}): " + (
            ", ".join(sanitize_terminal(name) for name in local_paths) or "none"
        )
        if self.draft.forward_of_reference:
            detail += "\n" + self._inherited_attachment_text()
        if validation.warnings:
            detail += "\n\n" + "\n".join(issue.message for issue in validation.warnings)
        if isinstance(self.app, GUCmailApp):
            delay = int(str(self.app._setting_value("composer.undo_send_seconds", 0)))
            if delay:
                detail += (
                    f"\nLocal Undo Send delay: {delay} seconds. Cancel from Scheduled Mail. "
                    "If you close the app, the queued message needs a running scheduler."
                )

        def confirmed(value: bool | None) -> None:
            self._send_review_open = False
            if value:
                if self._guard_running_change():
                    return
                if reviewed_input != self._reviewed_content():
                    self._set_note(
                        "The message changed after review. Review send again when ready."
                    )
                    return
                self._submit_value(payload)
            elif isinstance(self.app, GUCmailApp):
                self.request_send_status()

        self._send_review_open = True
        self.app.push_screen(
            ConfirmationScreen("Send this message?", detail, confirm_label="Send message"),
            confirmed,
        )

    def action_close(self) -> None:
        """Close safely, preserving and confirming edited content."""

        if self._submission_pending:
            self._set_note(
                "Waiting for the result. Closing cannot cancel an already started write."
            )
            return
        if self._submission_uncertain:

            def close_review(value: bool | None) -> None:
                if value:
                    self.dismiss(None)

            self.app.push_screen(
                ConfirmationScreen(
                    "Close this send review?",
                    (
                        "This draft has a Scheduled Mail entry. Closing does not cancel that job. "
                        "Review Scheduled Mail to edit or cancel it."
                        if self._attempt_state == "queued"
                        else "The prior send may have completed. Closing does not cancel it. "
                        "Your exact recovery copy remains available. Review Send status and "
                        "Sent Items before another send."
                    ),
                    confirm_label="Close review",
                ),
                close_review,
            )
            return
        if not self._has_unsaved_changes():
            self.dismiss(None)
            return
        payload = self._payload(action="close-recovery")
        if not isinstance(self.app, GUCmailApp) or not self.app.recover_composer_before_close(
            payload
        ):
            return

        def confirmed(value: bool | None) -> None:
            if value:
                self.dismiss(None)

        self.app.push_screen(
            ConfirmationScreen(
                "Close composer?",
                "Your unsent changes were saved to local recovery. Close this composer?",
            ),
            confirmed,
        )

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "compose-send-status":
            self.request_send_status(show_details=True)
            return
        if event.button.id == "compose-extra-addresses":
            self._show_extra_addresses(not self.query_one("#compose-cc-row").display)
            return
        if event.button.id == "compose-attachment-add":
            self.action_add_attachment()
            return
        if event.button.id == "compose-options-toggle":
            self.action_toggle_options()
            return
        if event.button.id == "compose-attachment-remove":
            self.action_remove_attachment()
            return
        if event.button.id == "compose-cancel":
            self.action_close()
            return
        if event.button.id == "compose-send":
            self.action_review_send()
            return
        if event.button.id == "compose-external-editor":
            self.action_open_external_editor()
            return
        if event.button.id == "compose-save":
            if self._guard_running_change():
                return
            payload = self._payload(action="save")
            if isinstance(self.app, GUCmailApp):
                self.set_save_preflight_pending(True)
                self._set_note("Checking attachment files…")
                self.app.prepare_draft_save(self, payload)
            else:
                self.dismiss(payload)


class AreaScreen(ModalScreen[None]):
    """Bounded read-only product-area view loaded outside the UI thread."""

    DEFAULT_CSS = """
    AreaScreen { align: center middle; }
    #area-dialog {
        width: 90%; height: 85%; border: thick $accent;
        background: $gucmail-modal-background; padding: 1 2;
    }
    #area-title { text-style: bold; height: 2; }
    #area-scroll { height: 1fr; }
    #area-content { width: 100%; }
    #area-actions { height: 3; align-horizontal: right; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(self, title: str, loader: Callable[[], str]) -> None:
        super().__init__()
        self.area_title = sanitize_terminal(title, max_length=100)
        self.loader = loader
        self._load_generation = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="area-dialog"):
            yield Label(self.area_title, id="area-title", markup=False)
            with VerticalScroll(id="area-scroll"):
                yield Static("Loading…", id="area-content", markup=False)
            with Horizontal(id="area-actions"):
                yield Button("Refresh", id="area-refresh")
                yield Button("Close", id="area-close", variant="primary")

    def on_mount(self) -> None:
        self.call_after_refresh(self._start_load)

    def on_unmount(self) -> None:
        self._load_generation += 1

    def _start_load(self) -> None:
        self._load_generation += 1
        generation = self._load_generation
        if self.is_mounted:
            try:
                self.query_one("#area-content", Static).update("Loading…")
            except NoMatches:
                return
        self.load_content(generation)

    @work(thread=True, exclusive=True, group="area-load")
    def load_content(self, generation: int) -> None:
        try:
            content = self.loader() or "No items in this bounded view."
        except Exception as exc:
            content = _safe_failure(exc)
        self.app.call_from_thread(self._show_content, generation, content)

    def _show_content(self, generation: int, content: str) -> None:
        if generation != self._load_generation or not self.is_mounted:
            return
        try:
            self.query_one("#area-content", Static).update(
                sanitize_terminal(content, max_length=1_000_000)
            )
        except NoMatches:
            return

    def _close(self) -> None:
        self._load_generation += 1
        self.dismiss(None)

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "area-refresh":
            self._start_load()
        elif event.button.id == "area-close":
            self._close()

    def action_refresh(self) -> None:
        self._start_load()

    def action_close(self) -> None:
        self._close()


class MailMessageTable(DataTable[str]):
    class MoreRequested(Message):
        """The user tried to move beyond the currently loaded messages."""

    def action_cursor_down(self) -> None:
        if self.row_count and self.cursor_row == self.row_count - 1:
            self.post_message(self.MoreRequested())
        super().action_cursor_down()

    def action_page_down(self) -> None:
        if self.row_count and self.cursor_row == self.row_count - 1:
            self.post_message(self.MoreRequested())
        super().action_page_down()

    # A row-cursor DataTable maps Home/End to horizontal column scrolling. In the
    # message list the documented meaning is first/last message, so route them to
    # the vertical cursor actions. End on the last loaded row asks for the next
    # bounded page, exactly like Down and Page Down.
    def action_scroll_home(self) -> None:
        super().action_scroll_top()

    def action_scroll_end(self) -> None:
        if self.row_count and self.cursor_row == self.row_count - 1:
            self.post_message(self.MoreRequested())
        super().action_scroll_bottom()


class GUCmailApp(App[None]):
    TITLE = "GUC Mail CLI"
    SUB_TITLE = ""
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen {
        layout: vertical;
        layers: default overlay;
        background: $gucmail-app-background;
        color: $gucmail-primary-text;
    }
    #mail-layout { height: 1fr; width: 100%; layout: horizontal; }
    #navigation {
        width: 24;
        min-width: 16;
        max-width: 40;
        height: 1fr;
        background: $gucmail-sidebar-background;
        border-right: solid $gucmail-divider;
    }
    #folder-tree {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-color: $gucmail-divider;
        scrollbar-background: $gucmail-sidebar-background;
    }
    #expanded-brand {
        width: 100%; height: auto; max-height: 6; padding: 0 1;
        background: $gucmail-header; color: $foreground;
    }
    #reader-layout { width: 1fr; height: 1fr; layout: horizontal; }
    #messages {
        width: 40%; min-width: 24; height: 1fr;
        background: $gucmail-message-list-background;
    }
    #message-filter {
        height: 1; margin: 0 1; padding: 0 1; border: none;
        background: $gucmail-secondary-background;
    }
    #message-filter > .input--placeholder { color: $text-muted; }
    #message-filter:focus { background: $gucmail-header; }
    #message-table { width: 100%; height: 1fr; min-height: 1; }
    #pane-divider {
        width: 1;
        height: 1fr;
        background: $gucmail-divider;
    }
    #pane-divider:hover { background: $accent; }
    #preview-scroll {
        width: 1fr; height: 1fr; padding: 1 2;
        background: $gucmail-reading-pane-background;
    }
    #preview { width: 100%; }
    #message-heading { height: 1; padding: 0 1; text-style: bold; color: $text; }
    #message-table > .datatable--header { background: $gucmail-secondary-background; }
    #status-bar { height: 1; padding: 0 1; background: $gucmail-footer; }
    #status-line { height: 1; width: 100%; background: $gucmail-footer; }
    #status-line #status-bar { width: 1fr; }
    #status-line #palette-launcher {
        width: auto; min-width: 3; height: 1; padding: 0 1;
        text-style: bold; color: $accent;
    }
    .medium #preview-scroll, .medium #pane-divider,
    .narrow #preview-scroll, .narrow #pane-divider { display: none; }
    .medium #messages, .narrow #messages { width: 1fr; min-width: 1; }
    .medium #navigation { min-width: 26; }
    .narrow #navigation { display: none; }
    .narrow.sidebar-visible #navigation { display: block; width: 22; }
    .sidebar-hidden #navigation { display: none; }
    .preview-hidden #preview-scroll, .preview-hidden #pane-divider { display: none; }
    .preview-hidden #messages { width: 1fr; }
    .split-horizontal #reader-layout { layout: vertical; }
    .split-horizontal #messages { width: 1fr; min-width: 1; }
    .split-horizontal #pane-divider { width: 1fr; height: 1; }
    .split-horizontal #preview-scroll { width: 1fr; min-height: 5; }
    .dialog-title { text-style: bold; margin-bottom: 1; }
    #restore-status-root { background: $gucmail-modal-background; }
    #restore-status-heading, #restore-plan-heading { border-bottom: solid $gucmail-divider; }
    #restore-status-brand, #restore-plan-brand { color: $gucmail-logo-primary; }
    #restore-status-actions, #restore-plan-actions, #restore-plan-detail {
        border-top: solid $gucmail-divider;
    }
    """
    CSS += FRONTEND_CSS
    ENABLE_COMMAND_PALETTE = False

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("/", "search", "Search"),
        Binding("c", "compose", "Compose"),
        Binding("r", "reply", "Reply", show=False),
        Binding("a", "reply_all", "Reply all", show=False),
        Binding("f", "forward", "Forward", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("u", "toggle_read", "Read/unread", show=False),
        Binding("space", "toggle_message_selection", "Select", show=False),
        Binding("escape", "navigate_back", "Back", show=False),
        Binding("backspace", "navigate_back", "Back", show=False),
        Binding("f5", "refresh", "Refresh"),
        Binding("ctrl+f5", "reconnect", "Reconnect", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Folders", show=False),
        Binding("alt+left", "preview_narrower", "Narrow preview", show=False),
        Binding("alt+right", "preview_wider", "Widen preview", show=False),
        Binding("alt+0", "reset_layout", "Reset layout", show=False),
        Binding("ctrl+backslash", "toggle_preview", "Preview", show=False),
        Binding("shift+enter", "detach_reader", "Floating reader", show=False),
    ]

    def __init__(
        self,
        context: AppContext | None = None,
        *,
        gateway: Any | None = None,
        offline: bool = False,
    ) -> None:
        super().__init__()
        self.context = context or build_context()
        app_write_coordinator(self)
        self._composer_recovery_lock = Lock()
        self._composer_recovery_sequences: dict[str, int] = {}
        self._theme_change_pending = Event()
        self._branding_change_pending = Event()
        self._finished_compositions: set[str] = set()
        self.custom_theme_repository = CustomThemeRepository(
            self.context.paths.data_dir / "custom-themes.json"
        )
        custom_theme_state = self.custom_theme_repository.load()
        self.theme_registry = ThemeRegistry(custom_theme_state.themes)
        self._theme_fallback_from: str | None = None
        selected_theme = self._resolve_config_theme(self.context.config.theme)
        self.context.config = replace(self.context.config, theme=selected_theme.id)
        self.theme_session = ThemeSession(
            self.theme_registry,
            self._load_theme_preferences(selected_theme.id),
            persist=self._persist_theme_preferences,
        )
        self.theme_browser_backend = ThemeBrowserBackend(
            self.custom_theme_repository,
            registry=self.theme_registry,
            session=self.theme_session,
            on_preview=self._activate_theme_callback,
            on_apply=self._activate_theme_callback,
            on_cancel=self._activate_theme_callback,
        )
        self._active_theme = selected_theme
        self._activate_theme(selected_theme)
        self.branding_preferences = self._load_branding_preferences()
        self._palette_bound_shortcuts: set[str] = set()
        self._load_shortcut_preferences()
        self._palette_launcher_style = str(self._setting_value("palette.launcher_style", "hidden"))
        self._palette_launcher_symbol = str(self._setting_value("palette.launcher_symbol", "⌘"))
        self._palette_launcher_position = str(
            self._setting_value("palette.launcher_position", "top_left")
        )
        self.branding_browser_backend = BrandingBrowserBackend(
            lambda: self.active_theme,
            preferences=self.branding_preferences,
            persist_preferences=self._persist_branding_preferences,
            on_preview=self._branding_preferences_changed,
            on_apply=self._branding_preferences_changed,
            on_cancel=self._branding_preferences_changed,
        )
        self.gateway = gateway
        self._gateway_injected = gateway is not None
        self.offline = offline
        configured_default = self._setting_value(
            "mail.default_folder", self.context.config.default_folder
        )
        default_folder = (
            configured_default.strip()
            if isinstance(configured_default, str) and configured_default.strip()
            else self.context.config.default_folder
        )
        saved_folder = self._setting_value_with_legacy(
            "mail.last_folder",
            default_folder,
            legacy_key="mail.last_folder",
            use_fallback_when_default=True,
        )
        initial_folder = (
            saved_folder.strip()
            if isinstance(saved_folder, str) and saved_folder.strip()
            else default_folder
        )
        self.ui = UIState(folder=initial_folder)
        self._session_store = MailSessionStore(self.context.state)
        self._restored_session = self._session_store.load()
        self._pending_session_position = self._restored_session
        self._session_position_scheduled = False
        self._session_snapshot: MailSession | None = None
        self._session_capture_timer: Timer | None = None
        self._last_search_scope = (
            self._restored_session.search_scope if self._restored_session else "all"
        )
        self._last_mail_search_scope = (
            self._restored_session.mail_search_scope if self._restored_session else None
        )
        self._last_mail_search_folder = (
            self._restored_session.mail_search_folder if self._restored_session else None
        )
        self._mail_search_default_scope = (
            self._restored_session.search_default_scope if self._restored_session else None
        )
        self._folder_location_history: list[str] = [initial_folder]
        stored_recent_folders = self._setting_value("palette.recent_folders", ())
        self._recent_folder_paths = (
            [
                value.strip()
                for value in stored_recent_folders
                if isinstance(value, str) and value.strip()
            ]
            if isinstance(stored_recent_folders, (list, tuple))
            else []
        )
        if initial_folder not in self._recent_folder_paths:
            self._recent_folder_paths.insert(0, initial_folder)
        self._recent_folder_paths = list(dict.fromkeys(self._recent_folder_paths))[:20]
        self._mailbox_quota_percent: float | None = None
        self._messages: tuple[Any, ...] = ()
        self._message_page_rows: tuple[Any, ...] = ()
        self._visible_messages: tuple[Any, ...] = ()
        self._selected_message_references: set[str] = set()
        self._message_detail: Any | None = None
        self._message_detail_reference: str | None = None
        self._mailbox_generation = 0
        self._message_generation = 0
        self._folder_generation = 0
        self._mailbox_timeout: Timer | None = None
        self._viewport_fill_timer: Timer | None = None
        self._loaded_mailbox_limit = 0
        self._loaded_mailbox_folder: str | None = None
        self._mailbox_next_offset = 0
        self._mailbox_has_more = False
        self._fill_message_viewport = bool(self._setting_value("mail.fill_viewport", True))
        self._message_timeout: Timer | None = None
        self._automatic_read_timer: Timer | None = None
        self._floating_reader_mounts: dict[FloatingReader, Awaitable[Any]] = {}
        self._automatic_read_request: _AutomaticReadRequest | None = None
        self._automatic_read_attempted_generation = -1
        self._automatic_read_inflight: set[str] = set()
        self._load_timeout_seconds = max(
            5.0,
            min(float(self.context.config.request_timeout_seconds) + 2.0, 300.0),
        )
        self._message_ratio = self._configured_message_ratio()
        orientation = str(
            self._setting_value_with_legacy(
                "layout.split_orientation",
                "vertical",
                legacy_key="layout.split_orientation",
            )
        )
        self._split_orientation = (
            orientation if orientation in {"vertical", "horizontal"} else "vertical"
        )
        self._preview_visible = bool(
            self._setting_value_with_legacy(
                "layout.preview_visible",
                True,
                legacy_key="layout.preview_visible",
            )
        )
        self._sidebar_visible = bool(self._setting_value("layout.sidebar_visible", True))
        self._sidebar_ratio = self._validated_sidebar_ratio(
            self._setting_value("layout.sidebar_ratio", 0.22)
        )
        self._status_bar_visible = bool(self._setting_value("layout.status_bar_visible", True))
        self._header_contents = parse_shell_fields(
            self._setting_value("layout.header_contents", HEADER_DEFAULT)
        )
        self._header_compactness = str(self._setting_value("layout.header_compactness", "adaptive"))
        self._status_bar_contents = parse_shell_fields(
            self._setting_value("layout.status_bar_contents", STATUS_DEFAULT), status=True
        )
        self._header_connection = "Account offline" if self.offline else "Account not connected"
        self._header_account_detail = self._header_connection
        self._header_quota_label = "Storage pending"
        self._status_activity = f"Ready · ? Help · {self._shortcut_label()} Command palette"
        self._message_columns = self._load_message_columns()
        self._reader_mode = str(self._setting_value("layout.reader_mode", "preview"))
        self._mail_sort_order = MailSortPreferences.from_settings(self._settings_service()).order_by
        self._group_conversations = bool(self._setting_value("mail.group_conversations", False))
        self._layout_last_preset = str(self._setting_value("layout.last_preset", "default"))
        expanded = self._setting_value_with_legacy(
            "mail.folder_expansion",
            (),
            legacy_key="folders.expanded",
        )
        self._expanded_folders = (
            {str(reference) for reference in expanded if isinstance(reference, str)}
            if isinstance(expanded, (list, tuple))
            else set()
        )
        self._folder_nodes: dict[str, Any] = {}
        self._folder_paths: dict[str, str] = {}
        self._folder_load_tokens: dict[Any, int] = {}
        self._folder_load_sequence = 0
        self._folder_root_node_id: Any | None = None
        self._folder_expand_all_remaining = 0
        self._folder_selection_to_restore: str | None = None
        self._folder_refresh_preserve_expanded: set[str] = set()
        self._folder_refresh_expanded_snapshot: set[str] = set()
        self._folder_refresh_pending_expanded: set[str] = set()
        self._folder_refresh_in_progress = False
        self._folder_refresh_generation = 0
        self._pending_reader_mode: str | None = None
        self._current_search_text = ""

    def compose(self) -> ComposeResult:
        launcher_label = self._palette_launcher_label()
        top_launcher = (
            launcher_label if self._palette_launcher_position.startswith("top_") else None
        )
        yield CompactHeader(
            f"Account {'offline' if self.offline else 'not connected'} · "
            f"Folder {sanitize_terminal(self.ui.folder, max_length=80)} · "
            f"{self._watcher_header_label()} · Storage pending",
            status_id="account-bar",
            classes="monochrome-brand" if self._monochrome_output() else None,
            branding=self._slim_branding(self.active_theme),
            launcher_label=top_launcher,
            launcher_first=self._palette_launcher_position == "top_left",
        )
        yield Static("", id="expanded-brand", markup=False)
        with Horizontal(id="workspace-tabs"):
            for label in ("Mail", "Calendar", "Contacts", "Tasks", "Settings"):
                yield Button(
                    label,
                    id=f"workspace-{label.lower()}",
                    classes="active" if label == "Mail" else "",
                )
        with Horizontal(id="mail-layout"):
            with Vertical(id="navigation"):
                yield Tree("Folders", id="folder-tree")
            with Horizontal(id="reader-layout"):
                with Vertical(id="messages"):
                    yield Static("Inbox", id="message-heading", markup=False)
                    yield Input(placeholder="Filter this view…", id="message-filter")
                    yield MailMessageTable(
                        id="message-table", cursor_type="row", zebra_stripes=False
                    )
                yield PaneDivider(id="pane-divider")
                with MessagePreviewScroll(id="preview-scroll"):
                    yield Static(
                        "Select a message\n\nPress Enter to read it.\n\n"
                        "C  Compose\n/  Search\nCtrl+K  Commands",
                        id="preview",
                        markup=False,
                    )
        with Horizontal(id="status-line"):
            bottom_launcher = (
                launcher_label if self._palette_launcher_position.startswith("bottom_") else None
            )
            if bottom_launcher and self._palette_launcher_position == "bottom_left":
                yield Static(bottom_launcher, id="palette-launcher", markup=False)
            yield Static(
                self._status_activity,
                id="status-bar",
                markup=False,
            )
            if bottom_launcher and self._palette_launcher_position == "bottom_right":
                yield Static(bottom_launcher, id="palette-launcher", markup=False)
        yield Footer()

    @property
    def active_theme(self) -> ThemeDefinition:
        """The selected semantic definition, before terminal/accessibility adaptation."""

        return self._active_theme

    @property
    def runtime_theme(self) -> ThemeDefinition:
        """The effective palette used to render mounted widgets and Rich spans."""
        return self._runtime_theme

    @property
    def theme_fallback_used(self) -> bool:
        """Whether startup replaced an unknown configured theme with the safe default."""

        return self._theme_fallback_from is not None

    def _resolve_config_theme(self, configured: str) -> ThemeDefinition:
        normalized = normalize_theme_identifier(configured)
        try:
            selected = self.theme_registry.get(normalized)
        except ThemeValidationError:
            self._theme_fallback_from = normalized
            return self.theme_registry.get(ThemePreferences().selected_theme_id)
        self._theme_fallback_from = None
        return selected

    def _load_theme_preferences(self, selected_theme_id: str) -> ThemePreferences:
        try:
            settings = self.context.config_store.load_document().settings
        except Exception:
            settings = {}
        existing = {theme.id for theme in self.theme_registry.themes}

        def identifiers(key: str, maximum: int) -> tuple[str, ...]:
            candidates = settings.get(key, [])
            if not isinstance(candidates, (list, tuple)):
                return ()
            result: list[str] = []
            for candidate in candidates:
                if not isinstance(candidate, str):
                    continue
                normalized = normalize_theme_identifier(candidate)
                if normalized in existing and normalized not in result:
                    result.append(normalized)
                if len(result) >= maximum:
                    break
            return tuple(result)

        def flag(key: str) -> bool:
            value = settings.get(key, False)
            return value if isinstance(value, bool) else False

        return ThemePreferences(
            selected_theme_id=selected_theme_id,
            favorites=identifiers("appearance.theme_favorites", 100),
            recently_used=identifiers("appearance.recent_themes", 12),
            policy=ThemePolicy.EXPLICIT,
            high_contrast=flag("accessibility.high_contrast"),
            monochrome=self.context.config.no_color,
        )

    def _persist_theme_preferences(self, preferences: ThemePreferences) -> None:
        document = self.context.config_store.load_document()
        settings = dict(document.settings)
        settings["appearance.theme_favorites"] = list(preferences.favorites)
        settings["appearance.recent_themes"] = list(preferences.recently_used)
        settings["accessibility.high_contrast"] = preferences.high_contrast
        stored_config = document.stored_config
        if document.value_sources.get("theme") != "environment":
            stored_config = replace(
                stored_config,
                theme=preferences.selected_theme_id,
            ).validate()
        self.context.config_store.save_document(
            stored_config,
            settings,
            expected_revision=document.revision,
        )
        self.context.config = replace(
            self.context.config,
            theme=preferences.selected_theme_id,
        ).validate()

    def _settings_service(self) -> SettingsService:
        return SettingsService(
            self.context.config_store,
            context=SettingsContext(
                account_id=self.context.config.email,
                machine_id=f"{sys.platform}:{platform.node()}",
            ),
        )

    def _setting_value(self, setting_id: str, fallback: object) -> object:
        try:
            return self._settings_service().value(setting_id).effective_value
        except (ConfigurationError, KeyError, ValueError):
            return fallback

    def _load_message_columns(self) -> tuple[MessageColumn, ...]:
        try:
            return parse_columns(self._setting_value("layout.message_columns", columns_value()))
        except ConfigurationError:
            return DEFAULT_COLUMNS

    def _apply_columns_preview(self, columns: tuple[MessageColumn, ...]) -> None:
        self._message_columns = columns
        if self.is_running:
            with contextlib.suppress(NoMatches):
                self._render_message_rows(self._message_page_rows)

    def _preview_message_columns(self, value: Any) -> Callable[[], None]:
        columns = parse_columns(value)

        def apply() -> tuple[MessageColumn, ...]:
            previous = self._message_columns
            self._apply_columns_preview(columns)
            return previous

        try:
            previous = self.call_from_thread(apply)
        except RuntimeError:
            previous = apply()

        def revert() -> None:
            if not self.is_running:
                self._message_columns = previous
                return
            try:
                self.call_from_thread(self._apply_columns_preview, previous)
            except RuntimeError:
                self._apply_columns_preview(previous)

        return revert

    def _preview_settings_theme(self, value: Any) -> Callable[[], None]:
        """Preview on the UI thread and retain the exact prior preview state."""

        def apply() -> tuple[str, bool]:
            previous = (self.theme_session.current.id, self.theme_session.is_previewing)
            self.preview_theme(str(value))
            return previous

        previous = self.call_from_thread(apply) if get_ident() != self._thread_id else apply()

        def restore() -> None:
            if previous[1]:
                self.preview_theme(previous[0])
            else:
                self.cancel_theme_preview()

        def revert() -> None:
            if self.is_running and get_ident() != self._thread_id:
                self.call_from_thread(restore)
            else:
                restore()

        return revert

    @on(ColumnPreviewRequested)
    def _inspect_column_preview(self, event: ColumnPreviewRequested) -> None:
        event.stop()
        settings = event.settings

        def reviewed(choice: str | None) -> None:
            if not settings.is_attached:
                return
            if choice == "apply":
                settings.action_apply()
            elif choice == "cancel":
                settings.action_cancel_changes()

        self.push_screen(
            MessageColumnsPreviewScreen(
                self.query_one("#message-table", DataTable), self.ui.selected_reference
            ),
            reviewed,
        )

    def _setting_value_with_legacy(
        self,
        setting_id: str,
        fallback: object,
        *,
        legacy_key: str,
        use_fallback_when_default: bool = False,
    ) -> object:
        """Read a registered setting, falling back to the former SQLite key once."""

        try:
            effective = self._settings_service().value(setting_id)
        except (ConfigurationError, KeyError, ValueError):
            return fallback
        if effective.source.value != "default":
            return effective.effective_value
        marker = object()
        legacy = self._read_ui_preference(legacy_key, marker)
        if legacy is not marker:
            return legacy
        return fallback if use_fallback_when_default else effective.effective_value

    def _configured_message_ratio(self) -> float:
        """Resolve both registered pane ratios, preserving the old single-ratio value."""

        service = self._settings_service()
        try:
            message = service.value("layout.message_list_ratio")
            preview = service.value("layout.preview_ratio")
        except (ConfigurationError, KeyError, ValueError):
            return 0.40
        if message.source.value == "default" and preview.source.value == "default":
            marker = object()
            legacy = self._read_ui_preference("layout.message_ratio", marker)
            if legacy is not marker:
                return self._validated_ratio(legacy, default=0.40)
        try:
            message_ratio = float(message.effective_value)
            preview_ratio = float(preview.effective_value)
        except (TypeError, ValueError):
            return 0.40
        total = message_ratio + preview_ratio
        if not math.isfinite(total) or total <= 0:
            return 0.40
        return self._validated_ratio(message_ratio / total, default=0.40)

    def _persist_setting_values(self, values: Mapping[str, object], *, reason: str) -> bool:
        """Persist runtime-owned preferences through the canonical Settings service."""

        try:
            session = self._settings_service().begin()
            for setting_id, value in values.items():
                session.set(setting_id, value)
            session.apply(reason=reason)
            return True
        except (ConfigurationError, KeyError, OSError, ValueError):
            # A policy/environment override or unwritable config must not make
            # the interactive mail surface unusable. The in-memory change is
            # still valid for this session and Settings reports the true source.
            if self.is_running:
                self.notify(
                    "This change applies for this session but could not be saved. "
                    "Review Settings for a policy, environment override, "
                    "or unwritable configuration.",
                    title="Preference not saved",
                    severity="warning",
                    timeout=10,
                )
            return False

    def _persist_folder_expansion(self) -> None:
        self._persist_setting_values(
            {"mail.folder_expansion": sorted(self._expanded_folders)},
            reason="Folder expansion updated",
        )

    def _persist_message_layout(self, *, reason: str = "Mail layout updated") -> bool:
        return self._persist_setting_values(
            {
                "layout.message_list_ratio": self._message_ratio,
                "layout.preview_ratio": 1.0 - self._message_ratio,
                "layout.split_orientation": self._split_orientation,
                "layout.preview_visible": self._preview_visible,
                "layout.sidebar_visible": self._sidebar_visible,
                "layout.sidebar_ratio": self._sidebar_ratio,
                "layout.status_bar_visible": self._status_bar_visible,
                "layout.last_preset": self._layout_last_preset,
            },
            reason=reason,
        )

    def _settings_applied_callback(self, setting_ids: tuple[str, ...]) -> None:
        """Marshal the Settings worker notification back onto Textual's UI thread."""

        if not self.is_running:
            self._apply_committed_settings(setting_ids)
            return
        try:
            self.call_from_thread(self._apply_committed_settings, setting_ids)
        except RuntimeError:
            # Direct backend calls in tests may already run on the UI thread.
            self.call_later(self._apply_committed_settings, setting_ids)

    def _apply_committed_settings(self, setting_ids: tuple[str, ...]) -> None:
        """Reload committed values and apply every mounted shell consumer immediately."""

        changed = set(setting_ids)
        service = self._settings_service()
        try:
            self.context.config = service.materialize_config()
        except ConfigurationError:
            return

        if changed & {
            "layout.message_list_ratio",
            "layout.preview_ratio",
        }:
            self._message_ratio = self._configured_message_ratio()
        if "layout.split_orientation" in changed:
            orientation = str(self._setting_value("layout.split_orientation", "vertical"))
            self._split_orientation = (
                orientation if orientation in {"vertical", "horizontal"} else "vertical"
            )
        if "layout.preview_visible" in changed:
            self._preview_visible = bool(self._setting_value("layout.preview_visible", True))
        if "layout.sidebar_visible" in changed:
            self._sidebar_visible = bool(self._setting_value("layout.sidebar_visible", True))
            if self.has_class("narrow"):
                self.set_class(self._sidebar_visible, "sidebar-visible")
        if "layout.sidebar_ratio" in changed:
            self._sidebar_ratio = self._validated_sidebar_ratio(
                self._setting_value("layout.sidebar_ratio", 0.22)
            )
        if "layout.status_bar_visible" in changed:
            self._status_bar_visible = bool(self._setting_value("layout.status_bar_visible", True))
        if "layout.header_contents" in changed:
            self._header_contents = parse_shell_fields(
                self._setting_value("layout.header_contents", HEADER_DEFAULT)
            )
        if "layout.header_compactness" in changed:
            self._header_compactness = str(
                self._setting_value("layout.header_compactness", "adaptive")
            )
        if "layout.status_bar_contents" in changed:
            self._status_bar_contents = parse_shell_fields(
                self._setting_value("layout.status_bar_contents", STATUS_DEFAULT), status=True
            )
        if changed & {
            "layout.header_contents",
            "layout.header_compactness",
            "layout.status_bar_contents",
        }:
            self._refresh_shell_status()
        if "layout.reader_mode" in changed:
            self._reader_mode = str(self._setting_value("layout.reader_mode", "preview"))
        if "layout.last_preset" in changed:
            self._layout_last_preset = str(self._setting_value("layout.last_preset", "default"))
        if "layout.message_columns" in changed:
            self._message_columns = self._load_message_columns()
            if self.is_running:
                self._render_message_rows(self._message_page_rows)

        if changed & {"mail.mark_read_on_open", "mail.mark_read_delay_seconds"}:
            # Apply takes effect for the next opening; an existing countdown cannot
            # silently inherit a shorter delay or survive disabling the preference.
            self._cancel_automatic_read()

        if "mail.fill_viewport" in changed:
            self._fill_message_viewport = bool(self._setting_value("mail.fill_viewport", True))
            if self._fill_message_viewport:
                self.call_after_refresh(self._schedule_viewport_fill)

        if changed & {"mail.sort_field", "mail.sort_direction"}:
            self._mail_sort_order = MailSortPreferences.from_settings(service).order_by
            if self.is_running and not self.offline:
                self.call_after_refresh(self.refresh_mailbox)

        if "mail.group_conversations" in changed:
            self._group_conversations = bool(
                service.value("mail.group_conversations").effective_value
            )
            if self.is_running:
                self._render_message_rows(self._message_page_rows)

        if "mail.attachment_collision" in changed:
            preference = AttachmentPreferences.from_settings(service)
            for screen in self.screen_stack:
                if isinstance(screen, DraftAttachmentScreen):
                    screen.update_collision_default(preference.collision)

        if "mail.last_folder" in changed:
            folder = self._setting_value("mail.last_folder", self.context.config.default_folder)
            if isinstance(folder, str) and folder.strip():
                self.ui.folder = folder.strip()
        if "mail.folder_expansion" in changed:
            expanded = self._setting_value("mail.folder_expansion", ())
            self._expanded_folders = (
                {str(reference) for reference in expanded if isinstance(reference, str)}
                if isinstance(expanded, (list, tuple))
                else set()
            )

        if changed & {"help.favorites", "help.recent_pages"}:
            current_help = self.context.state.load_help_state()
            favorites = self._setting_value("help.favorites", ())
            recent_pages = self._setting_value("help.recent_pages", ())
            self.context.state.save_help_state(
                HelpUserState(
                    recent_page_ids=(
                        tuple(str(value) for value in recent_pages)
                        if isinstance(recent_pages, (list, tuple))
                        else ()
                    ),
                    favorite_page_ids=(
                        frozenset(str(value) for value in favorites)
                        if isinstance(favorites, (list, tuple))
                        else frozenset()
                    ),
                    recent_queries=current_help.recent_queries,
                )
            )

        if changed & (SHORTCUT_DEFAULTS.keys() | {KEYBOARD_SETTING}):
            self._load_shortcut_preferences()
            self._bind_palette_shortcuts()
            self._bind_theme_shortcuts()
            self._apply_keyboard_bindings()

        if changed & {
            "appearance.theme",
            "accessibility.high_contrast",
            "accessibility.monochrome",
        }:
            self.theme_session.preferences = replace(
                self.theme_session.preferences,
                high_contrast=bool(self._setting_value("accessibility.high_contrast", False)),
                monochrome=self.context.config.no_color,
            )
            if "appearance.theme" in changed:
                selected = self._resolve_config_theme(self.context.config.theme)
                self.theme_session.preferences = replace(
                    self.theme_session.preferences,
                    selected_theme_id=selected.id,
                )
                self.theme_session.cancel()
            else:
                selected = self.theme_session.current
            self._active_theme = selected
            self._activate_theme(selected)
        if changed & {"appearance.density", "appearance.border_style"} and self.is_running:
            self._apply_theme_structure(self._runtime_theme)
            with contextlib.suppress(NoMatches):
                self._render_message_rows(self._message_page_rows)
            self.call_after_refresh(self._schedule_viewport_fill)
        if changed & {
            "appearance.branding",
            "appearance.custom_branding",
            "appearance.banner_visible",
            "appearance.banner_alignment",
            "appearance.branding_size",
            "appearance.branding_color_behavior",
            "appearance.branding_width",
            "appearance.branding_height",
        }:
            self.branding_preferences = self._load_branding_preferences()
            self.branding_browser_backend = BrandingBrowserBackend(
                lambda: self.active_theme,
                preferences=self.branding_preferences,
                persist_preferences=self._persist_branding_preferences,
                on_preview=self._branding_preferences_changed,
                on_apply=self._branding_preferences_changed,
                on_cancel=self._branding_preferences_changed,
            )
            self._render_expanded_branding()

        if changed & {
            "layout.message_list_ratio",
            "layout.preview_ratio",
            "layout.split_orientation",
            "layout.preview_visible",
            "layout.sidebar_visible",
            "layout.sidebar_ratio",
            "layout.status_bar_visible",
        }:
            self._apply_layout()
        if changed & {"mail.last_folder", "mail.folder_expansion"} and self.is_running:
            self.call_after_refresh(self.action_refresh)
        if changed & {"calendar.timezone", "calendar.week_start"}:
            for screen in self.screen_stack:
                if isinstance(screen, CalendarScreen):
                    try:
                        screen.refresh_display_preferences()
                    except ConfigurationError as exc:
                        screen._show_error("Calendar preferences", exc)
        if changed & set(CALENDAR_OPERATION_SETTINGS):
            for screen in self.screen_stack:
                if isinstance(screen, CalendarScreen):
                    try:
                        screen.refresh_operation_preferences()
                    except ConfigurationError as exc:
                        screen._show_error("Calendar preferences", exc)
        if changed & set(CONTACT_DISPLAY_SETTINGS):
            for screen in self.screen_stack:
                if isinstance(screen, ContactsScreen):
                    try:
                        screen.refresh_display_preferences()
                    except ConfigurationError as exc:
                        screen._show_error("Contacts preferences", exc)
        if changed & set(TASK_DISPLAY_SETTINGS):
            for screen in self.screen_stack:
                if isinstance(screen, TasksScreen):
                    try:
                        screen.refresh_display_preferences()
                    except ConfigurationError as exc:
                        screen._show_error("Task preferences", exc)

    def _palette_launcher_label(self) -> str | None:
        labels = {
            "circle": "●",
            "hamburger": "☰",
            "search": "⌕",
            "text": "Commands",
            "custom": sanitize_terminal(self._palette_launcher_symbol, max_length=8) or "⌘",
        }
        return labels.get(self._palette_launcher_style)

    def _shortcut_label(self) -> str:
        shortcut = sanitize_terminal(self._palette_primary_shortcut, max_length=40)
        return shortcut.replace("ctrl", "Ctrl").replace("alt", "Alt").replace("+", "+")

    def _load_shortcut_preferences(self) -> None:
        self._shortcut_notice = ""
        self._binding_overrides: dict[str, str] = {}
        try:
            service = self._settings_service()
            values = {
                value.spec.setting_id: value.effective_value
                for value in service.snapshot(KEYBOARD_SETTING_IDS).values
            }
            keys = validate_shortcuts(
                {identifier: values[identifier] for identifier in SHORTCUT_DEFAULTS}
            )
            self._binding_overrides = validate_binding_configuration(values[KEYBOARD_SETTING], keys)
        except ConfigurationError as exc:
            keys = dict(SHORTCUT_DEFAULTS)
            self._shortcut_notice = (
                f"{exc} Global defaults are active; repair shortcuts in Settings. "
                "General action defaults are also restored."
            )
        self._palette_primary_shortcut = keys["palette.primary_shortcut"]
        self._palette_secondary_shortcut = keys["palette.secondary_shortcut"]
        self._previous_theme_shortcut = keys["appearance.previous_theme_shortcut"]
        self._next_theme_shortcut = keys["appearance.next_theme_shortcut"]

    def _current_shortcuts(self) -> dict[str, str]:
        return {
            "palette.primary_shortcut": self._palette_primary_shortcut,
            "palette.secondary_shortcut": self._palette_secondary_shortcut,
            "appearance.previous_theme_shortcut": self._previous_theme_shortcut,
            "appearance.next_theme_shortcut": self._next_theme_shortcut,
        }

    def _apply_keyboard_bindings(self) -> None:
        apply_keyboard_bindings(self, self._binding_overrides)
        for screen in self.screen_stack:
            apply_keyboard_bindings(screen, self._binding_overrides)
        self.refresh_bindings()

    @overload
    def push_screen(
        self,
        screen: Screen[_ShortcutScreenResult] | str,
        callback: ScreenResultCallbackType[_ShortcutScreenResult] | None = None,
        wait_for_dismiss: Literal[False] = False,
    ) -> AwaitMount: ...

    @overload
    def push_screen(
        self,
        screen: Screen[_ShortcutScreenResult] | str,
        callback: ScreenResultCallbackType[_ShortcutScreenResult] | None = None,
        wait_for_dismiss: Literal[True] = True,
    ) -> asyncio.Future[_ShortcutScreenResult]: ...

    def push_screen(
        self,
        screen: Screen[Any] | str,
        callback: ScreenResultCallbackType[Any] | None = None,
        wait_for_dismiss: bool = False,
    ) -> AwaitMount | asyncio.Future[Any]:
        selected_screen = self.get_screen(screen)
        selected_screen.add_class("frontend")
        if hasattr(self, "_binding_overrides"):
            apply_keyboard_bindings(selected_screen, self._binding_overrides)
        if wait_for_dismiss:
            return super().push_screen(screen, callback, True)
        return super().push_screen(screen, callback, False)

    def install_screen(self, screen: Screen[Any], name: str) -> None:
        if hasattr(self, "_binding_overrides"):
            apply_keyboard_bindings(screen, self._binding_overrides)
        super().install_screen(screen, name)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Root action remaps remain mailbox actions rather than acquiring authority in
        # unrelated non-modal workspaces or focused editors through event bubbling.
        from gucmail.services.keyboard_bindings import keyboard_catalog

        remapped_actions = {
            row.action
            for row in keyboard_catalog()[0]
            if row.context == f"{type(self).__module__}.{type(self).__name__}"
            and row.scope == "Mail"
            and row.binding_id in getattr(self, "_binding_overrides", {})
        }
        if (
            action in remapped_actions
            and self.screen_stack
            and self.screen is not self.screen_stack[0]
        ):
            return False
        return super().check_action(action, parameters)

    @staticmethod
    def _valid_shortcut(shortcut: str) -> bool:
        try:
            return bool(normalize_shortcut(shortcut))
        except ConfigurationError:
            return False

    def _bind_palette_shortcuts(self) -> None:
        bindings = getattr(self, "_bindings", None)
        key_map = getattr(bindings, "key_to_bindings", None)
        if isinstance(key_map, dict):
            for shortcut in self._palette_bound_shortcuts:
                retained = [
                    binding
                    for binding in key_map.get(shortcut, ())
                    if getattr(binding, "action", None) != "palette"
                ]
                if retained:
                    key_map[shortcut] = retained
                else:
                    key_map.pop(shortcut, None)
        self._palette_bound_shortcuts.clear()
        for screen in self.screen_stack:
            if isinstance(screen, ComposeScreen):
                for key, entries in tuple(screen._bindings.key_to_bindings.items()):
                    retained = [binding for binding in entries if binding.action != "app.palette"]
                    if retained:
                        screen._bindings.key_to_bindings[key] = retained
                    else:
                        screen._bindings.key_to_bindings.pop(key, None)
        for shortcut in dict.fromkeys(
            (self._palette_primary_shortcut, self._palette_secondary_shortcut)
        ):
            if self._valid_shortcut(shortcut):
                normalized = normalize_shortcut(shortcut)
                with contextlib.suppress(Exception):
                    self._bindings.bind(normalized, "palette", "Commands", priority=True)
                    self._palette_bound_shortcuts.add(normalized)
                    for screen in self.screen_stack:
                        if isinstance(screen, ComposeScreen):
                            screen._bindings.bind(
                                normalized, "app.palette", "Commands", show=False, priority=True
                            )
        self.refresh_bindings()

    def _bind_theme_shortcuts(self) -> None:
        for key, entries in tuple(self._bindings.key_to_bindings.items()):
            retained = [
                binding
                for binding in entries
                if binding.action not in {"previous_theme", "next_theme"}
            ]
            if retained:
                self._bindings.key_to_bindings[key] = retained
            else:
                self._bindings.key_to_bindings.pop(key, None)
        for shortcut, action, description in (
            (self._previous_theme_shortcut, "previous_theme", "Previous theme"),
            (self._next_theme_shortcut, "next_theme", "Next theme"),
        ):
            if self._valid_shortcut(shortcut):
                with contextlib.suppress(Exception):
                    self.bind(
                        normalize_shortcut(shortcut), action, description=description, show=False
                    )
        self.refresh_bindings()

    def _load_branding_preferences(self) -> BrandingPreferences:
        settings = self._settings_service()

        def value(setting_id: str, fallback: object) -> object:
            try:
                return settings.value(setting_id).effective_value
            except (ConfigurationError, KeyError, ValueError):
                return fallback

        preset = value("appearance.branding", "builtin.guc-mail-minimal")
        custom_ascii = value("appearance.custom_branding", "")
        size = value("appearance.branding_size", "auto")
        color = str(value("appearance.branding_color_behavior", "theme")).replace("_", "-")
        alignment = value("appearance.banner_alignment", "left")
        width = value("appearance.branding_width", None)
        height = value("appearance.branding_height", None)
        try:
            return BrandingPreferences(
                preset_id=str(preset),
                visible=bool(value("appearance.banner_visible", True)),
                size=BrandSize(str(size)),
                color_behavior=BrandColorBehavior(color),
                alignment=BrandAlignment(str(alignment)),
                custom_ascii=str(custom_ascii or ""),
                target_width=int(width) if isinstance(width, (int, float)) else None,
                target_height=int(height) if isinstance(height, (int, float)) else None,
            )
        except ValueError:
            return BrandingPreferences()

    def _persist_branding_preferences(self, preferences: BrandingPreferences) -> None:
        service = self._settings_service()
        session = service.begin()
        session.set("appearance.branding", preferences.preset_id)
        session.set("appearance.custom_branding", preferences.custom_ascii or None)
        session.set("appearance.banner_visible", preferences.visible)
        session.set("appearance.banner_alignment", preferences.alignment.value)
        session.set("appearance.branding_size", preferences.size.value)
        session.set(
            "appearance.branding_color_behavior",
            preferences.color_behavior.value.replace("-", "_"),
        )
        session.set("appearance.branding_width", preferences.target_width)
        session.set("appearance.branding_height", preferences.target_height)
        session.apply(reason="ASCII branding updated")
        self.branding_preferences = preferences

    def _branding_preferences_changed(self, preferences: BrandingPreferences) -> None:
        if get_ident() != self._thread_id and self.is_running:
            self.call_from_thread(self._branding_preferences_changed, preferences)
            return
        self.branding_preferences = preferences
        if self.is_running:
            self._render_expanded_branding()

    def _activate_theme(self, definition: ThemeDefinition) -> ThemeDefinition:
        self.animation_level = (
            "none" if bool(self._setting_value("accessibility.reduced_motion", False)) else "full"
        )
        runtime_definition = definition
        if bool(self._setting_value("accessibility.high_contrast", False)):
            runtime_definition = adapt_theme_high_contrast(runtime_definition)
        runtime_definition = adapt_theme(runtime_definition, self._terminal_color_capability())
        self._runtime_theme = runtime_definition
        textual_theme = to_textual_theme(runtime_definition)
        already_active = self.theme == textual_theme.name
        self.register_theme(textual_theme)
        if already_active:
            self.refresh_css(animate=False)
        else:
            self.theme = textual_theme.name
        self._active_theme = definition
        # Theme selection also runs during ``__init__``, before Textual has a
        # screen stack or mounted widget tree.  Querying there raises
        # ``ScreenStackError`` rather than ``NoMatches``.  Runtime previews do
        # need to recolor the already-mounted brand, so only query once the app
        # is actually running.
        if self.is_running:
            with contextlib.suppress(NoMatches):
                self.query_one(CompactHeader).set_branding(self._slim_branding(definition))
            self._render_expanded_branding()
            self._apply_theme_structure(runtime_definition)
            with contextlib.suppress(NoMatches):
                self._render_message_rows(self._message_page_rows)
        return definition

    def _activate_theme_callback(self, definition: ThemeDefinition) -> None:
        # Appearance persistence may complete in an operational worker. Runtime
        # widgets and CSS still belong to the app's UI thread.
        if get_ident() != self._thread_id:
            if self.is_running:
                self.call_from_thread(self._activate_theme, definition)
            return
        self._activate_theme(definition)

    @staticmethod
    def _textual_border_style(value: str) -> TextualBorderStyle:
        styles: dict[str, TextualBorderStyle] = {
            "none": "none",
            "solid": "solid",
            "rounded": "round",
            "round": "round",
            "double": "double",
            "heavy": "heavy",
            "dashed": "dashed",
            "subtle": "solid",
        }
        return styles.get(value, "solid")

    def _apply_theme_structure(self, definition: ThemeDefinition) -> None:
        """Apply the non-colour portion of one complete theme to mounted mail UI."""

        theme_style = definition.style
        density_setting = str(self._setting_value("appearance.density", "theme"))
        density = theme_style.row_density if density_setting == "theme" else density_setting
        self._message_row_density = density
        border_setting = str(self._setting_value("appearance.border_style", "theme"))
        border = theme_style.border_style if border_setting == "theme" else border_setting
        border_style: TextualBorderStyle = self._textual_border_style(border)
        divider = self._textual_border_style(theme_style.divider_style)
        if bool(self._setting_value("accessibility.unicode_fallback", False)):
            border_style = "ascii" if border_style != "none" else "none"
            divider = "ascii" if divider != "none" else "none"
        palette = definition.palette

        for mounted_screen in self.screen_stack:
            if isinstance(mounted_screen, OperationalScreen):
                mounted_screen.refresh_workspace_theme()

        with contextlib.suppress(NoMatches):
            table = self.query_one("#message-table", DataTable)
            table.cell_padding = {"compact": 0, "comfortable": 1, "spacious": 2}.get(density, 1)
            cursor = table.get_component_styles("datatable--cursor")
            selection = theme_style.selection_style
            if selection == "reverse":
                cursor.background = palette.selection_foreground
                cursor.color = palette.selection_background
                cursor.text_style = "reverse"
            elif selection == "outline":
                cursor.background = palette.message_list_background
                cursor.color = palette.selection_background
                cursor.text_style = "bold underline"
            elif selection == "accent-bar":
                cursor.background = palette.active
                cursor.color = palette.selection_foreground
                cursor.text_style = "bold"
            else:
                cursor.background = palette.selection_background
                cursor.color = palette.selection_foreground
                cursor.text_style = "bold" if theme_style.bold_text else "none"

        for selector in ("#navigation", "#messages", "#preview-scroll"):
            with contextlib.suppress(NoMatches):
                widget = self.query_one(selector)
                widget.styles.border = (border_style, palette.border)
                if (
                    definition.id in {"builtin.default-dark", "builtin.default-light"}
                    and border_setting == "theme"
                    and not bool(self._setting_value("accessibility.unicode_fallback", False))
                ):
                    widget.styles.border = ("none", palette.border)
                    if selector == "#navigation":
                        widget.styles.border_right = ("solid", palette.divider)
        with contextlib.suppress(NoMatches):
            pane_divider = self.query_one("#pane-divider")
            pane_divider.styles.background = palette.divider
            pane_divider.styles.border_left = (divider, palette.divider)
        with contextlib.suppress(NoMatches):
            banner = self.query_one("#expanded-brand")
            treatment = theme_style.banner_treatment
            banner.styles.border_bottom = (
                ("none", palette.header)
                if treatment == "flat"
                else (
                    self._textual_border_style(theme_style.border_style),
                    palette.logo_accent if treatment == "outlined" else palette.divider,
                )
            )
        with contextlib.suppress(NoMatches):
            floating = self.query_one("#floating-reader", FloatingReader)
            elevation = theme_style.surface_elevation
            floating_border: TextualBorderStyle = (
                "none"
                if elevation == "flat"
                else "double"
                if elevation == "strong"
                else self._textual_border_style(theme_style.border_style)
            )
            floating.styles.background = palette.elevated_surface
            floating.styles.border = (
                floating_border,
                palette.primary_accent
                if theme_style.accent_intensity == "vivid"
                else palette.border,
            )

    def _monochrome_output(self) -> bool:
        """Whether colours are suppressed by configuration or by ``NO_COLOR``.

        Textual honours ``NO_COLOR`` by greyscaling every rendered cell.  The
        tricolour brand must then use the theme foreground: the luminance of
        German red on a dark header is almost the header colour itself.
        """

        return (
            self.context.config.no_color
            or bool(getattr(self, "no_color", False))
            or os.environ.get("NO_COLOR") is not None
        )

    def _terminal_color_capability(self) -> ColorCapability:
        if self._monochrome_output():
            return ColorCapability.MONOCHROME
        return {
            "truecolor": ColorCapability.TRUECOLOR,
            "256": ColorCapability.COLORS_256,
            "standard": ColorCapability.COLORS_16,
            "windows": ColorCapability.COLORS_16,
        }.get(self.console.color_system or "", ColorCapability.TRUECOLOR)

    def _slim_branding(self, definition: ThemeDefinition) -> RenderedBranding:
        capability = self._terminal_color_capability()
        return slim_brand(definition, capability=capability)

    @staticmethod
    def _branding_text(rendered: RenderedBranding) -> Text:
        value = Text(no_wrap=True, overflow="crop")
        for line_index, line in enumerate(rendered.lines):
            for span in line.spans:
                style = span.foreground
                if span.background:
                    style = f"{style} on {span.background}"
                value.append(span.text, style=style)
            if line_index + 1 < len(rendered.lines):
                value.append("\n")
        return value

    def _render_expanded_branding(self) -> None:
        try:
            widget = self.query_one("#expanded-brand", Static)
        except NoMatches:
            return
        capability = self._terminal_color_capability()
        max_height = min(6, max(2, self.size.height // 8))
        try:
            rendered = render_branding(
                self.branding_preferences,
                self.active_theme,
                terminal_width=max(1, self.size.width - 2),
                max_height=max_height,
                capability=capability,
                unicode_supported=not bool(
                    self._setting_value("accessibility.unicode_fallback", False)
                ),
            )
        except Exception:
            rendered = RenderedBranding(
                preset_id="none",
                variant=BrandSize.MINIMAL,
                lines=(),
                color_behavior=self.branding_preferences.color_behavior,
            )
        # The ordinary wordmark belongs in the same row as connection status.
        # Keep explicitly selected multi-line art as a separate optional banner.
        inline_wordmark = self.branding_preferences.preset_id in {
            "builtin.guc-mail-minimal",
            "builtin.guc-original-mark",
        }
        show_banner = bool(rendered.lines) and not inline_wordmark
        widget.display = show_banner
        widget.update(self._branding_text(rendered) if show_banner else "")
        with contextlib.suppress(NoMatches):
            self.query_one(CompactHeader).query_one("#compact-title").display = not show_banner

    def _watcher_header_label(self) -> str:
        try:
            records = self.context.state.list_watcher_status()
        except Exception:
            return "Watcher unknown"
        if not records:
            return "Watcher off"
        active = next(
            (
                record
                for record in records
                if str(getattr(record, "state", "")).casefold() not in {"stopped", "failed"}
            ),
            records[0],
        )
        state = str(getattr(active, "state", "unknown"))
        return f"Watcher {sanitize_terminal(state, max_length=30)}"

    def _help_runtime_context(self, active_view: str) -> HelpRuntimeContext:
        settings_service = self._settings_service()
        try:
            values = {
                value.spec.setting_id: value.effective_value
                for value in settings_service.values()
                if value.effective_value is not None
            }
        except ConfigurationError:
            values = {}
        facts: dict[str, object] = {}
        with contextlib.suppress(Exception):
            for observation in self.context.state.latest_quota_observations():
                prefix = f"quota.{observation.metric}"
                facts[f"{prefix}.bytes"] = observation.value_bytes
                facts[f"{prefix}.source"] = observation.source
                facts[f"{prefix}.confidence"] = observation.confidence
                facts[f"{prefix}.observed_at"] = observation.observed_at.isoformat()
        contextual = bool(values.get("help.contextual_suggestions", True))
        return HelpRuntimeContext(
            active_view=active_view if contextual else None,
            capabilities={
                "online": self.ui.online and not self.offline,
                "offline": self.offline,
            },
            settings=values,
            shortcut_assignments=runtime_shortcut_records(self, self._current_shortcuts()),
            facts=facts,
        )

    def _help_state_store(self) -> SettingsHelpStateStore:
        return SettingsHelpStateStore(self.context.state, self._settings_service())

    def preview_theme(self, theme_id: str) -> ThemeDefinition:
        """Preview a theme live without changing configuration or durable UI state."""

        selected = self.theme_session.preview(normalize_theme_identifier(theme_id))
        return self._activate_theme(selected)

    def _cycle_theme(self, offset: int) -> None:
        selected = self.theme_registry.next(self.active_theme.id, offset=offset)
        self.preview_theme(selected.id)
        self._set_status(
            f"Previewing theme {sanitize_terminal(selected.name, max_length=80)} · "
            "open Appearance → Themes to apply or cancel"
        )

    def action_previous_theme(self) -> None:
        self._cycle_theme(-1)

    def action_next_theme(self) -> None:
        self._cycle_theme(1)

    def cancel_theme_preview(self) -> ThemeDefinition:
        """Restore the last applied theme exactly, leaving persistence untouched."""

        return self._activate_theme(self.theme_session.cancel())

    def apply_theme(self, theme_id: str | None = None) -> ThemeDefinition:
        """Apply and durably persist either the preview or an explicit theme."""

        normalized = normalize_theme_identifier(theme_id) if theme_id is not None else None
        selected = self.theme_session.apply(normalized)
        return self._activate_theme(selected)

    def _read_ui_preference(self, key: str, default: Any) -> Any:
        """Read non-secret UI session state without making it configuration truth."""

        try:
            self.context.state.initialize()
            with self.context.state.connection() as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = ?",
                    (f"ui:{key}",),
                ).fetchone()
            return json.loads(str(row["value"])) if row is not None else default
        except Exception:
            return default

    def _write_ui_preference(self, key: str, value: Any) -> None:
        """Atomically persist bounded, non-secret UI state."""

        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            with self.context.state.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO metadata(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (f"ui:{key}", payload),
                )
        except Exception:
            # Layout persistence must never make mail unusable.
            return

    @staticmethod
    def _validated_ratio(value: Any, *, default: float) -> float:
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(ratio):
            return default
        # The complementary preview ratio is persisted too; these bounds
        # satisfy both registered pane constraints.
        return min(max(ratio, 0.25), 0.70)

    @staticmethod
    def _validated_sidebar_ratio(value: Any) -> float:
        try:
            ratio = float(value)
        except (TypeError, ValueError):
            return 0.22
        if not math.isfinite(ratio):
            return 0.22
        return min(max(ratio, 0.12), 0.45)

    def on_mount(self) -> None:
        self._bind_palette_shortcuts()
        self.screen.add_class("frontend")
        self._bind_theme_shortcuts()
        self._apply_keyboard_bindings()
        if self._shortcut_notice:
            self.notify(self._shortcut_notice, severity="warning", timeout=10)
        self._populate_static_navigation()
        table = self.query_one("#message-table", DataTable)
        for label, key in (
            ("", "state"),
            ("From", "sender"),
            ("Subject", "subject"),
            ("Received", "received"),
            ("Size", "size"),
        ):
            table.add_column(label, key=key)
        self._apply_theme_structure(self._runtime_theme)
        self._apply_breakpoint(self.size.width)
        self._apply_layout()
        self._render_expanded_branding()
        self._refresh_shell_status()
        self._restore_session_focus()
        self._session_capture_timer = self.set_interval(1, self._capture_session)
        if not self.offline:
            self._request_folder_children(parent_reference=None, node_id=self._folder_root_node_id)
        if not self.offline:
            # Wait for layout so the initial request can match the visible rows.
            self.call_after_refresh(self._initial_mailbox_refresh)

    @on(Button.Pressed, "#workspace-tabs Button")
    def workspace_tab_pressed(self, event: Button.Pressed) -> None:
        area = (event.button.id or "").removeprefix("workspace-")
        if area in {"calendar", "contacts", "tasks", "settings"}:
            self._open_area(area)

    def _initial_mailbox_refresh(self) -> None:
        # A user action may have started a search before the first layout completed.
        if self._mailbox_generation == 0:
            self.refresh_mailbox()

    def _restore_session_focus(self) -> None:
        restored = self._restored_session
        if not self.screen_stack:
            return
        if restored is None:
            self.screen_stack[0].query_one("#message-table", DataTable).focus()
            self._capture_session()
            return
        root = self.screen_stack[0]
        if self.screen is not root:
            return
        widget = root.query_one(f"#{restored.focus}", Widget)
        hidden_pane = (
            restored.focus == "preview-scroll"
            and not (self.has_class("wide") and self._preview_visible)
        ) or (
            restored.focus == "folder-tree"
            and (not self._sidebar_visible or self.has_class("narrow"))
        )
        if hidden_pane or widget not in root.focus_chain:
            widget = root.query_one("#message-table", DataTable)
        if widget in root.focus_chain:
            root.set_focus(widget, scroll_visible=False)
        tree = root.query_one("#folder-tree", Tree)
        tree.scroll_to(y=restored.folder_scroll, animate=False, force=True)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if self.is_running and self.screen_stack:
            with contextlib.suppress(NoScreen):
                if event.widget.screen is self.screen_stack[0]:
                    self.call_after_refresh(self._capture_session)

    def _capture_session(self) -> None:
        if not self.is_running or not self.screen_stack:
            return
        root = self.screen_stack[0]
        try:
            table = root.query_one("#message-table", DataTable)
            tree = root.query_one("#folder-tree", Tree)
            focus = root.focused.id if root.focused is not None else None
            if focus not in FOCUS_TARGETS:
                focus = self._session_snapshot.focus if self._session_snapshot else "message-table"
            # A modal screen can never supply focus, action parameters, or a body.
            pending = self._pending_session_position
            retain_position = pending is not None and pending.folder == self.ui.folder
            session = MailSession(
                folder=self.ui.folder,
                focus=focus,
                message_row=(
                    pending.message_row
                    if retain_position and pending is not None
                    else min(max(0, table.cursor_row), MAX_PAGE_SIZE - 1)
                ),
                message_scroll=(
                    pending.message_scroll
                    if retain_position and pending is not None
                    else min(max(0, int(table.scroll_y)), MAX_SCROLL)
                ),
                folder_scroll=min(max(0, int(tree.scroll_y)), MAX_SCROLL),
                search_scope=self._last_search_scope,
                mail_search_scope=self._last_mail_search_scope,
                mail_search_folder=self._last_mail_search_folder,
                search_default_scope=self._mail_search_default_scope,
            )
            if MailSession.parse(session.payload()) is not None:
                self._session_snapshot = session
        except (NoMatches, ValueError):
            return

    def exit(
        self,
        result: None = None,
        return_code: int = 0,
        message: RenderableType | None = None,
    ) -> None:
        self._capture_session()
        super().exit(result=result, return_code=return_code, message=message)

    @on(events.Click, "#palette-launcher")
    def _palette_launcher_clicked(self, event: events.Click) -> None:
        event.stop()
        self.action_palette()

    async def on_unmount(self) -> None:
        if self._session_capture_timer is not None:
            self._session_capture_timer.stop()
        if self._session_snapshot is not None:
            # Await an ordinary executor task: Textual cancels its own workers on exit.
            # No SQLite transaction or lock wait runs on the event loop.
            await asyncio.to_thread(self._session_store.save, self._session_snapshot)
        self._mailbox_generation += 1
        self._message_generation += 1
        self._folder_generation += 1
        self._cancel_automatic_read()
        if self._mailbox_timeout is not None:
            self._mailbox_timeout.stop()
        if self._viewport_fill_timer is not None:
            self._viewport_fill_timer.stop()
        if self._message_timeout is not None:
            self._message_timeout.stop()
        if not self._gateway_injected:
            self.context.close()

    @on(WriteBlocked)
    def _write_blocked(self, event: WriteBlocked) -> None:
        self._set_status(event.detail)
        if isinstance(self.screen, ComposeScreen):
            self.screen._set_note(event.detail)
        elif isinstance(self.screen, OperationalScreen):
            self.screen._last_operation = None
            self.screen._set_view_state(
                ViewState.LOADING if self.screen._operation_active else ViewState.ERROR,
                event.detail,
            )

    @on(WriteStateChanged)
    def _write_state_changed(self, _event: WriteStateChanged) -> None:
        self._refresh_status_bar()

    def _populate_static_navigation(self) -> None:
        tree = self.query_one("#folder-tree", Tree)
        tree.show_guides = False
        tree.show_root = False
        # Selecting a folder opens it; explicit collapse commands close branches.
        # Textual's default toggle would collapse an already-expanded selection.
        tree.auto_expand = False
        root = tree.root
        root.remove_children()
        root.expand()
        views = root.add("Views", expand=True)
        views.add_leaf("Inbox", data={"kind": "folder", "reference": "inbox"})
        views.add_leaf("Unread", data={"kind": "local_filter", "reference": "unread"})
        views.add_leaf("Flagged", data={"kind": "local_filter", "reference": "flagged"})

        # Keep the application areas reachable even when the mailbox has a long
        # hierarchy. The group starts collapsed so folders keep their space.
        tools = root.add("Tools", data={"kind": "tools_branch"}, expand=False)
        tools.add_leaf("Scheduled", data={"kind": "area", "reference": "scheduled"})
        for label, reference in (
            ("Calendar", "calendar"),
            ("Contacts", "contacts"),
            ("Tasks", "tasks"),
            ("Search", "search"),
            ("Backups", "backups"),
            ("Storage", "storage"),
            ("Activity", "activity"),
            ("Settings", "settings"),
            ("Saved layouts", "layouts"),
        ):
            tools.add_leaf(label, data={"kind": "area", "reference": reference})

        folders = root.add(
            "Exchange folders",
            data={"kind": "folder_branch", "reference": None, "loaded": False},
            expand=True,
        )
        folders.add_leaf(
            "Connect to load folders" if self.offline else "Loading folders…",
            data={"kind": "placeholder"},
        )
        self._folder_root_node_id = folders.id

    @staticmethod
    def _folder_label(folder: FolderInfo) -> str:
        name = sanitize_terminal(folder.name, max_length=180) or "(unnamed folder)"
        if folder.unread_count is not None and folder.total_count is not None:
            return f"{name}  {folder.unread_count}/{folder.total_count}"
        if folder.unread_count is not None:
            return f"{name}  ({folder.unread_count} unread)"
        if folder.total_count is not None:
            return f"{name}  ({folder.total_count})"
        return name

    def _request_folder_children(
        self,
        *,
        parent_reference: str | None,
        node_id: Any | None,
        offset: int = 0,
        append: bool = False,
    ) -> None:
        if node_id is None or self.offline:
            return
        self._folder_load_sequence += 1
        token = self._folder_load_sequence
        self._folder_load_tokens[node_id] = token
        try:
            node = self.query_one("#folder-tree", Tree).get_node_by_id(node_id)
        except Exception:
            return
        data = dict(node.data) if isinstance(node.data, dict) else {}
        data.update({"loading": True, "loaded": False})
        node.data = data
        self._load_folder_children(
            token,
            node_id,
            parent_reference=parent_reference,
            offset=offset,
            append=append,
        )

    @work(thread=True, exclusive=False, group="folder-load")
    def _load_folder_children(
        self,
        token: int,
        node_id: Any,
        *,
        parent_reference: str | None,
        offset: int,
        append: bool,
    ) -> None:
        try:
            gateway = self._ensure_gateway()
            service = FolderService(gateway)
            page = service.list_folders(
                parent_reference,
                limit=min(max(self.context.config.page_size, 25), 100),
                offset=offset,
            )
            self.call_from_thread(
                self._render_folder_children,
                token,
                node_id,
                parent_reference,
                page,
                append,
            )
        except Exception as exc:
            self.call_from_thread(
                self._render_folder_error,
                token,
                node_id,
                _safe_failure(exc, action="Folder hierarchy unavailable"),
            )

    def _render_folder_children(
        self,
        token: int,
        node_id: Any,
        parent_reference: str | None,
        page: Any,
        append: bool,
    ) -> None:
        if self._folder_load_tokens.get(node_id) != token:
            return
        tree = self.query_one("#folder-tree", Tree)
        try:
            node = tree.get_node_by_id(node_id)
        except Exception:
            return
        if not append:
            self._forget_folder_nodes(node)
            node.remove_children()
        else:
            for child in tuple(node.children):
                child_data = child.data
                if isinstance(child_data, dict) and child_data.get("kind") == "folder_more":
                    child.remove()
        for folder in page.items:
            if not _is_mail_folder_class(folder.folder_class):
                # Calendar/Contacts/Tasks have their own workspaces; Exchange system
                # folders (Recipient Cache, Conversation Action Settings, Files,
                # Journal, Notes) only clutter the mail tree. Unknown classes stay.
                continue
            reference = str(folder.reference)
            child_data = {
                "kind": "folder_branch" if folder.child_count != 0 else "folder",
                "reference": reference,
                "loaded": folder.child_count == 0,
                "path": folder.path,
                "folder": folder,
            }
            if folder.child_count == 0:
                child = node.add_leaf(Text(self._folder_label(folder)), data=child_data)
            else:
                child = node.add(Text(self._folder_label(folder)), data=child_data)
                child.add_leaf("Loading…", data={"kind": "placeholder"})
            self._folder_nodes[reference] = child.id
            self._folder_paths[reference] = folder.path
            auto_expand = self._folder_expand_all_remaining > 0 and folder.child_count != 0
            if auto_expand:
                self._folder_expand_all_remaining -= 1
            if (reference in self._expanded_folders or auto_expand) and folder.child_count != 0:
                child.expand()
            if reference == self.ui.folder:
                # Reflect the current location without queuing NodeSelected,
                # whose later delivery could undo a newer palette navigation.
                self.call_after_refresh(
                    self._sync_folder_cursor, tree, child, reference, tree.cursor_node
                )
                if self._folder_selection_to_restore == reference:
                    # Removing a refreshed subtree makes Textual temporarily move
                    # the cursor to an ancestor. Re-select after the replacement
                    # nodes finish, unless a newer user selection cancels the token.
                    self.call_after_refresh(
                        self._restore_folder_selection,
                        tree,
                        child,
                        reference,
                    )
        auto_page_offset: int | None = None
        if page.has_more and page.next_offset is not None:
            if self._folder_expand_all_remaining > 0:
                self._folder_expand_all_remaining -= 1
                auto_page_offset = page.next_offset
            else:
                node.add_leaf(
                    "Load more folders…",
                    data={
                        "kind": "folder_more",
                        "reference": parent_reference,
                        "offset": page.next_offset,
                    },
                )
        data = dict(node.data) if isinstance(node.data, dict) else {}
        data.update({"loading": False, "loaded": True})
        node.data = data
        if parent_reference is not None:
            self._folder_refresh_pending_expanded.discard(parent_reference)
        if self._folder_refresh_in_progress and not self._folder_refresh_pending_expanded:
            # Removing a branch's loading sentinel may enqueue NodeCollapsed.
            # Let those programmatic events drain before user collapses become
            # authoritative again.
            generation = self._folder_refresh_generation
            self.set_timer(
                0.25,
                lambda: self._finish_folder_refresh(generation),
            )
        if auto_page_offset is not None:
            self._request_folder_children(
                parent_reference=parent_reference,
                node_id=node.id,
                offset=auto_page_offset,
                append=True,
            )

    def _restore_folder_selection(self, tree: Tree[Any], node: Any, reference: str) -> None:
        """Restore a refresh selection unless the user selected something newer."""

        if self._folder_selection_to_restore == reference and self.ui.folder == reference:
            self._folder_selection_to_restore = None
            if self._folder_refresh_expanded_snapshot:
                self._expanded_folders.update(self._folder_refresh_expanded_snapshot)
                self._persist_folder_expansion()
            tree.move_cursor(node)

    def _sync_folder_cursor(
        self, tree: Tree[Any], node: Any, reference: str, previous_cursor: Any
    ) -> None:
        """Wait for new node lines without replacing a newer user cursor."""

        if (
            tree.is_mounted
            and self.ui.folder == reference
            and self._folder_nodes.get(reference) == node.id
            and tree.cursor_node is previous_cursor
        ):
            tree.move_cursor(node)

    def _load_rendered_folder_pages_for_expand_all(self, root: Any) -> None:
        """Consume existing pagination leaves while the bounded expand-all budget permits."""

        pending = [root]
        while pending and self._folder_expand_all_remaining > 0:
            parent = pending.pop()
            for child in tuple(parent.children):
                data = child.data
                if isinstance(data, dict) and data.get("kind") == "folder_more":
                    offset = data.get("offset")
                    if not isinstance(offset, int):
                        continue
                    reference = data.get("reference")
                    self._folder_expand_all_remaining -= 1
                    child.remove()
                    self._request_folder_children(
                        parent_reference=str(reference) if reference is not None else None,
                        node_id=parent.id,
                        offset=offset,
                        append=True,
                    )
                else:
                    pending.append(child)

    def _finish_folder_refresh(self, generation: int | None = None) -> None:
        """Release refresh event suppression after saved open branches are restored."""

        if generation is not None and generation != self._folder_refresh_generation:
            return
        if not self._folder_refresh_in_progress:
            return
        self._expanded_folders.update(self._folder_refresh_expanded_snapshot)
        self._persist_folder_expansion()
        self._folder_refresh_in_progress = False
        self._folder_refresh_preserve_expanded.clear()
        self._folder_refresh_expanded_snapshot.clear()
        self._folder_refresh_pending_expanded.clear()

    def _forget_folder_nodes(self, node: Any) -> None:
        """Remove stale identity/path entries before replacing one loaded subtree."""

        pending = list(node.children)
        while pending:
            child = pending.pop()
            pending.extend(child.children)
            data = child.data
            if not isinstance(data, dict):
                continue
            reference = data.get("reference")
            if isinstance(reference, str):
                if reference in self._expanded_folders:
                    self._folder_refresh_preserve_expanded.add(reference)
                self._folder_nodes.pop(reference, None)
                self._folder_paths.pop(reference, None)

    def _render_folder_error(self, token: int, node_id: Any, message: str) -> None:
        if self._folder_load_tokens.get(node_id) != token:
            return
        try:
            node = self.query_one("#folder-tree", Tree).get_node_by_id(node_id)
        except Exception:
            return
        node.remove_children()
        node.add_leaf("Folder loading failed · press F5 to retry", data={"kind": "placeholder"})
        data = dict(node.data) if isinstance(node.data, dict) else {}
        data.update({"loading": False, "loaded": False})
        node.data = data
        self._set_status(message)

    @on(events.Resize)
    def resized(self, event: events.Resize) -> None:
        self._apply_breakpoint(event.size.width)
        self._apply_layout()
        self._render_expanded_branding()
        self.call_after_refresh(self._fit_message_columns)
        self.call_after_refresh(self._schedule_viewport_fill)

    def _apply_breakpoint(self, width: int) -> None:
        self.remove_class("narrow", "medium", "wide")
        self.add_class("wide" if width >= 120 else "medium" if width >= 80 else "narrow")

    def _apply_layout(self) -> None:
        try:
            navigation = self.query_one("#navigation", Vertical)
            messages = self.query_one("#messages", Vertical)
            preview = self.query_one("#preview-scroll", VerticalScroll)
            divider = self.query_one("#pane-divider", PaneDivider)
            status_line = self.query_one("#status-line", Horizontal)
        except NoMatches:
            return

        self.set_class(self._split_orientation == "horizontal", "split-horizontal")
        self.set_class(not self._preview_visible, "preview-hidden")
        self.set_class(not self._sidebar_visible, "sidebar-hidden")
        if not self.has_class("narrow") or not self._sidebar_visible:
            self.remove_class("sidebar-visible")
        navigation.styles.width = f"{self._sidebar_ratio * 100.0:.3f}%"
        status_line.display = self._status_bar_visible
        pane_available = self.has_class("wide") and self._preview_visible
        if (
            not pane_available
            and self._automatic_read_request is not None
            and self._automatic_read_request.view.id == "preview"
        ):
            self._cancel_automatic_read()
        if not pane_available:
            messages.styles.width = "1fr"
            messages.styles.height = "1fr"
            preview.styles.width = "1fr"
            preview.styles.height = "1fr"
            divider.styles.width = 1
            divider.styles.height = "1fr"
            return

        message_percent = self._message_ratio * 100.0
        if self._split_orientation == "horizontal":
            messages.styles.width = "1fr"
            messages.styles.height = f"{message_percent:.3f}%"
            divider.styles.width = "1fr"
            divider.styles.height = 1
            preview.styles.width = "1fr"
            preview.styles.height = "1fr"
        else:
            messages.styles.width = f"{message_percent:.3f}%"
            messages.styles.height = "1fr"
            divider.styles.width = 1
            divider.styles.height = "1fr"
            preview.styles.width = "1fr"
            preview.styles.height = "1fr"

        self.call_after_refresh(self._fit_message_columns)

    def _resize_message_ratio(self, delta: float, *, persist: bool = True) -> None:
        self._message_ratio = self._validated_ratio(
            self._message_ratio + delta,
            default=0.40,
        )
        self._preview_visible = True
        self._layout_last_preset = "custom"
        self._apply_layout()
        if persist:
            self._persist_message_layout()

    @on(PaneDivider.Moved)
    def divider_moved(self, event: PaneDivider.Moved) -> None:
        if event.delta:
            try:
                reader = self.query_one("#reader-layout", Horizontal)
                span = (
                    reader.region.height
                    if self._split_orientation == "horizontal"
                    else reader.region.width
                )
            except NoMatches:
                return
            if span > 1:
                self._resize_message_ratio(event.delta / span, persist=False)
        if event.final:
            self._persist_message_layout()

    def action_preview_wider(self) -> None:
        self._resize_message_ratio(-0.05)

    def action_preview_narrower(self) -> None:
        self._resize_message_ratio(0.05)

    def action_toggle_preview(self) -> None:
        self._preview_visible = not self._preview_visible
        self._layout_last_preset = "custom"
        self._persist_message_layout()
        self._apply_layout()

    def action_reset_layout(self) -> None:
        self._message_ratio = self._validated_ratio(0.36 / (0.36 + 0.42), default=0.40)
        self._split_orientation = "vertical"
        self._preview_visible = True
        self._sidebar_visible = True
        self._sidebar_ratio = 0.22
        self._status_bar_visible = True
        self._layout_last_preset = "default"
        saved = self._persist_message_layout(reason="Mail layout reset")
        self._apply_layout()
        self._set_status("Layout reset" if saved else "Layout reset for this session · not saved")

    def action_toggle_sidebar(self) -> None:
        if self.has_class("narrow"):
            self.toggle_class("sidebar-visible")
            self._sidebar_visible = self.has_class("sidebar-visible")
        else:
            self._sidebar_visible = not self._sidebar_visible
        self._layout_last_preset = "custom"
        self._persist_message_layout()
        self._apply_layout()

    def _set_status(self, text: str) -> None:
        self._status_activity = text
        self._refresh_status_bar()

    def _layout_preset_values(self) -> dict[str, Any]:
        values = {
            key: self._setting_value(key, DEFAULT_SETTINGS.get(key).default)
            for key in PRESET_FIELDS
        }
        values.update(
            {
                "layout.sidebar_visible": self._sidebar_visible,
                "layout.sidebar_ratio": self._sidebar_ratio,
                "layout.message_list_ratio": self._message_ratio,
                "layout.preview_ratio": 1.0 - self._message_ratio,
                "layout.split_orientation": self._split_orientation,
                "layout.preview_visible": self._preview_visible,
                "layout.reader_mode": self._reader_mode,
                "layout.status_bar_visible": self._status_bar_visible,
                "layout.header_contents": self._header_contents,
                "layout.header_compactness": self._header_compactness,
                "layout.status_bar_contents": self._status_bar_contents,
                "layout.message_columns": columns_value(self._message_columns),
            }
        )
        return values

    def action_layout_presets(self) -> None:
        if isinstance(self.screen, SettingsScreen):
            self.notify(
                "Apply or cancel staged settings and close Settings before opening saved layouts.",
                title="Saved layouts",
            )
            return
        self.push_screen(
            LayoutPresetsScreen(
                LayoutPresetService(self._settings_service()),
                capture=self._layout_preset_values,
                on_applied=self._apply_committed_settings,
            )
        )

    def _shell_facts(self) -> dict[str, str]:
        return {
            "account": self._header_connection,
            "connection": self._header_connection,
            "storage": self._header_quota_label,
            "folder": sanitize_terminal(
                self._folder_paths.get(self.ui.folder, self.ui.folder), max_length=100
            ),
            "monitoring": self._watcher_header_label(),
        }

    def _refresh_status_bar(self) -> None:
        try:
            bar = self.query_one("#status-bar", Static)
        except NoMatches:
            return
        # No status lookup is needed for the default activity-only footer.
        facts = self._shell_facts() if len(self._status_bar_contents) > 1 else {}
        activity = self._status_activity
        if app_write_coordinator(self).pending and not activity.startswith(PENDING_CHANGE_MESSAGE):
            activity = CHANGE_IN_PROGRESS_MESSAGE
        bar.update(status_text(activity, self._status_bar_contents, facts))

    def _refresh_shell_status(self) -> None:
        try:
            bar = self.query_one("#account-bar", AccountStatus)
        except NoMatches:
            return
        facts = self._shell_facts()
        detail_facts = {**facts, "account": self._header_account_detail}
        detail = " · ".join(detail_facts[key] for key in self._header_contents)
        bar.set_mailbox_status(
            quota=facts["storage"],
            watcher=facts["monitoring"],
            folder=facts["folder"],
            detail=detail,
            connection=facts["account"],
            fields=self._header_contents,
            compactness=self._header_compactness,
        )
        self._refresh_status_bar()

    def _ensure_gateway(self) -> Any:
        if self.gateway is None:
            if self.offline:
                raise RuntimeError("Offline mode does not resolve credentials or Exchange.")
            self.gateway = self.context.gateway(interactive=False)
        return self.gateway

    def _quota_service(self, gateway: Any) -> QuotaService:
        settings = SettingsService(
            self.context.config_store,
            context=SettingsContext(
                account_id=self.context.config.email,
                machine_id=f"{sys.platform}:{platform.node()}",
            ),
        )

        def setting(setting_id: str, fallback: object) -> object:
            try:
                return settings.value(setting_id).effective_value
            except ConfigurationError:
                return fallback

        return quota_service_from_settings(gateway, setting)

    def _mailbox_page_limit(self) -> int:
        """Keep the list filled without making unbounded mailbox requests."""

        preferred = min(max(self.context.config.page_size, 1), MAX_PAGE_SIZE)
        if not self._fill_message_viewport:
            return preferred
        try:
            table = self.query_one("#message-table", DataTable)
        except NoMatches:
            return preferred
        height = table.content_size.height
        header = table.header_height if table.show_header else 0
        row_height = 2 if self._message_row_density == "spacious" else 1
        visible_rows = max(0, (height - header) // row_height)
        return min(MAX_PAGE_SIZE, max(preferred, visible_rows))

    def _schedule_viewport_fill(self) -> None:
        """Debounce growth; never replace a search or repeatedly load a short folder."""

        if self._viewport_fill_timer is not None:
            self._viewport_fill_timer.stop()
            self._viewport_fill_timer = None
        if (
            self.offline
            or not self.is_running
            or not self._fill_message_viewport
            or not self.ui.online
            or self.ui.busy is not None
            or self.ui.search
            or getattr(self, "_search_heading_label", None) is not None
            or self._loaded_mailbox_folder != self.ui.folder
            or not self._loaded_mailbox_limit
            or self._mailbox_next_offset > self._loaded_mailbox_limit
            or len(self._messages) < self._loaded_mailbox_limit
            or self._mailbox_page_limit() <= self._loaded_mailbox_limit
        ):
            return
        self._viewport_fill_timer = self.set_timer(0.75, self._fill_grown_viewport)

    def _fill_grown_viewport(self) -> None:
        self._viewport_fill_timer = None
        if (
            self.ui.online
            and self.ui.busy is None
            and not self.ui.search
            and getattr(self, "_search_heading_label", None) is None
            and self._loaded_mailbox_folder == self.ui.folder
            and self._fill_message_viewport
            and self._mailbox_next_offset <= self._loaded_mailbox_limit
            and len(self._messages) >= self._loaded_mailbox_limit
            and self._mailbox_page_limit() > self._loaded_mailbox_limit
        ):
            self.refresh_mailbox()

    def refresh_mailbox(self) -> None:
        if self.offline:
            self._set_status("Offline mode · Exchange was not contacted")
            return
        folder = self.ui.folder
        limit = self._mailbox_page_limit()
        generation = self._begin_mailbox_request(
            f"Loading {sanitize_terminal(folder, max_length=80)}… · Esc cancels"
        )
        self._refresh_mailbox_worker(generation, folder, limit)

    def _begin_mailbox_request(self, status: str) -> int:
        """Allocate the read identity on the UI thread, before a worker can start."""

        self._mailbox_generation += 1
        generation = self._mailbox_generation
        self.ui.busy = "mailbox"
        self._set_status(status)
        if self._mailbox_timeout is not None:
            self._mailbox_timeout.stop()
        self._mailbox_timeout = self.set_timer(
            self._load_timeout_seconds,
            lambda: self._mailbox_load_timed_out(generation),
        )
        return generation

    @work(thread=True, exclusive=True, group="mailbox-refresh")
    def _refresh_mailbox_worker(self, generation: int, folder: str, limit: int) -> None:
        try:
            gateway = self._ensure_gateway()
            status = gateway.status()
            try:
                quota_report = self._quota_service(gateway).report()
                quota_label = compact_quota_status(quota_report)
                quota_percent = self._quota_percent(quota_report)
            except Exception:
                quota_label = "Storage unavailable"
                quota_percent = None
            messages = (
                MessageService(gateway)
                .list_messages(
                    folder=folder,
                    limit=limit,
                    order_by=self._mail_sort_order,
                )
                .items
            )
            self.call_from_thread(
                self._complete_mailbox_refresh,
                generation,
                folder,
                status,
                messages,
                quota_label,
                quota_percent,
                limit,
            )
        except Exception as exc:
            self.call_from_thread(
                self._fail_mailbox_refresh,
                generation,
                _safe_failure(exc, action="Mailbox unavailable"),
            )

    def _complete_mailbox_refresh(
        self,
        generation: int,
        folder: str,
        status: Any,
        messages: Any,
        quota_label: str,
        quota_percent: float | None,
        limit: int,
    ) -> None:
        if generation != self._mailbox_generation or folder != self.ui.folder:
            return
        if self._mailbox_timeout is not None:
            self._mailbox_timeout.stop()
            self._mailbox_timeout = None
        self._render_status(status, quota_label=quota_label)
        self._mailbox_quota_percent = quota_percent
        self._loaded_mailbox_limit = limit
        self._loaded_mailbox_folder = folder
        self._mailbox_next_offset = len(messages)
        self._mailbox_has_more = len(messages) >= limit
        self._render_messages(messages)
        self.ui.busy = None
        self.ui.online = True
        self._set_status("Ready")
        self.call_after_refresh(self._schedule_viewport_fill)

    def _load_next_mailbox_page(self, *, mouse: bool = False) -> None:
        """Fetch one more bounded page only when the user reaches the end."""

        if (
            self.offline
            or not self.ui.online
            or self.ui.busy is not None
            or self.ui.search
            or getattr(self, "_search_heading_label", None) is not None
            or self._message_page_rows != self._messages
            or self._loaded_mailbox_folder != self.ui.folder
            or not self._mailbox_has_more
        ):
            return
        folder = self.ui.folder
        offset = self._mailbox_next_offset
        limit = self._mailbox_page_limit()
        generation = self._begin_mailbox_request("Loading more messages… · Esc cancels")
        self._load_next_mailbox_page_worker(generation, folder, offset, limit, mouse)

    @work(thread=True, exclusive=True, group="mailbox-refresh")
    def _load_next_mailbox_page_worker(
        self, generation: int, folder: str, offset: int, limit: int, mouse: bool
    ) -> None:
        try:
            page = MessageService(self._ensure_gateway(), state=self.context.state).list_messages(
                folder=folder, limit=limit, offset=offset, order_by=self._mail_sort_order
            )
            self.call_from_thread(
                self._complete_next_mailbox_page,
                generation,
                folder,
                offset,
                limit,
                page.items,
                mouse,
            )
        except Exception as exc:
            self.call_from_thread(
                self._fail_next_mailbox_page,
                generation,
                _safe_failure(exc, action="Loading more failed"),
            )

    def _complete_next_mailbox_page(
        self, generation: int, folder: str, offset: int, limit: int, messages: Any, mouse: bool
    ) -> None:
        if generation != self._mailbox_generation or folder != self.ui.folder:
            return
        if self._mailbox_timeout is not None:
            self._mailbox_timeout.stop()
            self._mailbox_timeout = None
        self.ui.busy = None
        if (
            self.ui.search
            or getattr(self, "_search_heading_label", None) is not None
            or self._message_page_rows != self._messages
            or offset != self._mailbox_next_offset
        ):
            self._set_status("View changed; additional messages were not added")
            return
        table = self.query_one("#message-table", DataTable)
        scroll_y = table.scroll_y
        known = {item.reference for item in self._messages if isinstance(item, MessageSummary)}
        appended = []
        for item in messages:
            if isinstance(item, MessageSummary) and item.reference not in known:
                appended.append(item)
                known.add(item.reference)
        self._mailbox_next_offset += len(messages)
        self._mailbox_has_more = len(messages) >= limit
        self._messages += tuple(appended)
        self._render_message_rows(self._messages)
        self.call_after_refresh(
            lambda: table.scroll_to(y=scroll_y + (3 if mouse and appended else 0), animate=False)
        )
        self._set_status("Ready" if self._mailbox_has_more else "End of folder")

    def _fail_next_mailbox_page(self, generation: int, message: str) -> None:
        if generation != self._mailbox_generation:
            return
        if self._mailbox_timeout is not None:
            self._mailbox_timeout.stop()
            self._mailbox_timeout = None
        self.ui.busy = None
        self._set_status(f"{message} · Scroll down to retry")

    @staticmethod
    def _quota_percent(report: object) -> float | None:
        mailbox = getattr(report, "mailbox", None)
        if mailbox is None:
            return None
        for candidate in (
            getattr(mailbox, "percent_used", None),
            getattr(mailbox, "send_threshold_percent", None),
        ):
            if isinstance(candidate, int | float) and math.isfinite(candidate):
                return max(float(candidate), 0.0)
        used = getattr(mailbox, "used_bytes", None)
        capacity = getattr(getattr(report, "effective_capacity", None), "value_bytes", None)
        if isinstance(used, int) and isinstance(capacity, int) and capacity > 0:
            return max(used / capacity * 100.0, 0.0)
        return None

    def _fail_mailbox_refresh(self, generation: int, message: str) -> None:
        if generation != self._mailbox_generation:
            return
        if self._mailbox_timeout is not None:
            self._mailbox_timeout.stop()
            self._mailbox_timeout = None
        self.ui.busy = None
        self.ui.online = False
        self._header_connection = "Connection unavailable"
        self._header_account_detail = self._header_connection
        self._refresh_shell_status()
        self._set_status(f"{message} · F5 retry · Ctrl+F5 reconnect")

    def _mailbox_load_timed_out(self, generation: int) -> None:
        if generation != self._mailbox_generation:
            return
        self._mailbox_generation += 1
        self.ui.busy = None
        self.ui.online = False
        self._header_connection = "Connection timed out"
        self._header_account_detail = self._header_connection
        self._refresh_shell_status()
        self._set_status(
            "Mailbox request timed out; its late result will be ignored · "
            "F5 retry · Ctrl+F5 reconnect"
        )

    def _render_status(self, status: Any, *, quota_label: str = "Storage unavailable") -> None:
        if isinstance(status, dict):
            version = status.get("exchange_version") or "Exchange"
            count = status.get("inbox_count")
            count_text = f" · Inbox {count}" if count is not None else ""
            self._header_connection = "Connected"
            self._header_account_detail = f"Connected · {sanitize_terminal(version)}{count_text}"
            self._header_quota_label = sanitize_terminal(quota_label, max_length=160)
            self._refresh_shell_status()

    def _format_mail_timestamp(self, value: datetime | None) -> str:
        return format_timestamp(
            value,
            pattern=self.context.config.datetime_format,
            timezone=self.context.config.timezone,
        )

    def _render_messages(self, messages: Any) -> None:
        self._search_heading_label: str | None = None
        self._messages = tuple(messages or ())
        self._render_message_rows(self._messages)
        if not self._messages:
            self._pending_session_position = None

    def _fit_message_columns(self, *, repopulate: bool = True) -> None:
        try:
            table = self.query_one("#message-table", DataTable)
        except NoMatches:
            return
        width = max(18, table.content_size.width - 1)
        fitted = fitted_columns(self._message_columns, width, table.cell_padding)
        changed = tuple(key.value for key in table.columns) != tuple(c.id for c, _ in fitted)
        if changed:
            table.clear(columns=True)
            for column, _ in fitted:
                table.add_column(
                    "" if column.id == "state" else COLUMN_LABELS[column.id], key=column.id
                )
        for displayed_column, (_, size) in zip(table.columns.values(), fitted, strict=True):
            displayed_column.width = size
            displayed_column.auto_width = False
        table.refresh(layout=True)
        if changed and repopulate:
            self._render_message_rows(self._message_page_rows)

    def _render_message_rows(self, messages: Any) -> None:
        self._message_page_rows = tuple(messages or ())
        self._visible_messages = group_message_page(
            self._message_page_rows, enabled=self._group_conversations
        )
        positions = (
            conversation_page_positions(self._visible_messages)
            if self._group_conversations
            else (None,) * len(self._visible_messages)
        )
        grouping_label = " · grouped in this page" if self._group_conversations else ""
        self.query_one("#message-heading", Static).update(
            f"{sanitize_terminal(getattr(self, '_search_heading_label', None) or self.ui.folder)}"
            f" · {len(self._visible_messages)} messages"
            f"{grouping_label}"
        )
        visible_references = {
            item.reference for item in self._visible_messages if isinstance(item, MessageSummary)
        }
        self._selected_message_references.intersection_update(visible_references)
        table = self.query_one("#message-table", DataTable)
        old_ref = self.ui.selected_reference
        # Replacing a page temporarily highlights its first row. Publish only
        # the final cursor, and make it usable before queued widget events run.
        with table.prevent(DataTable.RowHighlighted):
            self._fit_message_columns(repopulate=False)
            table.clear()
            restored = self._pending_session_position if self._visible_messages else None
            selected_row = (
                min(restored.message_row, max(0, len(self._visible_messages) - 1))
                if restored is not None
                and restored.folder == self.ui.folder
                and old_ref is None
                and not self.ui.search
                else 0
            )
            for index, item in enumerate(self._visible_messages):
                if isinstance(item, MessageSummary):
                    reference = item.reference
                    selected = "✓" if reference in self._selected_message_references else " "
                    is_read = item.is_read
                    flagged = item.flagged
                    important = item.importance is Importance.HIGH
                    unread = selected + ("●" if not is_read else " ")
                    sender = item.sender.name or item.sender.email if item.sender else ""
                    received = self._format_mail_timestamp(item.received_at)
                    size = str(item.size or "")
                    subject = item.subject or "(no subject)"
                    sent = self._format_mail_timestamp(item.sent_at)
                    attachments = "Yes" if item.has_attachments else ""
                    importance = item.importance.value
                    folder = item.folder_path or ""
                    conversation = item.conversation_id or ""
                else:
                    reference = str(item.get("reference", index))
                    selected = "✓" if reference in self._selected_message_references else " "
                    is_read = bool(item.get("is_read", False))
                    flagged = bool(item.get("flagged", False))
                    important = str(item.get("importance", "")).casefold() == "high"
                    unread = selected + ("●" if not is_read else " ")
                    sender = str(item.get("sender", ""))
                    received = str(item.get("received_at", ""))
                    size = str(item.get("size", ""))
                    subject = str(item.get("subject", "(no subject)"))
                    sent = str(item.get("sent_at", ""))
                    attachments = "Yes" if item.get("has_attachments") else ""
                    importance = str(item.get("importance", "normal"))
                    folder = str(item.get("folder_path", ""))
                    conversation = str(item.get("conversation_id", ""))
                position = positions[index]
                if position is not None:
                    subject = f"[{position[0]}/{position[1]}] {subject}"
                runtime_theme = getattr(self, "_runtime_theme", self.active_theme)
                palette = runtime_theme.palette
                foreground = (
                    palette.important
                    if important
                    else palette.flagged
                    if flagged
                    else palette.read_message
                    if is_read
                    else palette.unread_message
                )
                emphasis = "bold " if not is_read and runtime_theme.style.bold_text else ""
                cell_style = f"{emphasis}{foreground}"
                row_height = (
                    2 if getattr(self, "_message_row_density", "comfortable") == "spacious" else 1
                )
                cells = {
                    "state": Text(unread, style=cell_style),
                    "sender": Text(
                        sanitize_terminal(sender, max_length=300),
                        style=cell_style,
                        no_wrap=True,
                        overflow="ellipsis",
                    ),
                    "subject": Text(
                        sanitize_terminal(subject, max_length=300),
                        style=cell_style,
                        no_wrap=True,
                        overflow="ellipsis",
                    ),
                    "received": sanitize_terminal(received),
                    "size": sanitize_terminal(size),
                    "sent": sanitize_terminal(sent),
                    "attachments": attachments,
                    "flagged": "Yes" if flagged else "",
                    "importance": sanitize_terminal(importance),
                    "folder": sanitize_terminal(folder, max_length=300),
                    "conversation": sanitize_terminal(conversation, max_length=300),
                }
                table.add_row(
                    *(cells[str(key.value)] for key in table.columns),
                    key=reference,
                    height=row_height,
                )
                if reference == old_ref:
                    selected_row = index
            if table.row_count:
                table.move_cursor(row=selected_row)
        cursor_reference = (
            str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            if table.row_count
            else None
        )
        if cursor_reference != self.ui.selected_reference:
            self._cancel_automatic_read()
        self.ui.selected_reference = cursor_reference
        if restored is not None and not self._session_position_scheduled:
            self._session_position_scheduled = True
            self.call_after_refresh(
                self._restore_message_position, table, restored, table.cursor_row
            )
        self.call_after_refresh(self._capture_session)

    def _restore_message_position(
        self, table: DataTable[str], restored: MailSession, expected_row: int
    ) -> None:
        try:
            if (
                restored.folder == self.ui.folder
                and not self.ui.search
                and table.cursor_row == expected_row
            ):
                table.scroll_to(
                    y=restored.message_scroll, animate=False, force=True, immediate=True
                )
        finally:
            self._pending_session_position = None
            self._session_position_scheduled = False
            self._capture_session()

    @on(DataTable.RowHighlighted, "#message-table")
    def highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        reference = str(event.row_key.value)
        if reference != self.ui.selected_reference:
            self._cancel_automatic_read()
        self.ui.selected_reference = reference
        self.call_after_refresh(self._capture_session)
        table = self.query_one("#message-table", DataTable)
        if table.has_focus and table.row_count and table.cursor_row == table.row_count - 1:
            self._load_next_mailbox_page()

    @on(events.MouseScrollDown, "#message-table")
    def message_table_scrolled_down(self, event: events.MouseScrollDown) -> None:
        if event.shift or event.ctrl:
            return
        table = self.query_one("#message-table", DataTable)
        if table.scroll_y >= table.max_scroll_y:
            self._load_next_mailbox_page(mouse=True)

    @on(MailMessageTable.MoreRequested)
    def message_table_more_requested(self) -> None:
        self._load_next_mailbox_page()

    @on(DataTable.RowSelected, "#message-table")
    def selected(self, event: DataTable.RowSelected) -> None:
        self.ui.selected_reference = str(event.row_key.value)
        self.load_message(self.ui.selected_reference)

    def load_message(self, reference: str, *, mode: str = "auto") -> None:
        self._cancel_automatic_read()
        self._message_generation += 1
        generation = self._message_generation
        self.ui.busy = "message"
        self._pending_reader_mode = mode
        self._set_status(f"Loading {sanitize_terminal(reference, max_length=80)}… · Esc cancels")
        if self._message_timeout is not None:
            self._message_timeout.stop()
        self._message_timeout = self.set_timer(
            self._load_timeout_seconds,
            lambda: self._message_load_timed_out(generation),
        )
        self._load_message_worker(generation, reference, mode)

    @work(thread=True, exclusive=True, group="message-preview")
    def _load_message_worker(self, generation: int, reference: str, mode: str) -> None:
        try:
            gateway = self._ensure_gateway()
            # Fetching a stale or cancelled preview must never mutate read state.
            detail = gateway.show_message(reference)
            self.call_from_thread(
                self._complete_message_load,
                generation,
                reference,
                mode,
                detail,
            )
        except Exception as exc:
            self.call_from_thread(
                self._fail_message_load,
                generation,
                _safe_failure(exc, action="Message unavailable"),
            )

    def _complete_message_load(
        self,
        generation: int,
        reference: str,
        mode: str,
        detail: Any,
    ) -> None:
        if generation != self._message_generation:
            return
        if self._message_timeout is not None:
            self._message_timeout.stop()
            self._message_timeout = None
        self._message_detail = detail
        self._message_detail_reference = reference
        self.ui.busy = None
        self._render_message(detail, mode=mode)
        self._set_status(
            f"Message {sanitize_terminal(reference, max_length=60)} loaded without remote content"
        )

    def _fail_message_load(self, generation: int, message: str) -> None:
        if generation != self._message_generation:
            return
        if self._message_timeout is not None:
            self._message_timeout.stop()
            self._message_timeout = None
        self.ui.busy = None
        self._set_status(f"{message} · Enter retry · Ctrl+F5 reconnect")

    def _message_load_timed_out(self, generation: int) -> None:
        if generation != self._message_generation:
            return
        self._message_generation += 1
        self._cancel_automatic_read()
        self.ui.busy = None
        self._set_status(
            "Message request timed out; its late result will be ignored · Enter retries"
        )

    def _cancel_automatic_read(self) -> None:
        if self._automatic_read_timer is not None:
            self._automatic_read_timer.stop()
            self._automatic_read_timer = None
        if self._automatic_read_request is not None:
            self._automatic_read_request.cancel.set()
            self._automatic_read_request = None

    def _queue_automatic_read(
        self, detail: Any, container: Widget, selector: str | None = None
    ) -> None:
        if detail is not self._message_detail:
            return
        self.call_after_refresh(
            self._schedule_automatic_read,
            self._message_generation,
            self._message_detail_reference,
            container,
            selector,
        )

    def _schedule_automatic_read(
        self,
        generation: int,
        reference: str | None,
        container: Widget,
        selector: str | None,
    ) -> None:
        if reference is None or not container.is_mounted:
            return
        try:
            view = container.query_one(selector) if selector else container
        except NoMatches:
            return
        try:
            screen = view.screen
        except NoScreen:
            return
        request = _AutomaticReadRequest(generation, reference, view, screen.stack_updates, Event())
        if not self._automatic_read_eligible(request):
            return
        self._cancel_automatic_read()
        self._automatic_read_request = request
        delay = float(cast(float, self._setting_value("mail.mark_read_delay_seconds", 0.0)))
        if delay > 0:
            self._automatic_read_timer = self.set_timer(
                delay, lambda: self._start_automatic_read(request)
            )
        else:
            self.call_later(self._start_automatic_read, request)

    def _automatic_read_eligible(self, request: _AutomaticReadRequest) -> bool:
        detail = self._message_detail
        try:
            screen = request.view.screen
        except NoScreen:
            return False
        return bool(
            self.is_running
            and not self.offline
            and not request.cancel.is_set()
            and request.generation == self._message_generation
            and request.generation != self._automatic_read_attempted_generation
            and request.reference == self._message_detail_reference
            and request.reference == self.ui.selected_reference
            and request.reference not in self._automatic_read_inflight
            and isinstance(detail, MessageDetail)
            and detail.summary.source is ItemSource.LIVE
            and not detail.summary.is_read
            and bool(self._setting_value("mail.mark_read_on_open", False))
            and request.view.is_attached
            and screen is self.screen
            and screen.stack_updates == request.screen_updates
            and request.view.is_on_screen
        )

    def _start_automatic_read(self, request: _AutomaticReadRequest) -> None:
        if self._automatic_read_request is not request:
            return
        self._automatic_read_timer = None
        if self._automatic_read_eligible(request):
            self._automatic_mark_read_worker(request)
        else:
            self._cancel_automatic_read()

    def _claim_automatic_read(self, request: _AutomaticReadRequest) -> bool:
        # Recheck on the UI thread when the worker actually starts, including a
        # queued worker whose message/view may already have changed.
        if self._automatic_read_request is not request or not self._automatic_read_eligible(
            request
        ):
            return False
        self._automatic_read_attempted_generation = request.generation
        self._automatic_read_inflight.add(request.reference)
        return True

    @work(thread=True, exclusive=False, group="automatic-read-state")
    @coordinated_write()
    def _automatic_mark_read_worker(self, request: _AutomaticReadRequest) -> None:
        claimed = False
        started = False
        error: Exception | None = None
        try:
            claimed = bool(self.call_from_thread(self._claim_automatic_read, request))
            if not claimed:
                return
            gateway = self._ensure_gateway()
            if request.cancel.is_set():
                return
            service = MessageService(gateway)
            plan = service.plan_mark_read((request.reference,))
            started = True
            service.execute_mutation(plan)
        except Exception as exc:
            error = exc
        finally:
            if claimed:
                with contextlib.suppress(RuntimeError):
                    self.call_from_thread(self._finish_automatic_read, request, started, error)

    def _finish_automatic_read(
        self, request: _AutomaticReadRequest, started: bool, error: Exception | None
    ) -> None:
        self._automatic_read_inflight.discard(request.reference)
        if self._automatic_read_request is request:
            self._cancel_automatic_read()
        if error is not None:
            message = _safe_failure(error, action="Automatic read-state update unavailable")
            if started:
                message += " It may have completed; refresh before another change."
            self._set_status(message)
            return
        if not started:
            return
        # The exact target's acknowledged state may be updated even if the user
        # moved on. Never apply it to the newly selected message or replay a failure.
        for attribute in ("_messages", "_message_page_rows", "_visible_messages"):
            setattr(
                self,
                attribute,
                tuple(
                    replace(item, is_read=True)
                    if isinstance(item, MessageSummary) and item.reference == request.reference
                    else item
                    for item in getattr(self, attribute)
                ),
            )
        detail = self._message_detail
        if self._message_detail_reference == request.reference and isinstance(
            detail, MessageDetail
        ):
            self._message_detail = replace(detail, summary=replace(detail.summary, is_read=True))
        self._render_message_rows(self._message_page_rows)

    @staticmethod
    def _message_title(detail: Any) -> str:
        if isinstance(detail, MessageDetail):
            return detail.summary.subject or "(no subject)"
        return "Message"

    def _format_message(self, detail: Any) -> str:
        if isinstance(detail, MessageDetail):
            sender = detail.summary.sender
            received_at = detail.summary.received_at
            lines = [
                f"[b]{escape(sanitize_terminal(detail.summary.subject or '(no subject)'))}[/b]",
                f"From: {escape(sanitize_terminal(sender.name or sender.email if sender else ''))}",
                "To: " + ", ".join(escape(sanitize_terminal(item.email)) for item in detail.to),
                f"[dim]{escape(self._format_mail_timestamp(received_at))}[/dim]",
            ]
            if detail.cc:
                lines.append(
                    "Cc: " + ", ".join(escape(sanitize_terminal(item.email)) for item in detail.cc)
                )
            if detail.summary.importance is not Importance.NORMAL:
                lines.append(f"Importance: {detail.summary.importance.value}")
            if detail.attachments:
                lines.append(f"Attachments: {len(detail.attachments)} · use Commands to save")
            lines.extend(("", escape(sanitize_terminal(detail.body_text, max_length=1_000_000))))
            return "\n".join(lines)
        return escape(sanitize_terminal(detail, max_length=1_000_000))

    def _format_message_details(self, detail: Any) -> str:
        if not isinstance(detail, MessageDetail):
            return escape(sanitize_terminal(detail, max_length=1_000_000))

        def addresses(values: Sequence[Address]) -> str:
            return ", ".join(
                f"{item.name} <{item.email}>" if item.name else item.email for item in values
            )

        summary = detail.summary
        values: list[tuple[str, object]] = [
            ("Subject", summary.subject or "(no subject)"),
            ("Sender", addresses((summary.sender,)) if summary.sender else ""),
            ("Reply-To", addresses(detail.reply_to)),
            ("To", addresses(detail.to)),
            ("Cc", addresses(detail.cc)),
            ("Bcc", addresses(detail.bcc)),
            ("Sent", self._format_mail_timestamp(summary.sent_at)),
            ("Received", self._format_mail_timestamp(summary.received_at)),
            ("Importance", summary.importance.value),
            ("Sensitivity", detail.sensitivity.value),
            ("Read", summary.is_read),
            ("Flagged", summary.flagged),
            ("Categories", ", ".join(summary.categories)),
            ("Folder", summary.folder_path or ""),
            ("Size", human_bytes(summary.size) if summary.size is not None else "unknown"),
            ("Attachments", len(detail.attachments)),
            ("Internet message ID", detail.internet_message_id or ""),
            ("Conversation ID", summary.conversation_id or ""),
            ("In-Reply-To", detail.in_reply_to or ""),
            ("References", " ".join(detail.references)),
            ("Local reference", summary.reference),
            ("EWS item ID", detail.ews_id or ""),
            ("EWS change key", detail.change_key or ""),
            ("Links", "\n".join(detail.links)),
        ]
        lines = [
            f"{label}: {sanitize_terminal(value, max_length=20_000)}" for label, value in values
        ]
        if detail.attachments:
            lines.append("\nAttachment metadata:")
            lines.extend(
                f"- {item.name} · {human_bytes(item.size) if item.size is not None else 'unknown'}"
                f" · {item.content_type or 'unknown'} · ref {item.reference}"
                for item in detail.attachments
            )
        if detail.extended_properties:
            lines.append("\nExtended properties:")
            lines.extend(
                f"- {key}: {sanitize_terminal(value, max_length=2_000)}"
                for key, value in sorted(detail.extended_properties.items())
            )
        return escape(sanitize_terminal("\n".join(lines), max_length=1_000_000))

    def _render_message(self, detail: Any, *, mode: str = "auto") -> None:
        if mode == "details":
            self.push_screen(
                ReaderScreen(
                    f"Message details · {self._message_title(detail)}",
                    Text.from_markup(self._format_message_details(detail)),
                )
            )
            return
        rendered = Text.from_markup(self._format_message(detail))
        title = self._message_title(detail)
        target = mode
        if target == "auto":
            preferred = self._reader_mode
            if preferred == "floating":
                target = "floating"
            elif preferred == "fullscreen":
                target = "full"
            elif preferred == "preview":
                target = "pane" if self.has_class("wide") and self._preview_visible else "full"
            elif preferred == "external":
                target = "external"
            else:
                target = "ask"
        if target == "ask":
            self.push_screen(
                ReaderModeScreen(),
                lambda choice: (
                    self._render_message(detail, mode=choice) if choice is not None else None
                ),
            )
        elif target == "external":
            # This remains high-friction even when it is the persisted default:
            # the export/OS hand-off path presents its own scoped confirmation.
            self._preview_open_selected_externally()
        if target == "floating":
            self._show_floating_reader(
                title,
                rendered,
                on_ready=lambda floating: self._queue_automatic_read(
                    detail, floating, "#floating-reader-content"
                ),
            )
        elif target == "full":
            reader = ReaderScreen(title, rendered)
            self._after_reader_mount(
                self.push_screen(reader),
                lambda: self._queue_automatic_read(detail, reader, "#reader-screen-content"),
            )
        elif target == "pane" and self.has_class("wide") and self._preview_visible:
            preview = self.query_one("#preview", Static)
            preview.update(rendered)
            self._queue_automatic_read(detail, preview)
        elif target == "pane":
            # A saved preview preference must not make opening a message appear
            # to do nothing after the terminal narrows or the pane is hidden.
            reader = ReaderScreen(title, rendered)
            self._after_reader_mount(
                self.push_screen(reader),
                lambda: self._queue_automatic_read(detail, reader, "#reader-screen-content"),
            )

    @work(exclusive=False, group="reader-view-mount", exit_on_error=False)
    async def _after_reader_mount(
        self, completion: Awaitable[Any], on_ready: Callable[[], None]
    ) -> None:
        await completion
        on_ready()

    def _show_floating_reader(
        self,
        title: str,
        content: Any,
        *,
        on_ready: Callable[[FloatingReader], None] | None = None,
    ) -> FloatingReader:
        try:
            reader = self.query_one("#floating-reader", FloatingReader)
        except NoMatches:
            reader = FloatingReader(title, content)
            completion = self.mount(reader)
            self._floating_reader_mounts[reader] = completion
            if on_ready is not None:
                self._after_reader_mount(
                    completion, lambda: self._floating_reader_ready(reader, on_ready)
                )
        else:
            reader.set_message(title, content)
            if on_ready is not None:
                pending = self._floating_reader_mounts.get(reader)
                if pending is not None:
                    self._after_reader_mount(
                        pending, lambda: self._floating_reader_ready(reader, on_ready)
                    )
                else:
                    on_ready(reader)
        self.call_after_refresh(self.query_one("#message-table", DataTable).focus)
        return reader

    def _floating_reader_ready(
        self, reader: FloatingReader, on_ready: Callable[[FloatingReader], None]
    ) -> None:
        self._floating_reader_mounts.pop(reader, None)
        if not reader.is_attached:
            return
        # A new message can replace the pending view while its children mount.
        # Apply the latest content before testing whether its reader may mark it.
        reader.set_message(reader.reader_title, reader.reader_content)
        on_ready(reader)

    def action_detach_reader(self) -> None:
        if not self._needs_message("Open floating reader"):
            return
        reference = self.ui.selected_reference or ""
        if self._message_detail is not None and self._message_detail_reference == reference:
            self._render_message(self._message_detail, mode="floating")
        else:
            self.load_message(reference, mode="floating")

    @staticmethod
    def _bounded_lines(lines: list[str], *, empty: str) -> str:
        return "\n".join(lines) if lines else empty

    def _calendar_area(self) -> str:
        view = CalendarService(
            self._ensure_gateway(),
            display_timezone=self.context.config.timezone,
            default_limit=min(self.context.config.page_size, 100),
        ).upcoming(days=30, limit=min(self.context.config.page_size, 100))
        lines = [
            f"{event.start.isoformat(sep=' ', timespec='minutes')}  "
            f"{event.subject or '(no subject)'}"
            + (f"  ·  {event.location}" if event.location else "")
            for event in view.events
        ]
        return self._bounded_lines(lines, empty="No events in the next 30 days.")

    def _contacts_area(self) -> str:
        contacts = ContactsService(
            self._ensure_gateway(),
            default_limit=min(self.context.config.page_size, 100),
        ).list(limit=min(self.context.config.page_size, 100))
        lines = [
            f"{contact.display_name or '(unnamed contact)'}"
            + (
                "  ·  " + ", ".join(address.email for address in contact.emails)
                if contact.emails
                else ""
            )
            for contact in contacts
        ]
        return self._bounded_lines(lines, empty="No contacts in this bounded view.")

    def _tasks_area(self) -> str:
        tasks = TaskService(
            self._ensure_gateway(),
            default_limit=min(self.context.config.page_size, 100),
        ).list(limit=min(self.context.config.page_size, 100))
        lines = [
            f"[{task.status}] {task.subject or '(no subject)'}"
            + (f"  ·  due {task.due_date.isoformat()}" if task.due_date else "")
            for task in tasks
        ]
        return self._bounded_lines(lines, empty="No tasks in this bounded view.")

    def _scheduler_service(self) -> SchedulerService:
        def sender(draft: DraftData, **_kwargs: Any) -> object:
            return self._ensure_gateway().send(draft)

        return SchedulerService(
            self.context.state,
            DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            ),
            sender,
            timezone=self.context.config.timezone,
            retry_attempts=self.context.config.retry_attempts,
            retry_base_seconds=self.context.config.retry_base_seconds,
            retry_max_seconds=self.context.config.retry_max_seconds,
            maximum_attachment_bytes=self.context.config.max_attachment_bytes,
        )

    def _draft_attachment_backend(self) -> ServiceDraftAttachmentBackend:
        drafts = DraftService(
            self.context.state,
            self.context.paths.recovery_dir,
            maximum_attachment_bytes=self.context.config.max_attachment_bytes,
        )
        deferred_gateway = _DeferredGateway(self._ensure_gateway)
        return ServiceDraftAttachmentBackend(
            drafts,
            synchronizer=DraftSyncCoordinator(drafts, deferred_gateway),
            attachments=AttachmentService(
                deferred_gateway,
                max_download_bytes=self.context.config.max_attachment_bytes,
            ),
            online=not self.offline,
            maximum_local_copy_bytes=self.context.config.max_attachment_bytes,
        )

    def _open_draft_attachment_workspace(
        self,
        owner_reference: str | None = None,
        *,
        attachment_action: str | None = None,
    ) -> None:
        current = self._route_screen(RouteTarget.DRAFTS)
        if isinstance(current, DraftAttachmentScreen):
            if attachment_action:
                current.queue_attachment_action(attachment_action)
            if owner_reference:
                current.show_attachments_for(owner_reference)
            return
        screen = DraftAttachmentScreen(
            self._draft_attachment_backend(),
            page_size=min(max(self.context.config.page_size, 50), 1_000),
            download_directory=self.context.config.downloads,
            collision_policy=AttachmentPreferences.from_settings(
                self._settings_service()
            ).collision,
            initial_owner_reference=owner_reference,
            offline=self.offline,
        )
        if attachment_action:
            screen.queue_attachment_action(attachment_action)
        self.push_screen(screen)

    @work(thread=True, exclusive=False, group="schedule-draft")
    @coordinated_write()
    def _schedule_workspace_draft(self, reference: str, expression: str) -> None:
        try:
            scheduled = self._scheduler_service().schedule(
                reference,
                expression,
                timezone=self.context.config.timezone,
                require_final_confirmation=True,
            )
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Draft scheduling unavailable"),
            )
            return

        def scheduled_ready() -> None:
            self._set_status(
                "Draft scheduled for "
                f"{scheduled.scheduled_for.isoformat()} with final confirmation."
            )
            self.set_timer(0.08, lambda: self._open_area("scheduled"))

        self.call_from_thread(scheduled_ready)

    def _scheduled_area(self) -> str:
        records = self._scheduler_service().list(limit=100)
        lines = [
            f"[{record.message.state.value}] {record.message.scheduled_for.isoformat()}  "
            f"{record.message.reference}"
            for record in records
        ]
        return self._bounded_lines(lines, empty="No local scheduled messages.")

    def _backups_area(self) -> str:
        backups = BackupService(self.context.paths.backup_dir, state=self.context.state).list(
            limit=100,
            inspect_corrupt_sizes=False,
        )
        lines = [
            f"[{backup.state}] {backup.backup_id}  ·  {backup.item_count} item(s)  ·  "
            f"{human_bytes(backup.stored_bytes)}" + ("  ·  encrypted" if backup.encrypted else "")
            for backup in backups
        ]
        return self._bounded_lines(lines, empty="No local backups.")

    def _storage_area(self) -> str:
        reports = QuotaService.local_disk_usage(
            {
                "data": self.context.paths.data_dir,
                "cache": self.context.paths.cache_dir,
                "search index": self.context.paths.index_database,
                "backups": self.context.paths.backup_dir,
                "downloads": self.context.config.downloads,
            }
        )
        lines = [
            (
                f"{report.label}: {human_bytes(report.free_bytes)} free of "
                f"{human_bytes(report.total_bytes)}  ·  {report.path}"
                if report.available
                else f"{report.label}: unavailable"
            )
            for report in reports
        ]
        lines.append(
            "Mailbox total capacity is unknown unless Exchange reports it. "
            "Open Storage to refresh the bounded usage estimate and independent thresholds."
        )
        return "\n".join(lines)

    def _activity_area(self) -> str:
        history = NotificationDispatcher([], state=self.context.state).history(limit=100)
        self.context.state.initialize()
        with self.context.state.connection() as connection:
            checkpoints = connection.execute(
                """
                SELECT scope, last_success_at, last_error, updated_at
                FROM sync_checkpoints
                WHERE scope LIKE 'monitor:%'
                ORDER BY updated_at DESC LIMIT 20
                """
            ).fetchall()
        lines = [
            "Monitoring configuration: "
            + ("enabled" if self.context.config.monitoring_enabled else "disabled"),
            f"Configured poll interval: {self.context.config.poll_interval_seconds} seconds",
            (
                "Watcher strategy: detected when gucmail watch starts; "
                "no background process is assumed."
            ),
            f"Saved monitoring checkpoints: {len(checkpoints)} (showing up to 20)",
        ]
        lines.extend(
            f"- {str(row['scope']).rsplit(':', 1)[-1]}: "
            f"last success {row['last_success_at'] or 'never'}; "
            f"last error {'recorded (details omitted)' if row['last_error'] else 'none'}; "
            f"updated {row['updated_at'] or 'never'}"
            for row in checkpoints
        )
        lines.extend(("", "Recent notification outcomes (up to 100):"))
        notification_lines = [
            f"[{row['state']}] {row['created_at']}  {row['provider']}  {row['event_id']}"
            + (f"  ·  {row['detail']}" if row.get("detail") else "")
            for row in history
        ]
        lines.extend(notification_lines or ["No local notification delivery history."])
        return "\n".join(lines)

    def _settings_area(self) -> str:
        values = self.context.config.redacted_dict()
        return "\n".join(f"{key}: {values[key]}" for key in sorted(values))

    def _contacts_backend(self) -> ContactsServiceBackend:
        from gucmail.services.contact_history import ContactHistoryIndex
        from gucmail.services.contact_preferences import ContactOperationPreferences

        page_size = min(max(self.context.config.page_size, 1), 200)
        deferred_gateway = _DeferredGateway(self._ensure_gateway)
        return ContactsServiceBackend(
            ContactsService(
                deferred_gateway,
                correspondence_gateway=deferred_gateway,
                default_limit=page_size,
                operation_preferences=lambda: ContactOperationPreferences.from_settings(
                    self._settings_service()
                ),
                history_index=ContactHistoryIndex(self.context.state),
            )
        )

    def _open_unanswered(self) -> None:
        from gucmail.services.unanswered import UnansweredService
        from gucmail.tui.unanswered_screen import UnansweredScreen

        def open_message(reference: str) -> None:
            self.ui.selected_reference = reference
            self.load_message(reference, mode="full")

        self.push_screen(
            UnansweredScreen(
                UnansweredService(_DeferredGateway(self._ensure_gateway)),
                address=self.context.config.email or "",
                open_message=open_message,
            )
        )

    def _open_correspondents(self, order: str) -> None:
        self.push_screen(
            CorrespondentsScreen(
                self._contacts_backend(),
                order=order,
                operation_preferences=lambda: ContactOperationPreferences.from_settings(
                    self._settings_service()
                ),
                page_size=min(max(self.context.config.page_size, 20), 100),
                offline=self.offline,
            )
        )

    def _open_backup_area(
        self,
        *,
        selected_messages: Sequence[str] = (),
        default_kind: str | None = None,
        default_folders: Sequence[str] = (),
        default_destination: str | None = None,
    ) -> None:
        online = not self.offline
        configured_destination = self._setting_value("backup.default_destination", None)
        backup_destination = (
            default_destination.strip()
            if isinstance(default_destination, str) and default_destination.strip()
            else str(configured_destination).strip()
            if isinstance(configured_destination, str) and configured_destination.strip()
            else str(self.context.paths.backup_dir)
        )
        configured_retention = self._setting_value("backup.retention_count", 5)
        backup_retention = (
            int(configured_retention) if isinstance(configured_retention, int | float) else 5
        )
        self.push_screen(
            BackupScreen(
                BackupAreaBackend(
                    backup_destination,
                    state=self.context.state,
                    source_factory=(lambda: self._ensure_gateway()) if online else None,
                ),
                default_destination=backup_destination,
                default_kind=(
                    "selected-messages"
                    if selected_messages
                    else default_kind or str(self._setting_value("backup.default_kind", "full"))
                ),
                default_encrypted=bool(self._setting_value("backup.encryption_enabled", False)),
                default_verify_after_create=bool(
                    self._setting_value("backup.verify_after_create", True)
                ),
                retention_count=backup_retention,
                default_folders=default_folders,
                available_folders=tuple(
                    dict.fromkeys(
                        ("inbox", "sent", "drafts", "deleted", "junk", *self._folder_paths.values())
                    )
                ),
                selected_messages=selected_messages,
                offline=self.offline,
            )
        )

    def _backup_repository_path(self) -> Path:
        configured = self._setting_value("backup.default_destination", None)
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser().resolve(strict=False)
        return self.context.paths.backup_dir

    def _open_local_search(
        self,
        *,
        mode: LocalSearchMode | str | None = None,
        query: str = "",
        reference: str | None = None,
        fuzzy: bool | None = None,
        exact: bool = False,
        sender: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        minimum_size: int | None = None,
        maximum_size: int | None = None,
        attachment_extension: str | None = None,
        backup_id: str | None = None,
        auto_run: bool = False,
    ) -> None:
        service = LocalSearchService(
            SearchIndex(self.context.paths.index_database),
            BackupService(self._backup_repository_path(), state=self.context.state),
            semantic_enabled=bool(self._setting_value("search.semantic_enabled", False)),
            semantic=self._semantic_service(),
        )
        self.push_screen(
            LocalSearchScreen(
                ServiceLocalSearchBackend(service),
                initial_mode=mode if mode is not None else self._last_search_scope,
                initial_query=query,
                initial_reference=reference,
                initial_fuzzy=(
                    bool(self._setting_value("search.fuzzy_enabled", True))
                    if fuzzy is None and not exact
                    else bool(fuzzy)
                ),
                minimum_score=int(str(self._setting_value("search.fuzzy_threshold", 82))),
                initial_exact=exact,
                initial_sender=sender,
                initial_after=after,
                initial_before=before,
                initial_minimum_size=minimum_size,
                initial_maximum_size=maximum_size,
                initial_attachment_extension=attachment_extension,
                initial_backup_id=backup_id,
                auto_run=auto_run,
            )
        )

    @on(LocalSearchScopeChanged)
    def local_search_scope_changed(self, event: LocalSearchScopeChanged) -> None:
        if event.mode.value in SEARCH_SCOPES:
            self._last_search_scope = event.mode.value
            self._capture_session()

    def _semantic_service(self) -> SemanticSearchService:
        return SemanticSearchService(
            SearchIndex(self.context.paths.index_database),
            SemanticStore(
                self.context.paths.data_dir / "semantic.sqlite3",
                identity=account_identity(
                    self.context.config.email or "", self.context.config.endpoint
                ),
                enabled=bool(self._setting_value("search.semantic_enabled", False)),
            ),
        )

    def action_local_ocr(self) -> None:
        self.push_screen(OcrScreen(OcrService()))

    @on(LocalSearchSemanticSetupRequested)
    def local_search_semantic_setup_requested(
        self, event: LocalSearchSemanticSetupRequested
    ) -> None:
        event.stop()

        def set_enabled(enabled: bool) -> bool:
            service = self._settings_service()
            service.set("search.semantic_enabled", enabled)
            self.context.reload_config()
            return bool(service.value("search.semantic_enabled").effective_value)

        def refresh_search(_: object) -> None:
            current = self.screen
            if isinstance(current, LocalSearchScreen) and isinstance(
                current.backend, ServiceLocalSearchBackend
            ):
                semantic = self._semantic_service()
                current.backend.service.semantic = semantic
                current.backend.service.semantic_enabled = semantic.store.enabled

        self.push_screen(
            SemanticScreen(self._semantic_service(), set_enabled=set_enabled), refresh_search
        )

    def _close_runtime_for_cache_repair(self) -> None:
        """Release the reusable provider handle before clearing its derived identity cache."""

        if self._gateway_injected:
            reset = getattr(self.gateway, "reset_session", None)
            if callable(reset):
                reset()
        else:
            self.context.close()
            self.gateway = None

    def _open_area(self, area: str) -> None:
        if area == "layouts":
            self.action_layout_presets()
            return
        online = not self.offline
        page_size = min(max(self.context.config.page_size, 1), 200)
        deferred_gateway = _DeferredGateway(self._ensure_gateway)
        if area in {"drafts", "attachments"}:
            self._open_draft_attachment_workspace()
            return
        if area == "saved_searches":
            self._open_saved_searches()
            return
        if area == "local_search":
            self._open_local_search()
            return
        if self.offline and area in {"calendar", "contacts", "tasks"}:
            self.push_screen(
                OfflinePimScreen(
                    BackupService(self.context.paths.backup_dir, state=self.context.state),
                    area,
                    page_size=page_size,
                )
            )
            return
        if area == "calendar":
            try:
                display = CalendarDisplayPreferences.from_settings(self._settings_service())
                CalendarOperationPreferences.from_settings(self._settings_service())
            except ConfigurationError as exc:
                self._set_status("Calendar preferences: " + sanitize_terminal(str(exc)))
                return
            self.push_screen(
                CalendarScreen(
                    CalendarServiceBackend(
                        CalendarService(
                            deferred_gateway,
                            display_timezone=display.timezone,
                            default_limit=page_size,
                        )
                    ),
                    timezone=display.timezone,
                    display_preferences=lambda: CalendarDisplayPreferences.from_settings(
                        self._settings_service()
                    ),
                    operation_preferences=lambda: CalendarOperationPreferences.from_settings(
                        self._settings_service()
                    ),
                    creation_preferences=lambda: CalendarCreationPreferences.from_settings(
                        self._settings_service()
                    ),
                    working_time_preferences=lambda: CalendarWorkingTime.from_settings(
                        self._settings_service()
                    ),
                    page_size=page_size,
                    offline=self.offline,
                )
            )
            return
        if area == "delegates":
            if self.offline:
                self._set_status("Mailbox delegates require an Exchange connection.")
                return
            self.push_screen(
                DelegatesScreen(DelegateService(deferred_gateway), page_size=page_size)
            )
            return
        if area == "inbox_rules":
            if self.offline:
                self._set_status("Inbox rules require a live Exchange connection.")
                return
            self.push_screen(InboxRulesScreen(InboxRuleService(deferred_gateway)))
            return
        if area == "automatic_replies":
            if self.offline:
                self._set_status("Automatic replies require a live Exchange connection.")
                return
            self.push_screen(AutomaticRepliesScreen(AutomaticReplyService(deferred_gateway)))
            return
        if area == "contact_groups":
            self.push_screen(
                ContactGroupsScreen(
                    ContactGroupService(LocalContactGroups(self.context.state)),
                    ContactGroupService(deferred_gateway) if online else None,
                    page_size=page_size,
                )
            )
            return
        if area == "contacts":
            try:
                ContactsDisplayPreferences.from_settings(self._settings_service())
                ContactOperationPreferences.from_settings(self._settings_service())
            except ConfigurationError as exc:
                self._set_status("Contacts preferences: " + sanitize_terminal(str(exc)))
                return
            self.push_screen(
                ContactsScreen(
                    self._contacts_backend(),
                    display_preferences=lambda: ContactsDisplayPreferences.from_settings(
                        self._settings_service()
                    ),
                    operation_preferences=lambda: ContactOperationPreferences.from_settings(
                        self._settings_service()
                    ),
                    page_size=page_size,
                    offline=self.offline,
                )
            )
            return
        if area == "tasks":
            from gucmail.services.task_history import TaskHistory

            try:
                task_display = TaskDisplayPreferences.from_settings(self._settings_service())
            except ConfigurationError as exc:
                self._set_status("Task preferences: " + sanitize_terminal(str(exc)))
                return
            self.push_screen(
                TasksScreen(
                    TasksServiceBackend(
                        TaskService(deferred_gateway, default_limit=page_size),
                        show_completed=task_display.show_completed,
                        timezone=task_display.timezone,
                    ),
                    history_reader=TaskHistory(self.context.state).page,
                    view=task_display.default_view.value,
                    timezone=task_display.timezone,
                    creation_preferences=lambda: TaskCreationPreferences.from_settings(
                        self._settings_service()
                    ),
                    display_preferences=lambda: TaskDisplayPreferences.from_settings(
                        self._settings_service()
                    ),
                    show_completed=task_display.show_completed,
                    page_size=page_size,
                    offline=self.offline,
                )
            )
            return
        if area == "backups":
            self._open_backup_area()
            return
        if area == "storage":
            self.push_screen(
                StorageScreen(
                    StorageAreaBackend.from_context(self.context, online=online),
                    offline=self.offline,
                    analysis_limit=min(max(self.context.config.page_size, 25), 200),
                )
            )
            return
        if area == "diagnostics":
            self._open_diagnostics_area()
            return
        if area == "settings":
            self.push_screen(
                SettingsScreen(
                    SettingsAreaBackend.from_context(
                        self.context,
                        preview_handlers={
                            "layout.message_columns": self._preview_message_columns,
                            "appearance.theme": self._preview_settings_theme,
                        },
                        on_applied=self._settings_applied_callback,
                    ),
                    offline=self.offline,
                )
            )
            return
        if area == "startup":
            self.push_screen(StartupScreen(StartupService(self.context)))
            return
        if area == "notifications":
            webhook_secrets = SecretStore("webhooks")
            self.push_screen(
                NotificationSettingsScreen(
                    NotificationConfigurationService(self.context.state),
                    webhook_registry=WebhookRegistry(
                        self.context.state,
                        secret_store=webhook_secrets,
                    ),
                    webhook_delivery=None if self.offline else self._deliver_webhook_test,
                    recent_simulation_source=None if self.offline else self._recent_rule_sample,
                    automatic_replies_available=not self.offline,
                    desktop_setup=self._open_desktop_setup,
                )
            )
            return
        if area == "activity":
            self.push_screen(
                ActivityScreen(
                    ActivityAreaBackend.from_context(self.context, offline=self.offline),
                    offline=self.offline,
                    timezone=self.context.config.timezone,
                    datetime_format=self.context.config.datetime_format,
                )
            )
            return
        if area == "scheduled":
            scheduler = self._scheduler_service()
            self.push_screen(
                ScheduledScreen(
                    SchedulerServiceBackend(
                        scheduler,
                        online=online,
                        recurring=RecurringService(
                            self.context.state,
                            scheduler.drafts,
                            scheduler,
                            automation_enabled=True,
                        ),
                    ),
                    page_size=min(max(self.context.config.page_size, 50), 500),
                    offline=self.offline,
                )
            )
            return
        if area == "themes":
            self.push_screen(
                ThemeBrowserScreen(self.theme_browser_backend),
                lambda _result: self._refresh_appearance_settings(),
            )
            return
        if area == "branding":
            self.push_screen(
                BrandingBrowserScreen(self.branding_browser_backend),
                lambda _result: self._refresh_appearance_settings(),
            )
            return
        loaders: dict[str, tuple[str, Callable[[], str]]] = {}
        try:
            title, loader = loaders[area]
        except KeyError:
            self._set_status(f"Unknown application area: {sanitize_terminal(area)}")
            return
        self.push_screen(AreaScreen(title, loader))

    @on(Tree.NodeExpanded, "#folder-tree")
    def folder_expanded(self, event: Tree.NodeExpanded[Any]) -> None:
        data = event.node.data
        if not isinstance(data, dict) or data.get("kind") != "folder_branch":
            return
        reference = data.get("reference")
        if isinstance(reference, str):
            self._expanded_folders.add(reference)
            self._persist_folder_expansion()
        if not data.get("loaded") and not data.get("loading"):
            self._request_folder_children(
                parent_reference=str(reference) if reference else None,
                node_id=event.node.id,
            )

    @on(Tree.NodeCollapsed, "#folder-tree")
    def folder_collapsed(self, event: Tree.NodeCollapsed[Any]) -> None:
        data = event.node.data
        if not isinstance(data, dict) or data.get("kind") != "folder_branch":
            return
        reference = data.get("reference")
        if isinstance(reference, str):
            if self._folder_refresh_in_progress:
                return
            current_node_id = self._folder_nodes.get(reference)
            if current_node_id is not None and current_node_id != event.node.id:
                self._folder_refresh_preserve_expanded.discard(reference)
                return
            if reference in self._folder_refresh_preserve_expanded:
                self._folder_refresh_preserve_expanded.discard(reference)
                return
            self._folder_refresh_expanded_snapshot.discard(reference)
            self._expanded_folders.discard(reference)
            self._persist_folder_expansion()

    @on(Tree.NodeSelected, "#folder-tree")
    def folder_selected(self, event: Tree.NodeSelected[Any]) -> None:
        # Textual may deliver queued tree selection events during shell teardown.
        # Do not start a new mailbox request after the owning widgets are gone.
        if not self.is_running or not self.query("#status-bar"):
            return
        data = event.node.data
        if isinstance(data, dict) and data.get("kind") == "tools_branch":
            event.node.toggle()
        elif isinstance(data, dict) and data.get("kind") in {"folder", "folder_branch"}:
            if not data.get("reference"):
                return
            selected_reference = str(data["reference"])
            if data.get("kind") == "folder_branch":
                event.node.expand()
            if (
                self._folder_selection_to_restore is not None
                and selected_reference != self._folder_selection_to_restore
            ):
                self._folder_selection_to_restore = None
            self._activate_folder_location(selected_reference)
        elif isinstance(data, dict) and data.get("kind") == "folder_more":
            self._request_folder_children(
                parent_reference=(
                    str(data["reference"]) if data.get("reference") is not None else None
                ),
                node_id=event.node.parent.id if event.node.parent is not None else None,
                offset=int(data.get("offset") or 0),
                append=True,
            )
        elif isinstance(data, dict) and data.get("kind") == "local_filter":
            reference = str(data.get("reference") or "")
            if reference == "unread":
                filtered = tuple(
                    item
                    for item in self._messages
                    if isinstance(item, MessageSummary) and not item.is_read
                )
            else:
                filtered = tuple(
                    item
                    for item in self._messages
                    if isinstance(item, MessageSummary) and item.flagged
                )
            self._render_message_rows(filtered)
            self._set_status(f"{reference.title()}: {len(filtered)} in the current bounded page")
        elif isinstance(data, dict) and data.get("kind") == "area":
            area = str(data["reference"])
            if area == "search":
                self.action_search()
                self._set_status(
                    "Filter the current bounded page; use CLI search for deeper scope."
                )
            else:
                self._open_area(area)

    @on(Input.Changed, "#message-filter")
    def filter_messages(self, event: Input.Changed) -> None:
        self.ui.search = event.value
        query = event.value.casefold().strip()
        if not query:
            filtered = self._messages
        else:
            filtered = tuple(
                item
                for item in self._messages
                if query
                in " ".join(
                    (
                        item.subject if isinstance(item, MessageSummary) else str(item),
                        (
                            item.sender.name or item.sender.email
                            if isinstance(item, MessageSummary) and item.sender
                            else ""
                        ),
                    )
                ).casefold()
            )
        self._render_message_rows(filtered)
        self._set_status(f"Filter: {len(filtered)} of {len(self._messages)}" if query else "Ready")

    def _open_help(
        self,
        *,
        initial_page_id: str | None = None,
        initial_query: str = "",
        active_view: str | None = None,
    ) -> None:
        active = (
            active_view or type(self.screen).__name__.removesuffix("Screen").casefold() or "mail"
        )
        self.push_screen(
            HelpScreen(
                context=self._help_runtime_context(active),
                initial_page_id=initial_page_id,
                initial_query=initial_query,
                store=self._help_state_store(),
            )
        )

    def action_help(self) -> None:
        self._open_help()

    def _open_tutorials(self, lesson_id: str = "navigation") -> None:
        from gucmail.help.tutorials import TutorialProgress
        from gucmail.tui.tutorial_screen import TutorialScreen

        self.push_screen(
            TutorialScreen(
                TutorialProgress(self.context.state),
                initial_lesson=lesson_id,
                timezone=self.context.config.timezone,
            )
        )

    @on(HelpTutorialsRequested)
    def help_tutorials_requested(self, event: HelpTutorialsRequested) -> None:
        event.stop()
        from gucmail.help.tutorials import LESSONS

        lesson = next((item for item in LESSONS if item.help_page == event.page_id), LESSONS[0])
        self._open_tutorials(lesson.lesson_id)

    @on(TutorialHelpRequested)
    def tutorial_help_requested(self, event: TutorialHelpRequested) -> None:
        event.stop()
        self._open_help(initial_page_id=event.page_id, active_view="tutorial")

    @on(HelpCommandRequested)
    def help_command_requested(self, event: HelpCommandRequested) -> None:
        event.stop()
        query = event.command_id
        self.pop_screen()

        def focus_palette(attempt: int = 0) -> None:
            screen = self.screen
            if isinstance(screen, PaletteScreen):
                field = screen.query_one("#palette-input", Input)
                field.value = query
                field.focus()
                return
            if attempt == 0:
                self.action_palette()
            if attempt < 20:
                self.set_timer(0.05, lambda: focus_palette(attempt + 1))

        self.call_after_refresh(focus_palette)

    @on(ShortcutsRequested)
    def shortcuts_requested(self, event: ShortcutsRequested) -> None:
        event.stop()
        self._open_shortcuts()

    def _open_shortcuts(self) -> None:
        self.push_screen(
            ShortcutsScreen(
                runtime_shortcut_records(self, self._current_shortcuts()),
                notice=self._shortcut_notice,
                record_provider=lambda: runtime_shortcut_records(self, self._current_shortcuts()),
            )
        )

    @on(HelpSettingRequested)
    @on(ShortcutSettingRequested)
    def help_setting_requested(
        self, event: HelpSettingRequested | ShortcutSettingRequested
    ) -> None:
        event.stop()
        setting_id = event.setting_id
        if setting_id == KEYBOARD_SETTING:
            return_screen = self.screen
            committed = False

            def applied(identifiers: tuple[str, ...]) -> None:
                nonlocal committed
                committed = committed or bool(identifiers)
                self._apply_committed_settings(identifiers)

            def closed(_result: None) -> None:
                if committed and isinstance(return_screen, SettingsScreen):
                    return_screen.refresh_after_external_change()

            self.push_screen(
                KeyboardEditorScreen(
                    KeyboardPreferencesService(self._settings_service()),
                    on_applied=applied,
                    binding_id=event.binding_id
                    if isinstance(event, ShortcutSettingRequested)
                    else None,
                ),
                closed,
            )
            return
        self.pop_screen()

        def select_setting(attempt: int = 0) -> None:
            screen = self.screen
            if isinstance(screen, SettingsScreen) and screen._catalog is not None:
                screen.action_select_setting(setting_id)
                return
            if attempt == 0:
                self._open_area("settings")
            if attempt < 30:
                self.set_timer(0.05, lambda: select_setting(attempt + 1))

        self.call_after_refresh(select_setting)

    @on(SettingsSetupRequested)
    def settings_setup_requested(self, event: SettingsSetupRequested) -> None:
        event.stop()
        self._open_area(event.area)

    @on(SettingsHelpRequested)
    def settings_help_requested(self, event: SettingsHelpRequested) -> None:
        event.stop()
        self.push_screen(
            HelpScreen(
                context=self._help_runtime_context("settings"),
                initial_page_id=event.help_topic,
                initial_query=event.setting_id,
                store=self._help_state_store(),
            )
        )

    @on(AppearanceBrowseRequested)
    def appearance_browse_requested(self, event: AppearanceBrowseRequested) -> None:
        event.stop()
        self._open_area(event.browser)

    @on(SavedSearchHelpRequested)
    def saved_search_help_requested(self, event: SavedSearchHelpRequested) -> None:
        event.stop()
        self.push_screen(
            HelpScreen(
                context=self._help_runtime_context("search"),
                initial_page_id="search.overview",
                initial_query="saved searches advanced query recent search",
                store=self._help_state_store(),
            )
        )

    @on(SavedSearchRunRequested)
    def saved_search_run_requested(self, event: SavedSearchRunRequested) -> None:
        event.stop()
        self._current_search_text = event.query_text
        self.pop_screen()
        self._search_expression(
            event.query_text,
            f"Saved search · {event.name}" if event.name else "Recent search",
            payload=event.payload,
            source=event.source,
            name=event.name,
        )

    @on(LocalSearchIndexRequested)
    def local_search_index_requested(self, event: LocalSearchIndexRequested) -> None:
        event.stop()
        self._open_area("diagnostics")

    @on(LocalSearchHelpRequested)
    def local_search_help_requested(self, event: LocalSearchHelpRequested) -> None:
        event.stop()
        self.push_screen(
            HelpScreen(
                context=self._help_runtime_context("search"),
                initial_page_id="search.overview",
                initial_query="local index backup offline similar duplicate semantic search",
                store=self._help_state_store(),
            )
        )

    @on(LocalSearchOpenRequested)
    def local_search_open_requested(self, event: LocalSearchOpenRequested) -> None:
        event.stop()
        result = event.result
        if result.backup_id:
            self._set_status(
                f"Opening Backups for {sanitize_terminal(result.backup_id, max_length=100)} · "
                f"offline reference {sanitize_terminal(result.reference, max_length=100)}"
            )
            self.pop_screen()
            self._open_backup_area()
            return
        if result.kind.endswith("group"):
            self._set_status("Duplicate groups are read-only evidence; inspect members in Search.")
            return
        self.pop_screen()
        self.ui.selected_reference = result.reference
        self.load_message(result.reference, mode="full")

    @on(ContactGroupsRequested)
    def _contact_groups_requested(self, event: ContactGroupsRequested) -> None:
        event.stop()
        self._open_area("contact_groups")

    @on(ContactGroupComposeRequested)
    def _group_compose_requested(self, event: ContactGroupComposeRequested) -> None:
        event.stop()
        draft = DraftData(reference=f"dft-{uuid4().hex}", bcc=list(event.recipients))
        self.pop_screen()
        self.call_after_refresh(self._open_compose, draft, new=True)

    @on(PimActionRequested)
    def pim_action_requested(self, event: PimActionRequested) -> None:
        """Complete cross-area PIM actions without duplicating mailbox services in screens."""

        event.stop()
        if event.area == "contacts" and event.action == "history_index":
            from gucmail.services.contact_history import ContactHistoryIndex
            from gucmail.tui.contact_history_screen import ContactHistoryScreen

            self.push_screen(
                ContactHistoryScreen(
                    ContactHistoryIndex(self.context.state),
                    lambda: ContactOperationPreferences.from_settings(self._settings_service()),
                )
            )
            return
        if event.area == "contacts" and event.action == "compose":
            raw_recipients = event.payload.get("recipients", ())
            recipients = (
                tuple(str(value).strip() for value in raw_recipients if str(value).strip())
                if isinstance(raw_recipients, Sequence)
                and not isinstance(raw_recipients, str | bytes)
                else ()
            )
            if not recipients:
                self._set_status("The selected contacts do not have usable email addresses.")
                return
            draft = DraftData(
                reference=f"dft-{uuid4().hex}",
                to=[Address(address) for address in dict.fromkeys(recipients)],
            )
            self.pop_screen()
            self.call_after_refresh(self._open_compose, draft, new=True)
            return
        if event.area == "contacts" and event.action == "correspondence_messages":
            raw_addresses = event.payload.get("addresses", ())
            if not isinstance(raw_addresses, Sequence) or isinstance(raw_addresses, str | bytes):
                self._set_status("The selected contacts did not provide usable addresses.")
                return
            addresses: list[str] = []
            seen: set[str] = set()
            for value in raw_addresses:
                address = str(value).strip()
                key = address.casefold()
                if address and key not in seen:
                    addresses.append(address)
                    seen.add(key)
                if len(addresses) >= 20:
                    break
            if not addresses:
                self._set_status("The selected contacts did not provide usable addresses.")
                return
            self.pop_screen()
            self._search_contact_correspondence(tuple(addresses))
            return
        if event.area == "contacts" and event.action == "schedule_meeting":
            raw_attendees = event.payload.get("attendees", ())
            if not isinstance(raw_attendees, Sequence) or isinstance(raw_attendees, str | bytes):
                self._set_status("The selected contacts did not provide usable attendees.")
                return
            attendees: list[Address] = []
            seen_attendees: set[str] = set()
            for value in raw_attendees:
                email = str(value).strip()
                key = email.casefold()
                if email and key not in seen_attendees:
                    attendees.append(Address(email))
                    seen_attendees.add(key)
                if len(attendees) >= 100:
                    break
            if not attendees:
                self._set_status("The selected contacts did not provide usable attendees.")
                return
            self.pop_screen()
            self._open_area("calendar")

            def open_prefilled_meeting() -> None:
                screen = self._route_screen(RouteTarget.CALENDAR)
                if isinstance(screen, CalendarScreen):
                    screen.action_create_prefilled(tuple(attendees))

            self.set_timer(0.08, open_prefilled_meeting)
            return
        if event.area == "calendar" and event.action == "related_messages":
            try:
                CalendarOperationPreferences.from_settings(
                    self._settings_service()
                ).require_related()
            except ConfigurationError as exc:
                self._set_status(sanitize_terminal(str(exc)))
                return
            references = tuple(dict.fromkeys(event.references))[:5]
            if not references:
                self._set_status("Select a calendar event before searching related mail.")
                return
            self.pop_screen()
            self._search_calendar_related_messages(references)
            return
        self._set_status(
            f"Unsupported cross-area action: {sanitize_terminal(event.area)} · "
            f"{sanitize_terminal(event.action)}"
        )

    @on(DraftRecoveryRequested)
    def draft_recovery_requested(self, event: DraftRecoveryRequested) -> None:
        event.stop()
        self._palette_recover_draft(
            CommandInvocation.from_mapping("draft.recover", {}), self._palette_context()
        )

    @on(DraftHandoff)
    def draft_handoff_requested(self, event: DraftHandoff) -> None:
        """Connect the draft workspace to the existing composer and scheduler."""

        event.stop()
        request = event.request
        try:
            draft = DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            ).get(request.reference)
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Draft unavailable"))
            return

        if request.kind is DraftHandoffKind.SCHEDULE:

            def scheduled(expression: str | None) -> None:
                if not expression:
                    return
                if isinstance(self.screen, DraftAttachmentScreen):
                    self.pop_screen()
                self._schedule_workspace_draft(request.reference, expression)

            self.push_screen(
                DraftScheduleScreen(
                    request.reference,
                    expression=request.schedule_expression or "tomorrow 09:00",
                ),
                scheduled,
            )
            return

        if isinstance(self.screen, DraftAttachmentScreen):
            self.pop_screen()

        def open_composer() -> None:
            self._open_compose(draft, review_send=request.kind is DraftHandoffKind.SEND)

        self.call_after_refresh(open_composer)

    @work(thread=True, exclusive=True, group="pim-related-mail")
    def _search_contact_correspondence(self, addresses: tuple[str, ...]) -> None:
        try:
            ContactOperationPreferences.from_settings(self._settings_service()).require("history")
            service = MessageService(self._ensure_gateway(), state=self.context.state)
            result_limit = min(max(self.context.config.page_size, 1), 100)
            per_query = max(1, result_limit // max(1, len(addresses) * 2))
            found: dict[str, MessageSummary] = {}
            summaries: list[str] = []
            for address in addresses:
                for query in (SearchQuery(sender=address), SearchQuery(recipient=address)):
                    page = service.search_messages(query, limit=per_query)
                    summaries.append(page.execution_summary)
                    for item in page.items:
                        found.setdefault(item.reference, item)
                        if len(found) >= result_limit:
                            break
                    if len(found) >= result_limit:
                        break
                if len(found) >= result_limit:
                    break
            execution = " · ".join(dict.fromkeys(summaries))
            self.call_from_thread(
                self._render_palette_search,
                tuple(found.values()),
                f"Correspondence with {len(addresses)} contact address(es)",
                execution or "Bounded sender and recipient search completed.",
            )
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Contact correspondence search unavailable"),
            )

    @work(thread=True, exclusive=True, group="pim-related-mail")
    def _search_calendar_related_messages(self, references: tuple[str, ...]) -> None:
        try:
            CalendarOperationPreferences.from_settings(self._settings_service()).require_related()
            references = tuple(dict.fromkeys(references))[:5]
            if not references:
                return
            gateway = self._ensure_gateway()
            calendar = CalendarService(
                gateway,
                display_timezone=self.context.config.timezone,
                default_limit=min(max(self.context.config.page_size, 1), 100),
            )
            messages = MessageService(gateway)
            result_limit = min(max(self.context.config.page_size, 1), 100)
            per_event_limit = max(1, result_limit // len(references))
            found: dict[str, MessageSummary] = {}
            subjects: list[str] = []
            summaries: list[str] = []
            for reference in references:
                CalendarOperationPreferences.from_settings(
                    self._settings_service()
                ).require_related()
                value = calendar.get(reference)
                subject = value.subject.strip()
                if not subject:
                    continue
                subjects.append(subject)
                CalendarOperationPreferences.from_settings(
                    self._settings_service()
                ).require_related()
                page = messages.search_messages(
                    SearchQuery(subject=subject),
                    limit=per_event_limit,
                )
                summaries.append(page.execution_summary)
                for message in page.items:
                    found.setdefault(message.reference, message)
                    if len(found) >= result_limit:
                        break
                if len(found) >= result_limit:
                    break
            if not subjects:
                self.call_from_thread(
                    self._set_status,
                    "Selected calendar events have no subject to match against mail.",
                )
                return
            label = (
                f"Related mail for {subjects[0]}"
                if len(subjects) == 1
                else f"Related mail for {len(subjects)} selected events"
            )
            execution = f"{len(subjects)} bounded server-side subject search(es); " + (
                summaries[-1] if summaries else "no residual evaluation"
            )
            self.call_from_thread(
                self._render_calendar_related_search,
                tuple(found.values()),
                label,
                execution,
            )
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Related-mail search unavailable"),
            )

    def _render_calendar_related_search(
        self, messages: tuple[MessageSummary, ...], label: str, execution: str
    ) -> None:
        try:
            CalendarOperationPreferences.from_settings(self._settings_service()).require_related()
        except ConfigurationError as exc:
            self._set_status(sanitize_terminal(str(exc)))
            return
        self._render_palette_search(messages, label, execution)

    def _palette_context(
        self, handler_ids: Sequence[str] | None = None, *, source_screen: Screen[Any] | None = None
    ) -> CommandContext:
        screen = source_screen or self.screen
        area = self._active_palette_area(screen)
        values = {
            "active_view": area,
            "current_area": area,
            "current_folder": self.ui.folder,
        }
        if self.ui.selected_reference:
            values["selected_message"] = self.ui.selected_reference
            selected = self._selected_message_summary()
            if selected is not None:
                values["selected_message_has_attachments"] = str(
                    selected.has_attachments
                ).casefold()
                if selected.sender is not None:
                    sender = selected.sender.email or selected.sender.name
                    if sender:
                        values["selected_sender"] = sender
        if self._mailbox_quota_percent is not None:
            values["mailbox_quota_percent"] = f"{self._mailbox_quota_percent:.2f}"
            if self._mailbox_quota_percent >= 85.0:
                values["mailbox_near_limit"] = "true"
        if isinstance(screen, ComposeScreen):
            values["current_draft"] = screen.draft.reference
        elif isinstance(screen, DraftAttachmentScreen) and (
            draft_reference := screen.selected_draft_reference
        ):
            values["current_draft"] = draft_reference
        if isinstance(screen, CalendarScreen) and (
            event_reference := screen.selected_invitation_reference
        ):
            values["selected_event"] = event_reference
        if isinstance(screen, TasksScreen) and (task_reference := screen.selected_task_reference):
            values["selected_task"] = task_reference
        if isinstance(screen, ContactsScreen | CorrespondentsScreen) and (
            contact_value := screen.selected_contact_value
        ):
            values["selected_contact"] = contact_value
        if isinstance(screen, BackupScreen) and (backup_id := screen.selected_backup_id):
            values["current_backup"] = backup_id
        return CommandContext.from_mapping(
            values,
            online=self.ui.online and not self.offline,
            offline_mode=self.offline,
            expert_mode=self.context.config.mode == "expert",
            handler_ids=handler_ids,
        )

    def _active_palette_area(self, screen: Screen[Any] | None = None) -> str:
        screen = screen or self.screen
        area_by_type = (
            (ComposeScreen, "compose"),
            (DraftAttachmentScreen, "drafts"),
            (CalendarScreen, "calendar"),
            (ContactsScreen, "contacts"),
            (CorrespondentsScreen, "contacts"),
            (TasksScreen, "tasks"),
            (SettingsScreen, "settings"),
            (HelpScreen, "help"),
            (BackupScreen, "backup"),
        )
        for screen_type, area in area_by_type:
            if isinstance(screen, screen_type):
                return area
        title = getattr(screen, "title", "")
        if isinstance(title, str):
            normalized = title.casefold()
            for area in ("backups", "storage", "activity", "scheduled"):
                if area.rstrip("s") in normalized:
                    return area
        return "mail"

    @staticmethod
    def _palette_scalar(value: object) -> str | None:
        if value is None:
            return None
        for attribute in ("canonical", "email", "display", "expression", "original"):
            candidate = getattr(value, attribute, None)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _palette_datetime(value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value
        candidate = getattr(value, "utc", None)
        return candidate if isinstance(candidate, datetime) else None

    def _palette_open_folder(self, invocation: CommandInvocation, _context: CommandContext) -> None:
        presets = {
            "mail.open_inbox": "inbox",
            "mail.open_sent": "sent",
            "mail.open_drafts": "drafts",
            "mail.open_deleted": "trash",
            "mail.open_junk": "junk",
            "mail.open_archive": "archive",
        }
        folder = presets.get(invocation.command_id) or self._palette_scalar(
            invocation.get("folder")
        )
        if not folder:
            self._set_status("Choose a folder in the palette before opening it.")
            return
        self._activate_folder_location(folder)

    def _activate_folder_location(self, folder: str, *, record_history: bool = True) -> None:
        normalized = folder.strip()
        if not normalized:
            return
        if normalized != self.ui.folder:
            self._cancel_automatic_read()
            self._message_generation += 1
            if self._message_timeout is not None:
                self._message_timeout.stop()
                self._message_timeout = None
            self._message_detail = None
            self._message_detail_reference = None
            self.ui.selected_reference = None
            self._selected_message_references.clear()
            self.query_one("#preview", Static).update("Select a message to preview it safely.")
        if normalized != self.ui.folder and record_history:
            if (
                not self._folder_location_history
                or self._folder_location_history[-1] != self.ui.folder
            ):
                self._folder_location_history.append(self.ui.folder)
            if self._folder_location_history[-1] != normalized:
                self._folder_location_history.append(normalized)
            del self._folder_location_history[:-50]
        if record_history:
            self._recent_folder_paths = [
                normalized,
                *(path for path in self._recent_folder_paths if path != normalized),
            ][:20]
        self.ui.folder = normalized
        self._persist_setting_values(
            {
                "mail.last_folder": normalized,
                "palette.recent_folders": self._recent_folder_paths,
            },
            reason="Folder history updated",
        )
        self.refresh_mailbox()

    def _palette_open_message(
        self, invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        reference = self._palette_scalar(invocation.get("reference")) or self.ui.selected_reference
        if not reference:
            self._set_status("Select a message before opening it.")
            return
        self.load_message(reference, mode="auto")

    def _palette_compose_new(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        recipient = self._palette_scalar(invocation.get("recipient"))
        if recipient:
            self._open_compose(
                DraftData(reference=f"dft-{uuid4().hex}", to=[Address(recipient)]), new=True
            )
            return
        self.action_compose()

    @staticmethod
    def _search_query_text(query: SearchQuery) -> str:
        """Serialize the supported structured mail fields into expert syntax."""

        def quoted(value: object) -> str:
            text = str(value).replace("\\", "\\\\").replace('"', '\\"')
            return f'"{text}"'

        parts: list[str] = []
        if query.mailbox_scope:
            parts.append("scope:mailbox")
        if query.text:
            parts.append(quoted(query.text))
        for field, value in (
            ("from", query.sender),
            ("recipient", query.recipient),
            ("to", query.to),
            ("cc", query.cc),
            ("bcc", query.bcc),
            ("subject", query.subject),
            ("body", query.body),
            ("after", query.after.isoformat() if query.after else None),
            ("before", query.before.isoformat() if query.before else None),
            ("attachment", query.attachment_name),
            ("importance", query.importance.value if query.importance else None),
            ("folder", query.folder),
            ("min-size", query.minimum_size),
            ("max-size", query.maximum_size),
            ("message-id", query.internet_message_id),
        ):
            if value is not None and str(value).strip():
                parts.append(f"{field}:{quoted(value)}")
        if query.has_attachments is not None:
            parts.append("has:attachment" if query.has_attachments else "NOT has:attachment")
        if query.is_read is not None:
            parts.append(f"read:{str(query.is_read).lower()}")
        if query.flagged is not None:
            parts.append(f"flag:{str(query.flagged).lower()}")
        parts.extend(f"category:{quoted(category)}" for category in query.categories)
        exclusion_aliases = {
            "sender": "from",
            "recipient": "recipient",
            "subject": "subject",
            "body": "body",
            "attachment": "attachment",
            "attachment_name": "attachment",
            "folder": "folder",
            "category": "category",
            "importance": "importance",
        }
        for exclusion in query.exclusions:
            if exclusion.field == "has_attachments":
                positive = str(exclusion.value).casefold() in {"1", "true", "yes", "on"}
                parts.append("NOT has:attachment" if positive else "has:attachment")
                continue
            alias = exclusion_aliases.get(exclusion.field, exclusion.field)
            parts.append(f"NOT {alias}:{quoted(exclusion.value)}")
        return " AND ".join(parts)

    def _palette_sequence_query(self, invocation: CommandInvocation) -> SearchQuery:
        """Translate only the allowlisted search-step fields into a bounded server query."""

        if invocation.command_id not in {"search.mail", "attachments.search"}:
            raise ConfigurationError("A command sequence must begin with a supported search.")
        values = invocation.as_dict()
        sender = self._palette_scalar(values.get("sender"))
        date_range = values.get("date_range")
        size = values.get("size") or values.get("size_range")
        attachment_type = self._palette_scalar(values.get("attachment_type"))
        extensions = {
            "archive": ".zip",
            "document": ".doc",
            "image": ".png",
            "pdf": ".pdf",
            "presentation": ".ppt",
            "spreadsheet": ".xls",
        }
        attachment_name = extensions.get(attachment_type.casefold()) if attachment_type else None
        structured = any(
            value is not None
            for value in (
                sender,
                date_range,
                size,
                attachment_name,
                values.get("read_state"),
            )
        )
        return SearchQuery(
            text=None if structured else self._palette_scalar(values.get("query")),
            sender=sender,
            after=self._palette_datetime(getattr(date_range, "start", None)),
            before=self._palette_datetime(getattr(date_range, "end", None)),
            is_read=(
                False
                if values.get("read_state") == "unread"
                else True
                if values.get("read_state") == "read"
                else None
            ),
            has_attachments=True if invocation.command_id == "attachments.search" else None,
            attachment_name=attachment_name,
            minimum_size=getattr(size, "minimum_bytes", None),
            maximum_size=getattr(size, "maximum_bytes", None),
        )

    def _palette_search(self, invocation: CommandInvocation, _context: CommandContext) -> None:
        values = invocation.as_dict()
        preserve_query = bool(values.pop("_preserve_query", False))
        result_label = self._palette_scalar(values.pop("_label", None))
        ignore_original_text = bool(values.pop("_ignore_original_text", False))
        query_text = self._palette_scalar(values.get("query"))
        command = universal_palette_registry().get("search.mail")
        canonical_names = {
            " ".join(name.casefold().split())
            for name in (command.name, *command.aliases, *command.misspellings)
        }
        if (
            query_text is None
            and " ".join((invocation.original_text or "").casefold().split()) in canonical_names
        ):
            ignore_original_text = True
            values.pop("folder", None)
        if query_text is None and invocation.original_text and not ignore_original_text:
            query_text = invocation.original_text.strip()
        flagged = values.get("flagged")
        has_attachments = values.get("has_attachments")
        date_range = values.get("date_range")
        size = values.get("size") or values.get("size_range")
        important = values.get("important")
        if bool(values.get("fuzzy", False)):
            local_query = query_text or self._palette_scalar(values.get("subject")) or ""
            self._open_local_search(
                mode=LocalSearchMode.INDEX,
                query=local_query,
                fuzzy=True,
                auto_run=bool(local_query),
            )
            return
        default_scope = str(self._setting_value("search.default_scope", "mailbox"))
        if (
            self._mail_search_default_scope is not None
            and self._mail_search_default_scope != default_scope
        ):
            self._last_mail_search_scope = None
            self._last_mail_search_folder = None
            self._mail_search_default_scope = None
        explicit_scope = self._palette_scalar(values.get("scope"))
        scope = explicit_scope or self._last_mail_search_scope or default_scope
        restored_folder = (
            self._last_mail_search_folder if not explicit_scope and scope == "folder" else None
        )
        if scope in {"local_index", "backups", "all"} and not any(
            value is not None
            for key, value in values.items()
            if key not in {"query", "exact", "fuzzy", "scope"}
        ):
            self._open_local_search(
                mode={
                    "local_index": LocalSearchMode.INDEX,
                    "backups": LocalSearchMode.BACKUPS,
                    "all": LocalSearchMode.ALL,
                }[scope],
                query=query_text or "",
                auto_run=bool(query_text),
            )
            return
        structured_keys = {
            key
            for key, value in values.items()
            if key not in {"query", "exact", "fuzzy", "scope"} and value is not None
        }
        attachment_kinds = {
            "archive": ".zip",
            "document": ".doc",
            "image": ".png",
            "pdf": ".pdf",
            "presentation": ".ppt",
            "spreadsheet": ".xls",
        }
        attachment_type = self._palette_scalar(values.get("attachment_type"))
        categories_value = values.get("categories")
        categories = list(
            tuple(str(item) for item in categories_value if str(item).strip())
            if isinstance(categories_value, (tuple, list))
            else ()
        )
        category = self._palette_scalar(values.get("category"))
        if category and category not in categories:
            categories.append(category)
        exclusions_value = values.get("exclusions")
        exclusions = (
            tuple(
                SearchExclusion(
                    item.field,
                    item.value,
                    item.expression,
                )
                for item in exclusions_value
                if isinstance(item, Exclusion)
            )
            if isinstance(exclusions_value, (tuple, list))
            else ()
        )
        ranking_value = values.get("ranking")
        ranking = (
            SearchRanking(
                ranking_value.field,
                ranking_value.direction,
                ranking_value.limit,
                ranking_value.expression,
            )
            if isinstance(ranking_value, Ranking)
            else None
        )
        thread_value = values.get("thread_response")
        thread_response = getattr(thread_value, "state", None)
        importance: Importance | None = Importance.HIGH if important is True else None
        importance_value = self._palette_scalar(values.get("importance"))
        if importance_value:
            try:
                importance = Importance(importance_value.casefold())
            except ValueError:
                self._set_status("Importance must be low, normal, or high.")
                return
        attachment_name = self._palette_scalar(values.get("attachment_name"))
        if attachment_name is None and attachment_type:
            attachment_name = attachment_kinds.get(attachment_type.casefold())
        if query_text is None and not structured_keys:
            self._prompt_mail_search(
                scope=scope, folder=self.ui.folder if scope == "current_folder" else restored_folder
            )
            return
        extracted_query = bool(
            invocation.original_text
            and query_text
            and query_text != invocation.original_text.strip()
        )
        free_text = query_text if preserve_query or extracted_query or not structured_keys else None
        if scope == "current_folder" and not values.get("folder"):
            values["folder"] = self.ui.folder
        elif restored_folder and not values.get("folder"):
            values["folder"] = restored_folder
        explicit_after = self._palette_datetime(getattr(date_range, "start", None))
        explicit_before = self._palette_datetime(getattr(date_range, "end", None))
        if explicit_after is None:
            explicit_after = self._palette_datetime(date_range) or self._palette_datetime(
                values.get("when")
            )
            if explicit_after is not None:
                explicit_before = explicit_after + timedelta(days=1)
        query = SearchQuery(
            # The deterministic parser keeps the original phrase in ``query``.  Once it has
            # extracted structured clauses, sending that whole sentence as an additional free-
            # text condition would over-constrain EWS and silently lose valid results.
            text=free_text,
            sender=self._palette_scalar(values.get("sender")),
            recipient=self._palette_scalar(values.get("recipient")),
            to=self._palette_scalar(values.get("to")),
            cc=self._palette_scalar(values.get("cc")),
            bcc=self._palette_scalar(values.get("bcc")),
            subject=self._palette_scalar(values.get("subject")),
            body=self._palette_scalar(values.get("body")),
            after=explicit_after or self._palette_datetime(values.get("after")),
            before=explicit_before or self._palette_datetime(values.get("before")),
            is_read=(
                False
                if values.get("read_state") == "unread"
                else True
                if values.get("read_state") == "read"
                else None
            ),
            flagged=flagged if isinstance(flagged, bool) else None,
            has_attachments=has_attachments if isinstance(has_attachments, bool) else None,
            attachment_name=attachment_name,
            importance=importance,
            folder=self._palette_scalar(values.get("folder")),
            mailbox_scope=scope == "mailbox" and not values.get("folder"),
            categories=tuple(categories),
            minimum_size=getattr(size, "minimum_bytes", None),
            maximum_size=getattr(size, "maximum_bytes", None),
            internet_message_id=self._palette_scalar(values.get("internet_message_id")),
            fuzzy=bool(values.get("fuzzy", False)),
            exclusions=exclusions,
            ranking=ranking,
            thread_response=str(thread_response) if thread_response else None,
        )
        self._current_search_text = self._search_query_text(query)
        self._remember_mail_search_scope(
            "mailbox"
            if query.mailbox_scope
            else "current_folder"
            if scope == "current_folder"
            else "folder",
            None
            if query.mailbox_scope or scope == "current_folder"
            else query.folder or self.context.config.default_folder,
        )
        self._search_mail(query, result_label or query_text or "structured search")

    def _remember_mail_search_scope(self, scope: str, folder: str | None = None) -> None:
        self._last_mail_search_scope = scope
        self._last_mail_search_folder = folder
        self._mail_search_default_scope = str(
            self._setting_value("search.default_scope", "mailbox")
        )
        self._capture_session()

    def _prompt_mail_search(self, *, scope: str, folder: str | None = None) -> None:
        def accepted(query_text: str | None) -> None:
            if query_text:
                self._palette_search(
                    CommandInvocation.from_mapping(
                        "search.mail",
                        {
                            "query": query_text,
                            "scope": scope,
                            "folder": folder,
                            "_preserve_query": True,
                            "_label": "All mail search"
                            if scope == "mailbox"
                            else f"{folder} search",
                        },
                    ),
                    self._palette_context(),
                )

        self.push_screen(
            MailQueryScreen(
                "Search all mail"
                if scope == "mailbox"
                else "Search current folder"
                if scope == "current_folder"
                else "Search selected folder",
                "Search mail folders on Exchange. Up to 50 folders and a 200-result window."
                if scope == "mailbox"
                else f"Search in {sanitize_terminal(folder, max_length=100)}.",
                submit_label="Search",
                placeholder="Message text to find",
            ),
            accepted,
        )

    def _palette_search_preset(self, *, unread: bool = False, flagged: bool = False) -> None:
        folder = self.context.config.default_folder
        query = SearchQuery(
            is_read=False if unread else None,
            flagged=True if flagged else None,
            folder=folder,
        )
        self._current_search_text = self._search_query_text(query)
        self._remember_mail_search_scope("folder", folder)
        self._search_mail(
            query,
            "Unread mail" if unread else "Flagged mail",
        )

    def _search_mail(self, query: SearchQuery, label: str) -> Worker[None]:
        generation = self._begin_mailbox_request(
            f"Searching {sanitize_terminal(label, max_length=100)}… · Esc cancels"
        )
        return self._search_mail_worker(generation, query, label)

    @work(thread=True, exclusive=True, group="mailbox-refresh", name="_search_mail")
    def _search_mail_worker(self, generation: int, query: SearchQuery, label: str) -> None:
        worker = get_current_worker()
        if worker.is_cancelled or generation != self._mailbox_generation:
            return
        try:
            page = MessageService(self._ensure_gateway(), state=self.context.state).search_messages(
                query,
                limit=min(max(self.context.config.page_size, 1), 100),
            )
            if worker.is_cancelled or generation != self._mailbox_generation:
                return
            self.call_from_thread(
                self._complete_mail_search,
                generation,
                page.items,
                label,
                page.execution_summary,
            )
        except Exception as exc:
            self.call_from_thread(
                self._fail_mail_search, generation, _safe_failure(exc, action="Search unavailable")
            )

    def _search_expression(
        self,
        query_text: str,
        label: str,
        *,
        payload: Mapping[str, Any],
        source: str,
        name: str | None,
    ) -> Worker[None]:
        generation = self._begin_mailbox_request(
            f"Searching {sanitize_terminal(label, max_length=100)}… · Esc cancels"
        )
        return self._search_expression_worker(
            generation, query_text, label, payload=payload, source=source, name=name
        )

    @work(thread=True, exclusive=True, group="mailbox-refresh", name="_search_expression")
    def _search_expression_worker(
        self,
        generation: int,
        query_text: str,
        label: str,
        *,
        payload: Mapping[str, Any],
        source: str,
        name: str | None,
    ) -> None:
        worker = get_current_worker()
        if worker.is_cancelled or generation != self._mailbox_generation:
            return
        try:
            page = MessageService(
                self._ensure_gateway(), state=self.context.state
            ).search_expression(
                query_text,
                timezone=self.context.config.timezone,
                limit=min(max(self.context.config.page_size, 1), 100),
            )
            if worker.is_cancelled or generation != self._mailbox_generation:
                return
            self.context.state.record_search_run(
                query=dict(payload),
                result_count=len(page.items),
                source=source,
                name=name,
                keep_history=bool(self._setting_value("search.history_enabled", True)),
            )
            self.call_from_thread(
                self._complete_mail_search,
                generation,
                page.items,
                label,
                page.execution_summary,
                "mailbox" if page.plan.mailbox_scope else "folder",
                None
                if page.plan.mailbox_scope
                else page.plan.folder_scope or self.context.config.default_folder,
            )
        except Exception as exc:
            self.call_from_thread(
                self._fail_mail_search, generation, _safe_failure(exc, action="Search unavailable")
            )

    def _complete_mail_search(
        self,
        generation: int,
        messages: Sequence[MessageSummary],
        label: str,
        execution_summary: str,
        scope: str | None = None,
        folder: str | None = None,
    ) -> None:
        if generation != self._mailbox_generation:
            return
        if self._mailbox_timeout is not None:
            self._mailbox_timeout.stop()
            self._mailbox_timeout = None
        self.ui.busy = None
        if scope is not None:
            self._remember_mail_search_scope(scope, folder)
        self._render_palette_search(messages, label, execution_summary)

    def _fail_mail_search(self, generation: int, message: str) -> None:
        if generation != self._mailbox_generation:
            return
        if self._mailbox_timeout is not None:
            self._mailbox_timeout.stop()
            self._mailbox_timeout = None
        self.ui.busy = None
        self._set_status(message)

    def _render_palette_search(
        self,
        messages: Sequence[MessageSummary],
        label: str,
        execution_summary: str,
    ) -> None:
        self._search_heading_label = label
        self._messages = tuple(messages)
        self.ui.selected_reference = None
        self._render_message_rows(self._messages)
        self.query_one("#preview", Static).update("Select a search result to preview it safely.")
        self._set_status(
            f"{sanitize_terminal(label, max_length=120)}: {len(messages)} bounded result(s) · "
            f"{sanitize_terminal(execution_summary, max_length=400)}"
        )

    def _palette_set_flag(self, invocation: CommandInvocation, _context: CommandContext) -> None:
        reference = self._palette_scalar(invocation.get("reference")) or self.ui.selected_reference
        if not reference:
            self._set_status("Select a message before changing its flag.")
            return
        self._set_message_flag(reference, flagged=invocation.command_id == "mail.flag")

    @work(thread=True, exclusive=False, group="mailbox-mutation")
    @coordinated_write()
    def _set_message_flag(self, reference: str, *, flagged: bool) -> None:
        try:
            service = MessageService(self._ensure_gateway(), state=self.context.state)
            service.execute_mutation(service.plan_flag((reference,), flagged=flagged))
            self.call_from_thread(
                self._set_status, "Flagged message" if flagged else "Removed flag"
            )
            self.call_from_thread(self.refresh_mailbox)
        except Exception as exc:
            self.call_from_thread(
                self._set_status, _safe_failure(exc, action="Flag update unavailable")
            )

    def _palette_mark_read(self, invocation: CommandInvocation, _context: CommandContext) -> None:
        reference = self._palette_scalar(invocation.get("reference")) or self.ui.selected_reference
        if not reference:
            self._set_status("Select a message before changing its read state.")
            return
        self._toggle_message_read(reference, read=invocation.command_id == "mail.mark_read")

    def _palette_recover_draft(
        self, invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        service = DraftService(
            self.context.state,
            self.context.paths.recovery_dir,
            maximum_attachment_bytes=self.context.config.max_attachment_bytes,
        )
        references = service.recovery_references()
        if not references:
            self._set_status("No autosaved draft recovery is available.")
            self.notify("No autosaved draft recovery is available.")
            return
        requested = self._palette_scalar(invocation.get("reference"))
        if requested and requested not in references:
            self._set_status(f"No recovery snapshot exists for {sanitize_terminal(requested)}.")
            return
        draft = service.read_recovery(requested or references[-1])
        self._open_compose(draft)

    def _palette_save_current_draft(
        self,
        _invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        composer = self.screen if isinstance(self.screen, ComposeScreen) else None
        if composer is None:
            self._set_status("Open a composer before saving the current draft.")
            return
        if composer._guard_running_change():
            return
        payload = composer._payload(action="save")
        payload["save_mode"] = "local_only"
        composer.set_save_preflight_pending(True)
        composer._set_note("Checking attachment files…")
        self.prepare_draft_save(composer, payload)

    def _palette_schedule_current_draft(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        composer = self.screen if isinstance(self.screen, ComposeScreen) else None
        draft_workspace = self.screen if isinstance(self.screen, DraftAttachmentScreen) else None
        if draft_workspace is not None and draft_workspace.selected_draft_reference:
            expression = self._palette_scalar(invocation.get("when")) or "tomorrow 09:00"
            draft_workspace.action_schedule_draft(expression=expression)
            return
        if composer is None:
            self._set_status("Open a composer before scheduling the current draft.")
            return
        expression = self._palette_scalar(invocation.get("when")) or "tomorrow 09:00"
        composer.action_schedule_draft(expression=expression)

    def _palette_cancel_scheduled(
        self,
        _invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        screen = self._route_screen(RouteTarget.SCHEDULED)
        if isinstance(screen, ScheduledScreen):
            screen.action_cancel_schedule()
            return
        self._open_area("scheduled")
        self._set_status("Select a scheduled message, then choose Cancel schedule.")

    def _palette_open_attachments(
        self, invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        reference = self._palette_scalar(invocation.get("reference")) or self.ui.selected_reference
        if not reference:
            self._set_status("Select a message before opening attachments.")
            return

        self._open_draft_attachment_workspace(reference)

    def _palette_download_attachments(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        reference = self._palette_scalar(invocation.get("reference")) or self.ui.selected_reference
        if not reference:
            self._set_status("Select a message or local draft before downloading attachments.")
            return
        self._open_draft_attachment_workspace(reference, attachment_action="save_all")

    def _open_storage_analysis(self, analysis_id: str) -> None:
        screen = self._route_screen(RouteTarget.STORAGE)
        if isinstance(screen, StorageScreen):
            screen.queue_analysis(analysis_id)
            return
        self._open_area("storage")

        def analyze() -> None:
            mounted = self._route_screen(RouteTarget.STORAGE)
            if isinstance(mounted, StorageScreen):
                mounted.queue_analysis(analysis_id)

        self.set_timer(0.08, analyze)

    def _open_activity_with_control(self, control: str) -> None:
        screen = self._route_screen(RouteTarget.ACTIVITY)
        if isinstance(screen, ActivityScreen):
            screen.queue_watcher_control(control)
            return
        self._open_area("activity")

        def execute() -> None:
            mounted = self._route_screen(RouteTarget.ACTIVITY)
            if isinstance(mounted, ActivityScreen):
                mounted.queue_watcher_control(control)

        self.set_timer(0.08, execute)

    def _open_activity_with_notification_test(self, provider: str) -> None:
        screen = self._route_screen(RouteTarget.ACTIVITY)
        if isinstance(screen, ActivityScreen):
            screen.queue_notification_test(provider)
            return
        self._open_area("activity")

        def execute() -> None:
            mounted = self._route_screen(RouteTarget.ACTIVITY)
            if isinstance(mounted, ActivityScreen):
                mounted.queue_notification_test(provider)

        self.set_timer(0.08, execute)

    @on(InboxRulesRequested)
    def _inbox_rules_requested(self, event: InboxRulesRequested) -> None:
        event.stop()
        self._open_area("inbox_rules")

    @on(AutomaticRepliesRequested)
    def _automatic_replies_requested(self, event: AutomaticRepliesRequested) -> None:
        event.stop()
        self._open_area("automatic_replies")

    def _recent_rule_sample(
        self, profile: MonitoringProfile, cancel: Event, progress: ProgressReporter
    ) -> NotificationSample:
        service = NotificationSimulationService(
            MessageService(self._ensure_gateway(), state=self.context.state),
            account_id=self.context.config.email or "configured-account",
        )
        return service.recent(
            profile,
            cancel=cancel,
            progress=lambda completed, total: progress(
                OperationProgress(
                    "Sampling recent mail metadata",
                    completed=completed,
                    total=total,
                )
            ),
        )

    def _deliver_webhook_test(
        self,
        config: WebhookConfig,
        event: NotificationEvent,
    ) -> DeliveryResult:
        webhook_secrets = SecretStore("webhooks")
        with WebhookNotificationProvider(
            replace(config, enabled=True),
            state=self.context.state,
            secret_store=webhook_secrets,
        ) as provider:
            return provider.notify(event)

    def _open_notification_webhook_action(
        self,
        action: str,
        *,
        name: str | None = None,
        provider_kind: str | None = None,
    ) -> None:
        def execute(mounted: Screen[Any] | None = None) -> None:
            mounted = mounted or self._route_screen(RouteTarget.NOTIFICATIONS)
            if not isinstance(mounted, NotificationSettingsScreen):
                return
            if action == "test":
                mounted.action_test_webhook(name, provider_kind, confirmed=True)
            elif action == "show":
                mounted.action_show_webhook_configuration(name)

        current = self._route_screen(RouteTarget.NOTIFICATIONS)
        if isinstance(current, NotificationSettingsScreen):
            execute(current)
            return
        self._open_area("notifications")
        self.set_timer(0.08, execute)

    def _open_activity_with_delivery_retry(self, category: str = "webhook") -> None:
        screen = self._route_screen(RouteTarget.ACTIVITY)
        if isinstance(screen, ActivityScreen):
            screen.queue_delivery_retry(category)
            return
        self._open_area("activity")

        def execute() -> None:
            mounted = self._route_screen(RouteTarget.ACTIVITY)
            if isinstance(mounted, ActivityScreen):
                mounted.queue_delivery_retry(category)

        self.set_timer(0.08, execute)

    def _open_activity_filtered(self, category: str, text: str = "") -> None:
        def apply_filter(mounted: Screen[Any] | None = None) -> None:
            mounted = mounted or self._route_screen(RouteTarget.ACTIVITY)
            if not isinstance(mounted, ActivityScreen):
                return
            mounted.query_one("#activity-category", Select).value = category
            mounted.query_one("#activity-search", Input).value = text
            mounted.action_refresh()

        current = self._route_screen(RouteTarget.ACTIVITY)
        if isinstance(current, ActivityScreen):
            apply_filter(current)
            return
        self._open_area("activity")
        self.set_timer(0.08, apply_filter)

    def action_copy_webhook_event_schema(self) -> None:
        document = json.dumps(notification_event_schema(), ensure_ascii=False, indent=2)
        self.copy_to_clipboard(document)
        self._set_status("Copied webhook event JSON Schema to the clipboard")

    def _open_saved_searches(
        self,
        *,
        mode: str = "saved",
        create: bool = False,
    ) -> None:
        initial_query = self._current_search_text
        if not initial_query:
            try:
                page_filter = self.query_one("#message-filter", Input).value.strip()
            except NoMatches:
                page_filter = ""
            if page_filter:
                initial_query = f'text:"{page_filter.replace(chr(34), chr(92) + chr(34))}"'
        self.push_screen(
            SavedSearchScreen(
                StateSavedSearchBackend(
                    self.context.state,
                    timezone=self.context.config.timezone,
                    history_enabled=bool(self._setting_value("search.history_enabled", True)),
                ),
                initial_query=initial_query,
                mode=mode,
                create_on_mount=create,
            )
        )

    def _open_diagnostics_area(self, initial_action: str | None = None) -> None:
        online = not self.offline
        self.push_screen(
            DiagnosticsScreen(
                LocalDiagnosticsBackend(
                    self.context,
                    gateway_factory=self._ensure_gateway if online else None,
                    close_runtime=self._close_runtime_for_cache_repair,
                ),
                default_export_path=str(self.context.config.downloads / "gucmail-diagnostics.json"),
                default_folder=self.ui.folder,
                offline=self.offline,
                initial_action=initial_action,
            )
        )

    def _open_diagnostics_view(self, view: str) -> None:
        current = self._route_screen(RouteTarget.DIAGNOSTICS)
        if isinstance(current, DiagnosticsScreen):
            current.configure_view(view)
            return
        self._open_area("diagnostics")

        def configure() -> None:
            screen = self._route_screen(RouteTarget.DIAGNOSTICS)
            if isinstance(screen, DiagnosticsScreen):
                screen.configure_view(view)

        self.set_timer(0.05, configure)

    def _palette_doctor(self, _invocation: CommandInvocation, _context: CommandContext) -> None:
        self._open_diagnostics_view("overview")

    def _palette_connection(self, _invocation: CommandInvocation, _context: CommandContext) -> None:
        self._open_diagnostics_view("connection_status")

    def _palette_ews_capabilities(
        self, _invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        self._open_diagnostics_view("ews_capability_matrix")

    def _palette_logs(self, _invocation: CommandInvocation, _context: CommandContext) -> None:
        def load() -> str:
            directory = self.context.paths.log_dir.resolve(strict=False)
            files = sorted(
                (path for path in directory.glob("*.log*") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if not files:
                return f"No application logs exist in {directory}."
            listing = [
                f"{sanitize_terminal(path.name, max_length=120)} · {path.stat().st_size} bytes"
                for path in files[:10]
            ]
            newest = files[0]
            with newest.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - 131_072))
                tail = stream.read(131_072).decode("utf-8", errors="replace")
            safe_tail = redact_text(
                tail,
                redact_emails=self.context.config.redact_email_addresses,
                redact_urls=False,
            )
            safe_tail = sanitize_terminal(safe_tail, max_length=16_000)
            return "\n".join(
                (
                    f"Log directory: {directory}",
                    "Bounded sanitized files:",
                    *listing,
                    "",
                    f"Newest log tail (up to 128 KiB): {newest.name}",
                    safe_tail or "The newest log is empty.",
                )
            )

        self.push_screen(AreaScreen("Logs · bounded sanitized tail", load))

    def _palette_enter_offline(
        self, _invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        self._cancel_automatic_read()
        self._mailbox_generation += 1
        self._message_generation += 1
        self.ui.busy = None
        if not self._gateway_injected:
            self.context.close()
            self.gateway = None
        self.offline = True
        self.ui.online = False
        self._header_connection = "Account offline"
        self._header_account_detail = self._header_connection
        self._refresh_shell_status()
        self._set_status("Offline mode enabled · Exchange actions are unavailable")

    def _palette_leave_offline(
        self, _invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        self.offline = False
        self.ui.online = False
        self._header_connection = "Account not connected"
        self._header_account_detail = self._header_connection
        self._refresh_shell_status()
        self._set_status(
            "Offline mode disabled · the next Exchange action will reconnect on demand"
        )

    def _palette_offline_status(
        self, _invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        def load() -> str:
            repository = self._backup_repository_path()
            records = BackupAreaBackend.from_context(
                self.context,
                online=False,
                repository_root=repository,
            ).list_backups(cancel=Event(), progress=lambda _progress: None)
            complete = tuple(record for record in records if record.state == "complete")
            latest = max(
                complete,
                key=lambda record: (
                    record.completed_at or record.created_at or "",
                    record.backup_id,
                ),
                default=None,
            )
            search_index = SearchIndex(self.context.paths.index_database)
            index = search_index.stats()
            index_verification = search_index.verify()
            state = self.context.state.health()
            mode = "ENABLED" if self.offline else "DISABLED"
            mutation = (
                "Unavailable while offline"
                if self.offline
                else "Available after an on-demand authenticated connection"
            )
            return "\n".join(
                (
                    f"Offline mode: {mode}",
                    f"Server mutations: {mutation}",
                    "Local sources: full-text index and immutable backup archives",
                    "Search index: "
                    f"{index.documents} document(s) · database {index_verification.integrity}",
                    f"Backup repository: {repository}",
                    f"Complete backups: {len(complete)} of {len(records)} local record(s)",
                    "Latest complete backup: "
                    + (
                        f"{latest.backup_id} · "
                        f"{latest.completed_at or latest.created_at or 'unknown time'}"
                        if latest is not None
                        else "None"
                    ),
                    f"State database: {state.get('integrity', 'unknown')}",
                    "All values above are local. No Exchange request was made.",
                )
            )

        self.push_screen(AreaScreen("Offline status · local sources", load))

    def _palette_help_open(self, _invocation: CommandInvocation, _context: CommandContext) -> None:
        self._open_help()

    def _palette_help_search(self, invocation: CommandInvocation, _context: CommandContext) -> None:
        query = self._palette_scalar(invocation.get("query")) or ""
        self._open_help(initial_query=query, active_view=self._active_palette_area())

    def _palette_help_shortcuts(
        self, _invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        self._open_shortcuts()

    def _palette_apply_theme(self, invocation: CommandInvocation, _context: CommandContext) -> None:
        requested = self._palette_scalar(invocation.get("theme"))
        if not requested:
            self._open_area("themes")
            return
        try:
            definition = self.theme_registry.get(normalize_theme_identifier(requested))
        except ThemeValidationError:
            matches = self.theme_registry.search(requested)
            if not matches:
                self._set_status(f"No theme matches {sanitize_terminal(requested, max_length=80)}")
                return
            definition = matches[0]
        self._apply_palette_theme(definition.id)

    @work(thread=True, exclusive=False, group="appearance-change")
    @coordinated_write()
    def _apply_palette_theme(self, theme_id: str) -> None:
        self._theme_change_pending.set()
        try:
            self.call_from_thread(self._set_status, "Saving theme…")
            definition = self.theme_session.apply(theme_id)
            self.call_from_thread(self._activate_theme, definition)
            self.call_from_thread(
                self._set_status,
                f"Applied theme: {sanitize_terminal(definition.name, max_length=100)}",
            )
            self.call_from_thread(self._refresh_appearance_settings)
        except Exception as exc:
            self.call_from_thread(
                self._set_status, _safe_failure(exc, action="Theme application failed")
            )
        finally:
            self._theme_change_pending.clear()

    def _refresh_appearance_settings(self) -> None:
        if isinstance(self.screen, SettingsScreen):
            self.screen.refresh_after_external_change(changed="Appearance preferences")

    def _palette_toggle_banner(
        self, _invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        self._toggle_palette_banner()

    @work(thread=True, exclusive=False, group="appearance-change")
    @coordinated_write()
    def _toggle_palette_banner(self) -> None:
        self._branding_change_pending.set()
        try:
            self.call_from_thread(self._set_status, "Saving ASCII branding…")
            session = self.branding_browser_backend.session
            session.preview(visible=not session.preferences.visible)
            self.branding_browser_backend.apply()
            state = "shown" if session.preferences.visible else "hidden"
            self.call_from_thread(
                self._set_status,
                f"Expanded ASCII branding {state}; the slim GUC title remains visible",
            )
            self.call_from_thread(self._refresh_appearance_settings)
        except Exception as error:
            self.call_from_thread(
                self._set_status, _safe_failure(error, action="Branding application failed")
            )
        finally:
            self._branding_change_pending.clear()

    @staticmethod
    def _route_area(target: RouteTarget) -> str | None:
        return {
            RouteTarget.ACTIVITY: "activity",
            RouteTarget.BACKUP: "backups",
            RouteTarget.BRANDING: "branding",
            RouteTarget.CALENDAR: "calendar",
            RouteTarget.CONTACTS: "contacts",
            RouteTarget.DRAFTS: "drafts",
            RouteTarget.DIAGNOSTICS: "diagnostics",
            RouteTarget.LOCAL_SEARCH: "local_search",
            RouteTarget.NOTIFICATIONS: "notifications",
            RouteTarget.ATTACHMENTS: "attachments",
            RouteTarget.SEARCH: "saved_searches",
            RouteTarget.SCHEDULED: "scheduled",
            RouteTarget.SETTINGS: "settings",
            RouteTarget.STORAGE: "storage",
            RouteTarget.TASKS: "tasks",
            RouteTarget.THEMES: "themes",
        }.get(target)

    def _route_screen(self, target: RouteTarget) -> Screen[Any] | None:
        screen = self.screen
        expected: dict[RouteTarget, type[Screen[Any]]] = {
            RouteTarget.ACTIVITY: ActivityScreen,
            RouteTarget.BACKUP: BackupScreen,
            RouteTarget.CALENDAR: CalendarScreen,
            RouteTarget.COMPOSER: ComposeScreen,
            RouteTarget.CONTACTS: ContactsScreen,
            RouteTarget.DRAFTS: DraftAttachmentScreen,
            RouteTarget.DIAGNOSTICS: DiagnosticsScreen,
            RouteTarget.LOCAL_SEARCH: LocalSearchScreen,
            RouteTarget.NOTIFICATIONS: NotificationSettingsScreen,
            RouteTarget.ATTACHMENTS: DraftAttachmentScreen,
            RouteTarget.SEARCH: SavedSearchScreen,
            RouteTarget.SCHEDULED: ScheduledScreen,
            RouteTarget.SETTINGS: SettingsScreen,
            RouteTarget.STORAGE: StorageScreen,
            RouteTarget.TASKS: TasksScreen,
            RouteTarget.THEMES: ThemeBrowserScreen,
        }
        screen_type = expected.get(target)
        return screen if screen_type is not None and isinstance(screen, screen_type) else None

    @staticmethod
    def _route_search_value(invocation: CommandInvocation, field: str) -> object | None:
        aliases = {
            "attachment_name": ("attachment_name", "attachment", "query"),
            "category": ("category", "categories", "query"),
            "date_range": ("date_range", "when", "query"),
            "folder": ("folder", "query"),
            "headers": ("headers", "query"),
            "importance": ("importance", "query"),
            "recipient": ("recipient", "person", "query"),
            "sender": ("sender", "person", "query"),
            "size_range": ("size", "size_range", "query"),
            "subject": ("subject", "query"),
            "body": ("body", "query"),
        }
        for name in aliases.get(field, (field, "query")):
            value = invocation.get(name)
            if value is not None:
                return value
        return None

    @staticmethod
    def _search_operand(
        invocation: CommandInvocation,
        *prefixes: str,
    ) -> str:
        value = GUCmailApp._palette_scalar(invocation.get("query")) or ""
        if not value:
            value = (invocation.original_text or "").strip()
        lowered = value.casefold()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return value[len(prefix) :].strip()
        return value.strip()

    def _open_desktop_setup(self) -> None:
        from gucmail.services.windows_desktop import desktop_setup_service
        from gucmail.tui.desktop_setup_screen import DesktopSetupScreen

        self.push_screen(
            DesktopSetupScreen(lambda operation: desktop_setup_service(self.context, operation))
        )

    def _run_advanced_palette_search(self, query_text: str, *, label: str) -> None:
        try:
            preview = StateSavedSearchBackend(
                self.context.state,
                timezone=self.context.config.timezone,
                history_enabled=bool(self._setting_value("search.history_enabled", True)),
            ).preview(query_text)
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Invalid search expression"))
            return
        self._current_search_text = preview.query_text
        self._search_expression(
            preview.query_text,
            label,
            payload=preview.payload,
            source="palette",
            name=None,
        )

    def _prompt_advanced_search(self, *, initial_query: str = "") -> None:
        def accepted(query_text: str | None) -> None:
            if query_text:
                self._run_advanced_palette_search(query_text, label="Advanced search")

        self.push_screen(
            MailQueryScreen(
                "Advanced mailbox search",
                "Enter expert syntax such as from:teacher@example.test AND unread:true.",
                initial_name=initial_query,
                submit_label="Search",
                placeholder="Structured search expression",
            ),
            accepted,
        )

    def _palette_attachment_search(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        values = invocation.as_dict()
        query = self._search_operand(
            invocation,
            "search inside attachments for ",
            "search attachment content for ",
            "search attachments for ",
            "search attachments ",
            "find attachments for ",
            "find attachments ",
        )
        lowered = query.casefold()
        for marker in (" containing ", " contains ", " content for "):
            if marker in lowered:
                query = query[lowered.index(marker) + len(marker) :].strip()
                break
        if lowered.startswith("find attachment "):
            query = query[len("find attachment ") :].strip()
        sender = self._palette_scalar(values.get("sender"))
        date_range = values.get("date_range")
        after = self._palette_datetime(getattr(date_range, "start", None))
        before = self._palette_datetime(getattr(date_range, "end", None))
        size = values.get("size")
        minimum_size = getattr(size, "minimum_bytes", None)
        maximum_size = getattr(size, "maximum_bytes", None)
        attachment_type = self._palette_scalar(values.get("attachment_type"))
        extensions = {
            "archive": ".zip",
            "document": ".doc",
            "image": ".png",
            "pdf": ".pdf",
            "presentation": ".ppt",
            "spreadsheet": ".xls",
        }
        extension = extensions.get(attachment_type.casefold()) if attachment_type else None
        original = (invocation.original_text or "").strip()
        if (
            original
            and self._palette_scalar(values.get("query")) == original
            and "attachment" in original.casefold()
            and any(value is not None for value in (sender, date_range, size, attachment_type))
            and not any(marker in original.casefold() for marker in ("containing", "inside"))
        ):
            query = ""
        runnable = bool(query) or any(
            value is not None
            for value in (sender, after, before, minimum_size, maximum_size, extension)
        )
        self._open_local_search(
            mode=LocalSearchMode.ATTACHMENTS,
            query=query,
            sender=sender,
            after=after,
            before=before,
            minimum_size=minimum_size,
            maximum_size=maximum_size,
            attachment_extension=extension,
            auto_run=runnable,
        )

    def _catalog_search(self, route: PaletteRoute, invocation: CommandInvocation) -> None:
        if route.command_id in {"search.search_current_folder", "search.search_all_mail"}:
            spec = universal_palette_registry().get(route.command_id)
            names = {
                " ".join(name.casefold().split())
                for name in (spec.name, *spec.aliases, *spec.misspellings)
            }
            original = " ".join((invocation.original_text or "").casefold().split())
            supplied = self._palette_scalar(invocation.get("query"))
            bare_command = original in names and (
                supplied is None or " ".join(supplied.casefold().split()) == original
            )
            if supplied is None or bare_command:
                folder = self.ui.folder if route.command_id.endswith("current_folder") else None

                def accepted(query_text: str | None) -> None:
                    if query_text:
                        self._catalog_search(
                            route,
                            CommandInvocation.from_mapping(
                                route.command_id, {"query": query_text, "folder": folder}
                            ),
                        )

                self.push_screen(
                    MailQueryScreen(
                        "Search current folder" if folder is not None else "Search all mail",
                        f"Search in {sanitize_terminal(folder, max_length=100)}."
                        if folder is not None
                        else (
                            "Search mail folders on Exchange. "
                            "Up to 50 folders and a 200-result window."
                        ),
                        submit_label="Search",
                        placeholder="Message text to find",
                    ),
                    accepted,
                )
                return
        if route.command_id in {"search.fuzzy_search", "search.exact_search"}:
            matching = "fuzzy" if route.command_id.endswith("fuzzy_search") else "exact"
            query = self._search_operand(
                invocation,
                f"{matching} search for ",
                f"{matching} search ",
            )
            self._open_local_search(
                mode=LocalSearchMode.INDEX,
                query=query,
                fuzzy=matching == "fuzzy",
                exact=matching == "exact",
                auto_run=bool(query),
            )
            return
        if route.command_id == "search.advanced_search":
            command = universal_palette_registry().get(route.command_id)
            canonical_names = {
                " ".join(name.casefold().split())
                for name in (command.name, *command.aliases, *command.misspellings)
            }
            query = (
                ""
                if " ".join((invocation.original_text or "").casefold().split()) in canonical_names
                else self._search_operand(
                    invocation,
                    "advanced search for ",
                    "advanced search ",
                )
            )
            if query:
                self._run_advanced_palette_search(query, label="Advanced search")
            else:
                self._prompt_advanced_search()
            return
        values = invocation.as_dict()
        if route.command_id.startswith("cleanup."):
            # The typed command phrase is an instruction, not mailbox free text.
            values["_ignore_original_text"] = True
        for key, value in route.filters:
            if key == "is_read":
                values["read_state"] = "read" if value == "true" else "unread"
            elif key in {"flagged", "has_attachments", "fuzzy", "exact"}:
                values[key] = value == "true"
            elif key == "category":
                values["categories"] = (value,)
            elif key == "scope":
                # An explicit canonical route outranks the user's default scope.
                values["scope"] = "current_folder" if value == "current_folder" else "mailbox"
                if route.command_id in {"search.search_current_folder", "search.search_all_mail"}:
                    values["_label"] = (
                        f"{self.ui.folder} search"
                        if value == "current_folder"
                        else "All mail search"
                    )
                if value == "current_folder":
                    values["folder"] = self.ui.folder
                else:
                    values.pop("folder", None)
            elif key == "folder":
                values["folder"] = value
            elif key == "age_days":
                try:
                    days = int(value)
                except ValueError:
                    self._set_status("Cleanup age must be a positive whole number of days.")
                    return
                if days < 1:
                    self._set_status("Cleanup age must be at least one day.")
                    return
                values["before"] = datetime.now(ZoneInfo(self.context.config.timezone)) - timedelta(
                    days=days
                )
            elif key == "classification" and value == "newsletter":
                # EWS exposes no dependable newsletter classification. The common
                # unsubscribe marker is an explicit, review-only heuristic and the
                # result label says so; this route never mutates matched messages.
                values.setdefault("query", "unsubscribe")
            elif key == "label":
                values["_label"] = value
            elif key == "prompt_field":
                prompted = self._route_search_value(invocation, value)
                if prompted is None:
                    self._set_status(
                        f"Enter a {sanitize_terminal(value.replace('_', ' '))} in the palette."
                    )
                    return
                values[value] = prompted
                if values.get("query") == prompted:
                    values.pop("query", None)
            elif key != "classification":
                values[key] = value
        header_text = self._palette_scalar(values.get("headers"))
        if header_text:
            escaped_header = header_text.replace("\\", "\\\\").replace('"', '\\"')
            expression = f'header:"{escaped_header}"'
            self._run_advanced_palette_search(expression, label="Header search")
            return
        if route.target is RouteTarget.TASKS:
            if not isinstance(self._route_screen(RouteTarget.TASKS), TasksScreen):
                self._open_area("tasks")

            query = (
                self._palette_scalar(invocation.get("query")) or invocation.original_text or ""
            ).strip()
            for prefix in ("search tasks for ", "search tasks ", "find tasks for "):
                if query.casefold().startswith(prefix):
                    query = query[len(prefix) :].strip()
                    break

            def configure_tasks() -> None:
                screen = self._route_screen(RouteTarget.TASKS)
                if isinstance(screen, TasksScreen):
                    screen.configure_search(query)

            self.set_timer(0.05, configure_tasks)
            return
        values["_preserve_query"] = True
        routed = CommandInvocation.from_mapping(
            "search.mail",
            values,
            original_text=invocation.original_text,
            confidence=invocation.confidence,
            explanation=invocation.explanation,
        )
        self._palette_search(routed, self._palette_context())

    def _catalog_open(self, route: PaletteRoute, invocation: CommandInvocation) -> None:
        if route.command_id == "offline.show_offline_status":
            self._palette_offline_status(invocation, self._palette_context())
            return
        area = self._route_area(route.target)
        if area is None:
            if route.target is RouteTarget.COMPOSER:
                reference = self._palette_scalar(invocation.get("reference"))
                current = self.screen if isinstance(self.screen, ComposeScreen) else None
                if route.intent == "recover_latest_autosave":
                    self._palette_recover_draft(invocation, self._palette_context())
                elif current is not None and route.intent == "resume_current_draft":
                    current.query_one("#compose-body", TextArea).focus()
                    current._set_note("Continuing the current draft.")
                elif current is not None and route.intent in {
                    "duplicate_current_draft",
                    "duplicate_draft_reference",
                }:
                    duplicate = DraftService(
                        self.context.state,
                        self.context.paths.recovery_dir,
                        maximum_attachment_bytes=self.context.config.max_attachment_bytes,
                    ).create(
                        self._duplicate_draft(
                            self._draft_from_payload(current._payload(action="duplicate"))
                        )
                    )
                    current.dismiss(None)
                    self.call_after_refresh(self._open_compose, duplicate)
                elif reference:
                    try:
                        service = DraftService(
                            self.context.state,
                            self.context.paths.recovery_dir,
                            maximum_attachment_bytes=self.context.config.max_attachment_bytes,
                        )
                        draft = (
                            service.duplicate(reference)
                            if route.intent
                            in {"duplicate_current_draft", "duplicate_draft_reference"}
                            else service.get(reference)
                        )
                    except Exception as exc:
                        self._set_status(_safe_failure(exc, action="Draft unavailable"))
                    else:
                        self._open_compose(draft)
                else:
                    self._open_draft_attachment_workspace()

                    def prompt_for_draft() -> None:
                        screen = self._route_screen(RouteTarget.DRAFTS)
                        if isinstance(screen, DraftAttachmentScreen):
                            verb = (
                                "duplicate"
                                if route.intent
                                in {"duplicate_current_draft", "duplicate_draft_reference"}
                                else "edit"
                            )
                            screen.prompt_draft_selection(verb)

                    self.set_timer(0.08, prompt_for_draft)
                return
            if route.target is RouteTarget.DIAGNOSTICS:
                self._palette_doctor(invocation, self._palette_context())
                return
            self._set_status(f"No operational screen is registered for {route.target.value}.")
            return
        if area == "attachments":
            self._palette_open_attachments(invocation, self._palette_context())
            return
        if area == "saved_searches":
            filters = route.filter_values()
            self._open_saved_searches(
                mode=filters.get("mode", "saved"),
                create=filters.get("create") == "true",
            )
            return
        route_filters = route.filter_values()
        if area == "contacts" and route.intent == "recipient_history":
            self._open_correspondents(route_filters.get("order", "recent"))
            return
        previous_target_screen = self._route_screen(route.target)
        self._open_area(area)

        def configure_opened_screen(attempt: int = 0) -> None:
            if not self.is_running:
                return
            screen = self._route_screen(route.target)
            if screen is None or screen is previous_target_screen:
                if attempt < 20:
                    self.set_timer(
                        0.05,
                        lambda: configure_opened_screen(attempt + 1),
                    )
                return
            filters = route.filter_values()
            try:
                if isinstance(screen, ActivityScreen) and (
                    filters.get("category") or filters.get("text")
                ):
                    if filters.get("category"):
                        screen.query_one("#activity-category", Select).value = filters["category"]
                    if filters.get("text"):
                        screen.query_one("#activity-search", Input).value = filters["text"]
                    screen.action_refresh()
                elif isinstance(screen, ScheduledScreen):
                    state = filters.get("state", "")
                    if state == "uncertain":
                        state = "send_state_uncertain"
                    screen.configure_view(
                        state=state,
                        window=filters.get("window", ""),
                    )
                elif isinstance(screen, CalendarScreen):
                    if filters.get("source") == "selected_message":
                        summary = self._selected_message_summary()
                        if summary is None:
                            self._set_status("Select a message before creating an event.")
                            return
                        attendees = (summary.sender,) if summary.sender is not None else ()
                        screen.action_create_prefilled(
                            attendees,
                            subject=f"Follow up: {summary.subject}",
                        )
                        return
                    anchor = filters.get("anchor")
                    today = datetime.now(ZoneInfo(self.context.config.timezone)).date()
                    if anchor == "today":
                        screen.anchor_date = today
                    elif anchor == "tomorrow":
                        screen.anchor_date = today + timedelta(days=1)
                    elif anchor == "next_week":
                        screen.anchor_date = today + timedelta(days=7)
                    elif anchor == "parameter":
                        selected = self._calendar_invocation_date(invocation)
                        if selected is None:
                            self._set_status("Choose a calendar date in the palette command.")
                            return
                        screen.anchor_date = selected
                    if filters.get("view"):
                        screen.query_one("#calendar-view", Select).value = filters["view"]
                    if anchor:
                        screen.action_refresh()
                elif isinstance(screen, ContactsScreen):
                    if route.action == "directory":
                        query = self._contacts_invocation_query(invocation)
                        if query:
                            screen.configure_search(query, directory=True)
                        else:
                            screen.action_directory()
                    elif filters.get("source") == "selected_sender":
                        sender = self._selected_message_sender()
                        if sender is None:
                            self._set_status("Select a message with a sender first.")
                            return
                        if route.intent == "create_prefilled_contact":
                            screen.action_create_prefilled(sender)
                        else:
                            screen.configure_search(sender.email or sender.name or "")
                elif isinstance(screen, TasksScreen):
                    if filters.get("source") == "selected_message":
                        summary = self._selected_message_summary()
                        if summary is None:
                            self._set_status("Select a message before creating a task.")
                            return
                        screen.action_create_prefilled(
                            summary.subject,
                            source_reference=summary.reference,
                        )
                    elif filters.get("due") in screen.VIEWS:
                        screen.query_one("#tasks-view", Select).value = filters["due"]
                elif isinstance(screen, StorageScreen) and route.intent:
                    screen.queue_snapshot_focus(route.intent)
                elif isinstance(screen, DiagnosticsScreen) and route.intent:
                    screen.configure_view(route.intent)
                elif isinstance(screen, DraftAttachmentScreen):
                    if filters.get("location"):
                        screen.configure_draft_location(filters["location"])
                    elif route.intent == "draft_search" or filters.get("age_days"):
                        query = self._search_operand(
                            invocation,
                            "search drafts for ",
                            "search drafts ",
                            "find drafts for ",
                        )
                        age_days = int(filters["age_days"]) if filters.get("age_days") else None
                        screen.configure_draft_search(
                            query,
                            older_than_days=age_days,
                            focus_filter=route.intent == "draft_search" and not query,
                        )
            except (NoMatches, ValueError):
                return

        self.set_timer(0.05, configure_opened_screen)

    def _selected_message_sender(self) -> Address | None:
        summary = self._selected_message_summary()
        return summary.sender if summary is not None else None

    def _compose_from_route(self, route: PaletteRoute, invocation: CommandInvocation) -> None:
        explicit = self._palette_scalar(invocation.get("person"))
        if route.intent == "prefill_selected_contact":
            contacts = self._route_screen(RouteTarget.CONTACTS)
            if explicit:
                self._open_compose(
                    DraftData(reference=f"dft-{uuid4().hex}", to=[Address(explicit)]), new=True
                )
            elif isinstance(contacts, ContactsScreen | CorrespondentsScreen):
                contacts.action_compose()
            else:
                self._set_status("Choose a contact or person before composing.")
            return
        if route.intent == "prefill_selected_sender":
            sender = self._selected_message_sender()
            if sender is None and explicit:
                sender = Address(explicit)
            if sender is None:
                self._set_status("Select a message with a sender before composing.")
                return
            self._open_compose(DraftData(reference=f"dft-{uuid4().hex}", to=[sender]), new=True)
            return
        if route.intent == "prefill_selected_recipients":
            if explicit:
                self._open_compose(
                    DraftData(reference=f"dft-{uuid4().hex}", to=[Address(explicit)]), new=True
                )
                return
            reference = self.ui.selected_reference
            if not reference:
                self._set_status("Select a message before composing to its recipients.")
                return
            if (
                isinstance(self._message_detail, MessageDetail)
                and self._message_detail_reference == reference
            ):
                self._open_compose_for_message_recipients(self._message_detail)
            else:
                self._prepare_compose_for_message_recipients(reference)
            return
        self.action_compose()

    @work(thread=True, exclusive=True, group="compose-prepare")
    def _prepare_compose_for_message_recipients(self, reference: str) -> None:
        try:
            detail = MessageService(self._ensure_gateway(), state=self.context.state).show_message(
                reference
            )
            self.call_from_thread(self._open_compose_for_message_recipients, detail)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Message recipients unavailable"),
            )

    def _open_compose_for_message_recipients(self, detail: MessageDetail) -> None:
        own = self.context.config.email.casefold() if self.context.config.email else ""
        recipients: list[Address] = []
        seen: set[str] = set()
        for address in (*detail.to, *detail.cc, *detail.bcc):
            key = address.email.casefold()
            if not key or key == own or key in seen:
                continue
            seen.add(key)
            recipients.append(address)
        if not recipients:
            self._set_status("The selected message has no other usable recipients.")
            return
        self._open_compose(DraftData(reference=f"dft-{uuid4().hex}", to=recipients), new=True)

    def _selected_message_summary(self) -> MessageSummary | None:
        reference = self.ui.selected_reference
        if not reference:
            return None
        return next(
            (
                item
                for item in self._messages
                if isinstance(item, MessageSummary) and item.reference == reference
            ),
            None,
        )

    def _selected_message_summaries(self) -> tuple[MessageSummary, ...]:
        """Return explicit selections in visible row order, never hidden mailbox items."""

        return tuple(
            item
            for item in self._visible_messages
            if isinstance(item, MessageSummary)
            and item.reference in self._selected_message_references
        )

    def action_toggle_message_selection(self) -> None:
        reference = self.ui.selected_reference
        visible = {
            item.reference for item in self._visible_messages if isinstance(item, MessageSummary)
        }
        if not reference or reference not in visible:
            self._set_status("Highlight a visible message before toggling its selection.")
            return
        if reference in self._selected_message_references:
            self._selected_message_references.remove(reference)
        else:
            self._selected_message_references.add(reference)
        self._render_message_rows(self._message_page_rows)
        self._set_status(
            f"{len(self._selected_message_references)} of {len(self._visible_messages)} "
            "visible message(s) selected · Space toggles"
        )

    def _select_visible_messages(self, mode: str) -> None:
        visible = tuple(item for item in self._visible_messages if isinstance(item, MessageSummary))
        if not visible:
            self._set_status("No visible messages are available for selection.")
            return
        if mode == "all":
            selected = visible
        elif mode == "unread":
            selected = tuple(item for item in visible if not item.is_read)
        elif mode == "read":
            selected = tuple(item for item in visible if item.is_read)
        elif mode == "sender":
            current = self._selected_message_summary()
            if current is None or current.sender is None:
                self._set_status("Highlight a visible message with a sender first.")
                return
            sender = current.sender.email.casefold()
            selected = tuple(
                item
                for item in visible
                if item.sender is not None and item.sender.email.casefold() == sender
            )
        elif mode == "search":
            local_query = self.query_one("#message-filter", Input).value.strip()
            if not self._current_search_text and not local_query:
                self._set_status("Run or enter a search before selecting its visible results.")
                return
            selected = visible
        else:
            self._set_status("Unknown bounded selection mode.")
            return
        self._selected_message_references = {item.reference for item in selected}
        self._render_message_rows(self._message_page_rows)
        self._set_status(f"Selected {len(selected)} of {len(visible)} visible bounded message(s)")

    def _catalog_application_action(
        self, route: PaletteRoute, invocation: CommandInvocation | None = None
    ) -> None:
        invocation = invocation or CommandInvocation(route.command_id)
        if route.action == "compose" and route.intent:
            self._compose_from_route(route, invocation)
            return
        if route.action == "toggle_preview":
            should_toggle = (route.intent == "ensure_visible" and not self._preview_visible) or (
                route.intent == "ensure_hidden" and self._preview_visible
            )
            if should_toggle:
                self.action_toggle_preview()
            return
        method = getattr(self, route.action_method or "", None)
        if callable(method):
            method()
        else:
            self._set_status(f"Application action unavailable: {sanitize_terminal(route.action)}")

    def _catalog_backup_screen_action(
        self,
        route: PaletteRoute,
        invocation: CommandInvocation,
    ) -> bool:
        """Execute a Backup palette route without losing its explicit scope."""

        if route.target is not RouteTarget.BACKUP:
            return False
        action_name = route.action or ""
        filters = route.filter_values()

        if action_name in {"pause_backup", "cancel_backup"}:
            screen = self._route_screen(RouteTarget.BACKUP)
            if not isinstance(screen, BackupScreen):
                self._set_status(
                    "No backup operation is running. Open Backup Center and start or resume "
                    "a backup first."
                )
                return True
            method = getattr(screen, route.action_method or "", None)
            if callable(method):
                method()
            return True

        if route.intent in {"latest_complete", "folder_tree"}:
            screen = self._route_screen(RouteTarget.BACKUP)
            if screen is None:
                self._open_backup_area()

            def browse_latest_after_mount(attempt: int = 0) -> None:
                mounted = self._route_screen(RouteTarget.BACKUP)
                if isinstance(mounted, BackupScreen):
                    mounted.queue_browse_latest_complete(
                        folder_mode=route.intent == "folder_tree",
                        requested_folder=self._palette_scalar(invocation.get("folder")),
                    )
                    return
                if attempt < 20:
                    self.set_timer(
                        0.05,
                        lambda: browse_latest_after_mount(attempt + 1),
                    )
                    return
                self._set_status(
                    "Backup Center did not become ready; retry the offline backup command."
                )

            if screen is None:
                self.set_timer(0.08, browse_latest_after_mount)
            else:
                browse_latest_after_mount()
            return True

        kind = filters.get("kind")
        folders: tuple[str, ...] = ()
        selected_messages: tuple[str, ...] = ()
        scope = filters.get("scope")
        if action_name == "create":
            if scope == "current_folder":
                selected_folder = self._selected_folder_info()
                folder = selected_folder.path if selected_folder is not None else self.ui.folder
                if not folder:
                    self._set_status("Select a mailbox folder before creating this backup.")
                    return True
                folders = (folder,)
            elif scope == "selected_messages":
                selected_messages = tuple(
                    item.reference for item in self._selected_message_summaries()
                )
                if not selected_messages:
                    self._set_status(
                        "Select one or more visible messages before creating this backup."
                    )
                    return True
            elif scope == "search_results":
                try:
                    local_filter = self.query_one("#message-filter", Input).value.strip()
                except NoMatches:
                    local_filter = ""
                if not self._current_search_text and not local_filter:
                    self._set_status("Run a search before backing up its bounded results.")
                    return True
                selected_messages = tuple(
                    item.reference
                    for item in self._visible_messages
                    if isinstance(item, MessageSummary)
                )
                if not selected_messages:
                    self._set_status("The current bounded search has no messages to back up.")
                    return True
            elif scope == "selected_cluster":
                search_screen = self.screen if isinstance(self.screen, LocalSearchScreen) else None
                selected_messages = (
                    search_screen.current_member_references() if search_screen is not None else ()
                )
                if not selected_messages:
                    self._set_status(
                        "Highlight a local cluster or duplicate group before backing it up."
                    )
                    return True

            # Scoped backup forms need a fresh screen so their exact immutable selection is
            # visible and reviewable in the confirmation.  Unscoped actions reuse an already
            # open Backup Center when possible.
            screen = self._route_screen(RouteTarget.BACKUP)
            if selected_messages or folders or screen is None:
                self._open_backup_area(
                    selected_messages=selected_messages,
                    default_kind=kind,
                    default_folders=folders,
                )
                screen = None

            def create_after_mount() -> None:
                mounted = self._route_screen(RouteTarget.BACKUP)
                if isinstance(mounted, BackupScreen):
                    mounted.configure_create(kind=kind, folders=folders)
                    mounted.queue_action(
                        route.command_id.replace("backup.", "").replace("_", " "),
                        mounted.action_create,
                        requires_selection=False,
                    )

            if screen is None:
                self.set_timer(0.08, create_after_mount)
            elif isinstance(screen, BackupScreen):
                create_after_mount()
            return True

        screen = self._route_screen(RouteTarget.BACKUP)
        if screen is None:
            self._open_backup_area()

        selection_actions = {
            "browse",
            "export_backup",
            "restore_plan",
            "restore_selected_items",
            "resume",
            "verify",
        }

        def invoke_after_mount() -> None:
            mounted = self._route_screen(RouteTarget.BACKUP)
            if not isinstance(mounted, BackupScreen):
                return
            method = getattr(mounted, route.action_method or "", None)
            if callable(method):
                mounted.queue_action(
                    route.command_id.replace("backup.", "").replace("_", " "),
                    method,
                    requires_selection=action_name in selection_actions,
                )

        if screen is None:
            self.set_timer(0.08, invoke_after_mount)
        else:
            invoke_after_mount()
        return True

    def _catalog_screen_action(
        self,
        route: PaletteRoute,
        invocation: CommandInvocation,
    ) -> None:
        if self._catalog_backup_screen_action(route, invocation):
            return
        source_draft = self.screen if isinstance(self.screen, DraftAttachmentScreen) else None
        if (
            route.target is RouteTarget.COMPOSER
            and route.action == "schedule_draft"
            and source_draft is not None
            and source_draft.selected_draft_reference
        ):
            source_draft.action_schedule_draft(
                expression=route.filter_values().get("expression", "tomorrow 09:00")
            )
            return
        contextual_backup_id = (
            self.screen.selected_backup_id if isinstance(self.screen, BackupScreen) else None
        )
        contextual_message_reference = self.ui.selected_reference
        screen_was_open = self._route_screen(route.target) is not None
        if not screen_was_open and route.target is RouteTarget.DIAGNOSTICS:
            # Start the requested preview on mount; a competing overview refresh
            # must not cancel it or silently replace the requested action.
            self._open_diagnostics_area(route.action)
            return

        def invoke(screen: Screen[Any] | None) -> None:
            if isinstance(screen, ComposeScreen):
                if screen.defer_status_action(lambda: invoke(screen)):
                    return
                if route.action == "forward_as_attachment":
                    self.attach_selected_message_to_composer(
                        screen, reference=contextual_message_reference
                    )
                    return
            if isinstance(screen, ActivityScreen) and route.action == "control_watcher":
                control = route.filter_values().get("control", "")
                screen.queue_watcher_control(control)
                return
            if isinstance(screen, ActivityScreen) and route.action == "retry_delivery":
                screen.queue_delivery_retry(route.filter_values().get("category", "webhook"))
                return
            if isinstance(screen, CalendarScreen):
                filters = route.filter_values()

                def calendar_action() -> None:
                    if route.action == "respond" and filters.get("response"):
                        screen.action_respond(CalendarResponse(filters["response"]))
                        return
                    if route.action == "edit" and filters.get("field"):
                        person = self._palette_scalar(invocation.get("person"))
                        screen.action_edit(
                            filters["field"],
                            field_value=person,
                            field_operation=(
                                "remove"
                                if route.command_id == "calendar.remove_attendee"
                                else "add"
                            ),
                        )
                        return
                    method = getattr(screen, route.action_method or "", None)
                    if callable(method):
                        method()

                screen.queue_action(
                    route.command_id.replace("calendar.", "").replace("_", " "),
                    calendar_action,
                    requires_selection=route.action
                    in {
                        "cancel_meeting",
                        "delete",
                        "edit",
                        "export",
                        "propose_new_time",
                        "related",
                        "respond",
                    },
                )
                return
            if isinstance(screen, ComposeScreen) and route.action == "schedule_draft":
                screen.action_schedule_draft(
                    expression=route.filter_values().get("expression", "tomorrow 09:00")
                )
                return
            if isinstance(screen, ScheduledScreen) and route.action in {
                "duplicate",
                "reschedule",
            }:
                expression = self._palette_scalar(invocation.get("when"))
                if expression:
                    screen.query_one("#scheduled-time", Input).value = expression
                method = getattr(screen, route.action_method or "", None)
                if callable(method):
                    method()
                return
            if isinstance(screen, ThemeBrowserScreen):
                if route.action == "focus_search":
                    query = self._palette_scalar(invocation.get("query"))
                    search = screen.query_one("#theme-search", Input)
                    if query:
                        search.value = query
                    search.focus()
                    return
                if route.action in {"import_theme", "export_theme"}:
                    path = self._palette_scalar(invocation.get("path"))
                    if path:
                        screen.query_one("#theme-transfer-path", Input).value = path
            if isinstance(screen, SettingsScreen):
                if route.action == "open_transfer":
                    mode = route.intent if route.intent in {"export", "import"} else "export"
                    screen.action_open_transfer(mode, mode == "export")
                    return
                if route.action in {"reset_setting", "reset_category"}:
                    scope = "setting" if route.action == "reset_setting" else "category"
                    parameter = "setting" if scope == "setting" else "settings_category"
                    target = self._palette_scalar(invocation.get(parameter))
                    if target:
                        screen.queue_reset(scope, target)
                    elif screen_was_open:
                        if scope == "setting":
                            screen.action_reset_setting()
                        else:
                            screen.action_reset_category()
                    else:
                        screen.action_focus_search()
                        self._set_status(f"Select a {scope}, then run Reset {scope.title()} again.")
                    return
            if isinstance(screen, TasksScreen):
                filters = route.filter_values()
                if route.action == "edit" and filters.get("field"):
                    screen.action_edit(filters["field"])
                    return
            if isinstance(screen, ContactsScreen) and route.action == "directory":
                query = self._contacts_invocation_query(invocation)
                if query:
                    screen.configure_search(query, directory=True)
                else:
                    screen.action_directory()
                return
            if isinstance(screen, NotificationSettingsScreen) and route.action == "create_rule":
                predicate = route.filter_values().get("predicate")
                value: str | None = None
                if predicate == "sender":
                    value = self._palette_scalar(invocation.get("person"))
                    if value is None:
                        sender = self._selected_message_sender()
                        value = sender.email or sender.name if sender is not None else None
                elif predicate == "subject":
                    value = self._palette_scalar(invocation.get("subject"))
                    if value is None:
                        summary = self._selected_message_summary()
                        value = summary.subject if summary is not None else None
                screen.action_create_rule(predicate, value)
                return
            if (
                isinstance(screen, NotificationSettingsScreen)
                and route.action == "configure_folder_scope"
            ):
                filters = route.filter_values()
                folder = self._palette_scalar(invocation.get("folder"))
                if not folder and filters.get("scope") == "add":
                    selected = self._selected_folder_info()
                    folder = selected.path if selected is not None else None
                screen.action_configure_folder_scope(filters.get("scope", "add"), folder)
                return
            if (
                isinstance(screen, NotificationSettingsScreen)
                and route.action == "set_desktop_notifications"
            ):
                enabled = route.filter_values().get("enabled", "true") == "true"
                screen.action_set_desktop_notifications(enabled)
                return
            if (
                isinstance(screen, NotificationSettingsScreen)
                and route.action == "set_notification_privacy"
            ):
                screen.action_set_notification_privacy(route.filter_values().get("mode", "private"))
                return
            if (
                isinstance(screen, NotificationSettingsScreen)
                and route.action == "configure_webhook"
            ):
                screen.action_configure_webhook(
                    route.filter_values().get("provider_kind", "webhook")
                )
                return
            if (
                isinstance(screen, NotificationSettingsScreen)
                and route.action == "show_webhook_configuration"
            ):
                screen.action_show_webhook_configuration(
                    self._palette_scalar(invocation.get("name"))
                )
                return
            if isinstance(screen, StorageScreen) and route.action == "analyze":
                filters = route.filter_values()
                focus_label: str | None = None
                if filters.get("selection") == "current_folder":
                    selected_folder = self._selected_folder_info()
                    focus_label = (
                        selected_folder.path if selected_folder is not None else self.ui.folder
                    )
                focus_label = {
                    "storage.show_local_cache_size": "Cache",
                    "storage.show_search_index_size": "Index",
                    "storage.show_backup_storage": "Backups",
                }.get(route.command_id, focus_label)
                screen.queue_analysis(filters.get("analysis"), focus_label=focus_label)
                return
            if isinstance(screen, StorageScreen) and route.action == "cleanup_plan":
                screen.queue_cleanup_plan()
                return
            if isinstance(screen, LocalSearchScreen) and route.action == "search":
                filters = route.filter_values()
                mode = LocalSearchMode(filters.get("mode", LocalSearchMode.ALL.value))
                local_query = next(
                    (
                        value
                        for key in ("query", "text", "subject", "person")
                        if (value := self._palette_scalar(invocation.get(key)))
                    ),
                    None,
                )
                if (
                    route.command_id == "search.search_backups"
                    and (invocation.original_text or "").strip().casefold() == "search"
                ):
                    local_query = None
                if route.command_id == "search.search_all_data" and not local_query:
                    # Reopening the workspace is distinct from an explicit all-source query.
                    mode = LocalSearchMode(self._last_search_scope)
                reference = (
                    self._palette_scalar(invocation.get("reference")) or self.ui.selected_reference
                )
                runnable = bool(local_query) or mode in {
                    LocalSearchMode.DUPLICATES,
                    LocalSearchMode.NEAR_DUPLICATES,
                    LocalSearchMode.SEMANTIC,
                }
                runnable = runnable or (mode is LocalSearchMode.SIMILAR and bool(reference))
                screen.configure(
                    mode=mode,
                    query=local_query,
                    reference=reference,
                    backup_id=contextual_backup_id,
                    run=runnable,
                )
                return
            method = getattr(screen, route.action_method or "", None)
            if callable(method):
                method()

        screen = self._route_screen(route.target)
        if screen is None:
            if route.target is RouteTarget.COMPOSER:
                self.action_compose()
                composer = self._route_screen(route.target)
                if isinstance(composer, ComposeScreen):
                    composer.defer_status_action(lambda: invoke(composer))
                return
            self._catalog_open(route, invocation)
            if route.target is RouteTarget.CONTACTS and route.action == "directory":
                return

            def invoke_after_mount() -> None:
                invoke(self._route_screen(route.target))

            self.set_timer(0.08, invoke_after_mount)
            return
        if (isinstance(screen, ActivityScreen) and route.action == "control_watcher") or (
            isinstance(screen, NotificationSettingsScreen)
            and route.action == "configure_folder_scope"
        ):
            invoke(screen)
            return
        method = getattr(screen, route.action_method or "", None)
        if not callable(method):
            self._set_status(
                f"{route.target.value.title()} action unavailable: "
                f"{sanitize_terminal(route.action)}"
            )
            return
        invoke(screen)

    def _calendar_invocation_date(self, invocation: CommandInvocation) -> date | None:
        zone = ZoneInfo(self.context.config.timezone)
        for key in ("when", "date", "date_range"):
            value = invocation.get(key)
            if isinstance(value, datetime):
                return value.astimezone(zone).date() if value.tzinfo else value.date()
            if isinstance(value, date):
                return value
            for attribute in ("utc", "start"):
                candidate = getattr(value, attribute, None)
                if isinstance(candidate, datetime):
                    return (
                        candidate.astimezone(zone).date() if candidate.tzinfo else candidate.date()
                    )
            if isinstance(value, str):
                try:
                    return date.fromisoformat(value.strip())
                except ValueError:
                    pass
        return None

    def _palette_calendar_search(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        self._open_area("calendar")
        query = (
            self._palette_scalar(invocation.get("query"))
            or self._palette_scalar(invocation.get("person"))
            or invocation.original_text
            or ""
        )
        for prefix in ("search calendar for ", "search calendar ", "show meetings with "):
            if query.casefold().startswith(prefix):
                query = query[len(prefix) :]
                break

        def configure() -> None:
            screen = self._route_screen(RouteTarget.CALENDAR)
            if isinstance(screen, CalendarScreen):
                search = screen.query_one("#calendar-search", Input)
                search.value = query.strip()
                search.focus()
                screen.action_refresh()

        self.set_timer(0.05, configure)

    @staticmethod
    def _contacts_invocation_query(invocation: CommandInvocation) -> str:
        query = (
            GUCmailApp._palette_scalar(invocation.get("query"))
            or GUCmailApp._palette_scalar(invocation.get("person"))
            or GUCmailApp._palette_scalar(invocation.get("email"))
            or invocation.original_text
            or ""
        ).strip()
        for prefix in (
            "search contacts for ",
            "search contacts ",
            "search directory for ",
            "search directory ",
            "resolve email address for ",
            "resolve email address ",
        ):
            if query.casefold().startswith(prefix):
                return query[len(prefix) :].strip()
        return query

    def _palette_contacts_search(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        self._open_area("contacts")
        query = self._contacts_invocation_query(invocation)

        def configure() -> None:
            screen = self._route_screen(RouteTarget.CONTACTS)
            if isinstance(screen, ContactsScreen):
                screen.configure_search(query)

        self.set_timer(0.05, configure)

    def _palette_task_create(
        self,
        _invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        if not isinstance(self._route_screen(RouteTarget.TASKS), TasksScreen):
            self._open_area("tasks")

        def create() -> None:
            screen = self._route_screen(RouteTarget.TASKS)
            if isinstance(screen, TasksScreen):
                screen.action_create()

        self.set_timer(0.05, create)

    def _palette_task_complete(
        self,
        _invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        if not isinstance(self._route_screen(RouteTarget.TASKS), TasksScreen):
            self._open_area("tasks")

        def complete() -> None:
            screen = self._route_screen(RouteTarget.TASKS)
            if isinstance(screen, TasksScreen):
                screen.action_complete()

        self.set_timer(0.05, complete)

    def _palette_calendar_create(
        self,
        _invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        self._open_area("calendar")

        def create() -> None:
            screen = self._route_screen(RouteTarget.CALENDAR)
            if isinstance(screen, CalendarScreen):
                screen.action_create()

        self.set_timer(0.08, create)

    def _palette_calendar_create_tomorrow(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        """Open a real create-event editor prefilled for tomorrow at 09:00 local time."""

        self._open_area("calendar")
        subject = self._palette_scalar(invocation.get("subject")) or ""

        def create() -> None:
            screen = self._route_screen(RouteTarget.CALENDAR)
            if isinstance(screen, CalendarScreen):
                local_now = datetime.now(ZoneInfo(self.context.config.timezone))
                tomorrow = (local_now + timedelta(days=1)).replace(
                    hour=9,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                screen.action_create_prefilled((), subject=subject, start=tomorrow)

        self.set_timer(0.08, create)

    def _catalog_settings(
        self,
        route: PaletteRoute,
        invocation: CommandInvocation,
    ) -> None:
        if route.intent in {"set_false", "set_true", "toggle", "cycle"} and route.setting_id:
            service = self._settings_service()
            try:
                current = service.value(route.setting_id).effective_value
                if route.intent == "set_false":
                    updated: object = False
                elif route.intent == "set_true":
                    updated = True
                elif route.intent == "toggle":
                    if not isinstance(current, bool):
                        raise ConfigurationError(f"{route.setting_id} is not a boolean setting.")
                    updated = not current
                else:
                    cycles = {
                        "appearance.density": (
                            "theme",
                            "compact",
                            "comfortable",
                            "spacious",
                        )
                    }
                    values = cycles.get(route.setting_id)
                    if values is None:
                        raise ConfigurationError(
                            f"{route.setting_id} does not define a palette cycle."
                        )
                    try:
                        index = values.index(str(current))
                    except ValueError:
                        index = -1
                    updated = values[(index + 1) % len(values)]
                revision = service.set(
                    route.setting_id,
                    updated,
                    reason=f"Palette: {route.command_id}",
                )
            except (ConfigurationError, KeyError, ValueError) as exc:
                self._set_status(_safe_failure(exc, action="Appearance setting failed"))
                return
            changed = (
                tuple(change.setting_id for change in revision.changes)
                if revision is not None
                else ()
            )
            if changed:
                self._apply_committed_settings(changed)
            label = route.setting_id.rsplit(".", 1)[-1].replace("_", " ").title()
            self._set_status(f"{label}: {updated}")
            return

        if route.intent == "reset_category" and route.settings_category:
            service = self._settings_service()
            try:
                if route.command_id == "appearance.reset_appearance":
                    revision = (
                        service.begin()
                        .reset_category(route.settings_category)
                        .reset("accessibility.high_contrast")
                        .reset("accessibility.monochrome")
                        .apply(reason=f"Palette: {route.command_id}")
                    )
                else:
                    revision = service.reset_category(
                        route.settings_category,
                        reason=f"Palette: {route.command_id}",
                    )
            except (ConfigurationError, KeyError, ValueError) as exc:
                self._set_status(_safe_failure(exc, action="Appearance reset failed"))
                return
            changed = (
                tuple(change.setting_id for change in revision.changes)
                if revision is not None
                else ()
            )
            if changed:
                self._apply_committed_settings(changed)
            self._set_status(f"Reset {route.settings_category} preferences")
            return

        self._open_settings_surface(
            category=route.settings_category if route.intent == "open_category" else None,
            query=(
                self._palette_scalar(invocation.get("query"))
                or route.setting_id
                or route.settings_category
                or ""
            )
            if route.intent != "open_category"
            else None,
        )

    def _open_settings_surface(
        self,
        *,
        category: str | None = None,
        query: str | None = None,
    ) -> None:
        screen_open = isinstance(self._route_screen(RouteTarget.SETTINGS), SettingsScreen)
        if not screen_open:
            self._open_area("settings")

        def configure() -> None:
            screen = self._route_screen(RouteTarget.SETTINGS)
            if not isinstance(screen, SettingsScreen):
                return
            try:
                if category:
                    screen.action_open_category(category)
                elif query is not None:
                    screen.action_search(query)
                else:
                    screen.action_focus_search()
            except NoMatches:
                return

        if screen_open:
            configure()
        else:
            self.set_timer(0.05, configure)

    def _palette_settings_search(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        self._open_settings_surface(query=self._palette_scalar(invocation.get("query")) or "")

    def _catalog_help(
        self,
        route: PaletteRoute,
        invocation: CommandInvocation,
    ) -> None:
        query = route.query or ""
        page_id = route.help_topic
        if route.command_id == "help.explain_a_selected_setting":
            query = self._palette_scalar(invocation.get("reference")) or ""
            settings_screen = self._route_screen(RouteTarget.SETTINGS)
            if not query and isinstance(settings_screen, SettingsScreen):
                query = settings_screen._selected_setting_id or ""
            normalized = query.casefold()
            selected = next(
                (
                    setting
                    for setting in DEFAULT_SETTINGS
                    if normalized
                    in {
                        setting.setting_id.casefold(),
                        setting.label.casefold(),
                        *(alias.casefold() for alias in setting.aliases),
                    }
                ),
                None,
            )
            if selected is not None:
                page_id = selected.help_id
        self.push_screen(
            HelpScreen(
                context=self._help_runtime_context(self._active_palette_area()),
                initial_page_id=page_id,
                initial_query=query,
                store=self._help_state_store(),
            )
        )

    def _selected_exchange_folder(self) -> tuple[str, str] | None:
        tree = self.query_one("#folder-tree", Tree)
        node = tree.cursor_node
        data = node.data if node is not None else None
        if not isinstance(data, dict) or data.get("kind") not in {"folder", "folder_branch"}:
            return None
        reference = str(data.get("reference") or "")
        if not reference or reference not in self._folder_nodes:
            return None
        path = str(data.get("path") or self._folder_paths.get(reference) or reference)
        return reference, path

    def _selected_folder_info(self) -> FolderInfo | None:
        tree = self.query_one("#folder-tree", Tree)
        node = tree.cursor_node
        data = node.data if node is not None else None
        if not isinstance(data, dict):
            return None
        folder = data.get("folder")
        return folder if isinstance(folder, FolderInfo) else None

    def _prompt_folder_create(self, invocation: CommandInvocation) -> None:
        parent = self._selected_folder_info()
        parent_reference = parent.reference if parent is not None else None
        parent_label = parent.path if parent is not None else "the mailbox root"
        initial_name = self._palette_scalar(invocation.get("folder")) or ""

        def submitted(value: str | None) -> None:
            if value:
                self._prepare_folder_mutation(
                    "create",
                    reference=parent_reference,
                    value=value,
                )

        self.push_screen(
            FolderNameScreen(
                "Create folder",
                f"Parent: {parent_label}\nEnter the new folder display name.",
                initial_name=initial_name,
                submit_label="Preview create",
            ),
            submitted,
        )

    def _palette_create_folder(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        self._prompt_folder_create(invocation)

    def _prompt_folder_rename(self) -> None:
        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before choosing Rename folder.")
            return

        def submitted(value: str | None) -> None:
            if value:
                self._prepare_folder_mutation(
                    "rename",
                    reference=folder.reference,
                    value=value,
                )

        self.push_screen(
            FolderNameScreen(
                "Rename folder",
                f"Folder: {folder.path}\nEnter the replacement display name.",
                initial_name=folder.name,
                submit_label="Preview rename",
            ),
            submitted,
        )

    def _prompt_folder_delete(self) -> None:
        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before choosing Delete folder.")
            return
        self._prepare_folder_mutation("delete", reference=folder.reference)

    def _prompt_folder_empty(self) -> None:
        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before choosing Empty folder.")
            return
        self._prepare_folder_mutation("empty", reference=folder.reference)

    @work(thread=True, exclusive=True, group="folder-mutation")
    def _prepare_folder_empty_path(self, folder: str) -> None:
        """Resolve an explicit safe folder selector before showing strong confirmation."""

        try:
            plan = FolderService(self._ensure_gateway()).plan_empty(folder)
            self.call_from_thread(self._confirm_folder_mutation, plan)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Folder empty unavailable"),
            )

    @work(thread=True, exclusive=True, group="folder-mutation")
    def _prepare_folder_mutation(
        self,
        operation: str,
        *,
        reference: str | None,
        value: str | None = None,
    ) -> None:
        try:
            service = FolderService(self._ensure_gateway())
            if operation == "create":
                plan = service.plan_create(
                    value or "",
                    parent=f"id:{reference}" if reference is not None else None,
                )
            elif operation == "rename":
                plan = service.plan_rename(f"id:{reference}", value or "")
            elif operation == "delete":
                plan = service.plan_delete(f"id:{reference}")
            elif operation == "empty":
                plan = service.plan_empty(f"id:{reference}")
            else:
                raise ConfigurationError(f"Unsupported folder operation: {operation}")
            self.call_from_thread(self._confirm_folder_mutation, plan)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action=f"Folder {operation} unavailable"),
            )

    def _confirm_folder_mutation(self, plan: MutationPlan) -> None:
        operation = plan.operation.removeprefix("folder_").replace("_", " ")
        detail = plan.summary
        if plan.operation == "folder_empty":
            detail += (
                "\n\nStrong confirmation: this affects every item in the selected folder. "
                "The folder itself remains."
            )
        elif plan.operation == "folder_delete":
            detail += "\n\nThe selected folder and its contents will move to Deleted Items."

        def confirmed(value: bool | None) -> None:
            if value:
                self._execute_folder_mutation(plan)

        self.push_screen(
            ConfirmationScreen(f"Confirm folder {operation}", detail),
            confirmed,
        )

    @work(thread=True, exclusive=False, group="folder-mutation")
    @coordinated_write()
    def _execute_folder_mutation(self, plan: MutationPlan) -> None:
        try:
            execution = FolderService(self._ensure_gateway()).execute_mutation(
                plan,
                confirmation_scope=(
                    plan.confirmation_scope if plan.requires_confirmation else None
                ),
            )
            self.call_from_thread(self._folder_mutation_finished, plan, execution.results)
        except Exception as exc:
            operation = plan.operation.removeprefix("folder_").replace("_", " ")
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action=f"Folder {operation} failed"),
            )

    def _folder_mutation_finished(
        self,
        plan: MutationPlan,
        results: tuple[Any, ...],
    ) -> None:
        operation = plan.operation.removeprefix("folder_").replace("_", " ")
        result = results[0] if results else None
        if isinstance(result, FolderInfo):
            self.ui.folder = result.reference
            self._persist_setting_values(
                {"mail.last_folder": result.reference}, reason="Last folder updated"
            )
            if result.parent_reference:
                self._expanded_folders.add(result.parent_reference)
                self._persist_folder_expansion()
        elif plan.operation == "folder_delete" and self.ui.folder in plan.targets:
            self.ui.folder = self.context.config.default_folder
            self._persist_setting_values(
                {"mail.last_folder": self.ui.folder}, reason="Last folder updated"
            )
        self.action_refresh()
        self._set_status(f"Folder {operation} completed · refreshing tree")

    def _show_selected_folder_details(self) -> None:
        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before showing folder details.")
            return
        self.push_screen(FolderDetailsScreen(folder))

    def _show_selected_folder_unread(self) -> None:
        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before showing its unread count.")
            return
        count = "unavailable" if folder.unread_count is None else str(folder.unread_count)
        total = "unavailable" if folder.total_count is None else str(folder.total_count)
        self._set_status(
            f"{sanitize_terminal(folder.path, max_length=160)} · Unread {count} · Total {total}"
        )

    def _prompt_folder_move(self, invocation: CommandInvocation) -> None:
        selected = self._selected_exchange_folder()
        if selected is None:
            self._set_status("Select a movable Exchange folder before choosing Move folder.")
            return
        source_reference, source_path = selected
        destination = self._palette_scalar(invocation.get("folder"))
        if destination and destination not in {source_reference, source_path}:
            self._prepare_folder_move(source_reference, destination)
            return

        def submitted(value: str | None) -> None:
            if value:
                self._prepare_folder_move(source_reference, value)

        self.push_screen(FolderMoveScreen(source_path), submitted)

    @work(thread=True, exclusive=True, group="folder-move")
    def _prepare_folder_move(self, source_reference: str, destination: str) -> None:
        try:
            plan = FolderService(self._ensure_gateway()).plan_move(
                f"id:{source_reference}",
                destination,
            )
            self.call_from_thread(self._confirm_folder_move, plan)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Folder move unavailable"),
            )

    def _confirm_folder_move(self, plan: MutationPlan) -> None:
        def confirmed(value: bool | None) -> None:
            if value:
                self._execute_folder_move(plan)

        self.push_screen(
            ConfirmationScreen("Confirm folder move", plan.summary),
            confirmed,
        )

    @work(thread=True, exclusive=False, group="folder-move")
    @coordinated_write()
    def _execute_folder_move(self, plan: MutationPlan) -> None:
        try:
            result = FolderService(self._ensure_gateway()).execute_mutation(
                plan,
                confirmation_scope=plan.confirmation_scope,
            )
            moved = result.results[0]
            destination = moved.path if isinstance(moved, FolderInfo) else "the destination"
            self.call_from_thread(self.action_refresh)
            self.call_from_thread(
                self._set_status,
                "Folder moved to "
                f"{sanitize_terminal(destination, max_length=160)} · refreshing tree",
            )
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Folder move failed"),
            )

    def _catalog_direct(self, route: PaletteRoute, invocation: CommandInvocation) -> None:
        action = route.action or ""
        if route.target is RouteTarget.APPLICATION and action == "leave_offline_mode":
            self._palette_leave_offline(invocation, self._palette_context())
        elif route.target is RouteTarget.MAILBOX and action == "select_visible_messages":
            mode = route.filter_values().get("mode", "visible")
            normalized = {
                "visible": "all",
                "visible_unread": "unread",
                "visible_read": "read",
                "visible_same_sender": "sender",
                "current_search_results": "search",
            }.get(mode, mode)
            self._select_visible_messages(normalized)
        elif route.target is RouteTarget.MAILBOX and action == "mark_bounded_read":
            self._preview_bulk_mark_read(route.filter_values().get("scope", "selected"))
        elif route.target is RouteTarget.MAILBOX and action == "export_selected_messages":
            self._prompt_selected_message_export()
        elif route.target is RouteTarget.BACKUP and action == "backup_selected_messages":
            summaries = self._selected_message_summaries()
            if not summaries:
                self._set_status("Select one or more visible messages before opening backup.")
            else:
                self._open_backup_area(
                    selected_messages=tuple(item.reference for item in summaries)
                )
        elif route.target is RouteTarget.MESSAGE_READER and action == "open_selected_externally":
            self._preview_open_selected_externally()
        elif route.target is RouteTarget.ATTACHMENTS and action == "list_metadata":
            self._palette_open_attachments(invocation, self._palette_context())
        elif route.target is RouteTarget.MAILBOX and action == "open_folder":
            folder = route.filter_values().get("folder") or self._palette_scalar(
                invocation.get("folder")
            )
            routed = CommandInvocation.from_mapping(
                "folders.open", {"folder": folder} if folder else {}
            )
            self._palette_open_folder(routed, self._palette_context())
        elif route.target is RouteTarget.MAILBOX and action == "open_message":
            reference = (
                self._palette_scalar(invocation.get("reference")) or self.ui.selected_reference
            )
            if route.intent == "containing_message":
                attachment_screen = self._route_screen(RouteTarget.ATTACHMENTS)
                if isinstance(attachment_screen, DraftAttachmentScreen):
                    reference = (
                        self._palette_scalar(invocation.get("reference"))
                        or attachment_screen.query_one("#attachment-owner", Input).value.strip()
                        or self.ui.selected_reference
                    )
            if not reference:
                self._set_status("Select a message before opening it.")
            else:
                self.load_message(
                    reference,
                    mode="full"
                    if route.intent == "full_reader"
                    else "details"
                    if route.intent == "advanced_details"
                    else "auto",
                )
        elif route.target is RouteTarget.MAILBOX and action == "set_importance":
            self._prompt_message_importance(invocation)
        elif route.target is RouteTarget.MAILBOX and action in {
            "add_category",
            "remove_category",
        }:
            self._prompt_message_category(invocation, mode=action.removesuffix("_category"))
        elif route.target is RouteTarget.MAILBOX and action == "copy_message":
            self._prompt_message_copy(invocation)
        elif route.target is RouteTarget.MAILBOX and action == "permanently_delete_message":
            self._preview_permanent_message_delete()
        elif route.target is RouteTarget.MAILBOX and action == "open_message_section":
            reference = self.ui.selected_reference
            if not reference:
                self._set_status("Select a message before opening its source or headers.")
                return
            section = route.filter_values().get("section")

            def load() -> str:
                if section == "raw_mime":
                    raw = cast(bytes, self._ensure_gateway().raw_message(reference))
                    return raw.decode("utf-8", errors="replace")
                detail = self._ensure_gateway().show_message(reference)
                return "\n".join(
                    f"{sanitize_terminal(key)}: {sanitize_terminal(value, max_length=2_000)}"
                    for key, value in detail.raw_headers.items()
                )

            self.push_screen(AreaScreen("Raw MIME" if section == "raw_mime" else "Headers", load))
        elif route.target is RouteTarget.MAILBOX and action in {
            "copy_message_field",
            "copy_message_text",
        }:
            detail = (
                self._message_detail if isinstance(self._message_detail, MessageDetail) else None
            )
            if detail is None:
                self._set_status("Open a message before copying its details.")
                return
            field = route.filter_values().get("field")
            value = (
                detail.internet_message_id
                if field == "internet_message_id"
                else detail.summary.sender.email
                if field == "sender_address" and detail.summary.sender
                else detail.summary.subject
                if field == "subject"
                else self._format_message(detail)
            )
            self.copy_to_clipboard(str(value or ""))
            self._set_status("Copied sanitized message information to the clipboard")
        elif route.target is RouteTarget.MESSAGE_READER and action == "close_floating_reader":
            with contextlib.suppress(NoMatches):
                self.query_one("#floating-reader", FloatingReader).remove()
        elif route.target is RouteTarget.SEARCH and action == "clear":
            self._current_search_text = ""
            self.ui.search = ""
            self.query_one("#message-filter", Input).value = ""
            self.refresh_mailbox()
        elif route.target is RouteTarget.SEARCH and action == "refine_results":
            query = self._search_operand(
                invocation,
                "search within results for ",
                "search within results ",
                "refine results for ",
                "refine results ",
            )
            if query:
                self._refine_current_results(query)
            else:
                self._prompt_refine_current_results()
        elif route.target is RouteTarget.APPLICATION and action == "apply_layout_preset":
            preset = route.filter_values().get("preset")
            self._preview_visible = preset != "list_only"
            self._split_orientation = "horizontal" if preset == "reading" else "vertical"
            self._message_ratio = (
                0.30 if preset == "reading" else 0.55 if preset == "compact" else 0.40
            )
            self._layout_last_preset = str(preset or "default")
            self._persist_message_layout(reason="Layout preset applied")
            self._apply_layout()
        elif route.target is RouteTarget.APPLICATION and action == "resize_sidebar":
            navigation = self.query_one("#navigation", Vertical)
            current = max(navigation.region.width, 16)
            direction = route.filter_values().get("direction")
            delta = 2 if direction == "wider" else -2
            next_width = max(16, min(40, current + delta))
            available = max(self.query_one("#mail-layout", Horizontal).region.width, 1)
            self._sidebar_ratio = self._validated_sidebar_ratio(next_width / available)
            self._layout_last_preset = "custom"
            self._persist_message_layout()
            self._apply_layout()
        elif route.target is RouteTarget.COMPOSER:
            composer = self.screen if isinstance(self.screen, ComposeScreen) else None
            if composer is None:
                self._set_status("Open the composer before using this command.")
                return
            if action == "close":
                if route.intent == "discard_after_recovery":
                    self._preview_discard_composer(composer)
                else:
                    composer.action_close()
            elif action == "save":
                if composer._guard_running_change():
                    return
                payload = composer._payload(action="save")
                payload["save_mode"] = route.intent or "local_then_server"
                composer.set_save_preflight_pending(True)
                composer._set_note("Checking attachment files…")
                self.prepare_draft_save(composer, payload)
            elif action == "review_send":
                composer.action_review_send()
            elif action == "set_importance":
                if composer._guard_running_change():
                    return
                value = self._palette_scalar(invocation.get("importance")) or "high"
                composer.query_one("#compose-importance", Select).value = value
            elif action == "insert_signature":
                if composer._guard_running_change():
                    return
                signature = str(
                    self._settings_service().value("composer.signature").effective_value
                )
                if composer.query_one("#compose-body-mode", Select).value == "html":
                    signature = html.escape(signature).replace("\n", "<br>")
                editor = composer.query_one("#compose-body", TextArea)
                editor.text = (editor.text.rstrip() + "\n\n" + signature).strip()
        elif route.target is RouteTarget.FOLDER_TREE and action == "create_folder":
            self._prompt_folder_create(invocation)
        elif route.target is RouteTarget.FOLDER_TREE and action == "rename_selected_folder":
            self._prompt_folder_rename()
        elif route.target is RouteTarget.FOLDER_TREE and action == "move_selected_folder":
            self._prompt_folder_move(invocation)
        elif route.target is RouteTarget.FOLDER_TREE and action == "delete_selected_folder":
            self._prompt_folder_delete()
        elif route.target is RouteTarget.FOLDER_TREE and action == "empty_selected_folder":
            folder = self._palette_scalar(invocation.get("folder"))
            if folder:
                self._prepare_folder_empty_path(folder)
            else:
                self._prompt_folder_empty()
        elif route.target is RouteTarget.FOLDER_TREE and action == "show_selected_details":
            self._show_selected_folder_details()
        elif route.target is RouteTarget.FOLDER_TREE and action == "show_selected_unread_count":
            self._show_selected_folder_unread()
        elif route.target is RouteTarget.FOLDER_TREE and action in {
            "expand_selected",
            "collapse_selected",
            "expand_all_bounded",
            "collapse_all",
            "navigate_next_folder",
            "navigate_previous_folder",
        }:
            tree = self.query_one("#folder-tree", Tree)
            cursor_node = tree.cursor_node
            if action == "expand_selected":
                if cursor_node is not None:
                    cursor_node.expand()
            elif action == "collapse_selected":
                if cursor_node is not None:
                    cursor_node.collapse()
            elif action == "expand_all_bounded":
                self._folder_expand_all_remaining = 500
                tree.root.expand_all()
                self._load_rendered_folder_pages_for_expand_all(tree.root)
            elif action == "collapse_all":
                self._folder_expand_all_remaining = 0
                tree.root.collapse_all()
            else:
                self._navigate_visible_mail_folder(tree, forward=action == "navigate_next_folder")
        else:
            self._set_status(
                f"This route needs more input in {route.target.value}: {sanitize_terminal(action)}"
            )

    def _navigate_visible_mail_folder(self, tree: Tree[Any], *, forward: bool) -> None:
        """Navigate actual visible folders, skipping product areas and paging rows."""
        folders: list[Any] = []

        def visit(node: Any) -> None:
            data = node.data
            if (
                isinstance(data, dict)
                and data.get("kind") in {"folder", "folder_branch"}
                and data.get("reference")
            ):
                folders.append(node)
            if node.is_expanded:
                for child in node.children:
                    visit(child)

        visit(tree.root)
        if not folders:
            self._set_status("Expand the folder tree before navigating adjacent folders.")
            return
        current = next(
            (
                index
                for index, node in enumerate(folders)
                if node.data.get("reference") == self.ui.folder
            ),
            -1 if forward else 0,
        )
        tree.select_node(folders[(current + (1 if forward else -1)) % len(folders)])

    def _prompt_refine_current_results(self) -> None:
        if not self._messages:
            self._set_status("There are no current results to refine.")
            return

        def accepted(query_text: str | None) -> None:
            if query_text:
                self._refine_current_results(query_text)

        self.push_screen(
            FolderNameScreen(
                "Search within current results",
                "Refine only the bounded rows already loaded; this does not contact Exchange.",
                submit_label="Refine",
                placeholder="Text or structured expression",
            ),
            accepted,
        )

    def _refine_current_results(self, query_text: str) -> None:
        try:
            expression = parse_query(query_text, timezone=self.context.config.timezone)
            matches = tuple(item for item in self._messages if evaluate_residual(item, expression))
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Invalid refinement"))
            return
        previous_count = len(self._messages)
        self._messages = matches
        self._render_message_rows(matches)
        self._current_search_text = (
            f"({self._current_search_text}) AND ({query_text})"
            if self._current_search_text
            else query_text
        )
        self._set_status(
            f"Refined {previous_count} bounded result(s) to {len(matches)} "
            "without a network request"
        )

    def _palette_catalog_handler(
        self, invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        route = catalog_route(invocation.command_id)
        if route.is_gate:
            self._set_status(route.gate_reason or "This command needs an unavailable capability.")
        elif route.kind is RouteKind.SEARCH:
            self._catalog_search(route, invocation)
        elif route.kind is RouteKind.OPEN_SCREEN:
            self._catalog_open(route, invocation)
        elif route.kind is RouteKind.APPLICATION_ACTION:
            self._catalog_application_action(route, invocation)
        elif route.kind is RouteKind.SCREEN_ACTION:
            self._catalog_screen_action(route, invocation)
        elif route.kind is RouteKind.SETTINGS:
            self._catalog_settings(route, invocation)
        elif route.kind is RouteKind.HELP:
            self._catalog_help(route, invocation)
        else:
            self._catalog_direct(route, invocation)

    def _palette_webhook_test(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        self._open_notification_webhook_action(
            "test",
            name=self._palette_scalar(invocation.get("name")),
        )

    @work(thread=True, exclusive=True, group="draft-send-outcome")
    def _palette_draft_send_status(
        self, invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        from gucmail.services.send_attempts import SendAttemptStore

        reference = self._palette_scalar(invocation.get("reference"))
        try:
            attempt = SendAttemptStore(self.context.state).get(reference or "")
            outcome = attempt.state if attempt else "no recorded attempt"
            message = f"Draft send outcome: {outcome}."
            if outcome in {"sending", "uncertain"}:
                message += " Check Sent Items before composing another send; this draft is blocked."
            self.call_from_thread(self._set_status, message)
        except Exception as exc:
            self.call_from_thread(
                self._set_status, _safe_failure(exc, action="Send outcome unavailable")
            )

    def _palette_handlers(
        self,
    ) -> dict[str, Callable[[CommandInvocation, CommandContext], object]]:
        def action(
            callback: Callable[[], object],
        ) -> Callable[[CommandInvocation, CommandContext], object]:
            def handler(_invocation: CommandInvocation, _context: CommandContext) -> object:
                return callback()

            return handler

        handlers: dict[str, Callable[[CommandInvocation, CommandContext], object]] = {
            command_id: self._palette_open_folder
            for command_id in (
                "mail.open_inbox",
                "mail.open_sent",
                "mail.open_drafts",
                "mail.open_deleted",
                "mail.open_junk",
                "mail.open_archive",
                "folders.open",
            )
        }
        handlers.update(
            {
                "mail.open_unread": action(lambda: self._palette_search_preset(unread=True)),
                "mail.open_flagged": action(lambda: self._palette_search_preset(flagged=True)),
                "mail.refresh": action(self.action_refresh),
                "mail.open_message": self._palette_open_message,
                "mail.reply": action(self.action_reply),
                "mail.reply_all": action(self.action_reply_all),
                "mail.forward": action(self.action_forward),
                "mail.mark_read": self._palette_mark_read,
                "mail.mark_unread": self._palette_mark_read,
                "mail.flag": self._palette_set_flag,
                "mail.unflag": self._palette_set_flag,
                "mail.move": lambda invocation, _context: self._prompt_message_move(invocation),
                "mail.delete": action(self.action_delete),
                "mail.restore": action(self._preview_message_restore),
                "folders.create": self._palette_create_folder,
                "drafts.open_drafts": action(lambda: self._open_area("drafts")),
                "attachments.download": self._palette_download_attachments,
                "compose.new": self._palette_compose_new,
                "compose.reply_all": action(self.action_reply_all),
                "compose.recover_draft": self._palette_recover_draft,
                "draft.save": self._palette_save_current_draft,
                "draft.recover": self._palette_recover_draft,
                "draft.send_status": self._palette_draft_send_status,
                "scheduled.open": action(lambda: self._open_area("scheduled")),
                "scheduled.schedule_current": self._palette_schedule_current_draft,
                "scheduled.cancel": self._palette_cancel_scheduled,
                "search.mail": self._palette_search,
                "attachments.ocr": action(self.action_local_ocr),
                "attachments.open": self._palette_open_attachments,
                "attachments.search": self._palette_attachment_search,
                "calendar.open": action(lambda: self._open_area("calendar")),
                "calendar.tomorrow": action(
                    lambda: self._catalog_open(
                        catalog_route("calendar.open_tomorrow"),
                        CommandInvocation("calendar.open_tomorrow"),
                    )
                ),
                "calendar.show_tomorrow_events": action(
                    lambda: self._catalog_open(
                        catalog_route("calendar.open_tomorrow"),
                        CommandInvocation("calendar.show_tomorrow_events"),
                    )
                ),
                "calendar.create": self._palette_calendar_create,
                "calendar.create_tomorrow": self._palette_calendar_create_tomorrow,
                "calendar.search": self._palette_calendar_search,
                "contacts.open": action(lambda: self._open_area("contacts")),
                "contacts.groups": action(lambda: self._open_area("contact_groups")),
                "contacts.search": self._palette_contacts_search,
                "tasks.open": action(lambda: self._open_area("tasks")),
                "tasks.create": self._palette_task_create,
                "tasks.complete": self._palette_task_complete,
                "backup.open": action(lambda: self._open_area("backups")),
                "backup.create": lambda invocation, _context: self._catalog_screen_action(
                    catalog_route("backup.create_backup"), invocation
                ),
                "backup.verify": lambda invocation, _context: self._catalog_screen_action(
                    catalog_route("backup.inspect_backup"), invocation
                ),
                "storage.open": action(lambda: self._open_area("storage")),
                "insights.unanswered": action(self._open_unanswered),
                "storage.largest_messages": action(
                    lambda: self._open_storage_analysis("largest-messages")
                ),
                "storage.largest_attachments": action(
                    lambda: self._open_storage_analysis("largest-attachments")
                ),
                "cleanup.analyze": action(lambda: self._open_storage_analysis("mailbox-overview")),
                "monitoring.start": action(lambda: self._open_activity_with_control("start")),
                "monitoring.stop": action(lambda: self._open_activity_with_control("stop")),
                "monitoring.status": action(lambda: self._open_area("activity")),
                "notifications.settings": action(lambda: self._open_area("notifications")),
                "notifications.automatic_replies": action(
                    lambda: self._open_area("automatic_replies")
                ),
                "notifications.inbox_rules": action(lambda: self._open_area("inbox_rules")),
                "ews.delegates": action(lambda: self._open_area("delegates")),
                "notifications.test": action(
                    lambda: self._open_activity_with_notification_test("desktop")
                ),
                "notifications.history": action(
                    lambda: self._open_activity_filtered("notification")
                ),
                "integrations.open": action(lambda: self._open_notification_webhook_action("show")),
                "integrations.webhook_test": self._palette_webhook_test,
                "integrations.discord_test": action(
                    lambda: self._open_notification_webhook_action("test", provider_kind="discord")
                ),
                "appearance.settings": action(
                    lambda: self._open_settings_surface(category="Appearance")
                ),
                "appearance.apply_theme": self._palette_apply_theme,
                "appearance.toggle_banner": self._palette_toggle_banner,
                "layout.preview_wider": action(self.action_preview_wider),
                "layout.preview_narrower": action(self.action_preview_narrower),
                "layout.toggle_preview": action(self.action_toggle_preview),
                "layout.reset": action(self.action_reset_layout),
                "settings.open": action(lambda: self._open_area("settings")),
                "settings.search": self._palette_settings_search,
                "activity.open": action(lambda: self._open_area("activity")),
                "diagnostics.doctor": self._palette_doctor,
                "diagnostics.connection": self._palette_connection,
                "diagnostics.logs": self._palette_logs,
                "diagnostics.ews_capabilities": self._palette_ews_capabilities,
                "offline.enter": self._palette_enter_offline,
                "offline.browse": action(lambda: self._open_area("backups")),
                "help.open": self._palette_help_open,
                "help.tutorials": action(self._open_tutorials),
                "help.search": self._palette_help_search,
                "help.shortcuts": self._palette_help_shortcuts,
            }
        )
        handlers.update(
            {handler_id: self._palette_catalog_handler for handler_id in catalog_handler_ids()}
        )
        return handlers

    def _palette_usage(self) -> tuple[tuple[str, int], ...]:
        try:
            records = self.context.state.list_command_usage(
                context=self._active_palette_area(), limit=20
            )
            return tuple((record.command_id, record.use_count) for record in records)
        except Exception:
            return ()

    def _palette_supplemental_suggestions(
        self,
        runtime: CommandContext,
    ) -> tuple[PaletteSuggestion, ...]:
        """Return bounded local history rows without mailbox or network access."""

        registry = universal_palette_registry()
        rows: list[PaletteSuggestion] = []

        def add(
            command_id: str,
            *,
            group: str,
            display_name: str,
            reason: str,
            source: SuggestionSource,
            parameters: Mapping[str, object],
        ) -> None:
            invocation = CommandInvocation.from_mapping(
                command_id,
                parameters,
                original_text=display_name,
                explanation=reason,
            )
            command = registry.get(command_id)
            rows.append(
                PaletteSuggestion(
                    command=command,
                    score=1.0,
                    source=source,
                    availability=registry.availability(
                        command_id,
                        runtime,
                        invocation=invocation,
                    ),
                    invocation=invocation,
                    reason=reason,
                    group=group,
                    display_name=display_name,
                )
            )

        try:
            recent_searches = StateSavedSearchBackend(
                self.context.state,
                timezone=self.context.config.timezone,
                history_enabled=bool(self._setting_value("search.history_enabled", True)),
            ).list_recent(limit=1)
        except Exception:
            recent_searches = ()
        if recent_searches:
            recent_search = recent_searches[0]
            label = recent_search.name or recent_search.query_text
            add(
                "search.mail",
                group="Recent searches",
                display_name=f"Run · {label}",
                reason=(
                    f"Recent {recent_search.scope} search · "
                    f"{recent_search.result_count} prior result(s)"
                ),
                source=SuggestionSource.RECENT_SEARCH,
                parameters={"query": recent_search.query_text},
            )

        recent_folder = next(iter(self._recent_folder_paths), None)
        if recent_folder:
            add(
                "folders.open",
                group="Recent folders",
                display_name=f"Open · {recent_folder}",
                reason="Recently opened folder; resolved locally from bounded history",
                source=SuggestionSource.RECENT_FOLDER,
                parameters={"folder": recent_folder},
            )

        from gucmail.services.recipient_assistance import RecipientPreferences

        try:
            recent_person = next(
                iter(RecipientPreferences.from_settings(self._settings_service()).recent), None
            )
        except (ConfigurationError, OSError, ValueError):
            recent_person = None
        if recent_person:
            add(
                "compose.new",
                group="Recently contacted people",
                display_name=f"Compose to · {recent_person}",
                reason="Recipient from a successful local send-history record",
                source=SuggestionSource.RECENT_PERSON,
                parameters={
                    "recipient": PersonEntity(
                        recent_person,
                        recent_person if "@" in recent_person else None,
                        source="local_recipient_history",
                    )
                },
            )
        return tuple(rows)

    def _recent_palette_commands(self) -> tuple[str, ...]:
        usage = tuple(command_id for command_id, _count in self._palette_usage())
        configured = self._setting_value("palette.command_history", ())
        stored = (
            tuple(str(command_id) for command_id in configured)
            if isinstance(configured, (list, tuple))
            else ()
        )
        # ``palette.command_history`` is maintained most-recent-first, while the usage query is
        # deliberately frequency-first.  Preserve those two independent ranking signals instead
        # of accidentally presenting frequency order as recency order.
        return tuple(dict.fromkeys((*stored, *usage)))[:20]

    def _execute_palette_submission(self, submission: PaletteSubmission) -> None:
        composer = self.screen if isinstance(self.screen, ComposeScreen) else None
        route = CATALOG_ROUTES.get(submission.invocation.command_id)
        if composer is not None and (
            (route is not None and route.target is RouteTarget.COMPOSER)
            or submission.invocation.command_id in {"draft.save", "scheduled.schedule_current"}
        ) and composer.defer_status_action(lambda: self._execute_palette_submission(submission)):
            return
        executor = CommandExecutor(universal_palette_registry(), self._palette_handlers())
        runtime = self._palette_context(tuple(executor.handler_ids))
        try:
            if submission.invocation.command_id == "mail.restore":
                plan = executor.prepare(submission.invocation, runtime)
                if not plan.executable:
                    raise ConfigurationError(
                        "; ".join((*plan.availability.reasons, *plan.validation_errors))
                    )
                selection = submission.invocation.get("selection")
                references = tuple(selection) if isinstance(selection, (tuple, list)) else ()
                if len(references) != 1 or not isinstance(references[0], str):
                    raise ConfigurationError("Select exactly one message to restore.")
                self._preview_message_move("inbox", restoring=True, reference=references[0])
            else:
                executor.execute(submission.invocation, runtime, confirmed=submission.confirmed)
            self.context.state.record_command_usage(
                submission.invocation.command_id, context=self._active_palette_area()
            )
            history = tuple(
                dict.fromkeys((submission.invocation.command_id, *self._recent_palette_commands()))
            )[:20]
            self._settings_service().set("palette.command_history", list(history))
        except (CommandUnavailableError, CommandConfirmationRequired) as exc:
            # These carry registry-authored availability/validation reasons (for example
            # "An Exchange connection is required."); show them instead of a generic
            # "details omitted" message so the user knows what to do next.
            self._set_status(
                "Command unavailable: " + sanitize_terminal(str(exc), max_length=300)
            )
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Command unavailable"))

    @work(thread=True, exclusive=True, group="palette-sequence")
    def _preview_palette_sequence(self, submission: PaletteSequenceSubmission) -> None:
        """Run only the read-only first step and retain a bounded exact result handoff."""

        sequence = submission.sequence
        invocations = tuple(
            step.invocation for step in sequence.steps if step.invocation is not None
        )
        if len(invocations) != 2:
            self.call_from_thread(self._set_status, "Only two-step command sequences are allowed.")
            return
        first, second = invocations
        allowed_pairs = {
            ("search.mail", "mail.mark_read"),
            ("search.mail", "mail.mark_unread"),
            ("search.mail", "backup.back_up_search_results"),
            ("attachments.search", "attachments.download_all_attachments"),
        }
        if (first.command_id, second.command_id) not in allowed_pairs:
            self.call_from_thread(
                self._set_status,
                "That result handoff is not in the bounded command-sequence allowlist.",
            )
            return
        try:
            query = self._palette_sequence_query(first)
            page = MessageService(self._ensure_gateway(), state=self.context.state).search_messages(
                query,
                limit=min(max(self.context.config.page_size, 25), 100),
            )
            items: list[PaletteSequenceItem] = []
            truncated = page.has_more
            if second.command_id == "attachments.download_all_attachments":
                attachment_type = self._palette_scalar(first.get("attachment_type"))
                suffix = f".{attachment_type.casefold()}" if attachment_type else ""
                service = AttachmentService(
                    self._ensure_gateway(),
                    max_download_bytes=self.context.config.max_attachment_bytes,
                )
                for message in page.items[:50]:
                    remaining = 50 - len(items)
                    if remaining <= 0:
                        truncated = True
                        break
                    attachments = service.list_attachments(
                        message.reference,
                        limit=min(remaining, 100),
                    )
                    for attachment in attachments.items:
                        if suffix and not attachment.name.casefold().endswith(suffix):
                            continue
                        items.append(
                            PaletteSequenceItem(
                                "attachment",
                                attachment.reference,
                                message.reference,
                                f"{attachment.name} · {message.subject}",
                                attachment.size,
                                attachment,
                            )
                        )
                        if len(items) >= 50:
                            truncated = truncated or attachments.has_more
                            break
                if len(page.items) > 50:
                    truncated = True
            else:
                items.extend(
                    PaletteSequenceItem(
                        "message",
                        message.reference,
                        message.reference,
                        message.subject,
                        message.size,
                    )
                    for message in page.items
                )
            oversized = tuple(
                item
                for item in items
                if item.attachment is not None
                and item.size is not None
                and item.size > self.context.config.max_attachment_bytes
            )
            preview = PaletteSequencePreview(
                sequence,
                tuple(items),
                page.execution_summary,
                truncated=truncated,
                blocked_reason=(
                    f"Bulk download is blocked because {len(oversized)} attachment(s) exceed "
                    "the configured per-file download limit. Review them individually."
                    if oversized
                    else None
                ),
            )
            self.call_from_thread(self._show_palette_sequence_preview, preview)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Command-sequence preview unavailable"),
            )

    def _show_palette_sequence_preview(self, preview: PaletteSequencePreview) -> None:
        destination = (
            self._backup_repository_path()
            if preview.target_command_id == "backup.back_up_search_results"
            else self.context.config.downloads
            if preview.target_command_id == "attachments.download_all_attachments"
            else None
        )

        def reviewed(decision: PaletteSequenceDecision | None) -> None:
            if decision is not None:
                self._continue_palette_sequence(decision)

        self.push_screen(
            PaletteSequencePreviewScreen(preview, destination=destination),
            reviewed,
        )

    def _continue_palette_sequence(self, decision: PaletteSequenceDecision) -> None:
        preview = decision.preview
        command_id = preview.target_command_id
        references = tuple(item.reference for item in preview.items)
        if command_id in {"mail.mark_read", "mail.mark_unread"}:
            read = command_id == "mail.mark_read"
            plan = MessageService(cast(Any, _DeferredGateway(self._ensure_gateway))).plan_mark_read(
                references,
                read=read,
            )
            detail = (
                f"{plan.summary}\n\nMatched count: {len(preview.items)} bounded message(s)\n"
                f"Estimated size: {human_bytes(preview.known_bytes)} known"
                + (
                    f" + {preview.unknown_size_count} unknown-size"
                    if preview.unknown_size_count
                    else ""
                )
                + "\n\nExact local references:\n"
                + "\n".join(plan.targets)
            )

            def confirmed(value: bool | None) -> None:
                if value:
                    self._execute_message_mutation(
                        plan,
                        success=f"Marked bounded search results {'read' if read else 'unread'}",
                    )

            self.push_screen(
                ConfirmationScreen("Confirm bounded read-state change", detail), confirmed
            )
            return
        if command_id == "backup.back_up_search_results":
            if decision.destination is None:
                self._set_status("Review a backup destination before continuing.")
                return
            self._open_backup_area(
                selected_messages=tuple(item.owner_reference for item in preview.items),
                default_kind="selected-messages",
                default_destination=str(decision.destination),
            )
            return
        if command_id == "attachments.download_all_attachments":
            if decision.destination is None:
                self._set_status("Review an attachment destination before continuing.")
                return
            destination = decision.destination
            detail = (
                f"Download {len(preview.items)} exact attachment(s) to:\n"
                f"{destination}\n\nTotal known size: {human_bytes(preview.known_bytes)}\n"
                "Collision policy: safely rename; existing files are never overwritten.\n"
                "Files are saved only and never opened."
            )

            def confirmed(value: bool | None) -> None:
                if value:
                    self._execute_palette_sequence_download(preview, destination)

            self.push_screen(
                ConfirmationScreen("Confirm bounded attachment download", detail), confirmed
            )
            return
        self._set_status("That command-sequence follow-up is not implemented.")

    @work(thread=True, exclusive=True, group="palette-sequence-download")
    def _execute_palette_sequence_download(
        self,
        preview: PaletteSequencePreview,
        destination: Path,
    ) -> None:
        saved_count = 0
        saved_bytes = 0
        try:
            if preview.unknown_size_count or not preview.items:
                raise ConfigurationError("The preview is no longer safe for a bulk download.")
            if preview.known_bytes > PaletteSequencePreviewScreen.MAX_BULK_ATTACHMENT_BYTES:
                raise ConfigurationError("The preview exceeds the command-sequence byte limit.")
            service = AttachmentService(
                self._ensure_gateway(),
                max_download_bytes=self.context.config.max_attachment_bytes,
            )
            for item in preview.items:
                if item.attachment is None:
                    raise ConfigurationError("The attachment preview is incomplete.")
                plan = service.plan_save(
                    item.owner_reference,
                    item.reference,
                    destination,
                    collision=CollisionPolicy.RENAME,
                    attachment=item.attachment,
                )
                result = service.execute_save(plan, calculate_sha256=True)
                saved_count += 1
                saved_bytes += result.bytes_written
            self.call_from_thread(
                self._set_status,
                f"Downloaded {saved_count} bounded attachment(s) ({human_bytes(saved_bytes)})",
            )
        except Exception as exc:
            detail = _safe_failure(exc, action="Attachment sequence unavailable")
            if saved_count:
                detail = (
                    f"Downloaded {saved_count} attachment(s) before the failure; completed files "
                    f"remain. {detail}"
                )
            self.call_from_thread(self._set_status, detail)

    def action_palette(self) -> None:
        handlers = self._palette_handlers()
        source_screen = self.screen

        def complete(
            submission: PaletteSubmission | PaletteSequenceSubmission | None,
        ) -> None:
            if isinstance(submission, PaletteSequenceSubmission):
                self._preview_palette_sequence(submission)
            elif submission:
                self._execute_palette_submission(submission)

        runtime = self._palette_context(tuple(handlers))
        settings = SettingsService(
            self.context.config_store,
            context=SettingsContext(
                account_id=self.context.config.email,
                machine_id=f"{sys.platform}:{platform.node()}",
            ),
        )

        def preference(setting_id: str, fallback: object) -> object:
            try:
                return settings.value(setting_id).effective_value
            except (ConfigurationError, KeyError, ValueError):
                return fallback

        people: list[str] = []
        for message in self._messages[:100]:
            if not isinstance(message, MessageSummary) or message.sender is None:
                continue
            if message.sender.name:
                people.append(message.sender.name)
            if message.sender.email:
                people.append(message.sender.email)
        folders = (
            "inbox",
            "sent",
            "drafts",
            "trash",
            "junk",
            "archive",
            *tuple(self._folder_paths.values()),
        )
        fuzzy_value = preference("palette.fuzzy_threshold", 68)
        fuzzy_threshold = float(fuzzy_value) if isinstance(fuzzy_value, (int, float)) else 68.0
        count_value = preference("palette.suggestion_count", 18)
        suggestion_count = int(count_value) if isinstance(count_value, (int, float)) else 18
        usage = dict(self._palette_usage())
        self.push_screen(
            PaletteScreen(
                self.context.config.timezone,
                command_context=runtime,
                context_supplier=lambda: self._palette_context(
                    tuple(handlers), source_screen=source_screen
                ),
                folder_candidates=tuple(dict.fromkeys(folders)),
                person_candidates=tuple(dict.fromkeys(people)),
                recent_command_ids=self._recent_palette_commands(),
                supplemental_suggestions=self._palette_supplemental_suggestions(runtime),
                usage_counts=usage,
                fuzzy_threshold=fuzzy_threshold,
                natural_language=bool(preference("palette.natural_language", True)),
                suggestion_count=suggestion_count,
                show_descriptions=bool(preference("palette.show_descriptions", True)),
            ),
            complete,
        )

    def action_search(self) -> None:
        scope = str(self._setting_value("search.default_scope", "mailbox"))
        if scope in {"local_index", "backups", "all"}:
            self._open_local_search(
                mode={
                    "local_index": LocalSearchMode.INDEX,
                    "backups": LocalSearchMode.BACKUPS,
                    "all": LocalSearchMode.ALL,
                }[scope]
            )
            return
        self.query_one("#message-filter", Input).focus()

    @staticmethod
    def _parse_addresses(value: object) -> list[Address]:
        raw = str(value or "").replace(";", ",")
        return [Address(part.strip()) for part in raw.split(",") if part.strip()]

    def _open_compose(
        self,
        draft: DraftData | None = None,
        *,
        kind: ComposeKind = ComposeKind.NEW,
        inherited_attachments: tuple[AttachmentInfo, ...] | None = None,
        new: bool = False,
        review_send: bool = False,
    ) -> None:
        if draft is None or new or draft.reference.startswith("preview:"):
            preferences = ComposerPreferences.from_settings(self._settings_service())
            draft = preferences.seed_new(draft or DraftData(reference=f"dft-{uuid4().hex}"))

        def finished(payload: dict[str, Any] | None) -> None:
            if payload:
                self._set_status(
                    "Sending… waiting for Exchange. Do not resend while the outcome is pending."
                    if payload.get("action") == "send"
                    else "Saving draft…"
                )
                self.submit_composer(payload)

        composer = ComposeScreen(draft, kind=kind, inherited_attachments=inherited_attachments)
        if review_send:
            composer.request_initial_send_review()

        def submitted(payload: dict[str, Any]) -> None:
            self._set_status(
                "Waiting for the send result…"
                if payload.get("action") == "send"
                else "Saving draft…"
            )
            self.submit_composer(payload, composer=composer)

        composer.submit_handler = submitted
        self.push_screen(composer, finished)

    @work(thread=True, exclusive=True, group="compose-save-preflight")
    def prepare_draft_save(self, composer: ComposeScreen, payload: dict[str, Any]) -> None:
        try:
            validation = ComposeService(
                max_attachment_bytes=self.context.config.max_attachment_bytes,
                hash_attachments=False,
            ).validate(
                self._draft_from_payload(payload), allow_empty_subject=True, allow_empty_body=True
            )
            errors = [issue.message for issue in validation.errors if issue.field == "attachments"]
            message = sanitize_terminal("; ".join(errors))
        except Exception:
            message = (
                "Could not check attachments. Check their paths and permissions, then try again."
            )
        self.call_from_thread(self._draft_save_checked, composer, payload, message)

    def _draft_save_checked(
        self, composer: ComposeScreen, payload: dict[str, Any], error: str
    ) -> None:
        if not composer.is_mounted:
            return
        composer.set_save_preflight_pending(False)
        if composer._guard_running_change():
            return
        current = {
            key: value
            for key, value in composer._payload(action="save").items()
            if key != "recovery_sequence"
        }
        if current != {
            key: value
            for key, value in payload.items()
            if key not in {"save_mode", "recovery_sequence"}
        }:
            error = "The draft changed during validation. Review your edits and save again."
        if error:
            composer._set_note(error)
            composer.query_one("#compose-note").scroll_visible()
        else:
            composer._submit_value(payload)

    @work(thread=True, exclusive=True, group="forward-attachment-metadata")
    def load_forward_attachments(self, composer: ComposeScreen) -> None:
        try:
            detail = self._ensure_gateway().show_message(
                composer.draft.forward_of_reference or "", mark_read=False
            )
            if composer.is_mounted:
                self.call_from_thread(composer.show_forward_attachments, tuple(detail.attachments))
        except Exception:
            if composer.is_mounted:
                self.call_from_thread(
                    composer._set_note,
                    "Could not check inherited attachments. Your draft is retained; "
                    "reopen it online before sending.",
                )

    @staticmethod
    def _duplicate_draft(draft: DraftData) -> DraftData:
        return replace(
            draft,
            reference=f"dft-{uuid4().hex}",
            to=list(draft.to),
            cc=list(draft.cc),
            bcc=list(draft.bcc),
            attachment_paths=list(draft.attachment_paths),
            location=DraftLocation.LOCAL,
            revision=1,
            server_id=None,
            server_change_key=None,
        )

    def action_compose(self) -> None:
        self._open_compose()

    @staticmethod
    def _draft_from_payload(payload: dict[str, Any]) -> DraftData:
        save_sent_copy = payload.get("save_sent_copy", True)
        if type(save_sent_copy) is not bool:
            raise ConfigurationError("Choose whether to save a Sent Items copy.")
        raw_importance = str(payload.get("importance") or "normal").casefold()
        importance = (
            Importance(raw_importance)
            if raw_importance in {item.value for item in Importance}
            else Importance.NORMAL
        )
        return DraftData(
            reference=str(payload.get("reference") or f"dft-{uuid4().hex}"),
            to=GUCmailApp._parse_addresses(payload.get("to")),
            cc=GUCmailApp._parse_addresses(payload.get("cc")),
            bcc=GUCmailApp._parse_addresses(payload.get("bcc")),
            subject=str(payload.get("subject") or ""),
            body_text=str(payload.get("body") or ""),
            body_html=(str(payload["body_html"]) if payload.get("body_html") is not None else None),
            attachment_paths=[
                Path(value).expanduser()
                for value in _compose_attachment_paths(str(payload.get("attachment_paths") or ""))
            ],
            importance=importance,
            request_read_receipt=bool(payload.get("request_read_receipt")),
            request_delivery_receipt=bool(payload.get("request_delivery_receipt")),
            save_sent_copy=save_sent_copy,
            reply_to_reference=(
                str(payload["reply_to_reference"]) if payload.get("reply_to_reference") else None
            ),
            forward_of_reference=(
                str(payload["forward_of_reference"])
                if payload.get("forward_of_reference")
                else None
            ),
            revision=int(payload.get("revision", 1)),
            location=DraftLocation(str(payload.get("location") or DraftLocation.LOCAL.value)),
            server_id=str(payload["server_id"]) if payload.get("server_id") else None,
            server_change_key=(
                str(payload["server_change_key"]) if payload.get("server_change_key") else None
            ),
        )

    def open_composer_external_editor(
        self,
        composer: ComposeScreen,
        payload: dict[str, Any],
    ) -> None:
        command = self.context.config.external_editor
        if not command:
            composer._set_note(
                "No external editor is configured. Set Composer & Drafts → External editor."
            )
            return
        composer._set_note("Saving recovery state and opening the configured editor…")
        self._edit_composer_externally(composer, payload, command)

    @work(thread=True, exclusive=True, group="compose-external-editor")
    def _edit_composer_externally(
        self,
        composer: ComposeScreen,
        payload: dict[str, Any],
        command: str,
    ) -> None:
        try:
            service = DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            )
            candidate = self._draft_from_payload(payload)
            # The external editor is deliberately plain-text. Preserve the HTML source in
            # recovery, then make the returned editor text the active canonical body.
            candidate.body_html = None
            self._assert_composer_change_allowed(candidate.reference)
            try:
                existing = service.get(candidate.reference)
            except ItemNotFoundError:
                stored = service.create(candidate)
            else:
                if "revision" not in payload:
                    candidate.revision = existing.revision
                stored = service.save(candidate, expected_revision=candidate.revision)
            result = ExternalDraftEditor(service, command).edit(stored.reference)
            self.call_from_thread(self._external_editor_finished, composer, result.draft)
        except Exception as exc:
            self.call_from_thread(
                composer._set_note,
                _safe_failure(exc, action="External editor unavailable"),
            )

    @staticmethod
    def _external_editor_finished(composer: ComposeScreen, draft: DraftData) -> None:
        if composer.is_mounted:
            composer.apply_external_draft(draft)

    def attach_selected_message_to_composer(
        self, composer: ComposeScreen, *, reference: str | None = None
    ) -> None:
        reference = reference or self.ui.selected_reference
        if composer._guard_running_change():
            return
        if not reference:
            composer._set_note("Select a message before forwarding it as an attachment.")
            return
        composer._set_note("Retrieving a bounded MIME copy for attachment…")
        self._attach_message_mime(composer, reference)

    def schedule_composer_draft(
        self,
        composer: ComposeScreen,
        payload: dict[str, Any],
        *,
        expression: str,
    ) -> None:
        composer._set_note("Saving a recoverable local draft before scheduling…")
        self._prepare_composer_schedule(composer, payload, expression)

    @work(thread=True, exclusive=False, group="compose-schedule")
    @coordinated_write()
    def _prepare_composer_schedule(
        self,
        composer: ComposeScreen,
        payload: dict[str, Any],
        expression: str,
    ) -> None:
        try:
            service = DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            )
            candidate = self._draft_from_payload(payload)
            self._assert_composer_change_allowed(candidate.reference)
            try:
                existing = service.get(candidate.reference)
            except ItemNotFoundError:
                stored = service.create(candidate)
            else:
                if "revision" not in payload:
                    candidate.revision = existing.revision
                stored = service.save(candidate, expected_revision=candidate.revision)
            self.call_from_thread(
                self._open_composer_schedule_review,
                composer,
                stored.reference,
                expression,
            )
        except Exception as exc:
            self.call_from_thread(
                composer._set_note,
                _safe_failure(exc, action="Draft scheduling unavailable"),
            )

    def _open_composer_schedule_review(
        self,
        composer: ComposeScreen,
        reference: str,
        expression: str,
    ) -> None:
        if not composer.is_mounted:
            return

        def reviewed(value: str | None) -> None:
            if not value:
                composer._set_note("Scheduling cancelled; the recoverable local draft remains.")
                return
            if composer.is_mounted:
                composer.dismiss(None)
            self._schedule_workspace_draft(reference, value)

        self.push_screen(
            DraftScheduleScreen(reference, expression=expression),
            reviewed,
        )

    @work(thread=True, exclusive=True, group="compose-message-attachment")
    def _attach_message_mime(self, composer: ComposeScreen, reference: str) -> None:
        try:
            raw = self._ensure_gateway().raw_message(reference)
            directory = self.context.paths.recovery_dir / "forward-attachments"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"forwarded-{uuid4().hex}.eml"
            with target.open("xb") as handle:
                handle.write(raw)
            self.call_from_thread(composer.add_attachment_path, target)
        except Exception as exc:
            self.call_from_thread(
                composer._set_note,
                _safe_failure(exc, action="Forward-as-attachment unavailable"),
            )

    def _assert_composer_change_allowed(self, reference: str) -> None:
        from gucmail.services.send_attempts import SendAttemptStore

        attempt = SendAttemptStore(self.context.state).composition_status(reference)
        if attempt is not None and attempt.state != "failed":
            raise ConflictError(
                "This draft has a send or Scheduled Mail record. Its recovery was preserved. "
                "Review Send status before changing it."
            )

    def _write_composer_recovery(self, payload: dict[str, Any]) -> None:
        with self._composer_recovery_lock:
            reference = str(payload.get("reference"))
            sequence = payload.get("recovery_sequence")
            if reference in self._finished_compositions:
                return
            if isinstance(sequence, int) and sequence < self._composer_recovery_sequences.get(
                reference, -1
            ):
                return
            self._assert_composer_change_allowed(reference)
            DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            ).write_recovery(self._draft_from_payload(payload))
            if isinstance(sequence, int):
                self._composer_recovery_sequences[reference] = sequence

    def recover_composer_before_close(self, payload: dict[str, Any]) -> bool:
        """Durably recover edited content before claiming that it is safe to close."""

        try:
            self._write_composer_recovery(payload)
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Draft recovery unavailable"))
            return False
        return True

    @work(thread=True, exclusive=True, group="compose-autosave")
    def autosave_composer(self, payload: dict[str, Any]) -> None:
        try:
            self._write_composer_recovery(payload)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Draft recovery unavailable"),
            )

    def _preview_discard_composer(self, composer: ComposeScreen) -> None:
        if composer._guard_running_change():
            return
        draft = self._draft_from_payload(composer._payload(action="discard"))
        detail = (
            f"Draft reference: {draft.reference}\n"
            "Local draft and recovery state will be removed. If this draft has a synchronized "
            "Exchange copy, that copy will be moved to Deleted Items first."
        )

        def confirmed(value: bool | None) -> None:
            if value:
                self._discard_composer(composer, draft)

        self.push_screen(ConfirmationScreen("Discard this draft?", detail), confirmed)

    @work(thread=True, exclusive=False, group="compose-discard")
    @coordinated_write()
    def _discard_composer(self, composer: ComposeScreen, draft: DraftData) -> None:
        try:
            self._assert_composer_change_allowed(draft.reference)
            if draft.server_id and self.offline:
                raise ConfigurationError(
                    "Reconnect before discarding a synchronized draft. Its local copy is retained."
                )
            if draft.server_id and not self.offline:
                self._ensure_gateway().delete_message(draft.server_id, permanent=False)
            service = DraftService(
                self.context.state,
                self.context.paths.recovery_dir,
                maximum_attachment_bytes=self.context.config.max_attachment_bytes,
            )
            service.delete(draft.reference)
            service.remove_recovery(draft.reference)
            self.call_from_thread(composer.dismiss, None)
            self.call_from_thread(self._set_status, f"Discarded draft {draft.reference}")
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Draft discard unavailable"),
            )

    @work(thread=True, exclusive=False, group="compose-submit")
    def submit_composer(
        self, payload: dict[str, Any], *, composer: ComposeScreen | None = None
    ) -> None:
        try:
            # A rejected overlapping submission must remain recoverable even
            # when its composer has already dismissed after confirmation.
            self._write_composer_recovery(payload)
        except Exception as exc:
            self.call_from_thread(self._set_status, _safe_failure(exc, action="Draft recovery"))
            if composer is not None:
                self.call_from_thread(
                    composer.submission_finished,
                    "not_started",
                    "Recovery could not be updated, so submission did not start. "
                    "Your input remains here. Review Send status or local storage before retrying.",
                )
            return
        self._submit_composer(payload, composer=composer)
        if composer is not None and composer._submission_pending:
            self._composer_outcome(
                composer,
                "not_started",
                "Another change is still pending. Your input and recovery copy are retained. "
                "Wait for it to finish before another submission.",
            )

    def _composer_outcome(
        self, composer: ComposeScreen | None, outcome: SubmissionOutcome, message: str
    ) -> None:
        self.call_from_thread(self._set_status, message)
        if outcome == "uncertain" and composer is not None:
            with self._composer_recovery_lock:
                self._finished_compositions.add(composer.draft.reference)
        if composer is not None:
            self.call_from_thread(composer.submission_finished, outcome, message)

    @coordinated_write(blocked_hint="The composer recovery copy is retained.")
    def _submit_composer(
        self, payload: dict[str, Any], *, composer: ComposeScreen | None = None
    ) -> None:
        queue_started = False
        try:
            draft = self._draft_from_payload(payload)
            if payload.get("action") == "save":
                self._assert_composer_change_allowed(draft.reference)
                save_mode = str(
                    payload.get("save_mode")
                    or (
                        "local_then_server"
                        if bool(self._setting_value("composer.server_draft_sync", True))
                        else "local_only"
                    )
                )
                draft_service = DraftService(
                    self.context.state,
                    self.context.paths.recovery_dir,
                    maximum_attachment_bytes=self.context.config.max_attachment_bytes,
                )
                if draft_service.exists(draft.reference):
                    if "revision" not in payload:
                        draft.revision = draft_service.get(draft.reference).revision
                    stored = draft_service.save(draft)
                else:
                    stored = draft_service.create(draft)
                draft_service.remove_recovery(draft.reference)
                if save_mode == "local_only":
                    self._composer_outcome(
                        composer,
                        "saved",
                        f"Draft saved locally: {stored.reference} · server synchronization skipped",
                    )
                    return
                if self.offline:
                    self._composer_outcome(
                        composer,
                        "saved",
                        f"Draft saved locally: {stored.reference} · offline, server save deferred",
                    )
                    return
                try:
                    synchronized = DraftSyncCoordinator(
                        draft_service, self._ensure_gateway()
                    ).sync_to_server(stored.reference)
                except Exception as exc:
                    self._composer_outcome(
                        composer,
                        "saved",
                        f"Draft saved locally: {stored.reference} · "
                        f"{_safe_failure(exc, action='Exchange draft sync unavailable')}",
                    )
                    return
                self._composer_outcome(
                    composer,
                    "saved",
                    f"Draft saved locally and synchronized to Exchange: {synchronized.reference}",
                )
                return
            kind = ComposeKind(str(payload.get("kind") or ComposeKind.NEW.value))
            compose_service = ComposeService(
                max_attachment_bytes=self.context.config.max_attachment_bytes,
                state=self.context.state,
                preferences=ComposerPreferences.from_settings(self._settings_service()),
            )
            plan = compose_service.plan_send(
                draft, kind=kind, allow_empty_subject=payload.get("allow_empty_subject") is True
            )
            if payload.get("confirmation_scope") != plan.confirmation_scope:
                self._composer_outcome(
                    composer,
                    "not_started",
                    "The reviewed message or attachment files changed. Review send again. "
                    "Nothing was queued or sent.",
                )
                return
            delay = int(str(self._setting_value("composer.undo_send_seconds", 0)))
            if delay:
                service = DraftService(
                    self.context.state,
                    self.context.paths.recovery_dir,
                    maximum_attachment_bytes=self.context.config.max_attachment_bytes,
                )
                stored = (
                    service.save(plan.draft)
                    if service.exists(draft.reference)
                    else service.create(plan.draft)
                )
                queue_started = True
                job = self._scheduler_service().queue_undo_send(
                    stored.reference,
                    delay,
                    allow_empty_subject=payload.get("allow_empty_subject") is True,
                )
                with self._composer_recovery_lock:
                    self._finished_compositions.add(draft.reference)
                self.call_from_thread(self._queued_undo_send, job.reference, delay)
                self._composer_outcome(
                    composer,
                    "queued",
                    f"Queued with a {delay}-second Undo Send delay. Cancel in Scheduled Mail. "
                    "Closing the app leaves the job queued for a running scheduler.",
                )
                return
            compose_service.gateway = self._ensure_gateway()
            compose_service.execute_send(
                plan, confirmation_scope=str(payload["confirmation_scope"])
            )
            with self._composer_recovery_lock:
                self._finished_compositions.add(draft.reference)
                DraftService(
                    self.context.state,
                    self.context.paths.recovery_dir,
                    maximum_attachment_bytes=self.context.config.max_attachment_bytes,
                ).finish_sent(draft.reference, expected_revision=draft.revision)
            remembered = self._remember_contacted_people(plan.draft)
            self._composer_outcome(
                composer,
                "sent",
                "Send accepted by the Exchange request path"
                + (
                    "; local recipient history could not be saved. Do not resend."
                    if not remembered
                    else ""
                ),
            )
        except SendOutcomeUncertainError:
            self._composer_outcome(
                composer,
                "uncertain",
                "The send may have completed. It was not retried. "
                + (
                    "Check Sent Items before sending again; "
                    if payload.get("save_sent_copy", True) is True
                    else "No Sent Items copy was requested; establish delivery manually "
                    "before sending again. "
                )
                + "The local recovery copy remains available.",
            )
        except Exception:
            if queue_started:
                self._composer_outcome(
                    composer,
                    "uncertain",
                    "The local queue result could not be confirmed. Your composition is retained. "
                    "Do not repeat the submission; review Scheduled Mail before another send.",
                )
                return
            if payload.get("action") == "save":
                self._composer_outcome(
                    composer,
                    "failed",
                    "The draft save could not be confirmed. Your input is retained. "
                    "Refresh Drafts to check the saved revision before trying again.",
                )
                return
            from gucmail.services.send_attempts import SendAttemptStore

            try:
                attempt = SendAttemptStore(self.context.state).get(str(payload.get("reference")))
            except Exception:
                self._composer_outcome(
                    composer,
                    "uncertain",
                    "The submission outcome could not be checked. Your recovery copy is retained. "
                    "Do not repeat it; review Send status and Scheduled Mail first.",
                )
                return
            if attempt and attempt.state == "sent":
                self._composer_outcome(
                    composer,
                    "sent",
                    "Send was accepted, but local cleanup could not be completed. Do not resend. "
                    "Review the retained draft/recovery after checking Sent Items.",
                )
            elif attempt and attempt.state in {"sending", "uncertain"}:
                self._composer_outcome(composer, "uncertain", attempt_message(attempt.state))
            elif attempt and attempt.state == "failed":
                self._composer_outcome(
                    composer, "failed", attempt_message("failed") + " Your input is retained here."
                )
            else:
                self._composer_outcome(
                    composer,
                    "not_started",
                    "Submission did not reach a confirmed send. "
                    "Your input and recovery are retained. "
                    "Check the connection, message and files. "
                    "Review Scheduled Mail before queuing again.",
                )

    def _queued_undo_send(self, reference: str, delay: int) -> None:
        self._set_status(
            f"Queued with a {delay}-second Undo Send delay. Cancel in Scheduled Mail. "
            "Closing the app leaves the job queued for a running scheduler."
        )
        self._open_area("scheduled")
        self.set_timer(delay, lambda: self._finish_undo_delay(reference))

    @work(thread=True, exclusive=False, group="undo-send-countdown")
    @coordinated_write(blocked_hint="The delayed job remains queued. Review it in Scheduled Mail.")
    def _finish_undo_delay(self, reference: str) -> None:
        if self.offline:
            return
        try:
            scheduler = self._scheduler_service()
            result = scheduler.run_reference_if_due(reference)
            if result is not None:
                self.call_from_thread(self._set_status, f"Delayed send: {result.state.value}")
            else:
                # Do not leave the "Queued with ... Undo Send delay" message standing
                # after the job was cancelled, rescheduled or paused elsewhere.
                state = str(getattr(scheduler.get(reference).message.state, "value", "")).casefold()
                if state == "cancelled":
                    message = "Undo Send: the delayed message was cancelled. Nothing was sent."
                else:
                    message = (
                        f"Delayed send was not run now (state: {state or 'unknown'}). "
                        "Review it in Scheduled Mail."
                    )
                self.call_from_thread(self._set_status, message)
        except Exception as exc:
            self.call_from_thread(self._set_status, _safe_failure(exc, action="Delayed send"))

    def _remember_contacted_people(self, draft: DraftData) -> bool:
        """Persist only explicit successful-send recipients, most recent first."""

        from gucmail.services.recipient_assistance import remember_sent_recipients

        try:
            remember_sent_recipients(
                self._settings_service(), draft, own_address=self.context.config.email
            )
            return True
        except Exception:
            # An ancillary local write must not obscure the accepted send or cause a retry.
            return False

    @work(thread=True, exclusive=True, group="compose-prepare")
    def _prepare_reply(self, reference: str, *, reply_all: bool | None) -> None:
        try:
            service = ComposeService(
                self._ensure_gateway(),
                max_attachment_bytes=self.context.config.max_attachment_bytes,
                response_preferences=MailResponsePreferences.from_settings(
                    self._settings_service()
                ),
            )
            own = (self.context.config.email,) if self.context.config.email else ()
            preview = service.preview_reply(
                reference,
                reply_all=reply_all,
                account_addresses=own,
            )
            self.call_from_thread(
                self._open_compose,
                preview.draft,
                kind=preview.kind,
            )
        except Exception as exc:
            self.call_from_thread(self._set_status, _safe_failure(exc))

    @work(thread=True, exclusive=True, group="compose-prepare")
    def _prepare_forward(self, reference: str) -> None:
        try:
            service = ComposeService(
                self._ensure_gateway(),
                max_attachment_bytes=self.context.config.max_attachment_bytes,
                response_preferences=MailResponsePreferences.from_settings(
                    self._settings_service()
                ),
            )
            preview = service.preview_forward(reference)
            self.call_from_thread(
                self._open_compose,
                preview.draft,
                kind=preview.kind,
                inherited_attachments=tuple(preview.source.attachments),
            )
        except Exception as exc:
            self.call_from_thread(self._set_status, _safe_failure(exc))

    def _needs_message(self, action: str) -> bool:
        if self.ui.selected_reference:
            return True
        self._set_status(f"Select a message before choosing {action}.")
        return False

    def action_reply(self) -> None:
        if self._needs_message("Reply"):
            self._prepare_reply(self.ui.selected_reference or "", reply_all=None)

    def action_reply_all(self) -> None:
        if self._needs_message("Reply all"):
            self._prepare_reply(self.ui.selected_reference or "", reply_all=True)

    def action_forward(self) -> None:
        if self._needs_message("Forward"):
            self._prepare_forward(self.ui.selected_reference or "")

    def _message_mutation_service(self) -> MessageService:
        return MessageService(cast(Any, _DeferredGateway(self._ensure_gateway)))

    def _prompt_message_importance(self, invocation: CommandInvocation) -> None:
        if not self._needs_message("Set importance"):
            return
        value = self._palette_scalar(invocation.get("importance"))
        if value:
            self._plan_message_importance(value)
            return

        def chosen(importance: str | None) -> None:
            if importance:
                self._plan_message_importance(importance)

        self.push_screen(
            FolderNameScreen(
                "Set message importance",
                "Enter low, normal, or high. The selected message is updated only after "
                "validation.",
                submit_label="Apply",
                placeholder="low, normal, or high",
            ),
            chosen,
        )

    def _plan_message_importance(self, importance: str) -> None:
        reference = self.ui.selected_reference
        if not reference:
            return
        try:
            plan = self._message_mutation_service().plan_importance(
                [reference], importance=importance
            )
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Importance update unavailable"))
            return
        self._execute_message_mutation(plan, success="Message importance updated")

    def _prompt_message_category(self, invocation: CommandInvocation, *, mode: str) -> None:
        label = "Add category" if mode == "add" else "Remove category"
        if not self._needs_message(label):
            return
        value = self._palette_scalar(invocation.get("category"))
        if value:
            self._plan_message_category(value, mode=mode)
            return

        def chosen(category: str | None) -> None:
            if category:
                self._plan_message_category(category, mode=mode)

        self.push_screen(
            FolderNameScreen(
                label,
                f"Enter the exact category name to {mode} on the selected message.",
                submit_label=label,
                placeholder="Category name",
            ),
            chosen,
        )

    def _plan_message_category(self, category: str, *, mode: str) -> None:
        reference = self.ui.selected_reference
        if not reference:
            return
        try:
            plan = self._message_mutation_service().plan_categorize(
                [reference], [category], mode=mode
            )
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Category update unavailable"))
            return
        self._execute_message_mutation(plan, success=f"Category {mode} completed")

    def _prompt_message_copy(self, invocation: CommandInvocation) -> None:
        if not self._needs_message("Copy message"):
            return
        destination = self._palette_scalar(invocation.get("folder"))
        if destination:
            self._plan_message_copy(destination)
            return

        def chosen(folder: str | None) -> None:
            if folder:
                self._plan_message_copy(folder)

        self.push_screen(
            FolderNameScreen(
                "Copy message",
                "Enter the destination folder path. The source message remains in place.",
                submit_label="Copy",
                placeholder="Destination folder path",
            ),
            chosen,
        )

    def _plan_message_copy(self, destination: str) -> None:
        reference = self.ui.selected_reference
        if not reference:
            return
        try:
            plan = self._message_mutation_service().plan_copy([reference], destination)
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Message copy unavailable"))
            return
        self._execute_message_mutation(plan, success=f"Message copied to {destination}")

    def _prompt_message_move(self, invocation: CommandInvocation) -> None:
        if not self._needs_message("Move message"):
            return
        destination = self._palette_scalar(invocation.get("folder"))
        if destination:
            self._preview_message_move(destination, restoring=False)
            return

        def chosen(folder: str | None) -> None:
            if folder:
                self._preview_message_move(folder, restoring=False)

        self.push_screen(
            FolderNameScreen(
                "Move message",
                "Enter the destination folder path. The exact move is previewed next.",
                submit_label="Preview move",
                placeholder="Destination folder path",
            ),
            chosen,
        )

    def _preview_message_restore(self) -> None:
        if self._needs_message("Restore message"):
            self._preview_message_move("inbox", restoring=True)

    def _preview_message_move(
        self, destination: str, *, restoring: bool, reference: str | None = None
    ) -> None:
        reference = reference or self.ui.selected_reference
        if not reference:
            return
        try:
            plan = self._message_mutation_service().plan_move([reference], destination)
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Message move unavailable"))
            return
        detail = f"{plan.summary}\n\nExact local reference:\n{reference}"

        def confirmed(value: bool | None) -> None:
            if value:
                self._execute_message_mutation(
                    plan,
                    success=(
                        "Message restored to Inbox"
                        if restoring
                        else f"Message moved to {destination}"
                    ),
                )

        self.push_screen(
            ConfirmationScreen("Restore message?" if restoring else "Move message?", detail),
            confirmed,
        )

    def _preview_permanent_message_delete(self) -> None:
        if not self._needs_message("Permanently delete"):
            return
        reference = self.ui.selected_reference or ""
        plan = self._message_mutation_service().plan_delete([reference], permanent=True)
        detail = (
            f"{plan.summary}\n\nExact local reference:\n{reference}\n\n"
            "This bypasses Deleted Items and cannot be undone."
        )

        def confirmed(value: bool | None) -> None:
            if value:
                self._execute_message_mutation(plan, success="Message permanently deleted")

        self.push_screen(ConfirmationScreen("Permanently delete message?", detail), confirmed)

    @work(thread=True, exclusive=False, group="message-mutation")
    @coordinated_write()
    def _execute_message_mutation(self, plan: MutationPlan, *, success: str) -> None:
        try:
            result = MessageService(
                self._ensure_gateway(), state=self.context.state
            ).execute_mutation(
                plan,
                confirmation_scope=plan.confirmation_scope if plan.requires_confirmation else None,
            )
            self.call_from_thread(
                self._set_status,
                f"{success} ({len(result.results)} message(s))",
            )
            self.call_from_thread(self.refresh_mailbox)
        except MutationExecutionError as exc:
            self.call_from_thread(self._show_message_operation_failure, exc)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Message update unavailable"),
            )

    def _show_message_operation_failure(self, error: MutationExecutionError) -> None:
        self._set_status(error.message)
        detail = "\n".join(
            f"{item['status'].replace('_', ' ').capitalize()}: {item['reference']}"
            + (f" → {item['result_reference']}" if item.get("result_reference") else "")
            for item in error.outcomes
        )
        self.push_screen(
            AreaScreen(
                "Message operation results",
                lambda: error.message + "\n\n" + detail + "\n\n" + (error.context.suggestion or ""),
            )
        )
        if not self.offline:
            self.refresh_mailbox()

    @on(MessageOperationRequested)
    def _inspect_message_operation(self, event: MessageOperationRequested) -> None:
        event.stop()
        operation_id = event.operation_id

        def load() -> str:
            record = self.context.state.get_message_operation(operation_id)
            if record is None:
                raise ConfigurationError("This operation is unavailable in local recovery history.")
            lines = [f"Operation: {record['operation']}", f"ID: {operation_id}", ""]
            for item in record["outcomes"]:
                lines.append(
                    f"{item['status'].replace('_', ' ').capitalize()}: {item['reference']}"
                    + (f" → {item['result_reference']}" if item.get("result_reference") else "")
                )
            lines += [
                "",
                "Completed items have an acknowledgment. Uncertain items need checking.",
                "This view does not repeat any operation or infer server reconciliation.",
            ]
            return sanitize_terminal("\n".join(lines), max_length=60_000)

        self.push_screen(AreaScreen("Message operation results", load))

    def _preview_bulk_mark_read(self, scope: str) -> None:
        if scope == "visible":
            summaries = tuple(
                item
                for item in self._visible_messages
                if isinstance(item, MessageSummary) and not item.is_read
            )
        else:
            summaries = tuple(
                item for item in self._selected_message_summaries() if not item.is_read
            )
        if not summaries:
            self._set_status(
                "No unread visible messages are in this scope."
                if scope == "visible"
                else "Select one or more unread visible messages first."
            )
            return
        plan = MessageService(cast(Any, _DeferredGateway(self._ensure_gateway))).plan_mark_read(
            [item.reference for item in summaries],
            read=True,
        )
        detail = f"{plan.summary}\nScope: {scope} bounded rows\nReferences (exact):\n" + "\n".join(
            plan.targets
        )

        def confirmed(value: bool | None) -> None:
            if value:
                self._execute_bulk_mark_read(plan)

        self.push_screen(ConfirmationScreen("Mark messages read?", detail), confirmed)

    @work(thread=True, exclusive=False, group="mailbox-mutation")
    @coordinated_write()
    def _execute_bulk_mark_read(self, plan: MutationPlan) -> None:
        try:
            result = MessageService(
                self._ensure_gateway(), state=self.context.state
            ).execute_mutation(
                plan,
                confirmation_scope=plan.confirmation_scope,
            )
            self.call_from_thread(
                self._set_status,
                f"Marked {len(result.results)} bounded message(s) read",
            )
            self.call_from_thread(self.refresh_mailbox)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Bulk read-state update unavailable"),
            )

    def _message_export_service(self) -> MessageExportService:
        maximum = max(int(self.context.config.max_attachment_bytes), 1)
        return MessageExportService(
            cast(Any, _DeferredGateway(self._ensure_gateway)),
            maximum_message_bytes=maximum,
            maximum_total_bytes=max(maximum, 1024 * 1024 * 1024),
        )

    def _prompt_selected_message_export(self) -> None:
        summaries = self._selected_message_summaries()
        if not summaries:
            self._set_status("Select one or more visible messages before exporting.")
            return

        def destination_chosen(destination: str | None) -> None:
            if not destination:
                return
            try:
                plan = self._message_export_service().plan(
                    [item.reference for item in summaries],
                    destination,
                    subjects={item.reference: item.subject for item in summaries},
                )
            except Exception as exc:
                self._set_status(_safe_failure(exc, action="Message export preview unavailable"))
                return
            detail = plan.summary + "\nReferences (exact):\n" + "\n".join(plan.targets)

            def confirmed(value: bool | None) -> None:
                if value:
                    self._execute_message_export(plan, open_after=False)

            self.push_screen(ConfirmationScreen("Export selected messages?", detail), confirmed)

        self.push_screen(
            MessageExportPathScreen(
                str(self.context.config.downloads),
                selection_count=len(summaries),
            ),
            destination_chosen,
        )

    def _preview_open_selected_externally(self) -> None:
        selected = self._selected_message_summaries()
        summary = selected[0] if len(selected) == 1 else self._selected_message_summary()
        if summary is None:
            self._set_status("Highlight one message before opening it externally.")
            return
        if len(selected) > 1:
            self._set_status("External open accepts exactly one selected message.")
            return
        destination = self.context.paths.cache_dir / "external-messages" / uuid4().hex
        try:
            plan = self._message_export_service().plan(
                [summary.reference],
                destination,
                subjects={summary.reference: summary.subject},
            )
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="External-open preview unavailable"))
            return
        detail = (
            "Export one raw .eml copy to the private application cache, then ask the "
            "operating system's registered handler to open it. No shell command is used.\n"
            f"Reference: {summary.reference}\nTemporary directory: {destination}"
        )

        def confirmed(value: bool | None) -> None:
            if value:
                self._execute_message_export(plan, open_after=True)

        self.push_screen(ConfirmationScreen("Open message externally?", detail), confirmed)

    @work(thread=True, exclusive=False, group="message-export")
    def _execute_message_export(self, plan: MutationPlan, *, open_after: bool) -> None:
        try:
            result = self._message_export_service().execute(
                plan,
                confirmation_scope=plan.confirmation_scope,
            )
            paths = tuple(path for path in result.results if isinstance(path, Path))
            if open_after:
                if len(paths) != 1:
                    raise ConfigurationError("External open expected exactly one exported file.")
                _open_file_with_registered_handler(paths[0])
                message = f"Opened exported message with the registered handler: {paths[0]}"
            else:
                message = f"Exported {len(paths)} message(s) to {paths[0].parent}"
            self.call_from_thread(self._set_status, message)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(
                    exc,
                    action="External message open unavailable"
                    if open_after
                    else "Message export unavailable",
                ),
            )

    def action_delete(self) -> None:
        if not self._needs_message("Delete"):
            return
        reference = self.ui.selected_reference or ""
        try:
            destination = MailDeletionPreferences.from_settings(
                self._settings_service()
            ).destination_for()
            plan = self._message_mutation_service().plan_delete(
                (reference,), destination=destination
            )
        except ConfigurationError as exc:
            self._set_status(f"Delete unavailable: {sanitize_terminal(str(exc), max_length=500)}")
            return
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Delete unavailable"))
            return

        def confirmed(value: bool | None) -> None:
            if value:
                if destination is None:
                    self._delete_message(reference)
                else:
                    self._execute_message_mutation(
                        plan, success=f"Message moved to {sanitize_terminal(destination)}"
                    )

        self.push_screen(
            ConfirmationScreen(
                "Move one message?",
                f"{plan.summary}\nScope: exactly 1 message\nReference: {reference}\n"
                "This moves the message; permanent deletion requires a separate explicit action.",
                confirm_label="Move message",
            ),
            confirmed,
        )

    @work(thread=True, exclusive=False, group="mailbox-mutation")
    @coordinated_write()
    def _delete_message(self, reference: str) -> None:
        try:
            service = MessageService(self._ensure_gateway(), state=self.context.state)
            plan = service.plan_delete((reference,), permanent=False)
            service.execute_mutation(plan, confirmation_scope=plan.confirmation_scope)
            self.call_from_thread(self._set_status, "Message moved to Deleted Items")
            self.call_from_thread(self.refresh_mailbox)
        except Exception as exc:
            self.call_from_thread(self._set_status, _safe_failure(exc))

    def action_toggle_read(self) -> None:
        if self._needs_message("Mark read/unread"):
            reference = self.ui.selected_reference or ""
            current = next(
                (
                    item
                    for item in self._messages
                    if isinstance(item, MessageSummary) and item.reference == reference
                ),
                None,
            )
            self._toggle_message_read(reference, read=bool(current and not current.is_read))

    @work(thread=True, exclusive=False, group="mailbox-mutation")
    @coordinated_write()
    def _toggle_message_read(self, reference: str, *, read: bool) -> None:
        try:
            service = MessageService(self._ensure_gateway(), state=self.context.state)
            service.execute_mutation(service.plan_mark_read((reference,), read=read))
            self.call_from_thread(self._set_status, "Marked read" if read else "Marked unread")
            self.call_from_thread(self.refresh_mailbox)
        except Exception as exc:
            self.call_from_thread(self._set_status, _safe_failure(exc))

    def action_refresh(self) -> None:
        if not self.offline and self._folder_root_node_id is not None:
            try:
                self._folder_selection_to_restore = self.ui.folder
                self._folder_refresh_expanded_snapshot = set(self._expanded_folders)
                self._folder_refresh_pending_expanded = set(self._expanded_folders)
                self._folder_refresh_in_progress = True
                self._folder_refresh_generation += 1
                generation = self._folder_refresh_generation
                # A saved branch may have been deleted elsewhere. Do not leave
                # programmatic-collapse suppression active forever in that case.
                self.set_timer(
                    10.0,
                    lambda: self._finish_folder_refresh(generation),
                )
                node = self.query_one("#folder-tree", Tree).get_node_by_id(
                    self._folder_root_node_id
                )
                data = dict(node.data) if isinstance(node.data, dict) else {}
                data.update({"loaded": False, "loading": False})
                node.data = data
                self._request_folder_children(parent_reference=None, node_id=node.id)
            except Exception as exc:
                self.log.debug(
                    "Folder refresh selection could not be restored: %s",
                    type(exc).__name__,
                )
        self.refresh_mailbox()

    def action_reconnect(self) -> None:
        self._cancel_automatic_read()
        self._mailbox_generation += 1
        self._message_generation += 1
        self.ui.busy = None
        try:
            if self._gateway_injected:
                reset = getattr(self.gateway, "reset_session", None)
                if callable(reset):
                    reset()
            else:
                self.context.close()
                self.gateway = None
            self._set_status("Connection reset · loading a fresh session…")
            self.action_refresh()
        except Exception as exc:
            self._set_status(_safe_failure(exc, action="Reconnect unavailable"))

    def action_navigate_back(self) -> None:
        self._cancel_automatic_read()
        if self.ui.busy:
            self._mailbox_generation += 1
            self._message_generation += 1
            if self._mailbox_timeout is not None:
                self._mailbox_timeout.stop()
                self._mailbox_timeout = None
            if self._message_timeout is not None:
                self._message_timeout.stop()
                self._message_timeout = None
            self.ui.busy = None
            self._set_status("Loading cancelled; late results will be ignored")
            return
        if len(self._folder_location_history) > 1:
            self._folder_location_history.pop()
            previous = self._folder_location_history[-1]
            self._activate_folder_location(previous, record_history=False)
            self._set_status(f"Returned to {sanitize_terminal(previous, max_length=120)}")
            return
        self.query_one("#message-table", DataTable).focus()


def _is_mail_folder_class(folder_class: str | None) -> bool:
    """True unless Exchange explicitly marks the folder as a non-mail item store."""

    if not folder_class:
        return True
    return folder_class.casefold().startswith("ipf.note") or folder_class.casefold() == "ipf"


def run_tui(context: AppContext | None = None, *, offline: bool = False) -> None:
    GUCmailApp(context=context, offline=offline).run()
