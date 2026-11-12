"""User-facing entry points and safe content in the redesigned terminal UI."""

from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import Button, Checkbox, Static

from gucmail.models import Address, DraftData, MessageDetail, MessageSummary
from gucmail.tui.app import ComposeScreen, GUCmailApp, ReaderScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_tui import FakeGateway, _ready_composer_for_editing, context_for


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [80, 160])
async def test_visible_settings_tab_opens_and_returns_to_mail(tmp_path: Path, width: int) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    async with app.run_test(size=(width, 32)) as pilot:
        await pilot.pause()
        tab = app.query_one("#workspace-settings", Button)
        assert tab.region.right <= width
        await pilot.click(tab)
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("escape")
        assert not isinstance(app.screen, SettingsScreen)
        assert app.query_one("#message-table").is_on_screen


@pytest.mark.asyncio
async def test_compose_options_keep_nondefault_delivery_choices_on_reopen(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        screen = ComposeScreen(DraftData(reference="synthetic", request_read_receipt=True))
        app.push_screen(screen)
        await pilot.pause()
        await _ready_composer_for_editing(pilot)
        assert screen.query_one("#compose-options-area").display
        assert screen.query_one("#compose-read-receipt", Checkbox).value
        await pilot.click("#compose-options-toggle")
        assert not screen.query_one("#compose-options-area").display
        assert screen.query_one("#compose-read-receipt", Checkbox).value
        for selector in ("#compose-save", "#compose-send"):
            button = screen.query_one(selector, Button)
            assert screen.query_one("#composer").content_region.contains_region(button.region)


@pytest.mark.asyncio
async def test_full_reader_formats_our_labels_but_preserves_hostile_markup(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    detail = MessageDetail(
        summary=MessageSummary(
            reference="synthetic",
            subject="[bold]Subject[/bold]",
            sender=Address("a@example.test"),
            received_at=None,
        ),
        body_text="[red]Literal body[/red]\x1b[2J",
    )
    async with app.run_test(size=(120, 36)) as pilot:
        app._render_message(detail, mode="full")
        await pilot.pause()
        assert isinstance(app.screen, ReaderScreen)
        content = app.screen.query_one("#reader-screen-content", Static).content
        assert isinstance(content, Text)
        assert "[bold]Subject[/bold]" in content.plain
        assert "[red]Literal body[/red]" in content.plain
        assert "\x1b" not in content.plain
        assert "[b]Subject" not in content.plain
