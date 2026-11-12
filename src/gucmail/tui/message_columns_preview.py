"""Inspect a snapshot produced by the actual mailbox-list renderer."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from gucmail.tui.literal_table import LiteralDataTable as DataTable


class MessageColumnsPreviewScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    MessageColumnsPreviewScreen { align: center middle; background: $background 80%; }
    #column-preview-box {
        width: 96%; height: 88%; border: thick $accent;
        background: $gucmail-modal-background; padding: 1;
    }
    #column-preview-note { height: auto; }
    #column-preview-table { height: 1fr; }
    #column-preview-actions { height: 3; }
    #column-preview-actions Button { width: auto; min-width: 8; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel preview"),
        Binding("ctrl+s", "apply", "Apply staged settings"),
    ]

    def __init__(self, source: DataTable[Any], selected: str | None) -> None:
        super().__init__()
        self.columns = tuple(
            (column.label.copy(), str(key.value), column.width)
            for key, column in source.columns.items()
        )
        self.rows = tuple(
            (
                str(row.key.value),
                tuple(
                    cell.copy() if isinstance(cell, Text) else cell
                    for cell in source.get_row(row.key)
                ),
                row.height,
            )
            for row in source.ordered_rows[:200]
        )
        self.selected = selected

    def compose(self) -> ComposeResult:
        with Vertical(id="column-preview-box"):
            yield Static(
                "Mailbox column preview · loaded rows only\n"
                "Apply keeps all staged settings. Cancel restores the prior columns.",
                id="column-preview-note",
                markup=False,
            )
            yield DataTable(id="column-preview-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="column-preview-actions"):
                yield Button("Apply staged settings", id="column-preview-apply", variant="primary")
                yield Button("Cancel preview", id="column-preview-cancel")
                yield Button("Keep editing", id="column-preview-edit")

    def on_mount(self) -> None:
        table = self.query_one("#column-preview-table", DataTable)
        for label, key, width in self.columns:
            table.add_column(label, key=key, width=width)
        for index, (key, cells, height) in enumerate(self.rows):
            table.add_row(*cells, key=key, height=height)
            if key == self.selected:
                table.move_cursor(row=index)
        table.focus()

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    def action_apply(self) -> None:
        self.dismiss("apply")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "column-preview-apply":
            self.action_apply()
        elif event.button.id == "column-preview-cancel":
            self.action_cancel()
        elif event.button.id == "column-preview-edit":
            self.dismiss(None)
