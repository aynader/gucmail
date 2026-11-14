"""Responsive Textual mail client over the same gateway used by the CLI."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import Any, ClassVar, Literal, TypeVar, overload
from uuid import uuid4

from rich.console import RenderableType
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.dom import NoScreen
from textual.screen import Screen, ScreenResultCallbackType
from textual.widget import AwaitMount, Widget
from textual.widgets import (
    Button,
    Input,
    Static,
    Tree,
)

from gucmail.bootstrap import AppContext
from gucmail.commands import (
    CommandInvocation,
)
from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import (
    ConfigurationError,
)
from gucmail.models import (
    Address,
    DraftData,
    MessageSummary,
)
from gucmail.security import sanitize_terminal
from gucmail.services.calendar_preferences import (
    CalendarOperationPreferences,
)
from gucmail.services.contact_preferences import ContactOperationPreferences
from gucmail.services.drafts import DraftService
from gucmail.services.keyboard_bindings import (
    KEYBOARD_SETTING,
)
from gucmail.services.keyboard_preferences import KeyboardPreferencesService
from gucmail.services.shell_preferences import (
    status_text,
)
from gucmail.services.ui_session import (
    FOCUS_TARGETS,
    MAX_SCROLL,
    SEARCH_SCOPES,
    MailSession,
)
from gucmail.tui.activity_screen import MessageOperationRequested
from gucmail.tui.app_areas import AreasMixin
from gucmail.tui.app_catalog import CatalogMixin
from gucmail.tui.app_compose import ComposeMixin
from gucmail.tui.app_folders import FoldersMixin
from gucmail.tui.app_helpers import (
    _compose_attachment_paths,
    _is_mail_folder_class,
    _open_file_with_registered_handler,
    _safe_failure,
)
from gucmail.tui.app_mail import MailMixin
from gucmail.tui.app_palette import PaletteMixin
from gucmail.tui.app_preferences import PreferencesMixin
from gucmail.tui.area_common import (
    OperationalScreen,
    ViewState,
    current_row_highlight,
)
from gucmail.tui.compose_screen import (
    ComposerInput,
    ComposerTextArea,
    ComposeScreen,
    _encode_compose_attachment_paths,
)
from gucmail.tui.contact_groups_screen import (
    ContactGroupComposeRequested,
    ContactGroupsRequested,
)
from gucmail.tui.dialogs import (
    ConfirmationScreen,
    FolderDetailsScreen,
    FolderMoveScreen,
    FolderNameScreen,
    MailQueryScreen,
    MessageExportPathScreen,
)
from gucmail.tui.drafts_attachments_screen import (
    DraftAttachmentScreen,
    DraftHandoff,
    DraftHandoffKind,
    DraftRecoveryRequested,
    DraftScheduleScreen,
)
from gucmail.tui.help_screen import (
    HelpCommandRequested,
    HelpScreen,
    HelpSettingRequested,
    HelpTutorialsRequested,
    SettingsHelpStateStore,
)
from gucmail.tui.keyboard_bindings import apply_keyboard_bindings
from gucmail.tui.keyboard_editor import KeyboardEditorScreen
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
)
from gucmail.tui.palette_routes import (
    RouteTarget,
)
from gucmail.tui.palette_screen import (
    PaletteParameterReviewScreen,
    PaletteScreen,
    PaletteSequenceDecision,
    PaletteSequenceItem,
    PaletteSequencePreview,
    PaletteSequencePreviewScreen,
    PaletteSequenceSubmission,
    PaletteSubmission,
)
from gucmail.tui.pim_screens import (
    CalendarScreen,
    PimActionRequested,
)
from gucmail.tui.reader import FloatingReader, MessagePreviewScroll, ReaderModeScreen, ReaderScreen
from gucmail.tui.saved_search_screen import (
    SavedSearchHelpRequested,
    SavedSearchRunRequested,
)
from gucmail.tui.semantic_screen import SemanticScreen
from gucmail.tui.settings_screen import (
    AppearanceBrowseRequested,
    ColumnPreviewRequested,
    SettingsHelpRequested,
    SettingsScreen,
    SettingsSetupRequested,
)
from gucmail.tui.shell_widgets import (
    AccountStatus,
    AreaScreen,
    CompactHeader,
    MailMessageTable,
    PaneDivider,
    UIState,
    _AutomaticReadRequest,
)
from gucmail.tui.shortcuts_screen import (
    ShortcutSettingRequested,
    ShortcutsRequested,
)
from gucmail.tui.stable_footer import StableFooter as Footer
from gucmail.tui.themes import (
    ThemeDefinition,
)
from gucmail.tui.tutorial_screen import TutorialHelpRequested
from gucmail.tui.visual_language import FRONTEND_CSS
from gucmail.tui.write_coordinator import (
    CHANGE_IN_PROGRESS_MESSAGE,
    PENDING_CHANGE_MESSAGE,
    WriteBlocked,
    WriteStateChanged,
    app_write_coordinator,
)

_ShortcutScreenResult = TypeVar("_ShortcutScreenResult")


class GUCmailApp(
    PreferencesMixin,
    FoldersMixin,
    PaletteMixin,
    MailMixin,
    AreasMixin,
    ComposeMixin,
    CatalogMixin,
    App[None],
):
    TITLE = "gucmail"
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
        Binding("m", "mark_selected_read", "Mark selected read", show=False),
        Binding("e", "export_selected", "Export", show=False),
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

    @on(events.Resize)
    def resized(self, event: events.Resize) -> None:
        self._apply_breakpoint(event.size.width)
        self._apply_layout()
        self._render_expanded_branding()
        self.call_after_refresh(self._fit_message_columns)
        self.call_after_refresh(self._schedule_viewport_fill)

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

    def _set_status(self, text: str) -> None:
        self._status_activity = text
        self._refresh_status_bar()

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

    @staticmethod
    def _bounded_lines(lines: list[str], *, empty: str) -> str:
        return "\n".join(lines) if lines else empty

    @on(LocalSearchScopeChanged)
    def local_search_scope_changed(self, event: LocalSearchScopeChanged) -> None:
        if event.mode.value in SEARCH_SCOPES:
            self._last_search_scope = event.mode.value
            self._capture_session()

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

    def action_help(self) -> None:
        self._open_help()

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

    @on(InboxRulesRequested)
    def _inbox_rules_requested(self, event: InboxRulesRequested) -> None:
        event.stop()
        self._open_area("inbox_rules")

    @on(AutomaticRepliesRequested)
    def _automatic_replies_requested(self, event: AutomaticRepliesRequested) -> None:
        event.stop()
        self._open_area("automatic_replies")

    @staticmethod
    def _external_editor_finished(composer: ComposeScreen, draft: DraftData) -> None:
        if composer.is_mounted:
            composer.apply_external_draft(draft)

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


def run_tui(context: AppContext | None = None, *, offline: bool = False) -> None:
    GUCmailApp(context=context, offline=offline).run()


__all__ = [
    "AccountStatus",
    "AreaScreen",
    "CompactHeader",
    "ComposeScreen",
    "ComposerInput",
    "ComposerTextArea",
    "ConfirmationScreen",
    "FloatingReader",
    "FolderDetailsScreen",
    "FolderMoveScreen",
    "FolderNameScreen",
    "GUCmailApp",
    "HelpCommandRequested",
    "HelpScreen",
    "HelpSettingRequested",
    "HelpTutorialsRequested",
    "MailMessageTable",
    "MailQueryScreen",
    "MessageExportPathScreen",
    "MessagePreviewScroll",
    "PaletteParameterReviewScreen",
    "PaletteScreen",
    "PaletteSequenceDecision",
    "PaletteSequenceItem",
    "PaletteSequencePreview",
    "PaletteSequencePreviewScreen",
    "PaletteSequenceSubmission",
    "PaletteSubmission",
    "PaneDivider",
    "ReaderModeScreen",
    "ReaderScreen",
    "SettingsHelpStateStore",
    "UIState",
    "_AutomaticReadRequest",
    "_compose_attachment_paths",
    "_encode_compose_attachment_paths",
    "_is_mail_folder_class",
    "_open_file_with_registered_handler",
    "_safe_failure",
    "run_tui",
]
