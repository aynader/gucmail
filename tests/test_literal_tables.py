"""Hostile strings must remain literal through every DataTable write path."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree as ElementTree
from rich.text import Text
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate

from gucmail.tui.app import AreaScreen, GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.literal_table import LiteralDataTable
from test_tui import context_for


class TableApp(App[None]):
    def compose(self) -> ComposeResult:
        yield LiteralDataTable(id="records")


@pytest.mark.asyncio
async def test_first_painted_row_is_invalidated_after_column_measurement() -> None:
    app = TableApp()
    async with app.run_test(size=(100, 30)):
        table = app.query_one(LiteralDataTable)
        table.add_column("Name", key="name")
        table.add_column("State", key="state")
        label = "University student [red] literal"
        table.add_row(label, "Ready")
        before = table.render_line(table.header_height).text
        assert label not in before
        # Force the same deferred measurement that DataTable performs on Idle,
        # after an initial paint has already populated its line/row caches.
        table._update_dimensions(tuple(table.rows))
        assert label in table.render_line(table.header_height).text


@pytest.mark.asyncio
async def test_hostile_cells_labels_defaults_and_updates_render_literally() -> None:
    app = TableApp()
    async with app.run_test(size=(80, 24)) as pilot:
        table = app.query_one(LiteralDataTable)
        table.add_column("Column [/red]", key="name")
        table.add_rows((("Synthetic [red]literal[/red]",), ("Synthetic [/red]",)))
        table.add_row("\x1b[31mANSI\x1b[0m", key="ansi", label="Row [/red]")
        table.add_column("Extra", key="extra", default="Default [/red]")
        table.update_cell("ansi", "name", "Updated [/red]", update_width=True)
        table.update_cell_at(Coordinate(0, 1), "研究 👩🏽‍💻 [link=x]literal[/link]")
        await pilot.pause()
        assert table.columns[next(iter(table.columns))].label.plain == "Column [/red]"
        assert str(table.get_cell_at(Coordinate(0, 0))) == "Synthetic [red]literal[/red]"
        assert str(table.get_cell_at(Coordinate(1, 0))) == "Synthetic [/red]"
        assert str(table.get_cell("ansi", "name")) == "Updated [/red]"
        assert str(table.get_cell("ansi", "extra")) == "Default [/red]"
        assert str(table.get_cell_at(Coordinate(0, 1))) == "研究 👩🏽‍💻 [link=x]literal[/link]"
        assert table.rows[next(reversed(table.rows))].label.plain == "Row [/red]"
        # Force an actual terminal-cell render, rather than testing stored values only.
        svg = app.export_screenshot()
        rendered_text = [
            "".join(node.itertext()).replace("\xa0", " ")
            for node in ElementTree.fromstring(
                svg.encode(), parser=ElementTree.XMLParser(resolve_entities=False, no_network=True)
            ).iter("{http://www.w3.org/2000/svg}text")
        ]
        assert any("Updated [/red]" in text for text in rendered_text)


@pytest.mark.asyncio
async def test_explicit_styled_text_survives_and_terminal_controls_are_removed() -> None:
    app = TableApp()
    async with app.run_test() as pilot:
        table = app.query_one(LiteralDataTable)
        table.add_column("Value", key="value")
        styled = Text("Trusted application formatting", style="bold")
        table.add_row(styled, key="styled")
        table.add_row("\x1b]0;injected title\x07Safe\x1b[31m text", key="string")
        await pilot.pause()
        assert table.get_cell("styled", "value") is styled
        assert styled.style == "bold"
        assert str(table.get_cell("string", "value")) == "Safe text"


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [False, True])
async def test_record_bearing_dialog_titles_are_literal(tmp_path: Path, confirmation: bool) -> None:
    title = "Synthetic [/red] [link=https://example.test]title[/link]"
    screen = (
        ConfirmationScreen(title, "Synthetic details")
        if confirmation
        else AreaScreen(title, lambda: "Synthetic content")
    )
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        svg = app.export_screenshot()
        root = ElementTree.fromstring(
            svg.encode(), parser=ElementTree.XMLParser(resolve_entities=False, no_network=True)
        )
        text = " ".join(
            "".join(node.itertext()) for node in root.iter("{http://www.w3.org/2000/svg}text")
        ).replace("\xa0", " ")
        assert "Synthetic [/red]" in text
        assert "[link=https://example.test]" in text
