"""A bare expert command opens a prompt before running any mailbox expression."""

import json

import pytest
from textual.widgets import Input

from gucmail.palette import universal_palette_registry
from gucmail.services.ui_session import SESSION_KEY, SESSION_SCOPE
from gucmail.tui.app import FolderNameScreen, GUCmailApp, PaletteScreen
from test_mail_search_heading import SyntheticMailboxGateway
from test_mail_search_prompt import wait
from test_ui_session import context_for, tab_to


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant,size",
    [
        ("name", (120, 35)),
        ("name", (80, 24)),
        ("name", (70, 20)),
        ("alias", (80, 24)),
        ("misspelling", (80, 24)),
        ("whitespace", (80, 24)),
    ],
)
async def test_bare_advanced_command_prompts_and_preserves_literal_expression(
    tmp_path, variant, size
):
    context = context_for(tmp_path)
    gateway = SyntheticMailboxGateway()
    commands = []
    original = gateway.search_messages_expression_planned

    def record(*args, **kwargs):
        commands.append(args)
        return original(*args, **kwargs)

    gateway.search_messages_expression_planned = record
    app = GUCmailApp(context, gateway=gateway)
    command = universal_palette_registry().get("search.advanced_search")
    text = (
        command.name
        if variant == "name"
        else command.aliases[-1]
        if variant == "alias"
        else command.misspellings[0]
        if variant == "misspelling"
        else "  " + command.name.upper().replace(" ", "   ") + "  "
    )
    try:
        async with app.run_test(size=size) as pilot:
            await wait(pilot, lambda: app.ui.online and app.ui.busy is None)
            await pilot.press("ctrl+k", *text)
            await wait(pilot, lambda: isinstance(app.screen, PaletteScreen))
            assert app.screen._interpretation.invocation.command_id == command.command_id
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, FolderNameScreen)
            assert app.screen.dialog_title == "Advanced mailbox search"
            assert not commands
            prompt = app.screen.query_one("#folder-name-input", Input)
            assert prompt.value == "" and app.focused is prompt
            box = app.screen.query_one("#folder-name-box").region
            assert box.x >= 0 and box.right <= size[0]
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, FolderNameScreen) and not commands
            expression = 'folder:"/Mailbox/Gesendet" AND subject:"Synthetic"'
            await pilot.press(*expression, "enter")
            await wait(
                pilot,
                lambda: app._last_mail_search_scope == "folder" and len(app.screen_stack) == 1,
            )
            assert len(commands) == 1 and len(app._messages) == 1
            assert app._last_mail_search_folder == "/Mailbox/Gesendet"
            await tab_to(pilot, app, "message-table")
            await pilot.press("q")
        record = context.state.get_ui_preference(SESSION_KEY, scope=SESSION_SCOPE)
        assert record.value["mail_search_scope"] == "folder"
        assert record.value["mail_search_folder"] == "/Mailbox/Gesendet"
        assert "Synthetic" not in json.dumps(record.value)
    finally:
        context.close()
