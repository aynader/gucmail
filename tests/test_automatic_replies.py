"""Automatic replies: installed serialization, exact review, conflicts and uncertain readback."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from exchangelib.settings import OofSettings
from exchangelib.version import Build, Version
from textual.app import App
from textual.widgets import Button, Select, Static, TextArea
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.config import AppConfig
from gucmail.errors import ConfigurationError, ConfirmationRequiredError, ConflictError
from gucmail.ews.adapter import EWSGateway
from gucmail.services.automatic_replies import (
    AutomaticReplyService,
    AutomaticReplySettings,
    AutomaticReplyState,
    AutomaticReplyVerificationError,
    ExternalReplyAudience,
    parse_reply_time,
)
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen
from gucmail.tui.automatic_replies_screen import AutomaticRepliesScreen, AutomaticReplyEditor
from gucmail.tui.notification_settings_screen import NotificationSettingsScreen
from test_cli import _environment
from test_settings_secondpass import _context
from test_tui import FakeGateway


class _Gateway:
    def __init__(self):
        self.settings = AutomaticReplySettings()
        self.reads = 0
        self.writes = []
        self.readback_fails = False
        self.ignore_write = False

    def get_automatic_replies(self):
        self.reads += 1
        if self.writes and self.readback_fails:
            raise TimeoutError("synthetic readback error")
        return self.settings

    def set_automatic_replies(self, settings):
        self.writes.append(settings)
        if not self.ignore_write:
            self.settings = settings


def _enabled():
    return AutomaticReplySettings(
        AutomaticReplyState.ENABLED,
        ExternalReplyAudience.NONE,
        "Back on Monday.",
    )


def test_policy_requires_confirmation_rechecks_preview_and_verifies_readback():
    gateway = _Gateway()
    service = AutomaticReplyService(gateway)
    plan = service.plan(_enabled())
    with pytest.raises(ConfirmationRequiredError):
        service.apply(plan)
    assert gateway.reads == 1 and not gateway.writes
    result = service.apply(plan, confirmed=True)
    assert result == _enabled()
    assert gateway.reads == 3 and len(gateway.writes) == 1
    stale = service.plan(replace(result, internal_reply="Changed reply"))
    gateway.settings = replace(result, internal_reply="Updated elsewhere")
    with pytest.raises(ConflictError):
        service.apply(stale, confirmed=True)
    assert len(gateway.writes) == 1


@pytest.mark.parametrize("failure", ["readback", "mismatch"])
def test_accepted_write_with_failed_verification_never_replays(failure):
    gateway = _Gateway()
    service = AutomaticReplyService(gateway)
    plan = service.plan(_enabled())
    gateway.readback_fails = failure == "readback"
    gateway.ignore_write = failure == "mismatch"
    with pytest.raises(AutomaticReplyVerificationError) as error:
        service.apply(plan, confirmed=True)
    assert len(gateway.writes) == 1
    assert error.value.context.retry_safe is False
    assert error.value.context.mutation_uncertain is True


@pytest.mark.parametrize("value", ["2026-10-02", "2026-10-02T12:00:00", "invalid"])
def test_naive_or_invalid_schedule_is_rejected_before_authentication(value, monkeypatch, tmp_path):
    def unexpected_gateway(*args, **kwargs):
        pytest.fail("Invalid local input must not resolve a gateway.")

    monkeypatch.setattr(cli, "_gateway", unexpected_gateway)
    result = CliRunner().invoke(
        cli.app,
        ["automatic-replies", "set", "--state", "scheduled", "--start", value],
        env=_environment(tmp_path),
    )
    assert result.exit_code == 3, result.output


@pytest.mark.parametrize(
    "settings",
    [
        AutomaticReplySettings(AutomaticReplyState.ENABLED),
        replace(_enabled(), external_audience=ExternalReplyAudience.ALL),
        replace(_enabled(), state=AutomaticReplyState.SCHEDULED),
        replace(_enabled(), internal_reply="x" * 131073),
    ],
)
def test_invalid_policy_causes_zero_reads_and_writes(settings):
    gateway = _Gateway()
    with pytest.raises(ConfigurationError):
        AutomaticReplyService(gateway).plan(settings)
    assert gateway.reads == 0 and not gateway.writes


@pytest.mark.parametrize("state", list(AutomaticReplyState))
def test_real_ews_serializer_preserves_policy_and_never_copies_internal_text_externally(state):
    now = datetime.now(UTC)
    settings = replace(
        _enabled(), state=state, start=now + timedelta(days=1), end=now + timedelta(days=2)
    )
    account = SimpleNamespace(oof_settings=None)
    gateway = EWSGateway(AppConfig(), account_factory=lambda: account)
    gateway.set_automatic_replies(settings)
    value = account.oof_settings
    assert isinstance(value, OofSettings)
    wire = value.to_xml(Version(build=Build(15, 1, 2375, 7)))
    assert wire.find("{*}OofState").text == state.value.title()
    assert wire.find("{*}ExternalAudience").text == "None"
    assert "Back on Monday" not in wire.find("{*}ExternalReply/{*}Message").text
    loaded = gateway.get_automatic_replies()
    assert loaded.fingerprint == settings.fingerprint


def test_cli_dry_run_noninteractive_confirmation_and_scheduled_json(tmp_path, monkeypatch):
    gateway = _Gateway()
    monkeypatch.setattr(cli, "_gateway", lambda *args, **kwargs: gateway)
    internal = tmp_path / "internal.txt"
    internal.write_text("Back on Monday.", encoding="utf-8")
    base = [
        "automatic-replies",
        "set",
        "--state",
        "scheduled",
        "--external-audience",
        "none",
        "--internal-reply-file",
        str(internal),
        "--start",
        "2027-01-02T09:00:00+03:00",
        "--end",
        "2027-01-05T17:00:00+03:00",
    ]
    runner = CliRunner()
    dry = runner.invoke(cli.app, ["--json", *base, "--dry-run"], env=_environment(tmp_path))
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["data"]["dry_run"] is True
    assert not gateway.writes
    rejected = runner.invoke(cli.app, ["--non-interactive", *base], env=_environment(tmp_path))
    assert rejected.exit_code == 9 and not gateway.writes
    applied = runner.invoke(cli.app, ["--json", "--yes", *base], env=_environment(tmp_path))
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["data"]["state"] == "scheduled"
    assert gateway.settings.start == parse_reply_time("2027-01-02T09:00:00+03:00")
    assert len(gateway.writes) == 1


class _Harness(App):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def on_mount(self):
        self.push_screen(AutomaticRepliesScreen(self.service))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_mounted_policy_editor_reviews_then_confirms_and_cancel_does_not_write(size):
    gateway = _Gateway()
    app = _Harness(AutomaticReplyService(gateway))
    async with app.run_test(size=size) as pilot:
        for _ in range(30):
            await pilot.pause(0.05)
            if app.screen.current is not None:
                break
        screen = app.screen
        screen.query_one("#oof-edit", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, AutomaticReplyEditor)
        await pilot.press("escape")
        assert not gateway.writes
        screen.action_edit()
        await pilot.pause()
        app.screen.query_one("#oof-state", Select).value = "enabled"
        app.screen.query_one("#oof-internal", TextArea).load_text("Back on Monday.")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "External audience: none" in app.screen.detail_text
        assert not gateway.writes
        await pilot.press("y")
        for _ in range(30):
            await pilot.pause(0.05)
            if gateway.writes and app.screen.current.state is AutomaticReplyState.ENABLED:
                break
        assert len(gateway.writes) == 1
        assert "State: enabled" in str(screen.query_one("#oof-details", Static).content)


@pytest.mark.asyncio
async def test_actual_mail_app_navigates_from_notifications_to_live_policy_surface(tmp_path):
    class Gateway(FakeGateway, _Gateway):
        def __init__(self):
            _Gateway.__init__(self)

    gateway = Gateway()
    app = GUCmailApp(context=_context(tmp_path), gateway=gateway)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert gateway.reads == 0
        app._open_area("notifications")
        await pilot.pause()
        assert isinstance(app.screen, NotificationSettingsScreen)
        # A mounted workspace can still be loading its configuration. Its
        # controls are deliberately disabled until that read completes.
        screen = app.screen
        async with asyncio.timeout(15):
            while (
                not screen.is_mounted or screen._operation_active
                or screen.view_state.value in {"idle", "loading"}
            ):
                await asyncio.sleep(0.005)
        await pilot.pause()
        button = screen.query_one("#notification-automatic-replies", Button)
        assert not button.disabled
        button.press()
        for _ in range(30):
            await pilot.pause(0.05)
            if isinstance(app.screen, AutomaticRepliesScreen) and app.screen.current is not None:
                break
        assert isinstance(app.screen, AutomaticRepliesScreen)
        assert gateway.reads == 1 and not gateway.writes


@pytest.mark.asyncio
async def test_notification_automatic_reply_route_waits_for_actual_configuration_read(
    tmp_path, monkeypatch
):
    from threading import Event

    class Gateway(FakeGateway, _Gateway):
        def __init__(self):
            _Gateway.__init__(self)

    started, release = Event(), Event()
    load = NotificationSettingsScreen._load_configuration

    def held(screen, *args):
        started.set()
        if not release.wait(20):
            raise TimeoutError("Synthetic notification configuration hold expired")
        return load(screen, *args)

    monkeypatch.setattr(NotificationSettingsScreen, "_load_configuration", held)
    gateway = Gateway()
    app = GUCmailApp(context=_context(tmp_path), gateway=gateway)
    try:
        async with app.run_test(size=(100, 35)) as pilot:
            app._open_area("notifications")
            async with asyncio.timeout(15):
                while not (isinstance(app.screen, NotificationSettingsScreen) and started.is_set()):
                    await asyncio.sleep(0.005)
            screen = app.screen
            button = screen.query_one("#notification-automatic-replies", Button)
            assert screen._operation_active and button.disabled
            assert gateway.reads == 0 and not gateway.writes
            release.set()
            async with asyncio.timeout(15):
                while screen._operation_active or button.disabled:
                    await asyncio.sleep(0.005)
            button.focus()
            await pilot.press("enter")
            async with asyncio.timeout(15):
                while not (isinstance(app.screen, AutomaticRepliesScreen)
                           and app.screen.current is not None):
                    await asyncio.sleep(0.005)
            assert gateway.reads == 1 and not gateway.writes
    finally:
        release.set()
