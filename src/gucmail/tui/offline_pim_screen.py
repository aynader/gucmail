"""Read-only PIM browsing from an explicitly selected local backup."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Button, Input, Label, Static

from gucmail.security import sanitize_terminal
from gucmail.services.backup import BackupService, BackupSummary, OfflineArchive, OfflineItem
from gucmail.tui.area_common import OperationalScreen, ViewState, clean_cell, current_row_highlight
from gucmail.tui.backup_screen import BackupPassphraseScreen
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable

KINDS = {
    "calendar": {"calendar", "event", "calendarevent"},
    "contacts": {"contact", "contacts", "contactinfo"},
    "tasks": {"task", "tasks", "taskinfo"},
}


class OfflinePimScreen(OperationalScreen):
    """Bounded pages, searchable locally, with no online gateway or mutation path."""

    DEFAULT_CSS = """
    #offline-pim-root { padding: 1 2; }
    #offline-pim-root Label, #offline-pim-source { height: auto; }
    #offline-pim-table { height: 1fr; min-height: 4; }
    #offline-pim-reader { height: 1fr; min-height: 4; border: solid $panel; }
    #offline-pim-actions { height: 3; }
    #area-state, #area-progress { height: auto; }
    """

    def __init__(self, service: BackupService, area: str, *, page_size: int = 50) -> None:
        if area not in KINDS:
            raise ValueError("Unknown offline area")
        super().__init__(area_name=area, offline=True)
        self.service = service
        self.area = area
        self.page_size = min(max(page_size, 1), 200)
        self.archive: OfflineArchive | None = None
        self._backups: dict[str, BackupSummary] = {}
        self._items: dict[str, OfflineItem] = {}
        self._offset = 0
        self._selected_item: OfflineItem | None = None
        self._raw_details = False
        self._filter_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="offline-pim-root"):
            yield Label(f"{self.area.title()} · offline backup", markup=False)
            yield Select([], prompt="Choose a completed backup", id="offline-pim-backup")
            yield Static(
                "Choose a backup to browse saved records.", id="offline-pim-source", markup=False
            )
            yield Input(
                placeholder="Search this backup (all saved fields)", id="offline-pim-filter"
            )
            yield Static("Loading backup list…", id="area-state", markup=False)
            yield DataTable(id="offline-pim-table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="offline-pim-reader"):
                yield Static(
                    "Select a record to read its saved details.",
                    id="offline-pim-detail",
                    markup=False,
                )
            with Horizontal(id="offline-pim-actions"):
                yield Button("Previous", id="offline-pim-prev", disabled=True)
                yield Button("Next", id="offline-pim-next", disabled=True)
                yield Button("Raw details", id="offline-pim-raw", disabled=True)
                yield Button("Close", classes="area-close")
            yield Static("", id="area-progress", markup=False)

    def on_mount(self) -> None:
        self.query_one("#offline-pim-table", DataTable).add_columns("Title", "Folder", "Type")
        self._begin_operation(
            "Listing local backups",
            lambda _cancel, _progress: self.service.list(limit=100, inspect_corrupt_sizes=False),
            self._listed,
            retry_safe=True,
        )

    def _listed(self, value: object) -> None:
        backups = cast(list[BackupSummary], value)
        self._backups = {item.backup_id: item for item in backups if item.state == "complete"}
        self.query_one("#offline-pim-backup", Select).set_options(
            (
                clean_cell(
                    f"{item.backup_id} · {item.completed_at or item.created_at}", max_length=180
                ),
                item.backup_id,
            )
            for item in self._backups.values()
        )
        self._set_view_state(
            ViewState.READY if self._backups else ViewState.EMPTY,
            "Choose a backup. No Exchange connection is used."
            if self._backups
            else "No completed backups. Create a backup while online to browse it here.",
        )

    @on(Select.Changed, "#offline-pim-backup")
    def _selected_backup(self, event: Select.Changed) -> None:
        self._stop_filter_timer()
        self._invalidate_operation()
        self.archive = None
        self._items.clear()
        self._render_items()
        backup = self._backups.get(str(event.value))
        if backup is None:
            return

        def open_backup(passphrase: str | None) -> None:
            if backup.encrypted and not passphrase:
                self._set_view_state(ViewState.CANCELLED, "Encrypted backup opening cancelled.")
                return
            self._begin_operation(
                "Opening local backup",
                lambda _cancel, _progress: self.service.open_offline(
                    backup.backup_id, passphrase=passphrase, verify=False
                ),
                self._opened,
                remember=False,
                retry_safe=True,
            )

        if backup.encrypted:
            self.app.push_screen(BackupPassphraseScreen("browse saved " + self.area), open_backup)
        else:
            open_backup(None)

    def _opened(self, value: object) -> None:
        self.archive = cast(OfflineArchive, value)
        self._offset = 0
        self.query_one("#offline-pim-source", Static).update(
            clean_cell(self.archive.source_notice, max_length=500)
        )
        self._load_page()

    def _load_page(self) -> None:
        self._stop_filter_timer()
        archive = self.archive
        if archive is None:
            return
        offset = self._offset
        query = self.query_one("#offline-pim-filter", Input).value
        self.query_one("#offline-pim-prev", Button).disabled = True
        self.query_one("#offline-pim-next", Button).disabled = True
        self._begin_operation(
            "Loading saved items",
            lambda cancel, _progress: archive.list_items(
                kinds=KINDS[self.area],
                offset=offset,
                limit=self.page_size + 1,
                query=query,
                cancelled=cancel.is_set,
            ),
            self._loaded,
            remember=False,
            retry_safe=True,
        )

    def _loaded(self, value: object) -> None:
        items = cast(tuple[OfflineItem, ...], value)
        self._items = {item.item_key: item for item in items[: self.page_size]}
        self.query_one("#offline-pim-prev", Button).disabled = self._offset == 0
        self.query_one("#offline-pim-next", Button).disabled = len(items) <= self.page_size
        self._render_items()

    @staticmethod
    def _details(item: OfflineItem) -> str:
        # Static(markup=False) never evaluates Rich markup or HTML, and control
        # sequences are removed before display. Attachment payloads stay on disk.
        return sanitize_terminal(
            json.dumps(
                {"data": item.data, "metadata": item.metadata, "attachments": item.attachments},
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            max_length=100_000,
        )

    def _render_items(self) -> None:
        table = self.query_one("#offline-pim-table", DataTable)
        table.clear()
        self._selected_item = None
        self.query_one("#offline-pim-raw", Button).disabled = True
        self.query_one("#offline-pim-detail", Static).update(
            "Select a saved record. Details are limited to 100,000 characters."
        )
        for key, item in self._items.items():
            data = item.data if isinstance(item.data, Mapping) else {}
            title = (
                data.get("subject")
                or data.get("display_name")
                or data.get("title")
                or data.get("name")
                or "(untitled)"
            )
            table.add_row(
                clean_cell(title, max_length=200),
                clean_cell(item.folder_path or "", max_length=100),
                clean_cell(item.kind),
                key=key,
            )
        self._set_view_state(
            ViewState.READY if table.row_count else ViewState.EMPTY,
            f"Offline · page {self._offset // self.page_size + 1} · "
            f"{table.row_count} saved record(s). Search covers this area's records in the backup.",
        )

    @staticmethod
    def _readable_details(item: OfflineItem) -> str:
        if not isinstance(item.data, Mapping):
            return OfflinePimScreen._details(item)
        data = item.data
        title = (
            data.get("display_name") or data.get("subject") or data.get("title") or "Saved record"
        )
        lines = [str(title), f"Folder: {item.folder_path or 'Unfiled'}"]
        fields = (
            ("company", "Company"),
            ("job_title", "Job title"),
            ("emails", "Email"),
            ("phones", "Phone"),
            ("addresses", "Address"),
            ("start", "Starts"),
            ("end", "Ends"),
            ("timezone", "Time zone"),
            ("location", "Location"),
            ("status", "Status"),
            ("percent_complete", "Complete (%)"),
            ("start_date", "Start date"),
            ("due_date", "Due date"),
            ("completed_at", "Completed"),
            ("reminder_at", "Reminder"),
            ("reminder_minutes", "Reminder (minutes before)"),
            ("availability", "Availability"),
            ("importance", "Importance"),
            ("categories", "Categories"),
            ("birthday", "Birthday"),
        )
        phone_labels = {
            "businessphone": "Work",
            "homephone": "Home",
            "mobilephone": "Mobile",
            "primaryphone": "Primary",
        }
        for key, label in fields:
            value = data.get(key)
            if value is None or value == "" or value == [] or value == {}:
                continue
            if isinstance(value, Mapping):
                for name, entry in value.items():
                    name = phone_labels.get(str(name).casefold(), str(name))
                    lines.append(f"{label} · {name}: {entry}")
            elif isinstance(value, (list, tuple)):
                parts = [
                    str(entry.get("email") or entry.get("name") or entry)
                    if isinstance(entry, Mapping)
                    else str(entry)
                    for entry in value
                ]
                lines.append(f"{label}: {', '.join(parts)}")
            else:
                text = (
                    str(value).replace("_", " ")
                    if key in {"status", "availability"}
                    else str(value)
                )
                lines.append(f"{label}: {text}")
        for key, label in (("notes", "Notes"), ("body", "Description")):
            if data.get(key):
                lines.extend(("", label, str(data[key])))
        lines.extend(("", "Raw details includes all saved fields and attachment metadata."))
        return sanitize_terminal("\n".join(lines), max_length=100_000)

    def _render_detail(self) -> None:
        item = self._selected_item
        if item is None:
            return
        self.query_one("#offline-pim-detail", Static).update(
            self._details(item) if self._raw_details else self._readable_details(item)
        )
        self.query_one("#offline-pim-reader", VerticalScroll).scroll_home(animate=False)
        button = self.query_one("#offline-pim-raw", Button)
        button.disabled = False
        button.label = "Summary" if self._raw_details else "Raw details"

    @on(Input.Changed, "#offline-pim-filter")
    def _filter(self) -> None:
        self._stop_filter_timer()
        if self.archive is None:
            return
        self._invalidate_operation()
        self._offset = 0
        self._items.clear()
        self._render_items()
        self.query_one("#offline-pim-prev", Button).disabled = True
        self.query_one("#offline-pim-next", Button).disabled = True
        self._set_view_state(ViewState.LOADING, "Searching saved records…")
        self._filter_timer = self.set_timer(0.25, self._load_page)

    def _stop_filter_timer(self) -> None:
        if self._filter_timer is not None:
            self._filter_timer.stop()
            self._filter_timer = None

    @on(DataTable.RowHighlighted, "#offline-pim-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not current_row_highlight(event):
            return
        item = self._items.get(str(event.row_key.value))
        if item:
            self._selected_item = item
            self._render_detail()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "offline-pim-raw":
            self._raw_details = not self._raw_details
            self._render_detail()
            return
        if event.button.id in {"offline-pim-prev", "offline-pim-next"}:
            self._offset = max(
                0,
                self._offset
                + (-self.page_size if event.button.id == "offline-pim-prev" else self.page_size),
            )
            self._load_page()

    def on_unmount(self) -> None:
        self._stop_filter_timer()
        super().on_unmount()
        self.archive = None
        self._selected_item = None
        self._items.clear()
