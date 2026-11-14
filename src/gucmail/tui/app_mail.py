"""Main window: loading, showing and changing messages."""

from __future__ import annotations

import contextlib
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, cast
from uuid import uuid4

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.css.query import NoMatches
from textual.dom import NoScreen
from textual.widget import Widget
from textual.widgets import (
    Input,
    Static,
    Tree,
)
from textual.worker import Worker, get_current_worker

from gucmail.commands import (
    CommandInvocation,
)
from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import (
    ConfigurationError,
    MutationExecutionError,
)
from gucmail.formatting import format_timestamp, human_bytes
from gucmail.models import (
    Address,
    Importance,
    ItemSource,
    MessageDetail,
    MessageSummary,
    SearchQuery,
)
from gucmail.security import sanitize_terminal
from gucmail.services._base import MutationPlan
from gucmail.services.contact_preferences import ContactOperationPreferences
from gucmail.services.gateway import DeferredGateway as _DeferredGateway
from gucmail.services.mail_preferences import (
    MailDeletionPreferences,
    conversation_page_positions,
    group_message_page,
)
from gucmail.services.messages import MessageService
from gucmail.services.quota import (
    compact_quota_status,
)
from gucmail.services.ui_session import (
    MailSession,
)
from gucmail.tui.app_core import AppCore
from gucmail.tui.app_helpers import _open_file_with_registered_handler, _safe_failure
from gucmail.tui.dialogs import (
    ConfirmationScreen,
    FolderNameScreen,
    MailQueryScreen,
    MessageExportPathScreen,
)
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.reader import FloatingReader, ReaderModeScreen, ReaderScreen
from gucmail.tui.shell_widgets import AreaScreen, _AutomaticReadRequest
from gucmail.tui.write_coordinator import (
    coordinated_write,
)


class MailMixin(AppCore):
    """Methods of GUCmailApp; see gucmail.tui.app."""

    def _initial_mailbox_refresh(self) -> None:
        # A user action may have started a search before the first layout completed.
        if self._mailbox_generation == 0:
            self.refresh_mailbox()

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

    def _selected_message_sender(self) -> Address | None:
        summary = self._selected_message_summary()
        return summary.sender if summary is not None else None

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

    def action_mark_selected_read(self) -> None:
        """M: mark the selected messages (or the open one) as read, after a preview."""
        self._preview_bulk_mark_read("selected")

    def action_export_selected(self) -> None:
        """E: save the selected messages (or the open one) to files."""
        self._prompt_selected_message_export()

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
