"""The one-switch notification setup (C2), with the Windows parts replaced by fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from gucmail.bootstrap import AppContext
from gucmail.config import AppConfig, AppPaths, ConfigStore
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.errors import ConfigurationError
from gucmail.headless import run_headless_cycles
from gucmail.services.notification_switch import NotificationSwitch
from gucmail.state import StateStore
from waiting import wait_until


def make_context(tmp_path: Path) -> AppContext:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        config_file=tmp_path / "config" / "config.toml",
        state_database=tmp_path / "data" / "state.db",
        recovery_dir=tmp_path / "data" / "recovery",
        index_database=tmp_path / "data" / "index.db",
        backup_dir=tmp_path / "data" / "backups",
    )
    store = ConfigStore(paths)
    config = AppConfig(email="demo@example.test")
    store.save(config)
    return AppContext(
        paths=paths,
        config_store=store,
        config=config,
        state=StateStore(paths.state_database),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


@dataclass
class FakeStartup:
    installed: bool = False
    enabled: bool = False
    known: bool = True
    issues: tuple[str, ...] = ()
    applied: list[str] = field(default_factory=list)
    fail: str | None = None

    def preview(self, *, operation: str | None = None):
        inspection = SimpleNamespace(
            state_known=self.known,
            installed=self.installed,
            enabled=self.enabled,
            issues=self.issues,
            detail="Task Scheduler is unavailable." if not self.known else None,
        )
        plan = None
        if operation is not None:
            no_changes = (operation == "disable" and not self.enabled) or (
                operation == "enable" and self.enabled
            )
            plan = SimpleNamespace(no_changes=no_changes, can_apply=True, reason="", op=operation)
        return SimpleNamespace(inspection=inspection, operation=plan)

    def apply(self, preview, *, confirmed):
        assert confirmed
        if self.fail:
            raise ConfigurationError(self.fail)
        operation = preview.operation.op
        self.applied.append(operation)
        if operation in {"enable", "repair"}:
            self.installed, self.enabled, self.issues = True, True, ()
        elif operation == "disable":
            self.enabled = False
        return {"applied": True}


@dataclass
class FakeDesktop:
    installed: bool = False
    can_apply: bool = True
    applied: int = 0

    def preview(self, operation="setup"):
        return SimpleNamespace(
            inspection=SimpleNamespace(known=True, installed=self.installed),
            can_apply=self.can_apply,
            reason="Another app owns this registration.",
        )

    def apply(self, preview, *, confirmed):
        assert confirmed
        self.applied += 1
        self.installed = True


@dataclass
class FakeOldScheduler:
    present: bool = False
    removed: int = 0

    def installed(self):
        return self.present

    def remove(self):
        self.removed += 1
        self.present = False


def switch(tmp_path, **overrides):
    parts = {
        "startup": FakeStartup(),
        "desktop_setup": FakeDesktop(),
        "old_scheduler": FakeOldScheduler(),
        "send_test": lambda: True,
        "windows": True,
    }
    parts.update(overrides)
    return NotificationSwitch(make_context(tmp_path), **parts), parts


def test_turning_on_sets_up_everything_and_reports_working(tmp_path):
    service, parts = switch(tmp_path)
    assert not service.status().on

    result = service.turn_on()

    assert result.working, result.as_dict()
    assert [step.label for step in result.steps] == [
        "Notification settings",
        "Windows registration",
        "Background task",
        "Test notification",
    ]
    assert result.summary.startswith("✓ Working")
    assert parts["desktop_setup"].applied == 1
    assert parts["startup"].applied == ["enable"]
    status = service.status()
    assert status.on and status.task_running


def test_turning_on_twice_changes_nothing_the_second_time(tmp_path):
    service, parts = switch(tmp_path)
    service.turn_on()
    again = service.turn_on(test=False)
    assert again.working
    assert parts["startup"].applied == ["enable"]
    assert parts["desktop_setup"].applied == 1
    assert "Already" in again.steps[1].detail and "Already" in again.steps[2].detail


def test_a_drifted_task_is_repaired(tmp_path):
    service, parts = switch(tmp_path, startup=FakeStartup(installed=True, issues=("drift",)))
    assert service.turn_on(test=False).working
    assert parts["startup"].applied == ["repair"]


def test_failures_are_reported_in_plain_words_and_skip_the_test_toast(tmp_path):
    toasts = []
    service, _ = switch(
        tmp_path,
        startup=FakeStartup(fail="Task Scheduler refused the change."),
        send_test=lambda: toasts.append(1) or True,
    )
    result = service.turn_on()
    assert not result.working
    failed = [step for step in result.steps if not step.ok]
    assert [step.label for step in failed] == ["Background task"]
    assert failed[0].detail == "Task Scheduler refused the change."
    assert not toasts
    assert "needs attention" in result.summary


def test_windows_registration_owned_by_someone_else_is_left_alone(tmp_path):
    service, parts = switch(tmp_path, desktop_setup=FakeDesktop(can_apply=False))
    result = service.turn_on(test=False)
    registration = result.steps[1]
    assert not registration.ok and "Another app" in registration.detail
    assert parts["desktop_setup"].applied == 0


def test_a_toast_windows_does_not_show_is_reported(tmp_path):
    service, _ = switch(tmp_path, send_test=lambda: False)
    result = service.turn_on()
    assert not result.steps[-1].ok
    assert "Windows Settings" in result.steps[-1].detail


def test_the_old_scheduler_task_is_replaced_by_the_background_task(tmp_path):
    service, parts = switch(tmp_path, old_scheduler=FakeOldScheduler(present=True))
    result = service.turn_on(test=False)
    assert parts["old_scheduler"].removed == 1
    assert any(step.label == "Old Scheduler task" and step.ok for step in result.steps)


def test_off_stops_the_task_when_nothing_is_scheduled(tmp_path):
    service, parts = switch(tmp_path)
    service.turn_on(test=False)
    result = service.turn_off()
    assert result.summary == "Notifications are off."
    assert parts["startup"].applied == ["enable", "disable"]
    assert not service.status().on


def test_off_keeps_the_task_running_while_scheduled_mail_waits(tmp_path, monkeypatch):
    service, parts = switch(tmp_path)
    service.turn_on(test=False)
    monkeypatch.setattr(service, "pending_scheduled", lambda: 2)
    result = service.turn_off()
    assert "2 scheduled message(s)" in result.steps[-1].detail
    assert parts["startup"].applied == ["enable"]
    assert "still running to send scheduled mail" in service.status().detail


def test_outside_windows_there_is_no_registration_or_toast_step(tmp_path):
    service, _ = switch(tmp_path, windows=False)
    labels = [step.label for step in service.turn_on().steps]
    assert labels == ["Notification settings", "Background task"]


def test_pending_scheduled_counts_only_messages_still_to_send(tmp_path):
    service, _ = switch(tmp_path)
    assert service.pending_scheduled() == 0


def test_watcher_sends_scheduled_mail_between_mail_checks():
    """A 5-minute mail interval must not delay scheduled mail by 5 minutes."""

    now = [0.0]
    ticks = []

    class Controller:
        stop_requested = False
        paused = False

        def wait(self, seconds):
            now[0] += seconds
            if now[0] >= 120:
                self.stop_requested = True

    class Runtime:
        controller = Controller()
        spec = SimpleNamespace(poll_interval_seconds=300)

        def running(self):
            from contextlib import nullcontext

            return nullcontext()

        def consume_control_request(self):
            return None

        def consume_sync_request(self):
            return None

        def heartbeat(self, result=None):
            pass

    result = SimpleNamespace(
        backoff_seconds=0, next_poll_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    run_headless_cycles(
        Runtime(),
        lambda: result,
        monotonic=lambda: now[0],
        tick=lambda: ticks.append(now[0]),
    )
    # One tick right after the mail check, then one every 30 seconds while waiting.
    assert ticks == [0.0, 30.0, 60.0, 90.0, 120.0]


def test_cli_on_off_and_status_use_plain_words(tmp_path, monkeypatch):
    from gucmail import cli
    from gucmail.cli import notifications as module

    service, _ = switch(tmp_path)
    monkeypatch.setattr(module, "_switch", lambda ctx: service)
    env = {
        "GUCMAIL_CONFIG_DIR": str(tmp_path / "c"),
        "GUCMAIL_CONFIG": str(tmp_path / "c" / "config.toml"),
        "GUCMAIL_DATA_DIR": str(tmp_path / "d"),
        "GUCMAIL_CACHE_DIR": str(tmp_path / "k"),
        "GUCMAIL_LOG_DIR": str(tmp_path / "l"),
    }
    runner = CliRunner()

    on = runner.invoke(cli.app, ["--no-color", "notifications", "on"], env=env)
    assert on.exit_code == 0, on.output
    assert "Working" in on.output and "Background task" in on.output

    status = runner.invoke(cli.app, ["--json", "notifications", "status"], env=env)
    assert status.exit_code == 0, status.output
    assert '"notifications": "on"' in status.output

    off = runner.invoke(cli.app, ["--no-color", "notifications", "off"], env=env)
    assert off.exit_code == 0, off.output
    assert "Notifications are off." in off.output


@pytest.mark.asyncio
async def test_the_settings_screen_switch_runs_the_whole_setup(tmp_path):
    from textual.widgets import Static, Switch

    from gucmail.tui.notification_settings_screen import NotificationSettingsScreen
    from test_notification_settings_screen_secondpass import NotificationSettingsHarness

    service, parts = switch(tmp_path)
    screen = NotificationSettingsScreen(service.settings, switch=service)
    app = NotificationSettingsHarness(screen)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_until(
            pilot,
            lambda: screen._profiles or not screen._operation_active,
            interval=0.05,
            pause_first=True,
            timeout=15,
        )
        toggle = screen.query_one("#notification-switch", Switch)
        assert toggle.value is False
        toggle.value = True
        await wait_until(
            pilot,
            lambda: parts["startup"].applied and not toggle.disabled,
            interval=0.05,
            pause_first=True,
            timeout=15,
        )
        steps = str(screen.query_one("#notification-switch-steps", Static).content)
        assert "✓ Background task" in steps and "✓ Test notification" in steps
        assert "Working" in str(screen.query_one("#notification-switch-status", Static).content)
        assert toggle.value is True

        # The palette command takes the same path.
        screen.action_set_desktop_notifications(False)
        await wait_until(
            pilot,
            lambda: "disable" in parts["startup"].applied and not toggle.disabled,
            interval=0.05,
            pause_first=True,
            timeout=15,
        )
        assert toggle.value is False
        assert not service.notifications_on()
