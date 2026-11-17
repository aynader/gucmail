from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Tree

from gucmail.models import FolderInfo
from gucmail.tui.app import GUCmailApp
from test_tui import FakeGateway, context_for
from waiting import rounds


class ManyFoldersGateway(FakeGateway):
    def list_folders(
        self, parent_reference: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[FolderInfo]:
        if parent_reference is not None:
            return []
        folders = [
            FolderInfo(
                reference=f"folder-{i}",
                name=f"Folder {i}",
                path=f"/Folder {i}",
                parent_reference=None,
            )
            for i in range(20)
        ]
        return folders[offset : offset + limit]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 24), (80, 24)])
async def test_tools_remain_reachable_above_long_folder_tree_and_filter_works(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=ManyFoldersGateway())
    async with app.run_test(size=size) as pilot:
        for _ in rounds(100):
            await pilot.pause(0.05)
            tree = app.query_one("#folder-tree", Tree)
            table = app.query_one("#message-table", DataTable)
            if (
                len(tree.root.children) == 3
                and len(tree.root.children[-1].children) == 20
                and table.row_count
            ):
                break

        views, tools, folders = tree.root.children
        assert str(views.label) == "Views"
        assert [str(child.label) for child in views.children] == [
            "Inbox",
            "Unread",
            "Flagged",
        ]
        assert str(tools.label) == "Tools"
        assert str(folders.label) == "Folders"
        assert len(folders.children) == 20
        assert not tools.is_expanded
        assert app.query_one("#message-filter", Input).region.height == 2

        tree.move_cursor(tools)
        tree.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert tools.is_expanded
        assert any(str(child.label) == "Settings" for child in tools.children)
        await pilot.press("enter")
        await pilot.pause()
        assert not tools.is_expanded

        message_filter = app.query_one("#message-filter", Input)
        message_filter.value = "not present"
        await pilot.pause()
        assert table.row_count == 0
        message_filter.value = "Synthetic"
        await pilot.pause()
        assert table.row_count == 1
        message_filter.value = ""
        message_filter.focus()
        await pilot.press("s", "y", "n")
        assert message_filter.value == "syn"
        assert table.row_count == 1
        assert message_filter.region.height == 2
