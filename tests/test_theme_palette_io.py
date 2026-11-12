"""The direct palette route must have the same persistence boundary as the browser."""

import asyncio
from pathlib import Path
from threading import Event, Timer, get_ident

import pytest
from textual.widgets import Static

from gucmail.commands import CommandContext, CommandInvocation
from gucmail.tui.app import GUCmailApp
from gucmail.tui.appearance_screen import ThemeBrowserScreen
from gucmail.tui.write_coordinator import app_write_coordinator
from test_tui_visual_regressions import _context


@pytest.mark.asyncio
async def test_named_palette_theme_rejects_an_existing_app_write(tmp_path: Path) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    invocation = CommandInvocation.from_mapping(
        "appearance.apply_theme", {"theme": "Default Light"}
    )
    before = context.paths.config_file.read_bytes()
    original = app.active_theme.id
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            lease = app_write_coordinator(app).reserve()
            assert lease is not None and lease.start()
            try:
                app._palette_apply_theme(invocation, CommandContext(offline_mode=True))
                await pilot.pause()
                assert context.paths.config_file.read_bytes() == before
                assert app.active_theme.id == original
                assert app_write_coordinator(app).pending
            finally:
                lease.finish()
    finally:
        context.close()


@pytest.mark.asyncio
async def test_named_palette_theme_keeps_ui_loop_responsive_during_owned_disk_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    invocation = CommandInvocation.from_mapping(
        "appearance.apply_theme", {"theme": "Default Light"}
    )
    started, release = Event(), Event()
    original_save = context.config_store.save_document
    threads: list[int] = []
    main_thread = get_ident()

    def held_save(*args, **kwargs):
        threads.append(get_ident())
        started.set()
        assert release.wait(5), "Owned palette config write was not released"
        return original_save(*args, **kwargs)

    monkeypatch.setattr(context.config_store, "save_document", held_save)
    timer = Timer(1.5, release.set)
    timer.daemon = True
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            timer.start()
            try:
                app._palette_apply_theme(invocation, CommandContext(offline_mode=True))
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                assert started.is_set()
                assert threads == [threads[0]] and threads[0] != main_thread
                await asyncio.sleep(0.03)
                assert not release.is_set() and app_write_coordinator(app).pending
                # A browser opened while the palette write runs shares the same session.
                browser = ThemeBrowserScreen(app.theme_browser_backend)
                await app.push_screen(browser)
                await pilot.pause(0.01)
                browser._refresh_rows(preserve="builtin.monochrome-dark")
                preferences = app.theme_session.preferences
                current = app.theme_session.current.id
                browser.action_preview()
                browser.action_cancel_preview()
                assert app.theme_session.preferences == preferences
                assert app.theme_session.current.id == current
                assert "still running" in str(browser.query_one("#theme-state", Static).content)
                assert not release.is_set() and app_write_coordinator(app).pending
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert context.config_store.load().theme == "builtin.default-light"
                assert app.active_theme.id == "builtin.default-light"
                assert not app_write_coordinator(app).pending
            finally:
                release.set()
                timer.cancel()
    finally:
        context.close()
