"""Restore a bounded source choice, never a query or a running search."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from gucmail.commands import CommandInvocation
from gucmail.services.ui_session import SESSION_KEY, SESSION_SCOPE, MailSession, MailSessionStore
from gucmail.settings import SettingsService
from gucmail.state import StateStore
from gucmail.tui.app import GUCmailApp, MailQueryScreen, PaletteScreen
from test_mail_search_heading import SyntheticMailboxGateway
from test_mail_search_prompt import wait
from test_tui import reloaded_context
from test_ui_session import context_for, tab_to


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize(
    "command, scope", [("Search Current Folder", "current_folder"), ("Search All Mail", "mailbox")]
)
async def test_normal_mail_scope_survives_restart_and_generic_search_reopens_empty(
    tmp_path: Path, size, command, scope
):
    context = context_for(tmp_path)
    SettingsService(context.config_store).set("search.default_scope", "backups")
    gateway = SyntheticMailboxGateway()
    first = GUCmailApp(context, gateway=gateway)
    try:
        async with first.run_test(size=size) as pilot:
            await wait(pilot, lambda: first.ui.online and first.ui.busy is None)
            await pilot.press("ctrl+k", *command, "enter")
            await wait(pilot, lambda: isinstance(first.screen, MailQueryScreen))
            await pilot.press(*"synthetic", "enter")
            await wait(pilot, lambda: len(gateway.searches) == 1 and len(first.screen_stack) == 1)
            await tab_to(pilot, first, "message-table")
            await pilot.press("q")
        record = context.state.get_ui_preference(SESSION_KEY, scope=SESSION_SCOPE)
        assert record.value["mail_search_scope"] == scope
        assert record.value["mail_search_folder"] is None
        assert record.value["search_default_scope"] == "backups"
        assert "synthetic" not in json.dumps(record.value).casefold()
        second_gateway = SyntheticMailboxGateway()
        restarted = GUCmailApp(reloaded_context(context), gateway=second_gateway)
        async with restarted.run_test(size=size) as pilot:
            await wait(pilot, lambda: restarted.ui.online and restarted.ui.busy is None)
            assert len(restarted.screen_stack) == 1 and not second_gateway.searches
            assert restarted._message_detail is None
            await pilot.press("ctrl+k", *"Search Mail")
            await wait(pilot, lambda: isinstance(restarted.screen, PaletteScreen))
            await pilot.press("enter")
            await wait(pilot, lambda: isinstance(restarted.screen, MailQueryScreen))
            assert restarted.screen.dialog_title == (
                "Search all mail" if scope == "mailbox" else "Search current folder"
            )
            assert restarted.screen.query_one("#folder-name-input").value == ""
            assert not second_gateway.searches
            await pilot.press(*"synthetic", "enter")
            await wait(pilot, lambda: len(second_gateway.searches) == 1)
            assert second_gateway.searches[0]["mailbox_scope"] == (scope == "mailbox")
            assert second_gateway.searches[0]["folder"] == (
                "inbox" if scope == "current_folder" else None
            )
            assert (
                SettingsService(context.config_store).value("search.default_scope").effective_value
                == "backups"
            )
    finally:
        context.close()


def test_legacy_session_shape_is_preserved_and_upgraded_only_on_a_valid_change(tmp_path):
    state = StateStore(tmp_path / "state.db")
    legacy = asdict(MailSession(folder="inbox", message_row=7, search_scope="attachments"))
    for field in ("mail_search_scope", "mail_search_folder", "search_default_scope"):
        legacy.pop(field, None)
    original = state.put_ui_preference(SESSION_KEY, legacy, scope=SESSION_SCOPE)
    store = MailSessionStore(state)
    assert store.load().message_row == 7
    assert state.get_ui_preference(SESSION_KEY, scope=SESSION_SCOPE).revision == original.revision
    changed = MailSession(
        folder="inbox",
        message_row=7,
        search_scope="attachments",
        mail_search_scope="folder",
        mail_search_folder="sent",
        search_default_scope="mailbox",
    )
    assert store.save(changed)
    assert MailSessionStore(state).load() == changed


@pytest.mark.parametrize(
    "change",
    [
        {"mail_search_scope": "body"},
        {"mail_search_scope": []},
        {
            "mail_search_scope": "mailbox",
            "mail_search_folder": "sent",
            "search_default_scope": "mailbox",
        },
        {
            "mail_search_scope": "folder",
            "mail_search_folder": None,
            "search_default_scope": "mailbox",
        },
        {
            "mail_search_scope": "folder",
            "mail_search_folder": "sent\x1b[31m",
            "search_default_scope": "mailbox",
        },
        {
            "mail_search_scope": "folder",
            "mail_search_folder": "x" * 1025,
            "search_default_scope": "mailbox",
        },
        {"mail_search_scope": "mailbox", "search_default_scope": "https://example.invalid"},
        {"mail_search_scope": "mailbox", "search_default_scope": True},
        {"mail_search_scope": "mailbox", "search_default_scope": None},
        {"mail_search_scope": None, "mail_search_folder": "sent"},
    ],
)
def test_session_rejects_unsafe_or_inconsistent_search_scope_extensions(change):
    assert MailSession.parse({**asdict(MailSession()), **change}) is None


@pytest.mark.asyncio
async def test_changed_default_source_supersedes_previous_session_scope(tmp_path):
    context = context_for(tmp_path)
    store = MailSessionStore(context.state)
    store.load()
    assert store.save(
        MailSession(mail_search_scope="current_folder", search_default_scope="mailbox")
    )
    SettingsService(context.config_store).set("search.default_scope", "backups")
    app = GUCmailApp(context, offline=True)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._palette_search(
                CommandInvocation.from_mapping("search.mail", {}), app._palette_context()
            )
            await wait(
                pilot,
                lambda: (
                    app.screen.__class__.__name__ == "LocalSearchScreen"
                    and bool(app.screen.query("#local-search-query"))
                ),
            )
            assert app.screen.query_one("#local-search-query").value == ""
            assert app.gateway is None
    finally:
        context.close()


@pytest.mark.asyncio
async def test_selected_folder_scope_reopens_empty_after_restart_and_keeps_literal_text(tmp_path):
    context = context_for(tmp_path)
    first_gateway = SyntheticMailboxGateway()
    first = GUCmailApp(context, gateway=first_gateway)
    try:
        async with first.run_test(size=(80, 24)) as pilot:
            await wait(pilot, lambda: first.ui.online and first.ui.busy is None)
            first._palette_search(
                CommandInvocation.from_mapping(
                    "search.mail",
                    {
                        "query": "synthetic",
                        "folder": "/Mailbox/Gesendet",
                        "scope": "folder",
                        "_preserve_query": True,
                    },
                ),
                first._palette_context(),
            )
            await wait(pilot, lambda: len(first_gateway.searches) == 1)
            await tab_to(pilot, first, "message-table")
            await pilot.press("q")
        record = context.state.get_ui_preference(SESSION_KEY, scope=SESSION_SCOPE)
        assert record.value["mail_search_scope"] == "folder"
        assert record.value["mail_search_folder"] == "/Mailbox/Gesendet"
        second_gateway = SyntheticMailboxGateway()
        restarted = GUCmailApp(reloaded_context(context), gateway=second_gateway)
        async with restarted.run_test(size=(80, 24)) as pilot:
            await wait(pilot, lambda: restarted.ui.online and restarted.ui.busy is None)
            await pilot.press("ctrl+k", *"Search Mail", "enter")
            await wait(pilot, lambda: isinstance(restarted.screen, MailQueryScreen))
            assert restarted.screen.dialog_title == "Search selected folder"
            assert restarted.screen.query_one("#folder-name-input").value == ""
            assert not second_gateway.searches
            await pilot.press(*"synthetic", "enter")
            await wait(pilot, lambda: len(second_gateway.searches) == 1)
            assert second_gateway.searches[0]["text"] == "synthetic"
            assert second_gateway.searches[0]["folder"] == "/Mailbox/Gesendet"
            assert len(restarted._messages) == 1
    finally:
        context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression, expected_scope, expected_folder",
    [
        ('scope:mailbox AND subject:"Synthetic"', "mailbox", None),
        ('folder:"/Mailbox/Gesendet" AND subject:"Synthetic"', "folder", "/Mailbox/Gesendet"),
    ],
)
async def test_expert_and_saved_expression_execution_remembers_actual_scope_only(
    tmp_path, expression, expected_scope, expected_folder
):
    context = context_for(tmp_path)
    gateway = SyntheticMailboxGateway()
    app = GUCmailApp(context, gateway=gateway)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await wait(pilot, lambda: app.ui.online and app.ui.busy is None)
            app._search_expression(
                expression,
                "Synthetic expert",
                payload={"query": expression},
                source="palette",
                name=None,
            )
            await wait(pilot, lambda: app._last_mail_search_scope == expected_scope)
            assert app._last_mail_search_folder == expected_folder
            await tab_to(pilot, app, "message-table")
            await pilot.press("q")
        record = context.state.get_ui_preference(SESSION_KEY, scope=SESSION_SCOPE)
        assert record.value["mail_search_scope"] == expected_scope
        assert record.value["mail_search_folder"] == expected_folder
        assert "query" not in record.value and "Synthetic" not in json.dumps(record.value)
    finally:
        context.close()
