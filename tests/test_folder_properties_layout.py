"""Folder properties must keep dismissal available while long metadata scrolls."""

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Static

from gucmail.models import FolderInfo
from gucmail.tui.app import FolderDetailsScreen, GUCmailApp
from test_ui_session import context_for


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20), (50, 18)])
@pytest.mark.parametrize("long_metadata", [False, True])
async def test_folder_properties_keeps_close_visible_and_metadata_keyboard_reachable(
    tmp_path, size, long_metadata
):
    context = context_for(tmp_path)
    folder = FolderInfo(
        reference="fld_owned",
        parent_reference="fld_parent",
        name="Research/2026",
        path="/Mailbox/Archiv/Research\\/2026" + ("/Long component" * 50 if long_metadata else ""),
        ews_id="owned-identity" + ("x" * 300 if long_metadata else ""),
        change_key="current-key-tail",
        total_count=3,
        unread_count=2,
        child_count=1,
        folder_class="IPF.Note",
    )
    async with GUCmailApp(context, offline=True).run_test(size=size) as pilot:
        app = pilot.app
        await app.workers.wait_for_complete()
        app.push_screen(FolderDetailsScreen(folder))
        await pilot.pause()
        close = app.screen.query_one("#folder-details-close", Button)
        width, height = size
        assert 0 <= close.region.x < close.region.right <= width
        assert 0 <= close.region.y < close.region.bottom <= height
        assert app.screen.find_widget(close).clip.intersection(close.region) == close.region
        content = app.screen.query_one("#folder-details-content", Static)
        assert folder.path in str(content.content) and "current-key-tail" in str(content.content)
        scroll = app.screen.query_one("#folder-details-scroll", VerticalScroll)
        assert scroll.region.bottom <= close.region.y
        if scroll.max_scroll_y:
            for _ in range(15):
                if app.focused is scroll:
                    break
                await pilot.press("tab")
            assert app.focused is scroll
            await pilot.press("end")
            await app.animator.wait_for_idle()
            await pilot.pause()
            assert scroll.scroll_y == scroll.max_scroll_y
            # The final metadata and informational line remain accessible, not truncated.
            assert content.region.bottom <= scroll.region.bottom
        for _ in range(15):
            if app.focused is close:
                break
            await pilot.press("tab")
        assert (
            app.focused is close
            and app.screen.find_widget(close).clip.intersection(close.region) == close.region
        )
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.screen_stack) == 1
    context.close()
