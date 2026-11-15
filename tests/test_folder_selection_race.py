from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.widgets import Tree

from gucmail.commands import CommandInvocation
from gucmail.models import FolderInfo
from gucmail.tui.app import GUCmailApp
from test_tui import context_for
from waiting import wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("operation", ["load", "restore"])
async def test_folder_cursor_sync_cannot_overwrite_newer_palette_navigation(
    tmp_path, size, operation
):
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        tree = app.query_one("#folder-tree", Tree)
        node_id = app._folder_root_node_id
        app._folder_load_tokens[node_id] = 1
        page = SimpleNamespace(
            items=tuple(
                FolderInfo(reference, reference.title(), reference, None, child_count=0)
                for reference in ("inbox", "sent", "archive")
            ),
            has_more=False,
            next_offset=None,
        )
        if operation == "restore":
            app._render_folder_children(1, node_id, None, page, False)
            await pilot.pause()
            app._folder_selection_to_restore = "inbox"
            app._restore_folder_selection(
                tree, tree.get_node_by_id(app._folder_nodes["inbox"]), "inbox"
            )
            assert app._folder_selection_to_restore is None
        else:
            app._render_folder_children(1, node_id, None, page, False)
        # Deliberately navigate before any queued tree messages are delivered.
        # Rendering a current-folder cursor must not enqueue a later navigation.
        handlers = app._palette_handlers()
        for command in ("mail.open_sent", "mail.open_archive"):
            handlers[command](CommandInvocation(command), app._palette_context())
        assert app.ui.folder == "archive"
        persisted = context.paths.config_file.read_bytes()
        await pilot.pause()
        assert app.ui.folder == "archive"
        assert app._folder_location_history == ["inbox", "sent", "archive"]
        assert context.paths.config_file.read_bytes() == persisted
        assert app._setting_value("mail.last_folder", None) == "archive"

        # Actual tree activation still navigates and records the new location.
        tree.select_node(tree.get_node_by_id(app._folder_nodes["sent"]))
        await wait_until(pilot, lambda: app.ui.folder == "sent", interval=0.02)
        assert app.ui.folder == "sent"
        assert app._folder_location_history == ["inbox", "sent", "archive", "sent"]
        assert app._setting_value("mail.last_folder", None) == "sent"
        assert context._gateway is None


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_loaded_folder_cursor_reflects_current_location_without_navigation(tmp_path, size):
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        persisted = context.paths.config_file.read_bytes()
        tree = app.query_one("#folder-tree", Tree)
        node_id = app._folder_root_node_id
        app._folder_load_tokens[node_id] = 1
        app._render_folder_children(
            1,
            node_id,
            None,
            SimpleNamespace(
                items=(FolderInfo("inbox", "Inbox", "Inbox", None, child_count=0),),
                has_more=False,
                next_offset=None,
            ),
            False,
        )
        await wait_until(
            pilot,
            lambda: (
                tree.cursor_node is not None
                and isinstance(tree.cursor_node.data, dict)
                and tree.cursor_node.data.get("reference") == "inbox"
            ),
            interval=0.02,
        )
        assert tree.cursor_node.data["reference"] == "inbox"
        assert app.ui.folder == "inbox" and app._folder_location_history == ["inbox"]
        assert context.paths.config_file.read_bytes() == persisted
        assert context._gateway is None
