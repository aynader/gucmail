from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from gucmail.tui.app import CompactHeader, GUCmailApp
from gucmail.tui.branding import BrandingPreferences
from test_tui import FakeGateway, context_for


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [80, 120])
@pytest.mark.parametrize("preset", ["builtin.guc-mail-minimal", "builtin.guc-original-mark"])
async def test_wordmark_and_connection_share_one_row(
    tmp_path: Path, width: int, preset: str
) -> None:
    app = GUCmailApp(context_for(tmp_path), gateway=FakeGateway())
    app.branding_preferences = BrandingPreferences(preset_id=preset)
    async with app.run_test(size=(width, 35)) as pilot:
        await pilot.pause()
        header = app.query_one(CompactHeader)
        title = app.query_one("#compact-title")
        status = app.query_one("#account-bar", Static)
        banner = app.query_one("#expanded-brand", Static)
        assert header.region.height == 1
        assert title.display and not banner.display
        assert title.region.y == status.region.y == header.region.y
        tabs = app.query_one("#workspace-tabs")
        assert tabs.region.y == header.region.bottom
        assert app.query_one("#mail-layout").region.y == tabs.region.bottom
