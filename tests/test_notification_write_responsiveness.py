"""Synthetic notification persistence must leave UI input responsive."""

import threading

import pytest
from textual.widgets import Button, Static, TextArea

from gucmail.notification_events import PrivacyMode
from gucmail.secret_store import InMemorySecretBackend, SecretStore
from gucmail.services.webhooks import WebhookConfig, WebhookRegistry
from gucmail.state import StateStore
from gucmail.tui.notification_settings_screen import (
    NotificationSettingsScreen,
    ProfileEdit,
    preview_provider_metadata_document,
)
from gucmail.tui.write_coordinator import app_write_coordinator
from test_local_ocr_tui import wait
from test_notification_settings_screen_secondpass import (
    NotificationSettingsHarness,
    _event,
    _service,
)


def edit():
    return ProfileEdit(
        "campus", "Changed synthetic profile", ("Inbox",), 60, "automatic", True, False
    )


def test_provider_preview_rejects_oversized_document_before_parsing(monkeypatch):
    import gucmail.tui.notification_settings_screen as module

    def unexpected_parse(value):
        raise AssertionError("An oversized document must not enter the JSON parser")

    monkeypatch.setattr(module.json, "loads", unexpected_parse)
    with pytest.raises(ValueError, match="too large"):
        preview_provider_metadata_document(" " * (4 * 1024 * 1024 + 1))


@pytest.mark.asyncio
async def test_notification_profile_persistence_does_not_run_on_ui_thread(tmp_path, monkeypatch):
    service = _service(tmp_path / "state.sqlite3")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    original = service.profiles.save_profile
    threads = []
    ui_thread = threading.get_ident()

    def save(profile):
        threads.append(threading.get_ident())
        return original(profile)

    async with app.run_test(size=(120, 35)) as pilot:
        await wait(pilot, lambda: screen._profile() is not None and not screen._operation_active)
        monkeypatch.setattr(service.profiles, "save_profile", save)
        screen._profile_editor_resolved(edit())
        await wait(pilot, lambda: threads and not screen._operation_active)
        await app.workers.wait_for_complete()
        assert threads and all(thread != ui_thread for thread in threads)
        assert service.profiles.get_profile("campus").name == "Changed synthetic profile"


@pytest.mark.asyncio
async def test_closing_a_held_notification_write_retains_lease_until_actual_completion(
    tmp_path, monkeypatch
):
    service = _service(tmp_path / "state.sqlite3")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    original = service.profiles.save_profile
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def save(profile):
        started.set()
        assert release.wait(10)
        try:
            return original(profile)
        finally:
            finished.set()

    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await wait(
                pilot, lambda: screen._profile() is not None and not screen._operation_active
            )
            monkeypatch.setattr(service.profiles, "save_profile", save)
            screen._profile_editor_resolved(edit())
            await wait(pilot, started.is_set)
            assert screen._operation_active and not finished.is_set()
            assert screen.query_one("#notification-rule-new", Button).disabled
            assert not screen.query_one(".area-close", Button).disabled
            await pilot.press("escape")
            await wait(pilot, lambda: app.screen is not screen)
            coordinator = app_write_coordinator(app)
            assert coordinator.pending and coordinator.reserve() is None
            assert not finished.is_set()
            release.set()
            await wait(pilot, finished.is_set)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not coordinator.pending
            assert service.profiles.get_profile("campus").name == "Changed synthetic profile"
            assert screen._profile().name == "Campus mail"
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["folders", "desktop", "mute", "quiet", "privacy"])
async def test_palette_persistence_runs_in_worker_and_refreshes_real_state(
    tmp_path, monkeypatch, action
):
    service = _service(tmp_path / "state.sqlite3")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    calls = []
    ui_thread = threading.get_ident()
    target, method = (
        (service.profiles, "save_profile")
        if action == "folders"
        else (
            service,
            {
                "desktop": "set_desktop_notifications",
                "mute": "mute_notifications",
                "quiet": "enable_quiet_hours",
                "privacy": "set_notification_privacy",
            }[action],
        )
    )
    original = getattr(target, method)

    def traced(*args, **kwargs):
        calls.append(threading.get_ident())
        return original(*args, **kwargs)

    async with app.run_test(size=(80, 24)) as pilot:
        await wait(pilot, lambda: screen._profile() is not None and not screen._operation_active)
        monkeypatch.setattr(target, method, traced)
        {
            "folders": lambda: screen.action_configure_folder_scope("add", "Inbox/Synthetic"),
            "desktop": lambda: screen.action_set_desktop_notifications(False),
            "mute": screen.action_mute_notifications,
            "quiet": screen.action_enable_quiet_hours,
            "privacy": lambda: screen.action_set_notification_privacy("private"),
        }[action]()
        await wait(pilot, lambda: calls and not screen._operation_active)
        await app.workers.wait_for_complete()
        assert calls and all(thread != ui_thread for thread in calls)
        profile = service.profiles.active_profile()
        assert profile is not None and screen._profile() == profile
        if action == "folders":
            assert "Inbox/Synthetic" in profile.folders
        elif action in {"desktop", "mute"}:
            assert all(not item.enabled for item in profile.providers)
        elif action == "quiet":
            assert profile.quiet_hours.windows and all(
                w.enabled for w in profile.quiet_hours.windows
            )
        else:
            assert all(item.privacy.mode is PrivacyMode.PRIVATE for item in profile.providers)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["export", "simulate"])
async def test_local_notification_reads_are_cancellable_and_ignore_late_results(
    tmp_path, monkeypatch, kind
):
    service = _service(tmp_path / "state.sqlite3")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    target, method = (
        (service.profiles, "export_document") if kind == "export" else (service, "simulate")
    )
    original = getattr(target, method)
    started, release = threading.Event(), threading.Event()
    threads = []
    ui_thread = threading.get_ident()

    def held(*args, **kwargs):
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await wait(
                pilot, lambda: screen._profile() is not None and not screen._operation_active
            )
            monkeypatch.setattr(target, method, held)
            if kind == "export":
                editor = screen.query_one("#notification-transfer-payload", TextArea)
                editor.load_text("Keep this unsaved synthetic document")
                screen._export()
            else:
                screen._render_simulation("campus", (_event(),), source="Synthetic sample")
            await wait(pilot, started.is_set)
            assert screen._operation_active and all(t != ui_thread for t in threads)
            assert not screen.query_one("#area-cancel-operation", Button).disabled
            screen.action_cancel_operation()
            await pilot.pause()
            assert not screen._operation_active
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            if kind == "export":
                assert editor.text == "Keep this unsaved synthetic document"
            else:
                assert "DRY RUN" not in str(
                    screen.query_one("#notification-rule-detail", Static).content
                )
    finally:
        release.set()


@pytest.mark.asyncio
async def test_changed_webhook_definition_requires_fresh_confirmation_without_delivery(tmp_path):
    service = _service(tmp_path / "state.sqlite3")
    registry = WebhookRegistry(
        StateStore(tmp_path / "state.sqlite3", account_key="settings-test"),
        secret_store=SecretStore("webhooks", backend=InMemorySecretBackend()),
    )
    registry.add(WebhookConfig("synthetic", "https://example.test/original", enabled=False))
    deliveries = []
    screen = NotificationSettingsScreen(
        service,
        webhook_registry=registry,
        webhook_delivery=lambda config, event: deliveries.append((config, event)),
    )
    app = NotificationSettingsHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait(pilot, lambda: screen._webhooks and not screen._operation_active)
        registry.set_enabled("synthetic", enabled=True)
        screen.action_test_webhook("synthetic", confirmed=True)
        await wait(pilot, lambda: not screen._operation_active)
        await app.workers.wait_for_complete()
        assert not deliveries
        assert "changed after selection" in str(screen.query_one("#area-state", Static).content)


@pytest.mark.asyncio
async def test_persisted_metadata_does_not_allow_another_palette_write_before_completion(
    tmp_path, monkeypatch
):
    service = _service(tmp_path / "state.sqlite3")
    screen = NotificationSettingsScreen(service)
    app = NotificationSettingsHarness(screen)
    started, release = threading.Event(), threading.Event()
    desktop = service.set_desktop_notifications
    privacy = service.set_notification_privacy
    calls = []

    def held(*args, **kwargs):
        result = desktop(*args, **kwargs)
        started.set()
        assert release.wait(10)
        return result

    def traced(*args, **kwargs):
        calls.append(True)
        return privacy(*args, **kwargs)

    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await wait(
                pilot, lambda: screen._profile() is not None and not screen._operation_active
            )
            monkeypatch.setattr(service, "set_desktop_notifications", held)
            monkeypatch.setattr(service, "set_notification_privacy", traced)
            screen.action_set_desktop_notifications(True)
            await wait(pilot, started.is_set)
            assert service.providers.get_provider("desktop-main").enabled
            screen.action_set_notification_privacy("full")
            await pilot.pause()
            assert screen._operation_active and not calls
            release.set()
            await app.workers.wait_for_complete()
            await wait(pilot, lambda: not screen._operation_active)
            assert not calls
            screen.action_set_notification_privacy("full")
            await wait(pilot, lambda: calls and not screen._operation_active)
            profile = service.profiles.active_profile()
            assert calls == [True]
            assert profile is not None
            assert {item.privacy.mode for item in profile.providers} == {PrivacyMode.FULL}
    finally:
        release.set()
