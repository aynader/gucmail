from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gucmail.errors import ConflictError
from gucmail.services import startup_runtime as runtime
from gucmail.startup import (
    InstalledStartupSnapshot,
    generate_startup_plan,
    inspect_startup,
    plan_enable_startup,
)
from gucmail.watcher_entry import main
from test_settings_secondpass import _context


def test_watcher_process_binds_paths_and_child_entry_uses_them(tmp_path: Path, monkeypatch) -> None:
    context = _context(tmp_path)
    spec = runtime.context_watcher_process(context)
    assert spec.arguments[:2] == ("-m", "gucmail.watcher_entry")
    invoked = []
    import gucmail.cli

    monkeypatch.setattr(gucmail.cli, "app", lambda **kwargs: invoked.append(kwargs))
    for key in (
        "GUCMAIL_CONFIG",
        "GUCMAIL_CONFIG_DIR",
        "GUCMAIL_DATA_DIR",
        "GUCMAIL_CACHE_DIR",
        "GUCMAIL_LOG_DIR",
    ):
        monkeypatch.setenv(key, "stale-parent-path")
    main(list(spec.arguments[2:]))
    assert os.environ["GUCMAIL_CONFIG"] == str(context.paths.config_file.resolve())
    assert os.environ["GUCMAIL_DATA_DIR"] == str(context.paths.data_dir.resolve())
    assert invoked[0]["args"] == [
        "--non-interactive",
        "headless",
        "run",
        "--profile",
        "default",
        "--forever",
    ]
    assert (
        str(context.paths.data_dir.resolve())
        in generate_startup_plan(
            spec, platform_name="windows", windows_user_sid="S-1-5-21-100-200-300-1001"
        )
        .artifacts[0]
        .content
    )
    systemd = generate_startup_plan(spec, platform_name="linux").artifacts[0].content
    assert "ProtectSystem=strict" in systemd
    assert systemd.count("ReadWritePaths=") == 4
    assert "password" not in " ".join(spec.arguments).casefold()


def test_windows_inspection_ignores_xml_serialization_but_detects_changed_action(
    tmp_path: Path,
) -> None:
    spec = runtime.context_watcher_process(_context(tmp_path))
    plan = generate_startup_plan(
        spec,
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=tmp_path / "startup",
    )
    content = plan.artifacts[0].content.replace("UTF-16", "UTF-8").replace("  <", "    <")
    snapshot = InstalledStartupSnapshot(True, True, (content,), True)
    assert inspect_startup(plan, snapshot).healthy
    changed = replace(snapshot, definitions=(content.replace("--forever", "--different"),))
    assert not inspect_startup(plan, changed).healthy


def test_startup_apply_rechecks_state_after_confirmation(tmp_path: Path, monkeypatch) -> None:
    service = runtime.StartupService(_context(tmp_path))
    state = InstalledStartupSnapshot(False, executable_exists=True)
    monkeypatch.setattr(runtime, "inspect_startup_snapshot", lambda _: state)
    applied = []
    monkeypatch.setattr(runtime, "apply_startup_operation", lambda *args: applied.append(args))
    preview = service.preview(operation="enable")
    state = InstalledStartupSnapshot(
        True, enabled=True, definitions=("different",), executable_exists=True
    )
    with pytest.raises(ConflictError, match="changed after preview"):
        service.apply(preview, confirmed=True)
    assert applied == []


def test_shared_startup_executor_writes_only_planned_artifact_and_uses_argv(
    tmp_path: Path, monkeypatch
) -> None:
    spec = runtime.context_watcher_process(_context(tmp_path))
    plan = generate_startup_plan(
        spec,
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=tmp_path / "startup",
    )
    inspection = inspect_startup(plan, InstalledStartupSnapshot(False, executable_exists=True))
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        assert kwargs["shell"] is False
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    result = runtime.apply_startup_operation(plan, plan_enable_startup(plan, inspection))
    assert result["applied"] is True
    assert commands == [
        list(command.argv) for command in (*plan.create_commands, *plan.enable_commands)
    ]
    assert plan.artifacts[0].path.read_bytes().startswith(b"\xff\xfe")
    assert plan.artifacts[0].path.read_text(encoding="utf-16") == plan.artifacts[0].content


@pytest.mark.asyncio
async def test_settings_opens_startup_preview_and_cancel_never_applies(
    tmp_path: Path, monkeypatch
) -> None:
    from textual.widgets import Button

    from gucmail.tui.app import GUCmailApp
    from gucmail.tui.area_common import ConfirmationScreen
    from gucmail.tui.settings_screen import SettingsScreen
    from gucmail.tui.startup_screen import StartupScreen

    context = _context(tmp_path)
    observed = InstalledStartupSnapshot(False, executable_exists=True)
    monkeypatch.setattr(runtime, "inspect_startup_snapshot", lambda _: observed)
    applied = []

    def apply_operation(plan, operation):
        nonlocal observed
        applied.append(operation)
        observed = InstalledStartupSnapshot(
            True, True, tuple(item.content for item in plan.artifacts), True
        )
        return {"applied": True}

    monkeypatch.setattr(runtime, "apply_startup_operation", apply_operation)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(140, 50)) as pilot:
        app._open_area("settings")
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, SettingsScreen) and app.screen.is_mounted
            ) and "monitoring.start_at_login" in app.screen._settings_by_id:
                break
        app.screen.action_select_setting("monitoring.start_at_login")
        await pilot.pause()
        app.screen.query_one("#settings-managed", Button).press()
        for _ in range(80):
            await pilot.pause(0.025)
            if (
                isinstance(app.screen, StartupScreen) and app.screen.is_mounted
            ) and app.screen.current is not None:
                break
        assert isinstance(app.screen, StartupScreen)
        assert app.screen.current.inspection.installed is False
        screen = app.screen
        screen.query_one("#startup-enable", Button).press()
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted:
                break
        assert isinstance(app.screen, ConfirmationScreen)
        assert applied == []
        await pilot.press("escape")
        assert app.screen is screen
        assert applied == []

        screen.query_one("#startup-enable", Button).press()
        for _ in range(80):
            await pilot.pause(0.025)
            if isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted:
                break
        await pilot.press("y")
        for _ in range(80):
            await pilot.pause(0.025)
            if app.screen is screen and screen.current.inspection.healthy:
                break
        assert len(applied) == 1
        assert screen.current.inspection.healthy
        preference = context.state.get_ui_preference("preferences", scope="startup")
        assert preference is not None and preference.value["startup_delay"] == 30


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16"])
def test_disabled_windows_task_is_not_mistaken_for_enabled_trigger(tmp_path, monkeypatch, encoding):
    plan = generate_startup_plan(
        runtime.context_watcher_process(_context(tmp_path)),
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
    )
    xml = (
        plan.artifacts[0]
        .content.replace("UTF-16", encoding)
        .replace("<Settings>", "<Settings><Enabled>false</Enabled>")
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=xml, stderr=""),
    )
    snapshot = runtime.inspect_startup_snapshot(plan)
    assert snapshot.installed and not snapshot.enabled


def test_generated_send_scheduler_binds_custom_queue_and_entry_paths(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    import gucmail.cli as cli
    from gucmail.scheduler_entry import main as scheduler_main
    from test_cli import _environment

    result = CliRunner().invoke(
        cli.app, ["--json", "scheduler", "install"], env=_environment(tmp_path)
    )
    assert result.exit_code == 0, result.output
    arguments = json.loads(result.stdout)["data"]["arguments"]
    assert arguments[1:3] == ["-m", "gucmail.scheduler_entry"]
    assert arguments[arguments.index("--data-dir") + 1] == str((tmp_path / "data").resolve())
    invoked = []
    monkeypatch.setattr(cli, "app", lambda **kwargs: invoked.append(kwargs))
    for key in (
        "GUCMAIL_CONFIG",
        "GUCMAIL_CONFIG_DIR",
        "GUCMAIL_DATA_DIR",
        "GUCMAIL_CACHE_DIR",
        "GUCMAIL_LOG_DIR",
    ):
        monkeypatch.setenv(key, "stale-parent-path")
    scheduler_main(arguments[3:])
    assert os.environ["GUCMAIL_DATA_DIR"] == str((tmp_path / "data").resolve())
    assert invoked[0]["args"] == ["--non-interactive", "--quiet", "scheduler", "run-once"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("ERROR: The system cannot find the file specified.", "No startup task is installed."),
        ("ERROR: The system cannot find the path specified.", "No startup task is installed."),
        ("ERROR: Access is denied.", "Access is denied"),
    ],
)
def test_missing_startup_task_is_normal_but_permission_failure_is_preserved(
    tmp_path: Path, monkeypatch, error: str, expected: str
) -> None:
    plan = generate_startup_plan(
        runtime.context_watcher_process(_context(tmp_path)),
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=error),
    )
    snapshot = runtime.inspect_startup_snapshot(plan)
    assert not snapshot.installed
    assert expected in snapshot.detail
