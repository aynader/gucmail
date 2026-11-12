"""An open palette must track its source workspace and current connection."""

from dataclasses import replace

import pytest
from textual.widgets import Input

from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.delegates_screen import DelegatesScreen
from test_delegates import Gateway, _context


@pytest.mark.asyncio
async def test_open_palette_updates_availability_without_losing_query_or_focus(tmp_path):
    context = _context(tmp_path)
    context.config = replace(context.config, mode="advanced")
    gateway = Gateway()
    app = GUCmailApp(context=context, gateway=gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        for _ in range(100):
            await pilot.pause(0.02)
            if app.ui.online and app.ui.busy is None:
                break
        app.ui.online = False
        await pilot.press("ctrl+k")
        palette = app.screen
        assert isinstance(palette, PaletteScreen)
        await pilot.press(*"Show Mailbox Delegates")
        assert not palette.runtime_context.online
        app.ui.online = True
        for _ in range(30):
            await pilot.pause(0.02)
            if palette.runtime_context.online:
                break
        query = palette.query_one("#palette-input", Input)
        assert query.value == "Show Mailbox Delegates" and palette.focused is query
        assert palette.runtime_context.online
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.02)
            if isinstance(app.screen, DelegatesScreen) and gateway.reads:
                break
        assert isinstance(app.screen, DelegatesScreen) and gateway.reads == 1


@pytest.mark.asyncio
async def test_submit_rechecks_disconnect_even_before_periodic_refresh(tmp_path):
    context = _context(tmp_path)
    context.config = replace(context.config, mode="advanced")
    gateway = Gateway()
    app = GUCmailApp(context=context, gateway=gateway)
    async with app.run_test(size=(80, 24)) as pilot:
        for _ in range(100):
            await pilot.pause(0.02)
            if app.ui.online and app.ui.busy is None:
                break
        await pilot.press("ctrl+k")
        await pilot.press(*"Show Mailbox Delegates")
        palette = app.screen
        app.ui.online = False
        palette._submit_invocation(palette._interpretation.invocation, palette._interpretation)
        assert app.screen is palette and not palette.runtime_context.online
        assert gateway.reads == 0
