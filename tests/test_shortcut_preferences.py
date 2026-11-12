from __future__ import annotations

import ast
from pathlib import Path

import pytest
import textual.widgets as widgets
from tests.test_tui import context_for
from textual.widgets import Button, DataTable, Input, Select, Static

from gucmail.errors import ConfigurationError
from gucmail.help.models import HelpRuntimeContext, ShortcutHelpRecord
from gucmail.help.reference import (
    shortcut_conflicts,
    shortcut_records,
    shortcuts_reference_markdown,
)
from gucmail.services.shortcuts import SHORTCUT_DEFAULTS, normalize_shortcut, validate_shortcuts
from gucmail.settings import SettingsService
from gucmail.tui.app import ComposeScreen, GUCmailApp, HelpScreen, PaletteScreen
from gucmail.tui.area_common import ViewState
from gucmail.tui.settings_screen import SettingsScreen
from gucmail.tui.shortcuts_screen import REQUIRED_SCOPES, ShortcutsScreen, runtime_shortcut_records


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["ctrl+k", "ctrl+p"])
async def test_both_familiar_palette_defaults_still_open_the_unified_palette(
    tmp_path: Path, key: str
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press(key)
        assert isinstance(app.screen, PaletteScreen)
        assert app.gateway is None


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_settings_conflict_is_visible_and_keeps_committed_keys(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    before = context.paths.config_file.read_bytes()
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        for _ in range(100):
            await pilot.pause(0.02)
            if isinstance(app.screen, SettingsScreen) and app.screen._catalog:
                break
        assert isinstance(app.screen, SettingsScreen)
        settings = app.screen
        assert settings.action_select_setting("appearance.next_theme_shortcut")
        await pilot.pause()
        settings.query_one("#settings-editor", Input).value = "ctrl+k"
        settings.query_one("#settings-stage", Button).focus()
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if settings._staged.get("appearance.next_theme_shortcut") == "ctrl+k" and (
                not settings._operation_active
            ):
                break
        await pilot.press("ctrl+s")
        for _ in range(100):
            await pilot.pause(0.02)
            if settings.view_state is ViewState.ERROR and not settings._operation_active:
                break
        assert settings.view_state is ViewState.ERROR
        await pilot.pause()
        status = settings.query_one("#area-state", Static)
        assert 0 <= status.region.y < size[1] - 1
        assert "different actions" in str(settings.query_one("#area-state", Static).content)
        assert settings._staged["appearance.next_theme_shortcut"] == "ctrl+k"
        assert app._current_shortcuts() == SHORTCUT_DEFAULTS
        assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize(
    "raw,expected",
    [
        (" Control+Shift+K ", "ctrl+shift+k"),
        ("alt+ctrl+space", "ctrl+alt+space"),
        ("F24", "f24"),
        ("Ctrl+K", "ctrl+k"),
        ("", ""),
        ("  ", ""),
    ],
)
def test_shortcut_normalization_preserves_disabled_and_portable_keys(
    raw: str, expected: str
) -> None:
    assert normalize_shortcut(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "c",
        "delete",
        "shift+a",
        "ctrl+k,ctrl+p",
        "ctrl+k ctrl+p",
        "ctrl+ctrl+k",
        "ctrl+unknown",
        "ctrl+",
        "cmd+k",
        "f25",
        "ctrl+\x1b",
        True,
        4,
        "x" * 41,
    ],
)
def test_invalid_global_keys_fail_before_preferences_are_written(
    tmp_path: Path, raw: object
) -> None:
    context = context_for(tmp_path)
    service = SettingsService(context.config_store)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        service.set("palette.primary_shortcut", raw)
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize(
    "key",
    [
        "ctrl+s",
        "ctrl+shift+s",
        "ctrl+q",
        "alt+left",
        "f1",
        "f6",
        "f7",
        "ctrl+e",
        "ctrl+shift+k",
        "ctrl+pageup",
    ],
)
def test_reserved_editor_navigation_keys_are_rejected_on_review_and_apply(
    tmp_path: Path, key: str
) -> None:
    context = context_for(tmp_path)
    service = SettingsService(context.config_store)
    before = context.paths.config_file.read_bytes()
    session = service.begin().set("palette.primary_shortcut", key)
    with pytest.raises(ConfigurationError, match="reserved"):
        session.preview()
    with pytest.raises(ConfigurationError, match="reserved"):
        session.apply()
    assert context.paths.config_file.read_bytes() == before


def test_cross_action_conflicts_fail_atomically_but_palette_aliases_are_allowed(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    service = SettingsService(context.config_store)
    before = context.paths.config_file.read_bytes()
    session = service.begin().set("palette.primary_shortcut", "f8")
    session.set("appearance.next_theme_shortcut", "f8")
    with pytest.raises(ConfigurationError, match="different actions"):
        session.apply()
    assert context.paths.config_file.read_bytes() == before
    keys = validate_shortcuts({"palette.secondary_shortcut": "ctrl+k"})
    assert keys["palette.primary_shortcut"] == keys["palette.secondary_shortcut"]
    service.set("palette.secondary_shortcut", "ctrl+k")
    assert not shortcut_conflicts(
        shortcut_records(HelpRuntimeContext(settings={"palette.secondary_shortcut": "ctrl+k"}))
    )


def test_disabled_assignments_and_theme_customization_are_current_in_headless_help() -> None:
    context = HelpRuntimeContext(
        settings={"palette.secondary_shortcut": "", "appearance.next_theme_shortcut": "alt+g"}
    )
    records = shortcut_records(context)
    assert next(r for r in records if r.setting_id == "palette.secondary_shortcut").key == ""
    assert (
        next(r for r in records if r.setting_id == "appearance.next_theme_shortcut").key == "alt+g"
    )
    assert "Disabled" in shortcuts_reference_markdown(context)


def test_shortcut_swap_restart_transfer_reset_and_undo(tmp_path: Path) -> None:
    context = context_for(tmp_path / "first")
    service = SettingsService(context.config_store)
    staged = service.begin()
    staged.set("palette.primary_shortcut", "ctrl+alt+left")
    staged.set("appearance.previous_theme_shortcut", "alt+k")
    staged.set("palette.secondary_shortcut", "")
    assert len(staged.preview()) == 3
    staged.cancel()
    assert service.value("palette.primary_shortcut").effective_value == "ctrl+k"
    staged = service.begin()
    staged.set("palette.primary_shortcut", "ctrl+alt+left")
    staged.set("appearance.previous_theme_shortcut", "alt+k")
    staged.set("palette.secondary_shortcut", "")
    staged.apply()
    restarted = SettingsService(context.config_store)
    assert restarted.value("palette.primary_shortcut").effective_value == "ctrl+alt+left"
    target = SettingsService(context_for(tmp_path / "second").config_store)
    target.apply_import(target.plan_import(restarted.export()))
    for identifier in SHORTCUT_DEFAULTS:
        assert (
            target.value(identifier).effective_value == restarted.value(identifier).effective_value
        )
    restarted.reset("palette.primary_shortcut")
    assert restarted.value("palette.primary_shortcut").effective_value == "ctrl+k"
    restarted.undo()
    assert restarted.value("palette.primary_shortcut").effective_value == "ctrl+alt+left"


def test_import_and_environment_conflicts_are_validated_without_config_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = context_for(tmp_path)
    service = SettingsService(context.config_store)
    before = context.paths.config_file.read_bytes()
    payload = service.export()
    payload["settings"].update(
        {"palette.primary_shortcut": "f8", "appearance.next_theme_shortcut": "f8"}
    )
    with pytest.raises(ConfigurationError, match="different actions"):
        service.plan_import(payload)
    monkeypatch.setenv("GUCMAIL_SETTING_APPEARANCE__NEXT_THEME_SHORTCUT", "f8")
    with pytest.raises(ConfigurationError, match="different actions"):
        service.set("palette.primary_shortcut", "f8")
    assert context.paths.config_file.read_bytes() == before


def test_conflicts_use_actual_screen_context_and_ignore_same_action_aliases() -> None:
    rows = (
        ShortcutHelpRecord("one", "Mail", "d", "Delete", context="List"),
        ShortcutHelpRecord("two", "Mail", "d", "Details", context="Reader"),
        ShortcutHelpRecord("three", "Mail", "d", "Delete", context="List"),
        ShortcutHelpRecord("four", "Mail", "d", "Different action", context="List"),
    )
    assert shortcut_conflicts(rows) == {("list", "d"): ("one", "three", "four")}
    assert not shortcut_conflicts(rows[:3])


@pytest.mark.asyncio
async def test_binding_browser_metadata_covers_declared_screens_and_focused_controls(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(120, 35)):
        rows = runtime_shortcut_records(app, app._current_shortcuts())
        assert app.gateway is None
        assert set(REQUIRED_SCOPES) <= {row.scope for row in rows}
        contexts = {row.context for row in rows}
        for path in Path("src/gucmail/tui").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "textual.widgets":
                    for alias in node.names:
                        control = getattr(widgets, alias.name)
                        if control._merged_bindings.key_to_bindings:
                            assert alias.name in contexts
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and any(
                    isinstance(child, ast.AnnAssign)
                    and isinstance(child.target, ast.Name)
                    and child.target.id == "BINDINGS"
                    for child in node.body
                ):
                    assert node.name in contexts or node.name == "GUCmailApp"
        assert {"Input", "TextArea", "DataTable", "Tree", "Select", "Button"} <= contexts
        assert {row.key for row in rows if row.context == "DesktopSetupScreen"} >= {"escape", "f5"}
        assert {row.key for row in rows if row.context == "NotificationReaderApp"} >= {
            "q",
            "escape",
            "f5",
        }
        assert not shortcut_conflicts(rows)
        assert {row.key for row in rows if row.context == "ComposeScreen" and row.customizable} == {
            "ctrl+k",
            "ctrl+p",
        }
        assert any(
            row.context == "PaletteScreen" and row.key == "down" and "suggestions" in row.action
            for row in rows
        )
        assert any(
            row.context == "MessagePreviewScroll"
            and row.key == "end"
            and "message preview" in row.action
            for row in rows
        )
        rendered = shortcuts_reference_markdown(HelpRuntimeContext(shortcut_assignments=rows))
        assert "ComposeScreen" in rendered and "Command palette" in rendered


@pytest.mark.asyncio
async def test_legacy_or_environment_key_conflicts_use_visible_safe_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = context_for(tmp_path)
    service = SettingsService(context.config_store)
    service.set("palette.primary_shortcut", "f8")
    before = context.paths.config_file.read_bytes()
    monkeypatch.setenv("GUCMAIL_SETTING_APPEARANCE__NEXT_THEME_SHORTCUT", "f8")
    app = GUCmailApp(context, offline=True)
    assert app._current_shortcuts() == SHORTCUT_DEFAULTS
    assert "different actions" in app._shortcut_notice
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("ctrl+k")
        assert isinstance(app.screen, PaletteScreen)
        await pilot.press("escape")
        app._open_shortcuts()
        await pilot.pause()
        assert isinstance(app.screen, ShortcutsScreen)
        assert "Global defaults are active" in app.screen.notice
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.asyncio
async def test_applying_shortcuts_rebinds_mounted_composer_and_removes_old_theme_keys(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        assert isinstance(app.screen, ComposeScreen)
        composer = app.screen
        session = SettingsService(context.config_store).begin()
        session.set("palette.primary_shortcut", "f8")
        session.set("palette.secondary_shortcut", "")
        session.set("appearance.previous_theme_shortcut", "f9")
        session.set("appearance.next_theme_shortcut", "f10")
        revision = session.apply()
        assert revision is not None
        app._apply_committed_settings(tuple(SHORTCUT_DEFAULTS))
        assert not any(
            b.action == "app.palette" for b in composer._bindings.key_to_bindings.get("ctrl+k", [])
        )
        for key in ("ctrl+alt+left", "ctrl+alt+right"):
            assert key not in app._bindings.key_to_bindings
        composer.query_one("#compose-subject", Input).focus()
        await pilot.press("ctrl+k")
        assert app.screen is composer
        await pilot.press("f8")
        assert isinstance(app.screen, PaletteScreen)
        await pilot.press("escape")
        app.pop_screen()
        old_theme = app.active_theme
        await pilot.press("f10")
        assert app.active_theme != old_theme
        await pilot.press("f9")
        assert app.active_theme == old_theme


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_help_browser_filters_and_customizes_via_staged_settings(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        await pilot.press("?")
        assert isinstance(app.screen, HelpScreen)
        assert await pilot.click("#help-shortcuts")
        await pilot.pause()
        assert isinstance(app.screen, ShortcutsScreen)
        browser = app.screen
        for _ in range(250):
            await pilot.pause(0.02)
            if browser.is_mounted and browser.query_one("#shortcut-table", DataTable).columns:
                break
        assert len(browser.query_one("#shortcut-table", DataTable).columns) == 6
        for scope in REQUIRED_SCOPES:
            browser.query_one("#shortcut-scope", Select).value = scope
            table = browser.query_one("#shortcut-table", DataTable)
            for _ in range(100):
                await pilot.pause(0.02)
                if table.row_count and all(
                    str(table.get_row(row.key)[2]) == scope for row in table.ordered_rows
                ):
                    break
            assert table.row_count
            assert all(str(table.get_row(row.key)[2]) == scope for row in table.ordered_rows)
        browser.query_one("#shortcut-scope", Select).value = "Global"
        await pilot.press("ctrl+f")
        browser.query_one("#shortcut-search", Input).value = "palette.primary_shortcut"
        await pilot.pause()
        assert browser.query_one("#shortcut-table", DataTable).row_count == 1
        browser.query_one("#shortcut-table", DataTable).focus()
        await pilot.press("ctrl+enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if isinstance(app.screen, SettingsScreen) and app.screen._selected_setting_id == (
                "palette.primary_shortcut"
            ):
                break
        assert isinstance(app.screen, SettingsScreen)
        settings = app.screen
        assert settings._selected_setting_id == "palette.primary_shortcut"
        settings.query_one("#settings-editor", Input).value = "f8"
        settings.query_one("#settings-stage", Button).focus()
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if "palette.primary_shortcut" in settings._staged and not settings._operation_active:
                break
        assert app._palette_primary_shortcut == "ctrl+k"
        assert settings._staged.get("palette.primary_shortcut") == "f8", (
            settings._staged,
            settings.query_one("#settings-editor", Input).value,
            settings._selected_setting_id,
        )
        await pilot.press("ctrl+z")
        for _ in range(100):
            await pilot.pause(0.02)
            if not settings._staged and not settings._operation_active:
                break
        assert not settings._staged and not settings._operation_active
        assert app._palette_primary_shortcut == "ctrl+k"
        settings.action_edit_setting()
        await pilot.pause()
        for _ in range(100):
            await pilot.pause(0.02)
            if not settings.query_one("#settings-stage", Button).has_class("-active"):
                break
        settings.query_one("#settings-editor", Input).value = "f8"
        settings.query_one("#settings-stage", Button).focus()
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if "palette.primary_shortcut" in settings._staged and not settings._operation_active:
                break
        assert settings._staged.get("palette.primary_shortcut") == "f8", (
            settings._staged,
            settings.query_one("#settings-editor", Input).value,
            settings._selected_setting_id,
        )
        await pilot.press("ctrl+s")
        for _ in range(100):
            await pilot.pause(0.02)
            if app._palette_primary_shortcut == "f8" and not settings._operation_active:
                break
        assert app._palette_primary_shortcut == "f8"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        rows = shortcut_records(app.screen.help_context)
        primary = next(r for r in rows if r.setting_id == "palette.primary_shortcut")
        assert primary.key == "f8"
        await pilot.press("escape")
        await pilot.press("f8")
        assert isinstance(app.screen, PaletteScreen)
