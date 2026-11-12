"""Tables treat strings as data; explicit Rich Text carries trusted styling."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from rich.text import Text
from textual.widgets import DataTable
from textual.widgets.data_table import ColumnKey, RowKey

from gucmail.security import sanitize_terminal

Cell = TypeVar("Cell")


def literal_cell(value: Any) -> Any:
    """Keep literal brackets and strip terminal controls before Rich sees a string.

    Existing Rich renderables are deliberate application formatting. Domain data
    should arrive as plain strings, never as markup interpreted by DataTable.
    """
    return Text(sanitize_terminal(value)) if isinstance(value, str) else value


class LiteralDataTable(DataTable[Cell]):
    """Apply the literal boundary on insertion, column defaults and updates.

    Use public DataTable methods so width measurement, bulk insertion and
    coordinate-based updates all share the same protection.
    """

    def _update_dimensions(self, new_rows: Iterable[RowKey]) -> None:
        # Textual can paint a newly populated row before Idle measures its
        # content. Its line/row cache keys omit column widths, so that first
        # paint can survive a successful measurement indefinitely. Invalidate
        # only when effective widths change; never remeasure the whole dataset.
        before = tuple(column.get_render_width(self) for column in self.ordered_columns)
        super()._update_dimensions(new_rows)
        after = tuple(column.get_render_width(self) for column in self.ordered_columns)
        if before != after:
            self._clear_caches()
            self.refresh()

    def add_row(
        self,
        *cells: Cell,
        height: int | None = 1,
        key: str | None = None,
        label: str | Text | None = None,
    ) -> RowKey:
        return super().add_row(
            *(literal_cell(cell) for cell in cells),
            height=height,
            key=key,
            label=literal_cell(label),
        )

    def add_column(
        self,
        label: str | Text,
        *,
        width: int | None = None,
        key: str | None = None,
        default: Cell | None = None,
    ) -> ColumnKey:
        return super().add_column(
            literal_cell(label), width=width, key=key, default=literal_cell(default)
        )

    def update_cell(
        self,
        row_key: RowKey | str,
        column_key: ColumnKey | str,
        value: Cell,
        *,
        update_width: bool = False,
    ) -> None:
        super().update_cell(row_key, column_key, literal_cell(value), update_width=update_width)
