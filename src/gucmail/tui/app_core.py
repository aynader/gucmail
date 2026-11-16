"""State shared by the main window and its mixins: everything set up in __init__."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Literal

from textual import work
from textual.app import App
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import (
    Tree,
)
from textual.worker import Worker

from gucmail.bootstrap import AppContext, build_context
from gucmail.commands import (
    CommandContext,
    CommandInvocation,
)
from gucmail.help import (
    HelpRuntimeContext,
)
from gucmail.models import (
    Address,
    AttachmentInfo,
    DraftData,
    FolderInfo,
    MessageSummary,
    SearchQuery,
)
from gucmail.services._base import MutationPlan
from gucmail.services.compose import (
    ComposeKind,
)
from gucmail.services.local_search import LocalSearchMode
from gucmail.services.mail_preferences import (
    MailSortPreferences,
)
from gucmail.services.message_columns import MessageColumn
from gucmail.services.message_export import MessageExportService
from gucmail.services.messages import MessageService
from gucmail.services.quota import (
    QuotaService,
)
from gucmail.services.scheduler import SchedulerService
from gucmail.services.semantic_search import SemanticSearchService
from gucmail.services.shell_preferences import (
    HEADER_DEFAULT,
    STATUS_DEFAULT,
    parse_shell_fields,
)
from gucmail.services.ui_session import (
    MailSession,
    MailSessionStore,
)
from gucmail.settings import SettingsService
from gucmail.tui.appearance_screen import ThemeBrowserBackend
from gucmail.tui.branding import (
    BrandingPreferences,
    RenderedBranding,
)
from gucmail.tui.branding_screen import BrandingBrowserBackend
from gucmail.tui.palette_routes import (
    PaletteRoute,
    RouteTarget,
)
from gucmail.tui.reader import FloatingReader
from gucmail.tui.shell_widgets import UIState, _AutomaticReadRequest
from gucmail.tui.theme_repository import CustomThemeRepository
from gucmail.tui.themes import (
    ThemeDefinition,
    ThemePreferences,
    ThemeRegistry,
    ThemeSession,
)
from gucmail.tui.write_coordinator import (
    app_write_coordinator,
    coordinated_write,
)

if TYPE_CHECKING:
    from gucmail.tui.compose_screen import ComposeScreen
    from gucmail.tui.help_screen import SettingsHelpStateStore


TextualBorderStyle = Literal["none", "ascii", "solid", "round", "double", "heavy", "dashed"]


class AppCore(App[None]):
    """The main window's state. Behaviour lives in the mixins and in GUCmailApp."""

    # Set up later (by preferences, themes and search), not in __init__.
    _shortcut_notice: str
    _binding_overrides: dict[str, str]
    _palette_primary_shortcut: str
    _palette_secondary_shortcut: str
    _previous_theme_shortcut: str
    _next_theme_shortcut: str
    _runtime_theme: ThemeDefinition
    _message_row_density: str
    _search_heading_label: str | None

    if TYPE_CHECKING:
        # Declared here, defined in the mixins and GUCmailApp, so each part of the
        # main window can call the others with full type checking.

        @work(thread=True, exclusive=True, group="forward-attachment-metadata")
        def load_forward_attachments(self, composer: ComposeScreen) -> None: ...

        def open_composer_external_editor(
            self,
            composer: ComposeScreen,
            payload: dict[str, Any],
        ) -> None: ...

        def schedule_composer_draft(
            self,
            composer: ComposeScreen,
            payload: dict[str, Any],
            *,
            expression: str,
        ) -> None: ...

        def recover_composer_before_close(self, payload: dict[str, Any]) -> bool: ...

        @work(thread=True, exclusive=True, group="compose-autosave")
        def autosave_composer(self, payload: dict[str, Any]) -> None: ...

        def _resolve_config_theme(self, configured: str) -> ThemeDefinition: ...

        def _load_theme_preferences(self, selected_theme_id: str) -> ThemePreferences: ...

        def _persist_theme_preferences(self, preferences: ThemePreferences) -> None: ...

        def _load_message_columns(self) -> tuple[MessageColumn, ...]: ...

        def _setting_value_with_legacy(
            self,
            setting_id: str,
            fallback: object,
            *,
            legacy_key: str,
            use_fallback_when_default: bool = False,
        ) -> object: ...

        def _configured_message_ratio(self) -> float: ...

        def _shortcut_label(self) -> str: ...

        def _load_shortcut_preferences(self) -> None: ...

        def _load_branding_preferences(self) -> BrandingPreferences: ...

        def _persist_branding_preferences(self, preferences: BrandingPreferences) -> None: ...

        def _branding_preferences_changed(self, preferences: BrandingPreferences) -> None: ...

        def _activate_theme_callback(self, definition: ThemeDefinition) -> None: ...

        def _activate_folder_location(
            self, folder: str, *, record_history: bool = True
        ) -> None: ...

        def _activate_theme(self, definition: ThemeDefinition) -> ThemeDefinition: ...

        def _active_palette_area(self, screen: Screen[Any] | None = None) -> str: ...

        def _apply_breakpoint(self, width: int) -> None: ...

        def _apply_committed_settings(self, setting_ids: tuple[str, ...]) -> None: ...

        def _apply_keyboard_bindings(self) -> None: ...

        def _apply_layout(self) -> None: ...

        def _apply_theme_structure(self, definition: ThemeDefinition) -> None: ...

        def _backup_repository_path(self) -> Path: ...

        def _bind_palette_shortcuts(self) -> None: ...

        def _bind_theme_shortcuts(self) -> None: ...

        @staticmethod
        def _bounded_lines(lines: list[str], *, empty: str) -> str: ...

        def _calendar_invocation_date(self, invocation: CommandInvocation) -> date | None: ...

        def _cancel_automatic_read(self) -> None: ...

        def _capture_session(self) -> None: ...

        def _catalog_application_action(
            self, route: PaletteRoute, invocation: CommandInvocation | None = None
        ) -> None: ...

        def _catalog_direct(self, route: PaletteRoute, invocation: CommandInvocation) -> None: ...

        def _catalog_help(
            self,
            route: PaletteRoute,
            invocation: CommandInvocation,
        ) -> None: ...

        def _catalog_open(self, route: PaletteRoute, invocation: CommandInvocation) -> None: ...

        def _catalog_screen_action(
            self,
            route: PaletteRoute,
            invocation: CommandInvocation,
        ) -> None: ...

        def _catalog_search(self, route: PaletteRoute, invocation: CommandInvocation) -> None: ...

        def _catalog_settings(
            self,
            route: PaletteRoute,
            invocation: CommandInvocation,
        ) -> None: ...

        def _compose_from_route(
            self, route: PaletteRoute, invocation: CommandInvocation
        ) -> None: ...

        @staticmethod
        def _contacts_invocation_query(invocation: CommandInvocation) -> str: ...

        @staticmethod
        def _draft_from_payload(payload: dict[str, Any]) -> DraftData: ...

        @staticmethod
        def _duplicate_draft(draft: DraftData) -> DraftData: ...

        def _ensure_gateway(self) -> Any: ...

        @work(thread=True, exclusive=False, group="message-mutation")
        @coordinated_write()
        def _execute_message_mutation(self, plan: MutationPlan, *, success: str) -> None: ...

        @staticmethod
        def _external_editor_finished(composer: ComposeScreen, draft: DraftData) -> None: ...

        def _finish_folder_refresh(self, generation: int | None = None) -> None: ...

        def _fit_message_columns(self, *, repopulate: bool = True) -> None: ...

        def _format_message(self, detail: Any) -> str: ...

        def _help_runtime_context(self, active_view: str) -> HelpRuntimeContext: ...

        def _help_state_store(self) -> SettingsHelpStateStore: ...

        def _initial_mailbox_refresh(self) -> None: ...

        def _show_sign_in(self) -> None: ...

        def _load_next_mailbox_page(self, *, mouse: bool = False) -> None: ...

        def _load_rendered_folder_pages_for_expand_all(self, root: Any) -> None: ...

        def _message_export_service(self) -> MessageExportService: ...

        def _message_mutation_service(self) -> MessageService: ...

        def _monochrome_output(self) -> bool: ...

        def _navigate_visible_mail_folder(self, tree: Tree[Any], *, forward: bool) -> None: ...

        def _needs_message(self, action: str) -> bool: ...

        def _open_activity_filtered(self, category: str, text: str = "") -> None: ...

        def _open_activity_with_control(self, control: str) -> None: ...

        def _open_activity_with_notification_test(self, provider: str) -> None: ...

        def _open_area(self, area: str) -> None: ...

        def _open_backup_area(
            self,
            *,
            selected_messages: Sequence[str] = (),
            default_kind: str | None = None,
            default_folders: Sequence[str] = (),
            default_destination: str | None = None,
        ) -> None: ...

        def _open_compose(
            self,
            draft: DraftData | None = None,
            *,
            kind: ComposeKind = ComposeKind.NEW,
            inherited_attachments: tuple[AttachmentInfo, ...] | None = None,
            new: bool = False,
            review_send: bool = False,
        ) -> None: ...

        def _open_correspondents(self, order: str) -> None: ...

        def _open_diagnostics_area(self, initial_action: str | None = None) -> None: ...

        def _open_diagnostics_view(self, view: str) -> None: ...

        def _open_draft_attachment_workspace(
            self,
            owner_reference: str | None = None,
            *,
            attachment_action: str | None = None,
        ) -> None: ...

        def _open_help(
            self,
            *,
            initial_page_id: str | None = None,
            initial_query: str = "",
            active_view: str | None = None,
        ) -> None: ...

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
        ) -> None: ...

        def _open_notification_webhook_action(
            self,
            action: str,
            *,
            name: str | None = None,
            provider_kind: str | None = None,
        ) -> None: ...

        def _open_saved_searches(
            self,
            *,
            mode: str = "saved",
            create: bool = False,
        ) -> None: ...

        def _open_settings_surface(
            self,
            *,
            category: str | None = None,
            query: str | None = None,
        ) -> None: ...

        def _open_shortcuts(self) -> None: ...

        def _open_storage_analysis(self, analysis_id: str) -> None: ...

        def _open_tutorials(self, lesson_id: str = "navigation") -> None: ...

        def _open_unanswered(self) -> None: ...

        def _palette_compose_new(
            self,
            invocation: CommandInvocation,
            _context: CommandContext,
        ) -> None: ...

        def _palette_context(
            self,
            handler_ids: Sequence[str] | None = None,
            *,
            source_screen: Screen[Any] | None = None,
        ) -> CommandContext: ...

        def _palette_create_folder(
            self,
            invocation: CommandInvocation,
            _context: CommandContext,
        ) -> None: ...

        def _palette_doctor(
            self, _invocation: CommandInvocation, _context: CommandContext
        ) -> None: ...

        @work(thread=True, exclusive=True, group="draft-send-outcome")
        def _palette_draft_send_status(
            self, invocation: CommandInvocation, _context: CommandContext
        ) -> None: ...

        def _palette_launcher_label(self) -> str | None: ...

        def _palette_leave_offline(
            self, _invocation: CommandInvocation, _context: CommandContext
        ) -> None: ...

        def _palette_offline_status(
            self, _invocation: CommandInvocation, _context: CommandContext
        ) -> None: ...

        def _palette_open_attachments(
            self, invocation: CommandInvocation, _context: CommandContext
        ) -> None: ...

        def _palette_open_folder(
            self, invocation: CommandInvocation, _context: CommandContext
        ) -> None: ...

        def _palette_recover_draft(
            self, invocation: CommandInvocation, _context: CommandContext
        ) -> None: ...

        def _palette_save_current_draft(
            self,
            _invocation: CommandInvocation,
            _context: CommandContext,
        ) -> None: ...

        @staticmethod
        def _palette_scalar(value: object) -> str | None: ...

        def _palette_schedule_current_draft(
            self,
            invocation: CommandInvocation,
            _context: CommandContext,
        ) -> None: ...

        def _palette_search(
            self, invocation: CommandInvocation, _context: CommandContext
        ) -> None: ...

        def _persist_folder_expansion(self) -> None: ...

        def _persist_message_layout(self, *, reason: str = "Mail layout updated") -> bool: ...

        def _persist_setting_values(self, values: Mapping[str, object], *, reason: str) -> bool: ...

        @work(thread=True, exclusive=True, group="folder-mutation")
        def _prepare_folder_empty_path(self, folder: str) -> None: ...

        def _preview_bulk_mark_read(self, scope: str) -> None: ...

        def _preview_discard_composer(self, composer: ComposeScreen) -> None: ...

        def _preview_message_columns(self, value: Any) -> Callable[[], None]: ...

        def _preview_message_move(
            self, destination: str, *, restoring: bool, reference: str | None = None
        ) -> None: ...

        def _preview_message_restore(self) -> None: ...

        def _preview_open_selected_externally(self) -> None: ...

        def _preview_permanent_message_delete(self) -> None: ...

        def _preview_settings_theme(self, value: Any) -> Callable[[], None]: ...

        def _prompt_advanced_search(self, *, initial_query: str = "") -> None: ...

        def _prompt_folder_create(self, invocation: CommandInvocation) -> None: ...

        def _prompt_folder_delete(self) -> None: ...

        def _prompt_folder_empty(self) -> None: ...

        def _prompt_folder_move(self, invocation: CommandInvocation) -> None: ...

        def _prompt_folder_rename(self) -> None: ...

        def _prompt_mail_search(self, *, scope: str, folder: str | None = None) -> None: ...

        def _prompt_message_category(self, invocation: CommandInvocation, *, mode: str) -> None: ...

        def _prompt_message_copy(self, invocation: CommandInvocation) -> None: ...

        def _prompt_message_importance(self, invocation: CommandInvocation) -> None: ...

        def _prompt_message_move(self, invocation: CommandInvocation) -> None: ...

        def _prompt_refine_current_results(self) -> None: ...

        def _prompt_selected_message_export(self) -> None: ...

        def _quota_service(self, gateway: Any) -> QuotaService: ...

        def _refine_current_results(self, query_text: str) -> None: ...

        def _refresh_appearance_settings(self) -> None: ...

        def _refresh_shell_status(self) -> None: ...

        def _remember_mail_search_scope(self, scope: str, folder: str | None = None) -> None: ...

        def _render_expanded_branding(self) -> None: ...

        def _render_message_rows(self, messages: Any) -> None: ...

        def _render_palette_search(
            self,
            messages: Sequence[MessageSummary],
            label: str,
            execution_summary: str,
        ) -> None: ...

        def _request_folder_children(
            self,
            *,
            parent_reference: str | None,
            node_id: Any | None,
            offset: int = 0,
            append: bool = False,
        ) -> None: ...

        def _resize_message_ratio(self, delta: float, *, persist: bool = True) -> None: ...

        def _route_screen(self, target: RouteTarget) -> Screen[Any] | None: ...

        def _run_advanced_palette_search(self, query_text: str, *, label: str) -> None: ...

        def _schedule_viewport_fill(self) -> None: ...

        @work(thread=True, exclusive=False, group="schedule-draft")
        @coordinated_write()
        def _schedule_workspace_draft(self, reference: str, expression: str) -> None: ...

        def _scheduler_service(self) -> SchedulerService: ...

        @work(thread=True, exclusive=True, group="pim-related-mail")
        def _search_calendar_related_messages(self, references: tuple[str, ...]) -> None: ...

        @work(thread=True, exclusive=True, group="pim-related-mail")
        def _search_contact_correspondence(self, addresses: tuple[str, ...]) -> None: ...

        def _search_expression(
            self,
            query_text: str,
            label: str,
            *,
            payload: Mapping[str, Any],
            source: str,
            name: str | None,
        ) -> Worker[None]: ...

        def _search_mail(self, query: SearchQuery, label: str) -> Worker[None]: ...

        @staticmethod
        def _search_operand(
            invocation: CommandInvocation,
            *prefixes: str,
        ) -> str: ...

        def _select_visible_messages(self, mode: str) -> None: ...

        def _selected_folder_info(self) -> FolderInfo | None: ...

        def _selected_message_sender(self) -> Address | None: ...

        def _selected_message_summaries(self) -> tuple[MessageSummary, ...]: ...

        def _selected_message_summary(self) -> MessageSummary | None: ...

        def _semantic_service(self) -> SemanticSearchService: ...

        @work(thread=True, exclusive=False, group="mailbox-mutation")
        @coordinated_write()
        def _set_message_flag(self, reference: str, *, flagged: bool) -> None: ...

        def _set_status(self, text: str) -> None: ...

        def _setting_value(self, setting_id: str, fallback: object) -> object: ...

        def _settings_applied_callback(self, setting_ids: tuple[str, ...]) -> None: ...

        def _settings_service(self) -> SettingsService: ...

        def _show_selected_folder_details(self) -> None: ...

        def _show_selected_folder_unread(self) -> None: ...

        def _slim_branding(self, definition: ThemeDefinition) -> RenderedBranding: ...

        @work(thread=True, exclusive=False, group="mailbox-mutation")
        @coordinated_write()
        def _toggle_message_read(self, reference: str, *, read: bool) -> None: ...

        @staticmethod
        def _valid_shortcut(shortcut: str) -> bool: ...

        @staticmethod
        def _validated_sidebar_ratio(value: Any) -> float: ...

        def _watcher_header_label(self) -> str: ...

        def action_compose(self) -> None: ...

        def action_delete(self) -> None: ...

        def action_forward(self) -> None: ...

        def action_layout_presets(self) -> None: ...

        def action_local_ocr(self) -> None: ...

        def action_palette(self) -> None: ...

        def action_preview_narrower(self) -> None: ...

        def action_preview_wider(self) -> None: ...

        def action_refresh(self) -> None: ...

        def action_reply(self) -> None: ...

        def action_reply_all(self) -> None: ...

        def action_reset_layout(self) -> None: ...

        def action_search(self) -> None: ...

        def action_toggle_preview(self) -> None: ...

        @property
        def active_theme(self) -> ThemeDefinition: ...

        def attach_selected_message_to_composer(
            self, composer: ComposeScreen, *, reference: str | None = None
        ) -> None: ...

        def load_message(self, reference: str, *, mode: str = "auto") -> None: ...

        @work(thread=True, exclusive=True, group="compose-save-preflight")
        def prepare_draft_save(self, composer: ComposeScreen, payload: dict[str, Any]) -> None: ...

        def refresh_mailbox(self) -> None: ...

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
