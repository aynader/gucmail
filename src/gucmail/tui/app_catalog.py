"""Main window: palette commands that open screens and run actions."""

from __future__ import annotations

import contextlib
import html
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import (
    Input,
    TextArea,
    Tree,
)

from gucmail.errors import (
    ConfigurationError,
)
from gucmail.models import (
    CalendarResponse,
    MessageDetail,
    MessageSummary,
)
from gucmail.security import sanitize_terminal
from gucmail.settings import DEFAULT_SETTINGS
from gucmail.tui.app_core import AppCore
from gucmail.tui.app_helpers import _safe_failure
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.shell_widgets import AreaScreen

if TYPE_CHECKING:
    from gucmail.commands import CommandInvocation
    from gucmail.tui.palette_routes import PaletteRoute, RouteTarget


class CatalogMixin(AppCore):
    """Methods of GUCmailApp; see gucmail.tui.app."""

    @staticmethod
    def _route_area(target: RouteTarget) -> str | None:
        from gucmail.tui.palette_routes import RouteTarget

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
        from gucmail.tui.activity_screen import ActivityScreen
        from gucmail.tui.appearance_screen import ThemeBrowserScreen
        from gucmail.tui.backup_screen import BackupScreen
        from gucmail.tui.compose_screen import ComposeScreen
        from gucmail.tui.diagnostics_screen import DiagnosticsScreen
        from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
        from gucmail.tui.local_search_screen import LocalSearchScreen
        from gucmail.tui.notification_settings_screen import NotificationSettingsScreen
        from gucmail.tui.palette_routes import RouteTarget
        from gucmail.tui.pim_screens import CalendarScreen, ContactsScreen, TasksScreen
        from gucmail.tui.saved_search_screen import SavedSearchScreen
        from gucmail.tui.scheduled_screen import ScheduledScreen
        from gucmail.tui.settings_screen import SettingsScreen
        from gucmail.tui.storage_screen import StorageScreen

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
        from gucmail.tui.app_palette import PaletteMixin

        value = PaletteMixin._palette_scalar(invocation.get("query")) or ""
        if not value:
            value = (invocation.original_text or "").strip()
        lowered = value.casefold()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return value[len(prefix) :].strip()
        return value.strip()

    def _catalog_search(self, route: PaletteRoute, invocation: CommandInvocation) -> None:
        from gucmail.commands import CommandInvocation
        from gucmail.palette import universal_palette_registry
        from gucmail.services.local_search import LocalSearchMode
        from gucmail.tui.dialogs import MailQueryScreen
        from gucmail.tui.palette_routes import RouteTarget
        from gucmail.tui.pim_screens import TasksScreen

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
        from gucmail.services.drafts import DraftService
        from gucmail.tui.activity_screen import ActivityScreen
        from gucmail.tui.compose_screen import ComposeScreen
        from gucmail.tui.diagnostics_screen import DiagnosticsScreen
        from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
        from gucmail.tui.palette_routes import RouteTarget
        from gucmail.tui.pim_screens import CalendarScreen, ContactsScreen, TasksScreen
        from gucmail.tui.scheduled_screen import ScheduledScreen
        from gucmail.tui.storage_screen import StorageScreen

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
                        self._set_status(_safe_failure(exc, action="Couldn't open the draft"))
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

    def _catalog_application_action(
        self, route: PaletteRoute, invocation: CommandInvocation | None = None
    ) -> None:
        from gucmail.commands import CommandInvocation

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

        from gucmail.tui.backup_screen import BackupScreen
        from gucmail.tui.local_search_screen import LocalSearchScreen
        from gucmail.tui.palette_routes import RouteTarget

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
                    self._set_status("Run a search before backing up its results.")
                    return True
                selected_messages = tuple(
                    item.reference
                    for item in self._visible_messages
                    if isinstance(item, MessageSummary)
                )
                if not selected_messages:
                    self._set_status("The current search has no messages to back up.")
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
        from gucmail.services.local_search import LocalSearchMode
        from gucmail.tui.activity_screen import ActivityScreen
        from gucmail.tui.appearance_screen import ThemeBrowserScreen
        from gucmail.tui.backup_screen import BackupScreen
        from gucmail.tui.compose_screen import ComposeScreen
        from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
        from gucmail.tui.local_search_screen import LocalSearchScreen
        from gucmail.tui.notification_settings_screen import NotificationSettingsScreen
        from gucmail.tui.palette_routes import RouteTarget
        from gucmail.tui.pim_screens import CalendarScreen, ContactsScreen, TasksScreen
        from gucmail.tui.scheduled_screen import ScheduledScreen
        from gucmail.tui.settings_screen import SettingsScreen
        from gucmail.tui.storage_screen import StorageScreen

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
                self._set_status(_safe_failure(exc, action="Couldn't change the look"))
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
                self._set_status(_safe_failure(exc, action="Couldn't reset the look"))
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

    def _catalog_help(
        self,
        route: PaletteRoute,
        invocation: CommandInvocation,
    ) -> None:
        from gucmail.tui.help_screen import HelpScreen
        from gucmail.tui.palette_routes import RouteTarget
        from gucmail.tui.settings_screen import SettingsScreen

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

    def _catalog_direct(self, route: PaletteRoute, invocation: CommandInvocation) -> None:
        from gucmail.commands import CommandInvocation
        from gucmail.tui.compose_screen import ComposeScreen
        from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
        from gucmail.tui.palette_routes import RouteTarget
        from gucmail.tui.reader import FloatingReader

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
            self._set_status("Copied message information to the clipboard")
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
