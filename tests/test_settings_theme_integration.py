"""Settings shares runtime semantic colors without losing staged input or focus."""

from dataclasses import replace
from pathlib import Path

import pytest
from rich.text import Text
from textual.color import Color
from textual.widgets import Button, Input, Label, Select

from gucmail.state import StateStore
from gucmail.tui.app import AreaScreen, GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.settings_form import SettingRow
from gucmail.tui.settings_screen import SettingsScreen
from test_settings_visual_layout import ready
from test_tui import context_for
from waiting import wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [False, True])
async def test_settings_back_reviews_dirty_changes_and_can_keep_editing(tmp_path, preview):
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 36)) as pilot:
        app._open_area("settings")
        screen = app.screen
        await ready(pilot, screen)
        original_theme = app.active_theme.id
        assert screen.action_select_setting("mail.page_size")
        await pilot.pause()
        screen.query_one("#settings-editor", Input).value = "37"
        await pilot.click("#settings-stage")
        await ready(pilot, screen)
        assert screen._staged["mail.page_size"] == 37
        if preview:
            assert screen.action_select_setting("appearance.theme")
            await pilot.pause()
            screen.query_one("#settings-choice", Select).value = "builtin.default-light"
            await pilot.click("#settings-preview")
            await ready(pilot, screen)
            assert app.active_theme.id == "builtin.default-light"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen), (
            "Back must not silently lose staged edits"
        )
        await pilot.press("n")
        await pilot.pause()
        assert app.screen is screen and screen._staged["mail.page_size"] == 37
        if preview:
            assert app.active_theme.id == "builtin.default-light"
        await pilot.press("escape", "y")
        await wait_until(pilot, lambda: app.screen is not screen, interval=0.01, pause_first=True)
        assert len(app.screen_stack) == 1
        assert app.active_theme.id == original_theme
        from gucmail.settings import SettingsService

        assert SettingsService(context.config_store).value("mail.page_size").effective_value == 25
    context.close()


def assert_palette(app: GUCmailApp, screen: SettingsScreen) -> None:
    palette = app._runtime_theme.palette
    assert screen.has_class("frontend")
    assert screen.query_one("#settings-search", Input).content_size.height == 1
    tab = screen.query_one("#settings-tabs Button.active", Button)
    assert tab.styles.border_bottom == ("solid", Color.parse(palette.primary_accent))
    assert tab.content_size.height == 1
    assert screen.query_one("#settings-tabs").content_region.contains_region(tab.region)
    assert screen.query_one("#settings-root").styles.background == Color.parse(
        palette.app_background
    )
    assert screen.query_one("#settings-page-name").styles.color == Color.parse(palette.primary_text)
    assert screen.query_one("#settings-editor-dialog").styles.background == Color.parse(
        palette.elevated_surface
    )
    selected = next(row for row in screen.query(SettingRow) if row.has_class("selected"))
    assert selected.query_one(".setting-value").styles.color == Color.parse(palette.primary_accent)
    save = screen.query_one("#settings-apply", Button)
    if save.disabled:
        assert save.styles.background.a == 0
        assert save.styles.color == Color.parse(palette.disabled_text)
    else:
        assert save.styles.background == Color.parse(palette.primary_accent)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36), (160, 48)])
async def test_open_editor_tracks_theme_and_retains_edit_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await ready(pilot, screen)
        original_bytes = context.paths.config_file.read_bytes()
        assert_palette(app, screen)
        assert screen.action_select_setting("mail.page_size")
        await pilot.pause()
        editor = screen.query_one("#settings-editor", Input)
        editor.value = "42"
        editor.focus()
        for theme in ("builtin.default-light", "builtin.default-dark"):
            app.preview_theme(theme)
            await pilot.pause()
            assert_palette(app, screen)
            assert editor.value == "42" and screen.focused is editor
            assert screen._editor_open and not screen._staged
            assert context.paths.config_file.read_bytes() == original_bytes
        await pilot.click("#settings-stage")
        await ready(pilot, screen)
        await pilot.pause()
        assert screen._staged == {"mail.page_size": 42}
        assert_palette(app, screen)
        await pilot.press("ctrl+z")
        await ready(pilot, screen)
        assert not screen._staged
        assert_palette(app, screen)
        assert context.paths.config_file.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_settings_theme_preview_discard_save_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 36)) as pilot:
        app._open_area("settings")
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await ready(pilot, screen)
        original = app.active_theme.id
        before = context.paths.config_file.read_bytes()
        assert screen.action_select_setting("appearance.theme")
        await pilot.pause()
        screen.query_one("#settings-choice", Select).value = "builtin.default-light"
        assert await pilot.click("#settings-preview")
        await wait_until(
            pilot,
            lambda: screen._preview is not None and not screen._operation_active,
            interval=0.03,
            pause_first=True,
        )
        assert app.active_theme.id == "builtin.default-light"
        assert_palette(app, screen)
        assert context.paths.config_file.read_bytes() == before
        await pilot.press("ctrl+z")
        await ready(pilot, screen)
        assert app.active_theme.id == original
        assert_palette(app, screen)
        assert context.paths.config_file.read_bytes() == before
        assert screen.action_select_setting("appearance.theme")
        await pilot.pause()
        screen.query_one("#settings-choice", Select).value = "builtin.default-light"
        assert await pilot.click("#settings-preview")
        await wait_until(
            pilot,
            lambda: screen._preview is not None and not screen._operation_active,
            interval=0.03,
            pause_first=True,
        )
        await pilot.press("ctrl+s")
        await ready(pilot, screen)
        assert not screen._staged and app.active_theme.id == "builtin.default-light"
        assert_palette(app, screen)
    restarted_context = replace(
        context,
        config=context.config_store.load(),
        state=StateStore(context.state.path, account_key=context.state.account_key),
    )
    restarted = GUCmailApp(restarted_context, offline=True)
    async with restarted.run_test(size=(120, 36)) as pilot:
        restarted._open_area("settings")
        await ready(pilot, restarted.screen)
        assert restarted.active_theme.id == "builtin.default-light"
        assert_palette(restarted, restarted.screen)


@pytest.mark.asyncio
async def test_rich_workspace_titles_recolor_beneath_an_open_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(120, 36)) as pilot:
        app._open_area("activity")
        activity = app.screen
        await wait_until(
            pilot,
            lambda: activity._snapshot is not None and not activity._operation_active,
            interval=0.03,
            pause_first=True,
        )
        assert activity._snapshot is not None
        await pilot.pause()
        title = activity.query_one("#activity-title", Label)
        assert isinstance(title.content, Text)
        before_text = title.content.plain
        app.push_screen(AreaScreen("Theme propagation", lambda: "Synthetic read-only details"))
        await pilot.pause()
        for theme in (
            "builtin.default-light",
            "builtin.high-contrast-dark",
            "builtin.monochrome-light",
        ):
            app.preview_theme(theme)
            await pilot.pause()
            assert title.content.plain == before_text
            assert title.content.style == app.runtime_theme.palette.logo_primary
            assert title.content.spans[0].style == f"bold {app.runtime_theme.palette.primary_text}"
        app.screen.action_close()
        await pilot.pause()
        assert app.screen is activity
        assert title.content.style == app.runtime_theme.palette.logo_primary
