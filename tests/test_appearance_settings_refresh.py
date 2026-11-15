"""Appearance writes refresh their owning Settings view without losing staged edits."""

from pathlib import Path

import pytest
from textual.widgets import Select, Static

from gucmail.commands import CommandContext, CommandInvocation
from gucmail.tui.app import GUCmailApp
from gucmail.tui.appearance_screen import ThemeBrowserScreen
from gucmail.tui.branding_screen import BrandingBrowserScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_tui_visual_regressions import _context
from waiting import wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("route", ["branding", "themes", "toggle"])
async def test_appearance_write_refreshes_clean_settings_and_preserves_staging(
    tmp_path: Path, staged: bool, route: str
) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            app._open_area("settings")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            settings = app.screen
            assert isinstance(settings, SettingsScreen)
            setting_id, target = {
                "branding": ("appearance.branding", "builtin.guc-mail-standard"),
                "themes": ("appearance.theme", "builtin.default-light"),
                "toggle": ("appearance.banner_visible", False),
            }[route]
            # Mount schedules the first catalog load after refresh. Wait for its
            # delivered result, rather than an earlier empty worker snapshot.
            await wait_until(pilot, lambda: setting_id in settings._settings_by_id, interval=0.05)
            initial = settings._settings_by_id[setting_id].current_value
            assert initial != target
            if staged:
                assert settings.action_select_setting("appearance.density")
                settings.query_one("#settings-choice", Select).value = "compact"
                settings._validate_editor(preview=False)
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert settings._staged == {"appearance.density": "compact"}
            if route == "toggle":
                app._palette_toggle_banner(
                    CommandInvocation("appearance.toggle_banner"), CommandContext(offline_mode=True)
                )
            else:
                app._open_area(route)
                await pilot.pause()
                browser = app.screen
                if route == "branding":
                    assert isinstance(browser, BrandingBrowserScreen)
                    app.branding_browser_backend.session.preview(preset_id=target)
                else:
                    assert isinstance(browser, ThemeBrowserScreen)
                    browser._refresh_rows(preserve=str(target))
                browser.action_apply()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._settings_service().value(setting_id).effective_value == target
            if route != "toggle":
                browser.action_close()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is settings
            if staged:
                assert settings._staged == {"appearance.density": "compact"}
                message = str(settings.query_one("#area-state", Static).content)
                assert "Appearance preferences changed" in message
                assert "Cancel and Refresh" in message
            else:
                assert settings._settings_by_id[setting_id].current_value == target
    finally:
        context.close()
