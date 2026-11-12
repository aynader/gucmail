"""Notification metadata inspection must leave keyboard and timer input responsive."""

import threading

import pytest
from textual.widgets import Button, DataTable, Static

from gucmail.tui.area_common import ViewState
from gucmail.tui.notification_settings_screen import NotificationSettingsScreen
from test_local_ocr_tui import wait
from test_notification_settings_screen_secondpass import NotificationSettingsHarness, _service


@pytest.mark.asyncio
async def test_profile_and_provider_inspection_run_outside_ui_thread(tmp_path, monkeypatch):
    service = _service(tmp_path / "state.sqlite3")
    ui_thread = threading.get_ident()
    threads = []
    for owner in (service.profiles, service.providers):
        original = owner.inspect

        def inspect(original=original):
            threads.append(threading.get_ident())
            return original()

        monkeypatch.setattr(owner, "inspect", inspect)
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait(pilot, lambda: len(threads) >= 2 and not screen._operation_active)
        assert all(thread != ui_thread for thread in threads), (
            "Metadata reads can wait on local storage and must not block keyboard/rendering"
        )


@pytest.mark.asyncio
async def test_held_provider_read_allows_keyboard_close_and_ignores_late_snapshot(
    tmp_path, monkeypatch
):
    service = _service(tmp_path / "state.sqlite3")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = service.providers.inspect

    def held():
        started.set()
        assert release.wait(10), "The test did not release its owned read"
        try:
            return original()
        finally:
            finished.set()

    monkeypatch.setattr(service.providers, "inspect", held)
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await wait(pilot, started.is_set)
            assert screen.view_state is ViewState.LOADING
            assert screen._operation_active and not finished.is_set()
            assert screen.query_one("#notification-rule-new", Button).disabled
            assert not screen.query_one(".area-close", Button).disabled
            await pilot.press("escape")
            await wait(pilot, lambda: app.screen is not screen)
            assert not finished.is_set(), "Keyboard close must work before storage completes"
            release.set()
            await wait(pilot, finished.is_set)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not screen._profiles, "Late results must not populate a closed screen"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_cancelled_local_diagnostics_ignores_a_held_provider_result(tmp_path, monkeypatch):
    service = _service(tmp_path / "state.sqlite3")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = service.providers.inspect
    ui_thread = threading.get_ident()
    threads = []

    def held():
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(10)
        try:
            return original()
        finally:
            finished.set()

    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await wait(
                pilot, lambda: screen._profile() is not None and not screen._operation_active
            )
            monkeypatch.setattr(service.providers, "inspect", held)
            screen._run_diagnostics()
            await wait(pilot, started.is_set)
            assert screen._operation_active and len(threads) == 1
            assert threads[0] != ui_thread
            await pilot.press("ctrl+c")
            await wait(pilot, lambda: screen.view_state is ViewState.CANCELLED)
            assert not finished.is_set()
            release.set()
            await wait(pilot, finished.is_set)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not str(screen.query_one("#notification-diagnostics", Static).content)
    finally:
        release.set()


@pytest.mark.asyncio
async def test_requested_rule_editor_waits_for_metadata_instead_of_using_stale_profile(
    tmp_path, monkeypatch
):
    from gucmail.tui.notification_settings_screen import RuleEditorScreen

    service = _service(tmp_path / "state.sqlite3")
    started = threading.Event()
    release = threading.Event()
    original = service.providers.inspect

    def held():
        started.set()
        assert release.wait(10)
        return original()

    monkeypatch.setattr(service.providers, "inspect", held)
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    try:
        async with app.run_test(size=(120, 35)) as pilot:
            await wait(pilot, started.is_set)
            screen.action_create_rule()
            assert app.screen is screen and screen._pending_configuration_action is not None
            release.set()
            await wait(
                pilot,
                lambda: (
                    isinstance(app.screen, RuleEditorScreen)
                    and app.screen.is_mounted
                    and bool(app.screen.query("#rule-builder-actions"))
                ),
            )
            app.screen.query_one("#rule-builder-actions", DataTable)
            await pilot.pause()
            assert service.profiles.list_profiles()[0].profile_id == "campus"
            await pilot.press("escape")
            await wait(pilot, lambda: app.screen is screen)
    finally:
        release.set()


async def _keyboard_read_states_workflow(tmp_path, size, monkeypatch, capture=None):
    from test_notification_settings_screen_secondpass import _activate_tab_with_keyboard

    service = _service(tmp_path / "state.sqlite3")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    original = service.providers.inspect
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def held():
        started.set()
        assert release.wait(20)
        try:
            return original()
        finally:
            finished.set()

    async def frame(label):
        if capture is not None:
            await capture(app, pilot, size, label)

    monkeypatch.setattr(service.providers, "inspect", held)
    try:
        async with app.run_test(size=size) as pilot:
            await wait(pilot, started.is_set)
            assert screen.view_state is ViewState.LOADING
            await frame("loading")
            screen.action_create_rule()
            assert screen._pending_configuration_action is not None
            await pilot.press("ctrl+c")
            await wait(pilot, lambda: screen.view_state is ViewState.CANCELLED)
            assert not finished.is_set() and screen._pending_configuration_action is None
            await frame("cancelled")
            release.set()
            await wait(pilot, finished.is_set)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not screen._profiles and app.screen is screen

            monkeypatch.setattr(service.providers, "inspect", original)
            await pilot.press("r")
            await wait(
                pilot, lambda: screen._profile() is not None and not screen._operation_active
            )
            await pilot.pause()
            await frame("ready")

            def unexpected_read():
                raise AssertionError("A provider highlight must use the completed snapshot")

            monkeypatch.setattr(service.providers, "inspect", unexpected_read)
            await _activate_tab_with_keyboard(pilot, screen, "notification-provider-tab")
            table = screen.query_one("#notification-provider-table", DataTable)
            table.focus()
            await pilot.press("home", "down", "up")
            await pilot.pause()
            detail = str(screen.query_one("#notification-provider-detail", Static).content)
            assert "Privacy:" in detail and "keyring://" not in detail
            assert screen.view_state is ViewState.READY
            await frame("cached-provider")

            def failed():
                raise RuntimeError("synthetic-private-error-value")

            monkeypatch.setattr(service.providers, "inspect", failed)
            await pilot.press("r")
            await wait(pilot, lambda: screen.view_state is ViewState.ERROR)
            assert "synthetic-private-error-value" not in str(
                screen.query_one("#area-state", Static).content
            )
            await frame("error")
            monkeypatch.setattr(service.providers, "inspect", original)
            retry = screen.query_one("#area-retry", Button)
            retry.focus()
            await pilot.pause()
            await wait(pilot, lambda: app.focused is retry and retry.is_on_screen)
            await pilot.press("enter")
            await wait(
                pilot, lambda: screen.view_state is ViewState.READY and not screen._operation_active
            )
            await frame("retried")

            await _activate_tab_with_keyboard(pilot, screen, "notification-diagnostics-tab")
            run = screen.query_one("#notification-diagnostics-run", Button)
            run.focus()
            await pilot.pause()
            await wait(pilot, lambda: app.focused is run and run.is_on_screen)
            await pilot.press("enter")
            await wait(
                pilot,
                lambda: (
                    "LOCAL-ONLY DIAGNOSTICS"
                    in str(screen.query_one("#notification-diagnostics", Static).content)
                ),
            )
            assert not screen._operation_active
            assert "keyring://" not in str(
                screen.query_one("#notification-diagnostics", Static).content
            )
            await frame("diagnostics")
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_keyboard_read_states_cancel_retry_and_cached_provider(tmp_path, size, monkeypatch):
    await _keyboard_read_states_workflow(tmp_path, size, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,attribute",
    [
        ("rules", "_selected_rule_id"),
        ("quiet", "_selected_quiet_id"),
        ("digest", "_selected_digest_id"),
        ("provider", "_selected_provider_id"),
        ("routing", "_selected_route"),
        ("webhook", "_selected_webhook_name"),
    ],
)
async def test_rendered_notification_selection_survives_refresh_and_clears_empty(
    tmp_path, kind, attribute
):
    from dataclasses import replace

    from gucmail.notification_policies import RoutingMatrix
    from gucmail.secret_store import InMemorySecretBackend, SecretStore
    from gucmail.services.webhooks import WebhookConfig, WebhookRegistry
    from gucmail.state import StateStore
    from test_notification_settings_screen_secondpass import _activate_tab_with_keyboard

    service = _service(tmp_path / "state.sqlite3")
    profile = service.profiles.get_profile("campus")
    assert profile is not None
    expanded = replace(
        profile,
        rules=(
            *profile.rules,
            replace(profile.rules[0], rule_id="second-rule", name="Second rule"),
        ),
        quiet_hours=replace(
            profile.quiet_hours,
            windows=(
                *profile.quiet_hours.windows,
                replace(profile.quiet_hours.windows[0], window_id="second-window"),
            ),
        ),
        digest_schedules=(
            *profile.digest_schedules,
            replace(profile.digest_schedules[0], schedule_id="second-digest", name="Second digest"),
        ),
        providers=(
            *profile.providers,
            replace(profile.providers[0], provider_id="second-provider"),
        ),
    )
    service.profiles.save_profile(expanded)
    registry = WebhookRegistry(
        StateStore(tmp_path / "state.sqlite3", account_key="settings-test"),
        secret_store=SecretStore("webhooks", backend=InMemorySecretBackend()),
    )
    for name in ("first-webhook", "second-webhook"):
        registry.add(WebhookConfig(name, "https://hooks.example.test/synthetic", enabled=False))
    screen = NotificationSettingsScreen(service, webhook_registry=registry)
    app = NotificationSettingsHarness(screen)
    tabs = {
        "rules": "rules",
        "quiet": "quiet",
        "digest": "digest",
        "provider": "provider",
        "routing": "routing",
        "webhook": "webhook",
    }
    async with app.run_test(size=(120, 35)) as pilot:
        await wait(pilot, lambda: screen._profile() is not None and not screen._operation_active)
        await _activate_tab_with_keyboard(pilot, screen, f"notification-{tabs[kind]}-tab")
        table = screen.query_one(f"#notification-{kind}-table", DataTable)
        table.focus()
        await pilot.press("home", "down")
        await pilot.pause()
        selected = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

        def current():
            value = getattr(screen, attribute)
            return "\x1f".join(value) if isinstance(value, tuple) else value

        assert table.cursor_row == 1 and current() == selected
        await pilot.press("r")
        await wait(pilot, lambda: not screen._operation_active)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert current() == selected
        assert str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value) == selected
        service.profiles.save_profile(
            replace(
                expanded,
                rules=(),
                providers=(),
                quiet_hours=replace(expanded.quiet_hours, windows=()),
                digest_schedules=(),
                routing_matrix=RoutingMatrix(),
            )
        )
        for name in ("first-webhook", "second-webhook"):
            registry.remove(name)
        await pilot.press("r")
        await wait(pilot, lambda: not screen._operation_active)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert table.row_count == 0 and getattr(screen, attribute) is None
