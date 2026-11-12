"""A delayed Settings rebuild must preserve the user's newer action context."""

import asyncio

import pytest
from textual.widgets import Button, Input, Select

from gucmail.tui.app import GUCmailApp
from gucmail.tui.settings_form import SettingRow, SettingsForm
from gucmail.tui.settings_screen import SettingsScreen
from test_tasks_display_preferences import _context, configure


async def wait_until(condition):
    async with asyncio.timeout(10):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination,size",
    [
        ("save", (70, 20)),
        ("save", (80, 24)),
        ("save", (120, 36)),
        ("discard", (80, 24)),
        ("search", (80, 24)),
        ("other-row", (80, 24)),
        ("same-row", (80, 24)),
    ],
)
async def test_staged_rebuild_respects_newer_focus_and_keyboard_action(
    tmp_path, monkeypatch, destination, size
):
    context = _context(tmp_path)
    configure(context)
    before = context.paths.config_file.read_bytes()
    app = GUCmailApp(context, offline=True)
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        async with app.run_test(size=size) as pilot:
            try:
                app._open_area("settings")
                await wait_until(lambda: isinstance(app.screen, SettingsScreen)
                                 and app.screen._catalog is not None
                                 and not app.screen._render_pending)
                settings = app.screen
                assert settings.action_select_setting("tasks.sort_order")
                await wait_until(lambda: settings._editor_open and not settings._render_pending
                                 and bool(settings.query("#settings-choice #label")))
                settings.query_one("#settings-choice", Select).value = "subject"
                stage = settings.query_one("#settings-stage", Button)
                stage.focus()
                await wait_until(lambda: stage.has_focus and not stage.disabled)
                await pilot.pause()
                form = settings.query_one("#settings-form", SettingsForm)
                mount = SettingsForm.mount

                async def held_mount(self, *widgets, **kwargs):
                    if self is form and not entered.is_set():
                        if destination == "other-row":
                            result = await mount(self, *widgets, **kwargs)
                            entered.set()
                            await release.wait()
                            return result
                        entered.set()
                        await release.wait()
                    return await mount(self, *widgets, **kwargs)

                monkeypatch.setattr(SettingsForm, "mount", held_mount)
                pending = asyncio.create_task(pilot.press("enter"))
                await asyncio.wait_for(entered.wait(), 10)
                assert settings._staged["tasks.sort_order"] == "subject"
                target = None
                if destination in {"save", "discard", "search"}:
                    target = settings.query_one({
                        "save": "#settings-apply", "discard": "#settings-cancel",
                        "search": "#settings-search",
                    }[destination])
                elif destination == "other-row":
                    target = next(row for row in form.query(SettingRow)
                                  if row.setting_id == "tasks.default_view")
                if target is not None:
                    assert not target.disabled
                    target.focus()
                    await wait_until(lambda: target.has_focus)
            finally:
                # Release before run_test teardown even if a precondition fails.
                release.set()
            if pending is not None:
                await asyncio.wait_for(pending, 10)
            await wait_until(lambda: not settings._render_pending)
            await pilot.pause()
            if target is not None:
                assert target.has_focus
            else:
                assert isinstance(app.focused, SettingRow)
                assert app.focused.setting_id == "tasks.sort_order"
            if destination == "save":
                await pilot.press("enter")
                await wait_until(lambda: not settings._staged and not settings.mutation_pending)
                assert (
                    app._settings_service().value("tasks.sort_order").effective_value == "subject"
                )
                assert not settings._editor_open
            elif destination == "discard":
                await pilot.press("enter")
                await wait_until(lambda: not settings._staged and not settings.mutation_pending)
                assert context.paths.config_file.read_bytes() == before
            elif destination == "search":
                await pilot.press(*"tasks.sort_order")
                assert settings.query_one("#settings-search", Input).value == "tasks.sort_order"
                assert context.paths.config_file.read_bytes() == before
            else:
                await pilot.press("enter")
                await wait_until(lambda: settings._editor_open)
                assert settings._selected_setting_id == (
                    "tasks.default_view" if destination == "other-row" else "tasks.sort_order"
                )
                assert context.paths.config_file.read_bytes() == before
    finally:
        release.set()
        if pending is not None and not pending.done():
            await pending
        context.close()
