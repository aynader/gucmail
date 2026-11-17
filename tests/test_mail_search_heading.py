"""Search scope remains visible while the underlying folder selection is preserved."""

from pathlib import Path

import pytest
from textual.widgets import Static

from gucmail.commands import CommandContext
from gucmail.palette import PaletteEngine, universal_palette_registry
from gucmail.settings import SettingsService
from gucmail.tui.app import GUCmailApp, MailQueryScreen, PaletteScreen
from test_mail_search_prompt import wait
from test_mailbox_search_scope import mailbox
from test_tui import FakeGateway
from test_ui_session import context_for


class SyntheticMailboxGateway(FakeGateway):
    def __init__(self):
        self.core, self.account = mailbox()
        self.searches = []
        for folder in (
            self.account.inbox,
            *self.account.inbox.children,
            self.account.msg_folder_root.children[1],
        ):
            for item in folder.messages:
                item.subject = f"{folder.name} · Synthetic scope term"

    def list_folders(self, parent_reference=None, limit=50, offset=0):
        return self.core.list_folders(parent_reference, limit=limit, offset=offset)

    def list_messages(self, *, folder, limit, offset=0, order_by="-datetime_received"):
        return self.core.list_messages(folder, limit=limit, offset=offset, order_by=order_by)

    def search_messages_planned(self, query, plan, *, limit, offset):
        self.searches.append(
            {
                "text": query.text,
                "folder": query.folder,
                "mailbox_scope": query.mailbox_scope,
                "limit": limit,
                "offset": offset,
            }
        )
        return self.core.search_messages_planned(query, plan, limit=limit, offset=offset)

    def search_messages_expression_planned(self, query, plan, *, limit, offset):
        return self.core.search_messages_expression_planned(query, plan, limit=limit, offset=offset)

    def show_message(self, *_args, **_kwargs):
        raise AssertionError("Search workflow fetched or marked message detail")

    def close(self):
        self.core.close()


@pytest.mark.parametrize("command_id", ["search.search_current_folder", "search.search_all_mail"])
@pytest.mark.parametrize("variant", ["name", "alias", "misspelling", "whitespace"])
def test_canonical_mail_search_interpretation_has_no_literal_command_query(command_id, variant):
    registry = universal_palette_registry()
    command = registry.get(command_id)
    text = (
        command.name
        if variant == "name"
        else command.aliases[-1]
        if variant == "alias"
        else command.misspellings[0]
        if variant == "misspelling"
        else "  " + command.name.upper().replace(" ", "   ") + "  "
    )
    result = PaletteEngine(registry).interpret(text, context=CommandContext(online=True))
    assert result.invocation.command_id == command_id
    assert result.invocation.get("query") is None
    assert result.invocation.original_text.strip() == text.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command, expected_count, expected_heading",
    [("Search Current Folder", 1, "inbox search"), ("Search All Mail", 3, "All mail search")],
)
async def test_search_heading_survives_resize_filter_and_resets_on_folder_refresh(
    tmp_path: Path, command, expected_count, expected_heading
):
    context = context_for(tmp_path)
    SettingsService(context.config_store).set("search.default_scope", "backups")
    gateway = SyntheticMailboxGateway()
    app = GUCmailApp(context, gateway=gateway)
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await wait(pilot, lambda: app.ui.online and app.ui.busy is None)
            await pilot.press("ctrl+k", *command)
            await wait(pilot, lambda: isinstance(app.screen, PaletteScreen))
            assert "Query:" not in str(
                app.screen.query_one("#palette-explanation", Static).render()
            )
            assert "prompt" in str(app.screen.query_one("#palette-explanation", Static).render())
            await pilot.press("enter")
            await wait(pilot, lambda: isinstance(app.screen, MailQueryScreen))
            await pilot.press(*"synthetic", "enter")
            await wait(
                pilot, lambda: len(gateway.searches) == 1 and len(app._messages) == expected_count
            )
            heading = app.query_one("#message-heading", Static)
            assert expected_heading in str(heading.render()) and app.ui.folder == "inbox"
            await pilot.resize_terminal(70, 20)
            await pilot.pause()
            assert expected_heading in str(heading.render())
            await pilot.press("/", *"Synthetic")
            await pilot.pause()
            assert expected_heading in str(heading.render())
            await pilot.press("escape", "f5")
            await wait(pilot, lambda: app.ui.busy is None and len(app._messages) == 1)
            await pilot.pause()
            assert "Inbox · 1 message" in str(heading.render())
            assert "search" not in str(heading.render())
            assert app._message_detail is None
    finally:
        context.close()
