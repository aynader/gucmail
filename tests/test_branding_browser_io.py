"""Branding Apply and the named palette toggle share owned write boundaries."""

import asyncio
from pathlib import Path
from threading import Event, Timer, get_ident

import pytest
from textual.app import App
from textual.screen import Screen
from textual.widgets import DataTable, Input, Select, Static

from gucmail.commands import CommandContext, CommandInvocation
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ViewState
from gucmail.tui.branding import BrandingPreferences
from gucmail.tui.branding_screen import BrandingBrowserBackend, BrandingBrowserScreen
from gucmail.tui.themes import ThemeRegistry
from gucmail.tui.write_coordinator import app_write_coordinator
from test_tui_visual_regressions import _context
from waiting import rounds


class _Host(App[None]):
    def __init__(self, screen: Screen[None]) -> None:
        super().__init__()
        self.branding_screen = screen

    def get_default_screen(self) -> Screen[None]:
        return self.branding_screen


@pytest.mark.asyncio
async def test_pending_app_write_blocks_branding_apply_before_persistence() -> None:
    persisted = []
    theme = ThemeRegistry().get("builtin.default-dark")
    backend = BrandingBrowserBackend(lambda: theme, persist_preferences=persisted.append)
    screen = BrandingBrowserScreen(backend)
    app = _Host(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        before = backend.session.preferences
        lease = app_write_coordinator(app).reserve()
        assert lease is not None and lease.start()
        try:
            screen.action_apply()
            await pilot.pause()
            assert not persisted and backend.session.preferences == before
            assert app_write_coordinator(app).pending
        finally:
            lease.finish()


@pytest.mark.asyncio
async def test_pending_app_write_blocks_named_banner_toggle_before_disk_write(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    before = context.paths.config_file.read_bytes()
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            lease = app_write_coordinator(app).reserve()
            assert lease is not None and lease.start()
            try:
                app._palette_toggle_banner(
                    CommandInvocation("appearance.toggle_banner"), CommandContext(offline_mode=True)
                )
                await pilot.pause()
                assert context.paths.config_file.read_bytes() == before
                assert app_write_coordinator(app).pending
            finally:
                lease.finish()
    finally:
        context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "timeout", "unmount"])
async def test_started_branding_write_keeps_lease_and_ui_callback_after_wait_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    started, release, saved = Event(), Event(), Event()
    original_save = context.config_store.save_document
    original_render = app._render_expanded_branding
    render_threads = []
    main_thread = get_ident()

    def held_save(*args, **kwargs):
        started.set()
        assert release.wait(8), "Owned branding write was not released"
        result = original_save(*args, **kwargs)
        saved.set()
        return result

    def rendered():
        render_threads.append(get_ident())
        return original_render()

    monkeypatch.setattr(context.config_store, "save_document", held_save)
    monkeypatch.setattr(app, "_render_expanded_branding", rendered)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: pytest.fail("Local branding requested EWS"))
    timer = Timer(7, release.set)
    timer.daemon = True
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            screen = BrandingBrowserScreen(app.branding_browser_backend)
            if stop == "timeout":
                # Long enough for a busy machine to start the save; it is held for 7 s.
                screen.operation_timeout_seconds = 1.0
            await app.push_screen(screen)
            await pilot.pause()
            app.branding_browser_backend.session.preview(preset_id="builtin.guc-mail-standard")
            render_threads.clear()
            timer.start()
            try:
                screen.action_apply()
                for _ in rounds(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                assert started.is_set() and app_write_coordinator(app).pending
                if stop == "cancel":
                    await pilot.press("ctrl+c")
                    assert screen.view_state is ViewState.CANCELLED
                elif stop == "timeout":
                    for _ in rounds(100):
                        if not screen._operation_active:
                            break
                        await asyncio.sleep(0.005)
                    assert screen.view_state is ViewState.ERROR
                    assert "timed out" in str(screen.query_one("#branding-state", Static).content)
                else:
                    app.pop_screen()
                    await pilot.pause()
                    screen = BrandingBrowserScreen(app.branding_browser_backend)
                    await app.push_screen(screen)
                    await pilot.pause()
                current = app.branding_browser_backend.session.current
                screen.action_reset()
                screen.action_cancel_preview()
                screen.action_apply()
                assert app.branding_browser_backend.session.current == current
                assert "still running" in str(screen.query_one("#branding-state", Static).content)
                assert app_write_coordinator(app).pending and not saved.is_set()
                release.set()
                await app.workers.wait_for_complete()
                for _ in rounds(500):
                    if saved.is_set() and not app_write_coordinator(app).pending:
                        break
                    await asyncio.sleep(0.01)
                await pilot.pause()
                assert saved.is_set() and not app_write_coordinator(app).pending
                assert app.branding_preferences.preset_id == "builtin.guc-mail-standard"
                assert app._load_branding_preferences() == app.branding_preferences
                assert render_threads == [main_thread]
            finally:
                release.set()
                timer.cancel()
    finally:
        context.close()


@pytest.mark.asyncio
async def test_failed_branding_write_preserves_preview_and_redacts_error() -> None:
    def rejected(_preferences: BrandingPreferences) -> None:
        raise OSError("owned-secret-sentinel-password")

    theme = ThemeRegistry().get("builtin.default-dark")
    backend = BrandingBrowserBackend(lambda: theme, persist_preferences=rejected)
    original = backend.session.preferences
    backend.session.preview(preset_id="builtin.guc-mail-standard")
    screen = BrandingBrowserScreen(backend)
    app = _Host(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen.action_apply()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert backend.session.preferences == original
        assert backend.session.current.preset_id == "builtin.guc-mail-standard"
        assert backend.session.is_previewing
        assert not app_write_coordinator(app).pending
        message = str(screen.query_one("#branding-state", Static).content)
        assert "can't be opened" in message and "owned-secret-sentinel" not in message


@pytest.mark.asyncio
async def test_keyboard_branding_refresh_preserves_filter_and_selected_preset() -> None:
    theme = ThemeRegistry().get("builtin.default-dark")
    screen = BrandingBrowserScreen(BrandingBrowserBackend(lambda: theme))
    app = _Host(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen.query_one("#branding-category", Select).value = "wordmark"
        screen.query_one("#branding-search", Input).value = "GUC"
        await pilot.pause()
        screen._refresh_rows(preserve="builtin.guc-block")
        before = screen._selected_preset_id
        screen.query_one("#branding-table").focus()
        await pilot.press("r")
        await pilot.pause()
        assert screen.query_one("#branding-category", Select).value == "wordmark"
        assert screen.query_one("#branding-search", Input).value == "GUC"
        assert screen._selected_preset_id == before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel_preview", "reset"])
async def test_branding_cancel_and_reset_keep_filtered_row_and_apply_target_consistent(
    action: str,
) -> None:
    theme = ThemeRegistry().get("builtin.default-dark")
    persisted = []
    backend = BrandingBrowserBackend(lambda: theme, persist_preferences=persisted.append)
    original = backend.session.preferences
    screen = BrandingBrowserScreen(backend)
    app = _Host(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen.query_one("#branding-category", Select).value = "banner"
        screen.query_one("#branding-search", Input).value = "Standard"
        await pilot.pause()
        table = screen.query_one("#branding-table", DataTable)
        assert table.row_count == 1
        target = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        assert target != original.preset_id
        screen.action_preview()
        getattr(screen, "action_" + action)()
        await pilot.pause()
        assert not persisted and backend.session.preferences == original
        assert screen._selected_preset_id == target
        assert (
            screen._selected_preset_id
            == table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        )
        if action == "reset":
            screen.action_cancel_preview()
        screen.action_apply()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert backend.session.preferences.preset_id == target
        assert len(persisted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["browser", "palette"])
async def test_held_branding_config_write_stays_off_ui_thread_and_keeps_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    started, release = Event(), Event()
    original = context.config_store.save_document
    threads = []
    main_thread = get_ident()

    def held(*args, **kwargs):
        threads.append(get_ident())
        started.set()
        assert release.wait(5), "Owned branding write was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(context.config_store, "save_document", held)
    timer = Timer(1.5, release.set)
    timer.daemon = True
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            if route == "browser":
                screen = BrandingBrowserScreen(app.branding_browser_backend)
                await app.push_screen(screen)
                await pilot.pause()
            timer.start()
            try:
                if route == "browser":
                    screen.action_apply()
                else:
                    app._palette_toggle_banner(
                        CommandInvocation("appearance.toggle_banner"),
                        CommandContext(offline_mode=True),
                    )
                for _ in rounds(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                assert started.is_set()
                assert threads == [threads[0]] and threads[0] != main_thread
                await asyncio.sleep(0.03)
                assert not release.is_set() and app_write_coordinator(app).pending
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert not app_write_coordinator(app).pending
                assert app._load_branding_preferences() == app.branding_preferences
                assert (
                    GUCmailApp(context, offline=True).branding_preferences
                    == app.branding_preferences
                )
            finally:
                release.set()
                timer.cancel()
    finally:
        context.close()
