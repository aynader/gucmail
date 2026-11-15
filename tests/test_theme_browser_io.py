"""Appearance writes must remain responsive and share the app's write ownership."""

import asyncio
import json
from pathlib import Path
from threading import Event, Timer, get_ident

import pytest
from textual.app import App
from textual.screen import Screen
from textual.widgets import Button, Input, Select, Static

from gucmail.tui.app import GUCmailApp
from gucmail.tui.appearance_screen import ThemeBrowserBackend, ThemeBrowserScreen
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.theme_repository import CustomThemeRepository
from gucmail.tui.themes import ThemePreferences
from gucmail.tui.write_coordinator import app_write_coordinator
from test_tui_visual_regressions import _context
from waiting import rounds


class _Host(App[None]):
    def __init__(self, screen: Screen[None]) -> None:
        super().__init__()
        self.theme_screen = screen

    def get_default_screen(self) -> Screen[None]:
        return self.theme_screen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    ["apply", "favorite", "duplicate", "create", "rename", "import", "export", "delete", "reset"],
)
async def test_another_app_write_blocks_theme_changes_before_any_io_or_confirmation(
    tmp_path: Path, action: str
) -> None:
    persisted: list[ThemePreferences] = []
    backend = ThemeBrowserBackend(
        CustomThemeRepository(tmp_path / "custom.json"), persist_preferences=persisted.append
    )
    custom = backend.duplicate("builtin.default-dark", name="Owned synthetic custom")
    incoming = tmp_path / "incoming.json"
    payload = json.loads(backend.export_payload(custom.id))
    payload["id"] = "custom.owned-incoming"
    payload["name"] = "Owned incoming"
    incoming.write_text(json.dumps(payload), encoding="utf-8")
    outgoing = tmp_path / "outgoing.json"
    screen = ThemeBrowserScreen(backend)
    app = _Host(screen)
    before = backend.repository.path.read_bytes()
    preferences = backend.session.preferences
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen._refresh_rows(preserve=custom.id)
        screen.query_one("#theme-custom-name", Input).value = "Updated synthetic custom"
        screen.query_one("#theme-transfer-path", Input).value = str(
            incoming if action == "import" else outgoing
        )
        lease = app_write_coordinator(app).reserve()
        assert lease is not None and lease.start()
        try:
            if action == "create":
                screen.action_create_custom_theme()
            else:
                selector = "theme-apply" if action == "apply" else f"theme-{action}"
                screen.query_one("#" + selector, Button).press()
            await pilot.pause()
            assert app.screen is screen, "A rejected change must not open its confirmation/editor"
            assert backend.repository.path.read_bytes() == before
            assert backend.session.preferences == preferences and not persisted
            assert not outgoing.exists()
            assert "still running" in str(screen.query_one("#theme-state", Static).content)
            assert app_write_coordinator(app).pending
        finally:
            lease.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "timeout", "unmount"])
async def test_started_theme_write_keeps_lease_and_activates_on_ui_thread_after_wait_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    started, release, saved = Event(), Event(), Event()
    original_save = context.config_store.save_document
    original_activate = app._activate_theme
    activation_threads: list[int] = []
    main_thread = get_ident()

    def held_save(*args, **kwargs):
        started.set()
        assert release.wait(8), "Owned config write was not released"
        result = original_save(*args, **kwargs)
        saved.set()
        return result

    def activated(definition):
        activation_threads.append(get_ident())
        return original_activate(definition)

    def no_gateway():
        pytest.fail("Offline appearance work must not request Exchange")

    monkeypatch.setattr(context.config_store, "save_document", held_save)
    monkeypatch.setattr(app, "_activate_theme", activated)
    monkeypatch.setattr(app, "_ensure_gateway", no_gateway)
    timer = Timer(7, release.set)
    timer.daemon = True
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            screen = ThemeBrowserScreen(app.theme_browser_backend)
            if stop == "timeout":
                # Long enough for a busy machine to start the save; it is held for 7 s.
                screen.operation_timeout_seconds = 1.0
            await app.push_screen(screen)
            await pilot.pause()
            screen._refresh_rows(preserve="builtin.default-light")
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
                    await pilot.pause(0.01)
                    assert screen.view_state is ViewState.CANCELLED
                elif stop == "timeout":
                    for _ in rounds(100):
                        if not screen._operation_active:
                            break
                        await asyncio.sleep(0.005)
                    assert screen.view_state is ViewState.ERROR
                    assert "timed out" in str(screen.query_one("#theme-state", Static).content)
                else:
                    app.pop_screen()
                    await pilot.pause()
                    screen = ThemeBrowserScreen(app.theme_browser_backend)
                    await app.push_screen(screen)
                    await pilot.pause()
                screen.action_favorite()
                assert "still running" in str(screen.query_one("#theme-state", Static).content)
                assert app_write_coordinator(app).pending and not saved.is_set()
                release.set()
                for _ in rounds(500):
                    if (
                        saved.is_set()
                        and not app_write_coordinator(app).pending
                        and app.active_theme.id == "builtin.default-light"
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert saved.is_set() and not app_write_coordinator(app).pending
                assert context.config_store.load().theme == "builtin.default-light"
                assert app.active_theme.id == "builtin.default-light"
                assert activation_threads == [main_thread]
                assert app.theme_session.preferences.favorites == ()
            finally:
                release.set()
                timer.cancel()
    finally:
        context.close()


@pytest.mark.asyncio
async def test_failed_theme_write_keeps_preview_and_redacts_low_level_error(tmp_path: Path) -> None:
    def rejected(_preferences: ThemePreferences) -> None:
        raise OSError("owned-secret-sentinel-password")

    backend = ThemeBrowserBackend(
        CustomThemeRepository(tmp_path / "custom.json"), persist_preferences=rejected
    )
    original = backend.session.preferences
    backend.preview("builtin.default-light")
    screen = ThemeBrowserScreen(backend)
    app = _Host(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen._refresh_rows(preserve="builtin.default-light")
        screen.action_apply()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert backend.session.preferences == original
        assert backend.session.current.id == "builtin.default-light"
        assert backend.session.is_previewing
        assert not app_write_coordinator(app).pending
        message = str(screen.query_one("#theme-state", Static).content)
        assert "failed (OSError)" in message and "owned-secret-sentinel" not in message


@pytest.mark.asyncio
async def test_held_theme_persistence_runs_outside_ui_loop_and_reserves_shared_write(
    tmp_path: Path,
) -> None:
    started, release = Event(), Event()
    threads: list[int] = []
    persisted: list[ThemePreferences] = []

    def persist(preferences: ThemePreferences) -> None:
        threads.append(get_ident())
        started.set()
        assert release.wait(5), "Owned persistence fixture was not released"
        persisted.append(preferences)

    backend = ThemeBrowserBackend(
        CustomThemeRepository(tmp_path / "custom.json"), persist_preferences=persist
    )
    screen = ThemeBrowserScreen(backend)
    app = _Host(screen)
    main_thread = get_ident()
    timer = Timer(1.5, release.set)
    timer.daemon = True
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen._refresh_rows(preserve="builtin.default-light")
        timer.start()
        try:
            screen.action_apply()
            for _ in rounds(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            assert started.is_set()
            assert threads == [threads[0]] and threads[0] != main_thread
            heartbeat: list[bool] = []
            asyncio.get_running_loop().call_later(0.01, heartbeat.append, True)
            await asyncio.sleep(0.03)
            assert heartbeat and not release.is_set(), (
                "UI loop must run while the disk write is held"
            )
            assert app_write_coordinator(app).pending
            screen.action_favorite()
            await pilot.pause(0.01)
            assert len(threads) == 1, "No overlapping second persistence call is allowed"
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(persisted) == 1
            assert backend.session.applied.id == "builtin.default-light"
            assert not app_write_coordinator(app).pending
            assert "Applied" in str(screen.query_one("#theme-state", Static).content)
        finally:
            release.set()
            timer.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "reset"])
async def test_theme_confirmation_opened_before_another_write_rechecks_guard(
    tmp_path: Path, action: str
) -> None:
    backend = ThemeBrowserBackend(CustomThemeRepository(tmp_path / "custom.json"))
    custom = backend.duplicate("builtin.default-dark", name="Owned custom")
    screen = ThemeBrowserScreen(backend)
    app = _Host(screen)
    before = backend.repository.path.read_bytes()
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen._refresh_rows(preserve=custom.id)
        screen.query_one("#theme-" + action, Button).press()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        lease = app_write_coordinator(app).reserve()
        assert lease is not None and lease.start()
        try:
            await pilot.press("y")
            await pilot.pause()
            assert app.screen is screen
            assert backend.repository.path.read_bytes() == before
            assert "still running" in str(screen.query_one("#theme-state", Static).content)
            assert app_write_coordinator(app).pending
        finally:
            lease.finish()


@pytest.mark.asyncio
async def test_keyboard_refresh_discovers_new_custom_theme_and_preserves_selection(
    tmp_path: Path,
) -> None:
    backend = ThemeBrowserBackend(CustomThemeRepository(tmp_path / "custom.json"))
    screen = ThemeBrowserScreen(backend)
    app = _Host(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen._refresh_rows(preserve="builtin.default-dark")
        selected = screen._selected_theme_id
        custom = backend.duplicate("builtin.default-dark", name="Owned refresh custom")
        assert custom.id not in screen._entries
        screen.query_one("#theme-table").focus()
        await pilot.press("r")
        await pilot.pause()
        assert custom.id in screen._entries
        assert screen._selected_theme_id == selected
        assert len(screen._entries) == 662


@pytest.mark.asyncio
async def test_keyboard_refresh_retains_category_query_and_exact_selection(tmp_path: Path) -> None:
    backend = ThemeBrowserBackend(CustomThemeRepository(tmp_path / "custom.json"))
    screen = ThemeBrowserScreen(backend)
    app = _Host(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen.query_one("#theme-category", Select).value = "monochrome"
        screen.query_one("#theme-search", Input).value = "Green Screen"
        await pilot.pause()
        screen._refresh_rows(preserve="builtin.green-screen")
        screen.query_one("#theme-table").focus()
        await pilot.press("r")
        await pilot.pause()
        assert screen.query_one("#theme-category", Select).value == "monochrome"
        assert screen.query_one("#theme-search", Input).value == "Green Screen"
        assert screen._selected_theme_id == "builtin.green-screen"
