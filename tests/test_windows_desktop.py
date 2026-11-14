"""Owned synthetic registry/WinRT contract tests; never native display or setup."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from gucmail.errors import ConfigurationError
from gucmail.models import Address, MessageDetail, MessageSummary, NotificationEvent
from gucmail.notification_providers.core import PrivacyMode
from gucmail.notification_providers.windows import (
    NativeWindowsToastBackend,
    ToastDuration,
    ToastSound,
    WindowsBackendStatus,
    WindowsToastProvider,
    WindowsToastSettings,
    build_windows_toast,
    discover_windows_toast_backend,
    make_click_token,
    parse_click_token,
)
from gucmail.services.notifications import (
    DeliveryState,
    DesktopNotificationProvider,
    NotificationDispatcher,
)
from gucmail.services.windows_desktop import (
    DesktopIdentity,
    WindowsDesktopService,
    account_fingerprint,
    activation_reference,
    read_desktop_identity,
)
from gucmail.state import StateStore

EMAIL = "reader@example.test"
ENDPOINT = "https://exchange.example.test/EWS/Exchange.asmx"
ACCOUNT = account_fingerprint(EMAIL, ENDPOINT)


@pytest.fixture(autouse=True)
def restore_activation_profile_environment():
    # The real entrypoint runs in its own process and selects its registered
    # profile. These in-process contract tests must not select that profile for
    # subsequent unrelated CLI tests.
    keys = (
        "GUCMAIL_CONFIG",
        "GUCMAIL_CONFIG_DIR",
        "GUCMAIL_DATA_DIR",
        "GUCMAIL_CACHE_DIR",
        "GUCMAIL_LOG_DIR",
    )
    before = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class Registry:
    def __init__(self, identity):
        self.identity = identity
        self.keys = {path: None for path in identity.registry_values}
        self.calls = []
        self.error = None
        self.write_error = None
        self.remove_error = None

    def read(self, paths):
        assert set(paths) == set(self.identity.registry_values)
        if self.error:
            raise self.error
        return copy.deepcopy(self.keys)

    def write(self, values):
        self.calls.append("write")
        assert values == self.identity.registry_values
        if self.write_error:
            raise self.write_error
        self.keys = copy.deepcopy(values)

    def remove(self, paths):
        self.calls.append("remove")
        assert set(paths) <= set(self.identity.registry_values)
        if self.remove_error:
            raise self.remove_error
        for path in paths:
            self.keys[path] = None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"synthetic executable; never run")
    identity = DesktopIdentity(
        tmp_path / "config.toml",
        tmp_path / "data",
        tmp_path / "cache",
        tmp_path / "logs",
        executable,
        ACCOUNT,
    )
    registry = Registry(identity)
    service = WindowsDesktopService(identity, registry)
    # Every test must supply its owned fake instead of touching native registry.
    monkeypatch.setattr(
        "gucmail.services.windows_desktop.CurrentUserDesktopRegistry",
        lambda: pytest.fail("Native registry accessed"),
    )
    return identity, registry, service


def install(setup):
    identity, registry, service = setup
    result = service.apply(service.preview(), confirmed=True)
    assert result == {"applied": True, "operation": "setup", "installed": True}
    return identity, registry, service


@pytest.mark.parametrize(
    "reference",
    [
        "--help",
        "-m-reference",
        " m-reference",
        "m-reference ",
        "m-\x1b[31mreference",
        "m-\x00reference",
        "m-\nreference",
        "m-" + "x" * 600,
        "",
        "m/a",
        "m\\a",
        "m;exit",
        "m&exit",
        'm"x',
        "m%1",
        "m=raw",
        None,
    ],
)
def test_activation_rejects_rewritten_option_and_nonlocal_references(reference):
    with pytest.raises(ValueError):
        make_click_token(reference)


@pytest.mark.parametrize("reference", ["m-reference", "m-123", "local.ref:123", "x" * 200])
@pytest.mark.parametrize("account", [None, ACCOUNT])
def test_exact_activation_reference_roundtrip(reference, account):
    activation = make_click_token(reference, account_key=account)
    assert parse_click_token(activation.token) == activation
    assert activation.arguments == ("messages", "show", reference)
    assert activation.account_key == account


@pytest.mark.parametrize("account", ["other", "A" * 64, "f" * 63, True, []])
def test_invalid_account_identity_cannot_enter_activation(account):
    with pytest.raises(ValueError):
        make_click_token("m-reference", account_key=account)


def test_distinct_reference_is_not_merged_by_sanitization():
    clean = make_click_token("m-reference")
    with pytest.raises(ValueError):
        hostile = make_click_token("m-\x1b[31mreference")
        assert hostile.token != clean.token


def test_invalid_target_preserves_safe_notification_without_activation():
    event = _event("--help")
    request = build_windows_toast(event)
    assert request.activation is None and "Subject" in request.body


def _event(reference="m-reference"):
    return NotificationEvent(
        "evt-test",
        "new_message",
        datetime(2026, 10, 7, tzinfo=UTC),
        EMAIL,
        folder="Inbox",
        message_reference=reference,
        sender=Address("sender@example.test", "Sender"),
        subject="Subject",
        preview="Preview",
    )


@pytest.mark.parametrize("operation", ["setup", "remove"])
@pytest.mark.parametrize(
    "error", [PermissionError("Access denied"), OSError("Registry unavailable")]
)
def test_unknown_registration_blocks_every_change(setup, operation, error):
    identity, registry, service = setup
    registry.error = error
    preview = service.preview(operation)
    assert not preview.can_apply and not preview.inspection.known
    with pytest.raises(ConfigurationError):
        service.apply(preview, confirmed=True)
    assert not registry.calls and not identity.manifest.exists()


@pytest.mark.parametrize("operation", ["setup", "remove"])
@pytest.mark.parametrize("conflict", ["owner", "extra", "missing_owner", "manifest"])
def test_unknown_or_unowned_entries_are_preserved(setup, operation, conflict):
    identity, registry, service = install(setup)
    root = next(iter(registry.keys))
    if conflict == "owner":
        registry.keys[root]["GUCmailOwner"] = "Another application"
    elif conflict == "extra":
        registry.keys[root]["OtherValue"] = "preserve"
    elif conflict == "missing_owner":
        del registry.keys[root]["GUCmailOwner"]
    else:
        identity.manifest.write_bytes(b"preserve unowned file")
    before_keys = copy.deepcopy(registry.keys)
    before_file = identity.manifest.read_bytes()
    registry.calls.clear()
    preview = service.preview(operation)
    assert not preview.can_apply
    with pytest.raises(ConfigurationError):
        service.apply(preview, confirmed=True)
    assert registry.keys == before_keys and identity.manifest.read_bytes() == before_file
    assert registry.calls == []


def test_setup_is_confirmed_exact_owned_and_idempotent(setup):
    identity, registry, service = setup
    preview = service.preview()
    with pytest.raises(ConfigurationError):
        service.apply(preview, confirmed=False)
    assert not registry.calls and not identity.manifest.exists()
    install(setup)
    assert identity.manifest.read_bytes() == identity.content
    assert read_desktop_identity(identity.manifest) == identity
    assert service.apply(service.preview(), confirmed=True)["applied"] is False
    assert registry.calls == ["write"]
    assert service.apply(service.preview("remove"), confirmed=True)["installed"] is False
    assert not identity.manifest.exists() and not any(registry.keys.values())
    assert service.apply(service.preview("remove"), confirmed=True)["applied"] is False


@pytest.mark.parametrize("change", ["denied", "new_key", "new_manifest", "exe_gone"])
def test_after_confirmation_changes_block_stale_plan(setup, change):
    identity, registry, service = setup
    preview = service.preview()
    if change == "denied":
        registry.error = PermissionError("Denied after confirmation")
    elif change == "new_key":
        registry.keys[next(iter(registry.keys))] = {"Other": "preserve"}
    elif change == "new_manifest":
        identity.manifest.parent.mkdir()
        identity.manifest.write_bytes(b"preserve")
    else:
        identity.executable.unlink()
    with pytest.raises(ConfigurationError):
        service.apply(preview, confirmed=True)
    assert not registry.calls


def test_partial_native_write_is_reported_and_manifest_recovery_is_owned(setup):
    identity, registry, service = setup
    registry.write_error = PermissionError("Denied writing registration")
    with pytest.raises(ConfigurationError, match="some owned entries may have changed"):
        service.apply(service.preview(), confirmed=True)
    assert identity.manifest.read_bytes() == identity.content
    assert service.preview().can_apply
    registry.write_error = None
    assert service.apply(service.preview(), confirmed=True)["installed"] is True


def test_failed_native_removal_preserves_manifest(setup):
    identity, registry, service = install(setup)
    registry.remove_error = PermissionError("Denied removing registration")
    with pytest.raises(ConfigurationError, match="Inspect"):
        service.apply(service.preview("remove"), confirmed=True)
    assert identity.manifest.read_bytes() == identity.content
    assert service.inspect().installed


@pytest.mark.parametrize("bad", [b"{}", b"not JSON", b"x" * 16_385])
def test_activation_rejects_invalid_manifest_before_registry_or_credentials(setup, bad):
    identity, _, _ = setup
    identity.manifest.parent.mkdir()
    identity.manifest.write_bytes(bad)
    with pytest.raises(ConfigurationError):
        read_desktop_identity(identity.manifest)


@pytest.mark.parametrize(
    "kind", ["other_account", "legacy", "wrong_scheme", "url_suffix", "extra_uri", "overlong"]
)
def test_only_account_bound_canonical_uri_opens(setup, kind):
    identity, _, _ = setup
    token = make_click_token("m-reference", account_key=ACCOUNT).token
    uri = identity.scheme + ":" + token
    if kind == "other_account":
        uri = identity.scheme + ":" + make_click_token("m-reference", account_key="f" * 64).token
    elif kind == "legacy":
        uri = identity.scheme + ":" + make_click_token("m-reference").token
    elif kind == "wrong_scheme":
        uri = "gucmail-other:" + token
    elif kind == "url_suffix":
        uri += "?arg=--help"
    elif kind == "extra_uri":
        uri += '" --help'
    else:
        uri += "x" * 1_100
    with pytest.raises(ConfigurationError):
        activation_reference(identity, uri)


def native_api():
    shown, constructors = [], []

    class Toast:
        def __init__(self, text, **kwargs):
            self.text_fields = text
            self.__dict__.update(kwargs)

    class Toaster:
        def __init__(self, name, *, notifierAUMID):
            constructors.append((name, notifierAUMID))

        def show_toast(self, toast):
            shown.append(toast)

    api = SimpleNamespace(
        Toast=Toast,
        ToastAudio=lambda **kwargs: SimpleNamespace(**kwargs),
        ToastDuration=SimpleNamespace(Short="short", Long="long"),
        InteractableWindowsToaster=Toaster,
    )
    return api, shown, constructors


def test_native_contract_uses_owned_id_sound_group_duration_and_bound_protocol(setup):
    identity, registry, service = install(setup)
    api, shown, constructors = native_api()
    backend, status = discover_windows_toast_backend(
        platform_name="win32", identity=identity, desktop_service=service, native_api=api
    )
    assert isinstance(backend, NativeWindowsToastBackend)
    assert status.click_to_open and status.sound_control and status.native_grouping
    assert not status.native_display_verified and not status.native_activation_verified
    provider = WindowsToastProvider(
        backend,
        WindowsToastSettings(
            privacy=PrivacyMode.PRIVATE, sound=ToastSound.SILENT, duration=ToastDuration.LONG
        ),
        backend_status=status,
        activation_account=(EMAIL, ACCOUNT),
    )
    outcome = provider.notify(_event())
    assert outcome.state is DeliveryState.DELIVERED and "visibility depends" in outcome.detail
    toast = shown[0]
    assert toast.text_fields == ["GUC Mail", "You have a new message."]
    assert toast.audio.silent is True and toast.duration == "long"
    assert len(toast.tag) == 32 and len(toast.group) == 24
    assert activation_reference(identity, toast.launch_action) == "m-reference"
    assert constructors[0][1] == identity.app_id
    assert "reader@example.test" not in toast.launch_action
    provider.notify(replace(_event(), event_id="evt-next"))
    assert len(constructors) == 1 and registry.calls == ["write"]


def test_other_account_cannot_open_from_native_request(setup):
    identity, _, service = install(setup)
    api, shown, _ = native_api()
    backend = NativeWindowsToastBackend(api, identity, service)
    provider = WindowsToastProvider(backend, activation_account=(EMAIL, ACCOUNT))
    provider.notify(replace(_event(), account_id="other@example.test"))
    assert shown[0].launch_action is None


def test_native_setup_drift_fails_isolated_delivery_and_preserves_event_history(
    setup, tmp_path, monkeypatch
):
    identity, registry, service = install(setup)
    api, shown, _ = native_api()
    backend = NativeWindowsToastBackend(api, identity, service)
    provider = DesktopNotificationProvider(
        windows_provider=WindowsToastProvider(backend, activation_account=(EMAIL, ACCOUNT))
    )
    monkeypatch.setattr("gucmail.services.notifications.sys.platform", "win32")
    registry.error = PermissionError("Denied")
    dispatcher = NotificationDispatcher([provider], state=StateStore(tmp_path / "state.db"))
    outcome = dispatcher.dispatch(_event())[0]
    assert outcome.state is DeliveryState.FAILED and not shown
    assert dispatcher.history()[0]["state"] == "failed"


def test_explicit_unavailable_backend_is_not_rediscovered(monkeypatch):
    monkeypatch.setattr(
        "gucmail.notification_providers.windows.discover_windows_toast_backend",
        lambda **kwargs: pytest.fail("Duplicate discovery"),
    )
    provider = WindowsToastProvider(
        backend_status=WindowsBackendStatus(False, None, False, False, "Missing")
    )
    assert provider.notify(_event()).state is DeliveryState.SKIPPED


def test_protocol_entry_rejects_extra_arguments_and_checks_registration(setup, monkeypatch):
    from gucmail import toast_entry

    identity, _, service = install(setup)
    monkeypatch.setattr(toast_entry, "WindowsDesktopService", lambda value: service)
    uri = identity.scheme + ":" + make_click_token("m-reference", account_key=ACCOUNT).token
    assert toast_entry.prepare_activation([str(identity.manifest), uri]) == (
        identity,
        "m-reference",
    )
    monkeypatch.setattr(
        toast_entry, "open_activation", lambda *args: pytest.fail("Invalid entry opened")
    )
    assert toast_entry.main([str(identity.manifest), uri, "--help"]) == 3


@pytest.mark.parametrize("operation", ["setup", "remove"])
def test_cli_previews_requires_confirmation_and_applies_owned_service(
    setup, tmp_path, monkeypatch, operation
):
    from tests.test_notification_cli_secondpass import _environment

    import gucmail.cli as cli

    identity, registry, service = setup
    monkeypatch.setattr(cli.notifications, "desktop_setup_service", lambda *args: service)
    environment = _environment(tmp_path)
    arguments = ["--json", "--non-interactive", "notifications", "desktop", operation]
    runner = CliRunner()
    result = runner.invoke(cli.app, arguments, env=environment)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["can_apply"] and not data["native_display_verified"]
    assert not registry.calls and not identity.manifest.exists()
    refused = runner.invoke(cli.app, [*arguments, "--apply"], env=environment)
    assert refused.exit_code == 9 and not registry.calls
    applied = runner.invoke(cli.app, [*arguments, "--apply", "--yes"], env=environment)
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["data"]["operation"] == operation


def test_cli_confirmation_rechecks_registry_before_writing(setup, tmp_path, monkeypatch):
    from tests.test_notification_cli_secondpass import _environment

    import gucmail.cli as cli

    identity, registry, service = setup
    monkeypatch.setattr(cli.notifications, "desktop_setup_service", lambda *args: service)

    def confirm(*args, **kwargs):
        registry.error = PermissionError("Denied after review")

    monkeypatch.setattr(cli.core, "confirm", confirm)
    result = CliRunner().invoke(
        cli.app,
        ["--json", "notifications", "desktop", "setup", "--apply"],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 3 and "after preview" in result.output
    assert not registry.calls and not identity.manifest.exists()


@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("denied", [False, True])
@pytest.mark.asyncio
async def test_normal_notifications_desktop_setup_preview_and_cancel(
    setup, tmp_path, monkeypatch, size, denied
):
    from tests.test_settings_secondpass import _context
    from textual.widgets import Button, Input, Static, TabbedContent

    from gucmail.tui.app import GUCmailApp, PaletteScreen
    from gucmail.tui.area_common import ConfirmationScreen, ViewState
    from gucmail.tui.desktop_setup_screen import DesktopSetupScreen
    from gucmail.tui.notification_settings_screen import NotificationSettingsScreen

    identity, registry, service = setup
    context = _context(tmp_path)
    monkeypatch.setattr(
        "gucmail.services.windows_desktop.desktop_setup_service", lambda *args: service
    )
    monkeypatch.setattr(
        context.credential_resolver,
        "resolve",
        lambda *args, **kwargs: pytest.fail("Setup resolved credentials"),
    )
    if denied:
        registry.error = PermissionError("Access denied")
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:

        async def wait(predicate):
            for _ in range(120):
                await pilot.pause(0.025)
                if predicate():
                    return
            assert predicate()

        await pilot.press("ctrl+k")
        await wait(lambda: isinstance(app.screen, PaletteScreen))
        app.screen.query_one("#palette-input", Input).value = "Open Notifications"
        await pilot.pause()
        await pilot.press("enter")
        await wait(
            lambda: (
                isinstance(app.screen, NotificationSettingsScreen)
                and app.screen.is_mounted
                and app.screen.view_state not in {ViewState.IDLE, ViewState.LOADING}
                and not app.screen._operation_active
            )
        )
        notifications = app.screen
        tabs = notifications.query_one(TabbedContent)
        tabs.active = "notification-provider-tab"
        await pilot.pause()
        notifications.query_one("#notification-desktop-setup", Button).focus()
        await pilot.press("enter")
        await wait(
            lambda: (
                isinstance(app.screen, DesktopSetupScreen)
                and not app.screen._operation_active
                and app.screen.view_state in {ViewState.READY, ViewState.ERROR}
            )
        )
        desktop = app.screen
        assert (
            "Access denied" in str(desktop.query_one("#desktop-setup-status", Static).render())
            if denied
            else "Not configured"
            in str(desktop.query_one("#desktop-setup-status", Static).render())
        )
        assert desktop.query_one("#desktop-setup-enable", Button).disabled is denied
        if denied:
            assert desktop.view_state is ViewState.ERROR
            registry.error = None
            await pilot.press("f5")
            await wait(
                lambda: (
                    not desktop._operation_active
                    and not desktop.query_one("#desktop-setup-enable", Button).disabled
                )
            )
        desktop.query_one("#desktop-setup-enable", Button).focus()
        await pilot.press("enter")
        await wait(lambda: isinstance(app.screen, ConfirmationScreen))
        assert not registry.calls and not identity.manifest.exists()
        await pilot.press("escape")
        await wait(lambda: app.screen is desktop)
        await pilot.press("escape")
        await wait(lambda: app.screen is notifications)
        assert not registry.calls and not identity.manifest.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_notification_reader_reads_only_target_sanitizes_and_preserves_read_state(
    tmp_path, monkeypatch, size
):
    from tests.test_settings_secondpass import _context
    from textual.widgets import Static

    from gucmail.tui.notification_reader import NotificationReaderApp

    context = _context(tmp_path)
    context.config = replace(context.config, email=EMAIL, endpoint=ENDPOINT)
    fingerprint = account_fingerprint(EMAIL, ENDPOINT)
    calls = []

    class Gateway:
        def show_message(self, reference):
            calls.append(reference)
            return MessageDetail(
                MessageSummary(
                    reference,
                    "Subject \x1b[31m literal [red]",
                    Address("sender@example.test", "Sender"),
                    None,
                    is_read=False,
                ),
                body_html='<script>unsafe()</script><p>Read me</p><img src="https://tracking.example.test/x">',
            )

        def __getattr__(self, name):
            pytest.fail("Reader used unexpected operation: " + name)

    context._gateway = Gateway()
    monkeypatch.setattr(
        context.credential_resolver,
        "resolve",
        lambda *args, **kwargs: pytest.fail("Injected reader resolved credentials"),
    )
    app = NotificationReaderApp(context, fingerprint, "m-reference")
    async with app.run_test(size=size) as pilot:
        for _ in range(120):
            await pilot.pause(0.025)
            if "Read-only message" in str(app.query_one("#toast-reader-status", Static).render()):
                break
        content = str(app.query_one("#toast-reader-content", Static).render())
        assert "Read-only message" in str(app.query_one("#toast-reader-status", Static).render())
        assert "Read me" in content and "literal [red]" in content
        assert (
            "\x1b" not in content
            and "unsafe()" not in content
            and "tracking.example.test" not in content
        )
        assert calls == ["m-reference"]
        await pilot.press("f5")
        for _ in range(120):
            await pilot.pause(0.025)
            if len(calls) == 2:
                break
        assert calls == ["m-reference", "m-reference"]
        await pilot.press("escape")


def test_native_factory_uses_exact_saved_desktop_settings_and_no_display(
    setup, tmp_path, monkeypatch
):
    from tests.test_settings_secondpass import _context

    from gucmail.services.notification_configuration import (
        NotificationConfigurationService,
        ProviderConfigurationMetadata,
    )
    from gucmail.services.windows_desktop import desktop_notification_provider

    context = _context(tmp_path)
    context.config = replace(context.config, email=EMAIL, endpoint=ENDPOINT)
    service = NotificationConfigurationService(context.state)
    service.providers.save_provider(
        ProviderConfigurationMetadata(
            "quiet-desktop",
            "desktop",
            "Quiet",
            True,
            metadata={"sound": False, "duration": "long", "group_by_folder": False},
        )
    )
    captured = []

    class Backend:
        def show(self, request):
            captured.append(request)

    monkeypatch.setattr("gucmail.services.windows_desktop.sys.platform", "win32")
    monkeypatch.setattr(
        "gucmail.notification_providers.windows.discover_windows_toast_backend",
        lambda **kwargs: (
            Backend(),
            WindowsBackendStatus(True, "injected", False, False, sound_control=True),
        ),
    )
    provider = desktop_notification_provider(context, provider_id="quiet-desktop")
    assert not captured
    outcome = provider.notify(_event())
    assert outcome.provider == "desktop" and outcome.state is DeliveryState.DELIVERED
    assert captured[0].sound is ToastSound.SILENT and captured[0].duration is ToastDuration.LONG
    assert parse_click_token(captured[0].activation.token).account_key == ACCOUNT
    assert "sender@example.test" not in captured[0].body


@pytest.mark.parametrize("case", ["missing", "draft", "account_changed"])
def test_activation_checks_context_and_mapping_before_credentials(
    setup, tmp_path, monkeypatch, case
):
    from tests.test_settings_secondpass import _context

    from gucmail.toast_entry import open_activation

    identity, _, _ = setup
    context = _context(tmp_path)
    context.config = replace(context.config, email=EMAIL, endpoint=ENDPOINT)
    if case == "account_changed":
        context.config = replace(context.config, email="other@example.test")
    context.state.resolve_reference = lambda *args, **kwargs: (
        None if case == "missing" else {"item_kind": "draft"}
    )
    monkeypatch.setattr("gucmail.bootstrap.AppContext.create", lambda **kwargs: context)
    monkeypatch.setattr(
        type(context), "gateway", lambda self, **kwargs: pytest.fail("Resolved credentials")
    )
    closed = []
    monkeypatch.setattr(type(context), "close", lambda self: closed.append(True))
    with pytest.raises(ConfigurationError):
        open_activation(identity, "m-reference")
    assert closed == [True]


def test_activation_prompts_before_reader_and_closes_after_failure(setup, tmp_path, monkeypatch):
    from tests.test_settings_secondpass import _context

    from gucmail.toast_entry import open_activation

    identity, _, _ = setup
    context = _context(tmp_path)
    context.config = replace(context.config, email=EMAIL, endpoint=ENDPOINT)
    monkeypatch.setattr(
        context.state, "resolve_reference", lambda *a, **kw: {"item_kind": "message"}
    )
    monkeypatch.setattr("gucmail.bootstrap.AppContext.create", lambda **kwargs: context)
    calls = []
    monkeypatch.setattr(type(context), "gateway", lambda self, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(type(context), "close", lambda self: calls.append("close"))

    class Reader:
        def __init__(self, supplied_context, account, reference):
            assert calls == [{"interactive": True, "allow_legacy": False}]
            assert supplied_context is context and account == ACCOUNT and reference == "m-reference"

        def run(self):
            calls.append("reader")
            raise OSError("Synthetic reader failure")

    monkeypatch.setattr("gucmail.tui.notification_reader.NotificationReaderApp", Reader)
    with pytest.raises(OSError, match="Synthetic reader"):
        open_activation(identity, "m-reference")
    assert calls == [{"interactive": True, "allow_legacy": False}, "reader", "close"]


def test_portable_silent_setting_skips_before_optional_sender(tmp_path, monkeypatch):
    from tests.test_settings_secondpass import _context

    from gucmail.services.notification_configuration import (
        NotificationConfigurationService,
        ProviderConfigurationMetadata,
    )
    from gucmail.services.windows_desktop import desktop_notification_provider

    context = _context(tmp_path)
    NotificationConfigurationService(context.state).providers.save_provider(
        ProviderConfigurationMetadata(
            "silent", "desktop", "Silent", True, metadata={"sound": False}
        )
    )
    monkeypatch.setattr("gucmail.services.windows_desktop.sys.platform", "linux")
    monkeypatch.setattr(
        DesktopNotificationProvider,
        "_optional_sender",
        staticmethod(
            lambda: pytest.fail("Silent delivery discovered a potentially audible backend")
        ),
    )
    result = desktop_notification_provider(context, provider_id="silent").notify(_event())
    assert result.state is DeliveryState.SKIPPED and result.attempts == 0
    assert "silent" in result.detail


@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.asyncio
async def test_desktop_guided_save_reopen_cancel_and_runtime_settings(tmp_path, monkeypatch, size):
    from tests.test_notification_provider_filter import _activate, _wait
    from tests.test_settings_secondpass import _context
    from textual.widgets import Checkbox, Input, Select, TabbedContent

    from gucmail.notification_policies import MonitoringProfile
    from gucmail.services.notification_configuration import NotificationConfigurationService
    from gucmail.services.windows_desktop import desktop_notification_provider
    from gucmail.tui.app import GUCmailApp, PaletteScreen
    from gucmail.tui.notification_settings_screen import (
        NotificationSettingsScreen,
        ProviderEditorScreen,
    )

    context = _context(tmp_path)
    context.config = replace(context.config, email=EMAIL, endpoint=ENDPOINT)
    service = NotificationConfigurationService(context.state)
    service.profiles.save_profile(MonitoringProfile("default", "Default", ("Inbox",)))
    monkeypatch.setattr(
        context.credential_resolver, "resolve", lambda *a, **kw: pytest.fail("Credentials")
    )
    captured = []

    class Backend:
        def show(self, request):
            captured.append(request)

    monkeypatch.setattr("gucmail.services.windows_desktop.sys.platform", "win32")
    monkeypatch.setattr(
        "gucmail.notification_providers.windows.discover_windows_toast_backend",
        lambda **kwargs: (Backend(), WindowsBackendStatus(True, "injected", False, False)),
    )
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        await pilot.press("ctrl+k")
        await _wait(pilot, lambda: isinstance(app.screen, PaletteScreen))
        app.screen.query_one("#palette-input", Input).value = "Open Notifications"
        await pilot.press("enter")
        await _wait(
            pilot,
            lambda: (
                isinstance(app.screen, NotificationSettingsScreen)
                and not app.screen._operation_active
                and bool(app.screen._profiles)
            ),
        )
        screen = app.screen
        screen.query_one(TabbedContent).active = "notification-provider-tab"
        await pilot.pause()
        await _activate(pilot, screen, "#notification-provider-new")
        await _wait(pilot, lambda: isinstance(app.screen, ProviderEditorScreen))
        editor = app.screen
        await pilot.pause()
        assert editor.query_one("#notification-desktop-options").region.height > 0

        async def focus_option(selector):
            from textual.containers import VerticalScroll

            widget = editor.query_one(selector)
            widget.focus()
            pane = editor.query_one("#notification-provider-dialog", VerticalScroll)
            await _wait(
                pilot,
                lambda: (
                    widget.has_focus
                    and editor.query_one(
                        "#notification-desktop-options"
                    ).content_region.contains_region(widget.region)
                    and pane.content_region.contains_region(widget.region)
                    and editor.region.contains_region(widget.region)
                ),
            )

        editor.query_one("#notification-provider-id", Input).value = "guided-desktop"
        editor.query_one("#notification-provider-name", Input).value = "Guided desktop"
        editor.query_one("#notification-provider-enabled", Checkbox).value = True
        await focus_option("#notification-desktop-title")
        await pilot.press("home", "shift+end", "backspace", *"Private desktop")
        await focus_option("#notification-desktop-duration")
        await pilot.press("enter", "down", "enter")
        assert editor.query_one("#notification-desktop-duration", Select).value == "long"
        for key in ("sound", "group", "attachment", "importance"):
            await focus_option("#notification-desktop-" + key)
            await pilot.press("space")
            assert not editor.query_one("#notification-desktop-" + key, Checkbox).value
        await _activate(pilot, editor, "#notification-provider-save")
        await _wait(pilot, lambda: app.screen is screen and not screen._operation_active)
        metadata = service.providers.get_provider("guided-desktop")
        assert metadata.metadata == {
            "title": "Private desktop",
            "duration": "long",
            "sound": False,
            "group_by_folder": False,
            "show_attachment_indicator": False,
            "show_importance_indicator": False,
        }
        await _activate(pilot, screen, "#notification-provider-edit")
        await _wait(pilot, lambda: isinstance(app.screen, ProviderEditorScreen))
        editor = app.screen
        assert editor.query_one("#notification-desktop-duration", Select).value == "long"
        assert not editor.query_one("#notification-desktop-sound", Checkbox).value
        editor.query_one("#notification-desktop-sound", Checkbox).value = True
        editor.query_one("#notification-desktop-title", Input).value = "Unsaved"
        await _activate(pilot, editor, "#notification-provider-cancel")
        await _wait(pilot, lambda: app.screen is screen)
        assert service.providers.get_provider("guided-desktop") == metadata
        assert not captured
        provider = desktop_notification_provider(context, provider_id="guided-desktop")
        provider.notify(_event())
        assert captured[0].sound is ToastSound.SILENT and captured[0].duration is ToastDuration.LONG
        assert provider._windows_provider.settings.group_by_folder is False
        assert provider._windows_provider.settings.show_attachment_indicator is False
        assert provider._windows_provider.settings.show_importance_indicator is False
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_invalid_desktop_metadata_can_be_reviewed_and_cancelled_without_replacement():
    from tests.test_notification_provider_filter import EditorHarness, _activate, _wait
    from textual.widgets import Checkbox, Static

    from gucmail.services.notification_configuration import ProviderConfigurationMetadata
    from gucmail.tui.notification_settings_screen import ProviderEditorScreen

    metadata = ProviderConfigurationMetadata(
        "bad-desktop",
        "desktop",
        "Invalid",
        True,
        metadata={"duration": ["invalid"], "sound": "false", "title": "\x1b[31munsafe"},
    )
    original = copy.deepcopy(metadata.metadata)
    app = EditorHarness()
    results = []
    async with app.run_test(size=(70, 20)) as pilot:
        editor = ProviderEditorScreen(metadata, None, digest_ids=())
        await app.push_screen(editor, results.append)
        await pilot.pause()
        assert "invalid" in str(editor.query_one("#notification-desktop-warning", Static).render())
        assert not editor.query_one("#notification-desktop-sound", Checkbox).value
        await _activate(pilot, editor, "#notification-provider-cancel")
        await _wait(pilot, lambda: results == [None])
        assert metadata.metadata == original


@pytest.mark.parametrize("bad", [None, "child", "type", "count"])
def test_current_user_registry_adapter_limits_exact_keys_and_preserves_unknown_data(tmp_path, bad):
    from gucmail.services.windows_desktop import CurrentUserDesktopRegistry

    identity = DesktopIdentity(
        tmp_path / "config.toml",
        tmp_path / "data",
        tmp_path / "cache",
        tmp_path / "logs",
        tmp_path / "python.exe",
        ACCOUNT,
    )
    expected = identity.registry_values
    calls = []

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class API:
        HKEY_CURRENT_USER = object()
        KEY_READ = 1
        KEY_WRITE = 2
        REG_SZ = 3

        def OpenKey(self, root, path, reserved, access):
            assert root is self.HKEY_CURRENT_USER and path in expected
            assert reserved == 0 and access == self.KEY_READ
            calls.append(("read", path))
            return Key(path)

        def QueryInfoKey(self, key):
            children = self.children(key)
            return len(children), 21 if bad == "count" else len(expected[key.path]), 0

        def children(self, key):
            if bad == "child":
                return ["UnownedChild"]
            return [
                path[len(key.path) + 1 :]
                for path in expected
                if path.startswith(key.path + "\\") and "\\" not in path[len(key.path) + 1 :]
            ]

        def EnumKey(self, key, index):
            return self.children(key)[index]

        def EnumValue(self, key, index):
            name, value = tuple(expected[key.path].items())[index]
            return name, value, 99 if bad == "type" else self.REG_SZ

        def CreateKeyEx(self, root, path, reserved, access):
            assert root is self.HKEY_CURRENT_USER and path in expected
            assert reserved == 0 and access == self.KEY_WRITE
            return Key(path)

        def SetValueEx(self, key, name, reserved, kind, value):
            assert reserved == 0 and kind == self.REG_SZ and expected[key.path][name] == value
            calls.append(("write", key.path, name, value))

        def DeleteKey(self, root, path):
            assert root is self.HKEY_CURRENT_USER and path in expected
            calls.append(("delete", path))

    adapter = object.__new__(CurrentUserDesktopRegistry)
    adapter.api = API()
    if bad:
        with pytest.raises(ConfigurationError):
            adapter.read(tuple(expected))
        assert all(call[0] == "read" for call in calls)
    else:
        assert adapter.read(tuple(expected)) == expected
        adapter.write(expected)
        adapter.remove(tuple(expected))
        deleted = [call[1] for call in calls if call[0] == "delete"]
        assert deleted[0].endswith("\\shell\\open\\command")
        for path in deleted:
            assert all(
                deleted.index(child) < deleted.index(path)
                for child in deleted
                if child.startswith(path + "\\")
            )


def test_profile_desktop_factory_ignores_unrelated_invalid_provider_and_keeps_disclosure(
    tmp_path, monkeypatch
):
    from tests.test_settings_secondpass import _context

    from gucmail.cli.core import (
        profiled_notification_dispatcher as _profiled_notification_dispatcher,
    )
    from gucmail.notification_events import EventDisclosurePolicy
    from gucmail.notification_policies import MonitoringProfile, ProviderPolicy
    from gucmail.notification_rules import (
        NotificationRule,
        RuleAction,
        RuleActionKind,
        TextCondition,
        TextField,
        TextOperator,
    )
    from gucmail.services.notification_configuration import (
        NotificationConfigurationService,
        ProviderConfigurationMetadata,
    )
    from gucmail.services.notifications import DeliveryResult, ProviderBinding

    context = _context(tmp_path)
    context.config = replace(context.config, email=EMAIL, endpoint=ENDPOINT)
    service = NotificationConfigurationService(context.state)
    rule = NotificationRule(
        "inbox",
        "Inbox",
        TextCondition(TextField.FOLDER, TextOperator.EXACT, ("Inbox",)),
        (RuleAction(RuleActionKind.ROUTE_IMMEDIATE, "private-desktop"),),
        enabled=True,
    )
    service.profiles.save_profile(
        MonitoringProfile(
            "private",
            "Private",
            ("Inbox",),
            enabled=True,
            rules=(rule,),
            providers=(
                ProviderPolicy(
                    "private-desktop", True, privacy=EventDisclosurePolicy(PrivacyMode.PRIVATE)
                ),
            ),
        )
    )
    for provider_id in ("private-desktop", "unrelated-broken"):
        service.providers.save_provider(
            ProviderConfigurationMetadata(
                provider_id,
                "desktop",
                provider_id,
                True,
                metadata={"duration": "invalid"} if provider_id == "unrelated-broken" else {},
            )
        )
    selected = []
    captured = []

    class Capture:
        name = "desktop"

        def notify(self, event):
            captured.append(event)
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    def factory(context, *, provider_id):
        assert provider_id == "private-desktop"
        selected.append(provider_id)
        return Capture()

    monkeypatch.setattr("gucmail.cli.core.desktop_notification_provider", factory)
    dispatcher = _profiled_notification_dispatcher(
        context,
        [
            ProviderBinding(
                DesktopNotificationProvider(sender=lambda *a: pytest.fail("Wrong adapter"))
            )
        ],
        profile_id="private",
    )
    dispatcher.dispatch(_event())
    assert selected == ["private-desktop"] and len(captured) == 1
    assert (
        captured[0].subject is None and captured[0].sender is None and captured[0].preview is None
    )
    dispatcher.dispatch(_event())
    assert len(captured) == 1


@pytest.mark.parametrize(
    "case", ["sender_subject", "private", "missing", "draft", "wrong_owner", "unavailable"]
)
def test_privacy_hidden_account_activation_requires_owned_local_mapping(
    tmp_path, monkeypatch, case
):
    from tests.test_settings_secondpass import _context

    from gucmail.notification_events import EventDisclosurePolicy
    from gucmail.notification_events import PrivacyMode as DisclosureMode
    from gucmail.services.profiled_notifications import disclosed_event
    from gucmail.services.windows_desktop import desktop_notification_provider

    context = _context(tmp_path)
    context.config = replace(context.config, email=EMAIL, endpoint=ENDPOINT)
    context.state = StateStore(
        tmp_path / "activation.db", account_key="other" if case == "wrong_owner" else ACCOUNT
    )
    if case != "missing":
        context.state.put_reference(
            "m-reference", "draft" if case == "draft" else "message", "synthetic-ews-id"
        )
    if case == "unavailable":
        monkeypatch.setattr(
            context.state,
            "resolve_reference",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Synthetic unavailable state")),
        )
    requests = []

    class Backend:
        def show(self, request):
            requests.append(request)

    monkeypatch.setattr("gucmail.services.windows_desktop.sys.platform", "win32")
    monkeypatch.setattr(
        "gucmail.notification_providers.windows.discover_windows_toast_backend",
        lambda **kwargs: (Backend(), WindowsBackendStatus(True, "injected", False, False)),
    )
    event = disclosed_event(
        _event(),
        EventDisclosurePolicy(
            DisclosureMode.PRIVATE if case == "private" else DisclosureMode.SENDER_AND_SUBJECT
        ),
    )
    assert event.account_id == "configured-mailbox"
    outcome = desktop_notification_provider(context).notify(event)
    assert outcome.state is DeliveryState.DELIVERED and len(requests) == 1
    if case == "sender_subject":
        assert parse_click_token(requests[0].activation.token).account_key == ACCOUNT
    else:
        assert requests[0].activation is None
    assert EMAIL not in requests[0].body and EMAIL not in requests[0].title


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_desktop_setup_route_waits_for_actual_notification_read(
    setup, tmp_path, monkeypatch, size
):
    import asyncio
    from threading import Event

    from tests.test_settings_secondpass import _context
    from textual.widgets import Button, TabbedContent

    from gucmail.services.notification_configuration import PersistentMonitoringProfileRepository
    from gucmail.tui.app import GUCmailApp
    from gucmail.tui.area_common import ConfirmationScreen, ViewState
    from gucmail.tui.desktop_setup_screen import DesktopSetupScreen
    from gucmail.tui.notification_settings_screen import NotificationSettingsScreen

    identity, registry, service = setup
    context = _context(tmp_path)
    started, release = Event(), Event()
    inspect = PersistentMonitoringProfileRepository.inspect
    reads = []
    read = registry.read

    def counted(paths):
        reads.append(tuple(paths))
        return read(paths)

    def held(repository):
        started.set()
        if not release.wait(20):
            raise TimeoutError("Synthetic local notification read hold expired")
        return inspect(repository)

    monkeypatch.setattr(registry, "read", counted)
    monkeypatch.setattr(PersistentMonitoringProfileRepository, "inspect", held)
    monkeypatch.setattr(
        "gucmail.services.windows_desktop.desktop_setup_service", lambda *a: service
    )
    monkeypatch.setattr(
        context.credential_resolver,
        "resolve",
        lambda *a, **kw: pytest.fail("Setup resolved credentials"),
    )
    app = GUCmailApp(context, offline=True)
    try:
        async with app.run_test(size=size) as pilot:
            app._open_area("notifications")
            async with asyncio.timeout(15):
                while not (isinstance(app.screen, NotificationSettingsScreen) and started.is_set()):
                    await asyncio.sleep(0.005)
            screen = app.screen
            screen.query_one(TabbedContent).active = "notification-provider-tab"
            await pilot.pause()
            button = screen.query_one("#notification-desktop-setup", Button)
            assert screen._operation_active and button.disabled
            button.press()
            await pilot.pause()
            assert app.screen is screen and not reads and not registry.calls
            assert not identity.manifest.exists()
            release.set()
            async with asyncio.timeout(15):
                while (
                    screen._operation_active
                    or button.disabled
                    or screen.view_state in {ViewState.IDLE, ViewState.LOADING}
                ):
                    await asyncio.sleep(0.005)
            await pilot.pause()
            assert app.screen is screen and not reads and not registry.calls
            button.focus()
            await pilot.press("enter")
            async with asyncio.timeout(15):
                while not (
                    isinstance(app.screen, DesktopSetupScreen)
                    and app.screen.view_state is ViewState.READY
                    and not app.screen._operation_active
                ):
                    await asyncio.sleep(0.005)
            desktop = app.screen
            assert reads and not registry.calls and not identity.manifest.exists()
            desktop.query_one("#desktop-setup-enable", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmationScreen)
            assert not registry.calls and not identity.manifest.exists()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is desktop
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is screen
            assert not registry.calls and not identity.manifest.exists()
    finally:
        release.set()
        context.close()


def test_withheld_sender_and_subject_never_claims_no_subject():
    event = replace(_event(), sender=None, subject=None, preview=None)
    request = build_windows_toast(event)
    assert "(No subject)" not in request.body
    assert request.body == "You have a new message."


def test_genuinely_empty_subject_still_says_no_subject():
    request = build_windows_toast(replace(_event(), subject=""))
    assert request.body.startswith("(No subject)")
