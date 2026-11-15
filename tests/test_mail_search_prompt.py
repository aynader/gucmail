"""Canonical palette commands ask for a term without losing their explicit scope."""

from pathlib import Path

import pytest
from textual.widgets import Input

from gucmail.settings import SettingsService
from gucmail.tui.app import FolderNameScreen, GUCmailApp, PaletteScreen
from test_tui import FakeGateway
from test_ui_session import context_for
from waiting import rounds


async def wait(pilot, predicate):
    for _ in rounds(100):
        if predicate():
            return
        await pilot.pause(0.02)
    raise AssertionError("Expected keyboard state did not complete")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("command", ["Search All Mail", "Search Current Folder"])
async def test_bare_palette_mail_search_prompts_before_using_literal_query(
    tmp_path: Path, monkeypatch, size, command
):
    context = context_for(tmp_path)
    SettingsService(context.config_store).set("search.default_scope", "backups")
    app = GUCmailApp(context, gateway=FakeGateway())
    queries = []
    monkeypatch.setattr(app, "_search_mail", lambda query, _label: queries.append(query))
    try:
        async with app.run_test(size=size) as pilot:
            await wait(pilot, lambda: app.ui.online and app.ui.busy is None)
            await pilot.press("ctrl+k")
            await wait(pilot, lambda: isinstance(app.screen, PaletteScreen))
            await pilot.press(*command)
            await pilot.pause()
            assert app.screen._interpretation.invocation.command_id == (
                "search.search_all_mail"
                if command == "Search All Mail"
                else "search.search_current_folder"
            )
            await pilot.press("enter")
            await wait(pilot, lambda: isinstance(app.screen, FolderNameScreen))
            assert not queries
            prompt = app.screen.query_one("#folder-name-input", Input)
            assert prompt.value == "" and app.focused is prompt
            box = app.screen.query_one("#folder-name-box").region
            assert box.x >= 0 and box.right <= size[0]
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, FolderNameScreen) and not queries
            # These words are intentional message text once entered in the query prompt.
            await pilot.press(*command, "enter")
            await wait(pilot, lambda: len(queries) == 1)
            assert queries[0].text == command
            assert queries[0].folder == ("inbox" if command == "Search Current Folder" else None)
            assert queries[0].mailbox_scope == (command == "Search All Mail")
            assert len(app.screen_stack) == 1
    finally:
        context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["Search All Mail", "Search Current Folder"])
async def test_search_prompt_cancel_never_searches_or_changes_default(
    tmp_path: Path, monkeypatch, command
):
    context = context_for(tmp_path)
    SettingsService(context.config_store).set("search.default_scope", "local_index")
    app = GUCmailApp(context, gateway=FakeGateway())
    queries = []
    monkeypatch.setattr(app, "_search_mail", lambda query, _label: queries.append(query))
    try:
        async with app.run_test(size=(70, 20)) as pilot:
            await wait(pilot, lambda: app.ui.online and app.ui.busy is None)
            await pilot.press("ctrl+k", *command, "enter")
            await wait(pilot, lambda: isinstance(app.screen, FolderNameScreen))
            await pilot.press(*"Synthetic unsent query", "escape")
            await wait(pilot, lambda: len(app.screen_stack) == 1)
            assert not queries
            assert (
                SettingsService(context.config_store).value("search.default_scope").effective_value
                == "local_index"
            )
    finally:
        context.close()
