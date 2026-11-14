"""Main window: what the command palette does with what you type."""

from __future__ import annotations

import contextlib
import platform
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, cast
from zoneinfo import ZoneInfo

from textual import work
from textual.screen import Screen
from textual.widgets import (
    Input,
    Static,
)

from gucmail.commands import (
    CommandConfirmationRequired,
    CommandContext,
    CommandExecutor,
    CommandInvocation,
    CommandUnavailableError,
    Exclusion,
    Ranking,
)
from gucmail.config import normalize_theme_identifier
from gucmail.errors import (
    ConfigurationError,
)
from gucmail.formatting import human_bytes
from gucmail.models import (
    Importance,
    MessageSummary,
    SearchExclusion,
    SearchQuery,
    SearchRanking,
)
from gucmail.nlp import (
    PersonEntity,
)
from gucmail.palette import (
    PaletteSuggestion,
    SuggestionSource,
    universal_palette_registry,
)
from gucmail.security import redact_text, sanitize_terminal
from gucmail.services.attachments import AttachmentService, CollisionPolicy
from gucmail.services.gateway import DeferredGateway as _DeferredGateway
from gucmail.services.index import SearchIndex
from gucmail.services.local_search import LocalSearchMode
from gucmail.services.messages import MessageService
from gucmail.services.search import evaluate_residual, parse_query
from gucmail.services.shortcuts import normalize_shortcut
from gucmail.settings import SettingsContext, SettingsService
from gucmail.tui.app_core import AppCore
from gucmail.tui.app_helpers import _safe_failure
from gucmail.tui.area_backends import (
    BackupAreaBackend,
)
from gucmail.tui.backup_screen import BackupScreen
from gucmail.tui.compose_screen import ComposeScreen
from gucmail.tui.dialogs import ConfirmationScreen, FolderNameScreen, MailQueryScreen
from gucmail.tui.drafts_attachments_screen import (
    DraftAttachmentScreen,
)
from gucmail.tui.help_screen import HelpScreen
from gucmail.tui.palette_routes import (
    CATALOG_ROUTES,
    RouteKind,
    RouteTarget,
    catalog_handler_ids,
    catalog_route,
)
from gucmail.tui.palette_screen import (
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
    ContactsScreen,
    CorrespondentsScreen,
    TasksScreen,
)
from gucmail.tui.saved_search_screen import (
    StateSavedSearchBackend,
)
from gucmail.tui.scheduled_screen import ScheduledScreen
from gucmail.tui.settings_screen import (
    SettingsScreen,
)
from gucmail.tui.shell_widgets import AreaScreen
from gucmail.tui.themes import (
    ThemeValidationError,
)
from gucmail.tui.write_coordinator import (
    coordinated_write,
)


class PaletteMixin(AppCore):
    """Methods of GUCmailApp; see gucmail.tui.app."""

    def _palette_launcher_label(self) -> str | None:
        labels = {
            "circle": "●",
            "hamburger": "☰",
            "search": "⌕",
            "text": "Commands",
            "custom": sanitize_terminal(self._palette_launcher_symbol, max_length=8) or "⌘",
        }
        return labels.get(self._palette_launcher_style)

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

    def _palette_open_message(
        self, invocation: CommandInvocation, _context: CommandContext
    ) -> None:
        reference = self._palette_scalar(invocation.get("reference")) or self.ui.selected_reference
        if not reference:
            self._set_status("Select a message before opening it.")
            return
        self.load_message(reference, mode="auto")

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

    def _palette_mark_read(self, invocation: CommandInvocation, _context: CommandContext) -> None:
        reference = self._palette_scalar(invocation.get("reference")) or self.ui.selected_reference
        if not reference:
            self._set_status("Select a message before changing its read state.")
            return
        self._toggle_message_read(reference, read=invocation.command_id == "mail.mark_read")

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

    def _palette_settings_search(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        self._open_settings_surface(query=self._palette_scalar(invocation.get("query")) or "")

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
        if (
            composer is not None
            and (
                (route is not None and route.target is RouteTarget.COMPOSER)
                or submission.invocation.command_id in {"draft.save", "scheduled.schedule_current"}
            )
            and composer.defer_status_action(lambda: self._execute_palette_submission(submission))
        ):
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
            self._set_status("Command unavailable: " + sanitize_terminal(str(exc), max_length=300))
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
