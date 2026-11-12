"""Names from persistent records must remain literal in trees and dropdowns."""

import asyncio

import pytest
from qa.synthetic.mailbox import LocalMailbox
from rich.text import Text
from textual.widgets import Select, Tree

from gucmail.models import FolderInfo
from gucmail.notification_policies import MonitoringProfile
from gucmail.services.notification_configuration import NotificationConfigurationService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.literal_select import LiteralSelect
from test_tui import context_for


async def ready(pilot, predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await pilot.pause(0.02)
    await pilot.pause()


@pytest.mark.asyncio
async def test_persisted_notification_profile_name_is_literal_in_open_dropdown(tmp_path):
    context = context_for(tmp_path)
    name = "Synthetic [/red] [bold]学生[/bold]"
    service = NotificationConfigurationService(context.state)
    service.profiles.save_profile(MonitoringProfile("hostile", name, ("Inbox",)))
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 36)) as pilot:
        app._open_area("notifications")
        screen = app.screen
        await ready(pilot, lambda: "hostile" in screen._profiles and not screen._operation_active)
        selected = screen.query_one("#notification-profile-select", Select)
        selected.focus()
        await pilot.press("enter")
        await pilot.pause()
        labels = [str(label) for label, value in selected._options if value == "hostile"]
        assert labels == [name]
        assert "[/red]" in app.export_screenshot()
        await pilot.press("escape")
    context.close()


@pytest.mark.asyncio
async def test_choice_replacement_sanitizes_labels_preserves_values_and_explicit_styles(tmp_path):
    context = context_for(tmp_path)
    styled = Text("Trusted option", style="bold")
    original = "\x1b]0;injected\x07[/red] literal"
    choice = LiteralSelect([(original, "initial"), (styled, "styled")], allow_blank=False)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await app.screen.mount(choice)
        await pilot.pause()
        assert str(choice._options[0][0]) == "[/red] literal"
        assert choice._options[1][0] is styled and styled.style == "bold"
        assert choice.value == "initial"
        choice.set_options(
            (label, value) for label, value in [("[bold] replacement", "replacement")]
        )
        choice.focus()
        await pilot.press("enter")
        assert str(choice._options[0][0]) == "[bold] replacement"
        assert not choice._options[0][0].spans
        assert choice.value == "replacement"
        await pilot.press("escape")
    assert original.startswith("\x1b")  # Rendering does not modify domain/source strings.
    context.close()


@pytest.mark.asyncio
async def test_sql_folder_name_keeps_literal_brackets_in_actual_navigation_tree(tmp_path):
    context = context_for(tmp_path / "profile")
    gateway = LocalMailbox(tmp_path / "mailbox.sqlite")
    name = "Folder [red]学生"
    folder = FolderInfo("fld-hostile", name, "/Synthetic", None, child_count=0)
    gateway.store("folder", folder)
    context._gateway = gateway
    app = GUCmailApp(context, gateway=gateway)
    async with app.run_test(size=(160, 48)) as pilot:
        await ready(pilot, lambda: "fld-hostile" in app._folder_nodes)
        tree = app.query_one("#folder-tree", Tree)
        node = tree.get_node_by_id(app._folder_nodes["fld-hostile"])
        assert name in node.label.plain
        assert not node.label.spans
    context.close()
