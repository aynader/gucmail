"""Dropdown labels are data; explicit Rich Text retains trusted application styling."""

from collections.abc import Iterable
from typing import Any, TypeVar

from rich.console import RenderableType
from textual.widgets import Select

from gucmail.tui.literal_table import literal_cell

SelectValue = TypeVar("SelectValue")


class LiteralSelect(Select[SelectValue]):
    """Protect initial and replaced options without changing their identity/value."""

    def __init__(
        self, options: Iterable[tuple[RenderableType, SelectValue]], **kwargs: Any
    ) -> None:
        super().__init__(((literal_cell(label), value) for label, value in options), **kwargs)

    def set_options(self, options: Iterable[tuple[RenderableType, SelectValue]]) -> None:
        super().set_options((literal_cell(label), value) for label, value in options)
