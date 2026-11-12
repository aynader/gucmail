from __future__ import annotations

import pytest
from textual.widgets import DataTable, Input

from gucmail.models import Address, MessageSummary
from gucmail.tui.app import GUCmailApp
from gucmail.tui.appearance_screen import ThemeBrowserScreen
from test_tui import context_for


@pytest.mark.asyncio
async def test_late_theme_highlight_cannot_change_the_target_of_favorite(tmp_path):
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(140, 42)) as pilot:
        screen = ThemeBrowserScreen(app.theme_browser_backend)
        await app.push_screen(screen)
        table = screen.query_one("#theme-table", DataTable)
        old_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        late = DataTable.RowHighlighted(table, table.cursor_row, old_key)

        screen.query_one("#theme-search", Input).value = "defalt drk"
        screen._refresh_rows(preserve="builtin.default-dark")
        assert screen._selected_theme_id == "builtin.default-dark"
        assert old_key.value != "builtin.default-dark"
        screen._theme_highlighted(late)
        await pilot.pause()
        screen.action_favorite()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.theme_session.preferences.favorites == ("builtin.default-dark",)


@pytest.mark.asyncio
async def test_late_mail_highlight_cannot_replace_selection_after_refresh(tmp_path):
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        rows = [
            MessageSummary(
                reference=ref,
                subject=ref,
                sender=Address("synthetic@example.test"),
                received_at=None,
            )
            for ref in ("mail-old", "mail-selected")
        ]
        app._messages = rows
        app._render_message_rows(rows)
        table = app.query_one("#message-table", DataTable)
        late = DataTable.RowHighlighted(table, 0, table.coordinate_to_cell_key((0, 0)).row_key)
        app.ui.selected_reference = "mail-selected"
        app._render_message_rows(rows)
        app.highlighted(late)
        await pilot.pause()
        assert app.ui.selected_reference == "mail-selected"
        table.clear()
        app.highlighted(late)
        assert app.ui.selected_reference == "mail-selected"
