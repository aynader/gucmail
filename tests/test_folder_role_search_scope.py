"""Role-prefixed paths and preset searches keep their actual mailbox scope."""

import json

import pytest
from exchangelib.folders import Folder
from exchangelib.properties import ParentFolderId
from exchangelib.services import FindFolder

import test_mailbox_search_library_boundaries as library_boundaries
from gucmail.palette import universal_palette_registry
from gucmail.services.ui_session import SESSION_KEY, SESSION_SCOPE
from gucmail.settings import SettingsService
from gucmail.tui.app import GUCmailApp, MailQueryScreen, PaletteScreen
from test_mail_search_heading import SyntheticMailboxGateway
from test_mail_search_prompt import wait
from test_mailbox_search_scope import folder, mailbox
from test_tui import reloaded_context
from test_ui_session import context_for, tab_to


@pytest.mark.parametrize("prefix", ["inbox", "Inbox", "IN BOX"])
def test_role_prefixed_relative_path_resolves_under_localized_inbox(prefix):
    gateway, account = mailbox()
    assert account.inbox.name == "Posteingang"
    selected = gateway.get_folder(prefix + "/Onderwijs\\/2026")
    assert selected.ews_id == account.inbox.children[0].id


def test_absolute_display_path_disambiguates_custom_folder_named_like_role():
    gateway, account = mailbox()
    notes = folder("literal-notes", "Literal notes")
    literal = folder("literal-inbox", "Inbox", children=(notes,))
    account.msg_folder_root.children += (literal,)
    literal.parent_folder_id = type(account.inbox.parent_folder_id)(id=account.msg_folder_root.id)
    assert gateway.get_folder("/Mailbox/Inbox/Literal notes").ews_id == notes.id
    assert gateway.get_folder("inbox/Onderwijs\\/2026").ews_id == account.inbox.children[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("command_id", ["mail.open_unread", "mail.open_flagged"])
async def test_keyboard_preset_scope_replaces_prior_all_mail_and_reopens_empty_after_restart(
    tmp_path, command_id
):
    context = context_for(tmp_path)
    SettingsService(context.config_store).set("search.default_scope", "backups")
    gateway = SyntheticMailboxGateway()
    app = GUCmailApp(context, gateway=gateway)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await wait(pilot, lambda: app.ui.online and app.ui.busy is None)
            await pilot.press("ctrl+k", *"Search All Mail", "enter")
            await wait(pilot, lambda: isinstance(app.screen, MailQueryScreen))
            await pilot.press(*"synthetic", "enter")
            await wait(pilot, lambda: len(gateway.searches) == 1 and app.ui.busy is None)
            assert app._last_mail_search_scope == "mailbox"
            command = universal_palette_registry().get(command_id)
            await pilot.press("ctrl+k", *command.name)
            await wait(pilot, lambda: isinstance(app.screen, PaletteScreen))
            assert app.screen._interpretation.invocation.command_id == command_id
            await pilot.press("enter")
            await wait(pilot, lambda: len(gateway.searches) == 2 and app.ui.busy is None)
            assert app._last_mail_search_scope == "folder"
            assert app._last_mail_search_folder == context.config.default_folder
            assert app._message_detail is None
            await tab_to(pilot, app, "message-table")
            await pilot.press("q")
        record = context.state.get_ui_preference(SESSION_KEY, scope=SESSION_SCOPE)
        assert record.value["mail_search_scope"] == "folder"
        assert record.value["mail_search_folder"] == context.config.default_folder
        assert "synthetic" not in json.dumps(record.value).casefold()
        second_gateway = SyntheticMailboxGateway()
        second = GUCmailApp(reloaded_context(context), gateway=second_gateway)
        async with second.run_test(size=(80, 24)) as pilot:
            await wait(pilot, lambda: second.ui.online and second.ui.busy is None)
            assert not second_gateway.searches
            await pilot.press("ctrl+k", *"Search Mail", "enter")
            await wait(pilot, lambda: isinstance(second.screen, MailQueryScreen))
            assert second.screen.dialog_title == "Search selected folder"
            assert second.screen.query_one("#folder-name-input").value == ""
            assert not second_gateway.searches
    finally:
        context.close()


installed_mailbox = library_boundaries.installed_mailbox


def test_real_library_role_prefix_queries_only_the_distinguished_parent(
    installed_mailbox, monkeypatch
):
    gateway, account, children, _data, _requests, _details = installed_mailbox
    child = Folder(
        root=account.root,
        id="owned-course",
        name="Courses",
        folder_class="IPF.Note",
        parent_folder_id=ParentFolderId(id=children[0].id),
    )
    calls = []

    def find(service, **kwargs):
        payload = service.get_payload(
            **{k: v for k, v in kwargs.items() if k != "max_items"}, page_size=service.page_size
        )
        calls.append((kwargs, payload))
        return iter([child]) if "Courses" in str(kwargs["restriction"].q) else iter([])

    monkeypatch.setattr(FindFolder, "call", find)
    selected = gateway.get_folder("inbox/Courses")
    assert selected.ews_id == child.id
    assert len(calls) == 1
    kwargs, payload = calls[0]
    assert kwargs["depth"] == "Shallow" and kwargs["max_items"] == 2
    parent = payload.find(
        "{http://schemas.microsoft.com/exchange/services/2006/messages}ParentFolderIds"
    )[0]
    assert parent.get("Id") == children[0].id
    assert "Courses" in str(kwargs["restriction"].q)
