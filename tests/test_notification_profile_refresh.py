"""Programmatic profile-selector refreshes must not requery idle local state."""

import asyncio
from dataclasses import replace

import pytest
from textual.widgets import DataTable, Select, TabbedContent, Tabs

from gucmail.tui.notification_settings_screen import NotificationSettingsScreen
from test_local_ocr_tui import focus, wait
from test_notification_settings_screen_secondpass import (
    NotificationSettingsHarness,
    _profile,
    _service,
)


def counted_screen(service):
    screen = NotificationSettingsScreen(service)
    refreshes = []
    original = screen.action_refresh

    def refresh():
        refreshes.append(screen._selected_profile_id)
        return original()

    screen.action_refresh = refresh
    return screen, refreshes


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_idle_programmatic_profile_render_stops_after_initial_refresh(tmp_path, size):
    screen, refreshes = counted_screen(_service(tmp_path / "state.sqlite3"))
    app = NotificationSettingsHarness(screen)
    async with app.run_test(size=size):
        await asyncio.sleep(0.15)
        initial = len(refreshes)
        assert initial >= 1
        await asyncio.sleep(0.25)
        assert len(refreshes) == initial, "An idle screen must not repeatedly inspect SQLite"
        assert screen._selected_profile_id == "campus"


@pytest.mark.asyncio
async def test_real_keyboard_profile_change_refreshes_once_and_ignores_stale_events(tmp_path):
    service = _service(tmp_path / "state.sqlite3")
    secondary = _profile()
    retained = replace(secondary.rules[0], rule_id="retained-secondary-rule", name="Keep this rule")
    service.profiles.save_profile(
        replace(secondary, profile_id="secondary", name="Secondary profile",
                rules=(*secondary.rules, retained))
    )
    screen, refreshes = counted_screen(service)
    app = NotificationSettingsHarness(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait(pilot, lambda: screen._selected_profile_id == "campus"
                   and not screen._operation_active)
        initial = len(refreshes)
        selector = await focus(pilot, "#notification-profile-select")
        await pilot.press("enter", "end", "enter")
        await wait(pilot, lambda: screen._selected_profile_id == "secondary"
                   and not screen._operation_active)
        await pilot.pause()
        assert len(refreshes) == initial + 1
        assert selector.value == "secondary"
        tabs = screen.query_one("#notification-tabs", TabbedContent)
        tabs.query_one(Tabs).focus()
        await pilot.press("home", "right", "enter")
        await wait(pilot, lambda: tabs.active == "notification-rules-tab")
        table = screen.query_one("#notification-rules-table", DataTable)
        table.focus()
        await wait(pilot, lambda: table.has_focus and table.row_count == 2)
        await pilot.press("end")
        await wait(pilot, lambda: screen._selected_rule_id == retained.rule_id)
        selected_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        assert selected_key.value == retained.rule_id
        selector.post_message(Select.Changed(selector, "campus"))
        selector.post_message(Select.Changed(selector, "secondary"))
        await pilot.pause()
        assert len(refreshes) == initial + 1
        assert screen._selected_profile_id == "secondary"
        assert screen._selected_rule_id == retained.rule_id
        assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key == selected_key
