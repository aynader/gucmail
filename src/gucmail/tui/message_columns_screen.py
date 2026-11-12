"""Guided staged column editing; persistence remains owned by Settings."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from gucmail.errors import ConfigurationError
from gucmail.services.message_columns import (
    COLUMN_LABELS,
    DEFAULT_COLUMNS,
    columns_value,
    parse_columns,
)
from gucmail.tui.area_common import current_row_highlight
from gucmail.tui.literal_table import LiteralDataTable as DataTable


class MessageColumnsScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    MessageColumnsScreen { align: center middle; }
    #columns-dialog { width: 95%; max-width: 90; height: 90%;
        background: $surface; border: round $border; padding: 0 1; }
    #columns-table { height: 1fr; min-height: 3; }
    #columns-choices, #columns-width-row, #columns-actions { height: 3; }
    #columns-choices Checkbox { width: 1fr; }
    #columns-width-label { width: 22; height: 3; content-align: left middle; }
    #column-width { width: 1fr; }
    #columns-error { height: auto; color: $error; }
    MessageColumnsScreen Button { min-width: 0; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "stage", "Stage"),
        Binding("alt+up", "move_up", "Move up"),
        Binding("alt+down", "move_down", "Move down"),
    ]

    def __init__(self, value: object) -> None:
        super().__init__()
        self.columns = list(parse_columns(value))
        self.selected = self.columns[0].id
        self._controls_loaded = False

    def compose(self) -> ComposeResult:
        with Vertical(id="columns-dialog"):
            yield Label("Message columns · stage here, then Apply in Settings")
            yield Static(
                "Alt+↑/↓ reorders. Compact choices apply below 90 list cells.", markup=False
            )
            yield DataTable(id="columns-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="columns-choices"):
                yield Checkbox("Visible", id="column-visible")
                yield Checkbox("Also in compact lists", id="column-compact")
            with Horizontal(id="columns-width-row"):
                yield Label("Width (blank = auto)", id="columns-width-label")
                yield Input(id="column-width", type="integer", placeholder="Automatic")
            yield Static("", id="columns-error", markup=False)
            with HorizontalScroll(id="columns-actions"):
                yield Button("Up", id="column-up")
                yield Button("Down", id="column-down")
                yield Button("Defaults", id="column-defaults")
                yield Button("Cancel", id="column-cancel")
                yield Button("Stage columns", id="column-stage", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#columns-table", DataTable)
        for label, key in (
            ("Column", "name"),
            ("Visible", "visible"),
            ("Compact", "compact"),
            ("Width", "width"),
        ):
            table.add_column(label, key=key)
        self._render_columns()
        self._load_controls()

    def _render_columns(self) -> None:
        table = self.query_one("#columns-table", DataTable)
        table.clear()
        for index, column in enumerate(self.columns):
            table.add_row(
                COLUMN_LABELS[column.id],
                "Yes" if column.visible else "No",
                "Yes" if column.compact else "No",
                str(column.width or "Auto"),
                key=column.id,
            )
            if column.id == self.selected:
                table.move_cursor(row=index)

    def _load_controls(self) -> None:
        value = next(column for column in self.columns if column.id == self.selected)
        self.query_one("#column-visible", Checkbox).value = value.visible
        self.query_one("#column-compact", Checkbox).value = value.compact
        self.query_one("#column-width", Input).value = str(value.width) if value.width else ""
        self._controls_loaded = True

    def _commit_controls(self) -> bool:
        if not self._controls_loaded:
            return True
        raw = self.query_one("#column-width", Input).value.strip()
        try:
            width = int(raw) if raw else None
            if width is not None and not 1 <= width <= 120:
                raise ValueError
        except ValueError:
            self.query_one("#columns-error", Static).update("Width must be blank or 1-120.")
            return False
        index = next(i for i, column in enumerate(self.columns) if column.id == self.selected)
        self.columns[index] = replace(
            self.columns[index],
            width=width,
            visible=self.query_one("#column-visible", Checkbox).value,
            compact=self.query_one("#column-compact", Checkbox).value,
        )
        self.query_one("#columns-error", Static).update("")
        return True

    @on(Checkbox.Changed)
    @on(Input.Changed, "#column-width")
    def _controls_changed(self) -> None:
        if not self._controls_loaded or not self._commit_controls():
            return
        value = next(column for column in self.columns if column.id == self.selected)
        table = self.query_one("#columns-table", DataTable)
        table.update_cell(value.id, "visible", "Yes" if value.visible else "No")
        table.update_cell(value.id, "compact", "Yes" if value.compact else "No")
        table.update_cell(value.id, "width", str(value.width or "Auto"))

    @on(DataTable.RowHighlighted, "#columns-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = str(event.row_key.value)
        if not current_row_highlight(event) or key == self.selected:
            return
        if not self._commit_controls():
            index = next(i for i, c in enumerate(self.columns) if c.id == self.selected)
            self.query_one("#columns-table", DataTable).move_cursor(row=index)
            return
        self.selected = key
        self._load_controls()

    def _move(self, amount: int) -> None:
        if not self._commit_controls():
            return
        index = next(i for i, column in enumerate(self.columns) if column.id == self.selected)
        target = index + amount
        if 0 <= target < len(self.columns):
            self.columns[index], self.columns[target] = self.columns[target], self.columns[index]
        self._render_columns()

    def action_move_up(self) -> None:
        self._move(-1)

    def action_move_down(self) -> None:
        self._move(1)

    def action_stage(self) -> None:
        if not self._commit_controls():
            return
        try:
            value = columns_value(parse_columns(columns_value(tuple(self.columns))))
        except ConfigurationError as exc:
            self.query_one("#columns-error", Static).update(str(exc))
        else:
            self.dismiss(json.dumps(value))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "column-up":
            self.action_move_up()
        elif event.button.id == "column-down":
            self.action_move_down()
        elif event.button.id == "column-stage":
            self.action_stage()
        elif event.button.id == "column-cancel":
            self.action_cancel()
        elif event.button.id == "column-defaults":
            self.columns = list(DEFAULT_COLUMNS)
            self.selected = self.columns[0].id
            self._render_columns()
            self._load_controls()
