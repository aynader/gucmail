"""Read-only conversation response review with explicit identity and folder scope."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.widgets import Button, Input, Label, Static

from gucmail.services.unanswered import UnansweredReport, UnansweredService
from gucmail.tui.area_common import OperationalScreen, PublicOperationError, ViewState, clean_cell
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class UnansweredScreen(OperationalScreen):
    DEFAULT_CSS = """
    UnansweredScreen { padding: 1; }
    #response-title { height: 2; text-style: bold; }
    #response-fields { height: 6; }
    #response-folders { height: 3; }
    #response-folders Input { width: 1fr; }
    #response-limit { width: 18; }
    #response-table { height: 1fr; min-height: 3; }
    #response-note { height: auto; max-height: 3; }
    #area-state, #area-progress { height: auto; }
    #response-footer { dock: bottom; height: 3; }
    #response-footer Button { margin-right: 1; }
    """
    BINDINGS: ClassVar = [
        *OperationalScreen.BINDINGS,
        Binding("enter", "open_message", "Open message", show=False),
    ]

    def __init__(
        self,
        service: UnansweredService,
        *,
        address: str = "",
        open_message: Callable[[str], None],
    ) -> None:
        super().__init__(area_name="unanswered conversations")
        self.service = service
        self.address = address
        self.open_message = open_message
        self._report: UnansweredReport | None = None

    def compose(self) -> ComposeResult:
        yield Label("Unanswered conversations", id="response-title")
        yield Static(
            "Enter all your sending addresses, choose folders, then Analyze.",
            id="area-state",
            markup=False,
        )
        with Vertical(id="response-fields"):
            yield Input(
                self.address,
                placeholder="Your address and aliases (comma-separated)",
                id="response-addresses",
            )
            with Horizontal(id="response-folders"):
                yield Input(
                    "inbox",
                    placeholder="Incoming folders (semicolon-separated)",
                    id="response-incoming",
                )
                yield Input(
                    "sent", placeholder="Sent folders (semicolon-separated)", id="response-sent"
                )
                yield Select(
                    ((f"{count}/folder", count) for count in (50, 100, 200, 500, 1000)),
                    value=100,
                    allow_blank=False,
                    id="response-limit",
                )
        yield DataTable(id="response-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            "No reply found is evidence within these folders, not a judgment that "
            "a reply is expected. Other folders/accounts are outside this scan.",
            id="response-note",
            markup=False,
        )
        yield Static("", id="area-progress", markup=False)
        with HorizontalScroll(id="response-footer"):
            yield Button("Analyze", id="response-analyze", variant="primary")
            yield Button("Open message", id="response-open", disabled=True)
            retry = Button("Retry", id="area-retry")
            retry.display = False
            yield retry
            cancel = Button("Cancel scan", id="area-cancel-operation")
            cancel.display = False
            yield cancel
            yield Button("Close", classes="area-close")

    def on_mount(self) -> None:
        self.query_one("#response-table", DataTable).add_columns("State", "Subject", "Sender")

    @on(Input.Changed)
    @on(Select.Changed, "#response-limit")
    def _scope_changed(self) -> None:
        if not self.is_mounted or (self._report is None and not self._operation_active):
            return
        self._invalidate_operation()
        self._report = None
        self.query_one("#response-table", DataTable).clear()
        self.query_one("#response-open", Button).disabled = True
        self._set_view_state(ViewState.READY, "Scope changed. Analyze to refresh these results.")

    @on(Input.Submitted)
    def _input_submitted(self) -> None:
        self.action_refresh()

    @on(Button.Pressed, "#response-analyze")
    def action_refresh(self) -> None:
        addresses = tuple(self.query_one("#response-addresses", Input).value.split(","))
        folders = tuple(self.query_one("#response-incoming", Input).value.split(";"))
        sent = tuple(self.query_one("#response-sent", Input).value.split(";"))
        limit = int(str(self.query_one("#response-limit", Select).value))
        self._report = None
        self.query_one("#response-table", DataTable).clear()
        self.query_one("#response-open", Button).disabled = True
        self._begin_operation(
            "Loading the conversation",
            lambda cancel, _progress: self.service.analyze(
                self_addresses=addresses,
                folders=folders,
                sent_folders=sent,
                limit=limit,
                cancelled=cancel.is_set,
            ),
            self._loaded,
            retry_safe=True,
        )

    def _loaded(self, result: object) -> None:
        if not isinstance(result, UnansweredReport):
            raise PublicOperationError("The conversation service returned an invalid report.")
        self._report = result
        table = self.query_one("#response-table", DataTable)
        table.clear()
        for finding in result.findings:
            message = finding.message
            table.add_row(
                "Uncertain" if finding.status == "uncertain" else "No reply found",
                clean_cell(message.subject, max_length=160),
                clean_cell(message.sender.email if message.sender else "Unknown sender"),
                key=message.reference,
            )
        self.query_one("#response-open", Button).disabled = not result.findings
        self._set_view_state(
            ViewState.READY if result.findings else ViewState.EMPTY,
            f"{result.no_reply_found} without a later self message · "
            f"{result.uncertain} uncertain · "
            f"{result.later_self_message} with a later self message · "
            f"{result.missing_conversation_id} missing conversation ID · {result.scanned} scanned.",
        )

    @on(DataTable.RowHighlighted, "#response-table")
    def _highlight(self, event: DataTable.RowHighlighted) -> None:
        if self._report is None:
            return
        finding = next(
            (
                value
                for value in self._report.findings
                if value.message.reference == event.row_key.value
            ),
            None,
        )
        if finding:
            self.query_one("#response-note", Static).update(
                clean_cell(finding.explanation)
                + " Results cover the selected folders and aliases only."
            )

    @on(Button.Pressed, "#response-open")
    @on(DataTable.RowSelected, "#response-table")
    def action_open_message(self) -> None:
        table = self.query_one("#response-table", DataTable)
        if self._report is None or not table.row_count:
            return
        reference = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        self.dismiss(None)
        self.open_message(reference)
