"""Keyboard-accessible read-only delegate metadata with local page navigation."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import HorizontalScroll, VerticalScroll
from textual.widgets import Button, Label, Static

from gucmail.security import sanitize_terminal
from gucmail.services.delegates import (
    DelegateInfo,
    DelegatePage,
    DelegateService,
    delegate_details_text,
    delegate_page,
)
from gucmail.tui.area_common import OperationalScreen, PublicOperationError, ViewState
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class DelegatesScreen(OperationalScreen):
    DEFAULT_CSS = """
    DelegatesScreen { layout: vertical; }
    #delegates-table { height: 2fr; min-height: 3; }
    #delegates-details-scroll { height: 3fr; min-height: 3; padding: 0 1; }
    #delegates-toolbar { height: 3; }
    """

    def __init__(self, service: DelegateService, *, page_size: int = 100) -> None:
        super().__init__(area_name="delegates")
        self.service = service
        self.page_size = max(1, min(1000, page_size))
        self.entries: tuple[DelegateInfo, ...] = ()
        self.page_offset = 0
        self.page: DelegatePage | None = None

    def compose(self) -> ComposeResult:
        yield Label("Mailbox delegates · read only", id="delegates-title")
        yield Static("", id="area-state", markup=False)
        yield DataTable(id="delegates-table", cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="delegates-details-scroll"):
            yield Static(
                "Select a delegate to inspect permissions.", id="delegates-details", markup=False
            )
        yield Static("", id="area-progress", markup=False)
        with HorizontalScroll(id="delegates-toolbar"):
            yield Button("Refresh", id="delegates-refresh")
            yield Button("Previous", id="delegates-previous", disabled=True)
            yield Button("Next", id="delegates-next", disabled=True)
            yield Button("Retry", id="area-retry")
            yield Button("Cancel operation", id="area-cancel-operation")
            yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.query_one("#area-retry").display = False
        self.query_one("#area-cancel-operation").display = False
        self.query_one("#delegates-table", DataTable).add_columns(
            "Name", "Address", "Inbox", "Calendar"
        )
        self.action_refresh()

    def action_refresh(self) -> None:
        self._begin_operation(
            "Read mailbox delegates",
            lambda *_: self.service.list(limit=1000),
            self._loaded,
            retry_safe=True,
        )

    def _loaded(self, result: object) -> None:
        if not isinstance(result, DelegatePage):
            raise PublicOperationError("Exchange returned an invalid delegate list.")
        self.entries = result.entries
        self.page_offset = 0
        self._render_page()

    def _render_page(self) -> None:
        self.page = delegate_page(self.entries, limit=self.page_size, offset=self.page_offset)
        table = self.query_one("#delegates-table", DataTable)
        table.clear()
        for index, delegate in enumerate(self.page.entries):
            permissions = {value.folder: value.level for value in delegate.permissions}
            values = (
                delegate.display_name or "—",
                delegate.email_address or delegate.distinguished_user or "—",
                permissions.get("inbox") or "Not reported",
                permissions.get("calendar") or "Not reported",
            )
            table.add_row(*(Text(sanitize_terminal(value)) for value in values), key=str(index))
        table.move_cursor(row=0)
        self.query_one("#delegates-previous", Button).disabled = self.page_offset == 0
        self.query_one("#delegates-next", Button).disabled = self.page.next_offset is None
        self._show_selected()
        start = self.page_offset + 1 if self.page.entries else 0
        end = self.page_offset + len(self.page.entries) if self.page.entries else 0
        self._set_view_state(
            ViewState.READY,
            f"Delegates {start}-{end} of {self.page.total}. One EWS collection; pages are local. "
            "Reading does not change permissions.",
        )

    def _show_selected(self) -> None:
        table = self.query_one("#delegates-table", DataTable)
        if self.page is None or not self.page.entries:
            text = (
                "Exchange returned no delegates for this mailbox. "
                "Folder sharing may exist separately."
            )
        else:
            delegate = self.page.entries[min(table.cursor_row, len(self.page.entries) - 1)]

            text = delegate_details_text(delegate) + (
                "\n\nDelegate permissions do not describe every shared folder "
                "or effective access rule."
            )
        self.query_one("#delegates-details", Static).update(
            sanitize_terminal(text, max_length=20000)
        )

    @on(DataTable.RowHighlighted, "#delegates-table")
    def _selected(self) -> None:
        self._show_selected()

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "delegates-refresh":
            self.action_refresh()
        elif (
            event.button.id == "delegates-next" and self.page and self.page.next_offset is not None
        ):
            self.page_offset = self.page.next_offset
            self._render_page()
        elif event.button.id == "delegates-previous":
            self.page_offset = max(0, self.page_offset - self.page_size)
            self._render_page()
