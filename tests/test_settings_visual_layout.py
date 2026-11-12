from pathlib import Path

import pytest
from textual.widgets import Button, Input, Select, Static

from gucmail.tui.app import GUCmailApp
from gucmail.tui.settings_form import SettingRow, SettingsForm
from gucmail.tui.settings_screen import SettingsScreen
from test_tui import context_for


async def ready(pilot, screen: SettingsScreen) -> None:
    for _ in range(100):
        await pilot.pause(0.03)
        if (
            screen.query_one("#settings-form", SettingsForm).row_count
            and not screen._operation_active
            and not screen._render_pending
        ):
            return
    pytest.fail("Settings form did not load")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(70, 20), (80, 24), (140, 42)])
async def test_reference_layout_tabs_rows_and_footer(tmp_path: Path, size: tuple[int, int]) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await ready(pilot, screen)
        assert screen._active_tab == "Mail"
        rows = list(screen.query(SettingRow))
        assert [row.label for row in rows] == [
            "Default folder",
            "Page size",
            "Fill visible area",
            "Reading pane",
            "Mark read on open",
            "Open messages",
        ]
        assert rows[2].has_class("selected")
        assert rows[3].has_class("group-start")
        assert [row.number for row in rows] == list(range(1, 7))
        assert not screen.query_one("#settings-editor-overlay").display
        for selector in ("#settings-apply", "#settings-cancel", ".area-close"):
            button = screen.query_one(selector, Button)
            assert button.content_size.height >= 1
            assert button.region.bottom <= size[1]
        assert await pilot.click("#settings-tab-appearance")
        await ready(pilot, screen)
        assert screen._active_tab == "Appearance"
        assert screen.query_one("#settings-tab-appearance").has_class("active")
        assert any(row.setting_id == "appearance.theme" for row in screen.query(SettingRow))


@pytest.mark.asyncio
async def test_keyboard_edit_stage_save_and_discard(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        app._open_area("settings")
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await ready(pilot, screen)
        before = context.paths.config_file.read_bytes()
        await pilot.press("enter")
        assert screen._editor_open
        screen.query_one("#settings-choice", Select).value = "off"
        assert await pilot.click("#settings-stage")
        await ready(pilot, screen)
        assert screen._staged == {"mail.fill_viewport": False}
        assert context.paths.config_file.read_bytes() == before
        await pilot.press("ctrl+z")
        await ready(pilot, screen)
        assert not screen._staged
        assert context.paths.config_file.read_bytes() == before
        assert screen.action_select_setting("mail.page_size")
        await pilot.pause()
        screen.query_one("#settings-editor", Input).value = "42"
        await pilot.press("ctrl+s")
        await ready(pilot, screen)
        assert app._settings_service().value("mail.page_size").effective_value == 42
        assert not screen._staged


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(70, 20), (80, 24)])
async def test_validation_focus_trap_and_escape_from_editor(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await ready(pilot, screen)
        assert screen.action_select_setting("mail.page_size")
        await pilot.pause()
        stage = screen.query_one("#settings-stage", Button)
        assert stage.region.bottom <= size[1]
        assert stage.content_size.height >= 1
        screen.query_one("#settings-editor", Input).value = "999"
        await pilot.press("enter")
        await pilot.pause()
        assert not screen._staged
        assert screen.query_one("#settings-edit-status", Static).display
        assert screen._editor_open
        for _ in range(8):
            await pilot.press("tab")
            assert screen.focused in screen.query("#settings-editor-dialog *")
        await pilot.press("escape")
        assert app.screen is screen
        assert not screen._editor_open


@pytest.mark.asyncio
async def test_search_and_advanced_keep_entire_catalog_accessible(tmp_path: Path) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(100, 35)) as pilot:
        app._open_area("settings")
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await ready(pilot, screen)
        assert await pilot.click("#settings-tab-advanced")
        await ready(pilot, screen)
        assert {item.setting_id for item in screen._visible_settings} == set(screen._settings_by_id)
        await pilot.press("ctrl+f")
        screen.query_one("#settings-search", Input).value = "monitoring.start_at_login"
        await ready(pilot, screen)
        assert screen._selected_setting_id == "monitoring.start_at_login"
        assert screen.action_select_setting("monitoring.start_at_login")
        await pilot.pause()
        assert screen.query_one("#settings-managed", Button).display


@pytest.mark.asyncio
async def test_editor_initial_focus_does_not_steal_an_explicit_control_focus(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(70, 20)) as pilot:
        app._open_area("settings")
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await ready(pilot, screen)
        assert screen.action_select_setting("appearance.density")
        screen.query_one("#settings-choice", Select).value = "compact"
        stage = screen.query_one("#settings-stage", Button)
        stage.focus()
        await pilot.pause()
        assert screen.focused is stage
        await pilot.press("enter")
        await ready(pilot, screen)
        assert screen._staged == {"appearance.density": "compact"}
